#!/usr/bin/env bash
# S9 — the five driveable honua-site demos, headless-driven against the seeded candidate server.
#
#   two-protocols · esri-leaflet · geoprocessing · editing · analyst-workbench
#   (maui-3d and sdk-controls are out of scope by design — they are SDK-native, not backend demos.)
#
# HOW: a local static copy of honua-site is served on its own origin (a DIFFERENT origin from the
# candidate, exactly as honua.io -> demo.honua.io is in production) and each demo is opened with
# honua-site's backend-override shim engaged:
#
#   http://127.0.0.1:$E2E_SITE_PORT/demo-two-protocols.html?apiBase=$E2E_BASE
#
# Nothing is stubbed: no route interception, no injected fixtures. The demos run their own code
# against the candidate and the assertions are functional — matching cross-protocol counts, rendered
# features, a completed GP job with a result document, a live click-query popup, an edit applied and
# visible in the DOM. See drive.mjs for the per-demo checks.
#
# Honesty (AGENTS.md): a missing precondition (server not ready, no seed, no Node/Playwright, a
# honua-site checkout without the shim) is BLOCKED — and BLOCKED is promoted to FAIL under
# E2E_REQUIRE_REAL, so S9 is a real gate on a real cut. A reachable demo that does not work is FAIL.
# There is no opt-in switch and no way to make S9 unfailable.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
E2E_DIR="$(cd "$HERE/../.." && pwd)"
REPO_ROOT="$(cd "$E2E_DIR/.." && pwd)"
# shellcheck source=../../harness/lib/common.sh
source "$HERE/../../harness/lib/common.sh"

DEMOS=(shim-security two-protocols esri-leaflet geoprocessing editing analyst-workbench)
block_all() { for d in "${DEMOS[@]}"; do emit_scenario "S9-demos-$d" blocked "$1"; done; }

# ── preconditions ──────────────────────────────────────────────────────────────────────────────
if ! server_ready; then
  block_all "server not ready at $E2E_BASE"; exit 0
fi
if [ ! -f "$E2E_OUT/seed-manifest.json" ]; then
  block_all "seed manifest missing — the demo layer contract was never published"; exit 0
fi
if ! command -v node >/dev/null || ! command -v python3 >/dev/null; then
  block_all "node and python3 are required to serve + drive the demos"; exit 0
fi

# ── honua-site checkout (the demos are the thing under test; we never vendor a copy) ───────────
SITE_DIR="${E2E_SITE_DIR:-}"
if [ -z "$SITE_DIR" ] && [ -f "$REPO_ROOT/../honua-site/demo-two-protocols.html" ]; then
  SITE_DIR="$(cd "$REPO_ROOT/../honua-site" && pwd)"
fi
if [ -z "$SITE_DIR" ]; then
  SITE_DIR="$E2E_OUT/honua-site"
  echo "== S9: cloning honua-site@${E2E_SITE_REF:-trunk} =="
  rm -rf "$SITE_DIR"
  git clone --quiet --depth 1 --branch "${E2E_SITE_REF:-trunk}" \
    "${E2E_SITE_REPO:-https://github.com/honua-io/honua-site.git}" "$SITE_DIR" || {
      block_all "could not obtain a honua-site checkout (set E2E_SITE_DIR to one)"; exit 0; }
fi
if [ ! -f "$SITE_DIR/demo-two-protocols.html" ]; then
  block_all "E2E_SITE_DIR=$SITE_DIR is not a honua-site checkout"; exit 0
fi
if [ ! -f "$SITE_DIR/assets/demos/backend-override.js" ]; then
  block_all "honua-site checkout at $SITE_DIR predates the backend-override shim (assets/demos/backend-override.js) — the demos are still pinned to demo.honua.io by their CSP"
  exit 0
fi

# ── Playwright (cached outside the repo; CI warms the same cache) ──────────────────────────────
PW_HOME="${E2E_PW_HOME:-${XDG_CACHE_HOME:-$HOME/.cache}/honua-e2e-playwright}"
PW_VERSION="${E2E_PLAYWRIGHT_VERSION:-1.61.1}"
mkdir -p "$PW_HOME"
[ -f "$PW_HOME/package.json" ] || echo '{"name":"honua-e2e-playwright","private":true}' > "$PW_HOME/package.json"
if ! node -e "require('$PW_HOME/node_modules/playwright')" >/dev/null 2>&1; then
  echo "== S9: installing playwright@$PW_VERSION into $PW_HOME =="
  ( cd "$PW_HOME" && npm install --silent --no-audit --no-fund "playwright@$PW_VERSION" ) \
    || { block_all "could not install playwright@$PW_VERSION (no network / npm unavailable)"; exit 0; }
