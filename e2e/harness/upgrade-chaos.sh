#!/usr/bin/env bash
# Upgrade chaos driver for the packet-94 release harness.
#
# This is intentionally a hunt driver, not a recovery implementation.  It runs the real
# prior/candidate server images against the real PostGIS service from compose.candidate.yml and
# exits non-zero whenever it cannot prove convergence, rollback compatibility, lock serialization,
# transactional partial-failure behavior, fail-closed divergence handling, or idempotency.
# partial-migration-failure pauses the candidate's own runner inside a pending migration (see
# partial_failure_probe) and records blocked, never pass, when that pause cannot be proved.
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
EXPECTED_SCHEMA="$OUT/expected-schema.sql"
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
  for tool in docker curl jq od tr; do
    command -v "$tool" >/dev/null 2>&1 || die "required tool not found: $tool"
  done
  [ -n "$PRIOR_IMAGE" ] || die "HONUA_PRIOR_SERVER_IMAGE is required"
  [ -n "$CANDIDATE_IMAGE" ] || die "HONUA_CANDIDATE_SERVER_IMAGE is required"
  local image
  for image in "$PRIOR_IMAGE" "$CANDIDATE_IMAGE"; do
    [[ "$image" =~ ^[^[:space:]@]+@sha256:[0-9a-f]{64}$ ]] || die "server images must use repository@sha256:<64 lowercase hex digits>: $image"
  done
  jq -n --arg prior "$PRIOR_IMAGE" --arg candidate "$CANDIDATE_IMAGE" \
    '{priorImage:$prior,candidateImage:$candidate}' > "$OUT/images.json"
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
  compose up -d --force-recreate server >/dev/null
}

restart_image() {
  local image="$1"
  stop_server
  start_image "$image"
}

capture_schema() {
  # pg_dump covers tables, columns, indexes, constraints, functions and other schema objects.
  # Recent pg_dump releases emit a random psql restriction token; only that token is noise.
  compose exec -T db pg_dump -U honua -d honua --schema-only --no-owner --no-privileges |
    sed -E '/^\\(un)?restrict /d' > "$1"
}

capture_state() {
  local journal_file="$1" checksums_file="$2"
  db_exec 'SELECT scriptname FROM public.schema_versions ORDER BY scriptname' > "$journal_file"
  db_exec "SELECT 'e2e_src_fs', count(*), md5(COALESCE(string_agg(to_jsonb(v)::text, E'\\n' ORDER BY to_jsonb(v)::text), '')) FROM honua_data.e2e_src_fs v UNION ALL SELECT 'maui_zoning', count(*), md5(COALESCE(string_agg(to_jsonb(v)::text, E'\\n' ORDER BY to_jsonb(v)::text), '')) FROM honua_data.maui_zoning v ORDER BY 1" > "$checksums_file"
}

assert_state() {
  local label="$1" passed="${2:-schema, journal and seeded data converged}"
  local journal="$OUT/$label-journal.txt" checksums="$OUT/$label-checksums.txt"
  if ! capture_state "$journal" "$checksums" || ! capture_schema "$OUT/$label-schema.sql"; then
    record "$label" fail "could not capture recovered database state"
    return 1
  fi
  if ! cmp -s "$EXPECTED_SCHEMA" "$OUT/$label-schema.sql"; then
    record "$label" fail "migrated schema diverged after restart; see $OUT/$label-schema.sql"
    return 1
  fi
  if ! cmp -s "$EXPECTED_JOURNAL" "$journal"; then
    record "$label" fail "migration journal diverged after restart; see $journal"
    return 1
  fi
  if ! cmp -s "$EXPECTED_CHECKSUMS" "$checksums"; then
    record "$label" fail "seeded data checksum/count changed; see $checksums"
    return 1
  fi
  record "$label" pass "$passed"
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
  load_seed_layer
}

