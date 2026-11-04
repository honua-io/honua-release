"""The release train must keep a blocked verdict when the job itself succeeded."""
import io
import json
import sys
from pathlib import Path

import pytest
import yaml

from train_report import (
    ReportIdentity,
    assemble,
    main,
    rehearsal_overall,
    status_from_signal,
    strict_overall,
    write_report,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("signal,status", [
    ("pass", "pass"),
    ("fail", "fail"),
    ("blocked", "blocked"),
    ("skipped", "skipped"),
    ("success", "pass"),
    ("failure", "fail"),
    ("cancelled", "blocked"),
    ("", "fail"),
    ("timed_out", "fail"),
])
def test_a_verdict_is_kept_and_a_job_result_is_not_one(signal, status):
    assert status_from_signal(signal) == status


def test_blocked_stays_blocked_when_a_successful_job_would_otherwise_read_as_pass():
    assert status_from_signal("blocked") == "blocked"
    assert status_from_signal("success") == "pass"
    assert status_from_signal("blocked") != status_from_signal("success")


def test_a_failure_outranks_blocked_and_a_real_cut_rejects_a_skip():
    assert rehearsal_overall(["pass", "blocked"]) == "blocked"
    assert strict_overall(["pass", "blocked"]) == "blocked"
    assert strict_overall(["fail", "blocked"]) == "fail"
    assert strict_overall(["pass", "skipped"]) == "fail"
    assert rehearsal_overall(["pass", "skipped"]) == "pass"


def test_assemble_records_the_raw_signal_and_the_folded_verdict(tmp_path):
    document = assemble(
        ["build-test|blocked", "manifest|success", "e2e|failure"],
        ReportIdentity("2026.1-rc.1", True, "2026-11-04T00:00:00Z", "https://example.test/run/1"),
        rehearsal_overall,
    )
    assert document["dry_run"] is True
    assert document["overallStatus"] == "fail"
    assert [(row["gate"], row["status"], row["why"]) for row in document["gates"]] == [
        ("build-test", "blocked", "job/gate signal: blocked"),
        ("manifest", "pass", "job/gate signal: success"),
        ("e2e", "fail", "job/gate signal: failure"),
    ]
    assert document["summary"] == {"pass": 1, "fail": 1, "blocked": 1, "skipped": 0}
    out = tmp_path / "out" / "gate-report.json"
    write_report(out, document)
    assert json.loads(out.read_text())["overallStatus"] == "fail"


@pytest.mark.parametrize("dry_run,expected", [("true", "pass"), ("false", "fail")])
def test_cli_a_real_cut_rejects_a_skip_and_a_rehearsal_does_not(tmp_path, monkeypatch, dry_run, expected):
    monkeypatch.setattr(sys, "stdin", io.StringIO("cloud|skipped\nbuild-test|blocked\n"))
    out = tmp_path / "gate-report.json"
    assert main(["--label", "2026.1-rc.1", "--dry-run", dry_run,
                 "--generated-at", "2026-11-04T00:00:00Z", "--run-url", "https://example.test/run/1",
                 "--out", str(out)]) == 0
    document = json.loads(out.read_text())
    assert document["dry_run"] is (dry_run == "true")
    assert document["overallStatus"] == "blocked"
    assert [row["status"] for row in document["gates"]] == ["skipped", "blocked"]
    monkeypatch.setattr(sys, "stdin", io.StringIO("cloud|skipped\n"))
    assert main(["--label", "2026.1-rc.1", "--dry-run", dry_run,
                 "--generated-at", "2026-11-04T00:00:00Z", "--run-url", "https://example.test/run/1",
                 "--out", str(out)]) == 0
    assert json.loads(out.read_text())["overallStatus"] == expected


def test_train_feeds_semantic_verdicts_for_gates_that_publish_one():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release-train.yml").read_text())
    env = next(step["env"] for step in workflow["jobs"]["report"]["steps"]
               if step.get("name") == "Assemble platform gate-report.json")
    assert env["S_BUILD"] == (
        "${{ needs.gate_build_test.outputs.overall_status || needs.gate_build_test.result }}")
    assert env["S_CONTRACT"] == (
        "${{ needs.gate_contract.outputs.overall_status || needs.gate_contract.result }}")
    assert env["S_CONFORMANCE"] == (
        "${{ needs.gate_conformance.outputs.overall_status || needs.gate_conformance.result }}")
    run = next(step["run"] for step in workflow["jobs"]["report"]["steps"]
               if step.get("name") == "Assemble platform gate-report.json")
    assert "python tools/train_report.py" in run
    assert "jq -R" not in run
