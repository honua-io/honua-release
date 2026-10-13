# Bounded installed SDK setup proof

The opt-in `SDK local setup qualification` workflow exercises the published installer and
published MCP certifier on a fresh hosted Docker runner. Contract tests run on PRs; they
are never installer qualification evidence.

Execute only after a reviewed nightly candidate snapshot and a separately reviewed,
tracked setup catalog manifest are available. The setup manifest belongs under
`certification/sdk-local-setup/manifests/`, uses the published
`honua.setup-catalog-manifest/v1` schema, names **both** exact SDK/MCP package integrities,
and binds `candidateId` to `manifest-sha256:<adopted-platform-manifest-sha256>`.
The workflow takes that file's independently reviewed SHA256. It does not learn an
expected catalog from the running server and then call that a match.

Prepare that expectation with `export_catalog.py` after the final server source,
candidate, and public package pins exist. Pass a clean checkout of the candidate's
exact server commit, the independently checked platform-manifest SHA256, and the
exact shipped immutable installer image string. The exporter verifies the two
public client artifacts, builds the source into a new private directory, and runs
the server test assembly's existing source composition through its canonical
setup projector and JSON serializer. A small reflection adapter fails if those
reviewed source seams change. Full-composition and static setup projections must
agree; a future dynamic setup profile requires an explicit exporter review.

The adapter emits complete schemas, annotations, source metadata and canonical
digest inputs without contacting a server. Two fresh invocations must agree.
Schema, roster, stage, budget and digest controls precede output. The preparation
receipt binds the clean server source/tree, assembly hashes, exporter source and
code hashes, candidate digest, and actual public package verification. Private
build logs are not proof artifacts and must not be uploaded. The new output
directory is never reused or overwritten.

```bash
python certification/sdk-local-setup/export_catalog.py \
  --server-checkout "$SERVER_CHECKOUT" --server-source "$SERVER_SOURCE" \
  --platform-manifest "$PLATFORM_MANIFEST" --platform-sha256 "$PLATFORM_SHA256" \
  --server-image "$SHIPPED_INSTALLER_IMAGE" --output "$NEW_PREPARATION_DIRECTORY"
```

Independently review the emitted `expected-setup-manifest.json` together with
`source-projection.json` and `preparation.json`, then commit the expected manifest
under `manifests/` and pin its SHA256 before the installed replay. An offline
source expectation is not an observed catalog or a passing qualification receipt.
This exporter requires no server production change and does not gate RC server
image publication. It cannot override shipped installer or customer-image pins.

The canonical candidate-input action verifies snapshot lineage. The canonical package
verifier checks registry bytes and published source identity. Installation uses those
verified tarballs once, with normal peer resolution, disabled install lifecycle scripts,
and fresh npm configuration/cache. Child commands use the canonical candidate environment
allowlist; inherited cloud/OIDC/GitHub credentials and npm overrides are excluded.
Before starting Docker, the harness refuses any difference between the package's shipped
installer image/generated MCP pin and the reviewed setup/platform pins. There is no image
override, source rebuild, compatibility waiver, or customer manifest rewrite. The setup
manifest preserves the exact shipped image string, including its optional readable tag,
because the published qualifier compares that string; platform admission and RepoDigests
compare the canonical repository and immutable digest. Advancing
customer-install-manifest.json remains the canonical release mint's responsibility.

The producer then performs clean `gp-dev` installation, idempotent restart without replacing
the generated credential/configuration, configured/running image source verification,
and publication of the installer's own `public.zero_to_map_parcels` seed. Fixture setup
uses the narrowly allowed `env:HONUA_LOCAL_DB_PASSWORD` reference and a reviewed polygon
style. It records a dry-run style application's `applied:false`, then binds a real baseline
style. The published HTTP/stdio catalog certifier and published `honua admin qualify`
command must pass; the latter rejects style no-op, checks the intended style, decodes
visible PNG pixels and reads the server's emitted resource URI. A second actual resource
read retains the byte-identical rendered PNG.

Always-run cleanup targets only the random Compose project created in this run's private
directory. Replay stages have explicit time limits within the job limit, reserving time
for cleanup and evidence upload. Credentialed fixture requests reject foreign origins and
all redirects. The final report fails if any required stage or cleanup is missing. Only the
workflow's explicit public evidence allowlist is uploaded; `.env`, generated MCP credentials,
consumer files and raw command logs remain private and are never uploaded. Every failed
workflow remains part of the run history; no result is rewritten as success.

This receipt covers SDK issue1401's local setup, bounded catalog and style/render path.
Release123,129 and161 must independently consume its candidate/package/tool-view/style
identities and deliver their deterministic, AWS and genuine-model journeys. This harness
never performs or claims publication approval, model-selected execution or a platform
release certification lock. A passing source test or PR is not a live receipt.
