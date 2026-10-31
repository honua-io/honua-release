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
- **Setup on AWS.** The runner downloads honua-devops v2026.1.2, verifies its `.sha256` and archive digest against the pins in `arc.v1.json`, and runs `Honua.DevOps.Agent --mcp` as #1160's `StdioMcpBackend` with `HONUA_DEVOPS_DEPLOY_IDENTITY_CONTRACT_PATH` set to the honua-iac `bootstrap/aws-exec-identity` `deploy_identity_contract` export for the agent-chosen topology, plus a short-lived session for that contract's role only. The harness, as the customer's admin, applies that bootstrap after the agent's plan is approved, through g43's `operator_identity` hook or a slot provisioner. The runner checks the contract the way the operator does and records its digest, role and topology. This is equivalent to `claude mcp add honua-devops -- <dir>/Honua.DevOps.Agent --mcp` (or `codex mcp add`), and the registration is recorded in the receipt. `tools/ai_arc_devops.py` wires #1165's install harness unchanged: `StdioOperator`, the KMS-MAC `ApprovalIssuer`, `AwsController` and `InstallHarness` for plan review, approval binding, apply gating and handoff verification. The arc's session drives the harness through one `honua_devops` backend. The cell stays up for the later stages and is torn down when the run ends. It needs the slot file, the bootstrap outputs and the operator binary from the release cell machinery. EKS and Lambda are unavailable until #1160's adapter admits them.
- **Ops.** The server `/mcp` ops and proposal tools. The harness is the separate approver, and the product's executor applies the change. `certification/agent-ops/arc_ops.py` gates the stage on the product executors listed in `arc-executors.json`: honua-server #6824 (Compose), #6825, #6812, #6813 and #6814, and honua-iac #351. A gate counts as merged only once its merge commit is recorded. The stage then delegates to #1164's driver (`product_ops` / `ecs_stage`). **`stage-unavailable` until every gate for the target is merged.** Ops scope is per target (see `gating-scope.json`). Unscored ops assertions are listed as `unscored` in the stage and in every aggregate.
- **Services.** #1152 `execute_case` for the scenarios listed in `arc.v1.json` (`stages[services].scenarios`, now P10 and P15 as supported on trunk, frozen at Sun 23:00 from what is supported then) on the arc's stack, sharing one stage budget, with the plain handoff ahead of the first scenario's pack prompt, a pre-model credential screen on every response, using #1160's CLI corpus session for the arm, the vault records, frozen projections, customer files and the scenario handoff. Private evidence stays outside the upload, and only `export_case_evidence`'s redacted copy is exported. **`stage-unavailable` while a scenario does not support the variant.**
- **Studio.** Studio is the map editor embedded in Honua Console (GA). The required path is Console's built-in AI assistant, which calls the server's Studio AI proxy on Bedrock Sonnet 4.6. A scripted customer drives it, signed in to Console as the customer principal. CLI arms reach Console through #1162's origin-locked browser with real sign-in. Scoring uses saved map and style documents (controller reads) plus an independent render through the server's map rendering API. `stages.studio.assistant` is the model the Console assistant was observed to use. `tools/agent_e2e_studio_arc.py` runs the arm's Studio scenarios through #1162's browser (`agent_browser_studio`, the corpus case on the arc's stack) and the assistant through `agent_e2e_console_assistant`. **`stage-unavailable` until both drivers land, and on any slot where no Console is serving.** Console is consumed from the customer's Compose install when it has a `console` service (tunnelled), or from the ECS cell's `consoleHostname`. Deployment gaps, recorded in `arc.v1.json` `consoleDeployment`:
  - The published quickstart has no `console` service. For stage 4 the harness adds honua-server's own `docker-compose.yml` `console` service, read at an exact commit (`--console-compose`, `--console-compose-revision`).
  - honua-iac #359 and honua-devops #248 deploy Console on the ECS cell behind the OIDC edge.
  - **Console identity is bound.** `arc.v1.json` `console` names the single Console release pin (now `38a1c2b3…`: Studio #156 widget transport, Studio #158, Console #493). The compose must select it, and the running container must report it. Any other Console fails closed (#6828's compose selects `6612319d…` until honua-server #6829 re-pins it).

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

- `ai-arc-campaign-<arm>-<target>`: small, and the only artifact the admission gate reads. It holds `schedule.json`, `voids.json`, `runs/<runId with ':' as '-'>/trial-receipt.json` (each under 1 MB, enforced), `ai-arc-aggregate-<target>-<arm>.json` (`honua.ai-arc.aggregate/v1`) and `ai-arc-report-<target>-<arm>.json`.
- `ai-arc-evidence-<arm>-<target>`: redacted bulky evidence, one directory per run (CLI wire logs, observations, final answers, corpus controller records). Each run receipt's `evidenceArtifact` names this artifact and lists every file's path, sha256 and size. The installed operator release is never uploaded; its tag, commit and archive and binary sha256 are in the receipt.

`gating-scope.json` is owned here and ships unapproved (`gating` empty, `approvedBy` null). The release session fills it after the founder's decision, and #1161 only reads it.

## Running

- **Workflow** (one run per arm, never dispatched from a lane): `gh workflow run ai-arc.yml -f arm=A1 -f targets=local-docker -f lock_digest=... -f lock_run_id=... -f quickstart_revision=<honua-server sha> -f goal_set=held-out`.
- **Controller:** `tools/ai_arc_local.sh --arm A1 --target local-docker --lock-path platform-lock.json --quickstart quickstart.md --quickstart-revision <sha> --hosts honua-eval-1,honua-eval-3 --out <new dir>`.
- **CLI pins:** each arm's `cliVersion` in `arc.v1.json` is the CLI it must run (`2.1.316 (Claude Code)`, `codex-cli 0.166.0`). A session refuses to start on any other installed version, and the aggregate rejects a receipt whose session ran another one.
- **Development candidate** (rehearsals only, while no published trunk image exists): `tools/ai_arc_dev_candidate.sh <honua-server checkout> <trunk sha> dev-lock.json honua-eval-1 honua-eval-2 honua-eval-3` builds trunk once (labelled with the sha) and copies it to the other hosts, so every host has the same image id. Pass `--lock-path dev-lock.json` to `trial`. Receipts record `candidateKind: development-image`, and the aggregate is never certifying.
- **Freeze services:** `python3 tools/ai_arc_runner.py freeze-services` lists the scenarios this checkout supports for A and B (minus prereg exclusions), for `stages[services].scenarios` at Sun 23:00.
- **Tests:** `python -m pytest tools/test_ai_arc_*.py`. The composed-interface tests in `tools/test_ai_arc_integration.py` run when the #1160 and #1152 modules are importable, for example with `PYTHONPATH=tools:<#1160 checkout>/tools`.
