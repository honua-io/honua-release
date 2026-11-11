"""The candidate-verification trigger stays off and can still describe one row per ticket."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import candidate_verification as cv

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "candidate-verification.yml"
SHA = "0123456789abcdef0123456789abcdef01234567"
WHEN = "2026-11-10T18:40:00-08:00"


def test_switch_is_off_unless_the_environment_says_on(monkeypatch):
    monkeypatch.delenv(cv.SWITCH_ENV, raising=False)
    assert cv.switched_on() is False
    monkeypatch.setenv(cv.SWITCH_ENV, "off")
    assert cv.switched_on() is False
    monkeypatch.setenv(cv.SWITCH_ENV, "on")
    assert cv.switched_on() is True


def test_plan_lists_each_in_repo_workflow_once():
    assert cv.dispatch_workflows() == (
        "gate-executable-docs",
        "e2e-local-docker",
        "gate-installed-clients",
        "gate-protocol-certification",
    )


def test_plan_command_refuses_while_switched_off(tmp_path, monkeypatch):
    monkeypatch.delenv(cv.SWITCH_ENV, raising=False)
    completed = subprocess.run(
        [sys.executable, str(REPO / "tools" / "candidate_verification.py"), "plan", "--candidate-sha", SHA],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == cv.SWITCHED_OFF
    assert completed.stdout == ""
    assert "gate-executable-docs" not in completed.stderr


def test_row_replaces_the_same_candidate_and_keeps_other_shas():
    other = cv.result_row(WHEN, "a" * 40, 1131, "pass", "https://example.test/older", "s-rc2-vendor")
    first = cv.result_row(WHEN, SHA, 1131, "fail", "https://example.test/1", "s-rc2-vendor")
    second = cv.result_row(WHEN, SHA, 1131, "pass", "https://example.test/2", "s-rc2-vendor")
    body = cv.upsert_row(other + "\n" + first, second)
    assert body.splitlines() == [other, second]


def test_row_rejects_a_short_sha_or_unknown_result():
    with pytest.raises(ValueError):
        cv.result_row(WHEN, "abc", 1131, "pass", "https://example.test/1", "s-rc2-vendor")
    with pytest.raises(ValueError):
        cv.result_row(WHEN, SHA, 1131, "green", "https://example.test/1", "s-rc2-vendor")


def test_failure_issue_names_the_lane_once():
    row = cv.result_row(WHEN, SHA, 1132, "fail", "https://example.test/run", "s-qa-live")
    body = cv.failure_body([row])
    assert "Owning lane: s-qa-live." in body
    assert SHA in body
    assert cv.failure_body([]) == ""


def test_record_posts_one_row_per_ticket_and_one_failure_issue(monkeypatch):
    monkeypatch.setenv(cv.SWITCH_ENV, "on")
    calls = []

    def post(kind, target, body):
        calls.append((kind, target, body))

    older = cv.result_row(WHEN, "b" * 40, 1131, "blocked", "https://example.test/old", "s-rc2-vendor")
    rows = cv.record(
        WHEN,
        SHA,
        (
            cv.Outcome(1131, "pass", "https://example.test/docs"),
            cv.Outcome(1132, "fail", "https://example.test/live"),
        ),
        post,
        {1131: older},
    )
    assert [call[0] for call in calls] == ["comment", "comment", "issue"]
    assert calls[0][1] == 1131 and "result=pass" in calls[0][2]
    assert older in calls[0][2]
    assert calls[1][1] == 1132 and "lane=s-qa-live" in calls[1][2]
    assert calls[2][1] == cv.FAILURE_TITLE
    assert rows[1131].startswith(WHEN)
    assert set(rows) == {1131, 1132}


def test_record_command_refuses_before_reading_results(tmp_path, monkeypatch):
    monkeypatch.delenv(cv.SWITCH_ENV, raising=False)
    missing = tmp_path / "missing.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(REPO / "tools" / "candidate_verification.py"),
            "record",
            "--candidate-sha",
            SHA,
            "--results",
            str(missing),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        env={key: value for key, value in os.environ.items() if key != cv.SWITCH_ENV},
    )
    assert completed.returncode == cv.SWITCHED_OFF
    assert not missing.exists()


def test_workflow_is_skipped_and_does_not_enable_the_switch():
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    text = WORKFLOW.read_text(encoding="utf-8")
    trigger = document.get("on") or document[True]
    assert document["jobs"]["verify"]["if"] is False
    assert "HONUA_CANDIDATE_VERIFICATION: on" not in text
    assert "gh workflow run" not in text
    assert "schedule" not in trigger
    assert trigger["repository_dispatch"]["types"] == ["candidate-published"]
    step = document["jobs"]["verify"]["steps"][-1]["run"]
    assert "candidate_verification.py plan" in step


def test_enabled_plan_command_prints_the_workflow_list(tmp_path, monkeypatch):
    monkeypatch.setenv(cv.SWITCH_ENV, "on")
    completed = subprocess.run(
        [sys.executable, str(REPO / "tools" / "candidate_verification.py"), "plan", "--candidate-sha", SHA],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, cv.SWITCH_ENV: "on"},
    )
    assert completed.returncode == 0
    assert completed.stdout.splitlines() == list(cv.dispatch_workflows())


def test_record_results_round_trip(tmp_path):
    path = tmp_path / "results.json"
    path.write_text(json.dumps([{"ticket": 1131, "result": "blocked", "run_url": "https://example.test/x"}]))
    assert cv.load_outcomes(path) == (cv.Outcome(1131, "blocked", "https://example.test/x"),)
