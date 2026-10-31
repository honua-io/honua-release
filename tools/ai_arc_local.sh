#!/usr/bin/env bash
# Run one target x arm AI arc campaign from the controller machine (signed-in agent CLIs), then
# write its aggregate. Local Docker runs go to the ephemeral VM hosts, three slots per host.
#
#   tools/ai_arc_local.sh --arm A1 --target local-docker --lock-path platform-lock.json \
#     --quickstart quickstart.md --quickstart-revision <honua-server sha> \
#     --hosts honua-eval-1,honua-eval-3 --out /path/to/new-campaign-dir [--trials N]
#
# Every run is a first attempt; the output directory must not exist. After an undetermined run the
# campaign waits for <out>/adjudications/<run>.json (see certification/ai-arc/README.md).
set -euo pipefail
PYTHON="${PYTHON:-python3}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
arm="" target="" lock="" out="" held=""
pass=()
while [ $# -gt 0 ]; do
  case "$1" in
    --arm) arm="$2"; pass+=("$1" "$2"); shift 2 ;;
    --target) target="$2"; pass+=("$1" "$2"); shift 2 ;;
    --lock-path) lock="$2"; pass+=("$1" "$2"); shift 2 ;;
    --out) out="$2"; pass+=("$1" "$2"); shift 2 ;;
    --quickstart|--quickstart-revision|--hosts|--trials|--per-host) pass+=("$1" "$2"); shift 2 ;;
    --held-out) pass+=("$1"); held=--held-out; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$arm" ] && [ -n "$target" ] && [ -n "$lock" ] && [ -n "$out" ] || { sed -n 2,10p "$0"; exit 2; }
digest="sha256:$(sha256sum "$lock" | cut -d' ' -f1)"
"$PYTHON" "$ROOT/tools/ai_arc_campaign.py" "${pass[@]}" --lock-digest "$digest"
"$PYTHON" "$ROOT/tools/ai_arc_runner.py" aggregate --target "$target" --arm "$arm" \
  --schedule "$out/schedule.json" --trials-dir "$out/runs" --voids "$out/voids.json" \
  --evidence-root "$out-evidence" ${held:-} \
  --lock-path "$lock" --lock-digest "$digest" \
  --output "$out/ai-arc-aggregate-$target-$arm.json" --report "$out/ai-arc-report-$target-$arm.json"
