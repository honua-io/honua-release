#!/usr/bin/env bash

# Dispatch one evidence producer, wait for the run created by that dispatch, and
# download exactly the artifact named by the producer contract. A successful run
# at the wrong workflow path or an artifact from a different run is not evidence.

set -euo pipefail

ID=""
REPO=""
WORKFLOW=""
WORKFLOW_PATH=""
WORKFLOW_NAME=""
REF="trunk"
ARTIFACT_TEMPLATE=""
RECEIPT_NAME=""
OUT_DIR=""
EXPECTED_HEAD_SHA=""
declare -a INPUTS=()

usage() {
  cat >&2 <<'EOF'
Usage: dispatch-and-collect.sh --id ID --repo OWNER/REPO --workflow FILE
  --workflow-path PATH       REST-reported workflow path (required)
  --workflow-name NAME       REST-reported workflow name (required)
  --artifact-name TEMPLATE   e.g. evidence-{run_id}-{run_attempt} (required)
  --receipt-name NAME        exact file expected in the artifact (required)
  --out-dir DIR              directory for the downloaded receipt (required)
  --ref REF                  workflow ref (default: trunk)
  --expected-head-sha SHA    exact producer commit expected in the created run (required)
  --input k=v                workflow_dispatch input (repeatable)
EOF
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --id) ID="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --workflow) WORKFLOW="$2"; shift 2 ;;
    --workflow-path) WORKFLOW_PATH="$2"; shift 2 ;;
    --workflow-name) WORKFLOW_NAME="$2"; shift 2 ;;
    --artifact-name) ARTIFACT_TEMPLATE="$2"; shift 2 ;;
    --receipt-name) RECEIPT_NAME="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --expected-head-sha) EXPECTED_HEAD_SHA="$2"; shift 2 ;;
    --input) INPUTS+=("$2"); shift 2 ;;
    -h|--help) usage ;;
    *) echo "[ERROR] unknown argument: $1" >&2; usage ;;
  esac
done

[[ -n "$ID" && -n "$REPO" && -n "$WORKFLOW" && -n "$WORKFLOW_PATH" && -n "$WORKFLOW_NAME" \
  && -n "$ARTIFACT_TEMPLATE" && -n "$RECEIPT_NAME" && -n "$OUT_DIR" && -n "$EXPECTED_HEAD_SHA" ]] || usage
[[ "$EXPECTED_HEAD_SHA" =~ ^[0-9a-f]{40}$ ]] || { echo "[ERROR] expected head SHA must be a full lowercase SHA" >&2; exit 1; }
command -v gh >/dev/null || { echo "[ERROR] gh is required" >&2; exit 1; }
command -v jq >/dev/null || { echo "[ERROR] jq is required" >&2; exit 1; }

# GitHub can transiently answer 403 while Actions metadata is being indexed.
# A 403 is retried with bounded sleep; other API failures remain fatal.
gh_retry() {
  local attempt=1 output rc
  while :; do
    set +e
    output="$(gh "$@" 2>&1)"
    rc=$?
    set -e
    if [[ "$rc" -eq 0 ]]; then
      printf '%s\n' "$output"
      return 0
    fi
    if grep -q '403' <<<"$output" && [[ "$attempt" -lt 6 ]]; then
      echo "[WARN] GitHub returned 403; sleeping before retry ${attempt}/5" >&2
      sleep $((attempt * 10))
      attempt=$((attempt + 1))
      continue
    fi
    printf '%s\n' "$output" >&2
    return "$rc"
  done
}

dispatch_args=(workflow run "$WORKFLOW" -R "$REPO" --ref "$REF")
for input in "${INPUTS[@]}"; do dispatch_args+=(-f "$input"); done
dispatch_response="$(gh_retry "${dispatch_args[@]}")"
run_id="$(grep -oE '/actions/runs/[0-9]+' <<< "$dispatch_response" | tail -n 1 | cut -d/ -f4 || true)"
[[ -n "$run_id" ]] || {
  echo "[ERROR] gh workflow run did not return the created run URL; refusing to guess from concurrent runs" >&2
  exit 1
}

