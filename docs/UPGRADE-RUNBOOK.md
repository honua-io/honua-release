# Upgrading a Honua deployment between releases

This is the customer procedure that `gate-upgrade.yml` certifies. The headline
`published-release` pair runs these exact steps on every scheduled run: it starts from the image
and chart a customer of the newest published release runs, then moves to the next release
candidate. The canonical server reference is honua-server
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
GA cut. Its notes name `ghcr.io/honua-io/honua-server:nightly-aot-f897700`. Use the digest
`sha256:78e3088d64d832d3e2752c87d80bfcad201b414f4525989ca5d9a242cd5fee8a` from its
`finalized-manifest.yaml` (schema 106, chart `4c0a5d2d`). GHCR has no release-named tag for it.

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
serve it depends on that image:

- **Older image at schema 135 or later** (honua-server#6478). It starts on a database migrated by a
  newer image only when every migration it does not ship is expand-only, and when it can verify
  that itself. Otherwise it refuses.
- **Older image below schema 135 (this includes 2026.1 at 106).** It refuses **any** migration it
  does not ship. It fails startup with the typed `schema_reader_incompatible` error and never
  serves silently.
- **Across an approved contract migration.** No older image can serve the narrowed schema
  correctly. **2026.1 predates the schema-reader guard.** After `helm rollback` it starts and
  answers on the narrowed schema with no refusal. The gate observed this in run 39724980788
  (`imageRollbackServedSilentlyAcrossContract: true`). Do not treat that as a working rollback.
  Restore the backup.

In the last two cases the documented recovery is: stop the server, restore the pre-upgrade
backup into a fresh database, and start the prior image. The gate does exactly this. It records
`rollback.outcome: restored-from-pre-upgrade-backup` and the boundary it met, and it fails if the
prior image then cannot serve the data. An image that refuses without the typed error, or a
schema-135+ image refusing an expand-only edge, fails the gate.

## Where the evidence is

`gate-upgrade` (scheduled daily) runs `upgrade-kind-published-release-normal` and uploads
`published-release-upgrade-receipt.json`. The receipt records `priorSource: published-release`,
the release tag and its pre-release flag, the exact public image reference, the contract-approval
steps with the nonce and gated scripts, the backup, and the rollback outcome. The
`trunk-candidate` pair (previous trunk pin → candidate) is labelled `certification.role:
scaffolding` and is never a customer release pair. Neither pair has a signed prior lock yet
(`certifiedReleasePair: false`), so the signed-lock fragment stays `blocked` until the first
promoted release.
