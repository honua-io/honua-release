# Operate-stage harness (OPS v2)

Campaign E measures the shipped operator configuration on a scripted stand-in and, later, on a priced model. It is not GA evidence. Campaign Q bars are reported beside the intervals and are not applied here.

The shipped configuration is Bedrock `us.anthropic.claude-sonnet-4-6` and `guidance=none`: the prompt lists published tool names and does not add `requiredSequence` or a stage order. Exploratory arms, beside that default, are Claude Opus 5.5, Claude Sonnet 5.5, GPT-6.1 Sol and GPT-6.1 Terra. This lane does not call any of them. Haiku 4.5 is no longer an arm.

## Temperature

The shipped temperature is **1**. It is not chosen for this campaign, and it is not the canary client's hardcoded `0`.

The customer path omits the field:

- `StudioAiChatRequest.Temperature` is optional and falls back to the provider default when omitted (`honua-server` `StudioAiChatModels.cs`).
- `StudioAiProxyConfiguration` has `MaxTokens = 4096` and no temperature. Customer environment docs set `StudioAiProxy__MaxPromptCharacters` (`128000`) and `StudioAiProxy__MaxRequestBytes` (`1048576`) and do not set a temperature.
- `BedrockStudioAiProxyAdapter.BuildChatOptions` leaves `Temperature` null when the request omits it. `BedrockChatClientAdapter.MapInference` writes `inference.Temperature` only when a value is present.
- The JS SDK sends `temperature` only when the caller set it (`honua-sdk-js` `src/studio-agent/session.ts`).

When the field is omitted, the Bedrock Claude Messages API default is 1 (minimum 0, maximum 1): [Anthropic Claude Messages API request parameters](https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages-request-response.html). `summary.json` records `shipped_temperature` and `shipped_temperature_source`. The operate client omits the field so the signed proxy body matches that customer request. `tools/terminal_model_canary.py` still sends `0`; this harness does not edit it.

## Judge

R0, R3 and R4 run first. Ambiguous text uses one fixed fallback: inference profile `us.openai.gpt-6.1-terra` (`family: openai`, `mode: bedrock-fallback`). That profile is the configured judge and is named on the canary invoke allowlist in `certification/cloud-cell-inventory/main.tf`. The account currently reports that it is not available, so a failed call is queued rather than scored. No other non-Anthropic model was selected. Its list price is not recorded here, so a fallback call does not invent a USD cost.

Each fallback call includes the check (`R0`, `R3`, `R4`, or the no-change report), its purpose, what `pass` means, the rubric, and the expected evidence. `R0` can only set the false-success bit. It cannot turn an unresolved trial into a success. A reply that is not exactly `pass` or `fail` is not scored. The case is written under `review-queue/` and the trial stays unresolved. Stub runs do not call Bedrock. The live job installs `boto3` and passes the assumed role through `HONUA_CANARY_AWS_*`. The configured judge stays `us.openai.gpt-6.1-terra`. The account currently answers that this profile is not available, so a failed fallback call is written to `review-queue/` with detail `human-review-queue` and is not auto-scored. The fallback sampler is 0 because it only classifies. That sampler is not the candidate temperature.

## Endpoint

The operator client is the Studio AI proxy on the composed server, the same path the canary's local-docker job uses. It is not a direct Bedrock Converse call. `OperateClient` posts to `{base}/v1/studio/ai/chat` and presents the per-run admin key as `X-API-Key`.

The repository has no `TERMINAL_MODEL_BASE_URL` variable. The live job sets that URL to `http://127.0.0.1:8147/api`, publishes the server on `HONUA_PORT` 8147, and enables the proxy with `studio-proxy.yml`: `HONUA_CANARY_STUDIO_AI_ENABLED`, `HONUA_CANARY_BEDROCK_MODEL` (the selected arm), `HONUA_CANARY_BEDROCK_REGION` `us-east-1`, and `HONUA_CANARY_TRANSCRIPT_SIGNING_SEED`. The same file approves that exact provider, model, region, and `studio-chat` use case, and names the admission deployment `operate-live`. The signing manifest is `TERMINAL_MODEL_SIGNING_MANIFEST_SHA256`. A live server also needs a connection-encryption master key of at least 32 characters; the stub keeps `local-only`. A live run whose base URL, admin key, or signing manifest is unset stops with reason `endpoint` before a trial and before a model call. The `pilot-stub` job runs that refusal.

## Images, ledger, and scope

`aws-ecs` is unwired. The release session connects that target to the cloud cell later. Phrasing C, O11, O12 and the unseen faults stay in `held-out/` and are not opened unless `--qualification` is set.

Migration 142 is the reader-incompatible schema version named by the preregistration. It is not a numbered SQL file in honua-server. The oracle reads the controller's committed deployment record. The controller's prior and target digests stay the synthetic values in `constants.py`. `server_readback` stays false. Corpus layers are not published through the server admin API.

