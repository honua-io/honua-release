#!/usr/bin/env bash
# S4 — Console live suite. INVOKES honua-console's own live Playwright config against a stack this
# driver boots for the S4 stage only (e2e/harness/compose.console-s4.yml) — we do NOT rebuild it.
#
# The Console strips X-API-Key and forwards only a server-bound operator bearer (honua-console#403).
# Its live suite therefore signs in once through the governed OIDC exchange (honua-console#405) and
# needs, from this harness: a Keycloak realm on HONUA_CONSOLE_E2E_IDP_HOST, a honua-server whose
# PUBLIC base URL is the Console origin, and the source datasource inputs. The slice-1 server keeps
# its own public base URL (it builds the STAC/OGC links other drivers follow), so S4 gets a server
# of its own, booted from the same pinned image.
#
# Cost note: the live suite boots the Console app (dotnet run, .NET 10) plus a second server and an
# IdP. That is too heavy for the cheap per-PR tier, so S4 is OPT-IN: it runs when E2E_RUN_CONSOLE=1
# (nightly / require_real); otherwise it reports BLOCKED (honest) with the exact prerequisites —
# never a fabricated green.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS="$(cd "$HERE/../../harness" && pwd)"
# shellcheck source=../../harness/lib/common.sh
source "$HARNESS/lib/common.sh"

CONSOLE_DIR="${E2E_CONSOLE_DIR:-}"
CONSOLE_SHA="${E2E_CONSOLE_SHA:-<unpinned>}"
RUN="${E2E_RUN_CONSOLE:-}"
[ -n "$E2E_REQUIRE_REAL" ] && RUN=1

if [ -z "$RUN" ]; then
  emit_scenario "S4-console-studio" blocked \
    "opt-in: set E2E_RUN_CONSOLE=1 (needs a honua-console checkout carrying the governed operator session, .NET 10 SDK, Playwright, Docker and openssl)"
  exit 0
fi
if [ -z "$CONSOLE_DIR" ] || [ ! -d "$CONSOLE_DIR" ]; then
  emit_scenario "S4-console-studio" blocked "E2E_CONSOLE_DIR not set / not a honua-console checkout"; exit 0
fi
for tool in dotnet npx docker openssl jq curl; do
  if ! command -v "$tool" >/dev/null; then
    emit_scenario "S4-console-studio" blocked "toolchain unavailable: $tool"; exit 0
  fi
done
if [ -z "${HONUA_SERVER_IMAGE:-}" ]; then
  emit_scenario "S4-console-studio" blocked "HONUA_SERVER_IMAGE unset: run through e2e/harness/run_all.sh, which resolves the pin"; exit 0
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
# A Console without the governed sign-in (honua-console#405) runs its privileged specs with no
# operator, and its host refuses every one of them. That is not a missing dependency to wait out:
# the pinned Console cannot pass its own suite against a governed server.
if [ ! -f "$PW_DIR/live/operator-session.setup.ts" ]; then
  emit_scenario "S4-console-studio" fail \
    "honua-console@${CONSOLE_SHA} predates the governed operator session (honua-console#405): its live suite cannot sign in to a server-bound operator session"
  exit 0
fi

# --- S4 topology inputs ---------------------------------------------------------------------------
# Defaults are the ones honua-console#405 documents; each stays overridable through the same
# variable the suite itself reads, so the two sides cannot disagree.
LIVE_PORT="${HONUA_CONSOLE_E2E_LIVE_PORT:-5176}"
IDP_HOST="${HONUA_CONSOLE_E2E_IDP_HOST:-host.docker.internal:8443}"
IDP_HOSTNAME="${IDP_HOST%:*}"
IDP_PORT="${IDP_HOST##*:}"
if [ "$IDP_HOSTNAME" = "$IDP_HOST" ]; then IDP_PORT=443; fi
SERVER_PORT="${E2E_CONSOLE_SERVER_PORT:-8088}"
CONSOLE_ORIGIN="http://127.0.0.1:${LIVE_PORT}"
SERVER_URL="http://127.0.0.1:${SERVER_PORT}"
OPERATOR_USER="${HONUA_CONSOLE_E2E_OPERATOR_USER:-alice}"

