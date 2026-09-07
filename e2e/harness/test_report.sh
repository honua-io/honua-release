#!/usr/bin/env bash
# Fixture tests for the Slice-1 verdict logic (lib/report.sh). No docker, no images, no network:
# they feed synthetic scenario fragments to assemble_report and assert the overall status.
#
# The gate this protects (honua-release#303): on PR #299 all 13 slice1 scenarios were BLOCKED
# because the pinned server exited at DI validation before binding a port, and the check still went
# green because the pull_request trigger runs without require_real. An untested verdict function is
# a gate that can stop gating silently, so the boot rule ships with the fixtures that pin it.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/report.sh
source "$HERE/lib/report.sh"
# report.sh sets -e for the harness; these fixtures deliberately drive assemble_report to a failing
# verdict, so run the assertions with errexit off and capture the exit code explicitly.
set +e

FAILURES=0
TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT

# scenario_row ID STATUS WHY -> one gate fragment line
scenario_row() { jq -nc --arg s "$1" --arg st "$2" --arg why "$3" '{scenario:$s,status:$st,why:$why,evidence:null}'; }

# run_case NAME  (reads fixture setup from stdin into a fresh out-dir) -> echoes overall status,
# sets RC to assemble_report's exit code and REPORT to the produced gate-report.json path.
new_case() {
  CASE_DIR="$TMPROOT/$1"; mkdir -p "$CASE_DIR"
  REPORT="$CASE_DIR/gate-report.json"
}

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

# The 13 slice1 scenarios, all BLOCKED on the same boot reason - the exact PR #299 shape.
all_blocked_on_boot() {
  local i
  for i in S1-mcp-handshake S2-mcp-tool-catalog S3-studio-authoring S4-console-studio S5-geoprocessing \
           S-protocol-parity S8-formats S9-demos-a S9-demos-b S9-demos-c S9-demos-d S9-demos-e S6-sync; do
    scenario_row "$i" blocked "server not ready at http://localhost:8080"
  done
}

echo "== case 1: stack never booted, no require_real => FAIL (honua-release#303) =="
new_case boot-failure
all_blocked_on_boot > "$CASE_DIR/scenarios.jsonl"
cat > "$CASE_DIR/boot.json" <<'JSON'
{"booted":false,"reason":"never-ready","detail":"server never became ready at http://localhost:8080/healthz/ready within 180s",
 "image":"ghcr.io/honua-io/honua-server@sha256:deadbeef","healthUrl":"http://localhost:8080/healthz/ready",
 "exitCode":134,"containerState":"exited",
 "errorLines":["server-1 | Unhandled exception. System.AggregateException: Some services are not able to be constructed"]}
JSON
rc=0; ( unset E2E_REQUIRE_REAL; E2E_SERVER_BOOTED=false assemble_report "$CASE_DIR" >/dev/null 2>&1 ) || rc=$?
assert_eq "assemble_report rc"     1      "$rc"
assert_eq "status"                 fail   "$(jq -r .status "$REPORT")"
assert_eq "boot.failed"            true   "$(jq -r .boot.failed "$REPORT")"
assert_eq "require_real"           false  "$(jq -r .require_real "$REPORT")"
assert_eq "boot row present"       fail   "$(jq -r '.scenarios[]|select(.scenario=="S0-stack-boot")|.status' "$REPORT")"
assert_contains "boot row why" "134"      "$(jq -r '.scenarios[]|select(.scenario=="S0-stack-boot")|.why' "$REPORT")"
assert_contains "boot row why" "Unhandled exception" \
                                          "$(jq -r '.scenarios[]|select(.scenario=="S0-stack-boot")|.why' "$REPORT")"
assert_eq "the 13 driver rows survive" 13 "$(jq -r '[.scenarios[]|select(.scenario!="S0-stack-boot")]|length' "$REPORT")"

echo "== case 2: same, without boot.json (E2E_SERVER_BOOTED fallback) => FAIL =="
new_case boot-failure-nofile
all_blocked_on_boot > "$CASE_DIR/scenarios.jsonl"
( unset E2E_REQUIRE_REAL; E2E_SERVER_BOOTED=false assemble_report "$CASE_DIR" >/dev/null 2>&1 )
assert_eq "status"      fail                 "$(jq -r .status "$REPORT")"
assert_eq "boot.reason" server-never-ready   "$(jq -r .boot.reason "$REPORT")"

