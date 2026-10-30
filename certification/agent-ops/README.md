# Operate-stage harness

Local checks for the operate scenarios. Phrasings A and B are in `prompts_ab.json`. Phrasing C is in `prompts_c.json` and the runner does not read that file unless `--qualification` is set and a row uses it.

The prior image is `ghcr.io/honua-io/honua-server:2026.1-rc`. The newer image is `honua-server:trunk`. Schema 142 is the reader-incompatible step named for these scenarios. The server repository has no SQL file numbered 142; the harness records the schema version and the reader-incompatible marker.

The oracle reads the controller's committed deployment record. `--execute-compose` renders the compose file and inspects images that are already on the machine. It does not pull, and it does not publish the corpus layers through the server admin API. `server_readback` stays false. `aws-ecs` prints `unwired` and does not start a cell.

A scripted run is not a model measurement: `priced` is false and `model` is null. Priced dry-run, pilot, and full run stay with the release session.

```bash
python -m pytest certification/agent-ops -q
python certification/agent-ops/runner.py --self-test
python certification/agent-ops/runner.py --target local-docker --model stub --arm A \
  --rows certification/agent-ops/rows/development.jsonl --out /tmp/gis-ops-stub
python certification/agent-ops/score.py --results /tmp/gis-ops-stub/trials.jsonl
python certification/agent-ops/runner.py --target local-docker --model stub --pilot \
  --rows certification/agent-ops/rows/development.jsonl --out /tmp/gis-ops-pilot
python certification/agent-ops/runner.py --target aws-ecs --model stub --out /tmp/gis-ops-aws
```

The first three development rows are O01-A-1, O02-A-1, and O03-A-1. `--pilot` runs those three. `--model live` does nothing unless `--priced-ok` is also set.

Dry-run shape for the release session, after this harness is on trunk:

```bash
"$GSTACK_ROOT/bin/gstack-eval-plan" dry-run \
  --input certification/agent-ops/rows/development.jsonl \
  --arms claude-sonnet-4-6,claude-haiku-4-5 \
  --retry-prompt certification/agent-ops/retry-prompt.txt \
  --out /tmp/gis-ops-dry-run
```
