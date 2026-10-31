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
#   COMPOSE_UPGRADE_FAULT          compose-upgrade-rollback only: none (default), drop-seed-rows,
#                                  journal-stall or rollback-unreadable (negative controls)
#   HONUA_CANDIDATE_DB_SCHEMA      compose-upgrade-rollback only: declared candidate schema number
#
# Scenarios:
#   migration-kill-every-boundary, image-rollback, concurrent-app-start,
#   partial-migration-failure, journal-schema-divergence, migration-rerun-idempotency
#   compose-upgrade-rollback (the GA Docker Compose customer upgrade/rollback that gate-upgrade's
#   required compose-upgrade job runs; not part of `all`, it seeds its own prior install)
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

# SIGKILL the candidate server and prove it stopped. A failed or ignored kill is not an
# interruption: a later stop_server would end the runner normally and fake crash recovery.
sigkill_server() {
  local i running
  compose kill -s SIGKILL server >/dev/null 2>&1 || return 1
  for i in $(seq 1 "${CHAOS_KILL_ATTEMPTS:-10}"); do
    running="$(compose ps -q --status running server 2>/dev/null)" || return 1
    [ -z "$running" ] && return 0
    sleep 1
  done
  return 1
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

# GitHub artifact paths reject " : < > | * ? CR LF. Scenario ids in the receipt keep
# those characters; evidence files use this stem so upload-artifact can store them.
evidence_stem() {
  local stem
  # tr, not sed: sed splits on LF, so a line feed would survive inside $(...).
  stem="$(printf '%s' "$1" | tr $':"<>|*?\r\n' '_')"
  [ -n "$stem" ] || stem="evidence"
  printf '%s' "$stem"
}

assert_state() {
  local label="$1" passed="${2:-schema, journal and seeded data converged}"
  local stem journal checksums schema_file
  stem="$(evidence_stem "$label")"
  journal="$OUT/${stem}-journal.txt"
  checksums="$OUT/${stem}-checksums.txt"
  schema_file="$OUT/${stem}-schema.sql"
  if ! capture_state "$journal" "$checksums" || ! capture_schema "$schema_file"; then
    record "$label" fail "could not capture recovered database state"
    return 1
  fi
  if ! cmp -s "$EXPECTED_SCHEMA" "$schema_file"; then
    record "$label" fail "migrated schema diverged after restart; see $schema_file"
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
      if ! sigkill_server; then
        record "$name" fail "SIGKILL at migration boundary '$migration' failed or the server kept running"
        return 1
      fi
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
    server-killed)
      if ! sigkill_server; then
        compose logs server > "$OUT/partial-$mode-server.log" 2>&1 || true
        record "$name" fail "SIGKILL of the paused candidate runner failed or the server kept running"
        return 1
      fi ;;
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

# ── compose-upgrade-rollback: the GA Docker Compose customer upgrade and rollback ──────────────────
# Founder decision D9 (2026-10-26, honua-release#950): Docker Compose on-prem is GA, so this is the
# REQUIRED runtime fragment of gate-upgrade (job compose-upgrade); Helm on kind is Preview evidence.
# It follows docs/UPGRADE-RUNBOOK.md section 6 on this compose stack, with the kind-upgrade lane's
# fail-closed rules:
#   install the prior image -> real seed -> rollback-target guard probe -> mandatory pg_dump backup
#   -> recreate the server on the candidate digest (a typed contract-migration refusal is answered
#   with its one-shot HONUA_APPROVE_CONTRACT_MIGRATIONS nonce, the documented customer step)
#   -> every prior journal entry remains and the journal grew, or it held because this
#      candidate image (not the prior image) ships no new migration; the observed schema
#      equals the declared one; seeded rows survived
#   -> roll back to the prior image on the migrated database, or, at the documented boundary
#   (typed schema_reader_incompatible after approved contract migrations, below schema 135,
#   or a migration declared reader-incompatible rather than expand-only),
#   restore the pre-upgrade backup -> the prior image serves the seeded rows again.
# Only a recorded `pass` is a pass. COMPOSE_UPGRADE_FAULT injects a defect so CI can keep proving
# the gate fails on it through its own assertion (gate-upgrade negative-control cell):
#   drop-seed-rows       the "upgrade" loses one seeded row
#   journal-stall        the "candidate" is the prior image, so the journal cannot advance
#   rollback-unreadable  the seeded table is unreadable when the prior image comes back
COMPOSE_FAULT="${COMPOSE_UPGRADE_FAULT:-none}"
CANDIDATE_DB_SCHEMA="${HONUA_CANDIDATE_DB_SCHEMA:-}"
READER_EXPAND_FLOOR="${READER_EXPAND_FLOOR:-135}"
COMPOSE_PLATFORM="linux/amd64"
CU_NAME=compose-upgrade-rollback
CU_BACKUP="$OUT/compose-pre-upgrade.dump"
CU_APPROVAL_OVERRIDE="$OUT/compose-approve-contract.override.yml"
SEED_COUNTS_EXPECTED="e2e_src_fs=2 maui_zoning=12"

cu_reset_observations() {
  CU_BEFORE_VERSIONS=""; CU_AFTER_VERSIONS=""; CU_PRIOR_SCHEMA=""; CU_CANDIDATE_SCHEMA=""
  CU_BEFORE_COUNTS=""; CU_AFTER_COUNTS=""; CU_ROLLBACK_COUNTS=""; CU_CHECKSUMS_MATCHED=""
  CU_GUARD=unprobed; CU_BACKUP_TAKEN=false; CU_APPROVAL=not-required; CU_NONCE=""; CU_GATED=""
  CU_ROLLBACK_OUTCOME=""; CU_BOUNDARY=""; CU_RESTORE_VERIFIED=""; CU_SERVED_SILENTLY=""; CU_ROLLBACK_COUNT=""
}

cu_schema() {
  db_exec "SELECT COALESCE(max(substring(scriptname from '([0-9]{3})_')::integer), 0) FROM public.schema_versions"
}

cu_counts() {
  # "e2e_src_fs=N maui_zoning=M" from a capture_state checksum file.
  awk -F'|' '{printf "%s%s=%s", (NR>1?" ":""), $1, $2}' "$1"
}

cu_identity() {
  # <phase> <expected image reference>: the running server container must be exactly that image,
  # linux/amd64, and its admin capabilities answer is recorded as the observed runtime identity.
  local phase="$1" ref="$2" container image_id expected_id platform version
  container="$(compose ps -q server 2>/dev/null)" || container=""
  image_id="$( [ -n "$container" ] && docker inspect --format '{{.Image}}' "$container" 2>/dev/null)" || image_id=""
  expected_id="$(docker image inspect --format '{{.Id}}' "$ref" 2>/dev/null)" || expected_id=""
  platform="$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$ref" 2>/dev/null)" || platform=""
  curl -fsS -H "X-API-Key: $API_KEY" "${E2E_BASE:-http://localhost:8080}/api/v1/admin/capabilities" \
    -o "$OUT/compose-$phase-runtime.json" 2>/dev/null || echo '{}' > "$OUT/compose-$phase-runtime.json"
  version="$(jq -r '.data.serverVersion // empty' "$OUT/compose-$phase-runtime.json" 2>/dev/null)" || version=""
  jq -n --arg imageID "$image_id" --arg expected "$expected_id" --arg digest "${ref##*@}" --arg ref "$ref" \
    --arg platform "$platform" --arg version "$version" --arg schema "$(cu_schema 2>/dev/null)" \
    '{imageID:$imageID,expectedImageID:$expected,platform:$platform,
      runtimeIdentity:{imageReference:$ref,imageDigest:$digest,observedVersion:$version},databaseSchema:$schema}' \
    > "$OUT/compose-$phase-identity.json"
  [ -n "$image_id" ] && [ "$image_id" = "$expected_id" ] && [ "$platform" = "$COMPOSE_PLATFORM" ]
}

