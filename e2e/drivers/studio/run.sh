#!/usr/bin/env bash
# S3 — AI Studio authoring seam.
#
# For each package family, drive the deterministic authoring path end to end:
#   create draft -> validate -> preview-plan -> content-version (201 + real ids) -> publish-request,
# asserting the server's OWN declared contract for that family rather than a hardcoded copy of it.
#
# TWO THINGS THIS SEAM LEARNED THE HARD WAY (honua-release#305)
#
# 1. THE OPERATOR GATE. Every Studio draft mutation runs through the durable operation runtime. Under
#    disabled licensing mode, the entitlement snapshot uses the Enterprise guardrail ladder.
#    DefaultGuardrailLadder maps mutating classes to
#    GuardrailTier.RequiresApproval, so `POST /studio/package-drafts` answers 202 with an operation
#    handle and a control-plane proposal instead of 201 with a draft. That is CORRECT product
#    behaviour; the seam has to drive it. Approval is separation-of-duties enforced, so the seam
#    provisions a SECOND identity (an `admin:approve` key) and approves as the approver, then polls
#    the operation handle to Completed and reads the typed result. On an edition that direct-executes
#    (Community/Pro) the same steps answer 200/201 and the approval lane is simply not entered.
#    WHICH of those is honest is not the server's answer to decide: the seam asks the server for its
#    EDITION first and requires the lane that edition's guardrail ladder mandates, so a 200 on the
#    Enterprise harness is a guardrail regression and fails. The report records both lanes.
#
# 2. WHAT `blocked` IS FOR. Per e2e/canonical_checks.py (honua-release#128) `blocked` means the probe
#    had no INPUT to work with -- something OURS to supply was missing. A reachable server answering
#    with a status this seam does not accept has not failed to run; it has found a defect. So the only
#    `blocked` here is "server not ready"; everything else is pass or fail. Before #305 every
#    unexpected status returned `blocked`, which is how a permanently-202 create-draft rode every
#    trunk run as a tolerated BLOCKED instead of a gate failure.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../harness/lib/common.sh
source "$HERE/../../harness/lib/common.sh"

SCENARIO="S3-studio-authoring"

if ! server_ready; then
  emit_scenario "$SCENARIO" blocked "server not ready at $E2E_BASE"
  exit 0
fi

# TWO seeded services, because the two families need different ones and a single $SVC cannot be
# both. The query fixture filters on `zone_code`, which exists ONLY on the top-level `maui-zoning`
# service (seed.sh publishes honua_data.maui_zoning with the string codes; honua_data.e2e_src_fs
# carries just gid/name/geom). The analysis fixture needs a (service, layerId) PAIR, and the
# manifest pins one only for slice1's `e2e` service. Crossing them publishes a query the server can
# never answer.
QUERY_SVC="$(jq -r '.service // "maui-zoning"' "$E2E_OUT/seed-manifest.json" 2>/dev/null || echo maui-zoning)"
ANALYSIS_SVC="$(jq -r '.slice1.e2e_src_fs.service // "e2e"' "$E2E_OUT/seed-manifest.json" 2>/dev/null || echo e2e)"
LAYER="$(jq -r '.slice1.e2e_src_fs.layerId // 0' "$E2E_OUT/seed-manifest.json" 2>/dev/null || echo 0)"

# --- which guardrail lane this edition MUST drive ---------------------------------------------------
# DefaultGuardrailLadder is edition-driven: Enterprise routes mutating operation classes through
# approval, Community/Pro direct-execute, and any other edition fails closed to approval
# (honua-server src/Honua.Core/Features/Guardrails/DefaultGuardrailLadder.cs). compose.candidate.yml
# grants Enterprise, so on THIS harness a direct-executed Studio mutation is the guardrail
# REGRESSING -- and accepting 200/201 unconditionally would let every family pass with
# `separationOfDuties: not-exercised` and zero proposals. Ask the server which edition it runs and
# require the lane that edition mandates; only an explicitly direct-executing edition may skip it.
api_get "/api/v1/admin/license"
if [ "$HTTP_CODE" != "200" ]; then
  emit_scenario "$SCENARIO" fail \
    "GET /api/v1/admin/license -> HTTP $HTTP_CODE (cannot tell which guardrail lane this edition is required to drive)"
  exit 0
fi
EDITION="$(jget '.data.edition')"
case "$EDITION" in
  Community|Pro) EXPECT_GATE=false ;;
  *)             EXPECT_GATE=true ;;   # Enterprise, and the ladder's own defensive default
esac

