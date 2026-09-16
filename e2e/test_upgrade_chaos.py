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


def test_partial_failure_is_a_real_backend_termination_probe():
    text = DRIVER.read_text(encoding="utf-8")
    assert "pg_terminate_backend" in text
    assert "chaos_partial_first" in text and "chaos_partial_second" in text
    assert "backend termination rolled back all statements" in text


def test_scenario_failures_propagate_to_the_matrix_exit_code():
    text = DRIVER.read_text(encoding="utf-8")
    assert "local failed=0" in text
    assert "failed=1" in text
    assert "if ! assert_state concurrent-app-start" in text
