"""Public SDK endpoint row and encrypted fixture handoff controls; no AWS access."""
import json
from unittest import mock

import cloud_journey
import run_cloud


def test_secret_fixture_stays_in_encrypted_handoff_and_round_trips():
    context = {"bearer": "application-bearer", "dbPassword": "fixture-password"}
    datasource = dict(host="private-db", port=5432, databaseName="fixture", username="fixture", password="fixture-password")
    sealed_plaintext = cloud_journey.pack_handoff("app-key", datasource,
        tenant_signing_key="f" * 64, installed_clients=context)
    assert cloud_journey.unpack_handoff(sealed_plaintext) == ("app-key", datasource)
    assert json.loads(sealed_plaintext)["installedClients"] == context


def test_missing_fixture_is_blocked_and_never_invokes_an_installer():
    state = {"cell": "aws-ecs/redis-on"}
    with mock.patch.object(run_cloud, "installed_clients") as adapter:
        row = run_cloud.installed_client_row(state, None, "app-key")
    assert row.status == "blocked"
    assert "not qualified" in row.why
    adapter.assert_not_called()


def test_blocked_matrix_remains_blocked_with_all_cells_visible():
    results = [{"status": "pass"}] * 34 + [{"status": "blocked"}] * 7
    with mock.patch.object(run_cloud, "installed_clients") as adapter:
        adapter.return_value.run_cloud.return_value = {"status": "blocked", "results": results}
        row = run_cloud.installed_client_row({"cell": "aws-serverless/redis-on"}, {"secret": "private"}, "app-key")
    assert row.status == "blocked"
    assert row.evidence["counts"] == {"pass": 34, "blocked": 7, "fail": 0}
    assert "private" not in str(row.evidence)


def test_candidate_or_install_error_fails_without_echoing_secret():
    with mock.patch.object(run_cloud, "installed_clients") as adapter:
        adapter.return_value.run_cloud.side_effect = ValueError("fixture-password")
        row = run_cloud.installed_client_row({"cell": "aws-ecs/redis-on"}, {"secret": "private"}, "app-key")
    assert row.status == "fail"
    assert "ValueError" in row.why
    assert "fixture-password" not in row.why
