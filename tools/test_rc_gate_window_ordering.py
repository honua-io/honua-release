"""rc admission: the latest execution decides, a candidate-bound non-pass wins, and the gate inventory is the train's."""
from __future__ import annotations

import datetime as dt

import pytest

import rc_gate_window as w
from test_rc_gate_window import NOW, _eval, _report, _run


def test_a_candidate_bound_non_pass_wins_over_an_unrelated_pass():
    for bound_status in ("blocked", "skipped", None):
        bound = _report(status=bound_status) if bound_status else {k: v for k, v in _report().items() if k != "status"}
        run = _run(0.5, "success", bound)
        run["reports"] = [{"status": "pass"}]
        result = _eval({"e2e-cloud-aws.yml": {"runs": [run]}}, unbound={})
        assert result["admissible"] is False, bound_status
        assert "does not certify this candidate" in result["gates"]["e2e-cloud-aws.yml"]["reason"]


def test_a_bound_pass_beside_reports_without_a_status_qualifies():
    run = _run(0.5, "success", _report())
    run["reports"] = [{"artifact": "screenshot-index"}, {"schema": "honua.studio-evidence/v1"}]
    assert _eval({"e2e-cloud-aws.yml": {"runs": [run]}}, unbound={})["admissible"] is True


def _rerun(run: dict, days_ago: float, conclusion: str) -> dict:
    started = NOW - dt.timedelta(days=days_ago)
    run.update(run_started_at=started.strftime("%Y-%m-%dT%H:%M:%SZ"), run_attempt=2, conclusion=conclusion,
               updated_at=(started + dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return run


def test_an_older_dispatch_rerun_to_failure_after_a_newer_success_blocks():
    older = _rerun(_run(2, "success", _report(status="fail"), run_id=1), 0.1, "failure")
    newer = _run(1, "success", _report(), run_id=2)
    result = _eval({"e2e-cloud-aws.yml": {"runs": [older, newer]}}, unbound={})
    assert result["admissible"] is False
    assert result["gates"]["e2e-cloud-aws.yml"]["latest_run"] == older["html_url"]


def test_an_unbound_older_dispatch_rerun_to_failure_blocks():
    pin = NOW - dt.timedelta(days=3)
    older = _rerun(_run(2, "success", run_id=1), 0.1, "failure")
    newer = _run(1, "success", run_id=2)
    result = _eval({"gate-docs.yml": {"runs": [older, newer]}}, pin_at=pin)
    assert result["gates"]["gate-docs.yml"]["verdict"] == "red"
    assert "conclusion failure" in result["gates"]["gate-docs.yml"]["reason"]


def test_overlapping_runs_with_different_outcomes_have_no_deciding_run():
    first = _run(1, "success", _report(), run_id=1)
    second = _run(1, "failure", _report(status="fail"), run_id=2)
    second["run_started_at"] = first["run_started_at"]
    result = _eval({"e2e-cloud-aws.yml": {"runs": [first, second]}}, unbound={})
    assert result["admissible"] is False
    assert "overlap with different outcomes" in result["gates"]["e2e-cloud-aws.yml"]["reason"]


def test_overlapping_unbound_runs_that_agree_decide_the_gate():
    pin = NOW - dt.timedelta(days=3)
    first, second = _run(1, "success", run_id=1), _run(1, "success", run_id=2)
    first["updated_at"] = second["updated_at"]
    assert _eval({"gate-dr.yml": {"runs": [first, second]}}, pin_at=pin)["gates"]["gate-dr.yml"]["verdict"] == "green"
    second["conclusion"] = "failure"
    gate = _eval({"gate-dr.yml": {"runs": [first, second]}}, pin_at=pin)["gates"]["gate-dr.yml"]
    assert gate["verdict"] == "red" and "overlap with different outcomes" in gate["reason"]


def test_an_arc_arm_is_decided_by_its_latest_execution(monkeypatch):
    kinds = {1: "superseding", 2: "qualifying"}
    monkeypatch.setattr(w, "_classify", lambda run, candidate, gate="", arm="": kinds[run["id"]])
    older = _rerun(_run(2, "failure", run_id=1), 0.1, "failure")
    newer = _run(1, "success", run_id=2)
    arms = _eval({w.ARC_GATE: {"runs": [older, newer]}}, unbound={})["gates"][w.ARC_GATE]["arms"]
    assert all(arm["verdict"] == "red" and arm["run"] == older["html_url"] for arm in arms.values())
    kinds[1] = "qualifying"
    arms = _eval({w.ARC_GATE: {"runs": [older, newer]}}, unbound={})["gates"][w.ARC_GATE]["arms"]
    assert all(arm["verdict"] == "green" for arm in arms.values())


def test_train_gates_reads_local_and_pinned_calls_and_refuses_a_foreign_one(tmp_path):
    train = tmp_path / "release-train.yml"
    train.write_text(
        "jobs:\n"
        "  freeze: {runs-on: ubuntu-latest}\n"
        "  a: {uses: ./.github/workflows/gate-a.yml}\n"
        "  b: {uses: 'honua-io/honua-release/.github/workflows/gate-b.yml@" + "1" * 40 + "'}\n", encoding="utf-8")
    assert w.train_gates(train) == ("gate-a.yml", "gate-b.yml")
    train.write_text("jobs:\n  c: {uses: other-org/repo/.github/workflows/gate-c.yml@main}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        w.train_gates(train)


def test_protocol_certification_is_reported_for_rc_but_not_required():
    gate = "gate-protocol-certification.yml"
    assert gate in w.GA_DEFERRED and gate not in w.RC_GATES
    assert "train job" in w.UNBOUND_GATE_LIMITATIONS[gate]
