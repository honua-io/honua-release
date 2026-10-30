# Operate-stage harness (OPS v2)

Campaign E measures the shipped operator configuration on a scripted stand-in and, later, on a priced model. It is not GA evidence. Campaign Q bars are reported beside the intervals and are not applied here.

The shipped configuration is Bedrock `us.anthropic.claude-sonnet-4-6`, the canary client's own request temperature (0 in `tools/terminal_model_canary.py`), and `guidance=none`: the prompt lists published tool names and does not add `requiredSequence` or a stage order. Exploratory arms, beside that default, are Claude Opus 5.5, Claude Sonnet 5.5, GPT-6.1 Sol and GPT-6.1 Terra. This lane does not call any of them. Haiku 4.5 is no longer an arm.

Migration 142 is the reader-incompatible schema version named by the preregistration. It is not a numbered SQL file in honua-server. The oracle reads the controller's committed deployment record. `server_readback` stays false. `--execute-compose` renders `docker compose config` and inspects images that are already local. It does not pull and it does not publish corpus layers through the server admin API.

`aws-ecs` is unwired. The release session connects that target to the cloud cell later. Phrasing C, O11, O12 and the unseen faults stay in `held-out/` and are not opened unless `--qualification` is set.

## Commands

From the honua-release worktree, with PyYAML, jsonschema, cryptography and pytest installed:

```bash
python -m pytest certification/agent-ops -q
python certification/agent-ops/runner.py --self-test
python certification/agent-ops/runner.py --target local-docker --model stub --arm primary \
  --rows certification/agent-ops/rows/development.jsonl --out /tmp/gis-ops-stub
python certification/agent-ops/score.py --results /tmp/gis-ops-stub/trials.jsonl
python certification/agent-ops/runner.py --target local-docker --model stub --pilot \
  --rows certification/agent-ops/rows/development.jsonl --out /tmp/gis-ops-pilot
python certification/agent-ops/runner.py --target aws-ecs --model stub --out /tmp/gis-ops-aws
```

Do not pass `--model live` or dispatch the workflow's live job from this lane.

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
