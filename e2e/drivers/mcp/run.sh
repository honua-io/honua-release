#!/usr/bin/env bash
# S1/S2 — MCP handshake + BOUNDED discovery contract + Studio-critical tools/call.
#   S1: initialize -> assert JSON-RPC result carries protocolVersion + serverInfo.
#   S2: prove honua-server#3819's bounded default discovery contract end to end:
#         (a) an unnegotiated tools/list serves the server-authored `default` workflow view —
#             EXACTLY the committed bounded roster, in one page, identified by its own _meta;
#         (b) the same bound applies with no credential at all (it is a discovery bound, not a
#             credential artifact);
#         (c) the complete catalog is admin-only: `view: "full"` with no credential is denied;
#         (d) `view: "full"` under the harness admin key returns the WHOLE catalog, reached by
#             following nextCursor across pages of the server's documented page size, and the
#             assembled roster matches the committed snapshot AND the server's own totalToolCount;
#         (e) honua_list_capabilities is bounded the same way (<= 12 tools AND <= 12 resources, each
#             with its own cursor asserted against the server's own totals), and its admin-only full
#             inventory export both WORKS for an admin and is refused for anyone else;
#         (f) discovery is not authority: every Studio-critical tool still answers tools/call even
#             though it is outside the bounded default view.
#       Contract sources: honua-server#3819 (`5bf9410843`) and that repo's
#       docs/guides/connect/ai-agents-mcp.md ("Workflow views (bounded discovery)").
#       The rosters live in expected-tools.json (see honua-release#300).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../harness/lib/common.sh
source "$HERE/../../harness/lib/common.sh"
EXPECTED="$HERE/expected-tools.json"

# S2 MUST reach the report on every path. `run_all.sh` swallows a driver's nonzero exit
# (`|| echo "::warning:: driver $d exited non-zero"`) and `report.sh` assembles the verdict from
# whatever rows exist with no required-scenario list, so a driver that dies mid-way — one unguarded
# `jq` on a malformed response under `set -e` is enough — drops S2 silently and the gate can read
# green having never evaluated it. Two defences: `jq_or` below never fails the shell, and this trap
# emits a hard S2 failure if the driver leaves without one.
S2_EMITTED=false
emit_s2() { # status why [evidence-json]
  S2_EMITTED=true
  emit_scenario "S2-mcp-tool-catalog" "$@"
}
trap 'if [ "${S2_EMITTED:-false}" != true ]; then
        emit_scenario "S2-mcp-tool-catalog" fail \
          "driver aborted before S2 was evaluated (last HTTP ${HTTP_CODE:-none}): $(printf "%s" "${HTTP_BODY:-}" | head -c 200)"
      fi' EXIT

# Read a value out of a captured response body. NEVER fails: an unparseable body yields the caller's
# stated default, which then fails the comparison it feeds, so a malformed response reds S2 instead
# of killing the driver.
jq_or() { # default body jq-args...
  local def="$1" body="$2"; shift 2
  local out
  if out="$(printf '%s' "$body" | jq "$@" 2>/dev/null)"; then printf '%s' "$out"; else printf '%s' "$def"; fi
}

if ! server_ready; then
  emit_scenario "S1-mcp-handshake" blocked "server not ready at $E2E_BASE"
  emit_s2 blocked "server not ready at $E2E_BASE"
  exit 0
fi

rpc() { # method params-json  -> HTTP_BODY holds the JSON-RPC response (carries the admin key)
  local params="$2"; [ -z "$params" ] && params='{}'
  api_post "/mcp" "$(jq -nc --arg m "$1" --argjson p "$params" \
    '{jsonrpc:"2.0",id:1,method:$m,params:$p}')"
}

