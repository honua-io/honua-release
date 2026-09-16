#!/usr/bin/env bash
# Upgrade chaos driver for the packet-94 release harness.
#
# This is intentionally a hunt driver, not a recovery implementation.  It runs the real
# prior/candidate server images against the real PostGIS service from compose.candidate.yml and
# exits non-zero whenever it cannot prove convergence, rollback compatibility, lock serialization,
# transactional partial-failure behavior, fail-closed divergence handling, or idempotency.
#
# Required inputs:
#   HONUA_PRIOR_SERVER_IMAGE       image that owns the seeded starting schema
#   HONUA_CANDIDATE_SERVER_IMAGE   image under test
#
# Optional inputs:
#   CHAOS_SCENARIO                 all (default), or one named scenario
#   E2E_API_KEY                    admin key used by the existing seeder
#   E2E_OUT                        evidence directory (default: ./out/upgrade-chaos)
#   E2E_COMPOSE_FILE               compose file (default: compose.candidate.yml)
#   CHAOS_BOUNDARY_LOG_PATTERN     printf-style grep pattern; default matches DbUp's
#                                  "Executing Database Server script '<name>'" log line
#   CHAOS_SKIP_PREPARE             true reuses an existing evidence directory for focused reruns
#   CHAOS_KEEP_STACK               true keeps the compose stack after the run
#
# Scenarios:
#   migration-kill-every-boundary, image-rollback, concurrent-app-start,
#   partial-migration-failure, journal-schema-divergence, migration-rerun-idempotency
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
COMPOSE_FILE="${E2E_COMPOSE_FILE:-$HERE/compose.candidate.yml}"
COMPOSE=(docker compose -f "$COMPOSE_FILE")
PROJECT="${COMPOSE_PROJECT_NAME:-honua-upgrade-chaos}"
OUT="${E2E_OUT:-$REPO_ROOT/out/upgrade-chaos}"
SCENARIO="${CHAOS_SCENARIO:-all}"
API_KEY="${E2E_API_KEY:-honua-console-dev-key}"
PRIOR_IMAGE="${HONUA_PRIOR_SERVER_IMAGE:-}"
CANDIDATE_IMAGE="${HONUA_CANDIDATE_SERVER_IMAGE:-${HONUA_SERVER_IMAGE:-}}"
BOUNDARY_LOG_PATTERN="${CHAOS_BOUNDARY_LOG_PATTERN:-Executing Database Server script}"
LOCK_KEY="${MIGRATION_LOCK_KEY:-8044282257919950151}"
KEEP_STACK="${CHAOS_KEEP_STACK:-false}"
SKIP_PREPARE="${CHAOS_SKIP_PREPARE:-false}"

BASELINE_DUMP="$OUT/baseline.sql"
SEED_DIR="$OUT/seed"
EXPECTED_JOURNAL="$OUT/expected-journal.txt"
EXPECTED_CHECKSUMS="$OUT/expected-checksums.txt"
RESULTS="$OUT/scenario-matrix.json"
LOCK_HOLDER_PID=""
PARTIAL_JOB_PID=""
PARTIAL_BACKEND_PID=""
SRC_LAYER_ID=""

mkdir -p "$OUT"
printf '{"scenarios":[]}\n' > "$RESULTS"

die() {
  echo "::error::$*" >&2
  exit 2
}

require_tools() {
  local tool
  for tool in docker curl jq; do
    command -v "$tool" >/dev/null 2>&1 || die "required tool not found: $tool"
  done
  [ -n "$PRIOR_IMAGE" ] || die "HONUA_PRIOR_SERVER_IMAGE is required"
  [ -n "$CANDIDATE_IMAGE" ] || die "HONUA_CANDIDATE_SERVER_IMAGE is required"
  [ -f "$COMPOSE_FILE" ] || die "compose file not found: $COMPOSE_FILE"
}

record() {
  local name="$1" status="$2" why="$3"
  local tmp="$RESULTS.tmp"
  jq --arg name "$name" --arg status "$status" --arg why "$why" \
    '.scenarios += [{scenario:$name,status:$status,why:$why}]' "$RESULTS" > "$tmp"
  mv "$tmp" "$RESULTS"
  printf '  [%-7s] %s: %s\n' "${status^^}" "$name" "$why"
}

compose() { "${COMPOSE[@]}" "$@"; }

db_exec() {
  compose exec -T db psql -v ON_ERROR_STOP=1 -U honua -d honua -Atc "$1"
}

db_sql() {
  compose exec -T db psql -v ON_ERROR_STOP=1 -U honua -d honua -At
}

