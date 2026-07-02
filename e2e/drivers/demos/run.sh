#!/usr/bin/env bash
# S9 — honua-site demos headless-driven against the seeded server.
#
# DEFERRED to a tight fast-follow (see e2e/drivers/demos/FAST-FOLLOW.md). The honua-site demos
# hardcode https://demo.honua.io and are locked to that origin by a page CSP connect-src, and they
# assert a much larger data contract (maui-parcels, 6-layer zoning, inspections, imagery). Driving
# them needs either a cross-repo backend-override shim in honua-site OR a Playwright page.route
# redirect, plus a substantially larger seed — both out of scope for this honua-release-only slice.
# This driver reports BLOCKED honestly with the plan rather than fabricating a green.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../harness/lib/common.sh
source "$HERE/../../harness/lib/common.sh"

emit_scenario "S9-demos" blocked \
  "deferred to fast-follow: honua-site demos hardcode demo.honua.io behind a CSP connect-src; need a backend-override shim (window.HONUA_DEMO_BASE_URL / ?apiBase) or Playwright page.route redirect + a larger seed. See e2e/drivers/demos/FAST-FOLLOW.md"
