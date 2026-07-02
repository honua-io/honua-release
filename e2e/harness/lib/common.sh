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