echo "== case 3: boot.sh says booted, but every scenario blocked on the boot => FAIL =="
new_case boot-failure-derived
all_blocked_on_boot > "$CASE_DIR/scenarios.jsonl"
echo '{"booted":true,"reason":null,"detail":null,"image":"x","healthUrl":null,"exitCode":null,"containerState":"running","errorLines":[]}' > "$CASE_DIR/boot.json"
( unset E2E_REQUIRE_REAL; E2E_SERVER_BOOTED=true assemble_report "$CASE_DIR" >/dev/null 2>&1 )
assert_eq "status"        fail                          "$(jq -r .status "$REPORT")"
assert_eq "boot.reason"   all-scenarios-blocked-on-boot "$(jq -r .boot.reason "$REPORT")"

echo "== case 4: one scenario-level BLOCKED on a live stack => PASS without require_real =="
new_case one-blocked
{ scenario_row S1-mcp-handshake pass "initialize ok"
  scenario_row S4-console-studio blocked "E2E_CONSOLE_DIR not set / not a honua-console checkout"
  scenario_row S5-geoprocessing pass "geometry.area + geometry.buffer ran"
} > "$CASE_DIR/scenarios.jsonl"
echo '{"booted":true,"reason":null,"detail":null,"image":"x","healthUrl":null,"exitCode":null,"containerState":"running","errorLines":[]}' > "$CASE_DIR/boot.json"
( unset E2E_REQUIRE_REAL; E2E_SERVER_BOOTED=true assemble_report "$CASE_DIR" >/dev/null 2>&1 )
assert_eq "status"      pass  "$(jq -r .status "$REPORT")"
assert_eq "boot.failed" false "$(jq -r .boot.failed "$REPORT")"
assert_eq "no boot row" 0     "$(jq -r '[.scenarios[]|select(.scenario=="S0-stack-boot")]|length' "$REPORT")"

echo "== case 5: the same single BLOCKED => FAIL with require_real (require_real keeps its meaning) =="
( E2E_REQUIRE_REAL=1 E2E_SERVER_BOOTED=true assemble_report "$CASE_DIR" >/dev/null 2>&1 )
assert_eq "status"       fail "$(jq -r .status "$REPORT")"
assert_eq "require_real" true "$(jq -r .require_real "$REPORT")"
assert_eq "boot.failed"  false "$(jq -r .boot.failed "$REPORT")"

echo "== case 6: live stack, everything green => PASS, and assemble_report returns 0 =="
new_case all-pass
{ scenario_row S1-mcp-handshake pass "initialize ok"
  scenario_row S5-geoprocessing pass "geometry.area + geometry.buffer ran"
} > "$CASE_DIR/scenarios.jsonl"
echo '{"booted":true,"reason":null,"detail":null,"image":"x","healthUrl":null,"exitCode":null,"containerState":"running","errorLines":[]}' > "$CASE_DIR/boot.json"
rc=0; ( unset E2E_REQUIRE_REAL; E2E_SERVER_BOOTED=true assemble_report "$CASE_DIR" >/dev/null 2>&1 ) || rc=$?
assert_eq "status"    pass "$(jq -r .status "$REPORT")"
assert_eq "exit code" 0    "$rc"

echo "== case 7: a scenario FAIL on a booted stack still fails, and returns non-zero =="
new_case scenario-fail
{ scenario_row S1-mcp-handshake pass "initialize ok"
  scenario_row S8-formats fail "CORE format regression"
} > "$CASE_DIR/scenarios.jsonl"
echo '{"booted":true,"reason":null,"detail":null,"image":"x","healthUrl":null,"exitCode":null,"containerState":"running","errorLines":[]}' > "$CASE_DIR/boot.json"
rc=0; ( unset E2E_REQUIRE_REAL; E2E_SERVER_BOOTED=true assemble_report "$CASE_DIR" >/dev/null 2>&1 ) || rc=$?
assert_eq "status"    fail "$(jq -r .status "$REPORT")"
assert_eq "exit code" 1    "$rc"

echo "== case 8: the boot failure is annotated in the job summary with exit code + error lines =="
new_case summary
all_blocked_on_boot > "$CASE_DIR/scenarios.jsonl"
cp "$TMPROOT/boot-failure/boot.json" "$CASE_DIR/boot.json" 2>/dev/null || true
SUMMARY="$CASE_DIR/step-summary.md"; : > "$SUMMARY"
( unset E2E_REQUIRE_REAL; GITHUB_STEP_SUMMARY="$SUMMARY" E2E_SERVER_BOOTED=false assemble_report "$CASE_DIR" >/dev/null 2>&1 )
assert_contains "job summary" "did not boot"          "$(cat "$SUMMARY")"
assert_contains "job summary" "134"                   "$(cat "$SUMMARY")"
assert_contains "job summary" "Unhandled exception"   "$(cat "$SUMMARY")"

if [ "$FAILURES" -eq 0 ]; then
  echo "report.sh fixtures: OK"
else
  echo "report.sh fixtures: $FAILURES assertion(s) failed" >&2
  exit 1
fi