cu_serves_seed() {
  local response="$1"
  curl -fsS -H "X-API-Key: $API_KEY" \
    "${E2E_BASE:-http://localhost:8080}/rest/services/e2e/FeatureServer/$SRC_LAYER_ID/query?where=1%3D1&returnCountOnly=true&f=json" \
    -o "$response" 2>/dev/null || return 1
  CU_ROLLBACK_COUNT="$(jq -r '.count // empty' "$response" 2>/dev/null)" || CU_ROLLBACK_COUNT=""
  [ "$CU_ROLLBACK_COUNT" = 2 ]
}

cu_wait_ready() {
  # <attempts>: like wait_ready_attempts, but a server container that has exited (a typed startup
  # refusal such as schema_reader_incompatible or the contract-migration gate) ends the wait early.
  local attempts="$1" i
  for i in $(seq 1 "$attempts"); do
    if server_ready 2>/dev/null; then return 0; fi
    if [ "$i" -gt 3 ] && [ -z "$(compose ps -q --status running server 2>/dev/null)" ]; then return 1; fi
    sleep 2
  done
  return 1
}

cu_start_server() {
  # <image> [override file]: recreate only the server; db and redis (and the volume-less database
  # they hold for this run) stay up, exactly as `docker compose up -d` does for a changed image.
  local image="$1" override="${2:-}"
  export HONUA_SERVER_IMAGE="$image"
  if [ -n "$override" ]; then
    docker compose -f "$COMPOSE_FILE" -f "$override" up -d --force-recreate --no-deps server >/dev/null 2>&1
  else
    compose up -d --force-recreate --no-deps server >/dev/null 2>&1
  fi
}

