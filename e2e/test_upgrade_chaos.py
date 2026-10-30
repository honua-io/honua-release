"""Static contract tests for the real packet-94 database-upgrade chaos driver."""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DRIVER = REPO_ROOT / "e2e" / "harness" / "upgrade-chaos.sh"

SCENARIOS = {
    "migration-kill-every-boundary",
    "image-rollback",
    "concurrent-app-start",
    "partial-migration-failure",
    "journal-schema-divergence",
    "migration-rerun-idempotency",
}


def test_upgrade_chaos_driver_is_executable_and_has_all_required_scenarios():
    assert DRIVER.stat().st_mode & 0o111, "the operator driver must be directly runnable"
    text = DRIVER.read_text(encoding="utf-8")
    for scenario in SCENARIOS:
        assert scenario in text, scenario


def test_boundary_probe_kills_the_real_server_and_requires_convergence():
    text = DRIVER.read_text(encoding="utf-8")
    assert "compose kill -s SIGKILL server" in text
    assert "migration_names" in text
    assert "assert_state \"$name\"" in text
    assert "migration journal diverged" in text
    assert "seeded data checksum/count changed" in text


def test_driver_fails_closed_on_missing_inputs_and_unproved_divergence():
    text = DRIVER.read_text(encoding="utf-8")
    assert "HONUA_PRIOR_SERVER_IMAGE is required" in text
    assert "HONUA_CANDIDATE_SERVER_IMAGE is required" in text
    assert "server became ready after journaled layers schema was deleted" in text
    assert "never observed migration boundary" in text
    assert "[ \"$failures\" = 0 ]" in text


def test_partial_failure_can_pass_only_through_the_paused_candidate_runner():
    # A hand-written psql transaction only proves PostgreSQL rollback, not application migration
    # atomicity (honua-io/honua-release#440). The only pass is assert_state at the end of the probe
    # that paused the candidate's own runner; no line records this scenario as pass directly.
    text = DRIVER.read_text(encoding="utf-8")
    probe = text.split("partial_failure_probe() (", 1)[1].split("\n)\n", 1)[0]
    assert "LOCK TABLE public.schema_versions IN SHARE MODE" in probe
    assert "compose up -d --force-recreate server" in probe
    assert probe.count('assert_state "$name"') == 1
    assert probe.rstrip().splitlines()[-1].lstrip().startswith('assert_state "$name"')
    assert "pg_sleep" not in probe.split("compose up -d --force-recreate server", 1)[1]
    for line in text.splitlines():
        assert not ("partial-migration-failure" in line and "record" in line and " pass " in line), line


def test_scenario_failures_propagate_to_the_matrix_exit_code():
    text = DRIVER.read_text(encoding="utf-8")
    assert "local failed=0" in text
    assert "failed=1" in text
    assert "if ! assert_state concurrent-app-start" in text


# Execute the real Bash functions with controlled external boundaries. These are driver
# regressions, not substitutes for the separately dispatched real-image chaos matrix.
import json
import os
import subprocess

import pytest


def run_driver(tmp_path, commands, **env):
    definitions = DRIVER.read_text().split("\ntrap cleanup EXIT\n", 1)[0]
    return subprocess.run(
        ["bash", "-c", "source /dev/stdin"], input=definitions + "\n" + commands, text=True,
        capture_output=True, cwd=REPO_ROOT,
        env={**os.environ, "E2E_OUT": str(tmp_path),
             "E2E_COMPOSE_FILE": str(DRIVER.parent / "compose.candidate.yml"), **env}, timeout=10,
    )


@pytest.mark.parametrize("image,valid", [
    ("registry/server:latest", False),
    ("registry/server@sha256:abc", False),
    ("registry/server@sha256:" + "a" * 64, True),
])
def test_images_require_digest_coordinates(tmp_path, image, valid):
    result = run_driver(tmp_path, "require_tools", HONUA_PRIOR_SERVER_IMAGE=image,
                        HONUA_CANDIDATE_SERVER_IMAGE=image)
    assert (result.returncode == 0) == valid, result.stderr
    if valid:
        assert image in (tmp_path / "images.json").read_text()


def test_boundary_search_cannot_see_old_container_logs(tmp_path):
    result = run_driver(tmp_path, r'''
restore_baseline() { :; }
compose() {
  case "$*" in
    'up -d --force-recreate server') fresh=1 ;;
    'logs server') [ "${fresh:-0}" = 1 ] || echo 'Executing Database Server script migration.sql' ;;
    'kill -s SIGKILL server') echo 'unexpected kill'; exit 10 ;;
  esac
  return 0
}
sleep() { :; }
kill_at_boundary migration.sql
''', CHAOS_BOUNDARY_ATTEMPTS="1")
    assert result.returncode != 0
    assert "never observed migration boundary" in result.stdout
    assert "unexpected kill" not in result.stdout