# --- the approval lane ------------------------------------------------------------------------------
# api_json (common.sh) always carries the AUTHOR key. The approver is a distinct principal, so it gets
# its own minimal helper here rather than widening the shared wrapper.
APPROVER_KEY=""
approver_post() { # path json -> sets AP_CODE, AP_BODY
  local tmp; tmp="$(mktemp)"
  AP_CODE="$(curl -sS -o "$tmp" -w '%{http_code}' -X POST "${E2E_BASE}$1" \
    -H "Content-Type: application/json" -H "X-API-Key: ${APPROVER_KEY}" --data "$2" || echo 000)"
  AP_BODY="$(cat "$tmp")"; rm -f "$tmp"
}

# provision_approver — mint the second identity the operator gate requires. Empty APPROVER_KEY means
# the server refused; the caller turns that into a fail (the server answered, and wrongly).
provision_approver() {
  api_post "/api/v1/admin/api-keys" \
    '{"name":"e2e-s3-studio-approver","permissions":["admin:approve"]}'
  [ "$HTTP_CODE" = "201" ] || { APPROVER_REASON="POST /admin/api-keys -> HTTP $HTTP_CODE"; return 1; }
  APPROVER_KEY="$(jget '.data.key')"
  [ -n "$APPROVER_KEY" ] && [ "$APPROVER_KEY" != "null" ] \
    || { APPROVER_REASON="minted approver key carried no secret"; return 1; }
}

# Gate telemetry lives in a FILE, not in shell variables: studio_step is called through a command
# substitution, so it runs in a subshell and any variable it sets is discarded when that subshell
# exits. The lane still ran -- but the report would claim `direct-execute, 0 proposals` for a run that
# actually approved a proposal per step, which is precisely the kind of quiet mis-evidence this
# scenario exists to stop.
GATE_STATE="$E2E_OUT/s3-gate-state.json"
jq -nc --arg ed "$EDITION" --argjson req "$EXPECT_GATE" \
  '{edition:$ed, requiredLane:(if $req then "operator-gated" else "direct-execute" end),
    mode:"direct-execute", proposalsApproved:0, separationOfDuties:"not-exercised"}' > "$GATE_STATE"
gate_get() { jq -r --arg k "$1" '.[$k]' "$GATE_STATE"; }
gate_set() { # key json-value
  local tmp; tmp="$(mktemp)"
  jq -c --arg k "$1" --argjson v "$2" '.[$k] = $v' "$GATE_STATE" > "$tmp" && mv "$tmp" "$GATE_STATE"
}

# studio_step PATH BODY LABEL -> echoes "ok:<result-json>" | "fail:<reason>"
#
# 200/201 is the direct-execute answer and its `data` IS the result. 202 is the operator gate: assert
# the handle+proposal, approve as the approver, then poll the operation handle for the typed result.
studio_step() {
  local path="$1" body="$2" label="$3"
  api_post "$path" "$body"

  if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
    if [ "$EXPECT_GATE" = true ]; then
      printf 'fail:%s direct-executed (HTTP %s) on the %s edition, whose guardrail ladder routes mutating Studio classes through approval -- the operator gate did not run\n' \
        "$label" "$HTTP_CODE" "$EDITION"; return
    fi
    printf 'ok:%s\n' "$(jget '.data')"; return
  fi

  if [ "$HTTP_CODE" != "202" ]; then
    printf 'fail:%s HTTP %s %s\n' "$label" "$HTTP_CODE" \
      "$(printf '%s' "$HTTP_BODY" | tr -d '\n' | cut -c1-180)"; return
  fi

  gate_set mode '"operator-gated"'
  local handle proposal
  handle="$(jget '.data.handleId // .data.operationInstanceId')"
  proposal="$(jget '.data.proposalId')"
  if [ -z "$handle" ] || [ "$handle" = "null" ] || [ -z "$proposal" ] || [ "$proposal" = "null" ]; then
    printf 'fail:%s 202 without an operation handle + proposal id (%s)\n' "$label" \
      "$(printf '%s' "$HTTP_BODY" | tr -d '\n' | cut -c1-180)"; return
  fi
  # Separation of duties: the requester must NOT be able to approve its own proposal. Asserted once --
  # it is a property of the gate, not of each step -- and only on a proposal that is then approved by
  # the real approver, so the check costs no extra lifecycle work.
  if [ "$(gate_get separationOfDuties)" = "not-exercised" ]; then
    api_post "/api/v1/admin/proposals/$proposal/approve" '{}'
    if [ "$HTTP_CODE" = "403" ]; then
      gate_set separationOfDuties '"enforced"'
    else
      gate_set separationOfDuties "$(jq -nc --arg c "$HTTP_CODE" '"NOT-enforced (self-approve -> HTTP " + $c + ")"')"
      printf 'fail:%s separation of duties not enforced: the proposal requester self-approved (HTTP %s)\n' \
        "$label" "$HTTP_CODE"; return
    fi
  fi

  approver_post "/api/v1/admin/proposals/$proposal/approve" '{}'
  if [ "$AP_CODE" != "200" ]; then
    printf 'fail:%s approve proposal -> HTTP %s %s\n' "$label" "$AP_CODE" \
      "$(printf '%s' "$AP_BODY" | tr -d '\n' | cut -c1-180)"; return
  fi
  # Counted only HERE: a proposal that was raised but never approved (self-approve refused the
  # requester, the approver got 403/500) must not show up in the failure evidence as approved.
  gate_set proposalsApproved "$(( $(gate_get proposalsApproved) + 1 ))"

  # The approval executes the operation. Poll the handle rather than trusting the approval response.
  local deadline=$(( $(date +%s) + 60 )) status=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    api_get "/api/v1/operations/handles/$handle"
    status="$(jget '.data.status')"
    case "$status" in
      Completed) printf 'ok:%s\n' "$(jget '.data.result.details.payload // (.data|tojson)')"; return ;;
      Failed|Denied|Rejected|Cancelled)
        printf 'fail:%s operation %s: %s\n' "$label" "$status" "$(jget '.data.reason // ""')"; return ;;
    esac
    sleep 2
  done
  printf 'fail:%s operation handle never reached Completed (last status: %s)\n' "$label" "${status:-none}"
}

