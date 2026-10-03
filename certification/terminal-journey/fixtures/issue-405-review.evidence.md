# PR 405 coordinator review disposition

The executor corrections are complete. The local-Docker receipt is **blocked**;
this packet does not certify the complete build journey or close issue 377.
Candidate selection remains continuous certification on trunk under R18 as amended
by R19–R21. No manifest pin, release gate or test requirement changes in this packet.

| Finding | Disposition and evidence |
| --- | --- |
| 1. Approval terminal status | Fixed: `Succeeded` ends the poll and is accepted as success (`executor.py:386`). The regression exercises Pending → Executing → Succeeded without a timeout. |
| 2. Driver/canary verification contract | Fixed: exactly the canary's 11 assertion names, each a status string, including `finalUrlProof` (`live_driver.py:457`). Missing evidence stays blocked/fail. The canary state-machine test invokes the actual owned verifier (`tools/test_terminal_model_canary.py:737`). |
| 3. Blockers hidden as ready | Fixed: recorded blockers and failed prerequisites retain their real dependency IDs (`live_driver.py:302`). Unexecuted assertions remain pending actions; completed work missing required canonical identities blocks. Canary failure text names the stage, invocation, detail and dependencies (`tools/terminal_model_canary.py:1170`). |
| 4. Stage-eight proofs/principals | Fixed: approval runs final-content and authority proofs (`live_driver.py:422`). Local target declares all four principal references (`targets/local-docker.json:40`). The fixture verifies exact grants on expiring proposer/approver/viewer API keys and signs fresh five-minute other-tenant bearers (`local_fixture.py:73`). Private material is 0600 scratch data, removed at teardown, never a committed key. Authenticated Studio access precedes the tenant denial (`executor.py:462`). |
| 5. Open review threads | All six reviewed threads have code changes: findings 1, 2, 4 (two threads), 9, and unsupported injection stages. The adapter only supports stage 4 injection; canary validation rejects any other imported stage before setup (`tools/terminal_model_canary.py:963`). No stage IDs are duplicated in the harness. |
| 6. Stale proofs | Fixed: every proof attempt first clears the prior proof (`executor.py:104`). A render also invalidates its pixel proof before transport, including protocol/transport refusal (`executor.py:145`). Verification consults the latest check and proof together (`live_driver.py:489`). |
| 7. Uncaught response-shape errors | Fixed: empty job outputs fail explicitly (`executor.py:288`); AttributeError/StopIteration become check or per-stage execution failures (`executor.py:115`, `executor.py:581`). Regressions confirm all six executor-stage results survive malformed responses. |
| 8. Lost prerequisites | Fixed: merge execution evidence with measured prerequisites (`run.py:446`, `stages.py:137`). Every executor stage retains a pinned-client check; all measured CLI/tool-presence checks survive. Only unexecuted placeholders are replaced. |
| 9. Replica port | Fixed: Compose and replica proof URL resolve the same `HONUA_JOURNEY_REPLICA_PORT` setting (`local_fixture.py:47`, `targets/local-docker.json:13`). Regression uses port 19138. |
| 10. SDK environment | Fixed: invocation passes an explicit runtime-environment allowlist plus the candidate URL and proposer key (`sdk.py:81`). GH tokens, approver credentials, fixture signing material and NuGet source credentials are excluded. |
| 11. Separation-of-duties interpretation | Reported below; no additional approval-eligibility certification implemented. |
| 12. Principal-dependent discovery | Reported below; transport comparison remains enforced. |

## Approval evidence limit (finding 11)

The original executor fixture did not mint or read back a proposer's effective
grants. The repaired fixture now reads back exactly `admin:write` for the proposer
and `admin:approve` for the separate approver (`local_fixture.py:19`, `local_fixture.py:90`).
On the pinned candidate, `admin:write` resolves to administrative Write access,
and the approval policy admits Write access. Thus configured general approval
eligibility is established at that coarse permission layer; it is not inferred
from the self-approval 403.

There is still no positive live approval of an unrelated proposal by the proposer,
and no resource-specific effective approval/RBAC decision recorded by this journey.
The receipt never reached an approval attempt. Do not describe its self-denial or
distinct resolution actor as full separation-of-duties qualification. A candidate
would need to expose an effective approval authorization decision for the proposer
(including resource/tenant and the matching RBAC grant), or provide an independently
owned proposal that this same proposer successfully approves as a positive control,
then refuse its own proposal with an explicit separation reason.

