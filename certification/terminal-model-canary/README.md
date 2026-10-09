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

## Evidence boundary

Every action is attributed as either:

- `MODEL_SELECTED`: a command or tool call parsed from a captured model response; or
- `HARNESS_DRIVEN`: workspace setup, error injection, separate-principal approval, verification, or
  teardown.

Model actions must reference the redacted assistant transcript entry that selected them. The harness
injects one recoverable error through the driver, records the harness action that armed it, the model
action whose driver result reports that exact error ID, and the later model action whose driver result
reports recovery of that ID. Prompts, responses, requests, and results are recursively redacted;
the receipt stores only their SHA-256, UTF-8 byte count and `digest-only` marker.
The schema rejects raw payloads and extra fields. Redacted model context stays in
memory for subsequent turns and is never replayed from receipt digests. Each live run
uses a cryptographically random nonce, signed provider-event bytes bind both SSE names and data, and
the signed reported model must exactly match the requested model.

## Attempt identity, lock binding and failure attribution

Every receipt is one attempt of the `genuine-model` journey on one cell (fix unit J3 of the 2026.1
rc.3 plan). The schema requires:

- `mode` — always `genuine-model`.
- `cell` — `local-docker` or `aws-ecs/redis-off` (`--cell`).
- `attempt` — `1` or `2` (`--attempt`); promotion allows one retry after an attributed failure.
- `lockDigest` — `sha256:<hex>` of the exact `platform-lock.json` under certification
  (`--lock-digest`). With `--lock <path>` the harness hashes those bytes and fails the receipt if they
  differ. A live run without a lock digest is refused (`blocked`): an unbound receipt counts for no
  lock. A pass without one is rejected by both the harness and the schema.
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
  no `AWS_*` variable enters the job environment. Without a `lock_digest` input it binds the latest
  minted `nightly-lock/*` tag and certifies that lock's manifest.
- **dispatch or call `target=aws-ecs/redis-off`** — calls `e2e-cloud-aws.yml` for the ECS Redis-off
  cell with `genuine_model_bedrock: true`. The cell is provisioned with `enable_bedrock_ai=true` and
  `bedrock_ai_region=us-east-1` (`ECS_SPEC.opt_in_vars`, `HONUA_ENABLE_BEDROCK_AI`), and its credential-free journey job
  runs `e2e/run_cloud.py --phase model-canary` after the cell journey. A lock digest is required.
- **nightly train** — `nightly-certification.yml` passes `genuine_model` to `release-train.yml`, which
  forwards it with the qualification lock digest to its own cloud cells (`gate_cloud_parity`). The
  ECS Redis-off cell uploads `journey-gate-report-nightly-model`, which the train's
  `--declare-evidence` step reads. The canary runs inside that cell rather than a second workflow
  because the cell admits only its own run's journey runner.
- **dispatch `target=endpoint`** — the original harness against an operator-named endpoint, on
  `ubuntu-latest` or `self-hosted`.

### Repository settings the owner configures

| Kind | Name | Purpose |
|---|---|---|
| variable | `HONUA_AWS_ROLE_ARN` | OIDC role the local-docker job assumes; it must allow `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on the Sonnet 4.5 inference profile and its foundation-model ARNs (honua-iac `bootstrap/aws-release-cells` `runtime_bedrock_model_arns`). The cloud cell uses the same role to provision; Bedrock there is the ECS task role from `modules/aws-ecs/bedrock.tf`. |
| secret | `TERMINAL_MODEL_TRANSCRIPT_SIGNING_SEED` | Base64 32-byte Ed25519 seed the local-docker candidate signs transcripts with (`StudioAiProxy:TranscriptSigning`, key id `terminal-model-canary`, reference `env://HONUA_CANARY_TRANSCRIPT_SIGNING_SEED`). |
| variable | `TERMINAL_MODEL_SIGNING_MANIFEST_SHA256` | SHA-256 of the canonical signing manifest the candidate publishes for that key: the trust anchor. |
| variable | `TERMINAL_MODEL_NAME` (optional) | Model id; defaults to `us.anthropic.claude-sonnet-4-5-20250929-v1:0`. |
| secret | `RELEASE_GH_TOKEN` | Already required by the journey for pinned client sources. |

Bedrock model access for Claude Sonnet 4.5 must be enabled in the release account.

Until #123's live adapter drives the candidate to green, the honest terminal state is `skipped`,
`blocked` or an attributed `fail`.
