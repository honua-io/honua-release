"""rc admission: the latest execution decides, a candidate-bound non-pass wins, and the gate inventory is the train's."""
from __future__ import annotations

import datetime as dt
import re

import pytest

import rc_gate_window as w
from test_rc_gate_window import NOW, _eval, _report, _run
from test_upgrade_lock_binding import _case as _upgrade_case

import upgrade_lock_binding


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


def test_a_verified_upgrade_receipt_beside_its_raw_evidence_qualifies(tmp_path):
    prior, candidate_lock, evidence = _upgrade_case(tmp_path)
    receipt = upgrade_lock_binding.verify(prior, candidate_lock, evidence)
    identity = receipt["phases"]["candidate"]["runtimeIdentity"]
    candidate = w.Candidate(identity["sourceRevision"], identity["imageDigest"], identity["platformLockDigest"])
    run = _run(0.5, "success", receipt)
    run["reports"] = [dict(evidence, candidateLockDigest=identity["platformLockDigest"])]
    assert w.matches_candidate(receipt, candidate) and w.matches_candidate(run["reports"][0], candidate)
    assert w._classify(run, candidate, "gate-upgrade.yml") == "qualifying"
    run["report"] = dict(receipt, status="blocked")
    assert w._classify(run, candidate, "gate-upgrade.yml") == "superseding"


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


def _listing(runs: list[dict], pages: int = 1):
    """A fake GitHub runs listing: `runs` on page 1, then full pages of filler when `pages` > 1."""
    filler = [dict(_run(0.01, run_id=1000 + i), created_at=NOW.strftime("%Y-%m-%dT%H:%M:%SZ")) for i in range(100)]

    def get(url, token):
        page = int(re.search(r"[?&]page=(\d+)", url).group(1))
        if page == 1:
            return {"workflow_runs": runs if pages == 1 else filler}
        return {"workflow_runs": filler if page < pages else runs}
    return get


def test_a_rerun_of_a_dispatch_older_than_the_window_is_fetched_and_blocks(monkeypatch):
    since = NOW - dt.timedelta(days=7)
    older = _rerun(_run(9, "failure", run_id=1), 0.1, "failure")
    newer = _run(1, "success", run_id=2)
    reports = {1: [_report(status="fail")], 2: [_report()]}
    monkeypatch.setattr(w, "_get", _listing([older, newer]))
    monkeypatch.setattr(w, "fetch_run_reports", lambda repo, token, run_id: reports[run_id])
    record = w.fetch_gate_runs("o/r", "e2e-cloud-aws.yml", "t", "trunk", bound=True, since=since)
    assert record["complete"] and {run["id"] for run in record["runs"]} == {1, 2}
    gate = _eval({"e2e-cloud-aws.yml": record}, unbound={})["gates"]["e2e-cloud-aws.yml"]
    assert gate["verdict"] == "red" and gate["latest_run"] == older["html_url"]


def test_a_history_too_long_to_read_to_the_rerun_horizon_refuses(monkeypatch):
    monkeypatch.setattr(w, "_get", _listing([], pages=w._HISTORY_PAGES + 1))
    monkeypatch.setattr(w, "fetch_run_reports", lambda *_: [_report()])
    for bound in (True, False):
        record = w.fetch_gate_runs("o/r", "gate-docs.yml", "t", "trunk", bound=bound, since=NOW - dt.timedelta(days=7))
        assert record["complete"] is False
        gate = _eval({"gate-docs.yml": record}, unbound={} if bound else None,
                     pin_at=NOW - dt.timedelta(days=3))["gates"]["gate-docs.yml"]
        assert gate["verdict"] == "red" and "incomplete" in gate["reason"]


def test_fractional_and_whole_second_timestamps_order_as_instants():
    green, red = _run(1, "success", _report(), run_id=1), _run(1, "failure", _report(status="fail"), run_id=2)
    green.update(run_started_at="2026-10-21T12:00:00Z", updated_at="2026-10-21T12:00:00.100Z")
    red.update(run_started_at="2026-10-21T12:00:00.500Z", updated_at="2026-10-21T12:00:00.600Z")
    assert _eval({"e2e-cloud-aws.yml": {"runs": [green, red]}}, unbound={})["admissible"] is False


@pytest.mark.parametrize("created", [None, "", "2026-13-40T00:00:00Z", "yesterday"])
def test_a_deciding_run_without_a_valid_creation_time_is_red(created):
    bound = _run(1, "success", _report())
    unbound = _run(1, "success")
    bound["created_at"] = unbound["created_at"] = created
    result = _eval({"e2e-cloud-aws.yml": {"runs": [bound]}, "gate-docs.yml": {"runs": [unbound, _run(2, run_id=2)]}},
                   unbound={"gate-docs.yml": "no receipt"}, pin_at=NOW - dt.timedelta(days=3))
    assert result["gates"]["e2e-cloud-aws.yml"]["verdict"] == "red"
    assert result["gates"]["gate-docs.yml"]["verdict"] == "red"
    assert "created_at" in result["gates"]["gate-docs.yml"]["reason"]


def test_an_incomplete_history_keeps_the_receipt_limitation_of_an_unbound_gate():
    incomplete = {"runs": [], "complete": False}
    default = _eval({"gate-docs.yml": incomplete}, pin_at=NOW)["gates"]["gate-docs.yml"]
    assert default["candidate_bound"] is False and default["limitation"] == w.UNBOUND_GATE_LIMITATIONS["gate-docs.yml"]
    overridden = _eval({"e2e-cloud-aws.yml": incomplete}, unbound={"e2e-cloud-aws.yml": "no receipt"})["gates"]
    assert overridden["e2e-cloud-aws.yml"]["candidate_bound"] is False
    assert overridden["e2e-cloud-aws.yml"]["limitation"] == "no receipt"