`--execute-compose` creates a compose project per trial (`{slug}-gis-{token}`), then removes it. `--image-source local` uses images that are already present and does not pull. `--image-source pull` pulls `ghcr.io/honua-io/honua-server:2026.1-rc`, the newer image as `ghcr.io/honua-io/honua-server:trunk-<40-hex trunk sha>-amd64` or `ghcr.io/honua-io/honua-server@sha256:<digest>`, tags that newer image `honua-server:trunk`, and pulls `postgis/postgis:16-3.4`. honua-server publishes images from tags and `workflow_dispatch`, and that 40-hex tag was absent for `13d89d62f9065297d260429d7d94c9716a9e7606`. The floating `trunk-amd64` tag resolved to the same amd64 manifest as `2026.1-rc` then, so it is not the newer image. When the sha tag is missing, a live run with `--server-source` checks out that commit and builds `docker/Dockerfile.aot` with `HONUA_GIT_SHA` set to the recorded sha, then tags `honua-server:trunk`. The built image is kept only when its revision label matches that sha. Both server digests are recorded on `summary.json` under `images`. If either image is missing, the run stops with reason `images` before a model call.

`--scope` on the workflow is `pilot` (default) or `full`. Pilot passes `--pilot` and runs `O01-A-1`, `O05-A-1`, and `O08-A-1`. A full run passes `--pilot-summary` and refuses, before any trial, unless that file is a completed priced live pilot of the same arm with `n: 3` and no stop reasons. A stub summary or a full-run summary does not authorize the next full run. The workflow loads the file from the `operate-summary` artifact of `prior_run_id`.

`--ledger` writes `cap_usd`, `spent_usd`, and one entry per trial. The default cap is 250 USD. Live cost uses the measured Bedrock token counts (`promptTokens` / `completionTokens`, mapped from `InputTokenCount` / `OutputTokenCount`) and the arm rates: primary $3 / $15 per million input / output tokens, and the latest-model rates in the table below. Stub rows keep the character estimate, record `cost_usd: null`, and do not add spend. A trial that would start at or above the cap is not started. The trial that crosses the cap is kept. The workflow uploads `summary.json`, `ledger.json`, `trials.jsonl`, `transcripts/`, and `audit/` as artifact `operate-summary` for 90 days.

## Commands

From the honua-release worktree, with PyYAML, jsonschema, cryptography and pytest installed:

```bash
python -m pytest certification/agent-ops -q
python certification/agent-ops/runner.py --self-test
python certification/agent-ops/runner.py --target local-docker --model stub --arm primary \
  --pilot-summary /path/to/clean-pilot-summary.json \
  --rows certification/agent-ops/rows/development.jsonl --out /tmp/gis-ops-stub
python certification/agent-ops/score.py --results /tmp/gis-ops-stub/trials.jsonl
python certification/agent-ops/runner.py --target local-docker --model stub --pilot \
  --rows certification/agent-ops/rows/development.jsonl --out /tmp/gis-ops-pilot
python certification/agent-ops/runner.py --target aws-ecs --model stub --pilot --out /tmp/gis-ops-aws
```

Do not pass `--model live` or dispatch the workflow's live job from this lane.

The priced pilot, after this harness is on trunk, pulls the published `trunk-<sha>-amd64` image when it exists and otherwise builds that honua-server commit in the live job. Do not dispatch it from this lane:

```bash
gh workflow run agent-ops.yml --ref trunk \
  -f mode=live -f priced=yes -f target=local-docker -f arm=primary \
  -f scope=pilot -f spend_cap_usd=250 \
  -f newer_sha=b4a69b7093abe351babb9b68544733c945223974
```

`newer_sha` is the honua-server trunk commit the image was built from. The value above is trunk at the time this harness was updated. A full run adds `-f scope=full -f prior_run_id=<run id of a clean pilot>`.

The release session's dry run, after the freeze, is:

```bash
"$GSTACK_ROOT/bin/gstack-eval-plan" dry-run \
  --input certification/agent-ops/rows/development.jsonl \
  --arms claude-sonnet-4-6,claude-opus-5-5,claude-sonnet-5-5,gpt-6.1-sol,gpt-6.1-terra \
  --retry-prompt certification/agent-ops/retry-prompt.txt \
  --out /tmp/gis-ops-dry-run
```

A stub summary has `"priced": false` and `"model": null`. It is the harness check, not a Sonnet measurement.

Prices below are USD per 1M tokens for the `us.` profile (geo / US cross-region), standard short context, as of 2026-10. Cache writes and long-context rates are not in the table. Global CRIS is cheaper and is not the profile this grant uses.

| Arm | Bedrock id | Input | Output | Cache read |
|---|---|---:|---:|---:|
| `opus-5-5` | `us.anthropic.claude-opus-5-5` | 4.40 | 22.00 | 0.22 |
| `sonnet-5-5` | `us.anthropic.claude-sonnet-5-5` | 2.20 | 11.00 | 0.11 |
| `gpt-6.1-sol` | `us.openai.gpt-6.1-sol` | 2.20 | 11.00 | 0.11 |
| `gpt-6.1-terra` | `us.openai.gpt-6.1-terra` | 1.10 | 5.50 | 0.055 |

Opus 5.5 and Sonnet 5.5 come from the Amazon Bedrock pricing page, Geo and In-region table, price map `bedrockfoundationmodels` published 2026-10-12. Sol and Terra come from the AWS model cards that page links (US CRIS for Sol, Geo CRIS for Terra, at most 272K input).
# Execution integrity

The controller currently simulates operations and probes. No scenario has the
independent live readback required for an operate trial. Live runs are refused
with `not-real`; the stub is for unpriced self-tests only and cannot be combined
with `--priced-ok`. Passing a stub run is not evidence of deployment behavior.
Real execution requires product MCP proposal tools, approval by a separate
principal, a product-owned executor, concurrent customer probes, fault receipts,
and independent server and database observations. The local Compose executor
and restore gap is tracked in honua-server #6811.
