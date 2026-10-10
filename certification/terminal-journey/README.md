# Terminal-first control-plane certification

This directory owns the deterministic, model-free terminal journey (honua-release#123,
evidence key `release.e2e.terminal-zero-to-map`) and the control-plane roster drift gate
(#121). The primary 2026.1 workspace is one terminal using exact installed client
artifacts; Console and browser Studio remain separate client receipts and do not define
this roster.

## Layout

| Path | Role |
| --- | --- |
| `journey.v1.json` | The eight numbered stages: id, command, the pinned client commands each needs, and its upstream contracts. Imported by #161; never duplicated there. |
| `receipt.schema.json` | `terminal-journey-receipt-v1`. Binds package integrities, source SHAs, and the server/fixture/config/auth-policy tuple to a per-stage outcome. |
| `targets/local-docker.json` | Local Docker target. Extends the owned Docker fixture with a second candidate replica, Redis and an authored GeoServices source; the server digest remains manifest-bound. Stage 3 uploads `fixtures/journey-source.geojson` instead of importing from that source. |
| `cloud_target.py` | Cloud cell targets (`aws-ecs`, `aws-serverless`), generated per attempt by `e2e/cloud_journey.py` from the local fixture. See [Cloud cells](#cloud-cells). |
| `pins.py` | Consumes the exact #136 `clientArtifacts` from published registry bytes and proves which terminal commands they actually ship. |
| `probes.py` | Deterministic probe primitives: HTTP, compose lifecycle, and MCP JSON-RPC through the pinned `honua-mcp-proxy`. |
| `stages.py` | Contract prerequisites and the outcome discipline. |
| `executor.py`, `transport.py` | Authenticated stage 3–8 actions, canonical lifecycle polling, typed approval and content reads. |
| `sdk.py`, `sdk-import/` | Digest-verified published .NET package consumption and the typed import bridge. |
| `oracles.py` | Independent geometry, feature, PNG pixel and map-content assertions. |
| `run.py` | The driver. `--mode build` (contract only) or `--mode live --target …`. |
| `live_driver.py` | The `terminal-journey-driver-v1` adapter #161 calls, at the path its protocol contract fixes. |
| `fixtures/smoke-receipt.local-docker.json` | A real receipt from a real run against the pinned candidate. Not a template. |

## Honesty rules

These are enforced by `receipt.schema.json` and asserted by `test_run.py`:

- **There is no skip state.** A stage is `pass`, `fail`, or `blocked`. A stage that
  cannot run yet is `blocked` and must name at least one missing dependency.
- **A pass requires a live observation.** `harness-build` evidence can never be
  `verified-current`/`complete`, and build mode can never report `pass`.
- **A failure names the numbered stage and the command or tool that broke it**, in
  `failure.number` / `failure.stage` / `failure.command` / `failure.check`.
- **No fabricated receipts.** If the pinned client artifacts cannot be consumed from
  published bytes at their manifest integrity, the run fails closed: every affected
  stage is blocked and says why.
- **No model calls anywhere.** Fixed commands and tool calls only, so a failure
  identifies a broken contract. The genuine-model canary is #161 and is linked, never
  embedded.

## Pin consumption

`pins.py` resolves each `clientArtifacts` entry from the registry, checks the registry
integrity against the manifest, re-hashes the downloaded bytes, and re-runs
`tools/verify_client_artifacts.py`'s own archive identity check — the repository's
existing verifier is imported rather than reimplemented, so this driver and the manifest
gate agree by construction. Only those verified tarballs are installed. There is no
checkout, workspace, regenerated or floating-`npx` fallback, and the workspace is
materialized fresh on every run so stale bytes can never stand in for the pins under
certification.

The driver proves which terminal commands the pinned bytes actually ship, using
`clientWorkspace.commandSurface` in each run. `honua` and its `admin` command come
from `@honua/sdk-js`; `honua-mcp-proxy` comes from `@honua/mcp-server`. A successful
`honua --help` probe must advertise `admin`. A failed process, missing verification,
or ambiguous command row cannot certify presence.

The historical smoke fixture used SDK 0.1.7-beta.0 without Admin. The later
[issue #123 run](../../artifacts/terminal-journey-issue-123.md) verified Admin in
SDK 0.1.9-beta.0, but preserved obsolete stage blockers. Both are immutable
observations, not the current command inventory. New receipts distinguish command
discovery from credentialed execution. When the pinned `honua` binary and a
loopback admin credential are present, stage 2 runs a reversible private-CLI
preflight: mint an `admin:read` key into a 0600 sink, read its effective
permissions, list it without key material, revoke it, and delete the sink.
The root credential and the one-time key never enter the receipt. Service
mutation, style pixels, GP, composition, publication, and separate-principal
approval require their own live observations and canonical receipt identities.

Credentials travel only over HTTPS or plain HTTP to loopback (`probes.credential_transport`,
shared by the stage 2 preflight, setup discovery and the executor transport). A cloud journey
attempt (`e2e/cloud_journey.py`) additionally admits plain HTTP to exactly one host for its
duration: the cell's own harness-provisioned `*.elb.amazonaws.com` load balancer, under owner
ruling `canary-http-cell-2026-10-08` (the cell's per-run application key dies with the cell;
HTTPS on the cell is 2026.1.x hardening). The receipt records `Candidate transport:
http-cell-allowed (<host>)`. When the imported driver raises, the receipt and job log name the
exception type and message, the step it reached and its last HTTP exchange (status, path and a
redacted, bounded body excerpt).

## Cloud cells

`e2e/cloud_journey.py` runs this driver against an `aws-ecs` or `aws-serverless` cell with a target
document `cloud_target.build` generates for each attempt (`target-<n>.json` next to the receipt).
The receipt's target kind is the cell's, and its live evidence is `live-aws-ecs` or
`live-aws-serverless`, so a cloud receipt can qualify its cell (#377). The schema and
`cloud_journey.validate_attempt` bind every live source to the receipt's own kind and that kind to
the cell. Preview and stub cells keep the documented blocked build receipt.

- **Principals.** `local_fixture.credentials` mints the operator, proposer, approver and viewer keys
  through the cell's admin REST API with the cell's bootstrap credential
  (`HONUA_CLOUD_JOURNEY_ADMIN`), with the same grants and `layer-write-key` StudioDraft author grants
  as local Docker. They expire after an hour and the run revokes them when it ends, best effort
  (`local_fixture.revoke`; the receipt notes how many were revoked). Key material stays in the 0600
  private state file and memory; the last-exchange trace masks it.
- **Second tenant.** A cell trusts no issuer the harness holds a signing key for, and admin API keys
  are tenant-independent, so no second-tenant principal is fabricated. The target documents it as
  unavailable (`unavailablePrincipals`), and stage 8's tenant-isolation assertion is `blocked` on
  #377 with that reason while the rest of stage 8 runs.
- **Datasource.** The cell's own RDS database, with `sslRequired: true` and `sslMode: Require`. The
  provision job seals the connection its fixture seed used to the journey runner together with the
  application key (`cloud_journey.pack_handoff`). The target names the coordinates only by
  environment reference (`HONUA_JOURNEY_DATASOURCE_{HOST,PORT,DATABASE,USERNAME,PASSWORD}`), so the
  retained target document holds no host, login or password.
- **Replica.** A cell has one endpoint and the server (798d517) returns no per-instance identity,
  so stage 6's cross-replica read-after-write cannot be proven there. The `replica-map` assertion
  is `blocked` with that reason, naming the server need (a per-instance identity on responses).
  `replicaTopology` stays as evidence in the check and a receipt notice: `ecs-alb-tasks` (the ALB
  spreads requests across ECS tasks) or `lambda-function` (one function).
- **Candidate image.** ECS reports its running image through DescribeTasks. A serverless cell
  reports it through Lambda GetFunction: the API function must run the manifest's mirrored
  `awsLambdaEcrDigest`, and the receipt then names the Lambda pin (`awsLambdaImage@awsLambdaDigest`).
- **Transport.** Unchanged: the shared `probes.credential_transport` rule (HTTPS, loopback, or the
  cell's own load balancer for one attempt).
- **Redis-off cells.** Under owner ruling `redis-governed-control-plane-2026-10-08` (Redis is a
  requirement of the governed control plane), a stage on a cell that declares `redis-off` passes by
  recording the candidate's typed refusal instead of executing: the tool error must carry
  `code: unavailable` and `missingDependency: redis`, and the capability manifest must report the
  stage's capability `dependency-unavailable`. The stage row then carries `topologyRefusal` (schema:
  `receipt.schema.json`) and no canonical identity, because nothing ran, and the receipt target
  names `redisTopology: redis-off` (the schema refuses a refusal on any other target). Stages 5
  (`jobs.runner`) and 6-8 (`operations.proposals`) are the ruling as written; the owner extended it
  to stage 3 on 2026-10-10 (`operations.proposals`: `honua_publish_service` runs through the
  governed operation runtime, and on a Redis-off Production host it returns the typed durable-store
  refusal). Stage 3 then passes on its datasource and import checks plus the refusal; nothing that
  needs the published layer is required of it. On a Production Redis-off cell stages 4 and 6-8 have
  no published layer and stay blocked on it, so the cell receipt is not a pass until those stages
  can run or are ruled. Any other refusal, a redis-on cell,
  or a manifest that reports the capability available keeps the failure. A local target can
  declare `"redisTopology": "redis-off"` for the same treatment.

A failing tool call names what the candidate said: the problem's `type`, `title`, `code`,
`reasonCode`, `missingDependency`, `capability` and error `kind` as bounded tokens, and its message
masked (credential-shaped members and the run's own principal keys redacted) and bounded. A
`service.publish` that does not complete names its terminal `status`, policy outcome and masked
message; a published-SDK refusal names the bridge's exception type only.

See the [#120 acceptance audit](../../docs/2026.1-terminal-arc-acceptance.md) for
pre-cut implementation gaps and separately released exact-candidate reruns.

## Running it

Contract only, no target, no network:

```
python certification/terminal-journey/run.py \
  --output artifacts/terminal-journey.json --evidence-uri "$EVIDENCE_URI"
```

Live against the pinned candidate on local Docker:

```
python certification/terminal-journey/run.py --mode live \
  --target certification/terminal-journey/targets/local-docker.json \
  --output artifacts/terminal-journey.json --evidence-uri "$EVIDENCE_URI"
```

The live run brings the compose stack up on the manifest-pinned digest, probes it, and
tears it down. `--base-url` reuses an already-running stack; `--keep-stack` leaves it up.

Self-tests need Python with PyYAML/jsonschema and Node.js (the CLI subprocess
fixture), but no stack, Docker or network:

```
python certification/terminal-journey/test_run.py -v
```

## Control-plane roster gate

Once server#3363 publishes the authoritative Admin OpenAPI/CLI and MCP projection
exports, pass both files to `--rest-roster` and `--mcp-roster`; the gate requires an
exact 396 = 385 + 11 partition, unique IDs, and no overlap. Secret/session exclusions
stay REST/CLI-only and use a private secret sink. Anonymous MCP discovery never implies
call authorization. Until those exports exist the roster verdict is `blocked`, never
`pass`.

## What the live lane proves today

The committed smoke receipt records eleven passing probes against the pinned candidate,
including readiness, exact candidate identity (`deploymentRevision` equal to the manifest
server SHA), anonymous admin refusal, a paginated 52-tool surface read through the pinned
proxy, and the presence of the style, GP, Studio and publication tool families. Every
stage in that receipt is `blocked`, because no stage's full contract was satisfied.
The receipt names which dependency stops each one. It is not rewritten by the
stage 2 preflight. A later live run can pass stage 2 only from those observations,
and that still does not pass the journey.

AWS wrapping and genuine-model evidence are linked as #129 and #161, never embedded or
treated as substitutes.
# Measured setup discovery

Stage 1 now selects the server's `setup` view in the actual HTTP and installed
stdio initialize requests. Later selector-free discovery must retain that view.
An authenticated `full` request is drained independently of any expected tool
count, and a subsequent selector-free request must restore the initialized view.
Full catalog names remain available to later stage diagnostics; only the bounded
setup descriptors are exposed in `toolView.tools`.

`setup-discovery.json` retains the original HTTP JSON response, complete canonical
catalog descriptors, revision metadata, and measured byte/digest evidence. The
tools array and each descriptor are sliced from original UTF-8 JSON without
reserialization. The unchanged ceilings are 48 tools, 128 KiB aggregate and
16 KiB per descriptor; estimated tokens must equal aggregate bytes divided by
four, rounded down. The membership digest follows each tool's stage in original
wire order. The revision digest is server-authored and compared across transports,
not reconstructed from incomplete stage metadata. Full-catalog comparison hashes
use a separately named normalized serialization and are not server wire digests.

Both transports reject duplicate JSON keys, invalid/nonstandard JSON, oversized
responses and incomplete catalog pagination. The stdio consumer reads bounded
binary chunks before parsing. It invokes only the verified installed executable;
there is no source, resolved-module or floating-package success fallback.

The optional `Terminal journey contract` workflow dispatch accepts
`engineering_server_image` (an immutable Honua GHCR digest) and
`engineering_server_revision` (its full source SHA). The dedicated engineering
job reuses the hosted Docker fixture, generates an ephemeral bootstrap credential,
verifies image configuration/runtime identity and anonymous refusal, and consumes
the unchanged frozen client pins. It retains only `rehearsal.json` and
`setup-discovery.json`, excluding installation directories and credentials. The
receipt explicitly sets `qualification: false`; an image override does not create
a governed candidate cut or attestation. Failed pinned-proxy behavior remains a
failed rehearsal even if direct HTTP measurements pass.

The proxy negotiation prerequisite is
[SDK PR 1783](https://github.com/honua-io/honua-sdk-js/pull/1783). Its locally packed
source tests do not replace a published package or update this repository's
frozen pins. Stage executors now invoke authenticated operations when that negotiation succeeds.
Their candidate qualification remains blocked until the corresponding content,
authority and canonical receipt assertions pass. Discovery never grants call authority.

## Promise journey handoff and remaining acceptance (#377)

The model canary now passes canonical stage IDs to this driver and also accepts its
structured stage evidence, binding the ID, number, command and checks before
recognizing completion. A blocked stage stays blocked. Descriptor-form requests
are accepted only when they match the imported journey contract. Failed setup still
triggers teardown, and an unexecutable fault does not persist fictional armed state.

The canary retains action results in its in-memory conversation so a subsequent
model turn can inspect a failure. Each distinct approval proposal has its own
boundary; repeated approval of the same proposal fails. These are harness handoff
changes, not implementations of the remaining stage executors.

The StudioAi verifier consumes the server's signed event bodies and checks the SSE
event names against their enum types. It verifies the signature and exact signed
terminal digest, then compares parsed request/event values without reconstructing
System.Text.Json bytes in Python. Signed provenance must identify Claude on Bedrock.
Direct provider endpoints are refused before even the capabilities request.

Receipt action requests, results and transcript entries retain only a redacted
payload's SHA-256, UTF-8 byte count and `digest-only` retention marker. Their schema
rejects arbitrary nested content, including DSNs, presigned URLs and unknown secret
fields. Signed provenance, action attribution and sequence links remain separate.
The in-memory conversation is never restored from receipt digests or checkpoints.

The [2026-09-29 Docker observation](../../artifacts/terminal-journey-377-local-docker-20260929.md)
records failed frozen-proxy discovery and credential preflight against the unchanged
candidate snapshot. The signed loopback fixture and state-machine tests are unit and
transport evidence only; they are not genuine-model, GP, geometry or deployment
qualification.

The first #377 executor slice adds fixed build-stage calls and the matching model
action boundary. It has no shell execution. A model can select an observed MCP
call, stage 3's `honua-journey-sdk METHOD JSON_ARRAY` interpreter for the
datasource connection (`CreateConnectionAsync`, `TestConnectionAsync`), or the
fixed `honua admin import uploadImportFile` step. The bridge invokes the built SDK
with stdin JSON; it never executes that command string. Arguments bind the
authored fixture and previously returned resource identities. The model observes
the fixture, available method names and action results, rather than a forced
sequence. An upload cannot be repeated within a session.

Stage 3 imports by file upload, not by GeoServices URL. The pinned CLI sends the
committed `fixtures/journey-source.geojson` (pinned by SHA-256 in the target and
required to equal the authored feature rows) as `multipart/form-data` to
`POST /api/v1/admin/import/upload`, following a background import job if one is
returned. `honua_publish_service` then publishes exactly the uploaded table
through the canonical `service.publish` operation, which supplies the stage's
operation identities, and the published features are compared with the fixture.
A file-upload table keeps source attributes in one JSONB `properties` column, so
the feature oracle accepts the fixture key either as a column or nested in that
object, requires exactly one, and records which layout it saw. The GeoServices
URL import stays out of the local run because `GeoservicesServiceUrlValidation`
requires HTTPS and rejects private-network hosts with no operator opt-in.

The bridge verifies the manifest's published `Honua.Sdk` digest and package
identity, requires its exact `Honua.Sdk.Admin` dependency, and restores from the
GitHub Packages source mapping. A missing published method is `blocked`; a newer
SDK checkout never substitutes for the pin. The standalone
`test_sdk_bridge.py --dll PATH` exercise runs five actual published SDK HTTP calls
against an authored loopback peer. It is serialization/transport evidence only.

The local fixture mints four short-lived keys: an `operator` (`admin:write`) that
publishes, styles and runs GP; a non-admin Studio `proposer` (`write:journey`,
whose stamped `layer-write-key` role receives StudioDraft
Create/Read/Update/Publish operator grants in the isolated stack), because an
admin caller publishes immediately and never produces an AwaitingApproval
proposal; a separate `approver` (`admin:approve`); and a `viewer`. Stage 6 reads
the map family's `currentSchemaVersion` from `GET /api/v1/studio/package-families`.
Cloud cells mint these through the cell admin API (see [Cloud cells](#cloud-cells)).
Other targets need environment references for `HONUA_JOURNEY_OPERATOR_KEY` (optional;
the proposer is used when absent), `HONUA_JOURNEY_PROPOSER_KEY`,
`HONUA_JOURNEY_APPROVER_KEY` and `HONUA_JOURNEY_DATASOURCE_PASSWORD`; approval
requires different credentials and server-reported actor separation. Configure
`viewer` and `other-tenant` principal environment references for the final RBAC
and tenant probes. GitHub Packages consumption needs a read token in `GH_TOKEN`
or `GITHUB_TOKEN`. Credentials stay in private child environments, memory and
0600 state; receipts retain only the existing allowlisted proof fields.

The assertions compare imported IDs/counts/ordinates with the authored source,
calculate every buffer ordinate and its shoelace centroid independently, decode
the rendered PNG and check an authored pixel, and read the saved map from both
replicas before reopening it. The expected portable map binds its OGC source to
the imported layer. Final verification compares the actual anonymous published
body and the immutable item/version/hash join. A proposer approval attempt must
leave the proposal unchanged and the authenticated candidate must actually
return 403; CLI usage failure cannot stand in for a security denial.

Stage evidence is keyed on the identities the candidate actually emits for a
canonical invocation (`OperationHandle` in honua-server
`OperationExecutionModels.cs`): `operationInstanceId`, `correlationId` and
`auditId`, plus `proposalId` for the proposal and approval stages. A passing
stage 3, 4, 6, 7 or 8 must carry them; a missing one is an explicit
`canonical-evidence` blocker. Stage 5 runs on the job runtime, which is not the
operation gateway: `honua_execute_plan` returns `jobId`, `status`, `createdAt`
and `resourceUri` and no operation ids, so a passing stage 5 carries `jobId`,
`resourceUri`, `jobStatus` and `jobCreatedAt` instead (`stageEvidenceKeys` in
`journey.v1.json`). Approval is keyed on `proposalId` + `resolvedBy` + the approved
replay's audited operation instance (`executionOperationId` and its `auditId`).
`policyDecisionId`, `actuatorId`, `verificationId` and `approvalId` are optional,
nullable receipt fields: no candidate emits them, so they are never required and
never invented. The executor does not relax the receipt schema to turn content
assertions into release qualification. The render fault
is a real read-only invalid-width request; only the candidate's structured
refusal marks it observed, and an independently checked subsequent render marks
recovery. Its failed action remains failed in the protocol JSON; the adapter
allows that verified recoverable response to reach the canary so the next model
turn can select a recovery action. Other process failures remain strict. No
fictional armed state survives failed setup.

Remaining acceptance is explicit:

- Build stages 3–8, approval and verification still need a complete passing live
  receipt against the continuously certified candidate and published clients.
  The committed #377 observations are actual red runs, not qualification.
- Update and rollback executors, two local image revisions, the GP exactly-once
  and schema boundary checks, and incompatible-target refusal remain outstanding.
  Signed prior/target lock qualification also remains outstanding; no local image
  test is presented as a signed-lock result.
- Operate fault diagnosis and approved remediation, seeded layer/support-comment
  prompt-injection probes, and their per-run receipts remain outstanding.
- A genuine-model run additionally needs candidate StudioAi Bedrock configuration
  and the platform-controlled signing-manifest digest. Neither was configured in
  this lane. The deterministic entry gate is preserved.

R18 continuous certification on trunk governs candidate selection. A nightly
strict train must mint the signed lock and coherent published client set; this
executor does not freeze or hand re-pin the manifest. Issue #377 stays open.