cu_guard_probe() {
  # Rollback-target eligibility (honua-server#5333, #6484), as kind-upgrade probes it: does the
  # prior refuse a journal entry it does not ship? An unguarded prior starts on any schema, so an
  # image-only rollback to it can never be certified.
  local probe='Honua.Server.Migrations.999_RollbackTargetGuardProbe.sql'
  stop_server
  if ! db_exec "INSERT INTO public.schema_versions (scriptname, applied) VALUES ('$probe', now())" >/dev/null 2>&1; then
    CU_GUARD=unprobed; return 1
  fi
  cu_start_server "$PRIOR_IMAGE"
  if cu_wait_ready "${CHAOS_GUARD_ATTEMPTS:-45}"; then
    CU_GUARD=absent
  else
    compose logs server > "$OUT/compose-guard-probe.log" 2>&1 || true
    if grep -q 'schema_reader_incompatible' "$OUT/compose-guard-probe.log"; then CU_GUARD=present; else CU_GUARD=inconclusive; fi
  fi
  stop_server
  db_exec "DELETE FROM public.schema_versions WHERE scriptname = '$probe'" >/dev/null 2>&1 || return 1
  cu_start_server "$PRIOR_IMAGE" && wait_ready
}

cu_restore_backup() {
  # Documented recovery: stop the server, restore the pre-upgrade backup into a fresh database,
  # start the prior image. pg_restore notices are kept in the log; serving is the verdict.
  stop_server
  compose exec -T db psql -v ON_ERROR_STOP=1 -U honua -d postgres \
    -c 'DROP DATABASE honua WITH (FORCE)' -c 'CREATE DATABASE honua OWNER honua' > "$OUT/compose-restore.log" 2>&1 || return 1
  compose exec -T db pg_restore -U honua -d honua --no-owner < "$CU_BACKUP" >> "$OUT/compose-restore.log" 2>&1
  echo "pg_restore exit $?" >> "$OUT/compose-restore.log"
  wait_db || return 1
  cu_start_server "$PRIOR_IMAGE" && wait_ready
}