mint() { openssl rand -hex "$1"; }
export S4_IDP_HOSTNAME="$IDP_HOSTNAME" S4_IDP_PORT="$IDP_PORT" S4_SERVER_PORT="$SERVER_PORT"
export S4_CONSOLE_ORIGIN="$CONSOLE_ORIGIN"
export S4_CLIENT_SECRET="${HONUA_CONSOLE_E2E_IDP_CLIENT_SECRET:-honua-console-bff-live-proof-secret}"
# Per-run secrets: none of these outlives the stack. The bearer key is 64 bytes of hex (>= 32).
export S4_OPERATOR_BEARER_KEY="$(mint 32)"
export S4_MASTER_KEY="$(mint 32)"
export S4_ADMIN_KEY="$(mint 24)"
export S4_IDP_ADMIN_PASSWORD="$(mint 16)"
OPERATOR_PASSWORD="${HONUA_CONSOLE_E2E_OPERATOR_PASSWORD:-$(mint 16)}"
export S4_RUN_DIR
S4_RUN_DIR="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/honua-console-s4.XXXXXX")"
chmod 0755 "$S4_RUN_DIR"

COMPOSE=(docker compose -f "$HARNESS/compose.console-s4.yml")
STACK_LOG="$E2E_OUT/console-s4-stack.log"
LOG="$E2E_OUT/console-live.log"
# Keep Playwright's traces/screenshots under $E2E_OUT (not buried in the external checkout) so CI can
# upload them alongside the log — a failure message pointing at an unreadable file is a dead end.
PW_ARTIFACTS="$E2E_OUT/console-playwright"
PW_RESULTS="$E2E_OUT/console-playwright-results.json"

# The stack log is uploaded; redact this run's secrets even though they die with the stack.
export S4_REDACT_OPERATOR_PASSWORD="$OPERATOR_PASSWORD"
redact() {
  python3 -c '
import os, sys
keys = ("S4_OPERATOR_BEARER_KEY", "S4_MASTER_KEY", "S4_ADMIN_KEY", "S4_IDP_ADMIN_PASSWORD", "S4_REDACT_OPERATOR_PASSWORD")
secrets = [os.environ[k] for k in keys if os.environ.get(k)]
for line in sys.stdin:
    for secret in secrets:
        line = line.replace(secret, "***")
    sys.stdout.write(line)
'
}
teardown() {
  "${COMPOSE[@]}" logs --no-color --timestamps db keycloak server 2>&1 | redact > "$STACK_LOG" || true
  "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$S4_RUN_DIR"
}
stack_fail() { # stage detail
  emit_scenario "S4-console-studio" fail "S4 stack did not come up ($1): $2 (see $STACK_LOG)"
  exit 0
}

port_free() { python3 -c 'import socket,sys;s=socket.socket();s.bind((sys.argv[1],int(sys.argv[2])));s.close()' "$1" "$2" 2>/dev/null; }
for p in "0.0.0.0:$IDP_PORT" "127.0.0.1:$SERVER_PORT" "127.0.0.1:$LIVE_PORT"; do
  if ! port_free "${p%:*}" "${p##*:}"; then
    rm -rf "$S4_RUN_DIR"
    emit_scenario "S4-console-studio" fail "S4 port ${p##*:} is already in use on this host (override HONUA_CONSOLE_E2E_IDP_HOST / E2E_CONSOLE_SERVER_PORT / HONUA_CONSOLE_E2E_LIVE_PORT)"
    exit 0
  fi
done

S4_IDP_HOSTNAME="$IDP_HOSTNAME" S4_CONSOLE_ORIGIN="$CONSOLE_ORIGIN" S4_CLIENT_SECRET="$S4_CLIENT_SECRET" \
  S4_OPERATOR_USER="$OPERATOR_USER" S4_OPERATOR_PASSWORD="$OPERATOR_PASSWORD" \
  bash "$HARNESS/keycloak/render.sh" "$S4_RUN_DIR"