load_seed_layer() {
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
  # Every scenario migrates a database restored from the baseline dump. A pg_dump restore re-deparses
  # CHECK expressions (ARRAY[...]::text[] becomes ARRAY[(...)::text]), so the reference schema must be
  # produced by the same restored-baseline path or every recovery comparison diverges on that alone.
  restore_baseline
  restart_image "$CANDIDATE_IMAGE"
  wait_ready || { compose logs server > "$OUT/candidate-start.log" 2>&1 || true; die "candidate image did not become ready"; }
  capture_state "$EXPECTED_JOURNAL" "$EXPECTED_CHECKSUMS"
  capture_schema "$EXPECTED_SCHEMA"
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
  compose up -d --force-recreate server >/dev/null
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
  compose up -d --force-recreate server >/dev/null
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

run_concurrent_start() (
  # Subshell EXIT cleanup covers every return, including failed container creation/assertions.
  local holder_log="$OUT/advisory-lock.log" a b i ready=0 failed=0
  local cleanup_owner="$BASHPID"
  local holder_name
  holder_name="honua-chaos-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')" || return 1
  [[ "$holder_name" =~ ^honua-chaos-[0-9a-f]{32}$ ]] || return 1
  stop_concurrent_lock_holder() {
    [ -n "$LOCK_HOLDER_PID" ] || return 0
    local failed=0 remaining=""
    # Killing the Docker CLI does not establish that its remote psql session
    # exited. Terminate only our tagged database backend, then let the complete
    # holder command return naturally and reap it before continuing.
    db_exec "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = '$holder_name' AND datname = current_database() AND usename = current_user AND pid <> pg_backend_pid();" >/dev/null 2>&1 || failed=1
    wait "$LOCK_HOLDER_PID" >/dev/null 2>&1 || true
    LOCK_HOLDER_PID=""
    # A false pg_terminate_backend result is not proof. Require actual absence
    # after the complete external command has exited and been reaped.
    remaining="$(db_exec "SELECT count(*) FROM pg_stat_activity WHERE application_name = '$holder_name' AND datname = current_database() AND usename = current_user;")" || failed=1
    [ "$remaining" = 0 ] || failed=1
    [ "$failed" = 0 ]
  }
  a="${PROJECT}_chaos_app_a"
  b="${PROJECT}_chaos_app_b"
  # A background child must never remove the owning shell's containers, even
  # if it receives a signal before resetting inherited trap state.
  trap 'if [ "$BASHPID" = "$cleanup_owner" ]; then
    docker rm -f "$a" "$b" >/dev/null 2>&1 || true
    stop_concurrent_lock_holder || true
  fi' EXIT
  restore_baseline
  export HONUA_SERVER_IMAGE="$CANDIDATE_IMAGE"
  (trap - EXIT; db_exec "SET application_name = '$holder_name'; SET statement_timeout = '95s'; SELECT pg_advisory_lock($LOCK_KEY); SELECT pg_sleep(90);") > "$holder_log" 2>&1 &
  LOCK_HOLDER_PID=$!
  sleep 2
  docker rm -f "$a" "$b" >/dev/null 2>&1 || true
  compose run -d --no-deps --name "$a" server >/dev/null || return 1
  compose run -d --no-deps --name "$b" server >/dev/null || return 1
  for i in $(seq 1 "${CHAOS_READY_ATTEMPTS:-90}"); do
    docker logs "$a" > "$OUT/concurrent-a.log" 2>&1 || return 1
    docker logs "$b" > "$OUT/concurrent-b.log" 2>&1 || return 1
    if grep -Eiq 'migration failed' "$OUT/concurrent-a.log" "$OUT/concurrent-b.log"; then
      failed=1
      break
    fi
    if container_ready "$a" && container_ready "$b"; then
      ready=1
      break
    fi
    sleep 2
  done
  stop_concurrent_lock_holder || return 1
  if [ "$failed" = 0 ] && [ "$ready" = 1 ] && wait_for_journal; then
    if ! assert_state concurrent-app-start; then
      return 1
    fi
  else
    record concurrent-app-start fail "both concurrent starters must be healthy without migration failures and converge"
    return 1
  fi
)

container_ready() {
  [ "$(docker inspect --format '{{.State.Running}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$1")" = 'true healthy' ]
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

# The candidate's DbUp runner applies every pending script and its public.schema_versions INSERT in
# ONE transaction (.WithTransaction()). Holding SHARE on the journal table therefore pauses the real
# runner deterministically after the first pending script's statements have executed and before its
# journal row or COMMIT: the runner backend is observed waiting on that lock with a transaction id and
# write locks already held. That pause, not log timing, is the interruption point.
partial_holder_pid() {
  db_exec "SELECT l.pid FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid WHERE a.application_name = '$1' AND a.datname = current_database() AND l.locktype = 'relation' AND l.relation = 'public.schema_versions'::regclass AND l.mode = 'ShareLock' AND l.granted"
}

partial_paused_runner() {
  # pid|xid assigned|granted write-mode relation locks, for the backend blocked on the journal INSERT.
  db_exec "SELECT a.pid || '|' || (a.backend_xid IS NOT NULL) || '|' || (SELECT count(*) FROM pg_locks l WHERE l.pid = a.pid AND l.granted AND l.locktype = 'relation' AND l.mode IN ('RowExclusiveLock', 'ShareUpdateExclusiveLock', 'ShareLock', 'ShareRowExclusiveLock', 'ExclusiveLock', 'AccessExclusiveLock')) FROM pg_stat_activity a WHERE $1 = ANY(pg_blocking_pids(a.pid)) AND a.wait_event_type = 'Lock' AND a.query ILIKE '%insert%schema_versions%' AND a.datname = current_database()"
}

partial_interrupted_script() {
  compose logs server 2>&1 | grep -F -- "$BOUNDARY_LOG_PATTERN" | tail -n 1 | sed -nE "s/.*'([^']+)'.*/\1/p"
}

partial_client_backends() {
  db_exec "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND backend_type = 'client backend' AND pid <> pg_backend_pid()"
}

partial_failure_probe() (
  # Subshell EXIT cleanup always releases the journal-lock holder, mirroring concurrent startup.
  local mode="$1" name="partial-migration-failure:$1"
  local pre="$OUT/partial-$1-pre" post="$OUT/partial-$1-interrupted"
  local cleanup_owner="$BASHPID" holder_name holder_pid="" runner="" runner_pid="" script="" i
  local attempts="${CHAOS_PARTIAL_ATTEMPTS:-25}" hold="${CHAOS_PARTIAL_HOLD_SECONDS:-300}"
  holder_name="honua-chaos-$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')" || return 1
  [[ "$holder_name" =~ ^honua-chaos-[0-9a-f]{32}$ ]] || return 1
  release_partial_holder() {
    [ -n "$PARTIAL_JOB_PID" ] || return 0
    local failed=0 remaining=""
    db_exec "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = '$holder_name' AND datname = current_database() AND usename = current_user AND pid <> pg_backend_pid();" >/dev/null 2>&1 || failed=1
    wait "$PARTIAL_JOB_PID" >/dev/null 2>&1 || true
    PARTIAL_JOB_PID=""
    remaining="$(db_exec "SELECT count(*) FROM pg_stat_activity WHERE application_name = '$holder_name' AND datname = current_database() AND usename = current_user;")" || failed=1
    [ "$remaining" = 0 ] || failed=1
    [ "$failed" = 0 ]
  }
  trap 'if [ "$BASHPID" = "$cleanup_owner" ]; then release_partial_holder || true; fi' EXIT

  echo "== scenario: interrupt the candidate runner inside a pending migration ($mode) =="
  restore_baseline
  if [ -z "$(migration_names)" ]; then
    record "$name" blocked "candidate has no pending migration over the prior baseline to interrupt"
    return 1
  fi
  if ! capture_state "$pre-journal.txt" "$pre-checksums.txt" || ! capture_schema "$pre-schema.sql"; then
    record "$name" fail "could not capture the pre-migration baseline"
    return 1
  fi

  (trap - EXIT; db_exec "SET application_name = '$holder_name'; SET statement_timeout = '$((hold + 30))s'; BEGIN; LOCK TABLE public.schema_versions IN SHARE MODE; SELECT pg_sleep($hold); COMMIT;") > "$OUT/partial-$mode-holder.log" 2>&1 &
  PARTIAL_JOB_PID=$!
  for i in $(seq 1 "$attempts"); do
    holder_pid="$(partial_holder_pid "$holder_name" 2>/dev/null)" || holder_pid=""
    [[ "$holder_pid" =~ ^[0-9]+$ ]] && break
    holder_pid=""
    sleep 1
  done
  if [ -z "$holder_pid" ]; then
    record "$name" blocked "could not hold SHARE on public.schema_versions to pause the candidate runner"
    return 1
  fi

  export HONUA_SERVER_IMAGE="$CANDIDATE_IMAGE"
  compose up -d --force-recreate server >/dev/null || { record "$name" fail "candidate server could not be started"; return 1; }
  for i in $(seq 1 "$attempts"); do
    runner="$(partial_paused_runner "$holder_pid" 2>/dev/null)" || runner=""
    script="$(partial_interrupted_script)" || script=""
    if [[ "$runner" =~ ^[0-9]+\|true\|[1-9][0-9]*$ ]] && [ -n "$script" ]; then break; fi
    runner=""
    sleep 1
  done
  if [ -z "$runner" ]; then
    compose logs server > "$OUT/partial-$mode-server.log" 2>&1 || true
    record "$name" blocked "never observed the candidate runner paused before journaling with uncommitted migration writes (xid and write locks)"
    return 1
  fi
  runner_pid="${runner%%|*}"
  if ! migration_names | grep -Fxq -- "$script"; then
    compose logs server > "$OUT/partial-$mode-server.log" 2>&1 || true
    record "$name" blocked "paused script '$script' is not a pending candidate migration"
    return 1
  fi
  db_exec "SELECT l.locktype, l.relation, l.mode, l.granted FROM pg_locks l WHERE l.pid = $runner_pid ORDER BY 1, 2, 3" > "$OUT/partial-$mode-runner-locks.txt" 2>&1 || true
  printf 'script=%s\nrunner=%s\n' "$script" "$runner" > "$OUT/partial-$mode-interruption.txt"

  case "$mode" in
    server-killed) compose kill -s SIGKILL server >/dev/null 2>&1 || true ;;
    backend-terminated)
      if [ "$(db_exec "SELECT pg_terminate_backend($runner_pid)")" != t ]; then
        record "$name" fail "could not terminate the paused runner backend $runner_pid"
        return 1
      fi ;;
    *) record "$name" fail "unknown interruption mode"; return 1 ;;
  esac

  if [ "$mode" = backend-terminated ]; then
    # The runner process survives this interruption, so it owns the failure: it must stay unready.
    local diagnosed=0
    for i in $(seq 1 "$attempts"); do
      # Capture first: under pipefail, grep -q closing the pipe early would SIGPIPE compose logs.
      compose logs server > "$OUT/partial-$mode-server.log" 2>&1 || true
      if grep -Eiq 'migration[^[:cntrl:]]*fail' "$OUT/partial-$mode-server.log"; then diagnosed=1; break; fi
      sleep 1
    done
    if [ "$diagnosed" != 1 ]; then
      record "$name" fail "runner emitted no migration failure after its transaction was terminated"
      return 1
    fi
    if server_ready; then
      record "$name" fail "server reported ready after its migration transaction was terminated"
      return 1
    fi
  else
    compose logs server > "$OUT/partial-$mode-server.log" 2>&1 || true
  fi

  # Stop every runner (including any background retry) before releasing the journal lock, so the
  # state captured below is what the interruption left behind, not a later successful retry.
  stop_server
  if ! release_partial_holder; then
    record "$name" fail "could not release the journal lock holder"
    return 1
  fi
  for i in $(seq 1 "$attempts"); do
    [ "$(partial_client_backends 2>/dev/null)" = 0 ] && break
    sleep 1
  done
  if [ "$(partial_client_backends 2>/dev/null)" != 0 ]; then
    record "$name" fail "interrupted runner backend never ended"
    return 1
  fi

  if ! capture_state "$post-journal.txt" "$post-checksums.txt" || ! capture_schema "$post-schema.sql"; then
    record "$name" fail "could not capture post-interruption state"
    return 1
  fi
  if grep -Fxq -- "$script" "$post-journal.txt"; then
    record "$name" fail "interrupted migration '$script' was journaled"
    return 1
  fi
  if ! cmp -s "$pre-journal.txt" "$post-journal.txt"; then
    record "$name" fail "migration journal changed after interrupting '$script'; see $post-journal.txt"
    return 1
  fi
  if ! cmp -s "$pre-schema.sql" "$post-schema.sql"; then
    diff -u "$pre-schema.sql" "$post-schema.sql" > "$OUT/partial-$mode-schema.diff" || true
    record "$name" fail "partial schema from interrupted '$script' survived; see $OUT/partial-$mode-schema.diff"
    return 1
  fi
  if ! cmp -s "$pre-checksums.txt" "$post-checksums.txt"; then
    record "$name" fail "seeded rows changed after interrupting '$script'; see $post-checksums.txt"
    return 1
  fi

  compose up -d --force-recreate server >/dev/null || { record "$name" fail "candidate server could not be restarted"; return 1; }
  if ! wait_ready; then
    compose logs server > "$OUT/partial-$mode-retry.log" 2>&1 || true
    record "$name" fail "candidate did not become ready when retrying after interrupting '$script'"
    return 1
  fi
  assert_state "$name" "runner paused inside '$script' after its writes; $mode left the prior journal, schema and rows; retry converged"
)

