"""The rc gate window admits only the newest candidate-bound certifying run, and it must be able to fail."""
from __future__ import annotations

import datetime as dt
import io
import json
import re
import zipfile
from pathlib import Path

import rc_gate_window as w

NOW = dt.datetime(2026, 10, 22, 12, 0, tzinfo=dt.timezone.utc)
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
LOCK = "sha256:" + "c" * 64
CANDIDATE = w.Candidate(SHA, DIGEST, LOCK)


def _report(*, sha=SHA, digest=DIGEST, lock=LOCK, status="pass", certifying=True, scope=None):
    doc = {
        "status": status,
        "certifying": certifying,
        "certifyingScope": certifying,
        "serverSha": sha,
        "imageDigest": digest,
        "platformLockDigest": lock,
    }
    if scope is not None:
        doc["requestedScope"] = scope
    return doc


def _run(days_ago: float, conclusion: str = "success", report: dict | None = None,
         sha: str = "abc123def456", run_id: int = 1):
    at = (NOW - dt.timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": run_id, "html_url": f"https://x/run/{run_id}", "created_at": at,
            "conclusion": conclusion, "status": "completed", "event": "schedule",
            "head_sha": sha, "report": report}


def _eval(records, advisory=(), unbound=None):
    kwargs = {"candidate": CANDIDATE, "advisory": advisory}
    if unbound is not None:
        kwargs["unbound"] = unbound
    return w.evaluate(records, NOW, 7, **kwargs)


def test_exact_candidate_match_is_admissible():
    records = {"e2e-cloud-aws.yml": {"runs": [_run(1, report=_report(), run_id=7)]}}
    result = _eval(records, unbound={})
    assert result["admissible"] is True
    gate = result["gates"]["e2e-cloud-aws.yml"]
    assert gate["verdict"] == "green"
    assert gate["candidate_bound"] is True
    assert gate["last_green_build"] == SHA[:12]
    assert result["candidate"]["server_digest"] == DIGEST
    assert result["candidate"]["platform_lock"] == LOCK


def test_a_different_candidate_does_not_admit():
    others = (
        _report(sha="d" * 40),
        _report(digest="sha256:" + "e" * 64),
        _report(lock="sha256:" + "f" * 64),
        None,
    )
    for report in others:
        result = _eval({"e2e-cloud-aws.yml": {"runs": [_run(0.5, report=report)]}}, unbound={})
        assert result["admissible"] is False, report
        assert "no completed run binds" in result["gates"]["e2e-cloud-aws.yml"]["reason"]
    mixed = _eval({"e2e-cloud-aws.yml": {"runs": [
        _run(2, report=_report(sha="d" * 40), run_id=1),
        _run(0.4, report=_report(), run_id=2),
    ]}}, unbound={})
    assert mixed["admissible"] is True


def test_older_green_plus_newer_red_for_the_same_candidate_refuses():
    records = {"e2e-cloud-aws.yml": {"runs": [
        _run(2, "success", _report(certifying=True), run_id=1),
        _run(0.2, "failure", _report(status="fail", certifying=False), run_id=2),
    ]}}
    result = _eval(records, unbound={})
    assert result["admissible"] is False
    assert result["red"] == ["e2e-cloud-aws.yml"]
    assert "supersedes an older green" in result["gates"]["e2e-cloud-aws.yml"]["reason"]
    assert "conclusion failure" in result["gates"]["e2e-cloud-aws.yml"]["reason"]


def test_a_newer_failure_of_another_candidate_does_not_supersede():
    records = {"e2e-cloud-aws.yml": {"runs": [
        _run(1, "success", _report(), run_id=1),
        _run(0.1, "failure", _report(sha="d" * 40, status="fail", certifying=False), run_id=2),
    ]}}
    assert _eval(records, unbound={})["admissible"] is True


def test_diagnostic_run_does_not_count_and_supersedes_an_older_green():
    records = {"e2e-cloud-aws.yml": {"runs": [
        _run(2, "success", _report(certifying=True), run_id=1),
        _run(0.2, "success", _report(certifying=False), run_id=2),
    ]}}
    result = _eval(records, unbound={})
    gate = result["gates"]["e2e-cloud-aws.yml"]
    assert result["admissible"] is False
    assert gate["verdict"] == "red"
    assert "diagnostic or focused run" in gate["reason"]
    assert "supersedes an older green" in gate["reason"]

    alone = _eval({"e2e-cloud-aws.yml": {"runs": [_run(0.2, "success", _report(certifying=False))]}}, unbound={})
    assert alone["admissible"] is False
    assert "diagnostic or focused run" in alone["gates"]["e2e-cloud-aws.yml"]["reason"]


