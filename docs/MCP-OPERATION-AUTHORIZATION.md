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
corresponding proof. The protocol certification consumer now refuses a passing
MCP cell unless the receipt payload is `honua.mcp-auth-observations/v1` and
binds the cell operation, authorization policy, canonical client, lane, pinned
cohort version, and producer source, including every observation that policy
requires. Each observation names the method, tool, or resource it requested
and carries a response digest bound to that request, the observation name,
and the session. Copying one shared-tool probe and changing only the
top-level operation does not certify the other operation. Anonymous observations cannot use an authenticated credential mode.
A caller identity carries one credential mode across the whole payload: the
authenticated caller reappearing under an anonymous label, or an anonymous
caller presented as authenticated, is refused.
A protected operation's anonymous refusal cannot reuse the authenticated
session, and a different caller on that session must be refused. Non-passing
MCP cells are unchanged. This is a checker control: it does not qualify a
candidate, and missing producer observations stay nonpass. Release #695 remains
open for producer adoption and fresh exact-candidate qualification. The
map-package slice under #693 and geospatial-mcp#110 covers only its named
operations.

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

## Prepare a source-context review

When the producer's default requirements source predates the authorization
policy, actual certification stops before client execution. Prepare a separate
review directory from an exact protected MCP producer revision:

```sh
python3 tools/prepare_mcp_source_context.py \
  --producer-source FULL_PROTECTED_MCP_COMMIT \
  --output /tmp/honua-mcp-source-context-review
```

The command authenticates protected trunk history and fetches literal package
and lock bytes from that same commit. It requires the release ledger to be
explicitly pending and refuses a bound release. It uses the existing release generator
and validator, preserving all original requirements and all 82 MCP rows. Only
the MCP source revision and canonical client versions may change. The review
retains the ordinary strict CI verdict and actual check conclusions, including
failures. Package declarations and lock integrity are source inputs; installed
registry bytes and actual client observations remain required.

The output includes generated catalog/source payloads, the unchanged policy and
checker, and a hash inventory. It does not write source files, a platform
manifest, a candidate or a ledger. Review those payloads through a normal
protected PR before using its exact release commit and requirements revision
in the producer's existing certification inputs. Final source/cohort adoption
and actual qualification remain under #181 and #695. The normal resolver still
refuses a failing source; preparation does not establish candidate admission.
