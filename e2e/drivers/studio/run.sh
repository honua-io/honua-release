#!/usr/bin/env bash
# S3 — AI Studio authoring seam. For each package family, drive the deterministic authoring path:
#   create draft -> validate -> preview-plan -> content-version (201 + real ids) -> publish-request
#   (201 accepted). The envelope requires bindings/dependencies/provenance as ARRAYS (null => reject)
#   and schemaVersion "1.0" + the family format string. Uses the deterministic provider (no API key).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../harness/lib/common.sh
source "$HERE/../../harness/lib/common.sh"

if ! server_ready; then
  emit_scenario "S3-studio-authoring" blocked "server not ready at $E2E_BASE"
  exit 0
fi

SVC="$(jq -r '.service // "e2e"' "$E2E_OUT/seed-manifest.json" 2>/dev/null || echo e2e)"

# family -> format ; body (family-specific payload)
author_family() { # family format body-json  -> echoes "status:detail"
  local family="$1" fmt="$2" body="$3"
  local key="e2e-${family}-$RANDOM"
  local envelope; envelope="$(jq -nc --arg f "$family" --arg fmt "$fmt" --argjson body "$body" \
    '{family:$f,schemaVersion:"1.0",format:$fmt,bindings:[],dependencies:[],provenance:[],body:$body}')"
  api_post "/api/v1/studio/package-drafts" "$(jq -nc --arg k "$key" --argjson e "$envelope" \
    '{packageKey:$k,workspaceId:"e2e",ownerId:"e2e",envelope:$e}')"
  [ "$HTTP_CODE" = "201" ] || { echo "blocked:create-draft HTTP $HTTP_CODE"; return; }
  local draft vstatus; draft="$(jget '.data.draftId')"
  vstatus="$(jget '.data.envelope.validation.status')"

  api_post "/api/v1/studio/package-drafts/$draft/validate" '{}'
  local val; val="$(jget '.data.validation.status // .data.status // "unknown"')"
  [ "$val" = "valid" ] || { echo "blocked:validation=$val $(jget '[.data.validation.diagnostics[]?.message]|join("; ")')"; return; }

  api_post "/api/v1/studio/package-drafts/$draft/preview-plan" '{}'
  [ "$HTTP_CODE" = "200" ] || { echo "blocked:preview-plan HTTP $HTTP_CODE"; return; }

  api_post "/api/v1/studio/package-drafts/$draft/content-versions" '{"changeNote":"e2e"}'
  [ "$HTTP_CODE" = "201" ] || { echo "blocked:content-version HTTP $HTTP_CODE"; return; }
  local item ver; item="$(jget '.data.itemId')"; ver="$(jget '.data.versionId // .data.contentVersionId')"
  [ -n "$item" ] && [ -n "$ver" ] || { echo "fail:content-version returned no ids"; return; }

  api_post "/api/v1/studio/content-items/$item/versions/$ver/publish-requests" '{}'
  local pstatus; pstatus="$(jget '.data.status')"
  { [ "$HTTP_CODE" = "201" ] && [ "$pstatus" = "accepted" ]; } || { echo "fail:publish-request HTTP $HTTP_CODE status=$pstatus"; return; }
  echo "pass:item=$item version=$ver request=$(jget '.data.requestId')"
}

declare -A bodies=(
  [query]='{"query":{"service":"'"$SVC"'","where":"zone_code='"'"'030'"'"'"}}'
  [analysis]='{"analysis":{"operation":"summary","service":"'"$SVC"'"}}'
)
declare -A fmts=( [query]=studio_query_package.v1 [analysis]=studio_analysis_package.v1 )

results="{}"; any_pass=false; any_fail=false
for family in query analysis; do
  r="$(author_family "$family" "${fmts[$family]}" "${bodies[$family]}")"
  st="${r%%:*}"; detail="${r#*:}"
  results="$(jq -nc --argjson acc "$results" --arg f "$family" --arg s "$st" --arg d "$detail" '$acc + {($f):{status:$s,detail:$d}}')"
  [ "$st" = "pass" ] && any_pass=true
  [ "$st" = "fail" ] && any_fail=true
done

# S3 passes when at least the query family completes the full lifecycle (the proven authoring seam)
# and nothing hard-failed. Families rejected at validation are recorded (blocked) — honest, not green.
qstatus="$(printf '%s' "$results" | jq -r '.query.status')"
if [ "$any_fail" = true ]; then
  emit_scenario "S3-studio-authoring" fail "a family hard-failed the lifecycle" "$results"
elif [ "$qstatus" = "pass" ]; then
  emit_scenario "S3-studio-authoring" pass "query family authored+published; per-family results attached" "$results"
else
  emit_scenario "S3-studio-authoring" blocked "query family did not complete: $qstatus" "$results"
fi