def test_a_diagnostic_run_that_does_not_name_the_candidate_does_not_count():
    diagnostic = {"status": "pass", "certifying": False, "certifyingScope": False,
                  "requestedScope": {"target": "aws-eks", "redis": "off"}}
    hidden = _eval({"e2e-cloud-aws.yml": {"runs": [
        _run(1, "success", _report(), run_id=1),
        _run(0.1, "success", diagnostic, run_id=2),
    ]}}, unbound={})
    assert hidden["admissible"] is True
    alone = _eval({"e2e-cloud-aws.yml": {"runs": [_run(0.1, "success", diagnostic)]}}, unbound={})
    assert alone["admissible"] is False


def test_partial_target_does_not_count_even_when_the_workflow_succeeded():
    report = _report(certifying=True, scope={"target": "aws-ecs", "redis": "off"})
    result = _eval({"e2e-cloud-aws.yml": {"runs": [_run(0.2, "success", report)]}}, unbound={})
    assert result["admissible"] is False
    assert "diagnostic or focused run" in result["gates"]["e2e-cloud-aws.yml"]["reason"]


def test_canary_and_upgrade_receipt_shapes_match_the_candidate():
    canary = {"status": "pass", "lockDigest": LOCK,
              "serverImage": {"digest": DIGEST, "sourceSha": SHA}}
    upgrade = {"overallStatus": "pass", "runtimeIdentity": {
        "imageDigest": DIGEST, "sourceRevision": SHA, "platformLockDigest": LOCK}}
    for name, report in (("terminal-model-canary.yml", canary), ("gate-upgrade.yml", upgrade)):
        result = _eval({name: {"runs": [_run(0.4, "success", report)]}}, unbound={})
        assert result["admissible"] is True, name


def test_unbound_gate_stays_a_limitation_even_when_its_run_looks_green():
    records = {"manifest-validate.yml": {"runs": [_run(0.2, "success", _report())]}}
    result = _eval(records)
    gate = result["gates"]["manifest-validate.yml"]
    assert result["admissible"] is False
    assert gate["verdict"] == "limitation"
    assert gate["candidate_bound"] is False
    assert "no candidate-bound report" in gate["reason"]
    assert w.UNBOUND_GATE_LIMITATIONS["manifest-validate.yml"] in gate["reason"]
    md = w.render_markdown(result)
    assert "No candidate-bound report" in md
    assert "manifest-validate.yml" in md
    assert "Admissible: **no**" in md


def test_required_unbound_gates_block_even_when_every_bound_gate_matches():
    records = {}
    for index, gate in enumerate(w.RC_GATES):
        records[gate] = {"runs": [_run(1, report=_report(), run_id=index + 1)]}
    result = _eval(records, advisory=w.RC_ADVISORY)
    assert result["admissible"] is False
    assert result["gates"]["e2e-cloud-aws.yml"]["verdict"] == "green"
    assert result["gates"]["gate-upgrade.yml"]["verdict"] == "green"
    for gate in w.RC_GATES:
        if gate in w.CANDIDATE_BOUND_GATES:
            continue
        assert result["gates"][gate]["verdict"] == "limitation"
        assert gate in result["red"]


def test_never_run_and_stale_bound_gates_block():
    never = _eval({"e2e-cloud-aws.yml": {"runs": []}}, unbound={})
    assert never["red"] == ["e2e-cloud-aws.yml"]
    assert "no completed run binds" in never["gates"]["e2e-cloud-aws.yml"]["reason"]
    stale = _eval({"e2e-cloud-aws.yml": {"runs": [_run(7.01, report=_report())]}}, unbound={})
    assert stale["red"] == ["e2e-cloud-aws.yml"]
    assert "outside the 7-day window" in stale["gates"]["e2e-cloud-aws.yml"]["reason"]


def test_a_red_advisory_gate_does_not_block_when_the_required_gate_qualifies():
    records = {
        "e2e-cloud-aws.yml": {"runs": [_run(1, report=_report(), run_id=1)]},
        "terminal-model-canary.yml": {"runs": []},
    }
    result = _eval(records, advisory=("terminal-model-canary.yml",), unbound={})
    assert result["admissible"] is True
    assert result["red"] == []
    assert result["advisory_red"] == ["terminal-model-canary.yml"]
    assert "⚠️" in w.render_markdown(result)


