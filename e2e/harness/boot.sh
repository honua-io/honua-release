#!/usr/bin/env bash
# Boot the Slice-1 candidate stack (server + PostGIS + Redis) and block until /healthz/ready is 200.
#
# The server image comes from platform-manifest.yaml (source of truth) unless HONUA_SERVER_IMAGE is
# set. If the image cannot be pulled/booted, this exits non-zero AND records the failure in
# $E2E_OUT/boot.json (reason, container exit code, first error lines). A candidate that never boots
# is a hard gate FAIL on every trigger - see lib/report.sh - because a green check on a stack that
# never served a request is a lie, not an honest BLOCKED (honua-release#303).
#
# Usage: boot.sh up | down | wait
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
COMPOSE_FILE="$HERE/compose.candidate.yml"
MANIFEST="$REPO_ROOT/platform-manifest.yaml"
COMPOSE=(docker compose -f "$COMPOSE_FILE")

E2E_BASE="${E2E_BASE:-http://localhost:${E2E_SERVER_PORT:-8080}}"
E2E_OUT="${E2E_OUT:-$REPO_ROOT/e2e/out}"
BOOT_STATUS_FILE="$E2E_OUT/boot.json"

# Bounded excerpt limits for the container log carried into the report/job summary.
BOOT_LOG_MAX_LINES="${E2E_BOOT_LOG_MAX_LINES:-12}"
BOOT_LOG_MAX_COLS="${E2E_BOOT_LOG_MAX_COLS:-300}"

# scrub - the boot excerpt is published in gate-report.json and the job summary, so redact the
# shapes a connection string or a header can leak before anything is written down.
scrub() {
  sed -E \
    -e 's#(://[^:/@[:space:]]+):[^@[:space:]]+@#\1:***@#g' \
    -e 's/([Aa]uthorization|AUTHORIZATION)([[:space:]]*[=:][[:space:]]*).*/\1\2***/' \
    -e 's/(([Pp]ass(word|wd)?|PASSWORD|[Ss]ecret|SECRET|[Tt]oken|TOKEN|[Aa]pi[-_]?[Kk]ey|API[-_]?KEY)[[:space:]]*[=:][[:space:]]*)[^[:space:];,"]+/\1***/g'
}

server_container_field() { # go-template-field -> value ("" when there is no container)
  local cid; cid="$("${COMPOSE[@]}" ps -aq server 2>/dev/null | head -1)"
  [ -n "$cid" ] || return 0
  docker inspect -f "$1" "$cid" 2>/dev/null || true
}

# The first error-ish lines of the server log: exception messages, not stack frames, bounded and
# scrubbed. This is what tells a reviewer WHY the candidate never bound a port.
server_error_lines() {
  "${COMPOSE[@]}" logs --no-color --tail 400 server 2>/dev/null \
    | grep -avE '\|[[:space:]]*(at [A-Za-z_]|--- End of)' \
    | grep -aiE 'error|exception|fail|fatal|crit|unhandled|panic|refused|denied' \
    | head -n "$BOOT_LOG_MAX_LINES" \
    | scrub \
    | cut -c1-"$BOOT_LOG_MAX_COLS"
}

write_boot_status() { # booted reason detail
  local booted="$1" reason="$2" detail="$3"
  mkdir -p "$E2E_OUT"
  local exit_code state lines=""
  exit_code="$(server_container_field '{{.State.ExitCode}}')"
  state="$(server_container_field '{{.State.Status}}')"
  [ "$booted" = "true" ] || lines="$(server_error_lines || true)"
  jq -n \
    --argjson booted "$booted" \
    --arg reason "$reason" \
    --arg detail "$detail" \
    --arg image "${HONUA_SERVER_IMAGE:-unknown}" \
    --arg base "$E2E_BASE" \
    --arg code "$exit_code" \
    --arg state "$state" \
    --arg lines "$lines" '
    {
      booted: $booted,
      reason: (if $reason == "" then null else $reason end),
      detail: (if $detail == "" then null else $detail end),
      image: $image,
      healthUrl: ($base + "/healthz/ready"),
      exitCode: (if ($code|test("^-?[0-9]+$")) then ($code|tonumber) else null end),
      containerState: (if $state == "" then null else $state end),
      errorLines: ($lines | split("\n") | map(select(length > 0)))
    }' > "$BOOT_STATUS_FILE"
}

record_boot_failure() { # reason detail
  write_boot_status false "$1" "$2"
  echo "::error:: candidate stack did not boot ($1): $2"
  jq -r '"  container: state=\(.containerState // "none") exitCode=\(.exitCode // "unknown")",
         (.errorLines[] | "  | " + .)' "$BOOT_STATUS_FILE" >&2 || true
}

resolve_image() {
  if [ -n "${HONUA_SERVER_IMAGE:-}" ]; then echo "$HONUA_SERVER_IMAGE"; return; fi
  # Read the pinned honua-server image and immutable index digest from the manifest
  # (READ ONLY - never edited here). Pulling by digest prevents a per-SHA tag from
  # resolving to a different manifest while a release train is running.
  if [ -f "$MANIFEST" ]; then
    awk '
      $0 ~ /^  honua-server:/ {inblk=1; next}
      inblk && $0 ~ /^  [a-zA-Z]/ {exit}
      inblk && $1=="image:" {gsub(/"/,"",$2); image=$2}
      inblk && $1=="digest:" {gsub(/"/,"",$2); digest=$2}
      END {
        if (image != "") {
          if (digest != "" && image !~ /@sha256:/) print image "@" digest
          else print image
        }
      }
    ' "$MANIFEST"
  fi
}

cmd_up() {
  local img; img="$(resolve_image)"
  [ -z "$img" ] && img="ghcr.io/honua-io/honua-server:nightly-aot"
  export HONUA_SERVER_IMAGE="$img"
  echo "== booting candidate server image: $HONUA_SERVER_IMAGE =="
  if ! "${COMPOSE[@]}" pull server 2>/dev/null; then
    record_boot_failure "image-pull-failed" \
      "could not pull $HONUA_SERVER_IMAGE (unauthenticated / placeholder pin)"
    return 3
  fi
  if ! "${COMPOSE[@]}" up -d; then
    record_boot_failure "compose-up-failed" "docker compose up -d failed for $HONUA_SERVER_IMAGE"
    return 4
  fi
  cmd_wait
}

cmd_wait() {
  local timeout="${E2E_BOOT_TIMEOUT:-180}" deadline
  deadline=$(( $(date +%s) + timeout ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if [ "$(curl -sS -o /dev/null -w '%{http_code}' "${E2E_BASE}/healthz/ready" 2>/dev/null || echo 000)" = "200" ]; then
      echo "== server READY at ${E2E_BASE} =="
      if ! python3 "$REPO_ROOT/e2e/licensing.py" --base-url "$E2E_BASE" \
          --output "$E2E_OUT/licensing.json"; then
        record_boot_failure "licensing-mode-mismatch" "candidate must report mode: disabled"
        return 1
      fi
      write_boot_status true "" ""
      return 0
    fi
    sleep 3
  done
  "${COMPOSE[@]}" logs server 2>&1 | tail -200 || true
  record_boot_failure "never-ready" \
    "server never became ready at ${E2E_BASE}/healthz/ready within ${timeout}s"
  return 1
}

cmd_down() { "${COMPOSE[@]}" down -v 2>/dev/null || true; }

case "${1:-up}" in
  up)   cmd_up ;;
  wait) cmd_wait ;;
  down) cmd_down ;;
  *) echo "usage: boot.sh up|down|wait" >&2; exit 2 ;;
esac