trap teardown EXIT
# A stack left behind by an interrupted run would hand us an initialised database, whose first-run
# restart never happens, and stale realm state. Start from nothing.
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true

echo "== S4: booting IdP + PostGIS + Redis (server image $HONUA_SERVER_IMAGE) ==" >&2
"${COMPOSE[@]}" pull --quiet db redis keycloak server >/dev/null 2>&1 \
  || stack_fail "pull" "could not pull the S4 images"
"${COMPOSE[@]}" up -d db redis keycloak >/dev/null 2>&1 || stack_fail "up" "docker compose up failed for db/redis/keycloak"

# PostGIS runs its init scripts on a socket-only server, then stops it and starts the real one.
# pg_isready over the socket passes before that restart, and a server migrating then loses its
# connection mid-migrate (the schema floor does not repair a failed first migrate). So the server is
# CREATED only after the init-complete marker has been logged AND the final postmaster answers on
# TCP: its first migrate is against the final database.
db_final_ready() {
  "${COMPOSE[@]}" logs --no-color db 2>/dev/null | grep -q 'PostgreSQL init process complete; ready for start up' \
    && "${COMPOSE[@]}" exec -T db pg_isready -q -h 127.0.0.1 -p 5432 -U honua -d honua
}
deadline=$(( $(date +%s) + 180 ))
until db_final_ready; do
  [ "$(date +%s)" -lt "$deadline" ] || stack_fail "db" "PostGIS never completed first-run init and reopened on TCP within 180s"
  sleep 2
done

# The source table the suite publishes through the Console (services-layers / publishing-wizard).
# Same shape as honua-console's own testbed seed (e2e/initdb/01-seed.sql): integer PK, Polygon in
# EPSG:3857, exactly three features — the spec asserts all three come back in that SRID.
"${COMPOSE[@]}" exec -T db psql -q -v ON_ERROR_STOP=1 -U honua -d honua >/dev/null <<'SQL' || stack_fail "seed" "could not seed public.e2e_layer_src"
CREATE TABLE public.e2e_layer_src (
  id   integer PRIMARY KEY,
  name text    NOT NULL,
  geom geometry(Polygon, 3857) NOT NULL
);
INSERT INTO public.e2e_layer_src (id, name, geom) VALUES
 (1, 'alpha', ST_SetSRID(ST_MakeEnvelope(  0,   0, 100, 100, 3857), 3857)),
 (2, 'beta',  ST_SetSRID(ST_MakeEnvelope(200, 200, 300, 300, 3857), 3857)),
 (3, 'gamma', ST_SetSRID(ST_MakeEnvelope(400, 400, 500, 500, 3857), 3857));
SQL

# The realm is imported before Keycloak serves discovery, and the issuer must be the exact origin
# the server is configured with, or the code exchange fails on issuer validation.
ISSUER="https://${IDP_HOST}/realms/honua"
deadline=$(( $(date +%s) + 180 ))
until [ "$(curl -sk --max-time 5 --resolve "${IDP_HOSTNAME}:${IDP_PORT}:127.0.0.1" \
           "${ISSUER}/.well-known/openid-configuration" 2>/dev/null | jq -r '.issuer // empty' 2>/dev/null)" = "$ISSUER" ]; do
  [ "$(date +%s)" -lt "$deadline" ] || stack_fail "idp" "Keycloak never served realm honua with issuer $ISSUER within 180s"
  sleep 3
done

echo "== S4: starting honua-server against the final database ==" >&2
"${COMPOSE[@]}" up -d server >/dev/null 2>&1 || stack_fail "up" "docker compose up failed for server"
deadline=$(( $(date +%s) + ${E2E_BOOT_TIMEOUT:-180} ))
until [ "$(curl -sS -o /dev/null -w '%{http_code}' "${SERVER_URL}/healthz/ready" 2>/dev/null || echo 000)" = "200" ]; do
  [ "$(date +%s)" -lt "$deadline" ] || stack_fail "server" "honua-server never became ready at ${SERVER_URL}/healthz/ready"
  sleep 3