# The full-catalog export is admin-only and the default view must be bounded for EVERYONE, so S2
# needs one request shape the shared helper cannot make: the same POST /mcp with NO credential.
anon_rpc() { # method params-json -> HTTP_BODY holds the JSON-RPC response, unauthenticated
  local params="$2"; [ -z "$params" ] && params='{}'
  local tmp; tmp="$(mktemp)"
  HTTP_CODE="$(curl -sS -o "$tmp" -w '%{http_code}' -X POST "${E2E_BASE}/mcp" \
    -H "Content-Type: application/json" \
    --data "$(jq -nc --arg m "$1" --argjson p "$params" '{jsonrpc:"2.0",id:1,method:$m,params:$p}')" \
    || echo 000)"
  HTTP_BODY="$(cat "$tmp")"; rm -f "$tmp"
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

# ---- S2: the bounded discovery contract ----------------------------------------------------------
# Every check appends a human-readable reason to FAILURES; S2 passes only when FAILURES is empty.
# Nothing here degrades to a pass on an unexpected shape: an empty/absent field fails the comparison
# it feeds, so a server that stops serving a surface reds instead of reading as "nothing missing".
FAILURES=()
note_failure() { # usage: `[ x = y ] || note_failure "reason"` — records, never exits
  FAILURES+=("$1")
}

WANT_VIEW="$(jq -r '.defaultView.view' "$EXPECTED")"
WANT_TITLE="$(jq -r '.defaultView.title' "$EXPECTED")"
WANT_REVISION="$(jq -r '.defaultView.revision' "$EXPECTED")"
WANT_COUNT="$(jq -r '.defaultView.toolCount' "$EXPECTED")"
WANT_FULLNAME="$(jq -r '.defaultView.fullCatalogView' "$EXPECTED")"
WANT_DEFAULT="$(jq -c '.defaultView.tools | sort' "$EXPECTED")"
WANT_STAGES="$(jq -c '[.defaultView.stages[] | {id, tools:(.tools|sort)}] | sort_by(.id)' "$EXPECTED")"
WANT_FULL="$(jq -c '.fullCatalog.tools | sort' "$EXPECTED")"
FULL_VIEW_NAME="$(jq -r '.fullCatalog.view' "$EXPECTED")"
PAGE_SIZE="$(jq -r '.fullCatalog.pageSize' "$EXPECTED")"

# (a) The unnegotiated tools/list IS the bounded default view, served whole in one page.
#     honua-release#105 and honua-release#300 were both mis-read as server regressions because the
#     driver assumed a surface the server had stopped serving by default. So assert the SURFACE's
#     identity (_meta.view/revision/toolCount) before its contents: a future default-view change
#     then names itself instead of looking like 40 missing tools.
rpc tools/list '{}'
DEF_BODY="$HTTP_BODY"
DEF_NAMES="$(jq_or '[]' "$DEF_BODY" -c '[.result.tools[].name] | sort')"
DEF_META_VIEW="$(jq_or '' "$DEF_BODY" -r '.result._meta.view // ""')"
DEF_META_TITLE="$(jq_or '' "$DEF_BODY" -r '.result._meta.title // ""')"
DEF_META_REVISION="$(jq_or '' "$DEF_BODY" -r '.result._meta.revision // ""')"
DEF_META_COUNT="$(jq_or -1 "$DEF_BODY" -r '.result._meta.toolCount // -1')"
DEF_META_FULLNAME="$(jq_or '' "$DEF_BODY" -r '.result._meta.fullCatalogView // ""')"
DEF_META_DIGESTS="$(jq_or 'null' "$DEF_BODY" -c '.result._meta | {revisionDigest,membershipDigest,descriptorDigest}')"
DEF_CURSOR="$(jq_or '' "$DEF_BODY" -r '.result.nextCursor // empty')"
DEF_STAGES="$(jq_or '[]' "$DEF_BODY" -c '[.result._meta.stages[]? | {id, tools:(.tools|sort)}] | sort_by(.id)')"

[ "$DEF_META_VIEW" = "$WANT_VIEW" ] || note_failure "default tools/list served view '$DEF_META_VIEW', expected '$WANT_VIEW'"
[ "$DEF_META_TITLE" = "$WANT_TITLE" ] || note_failure "default view title '$DEF_META_TITLE', expected '$WANT_TITLE'"
[ "$DEF_META_REVISION" = "$WANT_REVISION" ] || note_failure "default view revision '$DEF_META_REVISION', expected '$WANT_REVISION'"
[ "$DEF_META_COUNT" = "$WANT_COUNT" ] || note_failure "_meta.toolCount $DEF_META_COUNT, expected $WANT_COUNT"
[ "$DEF_META_FULLNAME" = "$WANT_FULLNAME" ] || note_failure "_meta.fullCatalogView '$DEF_META_FULLNAME', expected '$WANT_FULLNAME' (the advertised escape hatch)"
# The view is budget-bounded by construction, so it arrives whole: a nextCursor here means the
# "bounded" surface is really a first page and the bound is not what it claims.
[ -z "$DEF_CURSOR" ] || note_failure "default view returned nextCursor '$DEF_CURSOR'; a budget-bounded view must arrive in one page"
# EXACT set equality, both directions. A missing tool means the bounded surface shrank; an EXTRA
# tool means the cap leaked, which is the regression #3819 exists to prevent — neither is a note.
DEF_MISSING="$(jq -nc --argjson live "$DEF_NAMES" --argjson want "$WANT_DEFAULT" '$want - $live')"
DEF_EXTRA="$(jq -nc --argjson live "$DEF_NAMES" --argjson want "$WANT_DEFAULT" '$live - $want')"
[ "$DEF_MISSING" = "[]" ] || note_failure "default view missing tools: $DEF_MISSING"
[ "$DEF_EXTRA" = "[]" ] || note_failure "default view advertised tools outside its documented bound: $DEF_EXTRA"
DEF_ACTUAL_COUNT="$(jq_or -1 "$DEF_NAMES" 'length')"
[ "$DEF_ACTUAL_COUNT" = "$WANT_COUNT" ] || note_failure "default view served $DEF_ACTUAL_COUNT tools, expected $WANT_COUNT"
[ "$DEF_STAGES" = "$WANT_STAGES" ] || note_failure "default view stage membership drifted: got $DEF_STAGES"

# (b) The bound is a DISCOVERY bound, not a credential artifact: an unauthenticated client sees the
#     same bounded view, not more and not less.
anon_rpc tools/list '{}'
ANON_BODY="$HTTP_BODY"
ANON_NAMES="$(jq_or '[]' "$ANON_BODY" -c '[.result.tools[].name] | sort')"
ANON_VIEW="$(jq_or '' "$ANON_BODY" -r '.result._meta.view // ""')"
ANON_CURSOR="$(jq_or '' "$ANON_BODY" -r '.result.nextCursor // empty')"
[ "$ANON_VIEW" = "$WANT_VIEW" ] || note_failure "unauthenticated tools/list served view '$ANON_VIEW', expected '$WANT_VIEW'"
[ "$ANON_NAMES" = "$WANT_DEFAULT" ] || note_failure "unauthenticated default roster differs from the bounded snapshot: $(jq_or '{}' "$(jq -nc --argjson a "$ANON_NAMES" --argjson w "$WANT_DEFAULT" '{missing:($w-$a),extra:($a-$w)}')" -c '.')"
# Same one-page bound as the authenticated default view. Without this an auth-specific regression
# could hand an anonymous client the expected 12 names PLUS a cursor into the rest, and both the
# roster check above and the `view: "full"` denial below would still pass while an unauthenticated
# caller enumerated past the bound.
[ -z "$ANON_CURSOR" ] || note_failure "unauthenticated default view returned nextCursor '$ANON_CURSOR'; an unauthenticated client must not be able to page past the bounded view"

# (c) The complete catalog is an ADMIN operation. Without a credential it must be denied — if an
#     anonymous caller can enumerate the full catalog, the bound is decorative and S2 must red.
anon_rpc tools/list "$(jq -nc --arg v "$FULL_VIEW_NAME" '{view:$v}')"
ANON_FULL_ERRCODE="$(jq_or '' "$HTTP_BODY" -r '.error.data.code // ""')"
ANON_FULL_RPCCODE="$(jq_or '' "$HTTP_BODY" -r '.error.code // ""')"
ANON_FULL_TOOLS="$(jq_or -1 "$HTTP_BODY" -r '[.result.tools[]?] | length')"
if [ "$ANON_FULL_TOOLS" -gt 0 ]; then
  note_failure "unauthenticated view '$FULL_VIEW_NAME' returned $ANON_FULL_TOOLS tools; the full catalog export must be admin-only"
elif [ "$ANON_FULL_ERRCODE" != "permission_denied" ] && [ "$ANON_FULL_ERRCODE" != "unauthenticated" ]; then
  note_failure "unauthenticated view '$FULL_VIEW_NAME' returned error code '$ANON_FULL_ERRCODE' (JSON-RPC $ANON_FULL_RPCCODE), expected permission_denied/unauthenticated"
fi

# (d) Under the admin credential, `view: "full"` returns the WHOLE catalog — paginated at the
#     server's documented page size, so following nextCursor is load-bearing here rather than
#     incidental (it is what honua-release#105 added and what the bounded default no longer needs).
#     A page cap keeps a server that never stops handing out cursors loud instead of looping.
FULL_NAMES="[]"
CURSOR=""
PAGES=0
MAX_PAGES=50
OVERSIZED_PAGES="[]"
FULL_WALK_ABORTED=false
while :; do
  if [ -z "$CURSOR" ]; then
    rpc tools/list "$(jq -nc --arg v "$FULL_VIEW_NAME" '{view:$v}')"
  else
    rpc tools/list "$(jq -nc --arg v "$FULL_VIEW_NAME" --arg c "$CURSOR" '{view:$v,cursor:$c}')"
  fi
  PAGE="$(jq_or '[]' "$HTTP_BODY" -c '[.result.tools[].name]')"
  PAGE_LEN="$(jq_or 0 "$PAGE" 'length')"
  PAGES=$((PAGES + 1))
  if [ "$PAGE_LEN" -gt "$PAGE_SIZE" ]; then
    OVERSIZED_PAGES="$(jq -nc --argjson acc "$OVERSIZED_PAGES" --argjson p "$PAGES" --argjson n "$PAGE_LEN" '$acc + [{page:$p,size:$n}]')"
  fi
  FULL_NAMES="$(jq -nc --argjson acc "$FULL_NAMES" --argjson page "$PAGE" '$acc + $page')"
  CURSOR="$(jget '.result.nextCursor // empty')"
  [ -z "$CURSOR" ] && break
  if [ "$PAGES" -ge "$MAX_PAGES" ]; then
    note_failure "view '$FULL_VIEW_NAME' did not terminate after $MAX_PAGES pages (last cursor: $CURSOR)"
    FULL_WALK_ABORTED=true
    break
  fi
done
FULL_LIVE="$(jq -nc --argjson n "$FULL_NAMES" '$n | sort')"
FULL_MISSING="$(jq -nc --argjson live "$FULL_LIVE" --argjson want "$WANT_FULL" '$want - $live')"
FULL_EXTRA="$(jq -nc --argjson live "$FULL_LIVE" --argjson want "$WANT_FULL" '$live - $want')"
FULL_LEN="$(jq_or -1 "$FULL_LIVE" 'length')"
[ "$FULL_MISSING" = "[]" ] || note_failure "full catalog missing tools: $FULL_MISSING"
[ "$OVERSIZED_PAGES" = "[]" ] || note_failure "full-catalog pages exceeded the documented page size $PAGE_SIZE: $OVERSIZED_PAGES"
# The full roster is bigger than one page, so cursor-following MUST have happened. A single-page
# walk here means the export silently truncated (the honua-release#105 failure shape).
if [ "$FULL_WALK_ABORTED" = false ] && [ "$FULL_LEN" -gt "$PAGE_SIZE" ] && [ "$PAGES" -lt 2 ]; then
  note_failure "full catalog returned $FULL_LEN tools in $PAGES page(s) at page size $PAGE_SIZE; cursor-following did not occur"
fi

# (e) The same bound governs honua_list_capabilities: at most 12 tools and 12 resources by default,
#     with INDEPENDENT cursors (both asserted, against the server's own totals), and a full inventory
#     export that is admin-only -- proven in both directions: the admin call must actually return the
#     complete inventory, and the anonymous call must be refused with an authorization code. Its
#     totalToolCount is the SERVER's own count of the catalog, so cross-checking the walk against it
#     means a stale hard-coded roster size cannot mask a truncated enumeration.
rpc tools/call '{"name":"honua_list_capabilities","arguments":{}}'
CAPS_BOUNDED="$HTTP_BODY"
CAPS_TOOLS="$(jq_or -1 "$CAPS_BOUNDED" -r '.result.structuredContent.toolCount // -1')"
CAPS_RESOURCES="$(jq_or -1 "$CAPS_BOUNDED" -r '.result.structuredContent.resourceCount // -1')"
CAPS_TOTAL_TOOLS="$(jq_or -1 "$CAPS_BOUNDED" -r '.result.structuredContent.totalToolCount // -1')"
CAPS_TOTAL_RESOURCES="$(jq_or -1 "$CAPS_BOUNDED" -r '.result.structuredContent.totalResourceCount // -1')"
CAPS_NEXT_TOOL="$(jq_or '' "$CAPS_BOUNDED" -r '.result.structuredContent.nextToolCursor // empty')"
CAPS_NEXT_RESOURCE="$(jq_or '' "$CAPS_BOUNDED" -r '.result.structuredContent.nextResourceCursor // empty')"
CAPS_DEFAULT_VIEW="$(jq_or 'null' "$CAPS_BOUNDED" -c --arg v "$WANT_VIEW" '[.result.structuredContent.workflowViews[]? | select(.name==$v)] | first // null')"
if [ "$CAPS_TOOLS" -lt 0 ] || [ "$CAPS_RESOURCES" -lt 0 ] || [ "$CAPS_TOTAL_TOOLS" -lt 0 ] || [ "$CAPS_TOTAL_RESOURCES" -lt 0 ]; then
  note_failure "honua_list_capabilities did not report toolCount/resourceCount/totalToolCount/totalResourceCount: $(printf '%s' "$CAPS_BOUNDED" | head -c 200)"
else
  [ "$CAPS_TOOLS" -le "$PAGE_SIZE" ] || note_failure "honua_list_capabilities advertised $CAPS_TOOLS tools per page, above the documented bound $PAGE_SIZE"
  [ "$CAPS_RESOURCES" -le "$PAGE_SIZE" ] || note_failure "honua_list_capabilities advertised $CAPS_RESOURCES resources per page, above the documented bound $PAGE_SIZE"
  # Independent cursors, asserted independently against the server's own totals. Checking only the
  # TOOL cursor would let a server truncate a >12 resource inventory with no nextResourceCursor and
  # still pass — the resource half of the bound would be uncertified.
  if [ "$CAPS_TOTAL_TOOLS" -gt "$CAPS_TOOLS" ] && [ -z "$CAPS_NEXT_TOOL" ]; then
    note_failure "honua_list_capabilities bounded $CAPS_TOOLS of $CAPS_TOTAL_TOOLS tools but returned no nextToolCursor"
  fi
  if [ "$CAPS_TOTAL_RESOURCES" -gt "$CAPS_RESOURCES" ] && [ -z "$CAPS_NEXT_RESOURCE" ]; then
    note_failure "honua_list_capabilities bounded $CAPS_RESOURCES of $CAPS_TOTAL_RESOURCES resources but returned no nextResourceCursor"
  fi
  if [ "$CAPS_TOTAL_RESOURCES" -lt "$CAPS_RESOURCES" ]; then
    note_failure "honua_list_capabilities reported totalResourceCount=$CAPS_TOTAL_RESOURCES below its own page of $CAPS_RESOURCES resources"
  fi
  if [ "$FULL_WALK_ABORTED" = false ] && [ "$FULL_LEN" != "$CAPS_TOTAL_TOOLS" ]; then
    note_failure "view '$FULL_VIEW_NAME' enumerated $FULL_LEN tools but the server reports totalToolCount=$CAPS_TOTAL_TOOLS"
  fi
fi
# The server publishes the view catalog so clients need no local view inventory; the default view it
# advertises must be the one it actually served.
CAPS_VIEW_COUNT="$(printf '%s' "$CAPS_DEFAULT_VIEW" | jq -r '.toolCount // -1')"
CAPS_VIEW_REVISION="$(printf '%s' "$CAPS_DEFAULT_VIEW" | jq -r '.revision // ""')"
[ "$CAPS_VIEW_COUNT" = "$WANT_COUNT" ] || note_failure "honua_list_capabilities advertises view '$WANT_VIEW' with toolCount $CAPS_VIEW_COUNT, expected $WANT_COUNT"
[ "$CAPS_VIEW_REVISION" = "$WANT_REVISION" ] || note_failure "honua_list_capabilities advertises view '$WANT_VIEW' revision '$CAPS_VIEW_REVISION', expected '$WANT_REVISION'"
# The admin-only full inventory export, proven in BOTH directions. The positive leg comes first and
# is load-bearing: without it, a `fullExport` that was removed, renamed, or that rejects every caller
# would still produce the anonymous error the negative leg wants, and S2 would certify an admin
# operation that does not work. So require the admin call to SUCCEED and to return the COMPLETE
# inventory — every tool and every resource the server counts, in one un-cursored response.
rpc tools/call '{"name":"honua_list_capabilities","arguments":{"fullExport":true}}'
CAPS_FULL="$HTTP_BODY"
CAPS_FULL_ISERR="$(jq_or '' "$CAPS_FULL" -r '.result.isError // empty')"
CAPS_FULL_TRANSPORT="$(jq_or '' "$CAPS_FULL" -r '.error.message // empty')"
CAPS_FULL_TOOLS="$(jq_or -1 "$CAPS_FULL" -r '.result.structuredContent.toolCount // -1')"
CAPS_FULL_RESOURCES="$(jq_or -1 "$CAPS_FULL" -r '.result.structuredContent.resourceCount // -1')"
CAPS_FULL_NEXT_TOOL="$(jq_or '' "$CAPS_FULL" -r '.result.structuredContent.nextToolCursor // empty')"
CAPS_FULL_NEXT_RESOURCE="$(jq_or '' "$CAPS_FULL" -r '.result.structuredContent.nextResourceCursor // empty')"
CAPS_FULL_NAMES="$(jq_or '[]' "$CAPS_FULL" -c '[.result.structuredContent.tools[]?.name] | sort')"
if [ -n "$CAPS_FULL_TRANSPORT" ] || [ "$CAPS_FULL_ISERR" = "true" ]; then
  note_failure "admin honua_list_capabilities fullExport did not succeed: $(printf '%s' "$CAPS_FULL" | head -c 200)"
else
  [ "$CAPS_FULL_TOOLS" = "$CAPS_TOTAL_TOOLS" ] || note_failure "admin fullExport returned $CAPS_FULL_TOOLS of the server's $CAPS_TOTAL_TOOLS tools; the export must be complete"
  [ "$CAPS_FULL_RESOURCES" = "$CAPS_TOTAL_RESOURCES" ] || note_failure "admin fullExport returned $CAPS_FULL_RESOURCES of the server's $CAPS_TOTAL_RESOURCES resources; the export must be complete"
  # A complete export is complete: it does not hand back a cursor into a remainder.
  [ -z "$CAPS_FULL_NEXT_TOOL" ] || note_failure "admin fullExport returned nextToolCursor '$CAPS_FULL_NEXT_TOOL'; a full export must not be paged"
  [ -z "$CAPS_FULL_NEXT_RESOURCE" ] || note_failure "admin fullExport returned nextResourceCursor '$CAPS_FULL_NEXT_RESOURCE'; a full export must not be paged"
  # Two independent admin paths to the same catalog must agree, so neither can drift alone.
  if [ "$FULL_WALK_ABORTED" = false ] && [ "$CAPS_FULL_NAMES" != "$FULL_LIVE" ]; then
    note_failure "admin fullExport roster differs from the '$FULL_VIEW_NAME' cursor walk: $(jq_or '{}' "$(jq -nc --argjson a "$CAPS_FULL_NAMES" --argjson w "$FULL_LIVE" '{onlyInExport:($a-$w),onlyInWalk:($w-$a)}')" -c '.')"
  fi
fi
# The negative leg: an anonymous caller must be refused, and refused for the RIGHT reason — an
# authorization code, not any error the tool happens to raise.
anon_rpc tools/call '{"name":"honua_list_capabilities","arguments":{"fullExport":true}}'
CAPS_ANON_BODY="$HTTP_BODY"
CAPS_ANON_ISERR="$(jq_or '' "$CAPS_ANON_BODY" -r '.result.isError // empty')"
CAPS_ANON_RPCERR="$(jq_or '' "$CAPS_ANON_BODY" -r '.error.code // empty')"
CAPS_ANON_CODE="$(jq_or '' "$CAPS_ANON_BODY" -r '.result.structuredContent.code // .error.data.code // ""')"
CAPS_ANON_TOOLCOUNT="$(jq_or -1 "$CAPS_ANON_BODY" -r '.result.structuredContent.toolCount // -1')"
if [ "$CAPS_ANON_TOOLCOUNT" -gt "$PAGE_SIZE" ]; then
  note_failure "unauthenticated honua_list_capabilities fullExport returned $CAPS_ANON_TOOLCOUNT tools; the full inventory export must be admin-only"
elif [ "$CAPS_ANON_ISERR" != "true" ] && [ -z "$CAPS_ANON_RPCERR" ]; then
  note_failure "unauthenticated honua_list_capabilities fullExport was not refused: $(printf '%s' "$CAPS_ANON_BODY" | head -c 200)"
elif [ "$CAPS_ANON_CODE" != "unauthenticated" ] && [ "$CAPS_ANON_CODE" != "permission_denied" ]; then
  note_failure "unauthenticated honua_list_capabilities fullExport was refused with code '$CAPS_ANON_CODE', expected unauthenticated/permission_denied"
fi

# (f) Discovery is not authority. The Studio-critical tools sit OUTSIDE the bounded default view, and
#     that must not change whether they answer tools/call — bounding discovery narrows what a model
#     is shown, it never removes a wired operation. Also call two read-only tools that must be
#     genuinely non-error.
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
[ "$non_error_ok" = true ] || note_failure "a critical tool did not answer tools/call: $call_results"

EVIDENCE="$(jq -nc \
  --argjson defaultMeta "$(jq -nc --arg v "$DEF_META_VIEW" --arg r "$DEF_META_REVISION" --argjson c "$DEF_META_COUNT" \
      --arg f "$DEF_META_FULLNAME" --argjson d "$DEF_META_DIGESTS" --arg nc "$DEF_CURSOR" \
      '{view:$v,revision:$r,toolCount:$c,fullCatalogView:$f,digests:$d,
        nextCursor:(if $nc == "" then null else $nc end)}')" \
  --argjson defaultTools "$DEF_NAMES" \
  --argjson fullCatalog "$(jq -nc --argjson n "$FULL_LEN" --argjson p "$PAGES" --argjson s "$PAGE_SIZE" \
      --argjson t "$CAPS_TOTAL_TOOLS" --argjson extra "$FULL_EXTRA" \
      '{tools:$n,pages:$p,pageSize:$s,serverTotalToolCount:$t,extraAdvertised:$extra}')" \
  --argjson adminOnly "$(jq -nc --arg e "$ANON_FULL_ERRCODE" '{anonymousFullView:$e,anonymousFullFinding:"denied"}')" \
  --argjson listCapabilities "$(jq -nc --argjson t "$CAPS_TOOLS" --argjson r "$CAPS_RESOURCES" --argjson tt "$CAPS_TOTAL_TOOLS" \
      --arg nt "$CAPS_NEXT_TOOL" --arg nr "$CAPS_NEXT_RESOURCE" \
      '{tools:$t,resources:$r,totalToolCount:$tt,
        nextToolCursor:(if $nt == "" then null else $nt end),
        nextResourceCursor:(if $nr == "" then null else $nr end)}')" \
  --argjson fullExport "$(jq -nc --argjson t "$CAPS_FULL_TOOLS" --argjson r "$CAPS_FULL_RESOURCES" \
      --argjson tt "$CAPS_TOTAL_TOOLS" --argjson tr "$CAPS_TOTAL_RESOURCES" --arg anon "$CAPS_ANON_CODE" \
      '{admin:{tools:$t,resources:$r,ofTools:$tt,ofResources:$tr},anonymous:$anon}')" \
  --argjson calls "$call_results" \
  '{defaultView:$defaultMeta,defaultViewTools:$defaultTools,fullCatalog:$fullCatalog,adminOnly:$adminOnly,listCapabilities:$listCapabilities,listCapabilitiesFullExport:$fullExport,toolCalls:$calls}')"

if [ "${#FAILURES[@]}" -eq 0 ]; then
  note="bounded default view '$DEF_META_VIEW' ($DEF_META_REVISION) = $DEF_ACTUAL_COUNT tools in 1 page; admin '$FULL_VIEW_NAME' export = $FULL_LEN tools over $PAGES pages (server totalToolCount=$CAPS_TOTAL_TOOLS); anonymous full export denied; all critical tools callable"
  if [ "$FULL_EXTRA" != "[]" ]; then note="$note (+extra advertised in full catalog: $FULL_EXTRA)"; fi
  emit_s2 pass "$note" "$EVIDENCE"
else
  # A pre-#3819 server trips nearly every check at once, so `why` carries the leading reasons and
  # evidence.failures carries all of them — the report never drops a finding, it only stops the
  # one-line summary from becoming a wall of roster diffs.
  ALL_FAILURES="$(printf '%s\n' "${FAILURES[@]}" | jq -Rc . | jq -sc .)"
  why="$(printf '%s; ' "${FAILURES[@]:0:4}")"; why="${why%; }"
  if [ "${#FAILURES[@]}" -gt 4 ]; then why="$why (+$(( ${#FAILURES[@]} - 4 )) more; see evidence.failures)"; fi
  emit_s2 fail "$why" \
    "$(jq -nc --argjson ev "$EVIDENCE" --argjson f "$ALL_FAILURES" '$ev + {failures:$f}')"
fi