run_partial_failure() {
  # Interrupts the candidate's own migration runner, never a hand-written psql transaction
  # (honua-io/honua-release#440): no PostgreSQL-only path may record pass.
  local failures=0 mode
  for mode in backend-terminated server-killed; do
    partial_failure_probe "$mode" || failures=$((failures + 1))
  done
  [ "$failures" = 0 ]
}

run_divergence() {
  restore_baseline
  export HONUA_SERVER_IMAGE="$CANDIDATE_IMAGE"
  compose up -d --force-recreate server >/dev/null
  if ! wait_ready; then
    record journal-schema-divergence fail "baseline candidate could not become ready before divergence probe"
    return 1
  fi
  stop_server
  db_exec 'DROP TABLE honua.layers CASCADE;' >/dev/null
  compose up -d --force-recreate server >/dev/null
  if wait_ready; then
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
  if [[ "$PARTIAL_BACKEND_PID" =~ ^[0-9]+$ ]]; then
    db_exec "SELECT pg_terminate_backend($PARTIAL_BACKEND_PID);" >/dev/null 2>&1 || true
  fi
  if [ "$KEEP_STACK" != true ]; then stack_down; fi
}
trap cleanup EXIT

require_tools
if [ "$SKIP_PREPARE" = true ]; then
  [ -s "$BASELINE_DUMP" ] || die "CHAOS_SKIP_PREPARE=true requires an existing baseline dump at $BASELINE_DUMP"
  [ -s "$EXPECTED_JOURNAL" ] || die "CHAOS_SKIP_PREPARE=true requires an existing expected journal at $EXPECTED_JOURNAL"
  [ -s "$EXPECTED_CHECKSUMS" ] || die "CHAOS_SKIP_PREPARE=true requires an existing expected checksum file at $EXPECTED_CHECKSUMS"
  [ -s "$EXPECTED_SCHEMA" ] || die "CHAOS_SKIP_PREPARE=true requires an existing expected schema at $EXPECTED_SCHEMA"
  load_seed_layer
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