# --- the server's declared family contracts ---------------------------------------------------------
# Hardcoding a family's format string is how the analysis family sat wrong for the life of this driver
# (studio_analysis_package.v1 vs the declared honua.analysis-content.v1) behind a BLOCKED create-draft.
# Read the contract from the server and assert against that.
api_get "/api/v1/studio/package-families"
if [ "$HTTP_CODE" != "200" ]; then
  emit_scenario "$SCENARIO" fail \
    "GET /api/v1/studio/package-families -> HTTP $HTTP_CODE (the family contract this seam asserts against is unreadable)"
  exit 0
fi
FAMILIES="$HTTP_BODY"

descriptor() { printf '%s' "$FAMILIES" | jq -c --arg f "$1" '.data.families[]? | select(.family == $f)'; }

# --- family bodies ----------------------------------------------------------------------------------
# Deterministic, seeded-data-backed payloads. `rasterSources: {}` is explicit on the analysis plan step:
# omitting it is rejected at persistence with invalid_field "Raster source bindings must be an object
# when supplied." even though the field has a non-null default (honua-server side; noted in #305).
query_body() { jq -nc --arg s "$QUERY_SVC" '{query:{service:$s, where:"zone_code='030'"}}'; }
analysis_body() {
  jq -nc --arg s "$ANALYSIS_SVC" --arg l "$LAYER" '
    {plan:{planId:"e2e-plan", intentId:"e2e-intent",
           steps:[{stepId:"s1", kind:"QueryFeatures",
                   inputs:{service:$s, layerId:$l, where:"1=1"},
                   rasterSources:{}, dependsOn:[]}],
           outputs:["Table"]},
     requestedArtifacts:["Table"]}'
}

# publish_required FAMILY -> 0 when this seam requires the family to still reach publish-request.
# Only `analysis` is a known non-publishable family (publishSupported=false, by its own descriptor).
publish_required() { case "$1" in analysis) return 1 ;; *) return 0 ;; esac; }

