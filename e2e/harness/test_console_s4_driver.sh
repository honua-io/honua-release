#!/usr/bin/env bash
# Fixture for the S4 driver failure paths that must still produce a scenario row.
# No images, no Docker daemon: docker/dotnet/npx are stubs. Two cases:
#   1. An inherited S4_ACCESS_TOKEN_LIFESPAN below 7200 makes render.sh refuse. The driver
#      must emit S4-console-studio fail and exit 0. A non-zero exit with no row lets
#      assemble_report stay green (run_all.sh continues; a missing scenario is not a fail).
#   2. A listener already bound to the S4 server port must be released by the project
#      `docker compose down` BEFORE the port check. If the check wins, the row blames the
#      port and the stale project is never removed.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRIVER="$HERE/../drivers/console/run.sh"
# shellcheck source=lib/report.sh
source "$HERE/lib/report.sh"
set +e

FAILURES=0
ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT

assert_eq() { # what expected actual
  if [ "$2" = "$3" ]; then
    printf '  ok   %s = %s\n' "$1" "$3"
  else
    printf '  FAIL %s: expected %s, got %s\n' "$1" "$2" "$3"; FAILURES=$((FAILURES+1))
  fi
}
assert_contains() { # what needle haystack
  case "$3" in
    *"$2"*) printf '  ok   %s contains %s\n' "$1" "$2" ;;
    *) printf '  FAIL %s: expected to contain %s, got: %s\n' "$1" "$2" "$3"; FAILURES=$((FAILURES+1)) ;;
  esac
}

free_port() {
  python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'
}

prepare_case() { # name -> sets CASE, OUT, RUNNER, CONSOLE, BIN, DOCKER_LOG
  CASE="$ROOT/$1"
  OUT="$CASE/out"
  RUNNER="$CASE/runner"
  CONSOLE="$CASE/console"
  BIN="$CASE/bin"
  DOCKER_LOG="$CASE/docker.log"
  mkdir -p "$OUT" "$RUNNER" "$BIN" "$CONSOLE/e2e/playwright/live"
  printf '%s\n' '{}' > "$CONSOLE/e2e/playwright/package.json"
  printf '%s\n' 'export {}' > "$CONSOLE/e2e/playwright/live/operator-session.setup.ts"
  : > "$DOCKER_LOG"
  cat > "$BIN/docker" <<'EOF'
#!/bin/bash
printf '%s\n' "$*" >> "$DOCKER_LOG"
case " $* " in
  *" down "*)
    if [ -n "${HOLDER_PIDFILE:-}" ] && [ -f "$HOLDER_PIDFILE" ]; then
      kill "$(cat "$HOLDER_PIDFILE")" 2>/dev/null || true
      for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
        if /usr/bin/python3 -c 'import socket,sys;s=socket.socket();s.bind(("127.0.0.1",int(sys.argv[1])));s.close()' "$HELD_PORT"; then
          exit 0
        fi
        sleep 0.05
      done
      echo "port did not free" >> "$DOCKER_LOG"
      exit 1
    fi
    exit 0
    ;;
  *" logs "*) exit 0 ;;
  *) echo "unexpected: $*" >> "$DOCKER_LOG"; exit 1 ;;
esac
EOF
  printf '%s\n' '#!/bin/sh' 'exit 0' > "$BIN/dotnet"
  cp "$BIN/dotnet" "$BIN/npx"
  chmod 0755 "$BIN/docker" "$BIN/dotnet" "$BIN/npx"
}

run_driver() { # extra env assignments are already exported by the caller
  env -u HONUA_CONSOLE_E2E_OPERATOR_PASSWORD \
      -u E2E_REQUIRE_REAL \
      PATH="$BIN:$PATH" \
      E2E_OUT="$OUT" \
      E2E_RUN_CONSOLE=1 \
      E2E_CONSOLE_DIR="$CONSOLE" \
      E2E_CONSOLE_SHA=fixture \
      HONUA_SERVER_IMAGE=example.invalid/honua-server:fixture \
      HONUA_CONSOLE_E2E_IDP_HOST="host.docker.internal:$IDP_PORT" \
      E2E_CONSOLE_SERVER_PORT="$SERVER_PORT" \
      HONUA_CONSOLE_E2E_LIVE_PORT="$LIVE_PORT" \
      S4_ACCESS_TOKEN_LIFESPAN=300 \
      RUNNER_TEMP="$RUNNER" \
      DOCKER_LOG="$DOCKER_LOG" \
      HOLDER_PIDFILE="${HOLDER_PIDFILE:-}" \
      HELD_PORT="${HELD_PORT:-}" \
      bash "$DRIVER" >"$CASE/driver.out" 2>"$CASE/driver.err"
}

