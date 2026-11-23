# Qualify the rc.2 to GA forward upgrade

The `2026.1.0` qualification source is the published `2026.1.0-rc.2` quickstart.
This is a required source and evidence contract, not a declaration that a candidate
has passed. Retain the installed run before publishing support for the pair.

## Select the artifacts

Use the published rc.2 release's finalized manifest and immutable server digest.
The release resolver accepts `2026.1.0-rc.2` and legacy `2026.1-rc.2` tags, with
optional `honua-` prefixes. It refuses drafts, other RCs, nightly substitutes and
an image identical to the candidate. Missing published rc.2 evidence blocks GA
qualification.

Select the GA candidate through its signed platform lock. Retain both manifests,
the lock digest and the prior and candidate image digests with the run URL.
Install the shipped rc.2 quickstart on a clean host; the Compose harness alone
proves its own topology and cannot prove that the shipped package installs.
Use the package's documented service names and persistent volumes.

## Populate, upgrade and observe

Back up the populated database before changing the server. Preserve the original
volumes and configuration. Before upgrading, publish a service with feature
attributes and geometry, save a map draft and immutable content version, store a
working database connection and mint an API key. Record their identities privately.
Do not recreate these resources after the upgrade to make a check pass.

Upgrade to the selected candidate digest using the package's documented procedure.
Migrations are forward-only and must be idempotent. Test startup again on the
installed migrated stack and confirm that already applied migrations do not
change the schema journal or application state. The Compose receipt also requires this restart proof for its topology. The
installed quickstart run must observe it separately; neither substitutes for the other.

The required normal Compose cell retains `compose-upgrade-receipt.json`:

- `versionEvidence` binds the actual candidate container, immutable image and
  `linux/amd64` platform. Runtime and OCI versions must both be `2026.1.0`.
- `credentialEvidence` authenticates with the API key minted before the upgrade,
  compares its metadata and checks the unchanged stored connection and password.
- `mapEvidence` compares the saved draft and immutable map version without rewriting
  them, and requires the durable map store.
- `serviceEvidence` compares published layer metadata and every seeded feature's
  attributes and geometry. The populated source contains exactly two features.
- `forwardEvidence` requires a pre-upgrade backup and the restarted candidate
  identity, readiness, unchanged migration journal and seeded rows, and repeated
  credential, map and published-service checks. The legacy harness scenario name
  `compose-upgrade-rollback` is retained for receipt consumers; it never starts the
  prior image on the migrated database.
- The schema journal retains prior entries, reaches the declared candidate schema
  and the seeded database checksums remain unchanged.

Retain only sanitized receipts and hashes. Private state contains credential
material and must be deleted without uploading it. Faults, missing evidence and
version mismatches must refuse admission. Python self-tests establish validator
behavior; they do not constitute installed qualification.

The server image reports its runtime version as `2026.1.0+<sha7>`. The build suffix
is accepted only when it names the candidate's own source revision; the OCI label
carries no suffix.

Console, Studio, CLI and documentation version surfaces require separate installed
observations bound to the same platform lock. Component package versions keep
their own version policy, so those surfaces report the platform version in a
separate `platformVersion` field. `tools/platform_version_surfaces.py compare --label
<lock label> --observed <file>` compares the server capabilities and admin version
endpoints, the OCI label and the Console, CLI and docs `platformVersion` fields, and
fails when any surface is missing or reports another version. Its `collect` command
builds the observations from the installed candidate; pass the admin API key in
`HONUA_API_KEY`, which is never written to the observations. The server receipt
does not certify those surfaces.

## Recovery

There is no rolling rollback or N-1 database-reader compatibility contract. Do not
start an older server against the migrated database. A recovery decision uses a
reviewed fix forward or restores the pre-upgrade backup separately with its matching
immutable artifacts. Historical rollback checks are not GA acceptance criteria.