def test_gate_partition_covers_every_train_and_ai_gate_once():
    covered = set(w.CANDIDATE_BOUND_GATES) | set(w.UNBOUND_GATE_LIMITATIONS)
    assert covered == set(w.TRAIN_GATES + w.AI_GATES)
    assert not (set(w.CANDIDATE_BOUND_GATES) & set(w.UNBOUND_GATE_LIMITATIONS))
    assert all(text.strip() for text in w.UNBOUND_GATE_LIMITATIONS.values())


def test_gate_list_matches_release_train_uses():
    """Every reusable workflow release-train.yml calls is in TRAIN_GATES, and nothing extra."""
    train = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release-train.yml"
    called = set(re.findall(r"uses:\s*\./\.github/workflows/([\w.-]+\.yml)", train.read_text()))
    assert called == set(w.TRAIN_GATES), (called ^ set(w.TRAIN_GATES))


def test_ga_deferred_gates_are_real_gates_and_excluded_from_rc_set():
    assert set(w.GA_DEFERRED) <= set(w.TRAIN_GATES + w.AI_GATES)
    assert not set(w.GA_DEFERRED) & set(w.RC_GATES)
    assert "gate-build-test.yml" in w.RC_GATES


def test_rc_set_requires_eighteen_train_gates_and_reports_both_ai_gates_as_advisory():
    """Founder re-tier 2026-10-25 (wedge-critical-retier-2026-10-25, honua-release#376
    comment 6407009748): the AI gates are out of RC admission. They are still read and reported."""
    assert len(w.RC_GATES) == 18
    assert set(w.AI_GATES) == {"gate-agent-e2e.yml", "terminal-model-canary.yml"}
    assert not set(w.AI_GATES) & set(w.RC_GATES)
    assert w.RC_ADVISORY == w.AI_GATES
    assert set(w.GA_DEFERRED) == {"capacity-soak.yml", "rollback-certification.yml", "gate-journey.yml"}


def test_rc_release_passes_the_candidate_and_enforces_before_publish():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/rc-release.yml").read_text()
    invocation = re.search(r"python tools/rc_gate_window\.py.*?--enforce", workflow, re.DOTALL)
    assert invocation is not None
    body = invocation.group()
    assert "--without-ai-gates" not in body and "--gate " not in body
    assert "--server-sha" in body and "--server-digest" in body and "--platform-lock" in body
    assert "needs: gate-window" in workflow
    assert "needs.gate-window.outputs.admissible == 'true'" in workflow
    assert "platform_lock:" in workflow


def test_markdown_names_deferred_gates_and_the_candidate():
    result = _eval({"e2e-cloud-aws.yml": {"runs": [_run(1, report=_report())]}}, unbound={})
    result["deferred_to_ga"] = ["gate-journey.yml"]
    md = w.render_markdown(result)
    assert "Deferred to GA" in md and "gate-journey.yml" in md
    assert SHA in md and DIGEST in md and LOCK in md


def test_main_refuses_a_candidate_it_cannot_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    missing = w.main(["--server-sha", SHA, "--server-digest", DIGEST, "--enforce",
                      "--out", str(tmp_path / "out.json")])
    assert missing == 2
    bad = w.main(["--server-sha", SHA, "--server-digest", DIGEST, "--platform-lock", "latest",
                  "--enforce", "--out", str(tmp_path / "out.json")])
    assert bad == 2
    lock = tmp_path / "platform-lock.json"
    lock.write_text("{}\n", encoding="utf-8")
    named = w.resolve_candidate(SHA, DIGEST, None, str(lock), "platform-manifest.yaml")
    assert named.server_sha == SHA and named.server_digest == DIGEST
    assert named.platform_lock == "sha256:" + __import__("hashlib").sha256(lock.read_bytes()).hexdigest()


def test_artifact_zip_yields_report_objects_and_ignores_other_members():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("gate-report.json", json.dumps(_report()))
        archive.writestr("notes.txt", "not json")
        archive.writestr("broken.json", "{")
    docs = w._json_documents(buffer.getvalue())
    assert len(docs) == 1
    assert w.matches_candidate(docs[0], CANDIDATE)
    assert w._json_documents(b"not a zip") == []
