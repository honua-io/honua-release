#!/usr/bin/env bash
# S5 — Geoprocessing via OGC API Processes. Submit two catalog processes (geometry.area,
# geometry.buffer), poll each job to `successful`, and validate the numerical and geometry outputs.
# Requires the Redis-backed job runtime (all entitlements active via Licensing__Mode=Disabled).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../harness/lib/common.sh
source "$HERE/../../harness/lib/common.sh"

# The harness consumes receipt rows even when a driver exits nonzero. Never let
# an aborted driver silently remove this required scenario from the report.
GP_EMITTED=false
emit_gp() {
  emit_scenario "S5-geoprocessing" "$@"
  GP_EMITTED=true
}
trap 'if [ "$GP_EMITTED" != true ]; then
  emit_scenario "S5-geoprocessing" fail "GP driver aborted before producing its receipt"
fi' EXIT
POLL_ATTEMPTS="${E2E_GP_POLL_ATTEMPTS:-30}"
POLL_INTERVAL="${E2E_GP_POLL_INTERVAL:-2}"
if ! [[ "$POLL_ATTEMPTS" =~ ^[1-9][0-9]{0,2}$ ]] || [ "$POLL_ATTEMPTS" -gt 300 ]; then exit 1; fi
[[ "$POLL_INTERVAL" =~ ^[0-9]+([.][0-9]+)?$ ]]

if ! server_ready; then
  emit_gp blocked "server not ready at $E2E_BASE"
  exit 0
fi

# base64 WKB of a unit square polygon (SRID 3857), computed deterministically with stdlib.
WKB="$(python3 - <<'PY'
import struct,base64
pts=[(0,0),(1,0),(1,1),(0,1),(0,0)]
b=struct.pack("<BI",1,3)+struct.pack("<I",1)+struct.pack("<I",len(pts))
for x,y in pts: b+=struct.pack("<dd",x,y)
print(base64.b64encode(b).decode())
PY
)"

# Redis-off topology (E2E_REDIS=off): the durable job substrate is not composed, so the server must
# refuse the async submission up front with the typed capability-unavailable receipt
# (honua-release#202) rather than queue a job that never runs. Anything else is a failure.
if [ "${E2E_REDIS:-on}" = "off" ]; then
  api_json POST "/ogc/processes/processes/geometry.area/execution" \
    "$(jq -nc --arg w "$WKB" '{inputs:{wkb:$w,srid:3857}}')" -H "Prefer: respond-async"
  evidence="$(jq -nc --arg c "$HTTP_CODE" --arg t "$(jget '.type')" --arg d "$(jget '.missingDependency')" \
    --arg k "$(jget '.capability')" '{httpStatus:$c,type:$t,missingDependency:$d,capability:$k}')"
  if [ "$HTTP_CODE" = "503" ] && [ "$(jget '.type')" = "https://honua.io/problems/capability-unavailable" ] \
     && [ "$(jget '.missingDependency')" = "redis" ]; then
    emit_gp pass "redis-off: async job submission refused with the typed capability-unavailable receipt (missingDependency=redis)" "$evidence"
  else
    emit_gp fail "redis-off: expected 503 capability-unavailable (missingDependency=redis), got HTTP $HTTP_CODE" "$evidence"
  fi
  exit 0
fi

ST=""; RESULTS=""
run_process() { # process-id inputs-json  -> sets globals ST and RESULTS (NOT via subshell)
  local pid="$1" inputs="$2"
  ST=""; RESULTS=""
  api_json POST "/ogc/processes/processes/$pid/execution" "$(jq -nc --argjson i "$inputs" '{inputs:$i}')" \
    -H "Prefer: respond-async"
  local job; job="$(jget '.jobID')"
  case "$HTTP_CODE" in 201|202) ;; *) ST="submit-failed:$HTTP_CODE"; return ;; esac
  if ! printf '%s' "$HTTP_BODY" | jq -e '.jobID | type == "string" and length > 0' >/dev/null; then
    ST="missing-job-id"; return
  fi
  local i
  for i in $(seq 1 "$POLL_ATTEMPTS"); do
    api_get "/ogc/processes/jobs/$job"
    [ "$HTTP_CODE" = "200" ] || { ST="poll-failed:$HTTP_CODE"; return; }
    [ "$(jget '.jobID // .jobId // empty')" = "$job" ] || { ST="wrong-job-id"; return; }
    ST="$(jget '.status')"
    { [ "$ST" = "successful" ] || [ "$ST" = "failed" ] || [ "$ST" = "dismissed" ]; } && break
    case "$ST" in accepted|running) ;; *) ST="unknown-status:$ST"; return ;; esac
    if [ "$i" -eq "$POLL_ATTEMPTS" ]; then ST="poll-timeout"; return; fi
    sleep "$POLL_INTERVAL"
  done
  [ "$ST" = "successful" ] || return 0
  api_get "/ogc/processes/jobs/$job/results"
  [ "$HTTP_CODE" = "200" ] || { ST="results-failed:$HTTP_CODE"; return; }
  RESULTS="$HTTP_BODY"
}

declare -A want=( [geometry.area]='{"wkb":"'"$WKB"'","srid":3857}' [geometry.buffer]='{"wkb":"AQEAAAAAAAAAAAAAAAAAAAAAAAAA","srid":3857,"distance":10,"geodesic":false}' )
fails=""; evidence="{}"
for pid in geometry.area geometry.buffer; do
  run_process "$pid" "${want[$pid]}"; st="$ST"
  has_result=no
  if [ "$st" = "successful" ] && printf '%s' "$RESULTS" | python3 "$HERE/validate_result.py" "$pid"; then
    has_result=yes
  fi
  evidence="$(jq -nc --argjson e "$evidence" --arg p "$pid" --arg s "$st" --arg r "$has_result" '$e + {($p):{status:$s,hasResult:$r}}')"
  { [ "$st" = "successful" ] && [ "$has_result" = "yes" ]; } || fails="$fails $pid($st)"
done

if [ -z "$fails" ]; then
  emit_gp pass "geometry.area + geometry.buffer completed with verified metric results" "$evidence"
else
  emit_gp fail "process(es) did not complete with results:$fails" "$evidence"
fi