Candidate source references at `87966c3f7b6c840ffc4d4da0b451714ab717b18a`:
[admin grant classification](https://github.com/honua-io/honua-server/blob/87966c3f7b6c840ffc4d4da0b451714ab717b18a/src/Honua.Hosting/Features/Authentication/AdminApiKeyPermission.cs#L307),
[approval policy](https://github.com/honua-io/honua-server/blob/87966c3f7b6c840ffc4d4da0b451714ab717b18a/src/Honua.Hosting/Features/Authentication/AdminApproveAuthorization.cs#L21),
[resource approval RBAC resolution](https://github.com/honua-io/honua-server/blob/87966c3f7b6c840ffc4d4da0b451714ab717b18a/src/Honua.Server/Features/Admin/ProposalEndpoints.cs#L380).

## Discovery on this candidate (finding 12)

The pinned candidate's explicit setup `tools/list` is **principal independent**.
`ListToolsAsync` projects the static catalog and runtime sources into the requested
workflow view; it does not filter by caller grants. Runtime-published operation
descriptors are selected by catalog, mapper/executor availability and feature
options, not principal permissions. Call-time authorization remains separate.
The root-admin observation and proposer's explicit setup view can therefore match
on this candidate. Catalog changes during a run still correctly refuse stale
authority. No comparison is relaxed.

Source references:
[setup projection](https://github.com/honua-io/honua-server/blob/87966c3f7b6c840ffc4d4da0b451714ab717b18a/src/Honua.Ai/Features/Protocols/Mcp/Mcp/McpDataAccessSurface.cs#L308),
[runtime sources](https://github.com/honua-io/honua-server/blob/87966c3f7b6c840ffc4d4da0b451714ab717b18a/src/Honua.Ai/Features/Protocols/Mcp/Mcp/McpDataAccessSurface.cs#L439),
[published-operation selection](https://github.com/honua-io/honua-server/blob/87966c3f7b6c840ffc4d4da0b451714ab717b18a/src/Honua.Ai/Features/Protocols/Mcp/Mcp/Tools/PublishedOperationToolSource.cs#L73).
The installed proxy failed before the executor could make live proposer tool calls;
this is source inspection, not a claim of passing installed-proxy call parity.

## Live receipt and validation

The receipt is [issue-405-review.local-docker.json](issue-405-review.local-docker.json).
The run consumes the manifest pins freshly fetched from `origin/trunk`; the client
refresh packet was not on trunk when this run began. Server candidate and image
remain `87966c3f7b6c840ffc4d4da0b451714ab717b18a` and
`sha256:069f196bfa5c7201223d4d89868934242c4ace8805a6e48c122a88d84fa6eb1a`.
Clients remain `@honua/sdk-js@0.1.9-beta.0` and `@honua/mcp-server@0.1.4-beta.0`.

Readiness, exact identity, disabled licensing, anonymous refusal and the published
CLI credential preflight pass. HTTP setup discovery validates the bounded view and
complete catalog; the installed pinned proxy fails its transport contract. Stage 1
and stages 3–8 remain blocked. Stage 2 passes. Each later stage retains the measured
client-pin and tool-presence checks. The receipt makes no candidate import, pixel,
geometry, saved-map, publication, approval or complete tenant-isolation claim.

The final invocation was:

```sh
DOCKER_CONFIG=/tmp/405-docker-config python3 certification/terminal-journey/run.py \
  --mode live --target certification/terminal-journey/targets/local-docker.json \
  --workdir /tmp/405-coordinator-final --keep-stack \
  --output certification/terminal-journey/fixtures/issue-405-review.local-docker.json \
  --evidence-uri https://github.com/honua-io/honua-release/pull/405
```

After the receipt, fixture-only controls return HTTP 200 for each of two fresh
other-tenant Studio bearer requests and HTTP 403 for viewer access to the admin
API-key surface. These controls do not replace blocked stage-eight
resource proofs. The isolated stack and private fixture material are then removed.
No shared Docker stack, persistent host credential configuration or AWS resource is
changed. The temporary Docker configuration avoids the host's unavailable external
credential helper when pulling public fixture images.

The optional exact-candidate check remains red on the unchanged pending
protocol-certification ledger and pending x86_64 Lambda qualification. Those
release criteria are not bypassed by this executor repair.

Focused journey/canary validation: **246 passed, 57 subtests passed**. Full required
validation and final CI results are recorded in the PR delivery comment. No .NET
project changed, so no build or format of an unrelated project is run.

Refs #377 (released: passing candidate build stages 3–8 and approval/verification receipts await the continuously certified installed proxy/client set and actual canonical identities; update/rollback executors, two local image revisions, GP exactly-once/schema/incompatible-target proofs and signed-lock qualification remain unimplemented; operate remediation with approval and both per-run prompt-injection probes remain unimplemented)
