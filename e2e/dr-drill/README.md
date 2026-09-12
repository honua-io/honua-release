# Full-platform DR drill, PostgreSQL restore seam, and the DR gate

`full_platform.py` is the **full-platform producer**: one run over the whole durable-substrate
inventory `platform-manifest.yaml` declares for the candidate, emitting the
`honua.dr-drill-receipt/v2` receipt gate-dr validates. `run.sh` is the older, narrower
**PostgreSQL restore seam**; it is retained as a separately scoped artifact and its receipt is
still rejected by the full-platform gate, by design. Both run from
`.github/workflows/dr-drill-local-docker.yml`, the signer identity gate-dr pins.

`run.sh` exercises a **PostgreSQL restore seam** using the image digest in
`platform-manifest.yaml`. It backs up PostgreSQL, destroys the original database,
restores into a clean database, compares logical snapshots, then starts the server
and checks the service catalog. Its PostgreSQL fixture jobs are not the production
Redis job store or queue. Its pre-start outbox snapshot does not prove dispatch.
Its receipt is therefore `honua.postgresql-restore-receipt/v1`, scoped to
`postgresql-restore`; the full-platform gate rejects it even when signed.

The existing fixture includes tenant, alert, and sync tables only as PostgreSQL
restore coverage. These do not reinstate GA alerting, multi-tenancy, or offline-sync
journeys: those remain Preview under the 2026-09-04 amendments.

## Full-platform receipt contract

`tools/validate_dr_receipt.py` is the executable receipt contract. Both a scheduled
`gate-dr` run and release-train intake validate a producer-attested receipt. The
train reads `platform-lock.json` from its frozen `candidate-manifest` artifact,
binding every lock field, including artifact identities and the compatibility-matrix
digest. A missing lock fails closed. Standalone runs use `platform-manifest.yaml`.
Promotion also requires a passing `dr` row, so an old report omitting DR is rejected.

Before qualification, the deployment owner must resolve the candidate's effective
configuration (including image defaults, enabled capabilities, deployment overrides,
and worker configuration) into `disasterRecovery` in the candidate manifest. The lock
generator preserves this block in `platform-lock.v1`. Every standard entry must
have an explicit boolean; missing configuration fails closed, with no default to
PostgreSQL-only. Extra named durable stores are supported and required when enabled.

```yaml
disasterRecovery:
  topology: local-docker-single-tenant
  objectives:
    rpoMs: 60000        # maximum tolerated data loss
    rtoMs: 300000       # maximum tolerated recovery time
  substrates:
    postgresql: true
    redis: true
    object-storage: true
    job-queue: true
    transactional-outbox: false
    workflow-cursors: false
```

This is an illustrative configuration, **not a claim about the current candidate**.
`objectives` are deployment-owned limits and are mandatory: without them a producer-reported
measurement has nothing it can fail against, so an absent block fails the gate closed.
Use `false` only when the candidate disables that substrate. Local referenced-output
files still count as `object-storage`; sharing Redis or PostgreSQL does not remove
logical job-queue, outbox, or workflow-cursor recovery obligations when enabled.
No alert delivery, multi-tenant, or offline-sync journey is added by this inventory.

A `honua.dr-drill-receipt/v2` receipt must carry:

- `scope: full-platform`, `status: pass`, the candidate's `topology`, and
  `candidateLockDigest` (SHA-256 of the exact manifest/lock bytes supplied to the gate).
- `startedAt`, `completedAt`, and finite nonnegative `measurements.rpoMs` / `rtoMs`.
  The whole drill must fall within the 24 hours before verification, matching the
  live gate-report age limit. Future completion times fail. Scheduled and release
  runs use the current UTC clock; reissuing a report never refreshes old telemetry.
- Measurements within the candidate's `objectives`. Neither may exceed its limit, so a
  producer-declared `status: pass` cannot green an objectively failed drill. `rpoMs` may be
  zero — zero data loss is a real result — but `rtoMs` must agree with the outage the receipt
  itself records: the window from the earliest `stoppedAt` to the latest
  `readAfterRestart.observedAt`, since recovery ends when the restored state is readable
  again through the runtime surface, not when a process reports ready. The allowance is the
  larger of one second and 5% of that window, which covers producer clock granularity and
  nothing else; a reported zero can never agree with an observed outage.
- A `substrates` object with exactly every enabled candidate substrate. A receipt's
  own purported required-set field has no authority.
- Per substrate, `backup.id`, `backup.sha256`, `primaryStateDestroyed: true`, and
  `restoredIntoCleanStore: true` inside `backup`.
- Per substrate, `restartRecovery` with distinct `instanceBefore` / `instanceAfter`
  identities, `stoppedAt` / `readyAt`, and both `writtenBeforeRestart` and
  `readAfterRestart`. Each observation records `stateId`, SHA-256 of the observed
  state/bytes, positive `count`, `runtimeSurface`, and timezone-aware `observedAt`.
  The state identity, count and checksum must match, and observations must bracket
  the restart within the drill interval. Record the restarted runtime's boot/container
  identity; a repeated hostname or a graceful stop with intact primary state is insufficient.

