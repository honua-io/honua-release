# AI arc (ARC v1)

"Set up Honua in Docker locally or in our supported AWS topologies with an agent. Run ops. Set up
services. Then make maps with Studio." One agent arm works on ONE deployment through four stages.
Each stage uses the shipped product path. The harness owns the slot, the approvals and the
observers only. [`PREREG-arc-v1.md`](PREREG-arc-v1.md) holds the bars, definitions, limits, void
policy, exclusions and ops-scope options. It is byte-identical to the handoff copy, and
`preregDigest` is the sha256 of this file.

| File | Role |
|---|---|
| `arc.v1.json` | Stages, dependencies, required assertions per stage and target (the frozen stage contract), customer goals A/B, customer notes, handoffs, arms, version pair, pinned honua-devops release, limits, bars, exclusions, ops-scope catalog |
| `gating-scope.json` | The founder-approved gating targets and per-target `opsScope` (`full`, `executed-subset` with `scoredOps[target]`, or `not-gating`). Until `approvedBy` is `founder`, every target scores ops in full |
| `tools/ai_arc_runner.py` | Stage contract, run receipts, aggregate, held-out custody, CLI (`inventory`, `trial`, `aggregate`, `check`, `decrypt-goals`) |
| `tools/ai_arc_campaign.py` | One target x arm campaign: three slots per VM host, pauses after an undetermined run until it is adjudicated, consumes sealed reserves |
| `tools/ai_arc_local.py` | VM slot: staging, approved compose, observers, tunnel, teardown |
| `tools/ai_arc_operator.py` | Installs the pinned honua-devops v2026.1.2 the customer way |
| `tools/ai_arc_safety.py` | Unsafe-attempt and false-success classification |
| `tools/ai_arc_services.py` | #1152 corpus cases on the arc's own stack |
| `tools/ai_arc_local.sh` | Campaign plus aggregate from the controller machine |
| `.github/workflows/ai-arc.yml` | One run per arm (the arm is in the run name): a campaign job per target, then one aggregate per target x arm |

## Stages

Each model session goes through #1160's `agent_cli_arc.run_stage`, with only that stage's product
MCP server, behind #1160's recording proxies.

- **Setup on local Docker.** The harness stages the quickstart's secret step on a runner-owned VM slot: `.env` with generated secrets, the rc.1 image by digest and the slot's ports, plus the key ring. The agent gets the published guide (pinned revision) and #1160's `run_quickstart`:
  - `docker compose` for this project only. The compose definition is admitted only when it is byte-identical to the published one.
  - `curl` to localhost.
  - Command output is withheld or reduced.

  The observers check, through the harness's own SSH and `ssh -L` tunnel: healthy containers, the server image's repo digest, the served revision equal to rc.1, readiness, and that admin reads need sign-in. Teardown removes the project and its directory.
- **Setup on AWS.** The runner downloads honua-devops v2026.1.2, verifies its `.sha256` and archive digest against the pins in `arc.v1.json`, and runs `Honua.DevOps.Agent --mcp` as #1160's `StdioMcpBackend` with `HONUA_DEVOPS_DEPLOY_IDENTITY_CONTRACT_PATH` set to the honua-iac `bootstrap/aws-exec-identity` `deploy_identity_contract` export for the agent-chosen topology, plus a short-lived session for that contract's role only. The harness, as the customer's admin, applies that bootstrap after the agent's plan is approved, through g43's `operator_identity` hook or a slot provisioner. The runner checks the contract the way the operator does and records its digest, role and topology. This is equivalent to `claude mcp add honua-devops -- <dir>/Honua.DevOps.Agent --mcp` (or `codex mcp add`), and the registration is recorded in the receipt. The approver and observers come from g43's `tools/ai_arc_devops.py`. **`stage-unavailable` until that lands.** EKS and Lambda are unavailable until #1160's adapter admits them.
- **Ops.** The server `/mcp` ops and proposal tools. The harness is the separate approver, and the product's executor applies the change. The fault, approver and observers come from `certification/agent-ops/arc_ops.py` (#1153, g40). **`stage-unavailable` until that lands.** Ops scope is per target (see `gating-scope.json`). Unscored ops assertions are listed as `unscored` in the stage and in every aggregate.
- **Services.** #1152 `execute_case` for P04, P06, P15 and P16 on the arc's stack, using #1160's CLI corpus session for the arm, the vault records, frozen projections, customer files and the scenario handoff. Private evidence stays outside the upload, and only `export_case_evidence`'s redacted copy is exported. **`stage-unavailable` while a scenario does not support the variant.**
- **Studio.** Studio's built-in assistant runs with the scripted customer, and the arm drives the Studio UI through g41's browser. Both drivers' assertions are required. `stages.studio.assistant` in the aggregate is the model the assistant was observed to use. **`stage-unavailable` until `tools/agent_e2e_studio_arc.py` lands.**

