"""The rc gate window admits only the newest candidate-bound certifying run, and it must be able to fail."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import subprocess
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


def _eval(records, advisory=(), unbound=None, pin_at=None):
    kwargs = {"candidate": CANDIDATE, "advisory": advisory, "pin_at": pin_at}
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


def _phase(sha: str, digest: str, lock: str) -> dict:
    return {"runtimeIdentity": {"imageDigest": digest, "sourceRevision": sha, "platformLockDigest": lock}}


def test_upgrade_receipt_matches_only_the_candidate_phase():
    """A→B→A must certify B. The prior install and the rollback must not certify A, and a SHA, digest,
    and lock taken from different phases must not assemble a candidate."""
    prior_sha, prior_digest, prior_lock = "d" * 40, "sha256:" + "e" * 64, "sha256:" + "f" * 64
    receipt = {
        "overallStatus": "pass",
        "priorLockDigest": prior_lock,
        "candidateLockDigest": LOCK,
        "phases": {
            "install": _phase(prior_sha, prior_digest, prior_lock),
            "candidate": _phase(SHA, DIGEST, LOCK),
            "rollback": _phase(prior_sha, prior_digest, prior_lock),
        },
    }
    assert w.matches_candidate(receipt, CANDIDATE)
    assert not w.matches_candidate(receipt, w.Candidate(prior_sha, prior_digest, prior_lock))
    assert not w.matches_candidate(receipt, w.Candidate(prior_sha, DIGEST, prior_lock))
    admitted = _eval({"gate-upgrade.yml": {"runs": [_run(0.3, "success", receipt)]}}, unbound={})
    assert admitted["admissible"] is True


def test_a_failed_upgrade_of_another_candidate_does_not_supersede():
    prior = _phase(SHA, DIGEST, LOCK)
    other_sha, other_digest, other_lock = "d" * 40, "sha256:" + "e" * 64, "sha256:" + "f" * 64
    failed = {
        "status": "fail",
        "priorLockDigest": LOCK,
        "candidateLockDigest": other_lock,
        "phases": {
            "install": prior,
            "candidate": _phase(other_sha, other_digest, other_lock),
            "rollback": prior,
        },
    }
    result = _eval({"gate-upgrade.yml": {"runs": [
        _run(2, "success", _report(), run_id=1),
        _run(0.2, "failure", failed, run_id=2),
    ]}}, unbound={})
    assert result["admissible"] is True
    assert result["gates"]["gate-upgrade.yml"]["verdict"] == "green"


def test_local_canary_artifact_name_is_evidence():
    assert w.artifact_carries_evidence("terminal-model-canary-local-docker-1")
    assert w.artifact_carries_evidence("terminal-model-canary-harness-receipt")
    assert w.artifact_carries_evidence("cloud-gate-report")
    assert w.artifact_carries_evidence("upgrade-kind-evidence-normal-pair")
    assert not w.artifact_carries_evidence("candidate-server-log")


def test_unbound_older_green_plus_newer_red_blocks():
    pin = NOW - dt.timedelta(days=5)
    records = {"manifest-validate.yml": {"runs": [
        _run(0.2, "failure", run_id=2),
        _run(2, "success", run_id=1),
    ]}}
    result = _eval(records, pin_at=pin)
    gate = result["gates"]["manifest-validate.yml"]
    assert result["admissible"] is False
    assert gate["verdict"] == "red"
    assert gate["reason"] != w.MANIFEST_PIN_PASS
    assert "supersedes an older green" in gate["reason"]
    assert "conclusion failure" in gate["reason"]


def test_unbound_run_before_the_pin_commit_does_not_count():
    pin = NOW - dt.timedelta(days=1)
    before = _eval({"manifest-validate.yml": {"runs": [_run(2, "success", run_id=1)]}}, pin_at=pin)
    assert before["admissible"] is False
    assert "before the manifest pin" in before["gates"]["manifest-validate.yml"]["reason"]
    # A run that starts at the pin commit's timestamp has not started after it.
    same = _eval({"manifest-validate.yml": {"runs": [_run(1, "success", run_id=3)]}}, pin_at=pin)
    assert same["admissible"] is False
    assert "before the manifest pin" in same["gates"]["manifest-validate.yml"]["reason"]


def test_unbound_latest_green_after_the_pin_is_manifest_pin_bound():
    pin = NOW - dt.timedelta(days=3)
    result = _eval({"manifest-validate.yml": {"runs": [_run(1, "success", run_id=4)]}}, pin_at=pin)
    gate = result["gates"]["manifest-validate.yml"]
    assert result["admissible"] is True
    assert gate["verdict"] == "green"
    assert gate["candidate_bound"] is False
    assert gate["binding"] == "manifest-pin"
    assert gate["reason"] == w.MANIFEST_PIN_PASS
    assert gate["last_green_build"] == "manifest-pin"
    assert result["manifest_pin_bound"] == ["manifest-validate.yml"]
    md = w.render_markdown(result)
    assert f"| `manifest-validate.yml` | ✅ {w.MANIFEST_PIN_PASS} |" in md
    assert w.UNBOUND_GATE_LIMITATIONS["manifest-validate.yml"] in md


def test_unbound_diagnostic_or_stale_or_missing_run_does_not_pass():
    pin = NOW - dt.timedelta(days=3)
    diagnostic = {"certifying": False, "requestedScope": {"target": "aws-eks", "redis": "off"}}
    focused = _eval({"manifest-validate.yml": {"runs": [
        _run(2, "success", run_id=1),
        _run(0.2, "success", diagnostic, run_id=2),
    ]}}, pin_at=pin)
    assert focused["admissible"] is False
    assert "diagnostic, focused, or partial" in focused["gates"]["manifest-validate.yml"]["reason"]
    assert "supersedes an older green" in focused["gates"]["manifest-validate.yml"]["reason"]
    # No report at all is not diagnostic. The workflow did not expose a focused scope.
    stale = _eval({"manifest-validate.yml": {"runs": [_run(7.01, "success")]}},
                  pin_at=NOW - dt.timedelta(days=10))
    assert "outside the 7-day window" in stale["gates"]["manifest-validate.yml"]["reason"]
    missing = _eval({"manifest-validate.yml": {"runs": []}}, pin_at=pin)
    assert "no completed trunk run" in missing["gates"]["manifest-validate.yml"]["reason"]
    unpinned = _eval({"manifest-validate.yml": {"runs": [_run(1, "success")]}})
    assert unpinned["admissible"] is False
    assert "pin commit was not found" in unpinned["gates"]["manifest-validate.yml"]["reason"]


def test_required_gates_pass_when_bound_match_and_unbound_are_manifest_pin_green():
    pin = NOW - dt.timedelta(days=3)
    records = {}
    for index, gate in enumerate(w.RC_GATES):
        records[gate] = {"runs": [_run(1, report=_report(), run_id=index + 1)]}
    result = _eval(records, advisory=w.RC_ADVISORY, pin_at=pin)
    assert result["admissible"] is True
    assert result["red"] == []
    md = w.render_markdown(result)
    for gate in w.RC_GATES:
        row = result["gates"][gate]
        assert row["verdict"] == "green"
        if gate in w.CANDIDATE_BOUND_GATES:
            assert row["reason"] != w.MANIFEST_PIN_PASS
            assert row.get("binding") != "manifest-pin"
        else:
            assert row["reason"] == w.MANIFEST_PIN_PASS
            assert row["binding"] == "manifest-pin"
            assert f"| `{gate}` | ✅ {w.MANIFEST_PIN_PASS} |" in md


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
    gate_job, _, publish_job = workflow.partition("\n  publish:")
    assert "fetch-depth: 0" in gate_job
    assert "fetch-depth: 0" not in publish_job


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


def test_main_refuses_when_the_pin_commit_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_TOKEN", "not-a-real-token")
    code = w.main(["--server-sha", SHA, "--server-digest", DIGEST, "--platform-lock", LOCK,
                   "--out", str(tmp_path / "out.json"), "--manifest", "platform-manifest.yaml"])
    assert code == 2
    assert "pin" in capsys.readouterr().err
    assert not (tmp_path / "out.json").exists()


def test_find_pin_time_is_the_commit_that_set_the_server_sha_field(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sha1 = "1" * 40
    sha2 = "2" * 40
    manifest = "platform-manifest.yaml"
    subprocess.run(["git", "-C", str(repo), "init", "-b", "trunk"], check=True, capture_output=True)

    def commit(message, body, when):
        (repo / manifest).write_text(body, encoding="utf-8")
        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "GIT_AUTHOR_DATE": when,
            "GIT_COMMITTER_DATE": when,
        })
        subprocess.run(["git", "-C", str(repo), "add", manifest], check=True, env=env, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", message], check=True, env=env,
                       capture_output=True)

    commit("pin sha1", f'components:\n  honua-server:\n    sha: "{sha1}"\n', "2026-10-01T00:00:00Z")
    # The diff line mentions sha2 as `sha: "..."`, which a pickaxe search sees. The field is still sha1.
    commit("mention sha2 without pinning it",
           f'components:\n  honua-server:\n    sha: "{sha1}"\n# planned sha: "{sha2}"\n',
           "2026-10-02T00:00:00Z")
    commit("pin sha2", f'components:\n  honua-server:\n    sha: "{sha2}"\n', "2026-10-03T00:00:00Z")
    assert w.find_pin_time(sha2, manifest, cwd=str(repo)) == dt.datetime(2026, 10, 3, tzinfo=dt.timezone.utc)
    # sha1's pin stays the commit that set the field, including after that line was removed.
    assert w.find_pin_time(sha1, manifest, cwd=str(repo)) == dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
    try:
        w.find_pin_time("e" * 40, manifest, cwd=str(repo))
    except ValueError as exc:
        assert "no commit pins" in str(exc)
    else:
        raise AssertionError("a missing pin commit must fail closed")


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
