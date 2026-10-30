# Operate-stage harness (OPS v2)

Campaign E measures the shipped operator configuration on a scripted stand-in and, later, on a priced model. It is not GA evidence. Campaign Q bars are reported beside the intervals and are not applied here.

The shipped configuration is Bedrock `us.anthropic.claude-sonnet-4-6`, the canary client's own request temperature (0 in `tools/terminal_model_canary.py`), and `guidance=none`: the prompt lists published tool names and does not add `requiredSequence` or a stage order. The exploratory arm is `claude-haiku-4-5`. This lane does not call either model.

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
  --arms claude-sonnet-4-6,claude-haiku-4-5 \
  --retry-prompt certification/agent-ops/retry-prompt.txt \
  --out /tmp/gis-ops-dry-run
```

A stub summary has `"priced": false` and `"model": null`. It is the harness check, not a Sonnet measurement.
