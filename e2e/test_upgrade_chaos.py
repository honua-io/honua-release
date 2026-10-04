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


def test_partial_failure_can_never_record_pass_without_the_candidate_runner():
    # A hand-written psql transaction only proves PostgreSQL rollback, not application migration
    # atomicity (honua-io/honua-release#440). Until a probe interrupts a real migration inside the
    # candidate runner, no line of the driver may record this scenario as pass.
    text = DRIVER.read_text(encoding="utf-8")
    body = text.split("run_partial_failure() {", 1)[1].split("\n}\n", 1)[0]
    assert " pass " not in body
    assert "record partial-migration-failure blocked" in body
    assert "honua-io/honua-release#440" in body
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
db_exec() { :; }
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


def test_partial_failure_reports_blocked_and_fails_the_matrix_even_when_postgres_rolls_back(tmp_path):
    # Every database and compose call succeeds, as it would when PostgreSQL rollback works.
    result = run_driver(tmp_path, r'''
restore_baseline() { :; }
db_exec() { echo 0; }
compose() { :; }
sleep() { :; }
run_partial_failure
''')
    assert result.returncode != 0
    scenarios = json.loads((tmp_path / "scenario-matrix.json").read_text())["scenarios"]
    assert scenarios == [{
        "scenario": "partial-migration-failure",
        "status": "blocked",
        "why": "not evidence: no probe interrupts a real migration inside the candidate runner "
               "(honua-io/honua-release#440)",
    }]
