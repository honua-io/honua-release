#!/usr/bin/env bash
# S5 — Geoprocessing via OGC API Processes. Submit two catalog processes (geometry.area,
# geometry.buffer), poll each job to `successful`, and assert the results document is returned.
# Requires the Redis-backed job runtime (entitled via Licensing__DevGrantEdition=Enterprise).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../harness/lib/common.sh
source "$HERE/../../harness/lib/common.sh"

if ! server_ready; then
  emit_scenario "S5-geoprocessing" blocked "server not ready at $E2E_BASE"
  exit 0
fi

# base64 WKB of a unit square polygon (SRID 4326), computed deterministically with stdlib.
WKB="$(python3 - <<'PY'
import struct,base64
pts=[(0,0),(1,0),(1,1),(0,1),(0,0)]
b=struct.pack("<BI",1,3)+struct.pack("<I",1)+struct.pack("<I",len(pts))
for x,y in pts: b+=struct.pack("<dd",x,y)
print(base64.b64encode(b).decode())
PY
)"

ST=""; RESULTS=""
run_process() { # process-id inputs-json  -> sets globals ST and RESULTS (NOT via subshell)
  local pid="$1" inputs="$2"
  ST=""; RESULTS=""
  api_json POST "/ogc/processes/processes/$pid/execution" "$(jq -nc --argjson i "$inputs" '{inputs:$i}')" \
    -H "Prefer: respond-async"
  local job; job="$(jget '.jobID')"
  [ -z "$job" ] && { ST="submit-failed:$HTTP_CODE"; return; }
  local i
  for i in $(seq 1 30); do
    api_get "/ogc/processes/jobs/$job"; ST="$(jget '.status')"
    { [ "$ST" = "successful" ] || [ "$ST" = "failed" ] || [ "$ST" = "dismissed" ]; } && break
    sleep 2
  done
  api_get "/ogc/processes/jobs/$job/results"; RESULTS="$HTTP_BODY"
}

declare -A want=( [geometry.area]='{"wkb":"'"$WKB"'","srid":4326}' [geometry.buffer]='{"wkb":"'"$WKB"'","srid":4326,"distance":10}' )
fails=""; evidence="{}"
for pid in geometry.area geometry.buffer; do
  run_process "$pid" "${want[$pid]}"; st="$ST"
  has_result="$(printf '%s' "$RESULTS" | jq -r 'if (.|length)>0 then "yes" else "no" end' 2>/dev/null || echo no)"
  evidence="$(jq -nc --argjson e "$evidence" --arg p "$pid" --arg s "$st" --arg r "$has_result" '$e + {($p):{status:$s,hasResult:$r}}')"
  { [ "$st" = "successful" ] && [ "$has_result" = "yes" ]; } || fails="$fails $pid($st)"
done

if [ -z "$fails" ]; then
  emit_scenario "S5-geoprocessing" pass "geometry.area + geometry.buffer ran to successful with results" "$evidence"
else
  emit_scenario "S5-geoprocessing" fail "process(es) did not complete with results:$fails" "$evidence"
fi
