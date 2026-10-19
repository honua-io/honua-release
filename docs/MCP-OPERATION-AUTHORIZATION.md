# MCP operation authorization qualification

The release-owned `certification/sources/mcp-operation-auth-policy.v1.json`
assigns an explicit decision to all 41 governed operations. Both canonical
clients retain all 82 rows and the positive, negative and media-schema facets.
Each row now also requires the auth facet. Missing, unknown, duplicate or
contradictory decisions stop generation and validation.

`initialize`, `ping`, the default `tools/list` and `resources/templates/list`
are public transport operations. Their positive requests must run without
credentials in fresh anonymous sessions. A privileged full tool catalog is
not an anonymous discovery result.

Every `tools/call` and `resources/read` row uses
`mcp-protected-operation-v1`. The server authenticates these methods before
tool dispatch or resource URI lookup. The source reference in the policy
explains the decision; it does not certify a selected server image.

For each protected operation and each canonical client, the auth facet must
retain an authenticated successful operation and a fresh anonymous request
for that same operation which the server refuses. The anonymous process must
receive no creator credentials and must initialize its own session. Reusing
an authenticated session ID or forwarding a key invalidates the negative.
The producer must also demonstrate the server's caller/session binding: an
operation with a session belonging to a different identity must not inherit
that session's authority. A client-side tool-discovery refusal does not prove
that the server received and rejected the protected operation.

The client name and installed version, selected producer SHA, server source
and image digest, request method/tool or resource, response, credential mode
and session identities must stay bound to the original observations. Keep
credential values private; publish only the reviewed observer's safe identity
bindings. A shared `list_layers` auth probe, source-level dispatcher review or
an assertion copied into another tool's receipt cannot satisfy these rows.

The policy and catalog alone do not implement those producer observations.
The existing producer explicitly rejects an unsupported policy or facet set;
it must keep doing so until its actual SDK and Inspector adapters supply the
corresponding proof. Release #695 remains open for producer adoption and
fresh exact-candidate qualification. The map-package slice under #693 and
geospatial-mcp#110 covers only its named operations.

## Source-coherent client selection

This change preserves the frozen historical producer revision and dependency
snapshots. The maintained `NIGHTLY-STAGE` path in `tools/convergence_rebind.py`
fetches both `certification/package.json` and `certification/package-lock.json`
from the same selected MCP producer SHA. The required manifest workflow
authenticates both literal snapshots against that source. Generation checks
the direct declaration, lock root, installed entry, registry URL and SHA512
integrity for each canonical client.

A source selection must advance through that maintained path. Independent
dependency-update PRs cannot establish a coherent selected producer cohort.
Historical SDK 1.30.0 / Inspector 2.3.0 requirements do not admit observations
from SDK 1.32.1 / Inspector 2.10.1. Adding an auth policy does not change that
rule, advance a candidate pin or confer release credit on older receipts.
