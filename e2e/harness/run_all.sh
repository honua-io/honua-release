#!/usr/bin/env bash
# Slice-1 orchestrator — the single entrypoint (humans + the release train both use it).
#
#   boot candidate stack -> seed deterministic contracts -> run every driver -> assemble gate-report.json
#
# Honesty (AGENTS.md): if the candidate server can't boot (unpullable/placeholder image), the drivers
# self-report BLOCKED rather than fabricating a green; E2E_REQUIRE_REAL promotes BLOCKED/SKIPPED to a
# hard FAIL (nightly / real release cut). Static checks in `check` always run and can fail with no image.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$HERE/.." && pwd)"

export E2E_OUT="${E2E_OUT:-$E2E_DIR/out}"
export E2E_BASE="${E2E_BASE:-http://localhost:${E2E_SERVER_PORT:-8080}}"
export E2E_API_KEY="${E2E_API_KEY:-honua-console-dev-key}"
export E2E_COMPOSE_FILE="$HERE/compose.candidate.yml"
# Resolve the pinned server image for report evidence (READ-ONLY from the manifest; never edited).
resolve_pin() {
  [ -n "${HONUA_SERVER_IMAGE:-}" ] && { echo "$HONUA_SERVER_IMAGE"; return; }
  awk '$0 ~ /^  honua-server:/{b=1;next} b&&/^  [a-zA-Z]/{b=0} b&&$1=="image:"{gsub(/"/,"",$2);print $2;exit}' \
    "$E2E_DIR/../platform-manifest.yaml" 2>/dev/null || true
}
export E2E_SERVER_IMAGE="$(resolve_pin)"; [ -z "$E2E_SERVER_IMAGE" ] && E2E_SERVER_IMAGE="ghcr.io/honua-io/honua-server:nightly-aot"
export HONUA_SERVER_IMAGE="$E2E_SERVER_IMAGE"
rm -rf "$E2E_OUT" 2>/dev/null || true; mkdir -p "$E2E_OUT"
# shellcheck source=lib/report.sh
source "$HERE/lib/report.sh"

DRIVERS=(mcp protocols studio gp formats console demos)

cmd_check() {
  echo "== static checks =="
  docker compose -f "$HERE/compose.candidate.yml" config >/dev/null && echo "compose config: OK"
  bash -n "$HERE/boot.sh" "$HERE/run_all.sh" "$HERE/seed/seed.sh"
  for d in "${DRIVERS[@]}"; do bash -n "$E2E_DIR/drivers/$d/run.sh"; done
  python3 -m py_compile "$E2E_DIR/drivers/formats/formats.py"
  jq -e . "$E2E_DIR/drivers/mcp/expected-tools.json" >/dev/null && echo "mcp snapshot: valid"
  echo "static checks: OK"
}

cmd_run() {
  export E2E_SERVER_BOOTED=false
  local booted=1
  if bash "$HERE/boot.sh" up; then
    booted=0; export E2E_SERVER_BOOTED=true
    echo "== seeding =="
    bash "$HERE/seed/seed.sh" || echo "::warning:: seed failed — data-dependent drivers will block/fail"
  else
    echo "::warning:: candidate server did not boot — drivers will report BLOCKED"
  fi

  for d in "${DRIVERS[@]}"; do
    echo "== driver: $d =="
    bash "$E2E_DIR/drivers/$d/run.sh" || echo "::warning:: driver $d exited non-zero"
  done

  [ "$booted" = 0 ] && bash "$HERE/boot.sh" down || true

  echo "== assembling gate-report.json =="
  if assemble_report "$E2E_OUT"; then
    echo "== formatCoverage[] =="; jq -r '.formatCoverage[] | "  \(.format): read=\(.read) write=\(.write) roundtrip=\(.roundtrip)"' "$E2E_OUT/gate-report.json" || true
    cp "$E2E_OUT/gate-report.json" "$E2E_DIR/gate-report.json"
    exit 0
  else
    cp "$E2E_OUT/gate-report.json" "$E2E_DIR/gate-report.json"
    exit 1
  fi
}

case "${1:-run}" in
  check) cmd_check ;;
  run)   cmd_run ;;
  *) echo "usage: run_all.sh check|run" >&2; exit 2 ;;
esac