set +e
gh_retry run watch "$run_id" -R "$REPO" --interval 10 --exit-status >&2
watch_rc=$?
set -e

run="$(gh_retry api "repos/$REPO/actions/runs/$run_id")"
conclusion="$(jq -r '.conclusion // ""' <<<"$run")"
[[ "$(jq -r '.event // ""' <<<"$run")" == "workflow_dispatch" ]] \
  || { echo "[ERROR] run $run_id was not created by workflow_dispatch" >&2; exit 1; }
[[ "$(jq -r '.head_sha // ""' <<<"$run")" == "$EXPECTED_HEAD_SHA" ]] \
  || { echo "[ERROR] run $run_id head SHA does not match expected producer pin $EXPECTED_HEAD_SHA" >&2; exit 1; }
[[ "$(jq -r '.path // ""' <<<"$run")" == "$WORKFLOW_PATH" ]] \
  || { echo "[ERROR] run $run_id reported an unexpected workflow path" >&2; exit 1; }
[[ "$(jq -r '.name // ""' <<<"$run")" == "$WORKFLOW_NAME" ]] \
  || { echo "[ERROR] run $run_id reported an unexpected workflow name" >&2; exit 1; }
[[ "$conclusion" == "success" && "$watch_rc" -eq 0 ]] \
  || { echo "[ERROR] evidence producer $ID concluded $conclusion" >&2; exit 1; }

attempt="$(jq -r '.run_attempt' <<<"$run")"
artifact_name="${ARTIFACT_TEMPLATE//\{run_id\}/$run_id}"
artifact_name="${artifact_name//\{run_attempt\}/$attempt}"
artifacts="$(gh_retry api "repos/$REPO/actions/runs/$run_id/artifacts?per_page=100")"
artifact="$(jq -cer --arg name "$artifact_name" --argjson run_id "$run_id" '
  .artifacts | map(select(.name == $name and .expired == false
    and .workflow_run.id == $run_id)) | if length == 1 then .[0] else empty end' <<<"$artifacts")" \
  || { echo "[ERROR] exact artifact $artifact_name was not uniquely produced by run $run_id" >&2; exit 1; }
artifact_id="$(jq -r '.id' <<<"$artifact")"

rm -rf -- "$OUT_DIR"
mkdir -p "$OUT_DIR"
gh_retry run download "$run_id" -R "$REPO" --name "$artifact_name" --dir "$OUT_DIR" >/dev/null
mapfile -t receipts < <(find "$OUT_DIR" -type f -name "$RECEIPT_NAME" -print)
[[ "${#receipts[@]}" -eq 1 ]] || { echo "[ERROR] artifact $artifact_name must contain exactly one $RECEIPT_NAME" >&2; exit 1; }
receipt="${receipts[0]}"
cp -- "$receipt" "$OUT_DIR/receipt.json"

jq -cn \
  --arg id "$ID" --arg repo "$REPO" --arg workflow "$WORKFLOW_PATH" \
  --arg name "$WORKFLOW_NAME" --arg run_id "$run_id" --arg attempt "$attempt" \
  --arg artifact_id "$artifact_id" --arg artifact_name "$artifact_name" \
  --arg url "$(jq -r '.html_url' <<<"$run")" --arg head_sha "$(jq -r '.head_sha' <<<"$run")" \
  --arg receipt "$OUT_DIR/receipt.json" \
  '{id:$id,repository:$repo,workflow:$workflow,workflowName:$name,runId:$run_id,
    runAttempt:$attempt,artifactId:$artifact_id,artifactName:$artifact_name,
    runUrl:$url,headSha:$head_sha,receipt:$receipt}' > "$OUT_DIR/metadata.json"

echo "[OK] $ID run=$run_id attempt=$attempt artifact=$artifact_id receipt=$OUT_DIR/receipt.json"