db_dump() {
  compose exec -T db pg_dump -U honua -d honua --clean --if-exists --no-owner --no-privileges
}

wait_db() {
  local i
  for i in $(seq 1 60); do
    # pg_isready/Compose health can turn green while the PostGIS image is still running its
    # extension bootstrap scripts. Require the actual extension before starting one-shot server
    # migrations, otherwise the first migration attempt can be lost to an initialization race.
    if db_exec 'SELECT postgis_full_version()' >/dev/null 2>&1; then return 0; fi
    sleep 2
  done
  return 1
}

server_ready() {
  curl -fsS -o /dev/null "${E2E_BASE:-http://localhost:8080}/healthz/ready"
}

wait_ready() {
  wait_ready_attempts "${CHAOS_READY_ATTEMPTS:-90}"
}

wait_ready_attempts() {
  local attempts="$1" i
  for i in $(seq 1 "$attempts"); do
    if server_ready; then return 0; fi
    sleep 2
  done
  return 1
}

stack_down() {
  compose down -v --remove-orphans >/dev/null 2>&1 || true
}

stop_server() {
  compose stop server >/dev/null 2>&1 || true
}

start_image() {
  local image="$1"
  export HONUA_SERVER_IMAGE="$image"
  # Wait on the same Compose health gates used by the server's depends_on contract. A bare
  # `up -d db redis` can return while PostgreSQL is accepting its first socket but is still
  # completing image initialization; the server's one-shot startup migration then records a
  # transient connection failure and remains unready forever.
  compose up -d --wait db redis >/dev/null
  wait_db || die "PostGIS did not become reachable"
  compose up -d server >/dev/null
}

restart_image() {
  local image="$1"
  stop_server
  start_image "$image"
}

capture_state() {
  local journal_file="$1" checksums_file="$2"
  db_exec 'SELECT scriptname FROM public.schema_versions ORDER BY scriptname' > "$journal_file"
  db_exec "SELECT 'e2e_src_fs', count(*), md5(COALESCE(string_agg(to_jsonb(v)::text, E'\\n' ORDER BY to_jsonb(v)::text), '')) FROM honua_data.e2e_src_fs v UNION ALL SELECT 'maui_zoning', count(*), md5(COALESCE(string_agg(to_jsonb(v)::text, E'\\n' ORDER BY to_jsonb(v)::text), '')) FROM honua_data.maui_zoning v ORDER BY 1" > "$checksums_file"
}

assert_state() {
  local label="$1"
  local journal="$OUT/$label-journal.txt" checksums="$OUT/$label-checksums.txt"
  capture_state "$journal" "$checksums"
  if ! cmp -s "$EXPECTED_JOURNAL" "$journal"; then
    record "$label" fail "migration journal diverged after restart; see $journal"
    return 1
  fi
  if ! cmp -s "$EXPECTED_CHECKSUMS" "$checksums"; then
    record "$label" fail "seeded data checksum/count changed; see $checksums"
    return 1
  fi
  record "$label" pass "journal and seeded data converged"
}

restore_baseline() {
  stop_server
  # A plain pg_dump --clean is not a safe reset primitive for this schema: PostgreSQL can refuse
  # a dependency-sensitive DROP ordering when an index introduced by a later migration depends on
  # an index from an earlier one. Recreate the disposable compose database, then restore into the
  # empty database so the snapshot itself is never modified by the reset operation.
  compose exec -T db psql -v ON_ERROR_STOP=1 -U honua -d postgres \
    -c 'DROP DATABASE honua WITH (FORCE)' -c 'CREATE DATABASE honua' >/dev/null
  db_sql < "$BASELINE_DUMP" >/dev/null
  wait_db || die "PostGIS did not recover after baseline restore"
}

seed_prior() {
  rm -rf "$SEED_DIR"
  mkdir -p "$SEED_DIR"
  E2E_BASE="${E2E_BASE:-http://localhost:8080}" \
  E2E_API_KEY="$API_KEY" \
  E2E_OUT="$SEED_DIR" \
  E2E_COMPOSE_FILE="$COMPOSE_FILE" \
  E2E_DB_HOST=db \
  E2E_PSQL="${COMPOSE[*]} exec -T db psql -U honua -d honua" \
    bash "$HERE/seed/seed.sh" > "$OUT/seed.log" 2>&1
  SRC_LAYER_ID="$(jq -r '.slice1.e2e_src_fs.layerId // empty' "$SEED_DIR/seed-manifest.json")"
  [[ "$SRC_LAYER_ID" =~ ^[0-9]+$ ]] || die "existing seed did not publish a numeric e2e_src_fs layer id"
}

