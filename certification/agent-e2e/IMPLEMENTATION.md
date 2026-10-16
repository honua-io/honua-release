# Agent E2E implementation and operations

This records the recovered implementation plan and its current delivery boundary.
The full delivery checklist is tracked in
[honua-release #619](https://github.com/honua-io/honua-release/issues/619).
The 36 scenarios and 108 prompts are unchanged. Qualification still requires all
252 cases across Docker, ECS with Redis, EKS, Lambda and Batch. No live cases have
been qualified by the offline tests described here.

## Implemented contracts

| Area | Implemented | Remaining live integration |
|---|---|---|
| Public OKF knowledge | Exact Git revision, graph/bundle/document hashes, allowlisted export, concept resolution, worker manifest binding | Deliver and verify the export on each isolated worker |
| Semantic verification | Fifteen deterministic predicates, protected-content checks, independent raw-response/observation evidence contract | Reviewed API recipes, fixture seeding and response normalization for every supported configuration |
| Goal execution | Model-selected actions, bounded turns/actions/cost, development holdout protection, cleanup accounting | Complete scenario backend for setup, permissions, fault injection, observation and custody changes |
| Worker isolation | Common per-attempt FIFO container transport, bounded terminal execution and persistent duplicate-delivery journal | Pinned image, Fargate/Docker deployment, IAM/network isolation and independent expiry cleanup |
| Durable state | Versioned S3 snapshots, fenced DynamoDB lease, synchronous reservation/event persistence, journal recovery and protection against overwriting newer local charges | Provision controller-only stores; integrate final receipt/registry publication and prove cloud crash recovery |
| Instrumentation | Hash-chained attempt journals, request/trace correlation contracts, JSON diagnostics and atomic Prometheus text output | Scrape/upload wiring, dashboards, alerts and observed server trace artifacts from the live backend |
| Release evidence | v2 gate checks exact knowledge, independent semantics, runtime interfaces, all receipts and candidate spending | Attesting campaign producer workflow and successful live campaign before release activation |

The HTTP recipe runner and worker transport are building blocks, not a complete
backend. There is no fallback to running model-selected commands on the controller.

Semantic evidence retains each exact HTTP response body as base64, its SHA-256,
HTTP status and parsed JSON. Source-before, operation and source-after responses
are ordered and content-addressed. The gate checks the raw bytes against the hash
and parsed body, then replays the saved JSON-pointer projections and compares the
result with the asserted observation. A hash without its original bytes, or an
observation disconnected from the retained responses, cannot qualify. This does
not replace reviewed API recipes, fixture seeding, protected spatial-source
snapshots or live transport observations; those integrations remain open in #624.

Each semantic configuration also requires a controller-collected deployment
observation within the campaign window: Docker inspect; ECS DescribeTasks;
EKS cluster and Kubernetes pod observations; Lambda function configuration; or
Batch DescribeJobs. The gate validates native identity and running/active status
(a completed Batch job is also valid), candidate and target. Every HTTP response
and resulting assertion artifact carries that deployment artifact's digest and
the actual probed endpoint. Relabeling a configuration or reusing another target's
response/observation fails validation. These artifacts must be collected by the
trusted backend; accepting model-supplied identity JSON would defeat this boundary.
Native identity validation alone does not establish image provenance, Redis
configuration, network routing or live API collection; the producer's preflight
and backend still must prove those properties before qualification.

Spatial semantic cases use two concrete EPSG:3857 source layers from
`agent_e2e_semantics.spatial_fixture()`: five points (inside, outer boundary,
hole interior, hole boundary and outside) and two polygons (a square with a hole
and an overlapping square). Both layers must be seeded and observed in their
entirety before and after each spatial operation. The predicates reject changed
holes, moved polygons, missing points/layers and CRS changes even when reported
result IDs still match. The cardinality case selects the inside/outside pair;
contains and boundary cases use all five points. Per-case recipes can capture up
to 16 source HTTP responses before and after an operation and assemble named
source fields through JSON pointers. The gate replays every source projection
from the exact retained bytes. The backend still needs reviewed target-specific
API paths, seeding and normalization recipes; point-only snapshots cannot stand
in for spatial-source protection.

The consumer integration in PR #607 must remain draft until the producer and
runtime evidence exist. This foundation adds no release/nightly/promotion
requirement. Unknown attribution and missing observations fail closed.

## Knowledge and documentation alignment

The public `honua.gis-workflow/v1` language describes typed GIS plans. It does not
install an interpreter, authorize an operation or prove runtime support. Existing
server-authored MCP schemas, HTTP operations, CLI contracts and Honua Spec remain
the execution interfaces. Their observed schema artifacts are separate from the
public documentation graph.

Export public knowledge from an exact 40-character commit, using a destination
that does not already exist:

```powershell
python tools/agent_e2e_knowledge.py --repository C:/path/to/honua-server --revision <40-character-commit> --pack certification/agent-e2e/pack.v2.json --root C:/controller/exports/public-knowledge
python tools/agent_e2e_knowledge.py --root C:/controller/exports/public-knowledge --query "filter shelters and create metric buffers"
```

Freeze the returned knowledge binding in the campaign before running it. The
export reads Git objects, not dirty working-tree files. Each node resolves to a
local document and immutable human-readable source link. The tested worker may
receive only this public export and its selected inputs, never this checkout.

Receipt v2 requires `knowledge`, `workerKnowledge`, `runtimeInterfaces` and
`campaignMode`. The trusted inventory additionally binds `knowledgeManifest` and
`semanticReport`. Updating a document, assertion or receipt version requires a new
campaign binding; old successful receipts cannot be relabeled.

## Measuring success and diagnosing failures

Generate reports from retained controller journals:

```powershell
python tools/agent_e2e_telemetry.py --journals C:/controller/journals --output C:/controller/agent-report.json --prometheus C:/controller/agent-metrics.prom
```

The JSON report includes:

- First-attempt passes and final goal-plus-cleanup passes, with the fixed 252-case
  qualification denominator, missing cases and interrupted attempts.
- Outcomes by scenario, prompt variant and target; development and qualification
  are separated. Documentation revision/digests and concept/workflow IDs identify
  which published knowledge was available.
- Tool errors and categories, clarifications, repeated requests and unchanged
  responses. A possible stall is a diagnostic signal: polling can be legitimate.
- Model turns, executed actions, input/output tokens, observed model cost and
  attempt-duration p50/p95. Model cost is not total AWS liability; consult the
  durable candidate ledger for infrastructure, cleanup and unresolved reservations.
- Failed independent assertions, cleanup outcome, and independently supported
  failure attribution. Backend discovery/document-read events are included when
  observed; absence is not evidence that discovery or reading succeeded.

Prometheus output exposes `honua_agent_attempts_total`, tool-error, token and
model-cost counters, `honua_agent_duration_seconds` histograms, failure-attribution
and cleanup-outcome counters. Labels are bounded target, campaign mode, status
and attribution values. Campaign IDs, prompts, commands, credentials, document IDs
and resource IDs are not metric labels. Retain one consistent journal dataset per
exporter; changing the input set changes these cumulative observations. The CLI
writes a text file; it does not install a collector, scrape job or dashboard.

Use an assertion failure to locate the discrepancy, then inspect independently
captured runtime evidence. An oracle mismatch alone does not establish whether
the model, server or infrastructure caused it. Product attribution requires an
independent runtime reproduction; infrastructure attribution requires an
independent infrastructure probe; invalid model actions have a distinct category.
Otherwise the cause is `undetermined`, which cannot authorize a retry. Raw
commands and provider/backend exception text do not enter the journal.

Server-side MCP instrumentation separately records tool calls, boundary
rejections, resource reads, grounding outcomes and request activity tags. Backend
integration must retain the matching server trace/request identifiers and evidence
artifacts to connect those signals to a controller attempt. Merely having both
sets of counters does not establish that correlation.

The controller journals each returned provider request ID with its model turn.
A model-selected action records an `action-intent` event synchronously before
calling the backend. The event contains only its ordinal, tool kind and request
digest. If durable publication fails, the backend is not called. A crash or lost
reply leaves an unresolved intent; it does not establish whether the command
executed or authorize replay. Completed observations must match a preceding
intent, and a journal claiming success with unresolved actions is rejected.
Reports distinguish action intents, observed results and unresolved actions.
The controller's `usage.executedActions` counts only results acknowledged by
the durable journal sink; attempted dispatches and unpersisted results do not
increase it. A zero count after interruption does not prove that nothing ran.
Prometheus exposes `honua_agent_action_intents_total`,
`honua_agent_actions_observed_total` and the `honua_agent_actions_unresolved`
gauge, with only target/mode labels. These are dispatch-intent counts, not proof
of executed commands. Historical journals lacking intent evidence are retained
for investigation; do not synthesize missing events to make them pass validation.

A backend may emit `runtime-trace` events containing the current action number,
observed `traceId`, `spanId`, `requestId` and the digest of the retained evidence.
The controller rejects events assigned to another action or malformed IDs.
Reports expose `providerRequests` and `runtimeTraces`; these identifiers never
become metric labels. The backend must collect actual correlation evidence: this
contract does not synthesize server spans or prove that a collector received them.
Each report/metrics file is published atomically, preserving the previous complete
file if publication fails. The JSON and metrics files are separate snapshots.

Reports always contain `qualification: false`. Only the independent, attested
complete-campaign gate can qualify a candidate. A final successful retry does not
erase the first failure or its cost from either history.

## Durable execution prerequisites

The controller CLI requires `--state-config` pointing to a JSON object with
`region`, `bucket` and `table`. The S3 bucket must have versioning enabled; DynamoDB
uses the string partition key `candidateKey`. Workers must have no access to either
store. The controller needs `s3:ListBucketVersions` on its candidate snapshot
prefix: a missing DynamoDB pointer may initialize only when no current or prior
snapshot version or delete marker exists. Unreferenced snapshots require explicit
reconciliation, not a fresh budget. Failure to inspect history blocks initialization.
Before its first snapshot, the controller conditionally creates a permanent
`initialization.json` record in that prefix (`If-None-Match: *`). Every snapshot
and DynamoDB update binds its exact version/digest. The conditional publication
closes the race between an empty history probe and an older writer: only one
controller can establish the candidate's accounting history. A failed or ambiguous
publication retains the record and requires reconciliation. Merely reading or
recovering an unknown candidate does not create this record or a fresh ledger.
Runtime identities must have no permission to delete the record, its versions,
or history; a lifecycle rule must not expire them while the candidate is usable.

Existing snapshots without an initialization binding require all older writers
to stop and explicit reconciliation of every retained snapshot and charge. They
are not adopted automatically. This is an internal state-format change; no live
campaign has used the earlier unanchored format. Do not delete history to migrate.
The controller environment requires boto3 as well as the offline test
dependencies. Credentials come from the controller's standard provider chain.

`--initialize-state` is only for a candidate with no remote state and no existing
destination ledger. It requires the authenticated bootstrap artifact described
below, including retained infrastructure and cleanup reservations. It does not
grant another $500 budget. All development attempts,
qualification attempts, retries, infrastructure and cleanup draw from the same
candidate-wide $500 ceiling, including the $50 cleanup reserve. Unknown usage
remains reserved. A lost lease stops further work; an independent watchdog is
still required to remove resources when the controller itself is gone.

### Bootstrap and shared-operation accounting

Ledger version 2 retains model-attempt reservations in `entries` and non-case
reservations in `operations`. Both arrays share the same $500 ceiling and $50
cleanup reserve; reservation IDs are unique across both. An operation records
`operationId`, `purpose` (`bootstrap`, `shared-environment`, `semantics`,
`telemetry` or `retention`), infrastructure/cleanup kind, reserved maximum, pricing
basis digest, settlement state and actual charge when known. These records never
create a case, model success, retry authorization or denominator credit.
An overrun blocks new reservations, but later actual charges for already-reserved
resources and cleanup are still retained. Settlement continues to return a
blocked result while the overrun remains; it never restores execution authority.

Before any Terraform apply, the trusted producer must reserve the complete
bootstrap/shared liability and durably publish that history in an existing,
controller-only substrate. A local file alone is not enough for controller-loss
recovery. The governed Terraform workflow then retains observed charges and
unknown reservations. The initial producer execution seals a bootstrap artifact
with `schemaVersion: 1`, `candidateKey`, the entire version-2 `ledger`, and its
`source` identity (`repository`, `workflow`, `sourceSha`, `runId`, `runAttempt`).
It must contain infrastructure and cleanup bootstrap reservations and no model
attempts. Failed or interrupted bootstrap work stays in that same history;
starting another producer run must not initialize a replacement budget.

The controller initialization arguments are:

```text
--initialize-state
--bootstrap-artifact <retained-bootstrap.json>
--bootstrap-digest <sha256-from-trusted-configuration>
--bootstrap-source-sha <exact-reviewed-producer-commit>
--bootstrap-run-id <completed-producer-run>
--bootstrap-run-attempt <exact-run-attempt>
```

The importer verifies the exact digest, candidate, successful trunk
`agent-e2e-campaign.yml` workflow-dispatch run and GitHub attestation, including
source/run/attempt and GitHub-hosted signing custody. It imports every retained
charge and unknown reservation in one initial fenced snapshot and preserves the
bootstrap artifact/binding in all later snapshots. Existing local or remote
history cannot be overwritten by an import. Recovery of existing state requires
no bootstrap flags and never reruns the import. Failed or ambiguous publication
retains the permanent initialization marker; reconcile rather than reinitialize.

The complete producer, pre-apply durable handoff and live import/recovery
demonstration are still prerequisites. These import routines do not provision
infrastructure or make an unauthenticated local ledger a trusted bootstrap.
There is currently no completed live bootstrap producer run to import.

The final trusted inventory uses version 2 and includes `operationReports`: one
hash-bound report per candidate operation, covering every operation in the full
ledger, including earlier development and bootstrap work. A report has
`schemaVersion: 1`, `candidateKey`, `operationId`, `purpose`, `status`, exact
`reservations` copied from the current ledger and nonempty `observations` artifact
references. Observations must come from the trusted infrastructure/cost collector.
`status` is `settled` only when every reservation is settled, otherwise
`liability-retained`; unknown usage counts at the full reserved maximum. The gate
checks all references and exact ledger bindings. It does not infer actual AWS
charges from an arbitrary JSON observation or independently replace the trusted
collector's pricing/usage work. New reports after settlement are new immutable
artifacts; retain earlier reports rather than overwriting their evidence.

`totalCostUsd` remains the current campaign's model-attempt receipt total.
`candidateLiabilityUsd` includes all attempts and all shared-operation actuals or
unresolved maxima. A stale, missing or relabeled operation report blocks the gate.
Version-1 ledgers remain readable for investigation; qualification requires
version 2, and adding operation charges to legacy history requires reviewed
reconciliation rather than resetting or silently upgrading it. No earlier live
campaign used the version-1 ledger.

Recover existing durable history on a replacement controller without starting a
model or initializing a new spending allowance:

```powershell
python tools/agent_e2e_state.py --state-config C:/controller/state.json --candidate-key <sha256-candidate-binding> --ledger C:/controller/recovered/budget.json --journal-root C:/controller/recovered/journals --output C:/controller/recovery.json
python tools/agent_e2e_telemetry.py --journals C:/controller/recovered/journals --output C:/controller/recovered/report.json --prometheus C:/controller/recovered/metrics.prom
```

Recovery acquires the candidate lease and verifies the retained journal identities
and chains before publishing missing files. An interrupted attempt remains
interrupted and cannot reuse its attempt identity. Existing divergent/torn local
journals are retained and block recovery; use an independent reconciliation process,
not deletion of evidence. A newer local charge cannot be overwritten by an older
remote ledger. Retain both copies and reconcile the actual liability before
resuming. The normal controller also restores durable journals before attempting
another case. Recovery itself never creates a passing receipt or authorizes retry.

## Delivery order and acceptance

### MCP action transport component

`tools/agent_e2e_mcp.py` implements the HTTP client needed by the scenario backend
under [#623](https://github.com/honua-io/honua-release/issues/623). It negotiates
Honua's `2025-03-26` protocol, acknowledges initialization, retains the session,
follows opaque tool-list cursors and sends the model-selected tool name and
arguments unchanged. It accepts JSON and POST SSE responses, preserves tool
errors and structured content, and refuses server-initiated requests for
unnegotiated capabilities. Protocol pings are answered once, bounded to 16 per
request, without advertising sampling or approval capabilities.
An incomplete or oversized catalog fails discovery;
it is never silently shortened into an apparently complete interface schema.

Use `Client(..., workflow_view="setup")` for the controller's setup-discovery
action. The client sends Honua's `initialize.params._meta["honua.io/workflow-view"]`
and repeats the explicit `tools/list.params.view` selector on every page, including
stateless sessions. `list_tools(view="full")` requests the authenticated full
catalog for that call without changing the configured session view. No client
tool roster is maintained. Returned `_meta` revisions, digests and stages are
preserved, changes between pages fail discovery, and an explicitly requested
named view must be observed in the response. Omit the selector only when the
server-profile default is intended. These negotiation fields were checked against
the server implementation at `baf41822fb`; deployed candidate parity remains a
live check. Server-reported digest values are retained claims, not independently
verified hashes of the descriptors.

The client uses only its supplied agent credential reference, verified TLS (or
loopback HTTP for local tests), one origin and no environment proxy or redirects.
No HTTP error, missing session, connection loss or malformed response triggers a
retry or automatic reinitialization. A lost response may follow a committed write:
retain the durable action intent and use an independent observation to reconcile
the outcome. Session deletion is attempted explicitly through `close()`; errors
are reported and never establish successful scenario cleanup.

Response observations retain exact received bytes, hashes, request digests,
statuses and allowlisted server correlation headers. Credentials and session
headers are excluded; echoed credentials (including JSON-escaped echoes) are
rejected before retaining a response, including SSE comments and partial frames.
These are untrusted action observations,
not independent semantic evidence or a success receipt. Bounds are 512 KiB per
request/response, 2 MiB total received per client, 32 catalog pages and 1,024 tools.
The client has a cumulative time budget of at most 120 seconds and socket waits
of at most 10 seconds. Its caller must enforce a process deadline as well; a
socket read can finish after the remaining cumulative budget expires.

The worker image now includes this client and a separate immutable Python
entrypoint for each MCP action. `Worker.execute_mcp` sends a structured discovery
or call request through the attempt's existing FIFO queues. The worker persists
intent before launching the process and the full result before publishing it.
Repeated delivery returns the retained result; a crash after dispatch returns
`interrupted` and never executes again. The subprocess receives only its agent
MCP configuration and a small execution environment, with a process deadline,
bounded output files and descendant termination. Workspace modules cannot replace
the immutable application imports.

The trusted lifecycle must supply `HONUA_AGENT_MCP_CONFIG` containing exactly
`endpoint`, `credentialKind` (`bearer` or `api-key`), `credential` (the agent's
token, or null for an anonymous test) and `workflowView` (`setup` for the standard
controller action). This is runtime secret configuration, not model tool input;
the model cannot override the endpoint or identity through the MCP action API.
Never supply controller or approver credentials. The Fargate root's
`mcp_configuration` input in [honua-iac #251](https://github.com/honua-io/honua-iac/pull/251)
provides an explicit secret-version reference through the execution role, with
attempt/candidate tags, fixed expiry and optional scoped KMS decryption. Supply
only secret metadata to Terraform; the credential custodian populates the
already-provisioned secret. Deployed delivery, application scope and revocation
remain live prerequisites. Terminal commands do not inherit this variable.

MCP replies carry at most 4 MiB of structured result and raw observations in
32 KiB chunks. Each chunk binds the attempt, action, complete command digest,
payload digest, byte length, index and count. The controller rejects mixed,
changed, missing or oversized chunks; it never reports a prefix as a complete
catalog. `execute_mcp(..., retain_chunk=...)` requires a synchronous durable
retention callback before deleting each reply message. Recovery can reassemble
retained chunks through `ReplyChunks` without dispatching an action. The worker
retains its complete reply if publication fails, and chunk deduplication IDs are
stable across redelivery. Delivery has a separate 30-second allowance inside the
attempt's absolute expiry; incomplete delivery leaves the outcome unverified.

`tools/agent_e2e_worker_evidence.py` supplies the controller-side retention
callback. Construct `open_evidence({"region": ..., "bucket": ...}, candidate_key,
worker_attempt_id, dispatch["actionId"])` after the backend has validated active
attempt/intent ownership and reserved the complete liability. Pass that object as
`retain_chunk` to `Worker.execute_mcp`. It uses the Terraform-owned controller
bucket and its existing `evidence/<candidate>/` permission; it neither provisions
storage nor runs inside the worker image.

Before the first chunk, the callback retains an immutable action header binding
candidate, worker attempt, action, complete command digest, reply digest, byte
length and chunk count. Every write requires `If-None-Match: *` and a matching
SHA-256 checksum acknowledgement with a non-null S3 version ID. Before queue
acknowledgement, a version-history listing and exact-version read must prove that
the returned version is the sole retained version, with matching bytes and no
delete markers or truncated history. S3 conditional writes can succeed over a
current delete marker; that response alone does not establish retention. Duplicate writes
can adopt only a single retained version whose canonical bytes match exactly.
Connection failures and incomplete write acknowledgements leave the queue
message unacknowledged. No raw model command or argument is added to the header.
The chunk bodies remain the bounded, untrusted worker output described above.

`ReplyEvidence.recover(header_reference)` reads the exact retained header
key/version/hash and reconstructs the full reply with exact chunk-version
references. If the header acknowledgement was lost, `recover()` discovers it
only when there is exactly one retained version with no delete markers or
truncated history. Missing chunks, extra versions, changed hashes or conflicting
identities require reconciliation; recovery never dispatches a command, grants a
retry, alters the budget or emits a qualifying receipt. Fully retained evidence
can be recovered after a controller crash.

For partial replies, `agent_e2e_reply_recovery.drain_mcp_replies` combines retained
chunks with pending messages from the reviewed reply queue. Supply a fresh
`ReplyEvidence` instance and the exact `protocol`, `attemptId`, `actionId` and
`commandDigest` from controller-owned dispatch history. The digest covers the
entire transport command, including deadline and timeout; the model action digest
alone is insufficient. Persist this binding before dispatch in the complete
backend integration. Never infer it from a queue message or accept an untrusted
binding to make recovery pass. Without trusted dispatch history, reconcile
manually and retain liability.

`ReplyEvidence.resume(binding)` validates existing header/chunk histories before
receiving messages. Missing chunks are allowed only when their keys have no
versions or delete markers; a missing header with orphaned evidence blocks.
Pending chunks must match the trusted binding and retained payload digest. Each
chunk is synchronously retained before queue deletion. A lost retention/delete
acknowledgement is recoverable from retained versions, without resending a command.
The recovery function has no command queue and does not construct a worker, so an
expired attempt can be reconciled while its reply queue remains available.

The poll window is at most 300 seconds with at most 256 receive calls. Supply an
SQS client with bounded connect/read timeouts and one total SDK attempt, as with
the evidence client; storage and queue calls have their own bounded duration.
Reserve the full worst-case recovery API/storage liability before starting.
Missing output, foreign messages, conflicting history and exhausted polling all
leave the attempt unverified. Complete output returns `qualification: false` and
exact retained references; it does not settle charges or grant a retry. Once a
complete reply is retained, redundant deliveries may remain on the queue for
governed teardown. Do not delete the queue before required recovery and evidence
publication finish. Independent expiry cleanup must still stop the worker.

Actual backend routing, final trusted inventory publication, and live
crash/restart/cleanup proof remain open.

Account for the bounded header/chunk PUTs, duplicate/recovery reads, version
listing, retained bytes and final artifact publication in the prepaid operation
liability. The adapter does not create a second spending ledger or claim that a
storage acknowledgement proves the agent's goal or application cleanup.

The controller passes the backend a `dispatch` object after durable intent
acknowledgement: controller-generated `actionId`, `ordinal`, `requestDigest` and
`intentDigest`. The ID binds candidate/campaign/case/attempt/ordinal, independent
of model-generated tool IDs. The backend must validate it against retained intent
and the active attempt, use that ID in the worker request, persist chunks in
controller custody, and bind the raw observations to the action. These identity
fields are not an atomic external-action fence: lifecycle fencing, real backend
routing, final evidence publication and independent cleanup still need live
integration. Return only the bounded public result to the model; the complete
raw observation payload belongs in retained controller artifacts.

Loopback HTTP integration tests and the image smoke check exercise the transport,
client, isolated process, catalog delivery and replay handling using fixture
servers. They neither run Honua nor qualify any of the 252 live cases.

### Deployment sequence

Terraform in `honua-iac` owns all campaign infrastructure, including disposable
workers, IAM, networking, state stores, telemetry infrastructure and the independent
cleanup deployment. Infrastructure changes use reviewed Terraform plans and
retained state. Runtime APIs start/stop predefined container jobs. Python owns
test orchestration, API actions, observations, budget accounting and evidence; it
must not provide a second infrastructure provisioner. Calling existing services
to run a test or write an evidence object is runtime use, not provisioning.

The accepted full work plan is [DELIVERY.md](DELIVERY.md). Cloud agent workers use
ECS Fargate; local Docker uses a dedicated container runner separate from the
controller. Existing EKS managed-node-group deployments remain in the target
matrix. The standalone EC2/SSM worker path and its watchdog drafts were removed.
No campaign infrastructure has been applied.

The queue transport binds attempt/action identity, command digest and deadline.
Duplicate completed delivery returns its retained result; a journal entry left
started by a crash returns interrupted and never executes again. A replacement
task must never reuse an attempt's queues after loss of its local journal. The
lifecycle's durable launch claim and prohibition on redrive enforce that boundary.
Worker replies are untrusted terminal output; only independent probes qualify a
goal. Live container isolation, independent cleanup and backend integration remain
required; the offline tests do not prove these deployments operational.

1. Validate the contracts and public documentation graph. Keep all 252 live
   cases unqualified while these offline checks pass.
2. Configure isolated controller/worker roles, immutable images, durable stores,
   watchdog and reviewed liability bounds. Prove crash recovery without resetting
   history and verify workers cannot reach controller evidence or credentials.
3. Implement and run the local scenario backend and deterministic semantic suite
   through actual existing APIs. Preserve source content and retain raw responses.
4. Run development A/B across the cloud targets, including Lambda's separate
   Redis-disabled refusal configuration and real Batch/native ETL execution.
5. Freeze the candidate, model, knowledge, recipes and expectations; run A/B/C
   qualification with all spending retained and only evidenced infrastructure
   retries allowed. Publish receipts through the attesting producer workflow.
6. Activate release consumption only after the complete campaign and existing
   release checks pass. Keep the desktop-client work outside this campaign.

The offline command is maintained in
[`agent-e2e-contracts.yml`](../../.github/workflows/agent-e2e-contracts.yml).
Its model/backend fixtures test the controller and gates; they are never uploaded
as live qualification receipts.
