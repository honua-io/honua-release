# Canonical Ops findings and event reads

Refs #727 (producer). The thin consumer is geospatial-mcp#128. This work
preserves the original 3,008 requirements and all 82 canonical MCP candidate
rows. Source tests and synthetic controls do not qualify those rows.

The four exact primary operation keys are `tools/call:ops_findings`,
`tools/call:alert_events`, `tools/call:operate_events`, and
`resources/read:Ops findings` (`honua://ops/findings`). Each requires both
distributed canonical clients and the original authorization, negative,
version, fixture, policy, and candidate facets. Supporting reads add no primary
operation credit.

## Owned source data

Use one fresh disposable Production candidate with its governed source,
immutable image, client versions, and authenticated bootstrap unchanged. The
Development/Test Operate fixture API is unavailable on this composition and
must remain unavailable. Provision the fixed `canonical-mcp-ops-v1` read fixture
through the bounded owned SQL setup, like the existing canonical base-schema
seed. Retain its original SQL-response bytes, allocated IDs, database cluster,
source hash, and runtime identity before any client invocation. The complete
setup projection is checked against fixed fields, values, and allocated-ID
types before its original bytes are retained. The canonical public database
password equals its username, `honua`; that substring in an image or schema
name is not an arbitrary secret echo. Unknown fields are refused. Database
credentials remain private inputs to the fixed observer command.

Run this isolated read journey before other candidate journeys can populate
the Operate feed. The observer independently checks the complete durable
event-ID window between the two authored occurrences, retaining at most three
IDs: anything beyond the original two refuses setup instead of silently
excluding an ambient event from the expected timeline. This isolation is a
fixture precondition, not evidence that a deployed shared feed is unsupported.

Both clients reuse that first setup. The setup refuses any existing profile;
it neither deduplicates old rows into a new clock nor repairs a partial fixture.
An independent observer reads both events, their real rule, and lifecycle from
the owned database in one read-only transaction. It retains causal fields and
original timestamps. It does not select credentials, arbitrary payloads, or
encrypted connection details. Restart must preserve these original rows.

The two alerts and resolved lifecycle are explicitly fixture-origin persisted
records. Rules/zones are inactive and channels empty. This covers real query,
filter, lifecycle projection and timeline composition. It does not cover
geofence evaluation, notification delivery, or lifecycle mutation APIs. No
Preview flag, edition, worker, or deployment-mode policy is changed. The
broader alert-engine qualification issue retains its separate scope.

## Required semantic checks

For `alert_events`, require both nonempty original events and compare every
returned event/lifecycle field with independent durable rows. Exercise an
explicit rule filter, severity and lifecycle filters, time bounds, one-row
cursor paging through both events, an empty nonmatching filter, and malformed
arguments. Enforce deterministic occurrence/event-ID ordering and reject
duplicates, substitutions, dropped lifecycle state, or replayed page cursors.

For `operate_events`, select the alert kind and original time interval. Require
the same two rows through the actual server fan-out with the documented
`alert:<eventId>` identity, resource reference, title, rule summary, severity,
scope, and ordering. Verify kind and time filters and the bound on page size.
Inspect partial-result and source-error posture; an unavailable source must
not produce an unqualified complete result. An empty other-kind projection
does not replace the nonempty alert composition.

For both findings surfaces, require a meaningful nonempty finding caused by
independently retained source evidence. The existing Operate alert/log fixture
does **not** itself cause a finding. The deterministic configuration
skew fixture binds the exact declared configuration, independently reads admin
preflight, and labels the result as L1 declared configuration. An empty findings array or a live health snapshot is not a
substitute. Check the finding identity, rule/severity, subject, source
references, safe recommended-action semantics, source completeness, and
observation window. Never equate declared artifact pins with observed deployed
revisions.

Retain raw installed-client request/response observations, discovery and
session joins for all four keys. Verify anonymous refusal and authenticated
permission denial through the real policy path; session/cohort
substitution must fail adjudication. Restart the same owned image without
re-seeding; obtain a new session and show durable alert/lifecycle readback and
freshly computed findings. Preserve the database cluster and original durable
rows across restart. Findings are computed on demand, not persisted snapshots.

Use two normal managed API keys, with exact `admin` and `read:*` grants. The
shared identity prerequisite creates them through the product admin API,
reads effective permissions and independent durable registry bytes, and joins
each salted credential fingerprint to its actual key ID. The final judge
rechecks original request/response bytes, installed-client provenance, unique
sessions, both caller lifecycles, and successful normal revocation. An invalid
key receiving a reauthentication error cannot replace the reader's typed
permission denial. These checks do not claim OAuth scope or tenant isolation;
the original row's remaining policy facets still require their own evidence.

The configuration declaration is derived again from the original protected
platform manifest. Every positive call is bracketed by independent store,
daemon-selected configuration, and authenticated normal deploy-preflight
observations. A continuation is taken from the actual first response and
rejudged against the original rows; an unresolved authored continuation is
never executed. Both clients reuse the first setup even when the first client
already restarted the same container; original timestamps and rows are kept.

## Delivery state

The observer, bounded SQL setup, configuration declaration, 26-call scenario,
semantic and raw-wire judge, installed-client bridge, and synthetic mutation
controls are drafted. Frozen component attempts exited 75 before execution;
no test pass or qualification is claimed. The newer judge/context/bridge bytes
have only Python syntax checks. The shared managed-identity prerequisite must
land before this producer can use its protected bytes. Full SDK/Inspector
positive and adversarial controls, real SQL/Docker/normal-API observations,
workflow selection, and the protected consumer remain required before #727
can close. Follow every PR through review, required validation, normal merge,
and content verification.