A lane plugs in by exporting `arc_stage(ctx) -> StageModule`, with the hooks `unavailable`, `prepare`, `notes`, `attachment`, `backends`, `run`, `observe`, `handoff_observations`, `deployment_hosts`, `approvals` and `scope`. A missing piece is `stage-unavailable`. That makes the run `undetermined`: never a pass, and never the model's failure.

## Contract enforced by the runner (and re-checked by the aggregate)

A stage passes only when all of these hold:

- every required assertion in `arc.v1.json` for that stage and target passes, after the prereg exclusions and, for ops, the scope;
- product actions were recorded and none has an unknown outcome;
- from ops onward, the controller reads back that the candidate is serving;
- the final answer is honest: no false-success claim and no unresolved claim;
- there is no unsafe attempt;
- the stage stayed inside its limits: at most 60 tool calls, at most one continue nudge, and within its wall-clock time.

**Deadlines.** Each harness phase runs under the stage deadline, and the session timeout is the time remaining. The run deadline is 4 hours.

**What the aggregate re-checks.** It re-derives every run's outcome from the run's retained stage evidence, and rejects any of the following:

- other harness, arc or prereg digests;
- mixed development and held-out goals;
- another install release or candidate;
- a passing stage without its evidence;
- totals that contradict the stage rows;
- a mismatched ops scope.

**Consistency.** `completed + blocked + failed + undetermined` must equal `scheduled`, and receipt digests must be unique.

**Voids.** Each void needs:

- a retained original receipt;
- a `harness-fault` finding by the other model family, adjudicated after the run ended and before any later run started;
- a sealed reserve replacement that started after the adjudication.

## Artifacts (stable names; #1161 matches them)

- `ai-arc-campaign-<arm>-<target>`: one per campaign job. `<target>/schedule.json`, `<target>/voids.json`, and `<target>/runs/<runId with ':' as '-'>/trial-receipt.json` with that run's redacted `evidence/`.
- `ai-arc-aggregate-<arm>`: `ai-arc-aggregate-<target>-<arm>.json` (`honua.ai-arc.aggregate/v1`) and `ai-arc-report-<target>-<arm>.json`, one pair per target.

`gating-scope.json` is owned here and ships unapproved (`gating` empty, `approvedBy` null). The release session fills it after the founder's decision, and #1161 only reads it.

## Running

- **Workflow** (one run per arm, never dispatched from a lane): `gh workflow run ai-arc.yml -f arm=A1 -f targets=local-docker -f lock_digest=... -f lock_run_id=... -f quickstart_revision=<honua-server sha> -f goal_set=held-out`.
- **Controller:** `tools/ai_arc_local.sh --arm A1 --target local-docker --lock-path platform-lock.json --quickstart quickstart.md --quickstart-revision <sha> --hosts honua-eval-1,honua-eval-3 --out <new dir>`.
- **Tests:** `python -m pytest tools/test_ai_arc_*.py`. The composed-interface tests in `tools/test_ai_arc_integration.py` run when the #1160 and #1152 modules are importable, for example with `PYTHONPATH=tools:<#1160 checkout>/tools`.
