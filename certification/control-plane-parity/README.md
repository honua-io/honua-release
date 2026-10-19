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

An additional `parity_final_route=true` input requests bounded private-route proof.
Its default is false, preserving the current 0.1.16 replay and its historical
receipts. Enabling it does not override package versions: the same manifest rows,
public artifact verifier and installed identity checks remain mandatory. Before
any command mutation, actual installed SDK and MCP schemas must expose the typed
route/audience subset, and a real MCP dry run must accept the intent and refuse
unknown authority, audience and relative-route inputs without any server call.
Unsupported published clients fail admission; no future-version stand-in runs.

The unique personal route then passes through all four publication adapters. It
must be absent before approval. After the separate fixture principal approves,
the server's returned active URL and publication URL must name the expected
same-origin resolver and return the approved item, version, publication, content
hash and exact immutable envelope. Anonymous and foreign-tenant reads must be
refused. Redirects and foreign origins are rejected before bearer forwarding.
This optional continuation addresses only the final route/readback portion of
step 8. It does not supply human approval acceptance or the missing installer,
catalog, layer/access, style, GP and full map-adapter stages listed above.

`parity_terminal_draft=true` separately admits the actual published terminal's
`studio draft save-version` dry run before any owned command mutation. It uses
the same verified manifest pair with no package override. A client without that
verb fails admission; the default retains the older three-adapter control and
cannot be counted as four-adapter tenant proof.

When admitted, CLI, SDK, Studio and MCP receive the identical owned draft ID,
generation and authenticated foreign-tenant principal. All four must return the
same typed status, correlation and idempotency identity. Because the existing
shared command checks the generation first, this is specifically a refusal of
its authorized draft pre-read: four persisted, hashed `studio.read_draft` denial
rows must identify the same foreign actor, owned draft and cross-tenant policy.
No accepted mutation handle is invented for those refusals. The legitimate owner
must still read the original generation, envelope and nonempty public owner ID afterward.
The four-adapter mode also reads the actual PostgreSQL draft row before and after
the refusals and requires its owner and tenant binding to match the authenticated
owner. The wire draft intentionally does not expose `tenantId`. The
operator-bearer audit actor oracle follows the canonical S4 issuer and server
scheme/subject normalization; token decoding alone never authenticates a caller.
