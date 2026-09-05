"""Regression tests for SHA-bound full-matrix evidence in the build/test gate."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "certification"))
import check_build_test as bt  # noqa: E402


PINNED_SHA = "a" * 40
FULL_MATRIX_POLICY = {
    "workflow": ".github/workflows/ci.yml",
    "events": frozenset({"schedule", "workflow_dispatch"}),
    "requiredChecks": frozenset({
        "Build & Format Check",
        ".NET Foundation Tests",
        "Python Integration Tests",
        "Test Suite Summary",
        "CI Gate",
    }),
}


def _full_matrix_payload(*, conclusion="success", omitted=frozenset(), overrides=None,
                         sha=PINNED_SHA, status="completed"):
    run_id = 12345
    overrides = overrides or {}
    checks = [{
        "name": name,
        "status": "completed",
        "conclusion": overrides.get(name, "success"),
        "details_url": f"https://github.com/honua-io/honua-server/actions/runs/{run_id}/job/1",
    } for name in FULL_MATRIX_POLICY["requiredChecks"] - set(omitted)]
    return {
        "check_runs": checks,
        "_workflow_runs": [{
            "id": run_id,
            "path": ".github/workflows/ci.yml",
            "event": "workflow_dispatch",
            "head_sha": sha,
            "status": status,
            "conclusion": conclusion,
            "updated_at": "2026-08-25T00:00:00Z",
        }],
    }


@pytest.mark.parametrize("payload", [
    {"check_runs": [], "_workflow_runs": []},
    _full_matrix_payload(sha="b" * 40),
    _full_matrix_payload(status="in_progress", conclusion=None),
])
def test_full_matrix_requires_completed_run_bound_to_exact_pinned_sha(payload):
    status, why = bt.classify_full_matrix(payload, FULL_MATRIX_POLICY, PINNED_SHA)
    assert status == "fail"
    assert "missing completed full-matrix run" in why
    assert "pinned sha" in why


def test_full_matrix_names_every_absent_expected_lane():
    missing = {"Build & Format Check", "Python Integration Tests"}
    status, why = bt.classify_full_matrix(
        _full_matrix_payload(omitted=missing), FULL_MATRIX_POLICY, PINNED_SHA
    )
    assert status == "fail"
    assert "missing expected lanes" in why
    assert all(name in why for name in missing)


def test_full_matrix_rejects_red_workflow_and_names_red_lane():
    payload = _full_matrix_payload(
        conclusion="failure", overrides={".NET Foundation Tests": "failure"}
    )
    status, why = bt.classify_full_matrix(payload, FULL_MATRIX_POLICY, PINNED_SHA)
    assert status == "fail"
    assert ".NET Foundation Tests" in why
    assert "workflow conclusion=failure" in why


def test_full_matrix_ignores_expected_lane_from_an_unrelated_run():
    payload = _full_matrix_payload(omitted={"CI Gate"})
    payload["check_runs"].append({
        "name": "CI Gate",
        "status": "completed",
        "conclusion": "success",
        "details_url": "https://github.com/honua-io/honua-server/actions/runs/99999/job/1",
    })
    status, why = bt.classify_full_matrix(payload, FULL_MATRIX_POLICY, PINNED_SHA)
    assert status == "fail"
    assert "CI Gate" in why


def test_full_matrix_green_run_with_all_expected_lanes_passes():
    status, why = bt.classify_full_matrix(_full_matrix_payload(), FULL_MATRIX_POLICY, PINNED_SHA)
    assert status == "pass"
    assert "all expected lanes" in why


def test_evaluate_rejects_green_subset_when_full_matrix_evidence_is_absent():
    manifest = {"components": {"honua-server": {"sha": PINNED_SHA}}}
    green_subset = {"check_runs": [{
        "name": "PR Gate",
        "status": "completed",
        "conclusion": "success",
    }]}
    report = bt.evaluate(
        manifest,
        lambda _repo, _sha: green_subset,
        full_matrix={"honua-server": FULL_MATRIX_POLICY},
    )
    row = report["components"][0]
    assert report["overallStatus"] == "fail"
    assert row["status"] == "fail"
    assert "missing completed full-matrix run" in row["why"]


def test_full_matrix_policy_names_the_stable_server_lanes():
    policy = bt.load_full_matrix()["honua-server"]
    assert policy["workflow"] == ".github/workflows/ci.yml"
    assert policy["events"] == frozenset({"schedule", "workflow_dispatch"})
    assert FULL_MATRIX_POLICY["requiredChecks"] == policy["requiredChecks"]


def test_github_collection_fetch_follows_every_page(monkeypatch):
    requested: list[str] = []
    pages = {
        1: {"total_count": 101, "check_runs": [{"id": i} for i in range(100)]},
        2: {"total_count": 101, "check_runs": [{"id": 100}]},
    }

    def fake_urlopen(request, timeout):
        requested.append(request.full_url)
        page = int(request.full_url.rsplit("page=", 1)[1])
        return io.BytesIO(json.dumps(pages[page]).encode())

    monkeypatch.setattr(bt.urllib.request, "urlopen", fake_urlopen)
    payload = bt._fetch_paginated_collection("https://api.github.test/check-runs", {}, "check_runs")

    assert len(payload["check_runs"]) == 101
    assert requested == [
        "https://api.github.test/check-runs?per_page=100&page=1",
        "https://api.github.test/check-runs?per_page=100&page=2",
    ]