prepare_fixture() {
  echo "== upgrade-chaos: prior image -> real seed -> baseline snapshot =="
  stack_down
  start_image "$PRIOR_IMAGE"
  wait_ready || { compose logs server > "$OUT/prior-start.log" 2>&1 || true; die "prior image did not become ready"; }
  seed_prior
  db_dump > "$BASELINE_DUMP"
  capture_state "$OUT/baseline-journal.txt" "$OUT/baseline-checksums.txt"
  echo "baseline seeded layer=$SRC_LAYER_ID"

  echo "== upgrade-chaos: candidate image -> normal migration =="
  restart_image "$CANDIDATE_IMAGE"
  wait_ready || { compose logs server > "$OUT/candidate-start.log" 2>&1 || true; die "candidate image did not become ready"; }
  capture_state "$EXPECTED_JOURNAL" "$EXPECTED_CHECKSUMS"
  diff -u "$OUT/baseline-journal.txt" "$EXPECTED_JOURNAL" > "$OUT/journal-advance.diff" || true
  if cmp -s "$OUT/baseline-journal.txt" "$EXPECTED_JOURNAL"; then
    die "candidate did not advance public.schema_versions"
  fi
}

rollback_query() {
  local response="$OUT/rollback-query.json"
  curl -fsS -H "X-API-Key: $API_KEY" \
    "${E2E_BASE:-http://localhost:8080}/rest/services/e2e/FeatureServer/$SRC_LAYER_ID/query?where=1%3D1&returnCountOnly=true&f=json" \
    -o "$response"
  [ "$(jq -r '.count // -1' "$response")" = 2 ]
}

run_image_rollback() {
  local failed=0
  restart_image "$PRIOR_IMAGE"
  if wait_ready && rollback_query; then
    record image-rollback pass "prior image served both seeded features against the migrated schema"
  else
    compose logs server > "$OUT/rollback-server.log" 2>&1 || true
    record image-rollback fail "prior image could not serve seeded data against the migrated schema"
    failed=1
  fi
  restart_image "$CANDIDATE_IMAGE"
  wait_ready || die "candidate could not be restarted after image rollback"
  [ "$failed" = 0 ]
}

migration_names() {
  comm -13 "$OUT/baseline-journal.txt" "$EXPECTED_JOURNAL"
}

kill_at_boundary() {
  local migration="$1" name="migration-kill-boundary:$1" found=0 i
  restore_baseline
  export HONUA_SERVER_IMAGE="$CANDIDATE_IMAGE"
  compose up -d server >/dev/null
  for i in $(seq 1 "${CHAOS_BOUNDARY_ATTEMPTS:-120}"); do
    if compose logs server 2>&1 | grep -F -- "$BOUNDARY_LOG_PATTERN" | grep -F -- "$migration" >/dev/null; then
      found=1
      compose kill -s SIGKILL server >/dev/null 2>&1 || true
      break
    fi
    sleep 1
  done
  if [ "$found" != 1 ]; then
    compose logs server > "$OUT/boundary-${migration}.log" 2>&1 || true
    record "$name" fail "never observed migration boundary '$migration' using log pattern '$BOUNDARY_LOG_PATTERN'"
    return 1
  fi
  compose up -d server >/dev/null
  if wait_ready && assert_state "$name"; then return 0; fi
  return 1
}

run_boundary_kills() {
  local failures=0 migration
  echo "== scenario: kill and restart at every discovered migration boundary =="
  while IFS= read -r migration; do
    [ -n "$migration" ] || continue
    kill_at_boundary "$migration" || failures=$((failures + 1))
  done < <(migration_names)
  [ "$failures" = 0 ] || return 1
}

