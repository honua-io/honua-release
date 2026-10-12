# Production control-plane engineering replay

This opt-in job exercises the bounded SDK issue 1424 journey against a supported
immutable server image. It is independent of canonical release gates and never
edits platform or customer manifests. A passing result is engineering evidence,
not a certified release or a human approval acceptance receipt.

Dispatch `e2e-local-docker.yml` with `control_plane_parity=true`, an exact
`parity_server_image` including its registry digest, and `parity_server_source`.
Use only the image emitted by the supported nightly workflow after the exact
source passed its full certification matrix. The replay checks the downloaded
digest, OCI source label, and actual running image identity before any proof.

The canonical S4 stack supplies PostGIS, Redis and a real Keycloak authorization
code plus PKCE flow. A small overlay selects Production, persistent storage,
encrypted operation keys and durable audit integrity. Each run creates private
credentials for tenant-a and tenant-b, an ordinary viewer, and a genuine admin
whose IdP claim limits it to read scope. There are no fabricated tenant headers
in command calls. The authentication probe separately checks that an attempted
tenant-header override cannot replace the authenticated identity.

The exact published SDK and independent MCP rows in this checkout's reviewed
`platform-manifest.yaml` are installed with npm's default peer resolution. The
canonical artifact verifier checks registry bytes, SRI and publisher provenance
before runtime startup; installation then checks the same versions, sources and
integrities plus one shared SDK module identity. No override or latest resolution
is accepted. The selected identities and installed lock are retained in receipts.
The real terminal executable, an MCP client/server protocol roundtrip, the Studio
command adapter and the shared SDK execute connection testing, one idempotent
background import, and package publication awaiting approval. SQL readback links
the imported row to the package input; this is explicitly not an SDK published
layer workflow. A separate three-adapter tenant-owned Studio draft control does
not claim a terminal draft verb. Direct HTTP controls cover concurrent imports,
completed retries, changed payloads, role/scope denial and principal isolation.

The publication continuation polls the actual proposal through the terminal and
generated SDK Admin client, rejects self-approval, viewer, read-scope and foreign
tenant attempts, then uses a distinct same-tenant IdP principal for exactly one
terminal approval. It checks terminal success against the persisted operation,
Studio publication request, immutable version/content hash and one hashed
`operation.applied` audit bearing the separate approver. This is an automated
separation-of-duties control, not a real human approval or four-adapter approval
equivalence claim. No REST mutation substitutes for the terminal.

The rate limiter remains disabled as in the canonical S4 fixture, so this replay
does not supply live rate-limit accounting evidence or substitute for the
release-scoped acceptance inventory below.

| Release step | This producer exercises | Still required |
| --- | --- | --- |
| 1. Install | Owned Production fixture | Actual published installer or IaC handoff |
| 2. Handoff | Image identity, real authentication and durable stores | Enabled workflow catalog and proxy handoff |
| 3. Datasource/import/service | Fixture connection, four-adapter test/import | Shared connection create, published layer and access configuration |
| 4. Style/render | None | Canonical published-layer style and returned image/resource |
| 5. GP | None | Discover/run buffer, canonical wait/cancel/results and audit joins |
| 6. Map lifecycle | Package endpoint's saved immutable version and tenant draft refusal | Explicit server draft composition, validate/save/reopen through required adapters |
| 7. Propose/poll | Four-adapter proposal and real terminal/SDK poll | Complete exact-candidate transport contract drift proof |
| 8. Approve/URL | Separate authenticated fixture approver and durable completion joins | Human acceptance, published typed route intent and verified final URL |

The reviewed SDK 0.1.16 shared `map-package.publish` command does not expose the
server's `intent` field. This producer never injects it through a different
mutation endpoint; a version saved without a route cannot prove the final URL.
The typed adapter must carry that field in a later published client before this
remaining criterion can pass. Live candidate execution is still pending.

One unique `sdk1424/<date>/<uuid>/fixture.geojson` object is written to the
configured release fixture bucket, read through a short-lived presigned URL, and
deleted during teardown. Only an explicit receipt allowlist is retained. Private
credentials, certificates, cookies, environment files, and unsanitized logs are
never uploaded; the artifact index records original and retained content hashes.
The JSON report's bounded status and each stage verdict are authoritative. A
failed stage makes the job fail and keeps its diagnostics.