The producer must write and read through the real substrate/product surfaces and
hash the actual observations. A matching claim is evidence validation, not independent
execution of recovery. The gate verifies the GitHub producer attestation against the
signing workflow identity and the trunk source ref, not the repository alone: a
repository-only check would accept a receipt attested by any workflow here, including one an
unmerged same-repository pull request controls. The producer keeps OIDC and attestation write
out of the job that runs pull-request-controlled drill code, and mints an attestation only off
a pull request. Repoint `--signer-workflow` when the full-platform producer is established.
The JSON files in `tools/fixtures/dr` are synthetic rejection-test inputs, never qualification
receipts.

```powershell
python tools/validate_dr_receipt.py --candidate platform-manifest.yaml --receipt receipt.json
python -m pytest tools/test_validate_dr_receipt.py -q
```

Supply `dr_receipt_url` to the release train, or `receipt_url` when dispatching
`gate-dr`. Scheduled runs use `HONUA_DR_RECEIPT_URL`. Missing URL, attestation,
configuration, objective, enabled substrate, or restart observation is a failure, as is an
attestation from an untrusted signer or ref.

The PostgreSQL seam retains its existing Linux CI runner. Its detached receipt signature
and GitHub artifact attestation certify only that explicitly scoped seam result.

## The full-platform producer (`full_platform.py`)

The drill composes `compose.full-platform.yml`: the candidate's local-docker **install**
topology (PostGIS, Redis, and a local file-storage volume), not the `e2e/local-docker` seam
tier, which composes no durable job/workflow substrate at all. The image comes from the
manifest pin; the substrate set comes from `disasterRecovery.substrates` in the same file.
An enabled substrate with no implemented drill surface fails the run rather than being
quietly dropped from the receipt.

For every enabled substrate the drill writes durable state through a real product surface,
observes and hashes it through a real runtime surface, backs it up through its own supported
path, destroys the primary state (the named volume, not a graceful stop), restores into a
freshly created store it asserts was empty, restarts, and re-reads the identical state:

| Substrate | Product write surface | Runtime read surface | Backup artifact |
| --- | --- | --- | --- |
| `postgresql` | file import (`POST /api/v1/admin/import/upload`) then publication | `GET /ogc/features/collections/{c}/items` | `pg_dump --format=custom` |
| `transactional-outbox` | an OGC API Features insert's outbox row, written in the same transaction | `SELECT` over `honua.feature_change_outbox` | table-scoped `pg_dump` |
| `redis` | the same insert's durable feature-change event | `GET /api/v1/admin/feature-events/replay` | `DUMP`/`RESTORE` slice of `featurechange:*` |
| `object-storage` | GeoServices `addAttachment` | `GET .../attachments/{id}` (the bytes) | `tar` of the storage volume |
| `job-queue` | GeoServices `submitJob` on the Redis-backed job runtime | `GET .../GPServer/{task}/jobs/{jobId}` | `DUMP`/`RESTORE` slice of the job keys |
| `workflow-cursors` | workflow package version published to a `Schedule` target | the durable orchestration definition | `DUMP`/`RESTORE` slice of `orchestration:*` |

Three substrates share one Redis instance and two share one PostgreSQL cluster. Sharing a
store does not merge the recovery obligations: each takes its own backup artifact with its
own id and SHA-256, each is restored from that artifact alone, and the outbox artifact is
additionally replayed into a scratch database to prove it stands up without the cluster dump
carrying it. The workflow package *catalog* does not survive a restart — only the compiled
durable definition does — so the drill reads that definition from the orchestration store
rather than from the in-memory publication list.

**Why the PostgreSQL write is an import, not the transactional insert.** On this candidate an
OGC API Features insert against a published (source-backed) layer answers `201 Created`, writes its
outbox row and publishes its change event — but the row lands in the managed feature store while
the serving protocols read the published source table, so the acknowledged feature is never
readable back (`GET .../items/{id}` answers 404). That is a honua-server defect reported alongside
this drill, not a property of recovery, and the drill must not launder it into a recovery claim.
So the `postgresql` evidence is built on the import path, whose rows the serving protocols really
do return, and the insert is kept only to drive the outbox and change-event substrates, whose
writes are genuinely durable. Revisit this split when the server defect is fixed.

Instance identity is the restarted runtime's boot identity: the PostgreSQL cluster's
`system_identifier`, the Redis `run_id`, and the server's container id. Destroying the
volumes forces all three to change, so a graceful restart over intact state cannot pass.

RPO and RTO are measured, never declared. RPO is the age of the recovery point when the
backup set closed; RTO is the window from the earliest destruction to the latest post-restart
read through a runtime surface — the same window `tools/validate_dr_receipt.py` recomputes
from the receipt's own observations, so a convenient number cannot be substituted.

```powershell
python e2e/dr-drill/full_platform.py                       # writes artifacts/dr-drill-full-platform/
python tools/validate_dr_receipt.py --candidate platform-manifest.yaml --receipt artifacts/dr-drill-full-platform/receipt.json
```

Off a pull request the workflow attests both receipts and publishes the full-platform one to a
commit-pinned `raw.githubusercontent.com` URL under `data/producers/dr-drills/receipts/` in
honua-evidence. That directory is one level below the aggregator's non-recursive
`data/producers/dr-drills/*.json` envelope glob, so a receipt can never be misread as a
`honua-evidence.dr-drill-envelope/v1` envelope. The URL is commit-pinned because a branch URL
would let the bytes behind an already-verified receipt change afterwards.