run_concurrent_start() {
  local holder_log="$OUT/advisory-lock.log" a b i ready=0
  restore_baseline
  export HONUA_SERVER_IMAGE="$CANDIDATE_IMAGE"
  db_exec "SELECT pg_advisory_lock($LOCK_KEY); SELECT pg_sleep(90);" > "$holder_log" 2>&1 &
  LOCK_HOLDER_PID=$!
  sleep 2
  a="${PROJECT}_chaos_app_a"
  b="${PROJECT}_chaos_app_b"
  docker rm -f "$a" "$b" >/dev/null 2>&1 || true
  compose run -d --no-deps --name "$a" server >/dev/null
  compose run -d --no-deps --name "$b" server >/dev/null
  for i in $(seq 1 60); do
    if docker logs "$a" 2>&1 | grep -Eiq 'ready|migration failed|migration complete' ||
       docker logs "$b" 2>&1 | grep -Eiq 'ready|migration failed|migration complete'; then
      ready=1
      break
    fi
    sleep 2
  done
  kill "$LOCK_HOLDER_PID" >/dev/null 2>&1 || true
  wait "$LOCK_HOLDER_PID" >/dev/null 2>&1 || true
  LOCK_HOLDER_PID=""
  if [ "$ready" = 1 ] && wait_for_journal; then
    if ! assert_state concurrent-app-start; then
      docker rm -f "$a" "$b" >/dev/null 2>&1 || true
      return 1
    fi
  else
    record concurrent-app-start fail "concurrent starters did not produce a ready, converged database"
    return 1
  fi
  docker rm -f "$a" "$b" >/dev/null 2>&1 || true
}

wait_for_journal() {
  local i
  for i in $(seq 1 "${CHAOS_CONVERGENCE_ATTEMPTS:-60}"); do
    if cmp -s <(db_exec 'SELECT scriptname FROM public.schema_versions ORDER BY scriptname') "$EXPECTED_JOURNAL"; then
      return 0
    fi
    sleep 2
  done
  return 1
}

run_partial_failure() {
  local pid_file="$OUT/partial-backend.pid" i remaining converged
  restore_baseline
  db_exec 'DROP TABLE IF EXISTS public.chaos_partial_second; DROP TABLE IF EXISTS public.chaos_partial_first;' >/dev/null
  (db_exec "SET application_name = 'honua_upgrade_chaos_partial'; BEGIN; CREATE TABLE public.chaos_partial_first (id integer PRIMARY KEY); INSERT INTO public.chaos_partial_first VALUES (1); SELECT pg_backend_pid(); SELECT pg_sleep(90); CREATE TABLE public.chaos_partial_second (id integer PRIMARY KEY); COMMIT;" > "$pid_file" 2>&1) &
  PARTIAL_JOB_PID=$!
  # Compose exec can take several seconds to attach while the real server stack is recycling.
  # Keep the transaction open for 90 seconds, but allow up to 75 seconds to observe it before
  # declaring the probe unobservable; the explicit application name makes this independent of
  # psql's stdout buffering.
  for i in $(seq 1 75); do
    PARTIAL_BACKEND_PID="$(db_exec "SELECT pid FROM pg_stat_activity WHERE application_name = 'honua_upgrade_chaos_partial' AND state = 'active' LIMIT 1" || true)"
    if [[ "$PARTIAL_BACKEND_PID" =~ ^[0-9]+$ ]]; then break; fi
    sleep 1
  done
  if ! [[ "$PARTIAL_BACKEND_PID" =~ ^[0-9]+$ ]]; then
    record partial-migration-failure fail "could not identify the in-flight migration backend"
    return 1
  fi
  db_exec "SELECT pg_terminate_backend($PARTIAL_BACKEND_PID);" >/dev/null || true
  wait "$PARTIAL_JOB_PID" >/dev/null 2>&1 || true
  PARTIAL_JOB_PID=""
  PARTIAL_BACKEND_PID=""
  remaining="$(db_exec "SELECT count(*) FROM pg_class WHERE relname IN ('chaos_partial_first','chaos_partial_second');" | tr -d '[:space:]')"
  if [ "$remaining" = 0 ]; then
    db_exec 'BEGIN; CREATE TABLE public.chaos_partial_first (id integer PRIMARY KEY); INSERT INTO public.chaos_partial_first VALUES (1); CREATE TABLE public.chaos_partial_second (id integer PRIMARY KEY); COMMIT;' >/dev/null
    converged="$(db_exec "SELECT CASE WHEN to_regclass('public.chaos_partial_first') IS NOT NULL AND to_regclass('public.chaos_partial_second') IS NOT NULL AND (SELECT count(*) FROM public.chaos_partial_first) = 1 AND (SELECT count(*) FROM public.chaos_partial_second) = 1 THEN 1 ELSE 0 END" | tr -d '[:space:]')"
    if [ "$converged" = 1 ]; then
      record partial-migration-failure pass "backend termination rolled back all statements; a clean re-run converged"
      return 0
    fi
  fi
  record partial-migration-failure fail "partial multi-statement migration was not rolled back atomically (remaining=$remaining converged=${converged:-unset})"
  return 1
}

