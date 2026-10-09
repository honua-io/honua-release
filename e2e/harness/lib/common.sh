# shellcheck shell=bash
# Shared helpers for the Slice-1 local-docker e2e drivers.
#
# Every driver sources this. It provides:
#   - the server base URL / admin API key (from env, with sane local defaults)
#   - curl wrappers that carry the admin key and capture status + body
#   - result emitters that append one JSON object per row to the shared fragment files
#     ($E2E_OUT/scenarios.jsonl | protocols.jsonl | formats.jsonl) which lib/report.sh merges
#     into gate-report.json.
#
# Honesty (AGENTS.md): a driver NEVER fabricates a pass. When the server is unreachable a driver
# emits `blocked` (real green requires a real server); a genuine contract break emits `fail`.
set -euo pipefail

E2E_BASE="${E2E_BASE:-http://localhost:8080}"
E2E_API_KEY="${E2E_API_KEY:-honua-console-dev-key}"
E2E_OUT="${E2E_OUT:-$(pwd)/out}"
E2E_REQUIRE_REAL="${E2E_REQUIRE_REAL:-}"

mkdir -p "$E2E_OUT"

# --- HTTP -----------------------------------------------------------------------------------------
# api_get PATH            -> sets HTTP_CODE, HTTP_BODY
# api_post PATH JSON      -> sets HTTP_CODE, HTTP_BODY
# api_json METHOD PATH JSON [extra curl args...]
api_json() {
  local method="$1" path="$2" data="${3:-}"; shift $(( $# >= 3 ? 3 : 2 ))
  local url="${E2E_BASE}${path}"
  local tmp; tmp="$(mktemp)"
  if [ -n "$data" ]; then
    HTTP_CODE="$(curl -sS -o "$tmp" -w '%{http_code}' -X "$method" "$url" \
      -H "Content-Type: application/json" -H "X-API-Key: ${E2E_API_KEY}" \
      --data "$data" "$@" || echo 000)"
  else
    HTTP_CODE="$(curl -sS -o "$tmp" -w '%{http_code}' -X "$method" "$url" \
      -H "X-API-Key: ${E2E_API_KEY}" "$@" || echo 000)"
  fi
  HTTP_BODY="$(cat "$tmp")"; rm -f "$tmp"
}
api_get()  { api_json GET  "$1" ""; }
api_post() { api_json POST "$1" "${2:-}"; }

# jget EXPR  — read a value from $HTTP_BODY via jq, empty string on any error.
jget() { printf '%s' "$HTTP_BODY" | jq -r "$1" 2>/dev/null || true; }

# --- result emitters ------------------------------------------------------------------------------
# All statuses are one of: pass | fail | blocked | skipped | na  (na only for format write/roundtrip)
emit_scenario() { # id status why [evidence-json]
  jq -nc --arg s "$1" --arg st "$2" --arg why "${3:-}" --argjson ev "${4:-null}" \
    '{scenario:$s, status:$st, why:$why, evidence:$ev}' >> "$E2E_OUT/scenarios.jsonl"
  printf '  [%-7s] %s: %s\n' "$(echo "$2" | tr a-z A-Z)" "$1" "${3:-}" >&2
}
emit_protocol() { # protocol operation status detail
  jq -nc --arg p "$1" --arg op "$2" --arg st "$3" --arg d "${4:-}" \
    '{protocol:$p, operation:$op, status:$st, detail:$d}' >> "$E2E_OUT/protocols.jsonl"
}
emit_format() { # format read write roundtrip notes
  jq -nc --arg f "$1" --arg r "$2" --arg w "$3" --arg rt "$4" --arg n "${5:-}" \
    '{format:$f, read:$r, write:$w, roundtrip:$rt, notes:$n}' >> "$E2E_OUT/formats.jsonl"
  printf '  [fmt] %-12s read=%-7s write=%-4s roundtrip=%-4s %s\n' "$1" "$2" "$3" "$4" "${5:-}" >&2
}

# server_ready — 0 if /healthz/ready returns 200, else non-zero.
server_ready() {
  local code; code="$(curl -sS -o /dev/null -w '%{http_code}' "${E2E_BASE}/healthz/ready" 2>/dev/null || echo 000)"
  [ "$code" = "200" ]
}

# --- topology (Redis-on vs Redis-off) -------------------------------------------------------------
# A Redis-off install is a different topology, not a broken one: honua-server composes the operation
# proposal store, its gateway and the admin executors only inside the Redis block (Program.cs:628,
# `connectedRedis != null && redisCacheEntitled`), so on Redis-off the 20 projected
# `honua_admin_layer_*`/`honua_admin_services_*` tools are not registered and every Studio draft
# mutation is refused with the typed "requires a Redis-backed durable store" conflict. Drivers that
# certify those surfaces must know which topology they face -- and must not take the cell's word for
# it, nor the server's word alone: the CELL declares its topology (E2E_REDIS, default `on` like the
# S5 driver) and the SERVER must confirm it.
#
# detect_topology -> sets TOPOLOGY (redis-on|redis-off|unknown), TOPOLOGY_SIGNAL, TOPOLOGY_REASON.
# Signals, in order of preference (the first that answers decides):
#   1. manifest capability `operations.proposals` (honua-server S1 adds it): available=true => on,
#      available=false => off, reasonCode recorded.
#   2. GET /api/v1/admin/proposals: 200 => on; 503 typed capability-unavailable => off (its `code`
#      is the reason, `missingDependency`/`missingEntitlement` names the cause). Read-only.
#   3. manifest capability `jobs.runner` (same Redis gate, CapabilityManifestService
#      `requiresDurableJobStore`): available=true => on; available=false with reasonCode
#      dependency-unavailable|license-required => off. Any other reason says nothing about Redis.
# Nothing here degrades to redis-off on an unreadable answer: unreadable is `unknown`, and `unknown`
# never earns a relaxed expectation.
detect_topology() {
  TOPOLOGY=unknown; TOPOLOGY_SIGNAL=none; TOPOLOGY_REASON=""
  local manifest="" manifest_code cap avail reason code typ dep
  api_get "/api/v1/capabilities/manifest"
  manifest_code="$HTTP_CODE"
  [ "$HTTP_CODE" = "200" ] && manifest="$HTTP_BODY"

  cap="$(printf '%s' "$manifest" | jq -c '[.capabilities[]? | select(.id=="operations.proposals")] | first // empty' 2>/dev/null || true)"
  if [ -n "$cap" ]; then
    avail="$(printf '%s' "$cap" | jq -r '.available' 2>/dev/null || true)"
    reason="$(printf '%s' "$cap" | jq -r '.reasonCode // ""' 2>/dev/null || true)"
    TOPOLOGY_SIGNAL="manifest:operations.proposals"
    case "$avail" in
      true)  TOPOLOGY=redis-on ;;
      false) TOPOLOGY=redis-off; TOPOLOGY_REASON="${reason:-unspecified}" ;;
    esac
    [ "$TOPOLOGY" != unknown ] && return 0
  fi

  api_get "/api/v1/admin/proposals"
  local proposals_code="$HTTP_CODE"
  if [ "$HTTP_CODE" = "200" ]; then
    TOPOLOGY=redis-on; TOPOLOGY_SIGNAL="admin-proposals:200"; return 0
  fi
  if [ "$HTTP_CODE" = "503" ]; then
    typ="$(jget '.type // ""')"; code="$(jget '.code // ""')"
    dep="$(jget '.missingDependency // .missingEntitlement // ""')"
    if [ "$typ" = "https://honua.io/problems/capability-unavailable" ] && [ -n "$code" ]; then
      TOPOLOGY=redis-off; TOPOLOGY_SIGNAL="admin-proposals:503"
      TOPOLOGY_REASON="$code${dep:+ (missing $dep)}"; return 0
    fi
  fi

  cap="$(printf '%s' "$manifest" | jq -c '[.capabilities[]? | select(.id=="jobs.runner")] | first // empty' 2>/dev/null || true)"
  if [ -n "$cap" ]; then
    avail="$(printf '%s' "$cap" | jq -r '.available' 2>/dev/null || true)"
    reason="$(printf '%s' "$cap" | jq -r '.reasonCode // ""' 2>/dev/null || true)"
    if [ "$avail" = true ]; then
      TOPOLOGY=redis-on; TOPOLOGY_SIGNAL="manifest:jobs.runner"; return 0
    fi
    if [ "$avail" = false ] && { [ "$reason" = dependency-unavailable ] || [ "$reason" = license-required ]; }; then
      TOPOLOGY=redis-off; TOPOLOGY_SIGNAL="manifest:jobs.runner"; TOPOLOGY_REASON="$reason"; return 0
    fi
    TOPOLOGY_REASON="jobs.runner available=$avail reasonCode=${reason:-none} does not identify the topology"
  else
    TOPOLOGY_REASON="no topology signal answered (manifest HTTP $manifest_code, admin/proposals HTTP $proposals_code)"
  fi
}