cu_write_observed() {
  jq -n --arg before "$CU_BEFORE_VERSIONS" --arg after "$CU_AFTER_VERSIONS" \
    --arg schema "$CU_CANDIDATE_SCHEMA" --arg declared "$CANDIDATE_DB_SCHEMA" --arg priorSchema "$CU_PRIOR_SCHEMA" \
    --arg beforeCounts "$CU_BEFORE_COUNTS" --arg afterCounts "$CU_AFTER_COUNTS" --arg rollbackCounts "$CU_ROLLBACK_COUNTS" \
    --arg checksums "$CU_CHECKSUMS_MATCHED" --arg rollbackCount "$CU_ROLLBACK_COUNT" \
    --arg approval "$CU_APPROVAL" --arg nonce "$CU_NONCE" --arg gated "$CU_GATED" \
    --arg rollback "$CU_ROLLBACK_OUTCOME" --arg boundary "$CU_BOUNDARY" --arg backup "$CU_BACKUP_TAKEN" \
    --arg floor "$READER_EXPAND_FLOOR" --arg guard "$CU_GUARD" --arg restored "$CU_RESTORE_VERIFIED" \
    --arg silent "$CU_SERVED_SILENTLY" --arg fault "$COMPOSE_FAULT" --arg platform "$COMPOSE_PLATFORM" \
    '{platform:$platform,injectedFault:$fault,
      schemaVersionsRows:{beforeUpgrade:$before,afterUpgrade:$after},observedSchema:$schema,
      declaredCandidateSchema:$declared,priorSchema:$priorSchema,
      seededRows:{expected:"e2e_src_fs=2 maui_zoning=12",beforeUpgrade:$beforeCounts,afterUpgrade:$afterCounts,
                  afterRollback:$rollbackCounts,checksumsMatched:(if $checksums=="" then null else ($checksums=="true") end)},
      rollbackPriorQueryCount:$rollbackCount,
      customerSteps:{preUpgradeBackup:($backup=="true"),
        contractMigrationApproval:{outcome:$approval,nonce:$nonce,gatedScripts:$gated,
          steps:(if $nonce=="" then [] else ["docker compose up -d (candidate): refused with the typed contract-migration gate and printed the one-shot nonce",
                 "docker compose up -d with HONUA_APPROVE_CONTRACT_MIGRATIONS=<nonce>"] end)},
        rollback:{outcome:$rollback,boundary:$boundary,readerExpandFloorSchema:$floor,priorSchemaReaderGuard:$guard,
                  certifiedRollbackPath:(if $guard=="absent" or $boundary!="" then "restore-pre-upgrade-backup" else "image-rollback" end),
                  restoreVerified:(if $restored=="" then null else ($restored=="true") end),
                  imageRollbackServedSilently:(if $silent=="" then null else ($silent=="true") end)}}}' \
    > "$OUT/compose-observed.json"
}

cu_fail() {
  compose logs server > "$OUT/compose-server-last.log" 2>&1 || true
  cu_write_observed
  record "$CU_NAME" fail "$1"
  return 1
}