run_divergence() {
  restore_baseline
  export HONUA_SERVER_IMAGE="$CANDIDATE_IMAGE"
  compose up -d server >/dev/null
  if ! wait_ready; then
    record journal-schema-divergence fail "baseline candidate could not become ready before divergence probe"
    return 1
  fi
  stop_server
  db_exec 'DROP TABLE honua.layers CASCADE;' >/dev/null
  compose up -d server >/dev/null
  if wait_ready_attempts "${CHAOS_DIVERGENCE_ATTEMPTS:-12}"; then
    compose logs server > "$OUT/divergence-server.log" 2>&1 || true
    record journal-schema-divergence fail "server became ready after journaled layers schema was deleted"
    return 1
  fi
  compose logs server > "$OUT/divergence-server.log" 2>&1 || true
  if grep -Eiq 'schema|diverg|missing|floor' "$OUT/divergence-server.log"; then
    record journal-schema-divergence pass "journal/schema divergence kept startup unready and emitted a bounded diagnostic"
  else
    record journal-schema-divergence fail "startup was unready but emitted no schema-divergence diagnostic"
    return 1
  fi
}

run_idempotency() {
  restore_baseline
  restart_image "$CANDIDATE_IMAGE"
  if ! wait_ready; then
    record migration-rerun-idempotency fail "candidate did not apply from baseline"
    return 1
  fi
  local first_journal="$OUT/idempotency-first-journal.txt" first_checksums="$OUT/idempotency-first-checksums.txt"
  capture_state "$first_journal" "$first_checksums"
  restart_image "$CANDIDATE_IMAGE"
  if wait_ready; then
    local second_journal="$OUT/idempotency-second-journal.txt" second_checksums="$OUT/idempotency-second-checksums.txt"
    capture_state "$second_journal" "$second_checksums"
    if cmp -s "$first_journal" "$second_journal" && cmp -s "$first_checksums" "$second_checksums"; then
      record migration-rerun-idempotency pass "re-running candidate migrations changed neither journal nor seeded rows"
      return 0
    fi
  fi
  record migration-rerun-idempotency fail "re-running candidate migrations changed the journal or seeded rows"
  return 1
}

cleanup() {
  [ -z "$LOCK_HOLDER_PID" ] || kill "$LOCK_HOLDER_PID" >/dev/null 2>&1 || true
  [ -z "$PARTIAL_JOB_PID" ] || kill "$PARTIAL_JOB_PID" >/dev/null 2>&1 || true
  [ -z "$PARTIAL_BACKEND_PID" ] || kill "$PARTIAL_BACKEND_PID" >/dev/null 2>&1 || true
  if [ "$KEEP_STACK" != true ]; then stack_down; fi
}
trap cleanup EXIT

require_tools
if [ "$SKIP_PREPARE" = true ]; then
  [ -s "$BASELINE_DUMP" ] || die "CHAOS_SKIP_PREPARE=true requires an existing baseline dump at $BASELINE_DUMP"
  [ -s "$EXPECTED_JOURNAL" ] || die "CHAOS_SKIP_PREPARE=true requires an existing expected journal at $EXPECTED_JOURNAL"
  [ -s "$EXPECTED_CHECKSUMS" ] || die "CHAOS_SKIP_PREPARE=true requires an existing expected checksum file at $EXPECTED_CHECKSUMS"
  compose up -d --wait db redis >/dev/null
  wait_db || die "PostGIS did not become reachable"
else
  prepare_fixture
fi

failures=0
case "$SCENARIO" in
  all)
    run_boundary_kills || failures=$((failures + 1))
    run_image_rollback || failures=$((failures + 1))
    run_concurrent_start || failures=$((failures + 1))
    run_partial_failure || failures=$((failures + 1))
    run_divergence || failures=$((failures + 1))
    run_idempotency || failures=$((failures + 1))
    ;;
  migration-kill-every-boundary) run_boundary_kills || failures=$((failures + 1)) ;;
  image-rollback) run_image_rollback || failures=$((failures + 1)) ;;
  concurrent-app-start) run_concurrent_start || failures=$((failures + 1)) ;;
  partial-migration-failure) run_partial_failure || failures=$((failures + 1)) ;;
  journal-schema-divergence) run_divergence || failures=$((failures + 1)) ;;
  migration-rerun-idempotency) run_idempotency || failures=$((failures + 1)) ;;
  *) die "unknown CHAOS_SCENARIO: $SCENARIO" ;;
esac

jq --argjson failures "$failures" '. + {failures:$failures, generatedAt:(now | todateiso8601)}' "$RESULTS" > "$RESULTS.tmp"
mv "$RESULTS.tmp" "$RESULTS"
cat "$RESULTS"
[ "$failures" = 0 ]
