#!/usr/bin/env bash
# Diagnostic (not a gate): print ONE JSON summary of a candidate's control-plane topology and its
# admin full MCP catalog against the committed roster.
#
#   E2E_BASE=http://localhost:8080 E2E_API_KEY=... bash e2e/drivers/mcp/probe_topology.sh
#
# Output fields:
#   topology / signal / reasonCode  -- harness/lib/common.sh detect_topology (manifest
#                                      `operations.proposals`, else GET /api/v1/admin/proposals,
#                                      else manifest `jobs.runner`)
#   toolCount                       -- names returned by tools/list {view:"full"}, cursor-followed
#   missingFromRoster               -- canonical (Redis-on) roster names the server did not advertise
#   missingDurableControlPlane      -- how many of those are fullCatalog.requiresDurableControlPlane
#   extraOverRoster                 -- advertised names the committed roster does not list
#   operations                      -- HTTP status and item count of GET /api/v1/operations
# Acceptance for the Redis-off topology (plan unit J1): against the same pinned digest, booted with
# and without docker-compose.no-redis.yml, the two catalogs differ by exactly the 20 names. It never
# writes scenario rows and always exits 0 once it has printed the summary.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_OUT="${E2E_OUT:-$(mktemp -d)}"
# shellcheck source=../../harness/lib/common.sh
source "$HERE/../../harness/lib/common.sh"
EXPECTED="$HERE/expected-tools.json"

if ! server_ready; then
  jq -nc --arg b "$E2E_BASE" '{error:"server not ready", base:$b}'
  exit 1
fi

detect_topology

NAMES="[]"; CURSOR=""; PAGES=0
while [ "$PAGES" -lt 50 ]; do
  if [ -z "$CURSOR" ]; then params='{"view":"full"}'; else params="$(jq -nc --arg c "$CURSOR" '{view:"full",cursor:$c}')"; fi
  api_post "/mcp" "$(jq -nc --argjson p "$params" '{jsonrpc:"2.0",id:1,method:"tools/list",params:$p}')"
  PAGES=$((PAGES + 1))
  NAMES="$(jq -nc --argjson acc "$NAMES" --argjson page "$(jget '[.result.tools[]?.name]')" '$acc + ($page // [])' 2>/dev/null || echo "$NAMES")"
  CURSOR="$(jget '.result.nextCursor // empty')"
  [ -z "$CURSOR" ] && break
done

api_get "/api/v1/operations"
OPS_CODE="$HTTP_CODE"
OPS_COUNT="$(jget '(.data.items // .data // .items // []) | if type == "array" then length else null end')"

jq -nc --argjson live "$NAMES" --slurpfile want "$EXPECTED" \
  --arg t "$TOPOLOGY" --arg s "$TOPOLOGY_SIGNAL" --arg r "$TOPOLOGY_REASON" --argjson pages "$PAGES" \
  --arg oc "$OPS_CODE" --arg on "${OPS_COUNT:-}" '
  ($want[0].fullCatalog.tools) as $roster
  | ($want[0].fullCatalog.requiresDurableControlPlane.tools // []) as $durable
  | ($roster - $live) as $missing
  | {topology:$t, signal:$s, reasonCode:(if $r == "" then null else $r end),
     toolCount:($live | unique | length), pages:$pages, rosterCount:($roster | length),
     missingFromRoster:($missing | sort),
     missingDurableControlPlane:([$missing[] | select(. as $n | $durable | index($n))] | length),
     extraOverRoster:(($live - $roster) | sort),
     operations:{httpStatus:$oc, count:(if $on == "" or $on == "null" then null else ($on | tonumber) end)}}'
