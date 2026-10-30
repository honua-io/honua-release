# Terminal-model canary harness

This directory defines the harness-only evidence contract for honua-release#161. It does not claim
that a model has completed the 2026.1 journey. Live execution remains sequenced after the deterministic
#123 driver is green against the same candidate.

The harness imports `../terminal-journey/journey.v1.json`; stage numbers, IDs, commands, and milestones
are never copied into the canary. Every receipt records that file's path and SHA-256. The current #123
artifact builds an honestly blocked receipt, but does not expose a live action adapter. #123 must
implement `driver-protocol.v1.json` at its declared
`certification/terminal-journey/live_driver.py` path before this harness can execute to green. The
workflow does not accept an arbitrary executable path.

## Endpoint configuration

The client uses the candidate server's `POST <base-url>/v1/studio/ai/chat` signed SSE
proxy. For the #377 promise journey, configure a provider named `bedrock` with
adapter kind `bedrock`; the harness checks that capability, explicitly selects it,
and requires signed Claude-on-Bedrock provenance. It never calls a provider directly.

- `TERMINAL_MODEL_BASE_URL` — candidate API base URL (normally ending in `/api`),
  or the complete `/v1/studio/ai/chat` URL. Plain HTTP is restricted to loopback;
  redirects and direct provider URLs are refused before credentials are sent.
- `TERMINAL_MODEL_NAME` — exact Claude model identifier accepted by the candidate's
  Bedrock adapter: an AWS-controlled `anthropic.claude-*` foundation model ID or a
  system inference profile ID prefixed with `us.`, `eu.`, `apac.`, or `global.`
  (for example, `us.anthropic.claude-sonnet-4-6`). Operator-controlled aliases,
  custom/imported model ARNs, and application inference profiles cannot certify the
  model family and are rejected. Supply IDs rather than ARNs. See the
  [AWS model ID documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/models-get-info.html).
- `TERMINAL_MODEL_API_KEY` — optional bearer credential for hosted/key-based endpoints. Local endpoints
  select authentication `none`, so this hosted credential is neither read nor forwarded. Hosted runs
  select `bearer` and fail closed if the secret is absent. Only the environment-variable reference is
  recorded.
- `TERMINAL_MODEL_RUNTIME` and `TERMINAL_MODEL_QUANTIZATION` — explicit receipt metadata.
- `TERMINAL_MODEL_SIGNING_MANIFEST_SHA256` — required platform-controlled SHA-256 of the canonical
  candidate transcript-signing manifest. Candidate-published keys are accepted only when that
  manifest matches this independently configured trust anchor.

Missing endpoint configuration produces a `skipped` receipt and a visible notice. It can never produce
`pass`. A configured endpoint without the #123 adapter produces `blocked`, naming that dependency.
An attempted live run also requires a repository-local path to #123's green receipt plus explicit
runtime and quantization identifiers. The harness parses that receipt, requires a passing roster and
all imported stages, enforces a 24-hour freshness window, and exact-matches its release, server, and
#123 client-artifact pins to the candidate manifest. It records the validated receipt's path and hash;
an arbitrary, stale, blocked, incomplete, or differently pinned receipt fails before model execution.

The workflow `model` input stays free text. When it is empty, the run uses the repository
variable or the profile named in the workflow. Ids:

- Shipped default, until that workflow default changes: `us.anthropic.claude-sonnet-4-6`
- Latest models: `us.anthropic.claude-opus-5-5`, `us.anthropic.claude-sonnet-5-5`,
  `us.openai.gpt-6.1-sol`, `us.openai.gpt-6.1-terra`

The promise-journey provenance check accepts only `anthropic.claude` ids. Opus 5.5 and
Sonnet 5.5 match that check. GPT-6.1 Sol and Terra do not, until
`tools/terminal_model_canary.py` accepts them. Sonnet 4.5, Haiku 4.5, and GPT-5 are not
ids for this harness.

## Evidence boundary

Every action is attributed as either:

- `MODEL_SELECTED`: a command or tool call parsed from a captured model response; or
- `HARNESS_DRIVEN`: workspace setup, error injection, separate-principal approval, verification, or
  teardown.

Model actions must reference the redacted assistant transcript entry that selected them. The harness
injects one recoverable error through the driver, records the harness action that armed it, the model
action whose driver result reports that exact error ID, and the later model action whose driver result
reports recovery of that ID.

Intermediate diagnostic actions may succeed without proving recovery. They consume
the existing stage action budget; the stage cannot complete until a candidate
result proves recovery of the exact injected error. Unrelated success, a different
error ID, a false recovery claim or exhausted limits cannot pass this requirement.

Prompts, responses, requests, and results are recursively redacted; the receipt
stores only their SHA-256, UTF-8 byte count and `digest-only` marker.
The schema rejects raw payloads and extra fields. Redacted model context stays in
memory for subsequent turns and is never replayed from receipt digests. Each live run
uses a cryptographically random nonce, signed provider-event bytes bind both SSE names and data, and
the signed reported model must exactly match the requested model.

## Attempt identity, lock binding and failure attribution