# resolve_topology -> runs detect_topology, then sets TOPOLOGY_DECLARED (E2E_REDIS, default on) and
# TOPOLOGY_MISMATCH (empty when the server confirms the declared topology, else the reason).
#   declared on  + detected redis-off      -> mismatch: a Redis-on cell lost its control plane
#   declared off + detected redis-on       -> mismatch: the cell is not the topology it claims
#   declared off + detected unknown        -> mismatch: a relaxed expectation needs the server's word
#   declared on  + detected on|unknown     -> no mismatch; the caller applies the FULL expectation
# Only `declared off + detected redis-off` licenses a Redis-off expectation.
resolve_topology() {
  detect_topology
  TOPOLOGY_DECLARED="${E2E_REDIS:-on}"
  TOPOLOGY_MISMATCH=""
  case "$TOPOLOGY_DECLARED:$TOPOLOGY" in
    on:redis-off)  TOPOLOGY_MISMATCH="cell declares Redis on but the server reports no durable control plane ($TOPOLOGY_SIGNAL: $TOPOLOGY_REASON)" ;;
    off:redis-on)  TOPOLOGY_MISMATCH="cell declares Redis off but the server reports a durable control plane ($TOPOLOGY_SIGNAL)" ;;
    off:unknown)   TOPOLOGY_MISMATCH="cell declares Redis off but the server did not confirm it: $TOPOLOGY_REASON" ;;
  esac
}

# topology_evidence -> one JSON object describing the resolved topology, for scenario evidence.
topology_evidence() {
  jq -nc --arg t "$TOPOLOGY" --arg d "${TOPOLOGY_DECLARED:-}" --arg s "$TOPOLOGY_SIGNAL" \
    --arg r "$TOPOLOGY_REASON" --arg m "${TOPOLOGY_MISMATCH:-}" \
    '{topology:$t, declared:$d, signal:$s,
      reasonCode:(if $r == "" then null else $r end),
      mismatch:(if $m == "" then null else $m end)}'
}
