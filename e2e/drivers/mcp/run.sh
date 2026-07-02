#!/usr/bin/env bash
# S1/S2 — MCP handshake + tool catalog + Studio-critical tools/call.
#   S1: initialize -> assert JSON-RPC result carries protocolVersion + serverInfo.
#   S2: tools/list  -> assert the advertised catalog == the committed manifest snapshot
#       (expected-tools.json; stands in for the #2338 emitter until it lands on trunk), and that
#       each Studio-critical tool responds to tools/call with a non-error envelope.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../harness/lib/common.sh
source "$HERE/../../harness/lib/common.sh"
EXPECTED="$HERE/expected-tools.json"

if ! server_ready; then
  emit_scenario "S1-mcp-handshake" blocked "server not ready at $E2E_BASE"
  emit_scenario "S2-mcp-tool-catalog" blocked "server not ready at $E2E_BASE"
  exit 0
fi

rpc() { # method params-json  -> HTTP_BODY holds the JSON-RPC response
  local params="$2"; [ -z "$params" ] && params='{}'
  api_post "/mcp" "$(jq -nc --arg m "$1" --argjson p "$params" \
    '{jsonrpc:"2.0",id:1,method:$m,params:$p}')"
}

# ---- S1: initialize ------------------------------------------------------------------------------
rpc initialize '{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"honua-e2e","version":"1"}}'
PROTO="$(jget '.result.protocolVersion')"; SRV="$(jget '.result.serverInfo.name')"
if [ -n "$PROTO" ] && [ -n "$SRV" ]; then
  emit_scenario "S1-mcp-handshake" pass "initialize ok: $SRV @ $PROTO" \
    "$(jq -nc --arg p "$PROTO" --arg s "$SRV" '{protocolVersion:$p,serverInfo:$s}')"
else
  emit_scenario "S1-mcp-handshake" fail "initialize did not return protocolVersion/serverInfo (HTTP $HTTP_CODE): $(printf '%s' "$HTTP_BODY" | head -c 200)"
fi

# ---- S2: tools/list vs manifest snapshot + critical tools/call -----------------------------------
rpc tools/list '{}'
LIVE="$(printf '%s' "$HTTP_BODY" | jq -c '[.result.tools[].name] | sort' 2>/dev/null || echo '[]')"
WANT="$(jq -c '.tools | sort' "$EXPECTED")"
MISSING="$(jq -nc --argjson live "$LIVE" --argjson want "$WANT" '$want - $live')"
EXTRA="$(jq -nc --argjson live "$LIVE" --argjson want "$WANT" '$live - $want')"

# tools/call each Studio-critical tool; also call two read-only tools that must return non-error.
call_results="[]"
non_error_ok=true
for tool in $(jq -r '.criticalTools[]' "$EXPECTED") honua_list_capabilities honua_list_layers; do
  rpc tools/call "$(jq -nc --arg n "$tool" '{name:$n,arguments:{}}')"
  transport_err="$(jget '.error.message // empty')"
  is_err="$(jget '.result.isError // empty')"
  status="ok"; [ -n "$transport_err" ] && status="transport-error"; [ "$is_err" = "true" ] && status="tool-error"
  call_results="$(jq -nc --argjson acc "$call_results" --arg t "$tool" --arg s "$status" '$acc + [{tool:$t,result:$s}]')"
  # The two read-only tools must be genuinely non-error; the mutating critical tools must at least
  # respond over JSON-RPC (no transport error) — a tool-error on empty args is an expected validation
  # response, not a wiring failure.
  case "$tool" in
    honua_list_capabilities|honua_list_layers) [ "$status" = "ok" ] || non_error_ok=false ;;
    *) [ "$status" = "transport-error" ] && non_error_ok=false ;;
  esac
done

if [ "$MISSING" = "[]" ] && [ "$non_error_ok" = true ]; then
  note="catalog matches snapshot"; [ "$EXTRA" != "[]" ] && note="$note (+extra advertised: $EXTRA)"
  emit_scenario "S2-mcp-tool-catalog" pass "$note; all critical tools callable" \
    "$(jq -nc --argjson calls "$call_results" --argjson extra "$EXTRA" '{toolCalls:$calls,extraAdvertised:$extra}')"
else
  emit_scenario "S2-mcp-tool-catalog" fail "missing tools: $MISSING; non_error_ok=$non_error_ok" \
    "$(jq -nc --argjson m "$MISSING" --argjson calls "$call_results" '{missing:$m,toolCalls:$calls}')"
fi
