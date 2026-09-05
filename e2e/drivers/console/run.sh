#!/usr/bin/env bash
# S4 — Console live AI-Studio suite. INVOKES honua-console's own live Playwright config
# (studio-results.live.spec.ts) against the seeded server — we do NOT rebuild it.
#
# Cost note: the live suite boots the Console app (dotnet run, .NET 10) and, for the QUERY/MAP/FORM/
# ANALYSIS flows, needs a local CPU inference model; only the WORKFLOW flow uses the deterministic
# provider. That is too heavy/flaky for the cheap per-PR tier, so S4 is OPT-IN: it runs when
# E2E_RUN_CONSOLE=1 (nightly / require_real) or when the toolchain + checkout are already present;
# otherwise it reports BLOCKED (honest) with the exact prerequisites — never a fabricated green.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../harness/lib/common.sh
source "$HERE/../../harness/lib/common.sh"

CONSOLE_DIR="${E2E_CONSOLE_DIR:-}"
CONSOLE_SHA="${E2E_CONSOLE_SHA:-f53e5eb}"   # honua-console HEAD carrying the live Studio suite
RUN="${E2E_RUN_CONSOLE:-}"
[ -n "$E2E_REQUIRE_REAL" ] && RUN=1

if ! server_ready; then
  emit_scenario "S4-console-studio" blocked "server not ready at $E2E_BASE"; exit 0
fi
if [ -z "$RUN" ]; then
  emit_scenario "S4-console-studio" blocked \
    "opt-in: set E2E_RUN_CONSOLE=1 (needs honua-console@${CONSOLE_SHA} checkout, .NET 10 SDK, Playwright, and a CPU model for non-WORKFLOW flows)"
  exit 0
fi
if [ -z "$CONSOLE_DIR" ] || [ ! -d "$CONSOLE_DIR" ]; then
  emit_scenario "S4-console-studio" blocked "E2E_CONSOLE_DIR not set / not a honua-console checkout"; exit 0
fi
if ! command -v dotnet >/dev/null || ! command -v npx >/dev/null; then
  emit_scenario "S4-console-studio" blocked "dotnet (.NET 10) and/or Playwright (npx) toolchain unavailable"; exit 0
fi

# The console's Playwright deps live in e2e/playwright (its own package.json pins @playwright/test),
# NOT in the console repo root — the root package.json declares no dependencies at all, it only
# shells out (`npm run e2e:live` -> e2e/run-live.mjs -> playwright with cwd=e2e/playwright). Running
# `npm ci` at the root therefore installs nothing, and the subsequent `npx playwright` resolves a
# floating playwright from the npx cache that cannot import the config's `@playwright/test`:
#     Error: Cannot find package '@playwright/test' imported from .../playwright.live.config.ts
# So install and invoke from e2e/playwright, exactly like the console's own runner does. The config
# resolves its webServer cwd from its own URL (`../../` = console repo root), so the Console host is
# still built and booted from the repo root regardless of where we invoke Playwright.
PW_DIR="$CONSOLE_DIR/e2e/playwright"
if [ ! -f "$PW_DIR/package.json" ]; then
  emit_scenario "S4-console-studio" blocked \
    "console checkout has no e2e/playwright/package.json (unexpected layout at ${CONSOLE_SHA})"; exit 0
fi

export HONUA_CONSOLE_E2E_SERVER_URL="$E2E_BASE"
# The suite talks to the admin API directly (live/admin-api.ts) and defaults to the same dev key the
# candidate stack is booted with. Pass the harness's key explicitly so overriding E2E_API_KEY does
# not silently desynchronise the two sides.
export HONUA_CONSOLE_E2E_ADMIN_KEY="$E2E_API_KEY"

# The live suite publishes a layer out of a REAL source datasource, typing its host/port/credentials
# into the console's connection form — so they must resolve FROM INSIDE the server container, not
# from this shell. honua-console's own testbed puts PostGIS on localhost:5544 (the server shares its
# network namespace); our candidate stack runs it as the compose service `db` on 5432. Point the
# suite at ours; seed/seed.sh creates public.e2e_layer_src there for exactly this purpose.
export HONUA_CONSOLE_E2E_SOURCE_HOST="${E2E_DB_HOST:-db}"
export HONUA_CONSOLE_E2E_SOURCE_PORT="5432"
export HONUA_CONSOLE_E2E_SOURCE_DB="honua"
export HONUA_CONSOLE_E2E_SOURCE_USER="honua"
export HONUA_CONSOLE_E2E_SOURCE_PASSWORD="honua"
export HONUA_CONSOLE_E2E_SOURCE_TABLE="public.e2e_layer_src"

LOG="$E2E_OUT/console-live.log"
# Keep Playwright's traces/screenshots under $E2E_OUT (not buried in the external checkout) so CI can
# upload them alongside the log — a failure message pointing at an unreadable file is a dead end.
PW_ARTIFACTS="$E2E_OUT/console-playwright"
if ( cd "$PW_DIR" && npm ci --silent && npx playwright install --with-deps chromium \
       && npx playwright test --config playwright.live.config.ts --output "$PW_ARTIFACTS" ) >"$LOG" 2>&1; then
  emit_scenario "S4-console-studio" pass "honua-console live Studio suite green against seeded server"
else
  emit_scenario "S4-console-studio" fail "honua-console live Studio suite failed (see $LOG)"
fi
