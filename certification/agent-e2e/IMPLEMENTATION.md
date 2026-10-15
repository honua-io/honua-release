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
| Worker isolation | EC2/SSM transport with pinned image/profile and bounded command lifetime | Reviewed AMI, IAM/network restrictions, independent watchdog and deployment configuration |
| Durable state | Versioned S3 snapshots, fenced DynamoDB lease, synchronous reservation/event persistence, journal recovery and protection against overwriting newer local charges | Provision controller-only stores; integrate final receipt/registry publication and prove cloud crash recovery |
| Instrumentation | Hash-chained attempt journals, request/trace correlation contracts, JSON diagnostics and atomic Prometheus text output | Scrape/upload wiring, dashboards, alerts and observed server trace artifacts from the live backend |
| Release evidence | v2 gate checks exact knowledge, independent semantics, runtime interfaces, all receipts and candidate spending | Attesting campaign producer workflow and successful live campaign before release activation |

The HTTP recipe runner and worker transport are building blocks, not a complete
backend. There is no fallback to running model-selected commands on the controller.
The consumer integration must remain draft until the producer and runtime evidence
exist. Unknown attribution and missing observations fail closed.

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
store. The controller environment requires boto3 as well as the offline test
dependencies. Credentials come from the controller's standard provider chain.

`--initialize-state` is only for a candidate with no remote state and no existing
local ledger. It does not grant another $500 budget. All development attempts,
qualification attempts, retries, infrastructure and cleanup draw from the same
candidate-wide $500 ceiling, including the $50 cleanup reserve. Unknown usage
remains reserved. A lost lease stops further work; an independent watchdog is
still required to remove resources when the controller itself is gone.

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