# --- case 1: renderer refusal is a failing scenario, and the realm dir is removed ---------------
echo "== case 1: short access-token lifespan => fail row, exit 0, realm dir removed =="
prepare_case render-refused
IDP_PORT="$(free_port)"
SERVER_PORT="$(free_port)"
LIVE_PORT="$(free_port)"
unset HOLDER_PIDFILE HELD_PORT
run_driver
rc=$?
assert_eq "driver exit" 0 "$rc"
assert_eq "one scenario" 1 "$(jq -s 'length' "$OUT/scenarios.jsonl" 2>/dev/null || echo 0)"
assert_eq "status" fail "$(jq -r .status "$OUT/scenarios.jsonl" 2>/dev/null)"
assert_contains "why" "at least 7200" "$(jq -r .why "$OUT/scenarios.jsonl" 2>/dev/null)"
assert_eq "realm dir removed" 0 "$(find "$RUNNER" -mindepth 1 -print | wc -l | tr -d ' ')"
# The row, not the missing row, is what fails the gate once the other scenarios passed.
boot='{"booted":true,"reason":null,"detail":null,"image":"x","healthUrl":null,"exitCode":null,"containerState":"running","errorLines":[]}'
printf '%s\n' "$boot" > "$OUT/boot.json"
jq -nc '{scenario:"S1-mcp-handshake",status:"pass",why:"initialize ok",evidence:null}' >> "$OUT/scenarios.jsonl"
report_rc=0
( unset E2E_REQUIRE_REAL; E2E_SERVER_BOOTED=true assemble_report "$OUT" >/dev/null 2>&1 ) || report_rc=$?
assert_eq "assemble_report rc" 1 "$report_rc"
assert_eq "gate status" fail "$(jq -r .status "$OUT/gate-report.json")"

# --- case 2: stale listener is cleared by compose down before the port check --------------------
echo "== case 2: port held by a leftover stack is released by compose down first =="
prepare_case stale-port
IDP_PORT="$(free_port)"
SERVER_PORT="$(free_port)"
LIVE_PORT="$(free_port)"
HOLDER_PIDFILE="$CASE/holder.pid"
HELD_PORT="$SERVER_PORT"
python3 -c 'import socket,sys,time
s=socket.socket(); s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(1)
open(sys.argv[2],"w").write(str(__import__("os").getpid()))
time.sleep(60)
' "$SERVER_PORT" "$HOLDER_PIDFILE" &
holder_bg=$!
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  [ -s "$HOLDER_PIDFILE" ] && break
  sleep 0.05
done
run_driver
rc=$?
kill "$holder_bg" 2>/dev/null || true
wait "$holder_bg" 2>/dev/null || true
assert_eq "driver exit" 0 "$rc"
assert_eq "status" fail "$(jq -r .status "$OUT/scenarios.jsonl" 2>/dev/null)"
assert_contains "why is the renderer, not the port" "at least 7200" "$(jq -r .why "$OUT/scenarios.jsonl" 2>/dev/null)"
case "$(jq -r .why "$OUT/scenarios.jsonl" 2>/dev/null)" in
  *"already in use"*) printf '  FAIL port check ran before compose down: %s\n' "$(jq -r .why "$OUT/scenarios.jsonl")"; FAILURES=$((FAILURES+1)) ;;
  *) printf '  ok   port check did not blame %s\n' "$SERVER_PORT" ;;
esac
assert_contains "docker saw down" "down" "$(cat "$DOCKER_LOG")"
# down is the first docker invocation. A port check that exits first never calls it before failing,
# and a later teardown down would only happen after the fail row was already the port error.
first="$(head -n 1 "$DOCKER_LOG")"
case "$first" in
  *" down "*) printf '  ok   first docker command is down\n' ;;
  *) printf '  FAIL first docker command: %s\n' "$first"; FAILURES=$((FAILURES+1)) ;;
esac

if [ "$FAILURES" -eq 0 ]; then
  echo "console S4 driver fixtures: OK"
else
  echo "console S4 driver fixtures: $FAILURES assertion(s) failed" >&2
  exit 1
fi