@pytest.mark.parametrize("mode,passed", [
    ("both", True), ("one", False), ("failure", False),
    ("creation-failure", False), ("journal-failure", False), ("state-failure", False),
])
def test_concurrent_requires_both_healthy_and_always_removes_containers(tmp_path, mode, passed):
    result = run_driver(tmp_path, r'''
restore_baseline() { :; }
db_exec() { [[ "$1" != "SELECT count(*) FROM pg_stat_activity"* ]] || echo 0; }
sleep() { :; }
compose() { [ "$MODE" != creation-failure ]; }
docker() {
  case "$1" in
    rm) echo "$*" >> "$OUT/removed" ;;
    logs) [ "$MODE" != failure ] || echo 'migration failed' ;;
    inspect)
      if [ "$MODE" = one ] && [[ "${*: -1}" = *_b ]]; then
        echo 'true starting'
      else
        echo 'true healthy'
      fi ;;
  esac
  return 0
}
wait_for_journal() { [ "$MODE" != journal-failure ]; }
assert_state() { [ "$MODE" != state-failure ]; }
run_concurrent_start
''', MODE=mode, CHAOS_READY_ATTEMPTS="2")
    assert (result.returncode == 0) == passed, result.stderr
    removals = (tmp_path / "removed").read_text().splitlines()
    assert len(removals) == 2  # Initial stale cleanup plus EXIT on every path.
    assert all("_chaos_app_a" in line and "_chaos_app_b" in line for line in removals)