fi
if ! node -e "
  const { chromium } = require('$PW_HOME/node_modules/playwright');
  process.exit(require('node:fs').existsSync(chromium.executablePath()) ? 0 : 1);
" >/dev/null 2>&1; then
  echo "== S9: downloading the chromium build playwright@$PW_VERSION expects =="
  ( cd "$PW_HOME" && node node_modules/playwright/cli.js install chromium ) \
    || { block_all "playwright chromium is unavailable (run: npx playwright@$PW_VERSION install chromium)"; exit 0; }
fi

# ── serve honua-site on its own origin ─────────────────────────────────────────────────────────
# The candidate must allow this origin: compose passes $E2E_SITE_CORS_ORIGINS through to
# HONUA_ADMIN_UI_CORS_ORIGINS. run_all.sh picks the port before booting so the two agree.
SITE_PORT="${E2E_SITE_PORT:-18099}"
SITE_LOG="$E2E_OUT/demos-site.log"
python3 -m http.server "$SITE_PORT" --bind 127.0.0.1 --directory "$SITE_DIR" >"$SITE_LOG" 2>&1 &
SITE_PID=$!
cleanup() { kill "$SITE_PID" 2>/dev/null || true; wait "$SITE_PID" 2>/dev/null || true; }
trap cleanup EXIT

SITE_URL="http://127.0.0.1:$SITE_PORT/"
for _ in $(seq 1 40); do
  [ "$(curl -sS -o /dev/null -w '%{http_code}' "$SITE_URL/demo-two-protocols.html" 2>/dev/null || echo 000)" = "200" ] && break
  sleep 0.25
done
if [ "$(curl -sS -o /dev/null -w '%{http_code}' "$SITE_URL/demo-two-protocols.html" 2>/dev/null || echo 000)" != "200" ]; then
  block_all "the local honua-site static server never came up on $SITE_URL (see $SITE_LOG)"; exit 0
fi

# The demos are cross-origin to the candidate; without an allow-listed CORS origin every request is
# refused by the server and the demos would fail on transport rather than on contract.
CORS_ORIGIN="$(curl -sS -o /dev/null -D - -H "Origin: http://127.0.0.1:$SITE_PORT" \
  "$E2E_BASE/rest/services?f=json" 2>/dev/null | tr -d '\r' \
  | awk -F': ' 'tolower($1)=="access-control-allow-origin"{print $2}' | head -1)"
if [ -z "$CORS_ORIGIN" ]; then
  block_all "the candidate does not allow the demo origin http://127.0.0.1:$SITE_PORT (set E2E_SITE_CORS_ORIGINS before booting)"
  exit 0
fi

# ── drive ──────────────────────────────────────────────────────────────────────────────────────
rm -f "$E2E_OUT/demos-results.json"
export E2E_PW_MODULE="$PW_HOME/node_modules/playwright/index.js"
export E2E_SITE_URL="$SITE_URL"
DRIVE_LOG="$E2E_OUT/demos-drive.log"
node "$HERE/drive.mjs" >"$DRIVE_LOG" 2>&1 || true
sed -n '1,200p' "$DRIVE_LOG" >&2 || true

if [ ! -s "$E2E_OUT/demos-results.json" ]; then
  for d in "${DEMOS[@]}"; do
    emit_scenario "S9-demos-$d" fail "the demo driver produced no verdict (see $DRIVE_LOG)"
  done
  exit 0
fi

for d in "${DEMOS[@]}"; do
  row="$(jq -c --arg id "$d" '.[] | select(.id == $id)' "$E2E_OUT/demos-results.json" 2>/dev/null | head -1)"
  if [ -z "$row" ]; then
    emit_scenario "S9-demos-$d" fail "the demo driver never reported on this demo (see $DRIVE_LOG)"
    continue
  fi
  emit_scenario "S9-demos-$d" \
    "$(printf '%s' "$row" | jq -r '.status')" \
    "$(printf '%s' "$row" | jq -r '.why')" \
    "$(printf '%s' "$row" | jq -c '.evidence')"
done