Every receipt is one attempt of the `genuine-model` journey on one cell (fix unit J3 of the 2026.1
rc.3 plan). The schema requires:

- `mode` — always `genuine-model`.
- `cell` — `local-docker` or one of `{aws-ecs, aws-serverless}/redis-{off,on}` (`--cell`); EKS remains Preview.
- `attempt` — `1` or `2` (`--attempt`); promotion allows one retry after an attributed failure.
- `lockDigest` — `sha256:<hex>` of the exact `platform-lock.json` under certification
  (`--lock-digest`). With `--lock <path>` the harness hashes those bytes and fails the receipt if they
  differ.
- `serverImage` — the honua-server `image@digest` the cell composes from `platform-manifest.yaml`
  (the same reference the deterministic #123 receipt must name). Operator ruling 2026-10-22: rc
  gates need a green trunk run against any build, so a run without a caller's lock binds this image
  instead. It binds a pass only once the green deterministic receipt verified that candidate. A live
  run bound to neither is refused (`blocked`), and a pass bound to neither is rejected by both the
  harness and the schema. An image-bound receipt counts for no lock in promotion readiness.
- `failureAttribution` — `null` exactly when the attempt passed; otherwise `model` or
  `infrastructure`. `model` means the model failed: no parseable or valid action, no stage progress
  within the bounded action budget, no recovery from the injected error, never reaching the approval
  boundary, or final assertions that fail after verification itself answered. Everything else
  (configuration, the deterministic prerequisite, the lock, the driver, the candidate proxy or
  provider, provenance, teardown) is `infrastructure`. The first attribution wins, so a teardown error
  after a model failure stays a model failure.
- `completedAt` — when the attempt finished.

Plain HTTP is accepted for loopback, and for exactly one other host: the cloud cell's own
harness-provisioned load-balancer hostname, named with `--allow-http-cell <hostname>` (owner ruling
`canary-http-cell-2026-10-08` in `docs/2026.1-release-decision-overrides.json`). The cell's
application key is random per run and is destroyed with the cell. HTTPS on the cell is 2026.1.x
hardening. Any other non-loopback HTTP host is still refused, and the exemption never covers a
provider endpoint (only an `*.elb.amazonaws.com` host qualifies). The receipt records
`endpoint.transport` (`https`, `http-loopback` or `http-cell-allowed`) and `endpoint.transportHost`,
so promotion evidence states how the key travelled. `e2e/run_cloud.py --phase model-canary` passes
the provisioned cell's host.

The harness also refuses a run whose driver reports a different candidate endpoint (`baseUrl`) than
the model proxy: a driver that composed a local stack cannot certify a cloud cell, or the reverse.

`tools/model_journey_report.py` folds a cell's attempts into the journey row that
`tools/mint_nightly_lock.py --declare-evidence` retains as
`promotion-receipts/nightly-model-journey/receipt.json`. Each attempt keeps its own `lockDigest`, and
`tools/check_promotion_readiness.py` refuses any attempt bound to another lock.

## Workflow

`.github/workflows/terminal-model-canary.yml`:

- **schedule** (08:45 UTC, after the deterministic local-docker and cloud journeys) and
  **dispatch `target=local-docker`** — one job runs the deterministic #123 journey
  (`certification/terminal-journey/run.py --mode live --target targets/local-docker.json`) first, then
  assumes the Bedrock-enabled role over OIDC and runs the canary against the candidate stack with that
  job's receipt as `--deterministic-receipt`. `targets/build-compose.yml` routes the candidate's
  `StudioAiProxy` to Bedrock (`StudioAiProxy__Enabled`, `DefaultProvider=bedrock`,
  `Providers__bedrock__{Kind,Model,Region}`) only when the job sets `HONUA_CANARY_STUDIO_AI_ENABLED`.
  The session credentials reach the server container under non-ambient `HONUA_CANARY_AWS_*` names;
  no `AWS_*` variable enters the job environment. Without a `lock_digest` input (the schedule) it binds the
  manifest-pinned server image the job composes; it no longer needs a minted `nightly-lock/*` tag.
- **dispatch or call `target=aws-ecs/redis-off`** — calls `e2e-cloud-aws.yml` for the ECS Redis-off
  cell with `genuine_model_bedrock: true`. The cell is provisioned with `enable_bedrock_ai=true` and
  `bedrock_ai_region=us-east-1` (`ECS_SPEC.opt_in_vars`, `HONUA_ENABLE_BEDROCK_AI`), and its credential-free journey job
  runs `e2e/run_cloud.py --phase model-canary` after the cell journey. This workflow refuses the
  dispatch without a lock digest. Dispatching `e2e-cloud-aws.yml` directly with `genuine_model_bedrock`
  and no `lock_digest` (no nightly lock minted yet) runs the canary once unbound: the harness writes
  its own `blocked` receipt and `tools/model_journey_report.py` a `blocked` row with a null lock digest,
  surfaced as a notice. It counts for no lock and is never a pass.
- **nightly train** — `nightly-certification.yml` passes `genuine_model` to `release-train.yml`, which
  forwards it with the qualification lock digest to its own cloud cells (`gate_cloud_parity`). The
  selected GA cells upload distinct `journey-gate-report-nightly-model-<cell>-<attempt>` artifacts, which the train's
  `--declare-evidence` step reads. The canary runs inside that cell rather than a second workflow
  because the cell admits only its own run's journey runner.
- **dispatch `target=endpoint`** — the original harness against an operator-named endpoint, on
  `ubuntu-latest` or `self-hosted`.

### AWS GA topology support and qualification boundary

The cloud workflow's explicit `genuine_model_bedrock` opt-in enables the model path on selected ECS
and Lambda cells in both Redis modes. Defaults remain off, and EKS Preview is excluded. Both maintained
Terraform target adapters require the pinned root to declare the provider inputs before passing them.
The journey worker receives application credentials, datasource references and the run-owned tenant
fixture through its sealed handoff; it receives no cloud or CI token environment.

Each cloud canary passes `--target` with the same retained target configuration used by its deterministic
journey. Its cell, SHA-256 and endpoint origin must agree. The driver verifies the linked receipt digest
and deployment-image observation; ECS uses the generic server image and Lambda uses the distinct pinned
`awsLambdaImage`/`awsLambdaDigest`. It observes the external endpoint, mints bounded application principals,
and revokes them after the attempt. Target changes and stale attempt files cannot replace current execution.
Harness fixture resets are recorded separately from model actions.

Redis-on uses `--profile full-workflow` and retains all eight stages, actual model-selected calls,
recoverable-error recovery, independent approval, security checks, pixel proof and final-URL proof.
Redis-off uses the separately authored `redis-off-serving-refusal` profile and receipt schema. The model
must select a serving query of the cell's authored `e2e_src_fs` publication that matches an independent
GeoServices read and both exact seed rows (IDs, names and geometries), attempt a valid buffer
plan, observe the actual typed `unavailable`/`missingDependency=redis` refusal, then select a subsequent
capability read acknowledging that refusal. The independent oracle requires `jobs.runner` unavailable
with `reasonCode=dependency-unavailable`. Authentication, anonymous/viewer denial and exact HTTP/proxy
catalog checks must pass. Every model action still requires the same signed, non-replayed candidate
transcript provenance. The profile reports `executionOutcome: refused` and `functionalGpExecuted: false`;
it makes no GP-completion, map persistence, publication, approval or final-URL claim.

The retained `extended/seed-manifest.json` resolves the allocated layer ID. Its SHA-256, the maintained
seed-script and extracted-SQL hashes, and the expected fixture-data digest are chained into setup state
and serving proof; later source/manifest changes are refused. Two equal but wrong adapters cannot qualify.

The explicit Redis-off prerequisite accepts a fresh, same-candidate deterministic receipt whose stages
1–2 passed and whose stage 5 records the actual typed dependency refusal. No failed stage is allowed.
Later blocked full-workflow stages retain their raw status: the linked proof names the bounded profile
and `sourceStatus`, and never changes the raw deterministic receipt to pass. Other profiles continue to
require the entire deterministic journey green. The report, nightly evidence declaration and promotion
consumer preserve and check the profile/outcome for each attempt. The broader deterministic cloud gate's
handling of blocked Redis-off full journeys remains a separate qualification boundary.

Unit and workflow tests establish these contracts only. Fresh exact-candidate signed model receipts are
still required. AWS candidates also need the maintained signing-secret wiring in honua-iac#256, the
server AWS signing-reference validator in honua-server#6292, reviewed operator key/ARN bindings and the
published signing-manifest trust anchor. A public manifest hash does not supply a private signing key.

### Repository settings the owner configures

| Kind | Name | Purpose |
|---|---|---|
| variable | `HONUA_AWS_ROLE_ARN` | OIDC role the local-docker job assumes; it must allow `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on the Sonnet 5.5 `us.` inference profile and its foundation-model ARNs (honua-iac `bootstrap/aws-release-cells` `runtime_bedrock_model_arns`). The cloud cell uses the same role to provision; Bedrock there is the ECS task role from `modules/aws-ecs/bedrock.tf`. |
| secret | `TERMINAL_MODEL_TRANSCRIPT_SIGNING_SEED` | Base64 32-byte Ed25519 seed the local-docker candidate signs transcripts with (`StudioAiProxy:TranscriptSigning`, key id `terminal-model-canary`, reference `env://HONUA_CANARY_TRANSCRIPT_SIGNING_SEED`). |
| variable | `TERMINAL_MODEL_SIGNING_MANIFEST_SHA256` | SHA-256 of the canonical signing manifest the candidate publishes for that key: the trust anchor. |
| variable | `TERMINAL_MODEL_NAME` (optional) | Model id; defaults to `us.anthropic.claude-sonnet-5-5`. |
| secret | `RELEASE_GH_TOKEN` | Already required by the journey for pinned client sources. |

Bedrock model access for Claude Sonnet 5.5 must be enabled in the release account.

Until #123's live adapter drives the candidate to green, the honest terminal state is `skipped`,
`blocked` or an attributed `fail`.