def test_saved_seed_layer_is_loaded_and_validated(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    manifest = seed / "seed-manifest.json"
    manifest.write_text('{"slice1":{"e2e_src_fs":{"layerId":9}}}')
    result = run_driver(tmp_path, 'load_seed_layer; echo "$SRC_LAYER_ID"')
    assert result.returncode == 0 and result.stdout.strip() == "9"
    manifest.write_text('{"slice1":{"e2e_src_fs":{"layerId":"bad"}}}')
    assert run_driver(tmp_path, "load_seed_layer").returncode != 0
    reuse = DRIVER.read_text().split('if [ "$SKIP_PREPARE" = true ]; then')[1]
    assert "load_seed_layer" in reuse.split("else", 1)[0]


def test_divergence_rejects_readiness_after_old_short_window(tmp_path):
    result = run_driver(tmp_path, r'''
restore_baseline() { :; }
compose() { :; }
db_exec() { :; }
stop_server() { :; }
sleep() { :; }
ready_calls=0
server_ready() {
  ready_calls=$((ready_calls + 1))
  [ "$ready_calls" = 1 ] || [ "$ready_calls" -ge 15 ]
}
run_divergence
''', CHAOS_READY_ATTEMPTS="20")
    assert result.returncode != 0
    assert "server became ready after journaled layers schema was deleted" in result.stdout


def test_cleanup_uses_database_namespace_even_when_keeping_stack(tmp_path):
    result = run_driver(tmp_path, r'''
PARTIAL_BACKEND_PID=1234
PARTIAL_JOB_PID=5678
kill() { echo "host kill $*" >> "$OUT/cleanup"; }
db_exec() { echo "$*" >> "$OUT/cleanup"; }
stack_down() { echo unexpected >> "$OUT/cleanup"; }
cleanup
''', CHAOS_KEEP_STACK="true")
    assert result.returncode == 0
    output = (tmp_path / "cleanup").read_text()
    assert "pg_terminate_backend(1234)" in output
    assert "host kill 5678" in output
    assert "host kill 1234" not in output and "unexpected" not in output


@pytest.mark.parametrize("schema,passed", [("expected", True), ("missing index", False)])
def test_matching_journal_and_rows_cannot_hide_schema_damage(tmp_path, schema, passed):
    (tmp_path / "expected-journal.txt").write_text("migration.sql\n")
    (tmp_path / "expected-checksums.txt").write_text("rows\n")
    (tmp_path / "expected-schema.sql").write_text("expected\n")
    result = run_driver(tmp_path, r'''
capture_state() { cp "$EXPECTED_JOURNAL" "$1"; cp "$EXPECTED_CHECKSUMS" "$2"; }
capture_schema() { echo "$RECOVERED_SCHEMA" > "$1"; }
assert_state boundary
''', RECOVERED_SCHEMA=schema)
    assert (result.returncode == 0) == passed, result.stderr
    if not passed:
        assert "migrated schema diverged" in result.stdout


def test_schema_capture_normalizes_only_random_dump_tokens(tmp_path):
    result = run_driver(tmp_path, r'''
compose() { printf '\\restrict random\nCREATE TABLE t (id integer);\n\\unrestrict random\n'; }
capture_schema "$OUT/schema.sql"
''')
    assert result.returncode == 0
    assert (tmp_path / "schema.sql").read_text() == "CREATE TABLE t (id integer);\n"


def test_assert_state_evidence_paths_are_safe_for_github_artifacts(tmp_path):
    # upload-artifact rejects " : < > | * ? CR LF in any path. Scenario ids in the
    # receipt keep the colon; only the evidence filename changes.
    (tmp_path / "expected-journal.txt").write_text("migration.sql\n")
    (tmp_path / "expected-checksums.txt").write_text("rows\n")
    (tmp_path / "expected-schema.sql").write_text("expected\n")
    label = "partial-migration-failure:backend-terminated"
    nasty = 'q"c:l<t>p|s*a?r\r\n'
    result = run_driver(tmp_path, r'''
capture_state() { cp "$EXPECTED_JOURNAL" "$1"; cp "$EXPECTED_CHECKSUMS" "$2"; }
capture_schema() { cp "$EXPECTED_SCHEMA" "$1"; }
assert_state "$LABEL" "converged"
printf 'STEM:%s\n' "$(evidence_stem "$NASTY")"
''', LABEL=label, NASTY=nasty)
    assert result.returncode == 0, result.stdout + result.stderr
    forbidden = set('" :<>|*?\r\n')
    evidence = [
        path for path in tmp_path.iterdir()
        if path.name.startswith("partial-migration-failure")
    ]
    assert [path.name for path in sorted(evidence)] == [
        "partial-migration-failure_backend-terminated-checksums.txt",
        "partial-migration-failure_backend-terminated-journal.txt",
        "partial-migration-failure_backend-terminated-schema.sql",
    ]
    for path in evidence:
        assert not (forbidden & set(path.name)), path.name
    assert "STEM:q_c_l_t_p_s_a_r__" in result.stdout
    matrix = json.loads((tmp_path / "scenario-matrix.json").read_text())
    assert matrix["scenarios"][-1]["scenario"] == label
    assert "[PASS   ] partial-migration-failure:backend-terminated" in result.stdout


def test_partial_failure_never_passes_when_postgres_only_succeeds(tmp_path):
    # Every database and compose call succeeds, as it would when PostgreSQL rollback works but no
    # candidate runner was ever observed paused inside a migration: vacuous, so never pass.
    result = run_driver(tmp_path, r"""
restore_baseline() { :; }
migration_names() { echo m2.sql; }
db_exec() { echo 0; }
compose() { :; }
sleep() { :; }
run_partial_failure
""", CHAOS_PARTIAL_ATTEMPTS="2")
    assert result.returncode != 0
    scenarios = json.loads((tmp_path / "scenario-matrix.json").read_text())["scenarios"]
    assert [s["status"] for s in scenarios] == ["blocked", "blocked"]
    assert all(s["status"] != "pass" for s in scenarios)


# A controlled candidate runner: it pauses inside pending migration m2.sql (xid + write locks) behind
# the harness's journal lock. FAULT injects a broken recovery after the interruption.
PARTIAL_STUB = r"""
for f in journal checksums schema; do printf 'pre-%s\n' "$f" > "$OUT/live-$f"; printf 'post-%s\n' "$f" > "$OUT/expected-$f.txt"; done
mv "$OUT/expected-schema.txt" "$OUT/expected-schema.sql"
printf 'post-journal\nm2.sql\n' > "$OUT/expected-journal.txt"
restore_baseline() { for f in journal checksums schema; do printf 'pre-%s\n' "$f" > "$OUT/live-$f"; done; rm -f "$OUT/ups" "$OUT/interrupted"; }
migration_names() { echo m2.sql; }
capture_state() { cp "$OUT/live-journal" "$1"; cp "$OUT/live-checksums" "$2"; }
capture_schema() { cp "$OUT/live-schema" "$1"; }
sleep() { :; }
interrupt() {
  touch "$OUT/interrupted"
  case "$FAULT" in
    journaled) echo m2.sql >> "$OUT/live-journal" ;;
    partial-schema) echo 'CREATE TABLE honua.half_done ();' >> "$OUT/live-schema" ;;
    rows) echo changed >> "$OUT/live-checksums" ;;
  esac
}
db_exec() {
  case "$1" in
    *"LOCK TABLE public.schema_versions IN SHARE MODE"*) echo "$1" >> "$OUT/holder-sql" ;;
    *"l.mode = 'ShareLock'"*) echo 41 ;;
    *pg_blocking_pids*)
      [ -f "$OUT/ups" ] || return 0
      case "$FAULT" in no-xid) echo '77|false|0' ;; never-paused) ;; *) echo '77|true|3' ;; esac ;;
    *"pg_terminate_backend(77)"*) interrupt; echo t ;;
    *pg_terminate_backend*) echo t ;;
    *"count(*) FROM pg_stat_activity"*) echo 0 ;;
  esac
  return 0
}
compose() {
  case "$*" in
    'up -d --force-recreate server')
      echo up >> "$OUT/ups"
      if [ "$(wc -l < "$OUT/ups")" -gt 1 ] && [ "$FAULT" != no-converge ]; then
        cp "$OUT/expected-journal.txt" "$OUT/live-journal"; cp "$OUT/expected-checksums.txt" "$OUT/live-checksums"
        cp "$OUT/expected-schema.sql" "$OUT/live-schema"
      fi ;;
    'kill -s SIGKILL server')
      [ "$FAULT" = kill-fails ] && return 1
      interrupt ;;
    'ps -q --status running server') [ "$FAULT" = kill-ignored ] && echo c0ffee ;;
    'logs server')
      echo "Executing Database Server script 'm1.sql'"
      echo "Executing Database Server script 'm2.sql'"
      if [ -f "$OUT/interrupted" ] && [ "$FAULT" != silent ]; then echo 'Database migration failed'; fi ;;
  esac
  return 0
}
stop_server() { :; }
server_ready() { [ "$FAULT" = ready-after-terminate ]; }
wait_ready() { [ "$FAULT" != retry-unready ]; }
"""


def test_partial_failure_passes_when_the_runner_rolls_back_and_retries(tmp_path):
    result = run_driver(tmp_path, PARTIAL_STUB + "run_partial_failure\n", FAULT="none",
                        CHAOS_PARTIAL_ATTEMPTS="2")
    assert result.returncode == 0, result.stdout + result.stderr
    scenarios = json.loads((tmp_path / "scenario-matrix.json").read_text())["scenarios"]
    assert [(s["scenario"], s["status"]) for s in scenarios] == [
        ("partial-migration-failure:backend-terminated", "pass"),
        ("partial-migration-failure:server-killed", "pass"),
    ]
    assert all("'m2.sql'" in s["why"] for s in scenarios)
    assert "LOCK TABLE public.schema_versions IN SHARE MODE" in (tmp_path / "holder-sql").read_text()


@pytest.mark.parametrize("fault,status,why", [
    ("journaled", "fail", "interrupted migration 'm2.sql' was journaled"),
    ("partial-schema", "fail", "partial schema from interrupted 'm2.sql' survived"),
    ("rows", "fail", "seeded rows changed after interrupting 'm2.sql'"),
    ("no-converge", "fail", "diverged after restart"),
    ("retry-unready", "fail", "candidate did not become ready when retrying"),
    ("no-xid", "blocked", "never observed the candidate runner paused"),
    ("never-paused", "blocked", "never observed the candidate runner paused"),
])
def test_partial_failure_catches_broken_recovery(tmp_path, fault, status, why):
    result = run_driver(tmp_path, PARTIAL_STUB + "run_partial_failure\n", FAULT=fault,
                        CHAOS_PARTIAL_ATTEMPTS="2")
    assert result.returncode != 0, result.stdout
    scenarios = json.loads((tmp_path / "scenario-matrix.json").read_text())["scenarios"]
    assert len(scenarios) == 2 and all(s["status"] == status for s in scenarios), scenarios
    assert all(why in s["why"] for s in scenarios), scenarios


@pytest.mark.parametrize("fault,why", [
    ("ready-after-terminate", "server reported ready after its migration transaction was terminated"),
    ("silent", "runner emitted no migration failure"),
])
def test_terminated_runner_must_own_its_failure(tmp_path, fault, why):
    result = run_driver(tmp_path, PARTIAL_STUB + 'partial_failure_probe backend-terminated\n',
                        FAULT=fault, CHAOS_PARTIAL_ATTEMPTS="2")
    assert result.returncode != 0
    scenarios = json.loads((tmp_path / "scenario-matrix.json").read_text())["scenarios"]
    assert scenarios == [{"scenario": "partial-migration-failure:backend-terminated",
                          "status": "fail", "why": scenarios[0]["why"]}]
    assert why in scenarios[0]["why"]


@pytest.mark.parametrize("fault", ["kill-fails", "kill-ignored"])
def test_server_killed_requires_a_successful_sigkill_that_stops_the_server(tmp_path, fault):
    result = run_driver(tmp_path, PARTIAL_STUB + 'partial_failure_probe server-killed\n',
                        FAULT=fault, CHAOS_PARTIAL_ATTEMPTS="2", CHAOS_KILL_ATTEMPTS="2")
    assert result.returncode != 0, result.stdout
    scenarios = json.loads((tmp_path / "scenario-matrix.json").read_text())["scenarios"]
    assert scenarios == [{"scenario": "partial-migration-failure:server-killed", "status": "fail",
                          "why": "SIGKILL of the paused candidate runner failed or the server kept running"}]


@pytest.mark.parametrize("kill_status,running,passed", [
    (0, "", True), (1, "", False), (0, "c0ffee", False),
])
def test_boundary_kill_requires_a_successful_sigkill_that_stops_the_server(tmp_path, kill_status, running, passed):
    result = run_driver(tmp_path, rf'''
restore_baseline() {{ :; }}
compose() {{
  case "$*" in
    'logs server') echo 'Executing Database Server script migration.sql' ;;
    'kill -s SIGKILL server') return {kill_status} ;;
    'ps -q --status running server') echo '{running}' | grep . ;;
  esac
  return 0
}}
sleep() {{ :; }}
wait_ready() {{ :; }}
assert_state() {{ :; }}
kill_at_boundary migration.sql
''', CHAOS_BOUNDARY_ATTEMPTS="1", CHAOS_KILL_ATTEMPTS="2")
    assert (result.returncode == 0) is passed, result.stdout + result.stderr
    if not passed:
        assert "SIGKILL at migration boundary 'migration.sql' failed or the server kept running" in result.stdout


def test_paused_script_must_be_a_pending_candidate_migration(tmp_path):
    result = run_driver(tmp_path, PARTIAL_STUB + 'migration_names() { echo other.sql; }\n'
                        'partial_failure_probe server-killed\n', FAULT="none", CHAOS_PARTIAL_ATTEMPTS="2")
    assert result.returncode != 0
    assert "paused script 'm2.sql' is not a pending candidate migration" in result.stdout


@pytest.mark.parametrize("historical", [True, False])
def test_concurrent_cleanup_is_owned_under_injected_child_exit(tmp_path, historical):
    # This immutable original demonstrates the ownership defect under deliberate
    # EXIT-handler inheritance. It is NOT a reproduction of the hosted fork/
    # signal race, whose original observation remains the retained 3-rm failure.
    # Source: c83e72cbe63ce22e838c89e23741ed632c22f1ff, driver blob
    # 0a9d196c999668e997109d718c1590af8e268ff0, run_concurrent_start only.
    import hashlib
    original = REPO_ROOT / "e2e" / "fixtures" / "upgrade-concurrent-start-before-712.sh"
    assert hashlib.sha256(original.read_bytes()).hexdigest() == "34e04542c4cf899c5a7a20ea409a551d2863c760ca2d84cd138f80b0be2b205c"
    commands = ('source "$ORIGINAL_CONCURRENT_FIXTURE"\n' if historical else "") + r'''
restore_baseline() { :; }
db_exec() { :; }
sleep() { :; }
compose() {
  printf '%s\n' "$BASHPID" > "$OUT/owner-pid"
  trap -p EXIT > "$OUT/registered-exit-trap"
  (
    # Transfer the actual registered handler into a different Bash PID and
    # exercise EXIT. This forces ownership independently of fork scheduling.
    source "$OUT/registered-exit-trap"
    printf '%s\n' "$BASHPID" > "$OUT/injected-child-pid"
    exit 1
  )
  return 1
}
docker() {
  if [ "$1" = rm ]; then
    printf '%s %s %s\n' "$BASHPID" "$BASH_SUBSHELL" "$*" >> "$OUT/removed"
  fi
  return 0
}
run_concurrent_start
'''
    result = run_driver(tmp_path, commands, ORIGINAL_CONCURRENT_FIXTURE=str(original))
    assert result.returncode != 0
    owner = (tmp_path / "owner-pid").read_text().strip()
    child = (tmp_path / "injected-child-pid").read_text().strip()
    assert owner != child
    removals = (tmp_path / "removed").read_text().splitlines()
    pids = [line.split(" ", 1)[0] for line in removals]
    if historical:
        assert len(removals) >= 3 and child in pids, removals
    else:
        assert len(removals) == 2 and pids == [owner, owner], removals
    assert all("_chaos_app_a" in line and "_chaos_app_b" in line for line in removals)


def test_concurrent_creation_failure_waits_for_holder_exit_before_return(tmp_path):
    result = run_driver(tmp_path, r'''
restore_baseline() { :; }
db_exec() {
  if [[ "$1" == "SELECT count(*) FROM pg_stat_activity"* ]]; then echo 0; return 0; fi
  if [[ "$1" == *pg_terminate_backend* ]]; then
    printf '%s\n' "$1" > "$OUT/backend-termination"
    kill "$(cat "$OUT/holder-pid")"
    return 0
  fi
  trap -p EXIT > "$OUT/holder-inherited-exit"
  trap 'command sleep 0.1; printf exited > "$OUT/holder-exited"' EXIT
  trap 'exit 0' TERM
  printf '%s\n' "$BASHPID" > "$OUT/holder-pid"
  while :; do command sleep 0.01; done
}
sleep() {
  while [ ! -s "$OUT/holder-pid" ]; do command sleep 0.01; done
}
compose() { return 1; }
docker() {
  [ "$1" != rm ] || printf '%s\n' "$*" >> "$OUT/removed"
  return 0
}
if run_concurrent_start; then exit 9; fi
[ -f "$OUT/holder-exited" ] || exit 8
[ ! -s "$OUT/holder-inherited-exit" ] || exit 7
if kill -0 "$(cat "$OUT/holder-pid")" 2>/dev/null; then exit 6; fi
printf holder-completed-before-return
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout == "holder-completed-before-return"
    removals = (tmp_path / "removed").read_text().splitlines()
    assert len(removals) == 2


def test_concurrent_creation_failure_waits_for_external_holder_child(tmp_path):
    result = run_driver(tmp_path, r'''
restore_baseline() { :; }
db_exec() {
  if [[ "$1" == "SELECT count(*) FROM pg_stat_activity"* ]]; then echo 0; return 0; fi
  if [[ "$1" == *pg_terminate_backend* ]]; then
    printf '%s\n' "$1" > "$OUT/backend-termination"
    kill "$(cat "$OUT/external-pid")"
    return 0
  fi
  printf '%s\n' "$1" > "$OUT/holder-query"
  bash -c '
    trap '"'"'command sleep 0.1; printf completed > "$OUT/external-exited"'"'"' EXIT
    trap '"'"'exit 0'"'"' TERM
    printf "%s\n" "$BASHPID" > "$OUT/external-pid"
    while :; do command sleep 0.01; done
  '
  printf reaped > "$OUT/external-reaped"
}
sleep() {
  while [ ! -s "$OUT/external-pid" ]; do command sleep 0.01; done
}
compose() { return 1; }
docker() {
  [ "$1" != rm ] || printf '%s\n' "$*" >> "$OUT/removed"
  return 0
}
export OUT
if run_concurrent_start; then exit 9; fi
[ -f "$OUT/external-exited" ] && [ -f "$OUT/external-reaped" ] || exit 8
if kill -0 "$(cat "$OUT/external-pid")" 2>/dev/null; then exit 6; fi
printf external-child-completed-before-return
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout == "external-child-completed-before-return"
    holder_query = (tmp_path / "holder-query").read_text()
    termination = (tmp_path / "backend-termination").read_text()
    import re
    name = re.search(r"SET application_name = '(honua-chaos-[0-9a-f]{32})'", holder_query).group(1)
    assert f"application_name = '{name}'" in termination
    assert "datname = current_database()" in termination
    assert "usename = current_user" in termination
    assert "pid <> pg_backend_pid()" in termination
    assert len((tmp_path / "removed").read_text().splitlines()) == 2


@pytest.mark.parametrize("backend,passed", [("absent",True),("still-present",False),("observation-failed",False)])
def test_concurrent_cleanup_requires_observed_backend_absence(tmp_path,backend,passed):
    result = run_driver(tmp_path,r'''
restore_baseline() { :; }
db_exec() {
  if [[ "$1" == *pg_terminate_backend* ]]; then echo f; return 0; fi
  if [[ "$1" == "SELECT count(*) FROM pg_stat_activity"* ]]; then
    [ "$BACKEND" != observation-failed ] || return 1
    if [ "$BACKEND" = absent ]; then echo 0; else echo 1; fi
  fi
}
sleep() { :; }
compose() { return 0; }
docker() {
  case "$1" in
    rm) printf '%s\n' "$*" >> "$OUT/removed" ;;
    inspect) echo 'true healthy' ;;
  esac
  return 0
}
wait_for_journal() { return 0; }
assert_state() { return 0; }
run_concurrent_start
''',BACKEND=backend,CHAOS_READY_ATTEMPTS="1")
    # SQL command success with a false termination result is insufficient;
    # only an independently observed absence can qualify completed cleanup.
    assert (result.returncode == 0) == passed,result.stderr
    assert len((tmp_path/"removed").read_text().splitlines()) == 2


def test_reference_state_is_migrated_from_the_restored_baseline():
    # A dump restore re-deparses CHECK expressions; the expected schema must come from the same
    # restored-baseline path every scenario migrates, or recovery diverges on deparse noise alone.
    text = DRIVER.read_text(encoding="utf-8")
    prepare = text.split("prepare_fixture() {", 1)[1].split("\n}\n", 1)[0]
    candidate = prepare.split("candidate image -> normal migration", 1)[1]
    assert candidate.index("restore_baseline") < candidate.index('restart_image "$CANDIDATE_IMAGE"')
    assert candidate.index('restart_image "$CANDIDATE_IMAGE"') < candidate.index('capture_schema "$EXPECTED_SCHEMA"')


def test_failure_diagnostic_survives_pipefail_on_large_logs(tmp_path):
    # A large real log made `compose logs | grep -q` SIGPIPE the producer and read as "no diagnostic".
    result = run_driver(tmp_path, PARTIAL_STUB + r'''
compose() {
  case "$*" in
    'logs server')
      echo "Executing Database Server script 'm2.sql'"
      echo 'Database migration failed: 57P01'
      seq 1 2000000
      return $? ;;  # like docker compose, report the producer's SIGPIPE status
    'up -d --force-recreate server') echo up >> "$OUT/ups"
      if [ "$(wc -l < "$OUT/ups")" -gt 1 ]; then
        cp "$OUT/expected-journal.txt" "$OUT/live-journal"; cp "$OUT/expected-checksums.txt" "$OUT/live-checksums"
        cp "$OUT/expected-schema.sql" "$OUT/live-schema"
      fi ;;
  esac
  return 0
}
set -o pipefail
partial_failure_probe backend-terminated
''', FAULT="none", CHAOS_PARTIAL_ATTEMPTS="2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "[PASS   ] partial-migration-failure:backend-terminated" in result.stdout


# ── compose-upgrade-rollback: the required GA Docker Compose fragment (D9, honua-release#950) ────
# A controlled compose stack: db_exec/compose/docker/curl/seed are a small state machine (journal,
# seeded rows, which image runs, contract gate, guard probe) so the driver's own assertions decide.
# These are self-tests that the gate can fail; the real run is gate-upgrade's compose-upgrade job.
PRIOR = "ghcr.io/honua-io/honua-server@sha256:" + "a" * 64
CANDIDATE = "ghcr.io/honua-io/honua-server@sha256:" + "c" * 64
COMPOSE_STUB = r"""
MIGRATED=0; ROWS=2; PROBE=0; APPROVED=0; UNREADABLE=0; CURRENT=""; SERVER_LOG=""
stack_down() { :; }; stop_server() { :; }; wait_db() { :; }; sleep() { :; }
start_image() { CURRENT="$1"; }
cu_start_server() { CURRENT="$1"; [ -z "${2:-}" ] || APPROVED=1; }
bash() { mkdir -p "$E2E_OUT"; echo '{"slice1":{"e2e_src_fs":{"layerId":9}}}' > "$E2E_OUT/seed-manifest.json"; }
wait_ready_attempts() {
  SERVER_LOG=""
  if [ "$CURRENT" = "$HONUA_CANDIDATE_SERVER_IMAGE" ]; then
    if [ "${CONTRACT:-0}" = 1 ] && [ "$APPROVED" != 1 ]; then
      SERVER_LOG="Migration safety gate blocked pending contract-phase migration(s) on an existing database: 136_Narrow.sql. These reviewed ... then set 'HONUA_APPROVE_CONTRACT_MIGRATIONS=0123abcd' to apply them"
      return 1
    fi
    MIGRATED=1; return 0
  fi
  if [ "$PROBE" = 1 ] && [ "${GUARD:-present}" = inconclusive ]; then
    SERVER_LOG='{"error":"database connection reset during startup"}'; return 1
  fi
  if [ "$MIGRATED" = 1 ] && [ "${READER_INCOMPATIBLE:-0}" = 1 ] && [ "$CURRENT" = "$HONUA_PRIOR_SERVER_IMAGE" ] && [ "$PROBE" != 1 ]; then
    SERVER_LOG='schema_reader_incompatible: applied migration Honua.Server.Migrations.142_CreateAiFinancialAccounting.sql (declared reader-incompatible, not expand).'
    return 1
  fi
  if [ "$PROBE" = 1 ] || { [ "$MIGRATED" = 1 ] && [ "${CONTRACT:-0}" = 1 ]; }; then
    [ "${GUARD:-present}" = absent ] && return 0
    SERVER_LOG='{"error":"schema_reader_incompatible: unknown journal entry"}'; return 1
  fi
  return 0
}
wait_ready() { wait_ready_attempts 1; }
cu_wait_ready() { wait_ready_attempts 1; }
journal() { printf 'Honua.Server.Migrations.135_A.sql\n'; [ "$MIGRATED" = 1 ] && printf 'Honua.Server.Migrations.136_B.sql\n'; [ "$PROBE" = 1 ] && printf 'Honua.Server.Migrations.999_RollbackTargetGuardProbe.sql\n'; return 0; }
db_exec() {
  case "$1" in
    *999_RollbackTargetGuardProbe*INSERT*|INSERT*999_RollbackTargetGuardProbe*) PROBE=1 ;;
    DELETE\ FROM\ public.schema_versions*) PROBE=0 ;;
    'SELECT scriptname FROM public.schema_versions ORDER BY scriptname') journal | sort ;;
    "SELECT 'e2e_src_fs'"*) [ "$UNREADABLE" = 1 ] && return 1
      printf 'e2e_src_fs|%s|%s\nmaui_zoning|12|m2\n' "$ROWS" "m$ROWS" ;;
    SELECT\ COALESCE*) [ "$MIGRATED" = 1 ] && echo 136 || echo 135 ;;
    DELETE\ FROM\ honua_data.e2e_src_fs*) ROWS=1 ;;
    ALTER\ TABLE\ honua_data.e2e_src_fs*) UNREADABLE=1 ;;
  esac
}
compose() {
  case "$*" in
    'ps -q server') echo c1 ;;
    'logs server') echo "$SERVER_LOG" ;;
    *pg_dump*) echo dump ;;
    *pg_restore*) MIGRATED=0; PROBE=0 ;;
  esac
  return 0
}
docker() {
  case "$*" in
    *'{{.Image}}'*) echo "id-$CURRENT" ;;
    *'{{.Id}}'*) echo "id-${*: -1}" ;;
    *'{{.Os}}/{{.Architecture}}'*) echo linux/amd64 ;;
  esac
}
curl() {
  local out="" url="" previous="" arg
  for arg in "$@"; do [ "$previous" = -o ] && out="$arg"; [[ "$arg" == http* ]] && url="$arg"; previous="$arg"; done
  case "$url" in
    *returnCountOnly*) [ "$UNREADABLE" = 1 ] && return 22; echo "{\"count\":$ROWS}" > "$out" ;;
    *) echo '{"data":{"serverVersion":"v"}}' > "$out" ;;
  esac
}
run_compose_upgrade_rollback
"""


def run_compose(tmp_path, **env):
    result = run_driver(tmp_path, COMPOSE_STUB, HONUA_PRIOR_SERVER_IMAGE=env.pop("prior", PRIOR),
                        HONUA_CANDIDATE_SERVER_IMAGE=CANDIDATE, HONUA_CANDIDATE_DB_SCHEMA="136", **env)
    rows = [s for s in json.loads((tmp_path / "scenario-matrix.json").read_text())["scenarios"]
            if s["scenario"] == "compose-upgrade-rollback"]
    observed = json.loads((tmp_path / "compose-observed.json").read_text())
    return result, rows, observed


def test_compose_upgrade_passes_only_through_the_full_customer_lifecycle(tmp_path):
    result, rows, observed = run_compose(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [r["status"] for r in rows] == ["pass"]
    assert "schema_versions advanced 1->2 (schema 135->136" in rows[0]["why"]
    assert observed["customerSteps"]["preUpgradeBackup"] is True
    assert observed["customerSteps"]["rollback"]["outcome"] == "image-rollback-served"
    assert observed["customerSteps"]["rollback"]["priorSchemaReaderGuard"] == "present"
    assert observed["seededRows"]["checksumsMatched"] is True and observed["platform"] == "linux/amd64"
    for phase in ("install", "candidate", "rollback"):
        identity = json.loads((tmp_path / f"compose-{phase}-identity.json").read_text())
        assert identity["platform"] == "linux/amd64" and identity["imageID"] == identity["expectedImageID"]


@pytest.mark.parametrize("fault,why", [
    ("drop-seed-rows", "seeded rows did not survive the upgrade"),
    ("journal-stall", "candidate did not advance public.schema_versions"),
    ("rollback-unreadable", "prior image could not serve the seeded rows after rollback"),
])
def test_compose_upgrade_negative_controls_fail_through_the_gates_own_assertion(tmp_path, fault, why):
    result, rows, observed = run_compose(tmp_path, COMPOSE_UPGRADE_FAULT=fault)
    assert result.returncode != 0
    assert [r["status"] for r in rows] == ["fail"] and why in rows[0]["why"], rows
    assert observed["injectedFault"] == fault


def test_compose_contract_nonce_is_the_customer_step_and_the_boundary_restores_the_backup(tmp_path):
    result, rows, observed = run_compose(tmp_path, CONTRACT="1")
    assert result.returncode == 0, result.stdout + result.stderr
    approval = observed["customerSteps"]["contractMigrationApproval"]
    assert approval["outcome"] == "approved-and-applied" and approval["nonce"] == "0123abcd"
    assert approval["gatedScripts"] == "136_Narrow.sql"
    assert 'HONUA_APPROVE_CONTRACT_MIGRATIONS: "0123abcd"' in (tmp_path / "compose-approve-contract.override.yml").read_text()
    rollback = observed["customerSteps"]["rollback"]
    assert rollback["outcome"] == "restored-from-pre-upgrade-backup"
    assert rollback["boundary"] == "contract-migrations-approved" and rollback["restoreVerified"] is True
    assert rollback["certifiedRollbackPath"] == "restore-pre-upgrade-backup"


def test_compose_reader_incompatible_declaration_restores_the_backup(tmp_path):
    result, rows, observed = run_compose(tmp_path, READER_INCOMPATIBLE="1")
    assert result.returncode == 0, result.stdout + result.stderr
    rollback = observed["customerSteps"]["rollback"]
    assert rollback["outcome"] == "restored-from-pre-upgrade-backup"
    assert rollback["boundary"] == "reader-incompatible-declaration"
    assert rollback["restoreVerified"] is True
    assert rollback["certifiedRollbackPath"] == "restore-pre-upgrade-backup"


def test_compose_unguarded_prior_is_rollback_target_ineligible(tmp_path):
    result, rows, observed = run_compose(tmp_path, GUARD="absent")
    assert result.returncode != 0
    assert rows[0]["status"] == "fail" and rows[0]["why"].startswith("rollback-target-ineligible")
    assert observed["customerSteps"]["rollback"]["restoreVerified"] is True


def test_compose_inconclusive_guard_probe_never_certifies_an_image_rollback(tmp_path):
    # The prior failed to start during the probe for an unrelated reason (no typed refusal), then
    # recovered: the guard is unproven, so an image-only rollback to it is not certified.
    result, rows, observed = run_compose(tmp_path, GUARD="inconclusive")
    assert result.returncode != 0
    assert rows[0]["status"] == "fail" and rows[0]["why"].startswith("rollback-target-guard-unproven")
    rollback = observed["customerSteps"]["rollback"]
    assert rollback["priorSchemaReaderGuard"] == "inconclusive"
    assert rollback["outcome"] == "rollback-target-guard-unproven"


def test_compose_never_certifies_the_candidate_against_itself(tmp_path):
    result, rows, _ = run_compose(tmp_path, prior=CANDIDATE)
    assert result.returncode != 0
    assert rows[0]["status"] == "blocked" and "prior image equals the candidate" in rows[0]["why"]


def test_compose_wait_ends_early_when_the_server_container_exited(tmp_path):
    result = run_driver(tmp_path, r"""
server_ready() { return 1; }
compose() { :; }
sleep() { echo slept >> "$OUT/sleeps"; }
cu_wait_ready 50
""")
    assert result.returncode != 0
    assert len((tmp_path / "sleeps").read_text().splitlines()) == 3


def test_compose_scenario_is_dispatchable_and_pins_x86_64():
    text = DRIVER.read_text(encoding="utf-8")
    assert "compose-upgrade-rollback) run_compose_upgrade_rollback" in text
    assert 'export DOCKER_DEFAULT_PLATFORM="$COMPOSE_PLATFORM"' in text and 'COMPOSE_PLATFORM="linux/amd64"' in text
    body = text.split("run_compose_upgrade_rollback() {", 1)[1].split("\n}\n", 1)[0]
    assert body.count('record "$CU_NAME" pass') == 1
    assert body.rstrip().splitlines()[-1].lstrip().startswith('record "$CU_NAME" pass')
