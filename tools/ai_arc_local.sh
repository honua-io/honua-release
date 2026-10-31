#!/usr/bin/env bash
# Run AI arc trials for one arm and target from the controller machine (signed-in agent CLIs),
# one trial at a time, then write the target x arm aggregate. See certification/ai-arc/README.md.
#
#   tools/ai_arc_local.sh --arm A1 --target local-docker --lock-path platform-lock.json \
#     --quickstart quickstart.md --host ssh://arc@203.0.113.10 --out /path/to/new-run-dir [--trials N]
#
# --host is the ephemeral VM Docker host for this campaign (ssh://user@host). --host local runs on
# this machine's Docker for a development rehearsal only; its receipts record hostKind=local.
# Every trial is a first attempt: the output directory must not exist and nothing is rerun.
set -euo pipefail

PYTHON="${PYTHON:-python3}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
arm="" target="" lock="" quickstart="" host="" out="" trials=""
while [ $# -gt 0 ]; do
  case "$1" in
    --arm) arm="$2"; shift 2 ;;
    --target) target="$2"; shift 2 ;;
    --lock-path) lock="$2"; shift 2 ;;
    --quickstart) quickstart="$2"; shift 2 ;;
    --host) host="$2"; shift 2 ;;
    --out) out="$2"; shift 2 ;;
    --trials) trials="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$arm" ] && [ -n "$target" ] && [ -n "$lock" ] && [ -n "$out" ] || { sed -n 2,11p "$0"; exit 2; }
[ ! -e "$out" ] || { echo "$out exists; every previous run is preserved" >&2; exit 2; }
lock_digest="sha256:$(sha256sum "$lock" | cut -d' ' -f1)"
mkdir -p "$out"

docker_healthy() {
  [ "$host" = local ] || return 0
  docker info --format '{{.ServerVersion}}' >/dev/null
  ! docker ps --format '{{.Status}}' | grep -qi unhealthy
}

args=(inventory --target "$target" --arm "$arm")
[ -z "$trials" ] || args+=(--trials "$trials")
"$PYTHON" "$ROOT/tools/ai_arc_runner.py" "${args[@]}" > "$out/schedule.json"

"$PYTHON" -c 'import json,sys; [print(r["runId"], r["variant"]) for r in json.load(open(sys.argv[1]))]' \
    "$out/schedule.json" | while read -r run variant; do
  docker_healthy || { echo "Docker is unhealthy; stopping before $run" >&2; exit 1; }
  trial=(trial --target "$target" --arm "$arm" --trial-id "$run" --variant "$variant"
         --output "$out/trials/${run//:/-}" --lock-digest "$lock_digest" --lock-path "$lock")
  [ -z "$quickstart" ] || trial+=(--quickstart "$quickstart")
  [ -z "$host" ] || trial+=(--docker-host "$host")
  "$PYTHON" "$ROOT/tools/ai_arc_runner.py" "${trial[@]}"
  # The runner tears its own compose project down; confirm nothing of the arc is left behind.
  if [ "$host" = local ] && docker ps -aq --filter label=com.docker.compose.project | \
      xargs -r docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' | grep -q '^arc-'; then
    echo "an arc compose project survived teardown after $run; stopping" >&2; exit 1
  fi
done

read -r sha digest < <("$PYTHON" -c '
import json, sys
c = json.load(open(sys.argv[1]))["components"]["honua-server"]
d = [a["digest"] for a in c["artifacts"] if a.get("kind") == "image"]
print(c["source"]["revision"], d[0] if len(d) == 1 else "")' "$lock")
"$PYTHON" "$ROOT/tools/ai_arc_runner.py" aggregate --target "$target" --arm "$arm" \
  --schedule "$out/schedule.json" --trials-dir "$out/trials" --candidate-sha "$sha" --candidate-digest "$digest" \
  --lock-digest "$lock_digest" --output "$out/ai-arc-aggregate-$target-$arm.json" \
  --report "$out/ai-arc-report-$target-$arm.json"
