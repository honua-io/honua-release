# MCP grounding, clarification, and declared release-status producer

This producer covers the six existing `ground_candidates`, `clarify_intent`, and
`platform_release_status` SDK/Inspector rows. It cannot create a new row or
qualify a candidate from source controls. The immutable candidate image,
installed client package, raw MCP wire and initialized session, authenticated
principal, and source dependencies use the same admission as the other
installed-client MCP producers.

The source observer creates separate owned `admin` and `read:*` API keys through
the normal managed-identity fixture, verifies their active Redis records and
effective permissions, and revokes both during cleanup. Their private keys
stay in separate mode `0600` journals. The public receipts bind one salt and
each credential fingerprint to the appropriate MCP wire calls; the admin
fingerprint also binds both preflight reads. The independent judge refuses a
mixed or substituted caller. An ambiguous revocation keeps the affected
private journal under owner custody and is NONPASS.

## Grounding source contract

The existing render bootstrap provisions an owned `sdkreg-area` publication
and reads its canonical `MetadataV2` service/publication/storage binding back
from PostgreSQL. The grounding producer reuses that *actual* readback: it
requires the same candidate, table locator, connection, public service ID,
and storage layer. A source-only fixture name or arbitrary candidate ID cannot
stand in for a readable metadata graph. The producer must also read
`honua://catalog/processes` through an authenticated real client and require
the advertised `geometry.buffer` entry to have a job entry point, asynchronous
execution mode, and the expected process ID. The independent oracle compares
returned ranked dataset/process IDs, names, provenance, and usable candidate
ordering with those two observations. Inaccessible metadata or process catalog
entries must not appear as positives.

An authored goal referring to the owned area and `geometry.buffer` exercises
real catalog grounding. A separate materially ambiguous goal tests a
clarification turn: observe `ground_candidates`, carry its returned `intentId`
and original goal to `clarify_intent`, answer the exact advertised question,
and check the same intent, updated classification, candidate ranking, answered
question provenance, and remaining unanswered questions. Missing or blank
intent IDs, blank answers, malformed choices, anonymous callers, and Catalog
Discover denial must refuse without a success-shaped response. The service is
stateless and cannot authenticate an arbitrary ID against an earlier call; the
producer instead binds the answer to the actual preceding response and verifies
the same ID in the resulting draft. The default engine is
`deterministic`; neither a candidate shortlist nor an answered question proves
model inference or an executable plan.

## Declared status contract

`platform_release_status` returns `McpPlatformOpsReader`'s projection of the
current `ControlPlaneOptions`. A separate authenticated admin preflight read
at `/api/v1/admin/deploy/preflight?includeDiagnostics=true` returns the same
projection through `DeployControlEndpoints`. Compare `releaseDeclared`,
`releaseVersion`, `isCoVersioned`, all safe fields of the serving/execution
entries (`id`, `runtimeProfile`, `effectiveArtifactReference`,
`projectedFromRelease`, `skewed`) and `skewedIds`
under the same inspected candidate before and after MCP invocation. Require
the evidence posture to identify configuration projection, not observed
deployment or health. Reject invalid tool arguments, anonymous callers, and
OpsRead denial. A truthful undeclared release can be a valid source response;
it does not qualify a platform release.

For the scope negative on an owned disposable candidate, the admin API-key
endpoint can mint a short-lived `read:*` key: it authenticates but lacks
`ops:read`. Keep its secret in private process memory, retain only the key ID
and safe refusal, and revoke the owned key in cleanup. An anonymous request
alone does not prove OpsRead enforcement.

The control harness must mutate each bound source dependency, response
semantic, initialized-session/credential identity, and independent source
observation and see refusal. Actual candidate execution remains a later,
separate receipt. Missing bootstrap, catalog, preflight, or client process is
NONPASS.