run_compose_upgrade_rollback() {
  local before_journal="$OUT/compose-before-journal.txt" before_sums="$OUT/compose-before-checksums.txt"
  local after_journal="$OUT/compose-after-journal.txt" after_sums="$OUT/compose-after-checksums.txt"
  local rollback_journal="$OUT/compose-rollback-journal.txt" rollback_sums="$OUT/compose-rollback-checksums.txt"
  local upgrade_image="$CANDIDATE_IMAGE" refusal="" journal_phrase=""
  cu_reset_observations
  case "$COMPOSE_FAULT" in
    none|drop-seed-rows|rollback-unreadable) ;;
    journal-stall) upgrade_image="$PRIOR_IMAGE" ;;
    *) cu_fail "unknown COMPOSE_UPGRADE_FAULT: $COMPOSE_FAULT"; return 1 ;;
  esac
  if [ "$PRIOR_IMAGE" = "$CANDIDATE_IMAGE" ]; then
    cu_write_observed
    record "$CU_NAME" blocked "prior image equals the candidate; a real upgrade cannot run (never certify the candidate against itself)"
    return 1
  fi
  echo "== $CU_NAME: install prior $PRIOR_IMAGE ($COMPOSE_PLATFORM, fault=$COMPOSE_FAULT) =="
  stack_down
  start_image "$PRIOR_IMAGE"
  wait_ready || { compose logs server > "$OUT/compose-prior-start.log" 2>&1 || true; cu_fail "prior image did not become ready on a fresh database"; return 1; }
  cu_identity install "$PRIOR_IMAGE" || { cu_fail "running prior container is not exactly $PRIOR_IMAGE on $COMPOSE_PLATFORM"; return 1; }
  rm -rf "$SEED_DIR"; mkdir -p "$SEED_DIR"
  if ! E2E_BASE="${E2E_BASE:-http://localhost:8080}" E2E_API_KEY="$API_KEY" E2E_OUT="$SEED_DIR" \
       E2E_COMPOSE_FILE="$COMPOSE_FILE" E2E_DB_HOST=db E2E_PSQL="${COMPOSE[*]} exec -T db psql -U honua -d honua" \
       bash "$HERE/seed/seed.sh" > "$OUT/compose-seed.log" 2>&1; then
    tail -n 40 "$OUT/compose-seed.log" || true
    cu_fail "prior image installed but the real e2e seed failed"; return 1
  fi
  SRC_LAYER_ID="$(jq -r '.slice1.e2e_src_fs.layerId // empty' "$SEED_DIR/seed-manifest.json" 2>/dev/null)" || SRC_LAYER_ID=""
  [[ "$SRC_LAYER_ID" =~ ^[0-9]+$ ]] || { cu_fail "real e2e seed did not publish the e2e_src_fs layer (layerId=$SRC_LAYER_ID)"; return 1; }
  capture_state "$before_journal" "$before_sums" || { cu_fail "could not capture the seeded prior state"; return 1; }
  CU_BEFORE_VERSIONS="$(wc -l < "$before_journal" | tr -d ' ')"
  CU_PRIOR_SCHEMA="$(cu_schema)" || CU_PRIOR_SCHEMA=""
  CU_BEFORE_COUNTS="$(cu_counts "$before_sums")"
  [ "$CU_BEFORE_COUNTS" = "$SEED_COUNTS_EXPECTED" ] || { cu_fail "seed did not produce the deterministic rows ($CU_BEFORE_COUNTS)"; return 1; }

  echo "== $CU_NAME: rollback-target guard probe and mandatory pre-upgrade backup =="
  cu_guard_probe || { cu_fail "prior did not return to ready after the rollback-target guard probe was removed"; return 1; }
  echo "ROLLBACK-TARGET GUARD PROBE: prior guard $CU_GUARD"
  if compose exec -T db pg_dump -U honua -d honua -Fc > "$CU_BACKUP" 2> "$OUT/compose-backup.log" && [ -s "$CU_BACKUP" ]; then
    CU_BACKUP_TAKEN=true
  else
    cu_fail "mandatory pre-upgrade backup (pg_dump) failed; the documented recovery path is unavailable"; return 1
  fi

  echo "== $CU_NAME: upgrade to candidate $upgrade_image =="
  stop_server
  cu_start_server "$upgrade_image"
  if ! cu_wait_ready "${CHAOS_READY_ATTEMPTS:-90}"; then
    compose logs server > "$OUT/compose-contract-gate.log" 2>&1 || true
    CU_NONCE="$(grep -hoE 'HONUA_APPROVE_CONTRACT_MIGRATIONS=[0-9a-f]{8,64}' "$OUT/compose-contract-gate.log" | head -n 1 | cut -d= -f2)" || CU_NONCE=""
    CU_GATED="$(sed -n 's/.*pending contract-phase migration(s) on an existing database: \(.*[.]sql\)[.] These reviewed.*/\1/p' "$OUT/compose-contract-gate.log" | head -n 1)" || CU_GATED=""
    if [ -z "$CU_NONCE" ]; then
      cu_fail "candidate did not become ready after the upgrade and printed no contract-migration nonce"; return 1
    fi
    CU_APPROVAL=contract-approval-required
    echo "CONTRACT-APPROVAL-REQUIRED (expected customer step): nonce $CU_NONCE for: $CU_GATED"
    printf 'services:\n  server:\n    environment:\n      HONUA_APPROVE_CONTRACT_MIGRATIONS: "%s"\n' "$CU_NONCE" > "$CU_APPROVAL_OVERRIDE"
    stop_server
    cu_start_server "$upgrade_image" "$CU_APPROVAL_OVERRIDE"
    if ! wait_ready; then
      CU_APPROVAL=approved-upgrade-failed
      cu_fail "candidate did not become ready after the contract migrations were approved"; return 1
    fi
    CU_APPROVAL=approved-and-applied
  fi
  if [ "$COMPOSE_FAULT" = drop-seed-rows ]; then
    db_exec 'DELETE FROM honua_data.e2e_src_fs WHERE ctid IN (SELECT ctid FROM honua_data.e2e_src_fs LIMIT 1)' >/dev/null 2>&1 || true
    echo "FAULT INJECTED: drop-seed-rows deleted one honua_data.e2e_src_fs row during the upgrade"
  fi
  capture_state "$after_journal" "$after_sums" || { cu_fail "could not capture the upgraded state"; return 1; }
  CU_AFTER_VERSIONS="$(wc -l < "$after_journal" | tr -d ' ')"
  CU_CANDIDATE_SCHEMA="$(cu_schema)" || CU_CANDIDATE_SCHEMA=""
  CU_AFTER_COUNTS="$(cu_counts "$after_sums")"
  cmp -s "$before_sums" "$after_sums" && CU_CHECKSUMS_MATCHED=true || CU_CHECKSUMS_MATCHED=false
  diff -u "$before_journal" "$after_journal" > "$OUT/compose-journal-advance.diff" || true
  cu_identity candidate "$upgrade_image" || { cu_fail "running candidate container is not exactly $upgrade_image on $COMPOSE_PLATFORM"; return 1; }
  # A later image can ship the same migrations. That is a real upgrade of the server,
  # and the journal holding is the proof. journal-stall reruns the prior image, so a
  # held journal there is still a failure. A missing or replaced prior entry never passes.
  local missing_prior
  missing_prior="$(comm -23 "$before_journal" "$after_journal")"
  if [ -n "$missing_prior" ] || [ "$CU_AFTER_VERSIONS" -lt "$CU_BEFORE_VERSIONS" ] \
     || { [ "$CU_AFTER_VERSIONS" -eq "$CU_BEFORE_VERSIONS" ] && [ "$upgrade_image" = "$PRIOR_IMAGE" ]; }; then
    cu_fail "candidate did not advance public.schema_versions ($CU_BEFORE_VERSIONS -> $CU_AFTER_VERSIONS rows; every prior entry must remain)"; return 1
  fi
  if [ "$CU_AFTER_VERSIONS" -gt "$CU_BEFORE_VERSIONS" ]; then
    journal_phrase="schema_versions advanced $CU_BEFORE_VERSIONS->$CU_AFTER_VERSIONS"
  else
    journal_phrase="schema_versions held at $CU_AFTER_VERSIONS (candidate image ships no new migration)"
  fi
  if [ -n "$CANDIDATE_DB_SCHEMA" ] && [ "$CU_CANDIDATE_SCHEMA" != "$CANDIDATE_DB_SCHEMA" ]; then
    cu_fail "observed schema $CU_CANDIDATE_SCHEMA does not exactly match the declared candidate schema $CANDIDATE_DB_SCHEMA"; return 1
  fi
  if [ "$CU_AFTER_COUNTS" != "$CU_BEFORE_COUNTS" ] || [ "$CU_CHECKSUMS_MATCHED" != true ]; then
    cu_fail "seeded rows did not survive the upgrade ($CU_BEFORE_COUNTS -> $CU_AFTER_COUNTS, checksums matched: $CU_CHECKSUMS_MATCHED)"; return 1
  fi
  echo "UPGRADE VERIFIED: $journal_phrase, schema $CU_PRIOR_SCHEMA -> $CU_CANDIDATE_SCHEMA, seeded rows $CU_AFTER_COUNTS (checksums matched)"

  echo "== $CU_NAME: roll back to the prior image on the migrated database =="
  stop_server
  if [ "$COMPOSE_FAULT" = rollback-unreadable ]; then
    db_exec 'ALTER TABLE honua_data.e2e_src_fs RENAME TO e2e_src_fs_unreadable' >/dev/null 2>&1 || true
    echo "FAULT INJECTED: rollback-unreadable renamed honua_data.e2e_src_fs before the prior image returned"
  fi
  cu_start_server "$PRIOR_IMAGE"
  if cu_wait_ready "${CHAOS_ROLLBACK_READY_ATTEMPTS:-60}"; then
    cu_identity rollback "$PRIOR_IMAGE" || { cu_fail "rolled-back container is not exactly the prior image $PRIOR_IMAGE"; return 1; }
    if [ "$CU_GUARD" = absent ]; then
      CU_BOUNDARY=rollback-target-unguarded
      cu_serves_seed "$OUT/compose-rollback-silent-query.json" && CU_SERVED_SILENTLY=true || CU_SERVED_SILENTLY=false
      CU_RESTORE_VERIFIED=false
      cu_restore_backup && cu_serves_seed "$OUT/compose-rollback-query.json" && CU_RESTORE_VERIFIED=true
      CU_ROLLBACK_OUTCOME=rollback-target-ineligible
      cu_fail "rollback-target-ineligible: prior (schema $CU_PRIOR_SCHEMA) has no schema-reader guard (it started on an unrecognised journal entry; honua-server#5333, #6484), so an image-only rollback to it cannot be certified (it served silently: $CU_SERVED_SILENTLY); certified rollback path: restore the pre-upgrade backup (verified: $CU_RESTORE_VERIFIED)"
      return 1
    fi
    if [ "$CU_GUARD" != present ]; then
      # Only a typed schema_reader_incompatible refusal of the probe entry proves the guard. A probe
      # that failed for another reason (inconclusive) and then recovered proves nothing, so an
      # image-only rollback to this prior is not certified: real telemetry or no pass.
      CU_ROLLBACK_OUTCOME=rollback-target-guard-unproven
      cu_fail "rollback-target-guard-unproven: the rollback-target guard probe did not observe a typed schema_reader_incompatible refusal from the prior (guard $CU_GUARD; see compose-guard-probe.log), so an image-only rollback to it cannot be certified"
      return 1
    fi
    if [ -n "$CU_NONCE" ]; then
      CU_ROLLBACK_OUTCOME=guarded-prior-served-across-contract
      cu_fail "prior image (guard $CU_GUARD) started on a schema narrowed by approved contract migrations instead of refusing with schema_reader_incompatible"
      return 1
    fi
    if ! cu_serves_seed "$OUT/compose-rollback-query.json"; then
      CU_ROLLBACK_OUTCOME=image-rollback-did-not-serve
      cu_fail "prior image could not serve the seeded rows after rollback (count ${CU_ROLLBACK_COUNT:-none}, expected 2)"
      return 1
    fi
    CU_ROLLBACK_OUTCOME=image-rollback-served
  else
    compose logs server > "$OUT/compose-rollback-prior.log" 2>&1 || true
    # The reason sits past the migration name. A 200-character cap hid
    # "declared reader-incompatible, not expand" on migration 142.
    refusal="$(grep -hoE 'schema_reader_incompatible[^"]*' "$OUT/compose-rollback-prior.log" | head -n 1)" || refusal=""
    CU_ROLLBACK_OUTCOME=image-rollback-refused
    if [[ "$refusal" == schema_reader_incompatible* ]]; then
      if [ -n "$CU_NONCE" ]; then CU_BOUNDARY=contract-migrations-approved
      elif [[ "$CU_PRIOR_SCHEMA" =~ ^[0-9]+$ ]] && [ "$CU_PRIOR_SCHEMA" -lt "$READER_EXPAND_FLOOR" ]; then CU_BOUNDARY="prior-schema-$CU_PRIOR_SCHEMA-below-$READER_EXPAND_FLOOR"
      elif [[ "$refusal" == *"declared reader-incompatible, not expand"* ]]; then CU_BOUNDARY=reader-incompatible-declaration
      fi
    fi
    if [ -z "$CU_BOUNDARY" ]; then
      cu_fail "prior image refused the migrated schema outside the documented boundary: ${refusal:-no typed schema_reader_incompatible refusal}"
      return 1
    fi
    echo "DOCUMENTED ROLLBACK BOUNDARY ($CU_BOUNDARY): restoring the pre-upgrade backup"
    CU_RESTORE_VERIFIED=false
    if ! cu_restore_backup || ! cu_serves_seed "$OUT/compose-rollback-query.json"; then
      CU_ROLLBACK_OUTCOME=restore-did-not-recover
      cu_fail "documented boundary ($CU_BOUNDARY) but restoring the pre-upgrade backup did not bring the prior back serving the seeded rows"
      return 1
    fi
    CU_RESTORE_VERIFIED=true
    CU_ROLLBACK_OUTCOME=restored-from-pre-upgrade-backup
    cu_identity rollback "$PRIOR_IMAGE" || { cu_fail "restored container is not exactly the prior image $PRIOR_IMAGE"; return 1; }
  fi
  if ! capture_state "$rollback_journal" "$rollback_sums" || ! cmp -s "$before_sums" "$rollback_sums"; then
    CU_ROLLBACK_COUNTS="$(cu_counts "$rollback_sums" 2>/dev/null)"
    cu_fail "seeded rows changed across the rollback ($CU_BEFORE_COUNTS -> ${CU_ROLLBACK_COUNTS:-unreadable})"
    return 1
  fi
  CU_ROLLBACK_COUNTS="$(cu_counts "$rollback_sums")"
  cu_write_observed
  record "$CU_NAME" pass "$journal_phrase (schema $CU_PRIOR_SCHEMA->$CU_CANDIDATE_SCHEMA, contract approval: $CU_APPROVAL); seeded rows survived ($CU_AFTER_COUNTS, checksums matched); rollback $CU_ROLLBACK_OUTCOME${CU_BOUNDARY:+ at $CU_BOUNDARY}: the prior image served 2 seeded features and the seeded checksums are unchanged"
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
if [ "$SCENARIO" = compose-upgrade-rollback ]; then
  # x86_64 only (2026-10-25 re-tier): every service of this run is the linux/amd64 image.
  export DOCKER_DEFAULT_PLATFORM="$COMPOSE_PLATFORM"
elif [ "$SKIP_PREPARE" = true ]; then
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
  compose-upgrade-rollback) run_compose_upgrade_rollback || failures=$((failures + 1)) ;;
  *) die "unknown CHAOS_SCENARIO: $SCENARIO" ;;
esac

jq --argjson failures "$failures" '. + {failures:$failures, generatedAt:(now | todateiso8601)}' "$RESULTS" > "$RESULTS.tmp"
mv "$RESULTS.tmp" "$RESULTS"
cat "$RESULTS"
[ "$failures" = 0 ]
