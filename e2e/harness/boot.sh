#!/usr/bin/env bash
# Boot the Slice-1 candidate stack (server + PostGIS + Redis) and block until /healthz/ready is 200.
#
# The server image comes from platform-manifest.yaml (source of truth) unless HONUA_SERVER_IMAGE is
# set. If the image cannot be pulled/booted, this exits non-zero and the orchestrator marks the
# server-dependent scenarios BLOCKED (honest) rather than fabricating a green.
#
# Usage: boot.sh up | down | wait
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
COMPOSE_FILE="$HERE/compose.candidate.yml"
MANIFEST="$REPO_ROOT/platform-manifest.yaml"
COMPOSE=(docker compose -f "$COMPOSE_FILE")

E2E_BASE="${E2E_BASE:-http://localhost:${E2E_SERVER_PORT:-8080}}"

resolve_image() {
  if [ -n "${HONUA_SERVER_IMAGE:-}" ]; then echo "$HONUA_SERVER_IMAGE"; return; fi
  # Read the pinned honua-server image from the manifest (READ ONLY — never edited here).
  if [ -f "$MANIFEST" ]; then
    awk '
      $0 ~ /^  honua-server:/ {inblk=1; next}
      inblk && $0 ~ /^  [a-zA-Z]/ {inblk=0}
      inblk && $1=="image:" {gsub(/"/,"",$2); print $2; exit}
    ' "$MANIFEST"
  fi
}

cmd_up() {
  local img; img="$(resolve_image)"
  [ -z "$img" ] && img="ghcr.io/honua-io/honua-server:nightly-aot"
  export HONUA_SERVER_IMAGE="$img"
  echo "== booting candidate server image: $HONUA_SERVER_IMAGE =="
  if ! "${COMPOSE[@]}" pull server 2>/dev/null; then
    echo "::warning:: could not pull $HONUA_SERVER_IMAGE (unauthenticated / placeholder pin)"
    return 3
  fi
  "${COMPOSE[@]}" up -d
  cmd_wait
}

cmd_wait() {
  local timeout="${E2E_BOOT_TIMEOUT:-180}" deadline
  deadline=$(( $(date +%s) + timeout ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if [ "$(curl -sS -o /dev/null -w '%{http_code}' "${E2E_BASE}/healthz/ready" 2>/dev/null || echo 000)" = "200" ]; then
      echo "== server READY at ${E2E_BASE} =="; return 0
    fi
    sleep 3
  done
  echo "::error:: server never became ready at ${E2E_BASE}/healthz/ready within ${timeout}s"
  "${COMPOSE[@]}" logs server 2>&1 | tail -40 || true
  return 1
}

cmd_down() { "${COMPOSE[@]}" down -v 2>/dev/null || true; }

case "${1:-up}" in
  up)   cmd_up ;;
  wait) cmd_wait ;;
  down) cmd_down ;;
  *) echo "usage: boot.sh up|down|wait" >&2; exit 2 ;;
esac
