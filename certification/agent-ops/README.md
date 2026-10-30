# Operate-stage harness (OPS v2)

Campaign E measures the shipped operator configuration on a scripted stand-in and, later, on a priced model. It is not GA evidence. Campaign Q bars are reported beside the intervals and are not applied here.

The shipped configuration is Bedrock `us.anthropic.claude-sonnet-4-6` and `guidance=none`: the prompt lists published tool names and does not add `requiredSequence` or a stage order. The exploratory arm is `claude-haiku-4-5`. This lane does not call either model.

## Temperature

The shipped temperature is **1**. It is not chosen for this campaign, and it is not the canary client's hardcoded `0`.

The customer path omits the field:

- `StudioAiChatRequest.Temperature` is optional and falls back to the provider default when omitted (`honua-server` `StudioAiChatModels.cs`).
- `StudioAiProxyConfiguration` has `MaxTokens = 4096` and no temperature. Customer environment docs set `StudioAiProxy__MaxPromptCharacters` (`128000`) and `StudioAiProxy__MaxRequestBytes` (`1048576`) and do not set a temperature.
- `BedrockStudioAiProxyAdapter.BuildChatOptions` leaves `Temperature` null when the request omits it. `BedrockChatClientAdapter.MapInference` writes `inference.Temperature` only when a value is present.
- The JS SDK sends `temperature` only when the caller set it (`honua-sdk-js` `src/studio-agent/session.ts`).

When the field is omitted, the Bedrock Claude Messages API default is 1 (minimum 0, maximum 1): [Anthropic Claude Messages API request parameters](https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages-request-response.html). `summary.json` records `shipped_temperature` and `shipped_temperature_source`. The operate client omits the field so the signed proxy body matches that customer request. `tools/terminal_model_canary.py` still sends `0`; this harness does not edit it.

## Judge

R0, R3 and R4 run first. Ambiguous text uses one fixed fallback: inference profile `us.openai.gpt-6.1-terra` (`family: openai`, `mode: bedrock-fallback`). That profile is on the account and on the canary invoke allowlist in `certification/cloud-cell-inventory/main.tf`, so the live job's role can call it. No other non-Anthropic model was selected. Its list price is not recorded here, so a fallback call does not invent a USD cost.

Each fallback call includes the check (`R0`, `R3`, `R4`, or the no-change report), its purpose, what `pass` means, the rubric, and the expected evidence. `R0` can only set the false-success bit. It cannot turn an unresolved trial into a success. A reply that is not exactly `pass` or `fail` is not scored. The case is written under `review-queue/` and the trial stays unresolved. Stub runs do not call Bedrock. The live job installs `boto3` and passes the assumed role through `HONUA_CANARY_AWS_*`. `human-review-queue` is the mode only when no such model is invocable; it was not selected. The fallback sampler is 0 because it only classifies. That sampler is not the candidate temperature.

## Images, ledger, and scope

`aws-ecs` is unwired. The release session connects that target to the cloud cell later. Phrasing C, O11, O12 and the unseen faults stay in `held-out/` and are not opened unless `--qualification` is set.

Migration 142 is the reader-incompatible schema version named by the preregistration. It is not a numbered SQL file in honua-server. The oracle reads the controller's committed deployment record. The controller's prior and target digests stay the synthetic values in `constants.py`. `server_readback` stays false. Corpus layers are not published through the server admin API.

`--execute-compose` creates a compose project per trial (`{slug}-gis-{token}`), then removes it. `--image-source local` uses images that are already present and does not pull. `--image-source pull` pulls `ghcr.io/honua-io/honua-server:2026.1-rc`, the newer image as `ghcr.io/honua-io/honua-server:trunk-<40-hex trunk sha>-amd64` (the native-AOT tag published by honua-server) or `ghcr.io/honua-io/honua-server@sha256:<digest>`, tags that newer image `honua-server:trunk`, and pulls `postgis/postgis:16-3.4`. Trunk at `13d89d62f9065297d260429d7d94c9716a9e7606` has no `trunk-<sha>-amd64` tag, and the floating `trunk-amd64` tag currently resolves to the same amd64 manifest as `2026.1-rc`. When the sha tag is missing, a live run with `--server-source` checks out that commit and builds `docker/Dockerfile.aot` with `HONUA_GIT_SHA` set to the recorded sha, then tags `honua-server:trunk`. The built image is kept only when its revision label matches that sha. Both server digests are recorded on `summary.json` under `images`. If either image is missing, the run stops with reason `images` before a model call.

`--scope` on the workflow is `pilot` (default) or `full`. Pilot passes `--pilot` and runs `O01-A-1`, `O05-A-1`, and `O08-A-1`. A full run passes `--pilot-summary` and refuses, before any trial, unless that file is a completed priced live pilot of the same arm with `n: 3` and no stop reasons. A stub summary or a full-run summary does not authorize the next full run. The workflow loads the file from the `operate-summary` artifact of `prior_run_id`.

`--ledger` writes `cap_usd`, `spent_usd`, and one entry per trial. The default cap is 250 USD. Live cost uses the measured Bedrock token counts (`promptTokens` / `completionTokens`, mapped from `InputTokenCount` / `OutputTokenCount`) and the arm rates: primary $3 / $15 per million input / output tokens, exploratory $1 / $5. Stub rows keep the character estimate, record `cost_usd: null`, and do not add spend. A trial that would start at or above the cap is not started. The trial that crosses the cap is kept. The workflow uploads `summary.json`, `ledger.json`, `trials.jsonl`, `transcripts/`, and `audit/` as artifact `operate-summary` for 90 days.

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
  -f newer_sha=13d89d62f9065297d260429d7d94c9716a9e7606
```

`newer_sha` is the honua-server trunk commit the image was built from. The value above is trunk at the time this harness was updated. A full run adds `-f scope=full -f prior_run_id=<run id of a clean pilot>`.

The release session's dry run, after the freeze, is:

```bash
"$GSTACK_ROOT/bin/gstack-eval-plan" dry-run \
  --input certification/agent-ops/rows/development.jsonl \
  --arms claude-sonnet-4-6,claude-haiku-4-5 \
  --retry-prompt certification/agent-ops/retry-prompt.txt \
  --out /tmp/gis-ops-dry-run
```

A stub summary has `"priced": false` and `"model": null`. It is the harness check, not a Sonnet measurement.
