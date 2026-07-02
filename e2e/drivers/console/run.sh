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

# Invoke the console repo's own live config, pointed at our seeded server.
export HONUA_CONSOLE_E2E_SERVER_URL="$E2E_BASE"
LOG="$E2E_OUT/console-live.log"
if ( cd "$CONSOLE_DIR" && npm ci --silent && npx playwright install --with-deps chromium \
       && npx playwright test --config e2e/playwright/playwright.live.config.ts ) >"$LOG" 2>&1; then
  emit_scenario "S4-console-studio" pass "honua-console live Studio suite green against seeded server"
else
  emit_scenario "S4-console-studio" fail "honua-console live Studio suite failed (see $LOG)"
fi
