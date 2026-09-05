# PostgreSQL restore seam and full-platform DR gate

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
  substrates:
    postgresql: true
    redis: true
    object-storage: true
    job-queue: true
    transactional-outbox: false
    workflow-cursors: false
```

This is an illustrative configuration, **not a claim about the current candidate**.
Use `false` only when the candidate disables that substrate. Local referenced-output
files still count as `object-storage`; sharing Redis or PostgreSQL does not remove
logical job-queue, outbox, or workflow-cursor recovery obligations when enabled.
No alert delivery, multi-tenant, or offline-sync journey is added by this inventory.

A `honua.dr-drill-receipt/v2` receipt must carry:

- `scope: full-platform`, `status: pass`, the candidate's `topology`, and
  `candidateLockDigest` (SHA-256 of the exact manifest/lock bytes supplied to the gate).
- `startedAt`, `completedAt`, and finite nonnegative `measurements.rpoMs` / `rtoMs`.
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
execution of recovery; the gate also verifies GitHub producer attestation. The JSON
files in `tools/fixtures/dr` are synthetic rejection-test inputs, never qualification receipts.

```powershell
python tools/validate_dr_receipt.py --candidate platform-manifest.yaml --receipt receipt.json
python -m pytest tools/test_validate_dr_receipt.py -q
```

Supply `dr_receipt_url` to the release train, or `receipt_url` when dispatching
`gate-dr`. Scheduled runs use `HONUA_DR_RECEIPT_URL`. Missing URL, attestation,
configuration, enabled substrate, or restart observation is a failure. The current
manifest has no resolved DR deployment inventory and the existing seam cannot
produce a full-platform receipt; they remain unqualified until the deployment owner
records the configuration and the producer executes recovery for all enabled stores.

The PostgreSQL seam retains its existing Linux CI runner. Its detached receipt signature
and GitHub artifact attestation certify only that explicitly scoped seam result.
