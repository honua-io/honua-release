# Upgrading a Honua deployment between releases

This is the customer procedure that `gate-upgrade.yml` certifies. Docker Compose on-prem is a GA
topology for 2026.1 ([2026-10-25 founder re-tier](https://github.com/honua-io/honua-release/issues/376#issuecomment-6407009748)),
so its upgrade and rollback (section 6) is the **required** runtime proof of the upgrade gate
(founder decision D9, 2026-10-26, [honua-release#950](https://github.com/honua-io/honua-release/issues/950)).
Helm is Preview: the Helm steps below are what the `kind-upgrade` cells run as **Preview evidence**;
they still run and publish receipts, but no longer decide the gate. The `published-release` kind pair
starts from the image and chart a customer of the newest published release runs, then moves to the
next release candidate. The canonical server reference is honua-server
[`docs/guides/deploy/upgrade-and-rollback.md`](https://github.com/honua-io/honua-server/blob/trunk/docs/guides/deploy/upgrade-and-rollback.md).
This page applies it to concrete releases.

## 1. Identify the exact release artefacts

Take every reference from the release, never from a floating tag:

- **Server image.** Use the digest in the release's `finalized-manifest.yaml` (and its signed
  `platform-lock.json` once releases carry one), for example
  `ghcr.io/honua-io/honua-server@sha256:…`. Deploy by digest.
- **Chart.** Use the `honua-helm` revision the same manifest pins (`components.honua-helm.sha`).
- **Schema.** Use `components.honua-server.dbSchema`, the migration number the release requires.

**2026.1 note.** The `honua-2026.1` GitHub release is a published **pre-release snapshot**, not a
GA cut. Its notes name the tag `ghcr.io/honua-io/honua-server:nightly-aot-f897700`, and **that
tag has since moved** (honua-release#776). Deploy only the digest its `finalized-manifest.yaml`
recorded: `ghcr.io/honua-io/honua-server@sha256:78e3088d64d832d3e2752c87d80bfcad201b414f4525989ca5d9a242cd5fee8a`
(schema 106, chart `4c0a5d2d`). GHCR has no release-named tag for it.

## 2. Back up the database

Take a backup before every upgrade, for example with
`pg_dump -Fc -d honua -f pre-upgrade.dump`, or with `Database__MigrationSafety__BackupCommand`.
It is the only rollback path across a contract migration, or onto an image older than
schema 135 (section 5).

## 3. Upgrade

```sh
helm upgrade honua <chart-at-release-sha> \
  --set image.repository=ghcr.io/honua-io/honua-server --set image.tag='' \
  --set image.digest=sha256:<release digest> -f your-values.yaml --wait
```

**Contract-phase migrations need a one-shot approval.** From 2026.1 to the next release, the
migrations include reviewed contract scripts (for example `106_RetainAlertEvidenceAfterRuleDeletion`,
`108`, `109`, `111`, `115` and `124`). With `Database:MigrationSafety:ContractApplyPolicy=Gate` on
an existing database, the new server refuses to apply them unattended. The pod logs a typed refusal
that names the scripts and prints a nonce bound to exactly those scripts:

```
Migration safety gate blocked pending contract-phase migration(s) on an existing database: …
… then set 'HONUA_APPROVE_CONTRACT_MIGRATIONS=<nonce>' to apply them …
```

1. Read the nonce from the pod log:
   `kubectl logs -l app.kubernetes.io/instance=honua | grep -o 'HONUA_APPROVE_CONTRACT_MIGRATIONS=[0-9a-f]*'`.
2. Review the scripts. `GET /api/v1/admin/deploy/preflight` and
   `GET /api/v1/admin/observability/migrations` on a running server list them.
3. Upgrade again with the approval:
   `helm upgrade … --set-string config.env.HONUA_APPROVE_CONTRACT_MIGRATIONS=<nonce>`.

The nonce approves only those scripts. A later contract migration needs a new review and a new
nonce. The gate classifies the first refusal as `contract-approval-required`, which is the expected
customer step. Only a failure **after** approval is a defect.

**Migrate-only (out of band).** Releases that contain honua-server#6479 accept
`HONUA_MIGRATE_ONLY=true`. The server applies the migrations under the same gates (nonce, backup
hook, preflight), prints one `honua.migrate-only/v1` JSON line and exits with a typed code: `0`
done, `5` contract approval required (the line carries the nonce), `7` backup failed, `8` schema
newer than binary, and so on. Run it as a one-shot Job or `docker compose run`, then serve with
`HONUA_SKIP_MIGRATIONS=true`. The current candidate predates #6479, so the gate's receipts record
`migrateOnly: not used` until a release candidate contains it.

## 4. Verify

Check the following after the upgrade:

- `public.schema_versions` advanced, and the highest script equals the release's `dbSchema`.
- `/healthz/ready` is healthy and `helm test` passes.
- Your own data still serves: the gate checks seeded GeoServices rows.

## 5. Roll back: the boundary

An image rollback (`helm rollback`) keeps the migrated database. Whether the older image may
serve it depends on that image.

**`honua-2026.1` is not a safe rollback target across any migration, and the only safe way back to 2026.1 is restoring the backup taken before the upgrade.** Its server image (honua-server
`f897700`, schema 106) predates the schema-reader guard (honua-server#5333), so it starts on any
newer schema, including contract-phase migrations, and serves with no refusal
(honua-server#6484). There is no release-named immutable tag for that image yet
(honua-release#776). When the restore needs the 2026.1 server, pin the digest recorded in that
release's `finalized-manifest.yaml`
(`ghcr.io/honua-io/honua-server@sha256:78e3088d64d832d3e2752c87d80bfcad201b414f4525989ca5d9a242cd5fee8a`).
Do not pull `nightly-aot-f897700`; that tag has moved.

Roll back only to images that include the schema-reader guard: schema 135 or later for additive
migrations, and never across a contract-phase migration without a restore.

- **Older image at schema 135 or later** (honua-server#6478). It starts on a database migrated by a
  newer image only when every migration it does not ship is expand-only, and when it can verify
  that itself. Otherwise it refuses.
- **Older image below schema 135 that still has the schema-reader guard.** It refuses **any**
  migration it does not ship. It fails startup with the typed `schema_reader_incompatible` error
  and never serves silently. This does not include 2026.1, which has no guard.
- **Across an approved contract migration.** No older image can serve the narrowed schema
  correctly. Restore the pre-upgrade backup.

The gate probes every prior image with an unrecognised journal entry. For 2026.1 it reports
`rollback-target-ineligible`. That cell is a failure, not a passing rollback: "the old image
still serves" proves nothing. The published-release pair stays advisory (honua-release#376), so
the finding is reported and does not by itself change `overallStatus`. The gate still executes
the restore and records it (`certifiedRollbackPath: restore-pre-upgrade-backup`,
`restoreVerified`). The cell stays failed even when the restore checks out.

For a guarded image below schema 135, or after an approved contract migration, the documented
recovery is: stop the server, restore the pre-upgrade backup into a fresh database, and start
the prior image. The gate does exactly this. It records
`rollback.outcome: restored-from-pre-upgrade-backup` and the boundary it met, and it fails if the
prior image then cannot serve the data. An image that refuses without the typed error, or a
schema-135+ image refusing an expand-only edge, fails the gate.

## 6. Docker Compose (GA): upgrade and roll back

The required `compose-upgrade` job of `gate-upgrade` runs exactly these steps on a GitHub-hosted
x86_64 runner (`e2e/harness/upgrade-chaos.sh`, scenario `compose-upgrade-rollback`, on
`e2e/harness/compose.candidate.yml`). The same steps apply to the customer quickstart
`compose.yaml` ([CUSTOMER-INSTALL.md](CUSTOMER-INSTALL.md)), whose server service is named `honua`
instead of `server`. Sections 1, 2, 4 and 5 apply unchanged; only the commands differ. 2026.1 is
x86_64 only: pull and run `linux/amd64` images.

1. **Record the image you run now.** It is your rollback target. Read it from your `compose.yaml`
   or `.env` (`HONUA_SERVER_IMAGE=ghcr.io/honua-io/honua-server@sha256:…`), never from a tag.
2. **Pull the new digest.** Take it from the release (section 1) and pull it before you stop
   anything: `docker pull --platform linux/amd64 ghcr.io/honua-io/honua-server@sha256:<new>`.
3. **Back up the database** (mandatory, section 2):
   `docker compose exec -T db pg_dump -U honua -d honua -Fc > pre-upgrade.dump`.
   Check that the file is not empty. Keep it until the new release has served for a while.
4. **Migrate.** Set `HONUA_SERVER_IMAGE` to the new digest and recreate only the server:
   `docker compose up -d --wait --force-recreate --no-deps honua`. The server applies its migrations
   at startup. If the release contains reviewed contract-phase migrations, the container exits with
   the typed refusal from section 3; read the nonce with
   `docker compose logs honua | grep -o 'HONUA_APPROVE_CONTRACT_MIGRATIONS=[0-9a-f]*'`, review the
   scripts, add `HONUA_APPROVE_CONTRACT_MIGRATIONS: "<nonce>"` to the server's `environment` (an
   override file works), and recreate the server again. Remove the approval afterwards; it is one-shot.
   Releases with honua-server#6479 can migrate out of band first with
   `docker compose run --rm -e HONUA_MIGRATE_ONLY=true honua`.
5. **Verify** (section 4): `/healthz/ready` answers 200, `public.schema_versions` gained rows and its
   highest script equals the release's `dbSchema`
   (`docker compose exec -T db psql -U honua -d honua -Atc "SELECT max(scriptname) FROM public.schema_versions"`),
   and your own data still serves.
6. **Roll back to the prior image.** Set `HONUA_SERVER_IMAGE` back to the digest from step 1 and
   recreate the server. The database stays migrated. A guarded prior (schema 135 or later) serves an
   expand-only edge; if it exits with the typed `schema_reader_incompatible` refusal at the documented
   boundary (section 5), or after you approved contract migrations, **restore** instead:
   ```sh
   docker compose stop honua
   docker compose exec -T db psql -U honua -d postgres \
     -c 'DROP DATABASE honua WITH (FORCE)' -c 'CREATE DATABASE honua OWNER honua'
   docker compose exec -T db pg_restore -U honua -d honua --no-owner < pre-upgrade.dump
   docker compose up -d --wait honua   # HONUA_SERVER_IMAGE = the prior digest
   ```
   Then verify again through the prior image: it is ready and serves your data.

**What the gate asserts, and what fails it.** The `compose-upgrade` cell installs the prior on a
fresh database, runs the real e2e seed, probes the prior's schema-reader guard, takes the backup,
upgrades to the candidate (answering a contract-migration refusal with its nonce), and then requires:
the journal advanced with every prior entry kept, the observed schema equals the candidate's declared
`dbSchema`, and the seeded rows (`e2e_src_fs`=2, `maui_zoning`=12) survived with unchanged
checksums. It rolls back to the prior image, or restores the backup at the documented boundary, and
requires the prior to serve both seeded features with the checksums still unchanged. It applies
kind-upgrade's fail-closed rules: an unguarded prior is `rollback-target-ineligible` (a failure even
when the restore works), a guarded prior that starts on a contract-narrowed schema fails, a refusal
outside the documented boundary fails, and the running container must be exactly the expected
`linux/amd64` digest at install, upgrade and rollback. The receipt is
`compose-upgrade-receipt.json` (`honua.upgrade-compose-receipt/v1`, `certification.verdict:
required`) in the `upgrade-compose-evidence-normal` artifact.

**Which release is "prior".** 2026.1 is the first release, so the gate defines it explicitly
(`tools/compose_upgrade_prior.py`): the honua-server digest published by the newest `2026.1-rc.N`
GitHub release, else the amd64 image of the newest signed nightly lock
(`refs/tags/nightly-lock/2026.1-rc.N`, keyless signature verified). When neither exists, ruling
`compose-upgrade-prior-nightly-2026-10-27` (founder decision 2026-10-27) uses the newest published,
certified nightly honua-server image older than the candidate. That is the first `nightly-<sha7>` image,
walking honua-server trunk back from the candidate's commit, whose commit has a green full-matrix
`ci.yml` (for example `nightly-08ace24`). It is resolved by its linux/amd64 digest. Once rc.1 is
published, the rc rule applies again. An rc, lock or nightly that ships the candidate's own image or
commit is refused. When no source yields a different immutable digest, the cell is `blocked` (a strict
train fails it); it never falls back to the candidate, a floating tag or a manifest-history pin. Multi-tenant deployments are out of scope (ADR-0024: internal only).

**Negative control.** On the schedule a second cell, `negative-control`, runs the same lifecycle with
`COMPOSE_UPGRADE_FAULT=drop-seed-rows` and passes only if the gate's own assertion ("seeded rows did
not survive the upgrade") fails the harness. The harness also supports `journal-stall` and
`rollback-unreadable`; `e2e/test_upgrade_chaos.py` proves each fails through its own assertion.

## Where the evidence is

The required runtime evidence is the GA Docker Compose cell `upgrade-compose-normal` and, on the
schedule, `upgrade-compose-negative-control` (section 6). Everything below is **Preview evidence**
from Helm on kind: every `upgrade-kind-*` fragment is advisory and does not change `overallStatus`
(founder decision D9, honua-release#950).

`gate-upgrade` (scheduled daily) runs `upgrade-kind-published-release-normal` and uploads
`published-release-upgrade-receipt.json`. The receipt records `priorSource: published-release`,
the release tag and its pre-release flag, the exact public image reference, the contract-approval
steps with the nonce and gated scripts, the backup, and the rollback outcome. For 2026.1 rc this
pair is **advisory** (`certification.verdict: advisory`) until a real release exists: the only
published artefact is the August 2026.1 snapshot, which has no schema-reader guard (coordinator
ruling, honua-release#376). The `trunk-candidate` kind pair (previous trunk pin → candidate →
rollback, plus the fault scenarios) was the required rc path under the 2026-10-22 ruling; under D9
it is Preview evidence like every kind cell (`certification.role: trunk-candidate-path`,
`verdict: advisory`, `tier: preview`), and it is never a customer release pair. Neither pair has a signed prior lock yet
(`certifiedReleasePair: false`), so the signed-lock fragment stays `blocked` until the first
promoted release.