# author_family FAMILY -> echoes "pass:<detail>" | "fail:<detail>"
author_family() {
  local family="$1" desc fmt schema body
  desc="$(descriptor "$family")"
  [ -n "$desc" ] || { echo "fail:server does not advertise the '$family' family"; return; }
  fmt="$(printf '%s' "$desc" | jq -r '.format')"
  schema="$(printf '%s' "$desc" | jq -r '.currentSchemaVersion')"
  case "$family" in
    query)    body="$(query_body)" ;;
    analysis) body="$(analysis_body)" ;;
    *) echo "fail:no e2e fixture for family '$family'"; return ;;
  esac

  # The envelope requires bindings/dependencies/provenance as ARRAYS (null => reject), the family's
  # declared schemaVersion, and its declared format string.
  local envelope key r draft
  envelope="$(jq -nc --arg f "$family" --arg sv "$schema" --arg fmt "$fmt" --argjson body "$body" \
    '{family:$f, schemaVersion:$sv, format:$fmt, bindings:[], dependencies:[], provenance:[], body:$body}')"
  key="e2e-${family}-$RANDOM"

  r="$(studio_step "/api/v1/studio/package-drafts" \
        "$(jq -nc --arg k "$key" --argjson e "$envelope" \
           '{packageKey:$k, workspaceId:"e2e", ownerId:"e2e", envelope:$e}')" create-draft)"
  [ "${r%%:*}" = "ok" ] || { echo "$r"; return; }
  draft="$(printf '%s' "${r#ok:}" | jq -r '.draftId // .resourceIds.draftId // empty')"
  [ -n "$draft" ] || { echo "fail:create-draft returned no draftId"; return; }

  r="$(studio_step "/api/v1/studio/package-drafts/$draft/validate" '{}' validate)"
  [ "${r%%:*}" = "ok" ] || { echo "$r"; return; }
  local val diag
  val="$(printf '%s' "${r#ok:}" | jq -r '.validation.status // .status // "unknown"')"
  if [ "$val" != "valid" ]; then
    # The gated `validate` operation's typed payload IS the validation summary; the direct-execute
    # response nests it under .validation. Read whichever the running lane produced -- a verdict with
    # no diagnostics attached is an unactionable failure report.
    diag="$(printf '%s' "${r#ok:}" | jq -r '[(.validation.diagnostics // .diagnostics // [])[]|"\(.code) \(.path): \(.message)"]|join("; ")')"
    echo "fail:validation=$val $diag"; return
  fi

  r="$(studio_step "/api/v1/studio/package-drafts/$draft/preview-plan" '{}' preview-plan)"
  [ "${r%%:*}" = "ok" ] || { echo "$r"; return; }

  r="$(studio_step "/api/v1/studio/package-drafts/$draft/content-versions" '{"changeNote":"e2e"}' content-version)"
  [ "${r%%:*}" = "ok" ] || { echo "$r"; return; }
  local item ver
  item="$(printf '%s' "${r#ok:}" | jq -r '.itemId // .resourceIds.itemId // empty')"
  ver="$(printf '%s' "${r#ok:}" | jq -r '.versionId // .contentVersionId // .resourceIds.versionId // empty')"
  { [ -n "$item" ] && [ -n "$ver" ]; } || { echo "fail:content-version returned no ids"; return; }

  # Publish what the family's own descriptor advertises -- but a descriptor is not a licence to drop
  # the assertion. `analysis` legitimately stops at the content-version boundary (it declares
  # publishSupported=false and omits publish-request.create), and driving it anyway asserted a
  # contract the server never offered. `query` publishing IS this scenario's documented lifecycle, so
  # a query descriptor that stops advertising publish-request.create is a contract REGRESSION, not a
  # shorter happy path -- absorbing it would silently delete the publish assertion from the S3 gate.
  local advertises_publish=false
  if printf '%s' "$desc" | jq -e '[.supportedOperations[]?] | index("publish-request.create")' >/dev/null 2>&1; then
    advertises_publish=true
  fi
  if [ "$advertises_publish" != true ]; then
    if publish_required "$family"; then
      echo "fail:the '$family' family no longer advertises publish-request.create (publishSupported=$(printf '%s' "$desc" | jq -r '.publishSupported')) -- publish is part of this seam's required lifecycle"
      return
    fi
    echo "pass:item=$item version=$ver publish=not-advertised-by-family ($(printf '%s' "$desc" | jq -r '.limitations[0] // "no publish-request.create in supportedOperations"'))"
    return
  fi

  r="$(studio_step "/api/v1/studio/content-items/$item/versions/$ver/publish-requests" '{}' publish-request)"
  [ "${r%%:*}" = "ok" ] || { echo "$r"; return; }
  local pstatus
  pstatus="$(printf '%s' "${r#ok:}" | jq -r '.status // empty')"
  [ "$pstatus" = "accepted" ] || { echo "fail:publish-request status=$pstatus (expected accepted)"; return; }
  echo "pass:item=$item version=$ver request=$(printf '%s' "${r#ok:}" | jq -r '.requestId // ""')"
}

# --- drive ------------------------------------------------------------------------------------------
APPROVER_REASON=""
if ! provision_approver; then
  # The server is up and refused to mint the approver identity the operator gate requires. That is the
  # server answering, and wrongly -- a fail, not a blocked.
  emit_scenario "$SCENARIO" fail \
    "could not provision the admin:approve identity the Studio operator gate requires: $APPROVER_REASON"
  exit 0
fi

results="{}"; any_fail=false
for family in query analysis; do
  r="$(author_family "$family")"
  st="${r%%:*}"; detail="${r#*:}"
  results="$(jq -nc --argjson acc "$results" --arg f "$family" --arg s "$st" --arg d "$detail" \
    '$acc + {($f):{status:$s, detail:$d}}')"
  [ "$st" = "pass" ] || any_fail=true
done

GATE_MODE="$(gate_get mode)"
evidence="$(jq -nc --argjson fam "$results" --slurpfile lane "$GATE_STATE" \
  '{families:$fam, approvalLane:$lane[0]}')"

if [ "$any_fail" = true ]; then
  emit_scenario "$SCENARIO" fail \
    "a family did not complete its declared authoring lifecycle" "$evidence"
else
  emit_scenario "$SCENARIO" pass \
    "query+analysis authored through the ${GATE_MODE} lane to each family's declared publish boundary" \
    "$evidence"
fi