done
echo "== S4: stack ready (server $SERVER_URL, IdP $ISSUER, Console origin $CONSOLE_ORIGIN) ==" >&2

# --- run the suite ----------------------------------------------------------------------------------
# The suite reads these. HONUA_CONSOLE_E2E_ADMIN_KEY is the independent verifier's key
# (live/admin-api.ts calls the server directly); no service key ever reaches the Console host, so
# every key variable the host or the receipt producer could pick up is removed from the environment.
# E2E_PLAYWRIGHT_WITH_DEPS=0 skips the apt install for hosts without root (dependencies preinstalled).
WITH_DEPS=(--with-deps)
[ "${E2E_PLAYWRIGHT_WITH_DEPS:-1}" = 0 ] && WITH_DEPS=()
rm -f "$PW_RESULTS"
if (
  unset HONUA_ADMIN_API_KEY HONUA_ADMIN_KEY HONUA_API_KEY
  export HONUA_CONSOLE_E2E_SERVER_URL="$SERVER_URL"
  export HONUA_CONSOLE_E2E_ADMIN_KEY="$S4_ADMIN_KEY"
  export HONUA_CONSOLE_E2E_LIVE_PORT="$LIVE_PORT"
  export HONUA_CONSOLE_E2E_IDP_HOST="$IDP_HOST"
  export HONUA_CONSOLE_E2E_OPERATOR_USER="$OPERATOR_USER"
  export HONUA_CONSOLE_E2E_OPERATOR_PASSWORD="$OPERATOR_PASSWORD"
  # The source datasource is typed into the Console's connection form and dialled BY THE SERVER, so
  # it names the S4 compose service, not anything this shell can reach.
  export HONUA_CONSOLE_E2E_SOURCE_HOST="db"
  export HONUA_CONSOLE_E2E_SOURCE_PORT="5432"
  export HONUA_CONSOLE_E2E_SOURCE_DB="honua"
  export HONUA_CONSOLE_E2E_SOURCE_USER="honua"
  export HONUA_CONSOLE_E2E_SOURCE_PASSWORD="honua"
  export HONUA_CONSOLE_E2E_SOURCE_TABLE="public.e2e_layer_src"
  export PLAYWRIGHT_JSON_OUTPUT_NAME="$PW_RESULTS"
  cd "$PW_DIR"
  npm ci --silent && npx playwright install "${WITH_DEPS[@]}" chromium \
    && npx playwright test --config playwright.live.config.ts --reporter=list,json --output "$PW_ARTIFACTS"
) >"$LOG" 2>&1; then
  suite=0
else
  suite=$?
fi

# Counts from Playwright's own JSON report (expected = passed, unexpected = failed).
counts="null"
if [ -s "$PW_RESULTS" ]; then
  counts="$(jq -c '.stats | {passed: .expected, failed: .unexpected, flaky: .flaky, skipped: .skipped}' "$PW_RESULTS" 2>/dev/null || echo null)"
fi
summary="$(jq -r 'if . == null then "no Playwright report" else "\(.passed) passed, \(.failed) failed, \(.flaky) flaky, \(.skipped) skipped" end' <<<"$counts")"
evidence="$(jq -nc --argjson c "$counts" --arg sha "$CONSOLE_SHA" --arg image "$HONUA_SERVER_IMAGE" \
  '{consoleSha: $sha, serverImage: $image, playwright: $c}')"
if [ "$suite" = 0 ]; then
  emit_scenario "S4-console-studio" pass "honua-console live suite green as a governed operator ($summary)" "$evidence"
else
  emit_scenario "S4-console-studio" fail "honua-console live suite failed: $summary (see $LOG)" "$evidence"
fi
