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
