"""Authenticated producers fail closed and bind receipts to an installed cohort."""
import datetime as dt
import json
from pathlib import Path

import pytest
import yaml

import authenticated_producers as producers
import outcome_eval as evaluation
import rc_gate_window as gate
import release_outcomes as outcomes
import release_promise

ROOT = Path(__file__).resolve().parents[1]
STACK = "honua-gate-outcome"
CANDIDATE = {"serverSha": "a" * 40, "imageDigest": "sha256:" + "b" * 64,
             "platformLockDigest": "sha256:" + "c" * 64}
SENTENCES = {"install": 1, "upload-publish": 2, "style": 3, "share": 4, "build-app": 5,
             "sign-in-roles": 6, "recover-incident": 7, "migrate-service": 8}
ENDS = {("upload-publish", "agent", "D"): 120, ("upload-publish", "agent", "A"): 60,
        ("build-app", "agent", "D"): 180, ("build-app", "agent", "A"): 240,
        ("install", "browser", None): 300, ("upload-publish", "browser", None): 360,
        ("build-app", "browser", None): 420}


def environment(monkeypatch):
    monkeypatch.setenv("SERVER_SHA", CANDIDATE["serverSha"])
    monkeypatch.setenv("SERVER_DIGEST", CANDIDATE["imageDigest"])
    monkeypatch.setenv("LOCK_DIGEST", CANDIDATE["platformLockDigest"])
    monkeypatch.setenv("ADMISSION_DIR", "admission")


def store(root, manifest, name, value, raw=None):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw if raw is not None else json.dumps(value))
    manifest["files"][name] = evaluation.digest(path)


def record(root, manifest, plan, trial_id, status="success", sentences=1, end=60, field=False):
    entry = next(row for row in manifest["trials"] if row["trialId"] == trial_id)
    trial = next(row for row in plan["trials"] + plan["baselines"] if row["id"] == trial_id)
    events = [{"id": "start", "type": "dispatch", "seconds": 0}]
    if sentences:
        events.append({"id": "brief", "type": "person", "seconds": 0,
                       "text": "Go. " * sentences, "sentences": ["Go."] * sentences})
    events.append({"id": "click", "type": "click", "seconds": 1, "count": 2})
    if field:
        events.append({"id": "typed", "type": "field", "seconds": 2, "fieldId": "name", "characters": 4})
    events.append({"id": "end", "seconds": end,
                   "type": "terminal-readback" if status == "success" else "stop"})
    event_name = f"{trial_id}.jsonl"
    grade_name = f"{trial_id}.grade.json"
    readback_name = f"{trial_id}.readback.json"
    (root / event_name).write_text("\n".join(json.dumps(event) for event in events) + "\n")
    manifest["files"][event_name] = evaluation.digest(root / event_name)
    readback = dict(CANDIDATE, stackName=STACK)
    store(root, manifest, readback_name, readback)
    grade = {"trialId": trial_id, "workflow": trial["workflow"], "candidate": dict(CANDIDATE),
             "modality": "agent" if "model" in trial else "browser", "status": status,
             "grader": "independent-grader", "reason": "Graded from the installed readback",
             "postconditionsPassed": True, "controlsPassed": True, "sentenceBoundariesReviewed": True,
             "terminalReadback": readback_name,
             "evidence": {event_name: manifest["files"][event_name],
                          readback_name: manifest["files"][readback_name]}}
    store(root, manifest, grade_name, grade)
    entry.update(eligible=status != "excluded", events=event_name, grade=grade_name)


def build_admission(root, *, migrate=True, blocked=True, harness_stack=STACK):
    plan = evaluation.plan()
    manifest = {"schema": "honua.outcome-cohort/v2", "plan": "plan.json", "files": {},
                "candidate": dict(CANDIDATE),
                "trials": [{"trialId": row["id"], "eligible": False, "reason": "Not in this fixture slice"}
                           for row in plan["trials"] + plan["baselines"]]}
    store(root, manifest, "plan.json", plan)
    for workflow, sentences in SENTENCES.items():
        if workflow == "migrate-service" and not migrate:
            continue
        record(root, manifest, plan, f"r1-gpt-6.1-sol-{workflow}-D", sentences=sentences,
               end=ENDS.get((workflow, "agent", "D"), 60))
    record(root, manifest, plan, "r1-gpt-6.1-sol-upload-publish-A", sentences=2,
           end=ENDS[("upload-publish", "agent", "A")])
    record(root, manifest, plan, "r1-gpt-6.1-sol-build-app-A", sentences=5,
           end=ENDS[("build-app", "agent", "A")])
    record(root, manifest, plan, "r1-gpt-6.1-sol-style-B", status="censored", sentences=9, end=60)
    for workflow in SENTENCES:
        record(root, manifest, plan, f"r1-browser-{workflow}", sentences=0,
               end=ENDS.get((workflow, "browser", None), 60), field=workflow == "install")
    proof = {"candidate": dict(CANDIDATE), "stackName": harness_stack, "phases": ["setup", "readback", "cleanup"],
             "injection": {}, "recovery": {}, "unattended": {"evidence": "unattended.json"}}
    for channel in producers.INJECTION_CHANNELS:
        name = f"injection/{channel}.json"
        proof["injection"][channel] = name
        store(root, manifest, name, {"channel": channel, "blocked": blocked})
    recovery_ids = sorted(row["id"] for row in producers.contract_rows(producers.OUTCOME_WORKFLOW)
                          if row["id"].startswith("recovery-operates-"))
    for row_id in recovery_ids:
        name = f"recovery/{row_id}.json"
        proof["recovery"][row_id] = {"evidence": name}
        store(root, manifest, name, {"row": row_id, "stackName": harness_stack, "startSeconds": 0, "endSeconds": 90})
    store(root, manifest, "unattended.json",
          {"row": "unattended-person", "stackName": harness_stack, "startSeconds": 10, "endSeconds": 10})
    store(root, manifest, "harness.json", proof)
    (root / "cohort.json").write_text(json.dumps(manifest))
    return proof


def admit(monkeypatch, tmp_path, **kwargs):
    monkeypatch.chdir(tmp_path)
    environment(monkeypatch)
    admission = tmp_path / "admission"
    admission.mkdir(exist_ok=True)
    proof = build_admission(admission, **kwargs)
    monkeypatch.setattr(producers, "installed_proof", lambda _admission, _candidate: proof)
    return admission, proof


def run(mode="outcomes"):
    return producers.main(["authenticated_producers.py", mode])


def window(document):
    """A finished Actions run that contains the receipt's observedAt."""
    from execution_order import instant
    observed = instant(document["observedAt"])
    now = observed + dt.timedelta(minutes=5)
    record = {"run_started_at": (observed - dt.timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "updated_at": (observed + dt.timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}
    return now, record


def test_missing_harness_leaves_no_receipt(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    environment(monkeypatch)
    admission = tmp_path / "admission"
    admission.mkdir()
    build_admission(admission)
    (tmp_path / "sentences-install.json").write_text("stale PASS")
    assert run() == 1
    assert not (tmp_path / "sentences-install.json").exists()
    assert not (tmp_path / "release-notes.md").exists()
    assert list(tmp_path.glob("ai-*.json")) == []


def test_incomplete_candidate_leaves_no_receipt(monkeypatch, tmp_path):
    admit(monkeypatch, tmp_path)
    monkeypatch.delenv("SERVER_SHA")
    (tmp_path / "ai-operate.json").write_text("stale")
    assert run("ai") == 1
    assert not (tmp_path / "ai-operate.json").exists()


def test_absolute_admission_path_is_refused(monkeypatch, tmp_path):
    admit(monkeypatch, tmp_path)
    monkeypatch.setenv("ADMISSION_DIR", str(tmp_path / "admission"))
    assert run() == 1
    assert not (tmp_path / "release-notes.md").exists()


def test_identity_mismatch_leaves_no_receipt(monkeypatch, tmp_path):
    admit(monkeypatch, tmp_path)
    monkeypatch.setenv("SERVER_SHA", "d" * 40)
    assert run() == 1
    assert list(tmp_path.glob("*.json")) == []


def test_forged_evidence_leaves_no_receipt(monkeypatch, tmp_path):
    admission, _proof = admit(monkeypatch, tmp_path)
    target = admission / "r1-gpt-6.1-sol-install-D.jsonl"
    target.write_bytes(target.read_bytes() + b" ")
    (tmp_path / "sentences-install.json").write_text("stale")
    assert run() == 1
    assert not (tmp_path / "sentences-install.json").exists()


def test_missing_evidence_leaves_no_receipt(monkeypatch, tmp_path):
    admission, _proof = admit(monkeypatch, tmp_path)
    (admission / "r1-browser-share.readback.json").unlink()
    assert run() == 1
    assert not (tmp_path / "release-notes.md").exists()


def test_grade_candidate_mismatch_leaves_no_receipt(monkeypatch, tmp_path):
    admission, _proof = admit(monkeypatch, tmp_path)
    grade_path = admission / "r1-gpt-6.1-sol-migrate-service-D.grade.json"
    grade = json.loads(grade_path.read_text())
    grade["candidate"]["serverSha"] = "e" * 40
    grade_path.write_text(json.dumps(grade))
    manifest = json.loads((admission / "cohort.json").read_text())
    manifest["files"][grade_path.name] = evaluation.digest(grade_path)
    (admission / "cohort.json").write_text(json.dumps(manifest))
    assert run("ai") == 1
    assert not (tmp_path / "ai-migrate.json").exists()


def test_censored_trials_stay_out_of_the_success_median(monkeypatch, tmp_path):
    admit(monkeypatch, tmp_path)
    assert run() == 0
    style = json.loads((tmp_path / "sentences-style.json").read_text())
    assert style["value"] == 3
    assert style["assertions"]["censored"] == 1
    assert style["assertions"]["successful"] == 1
    assert style["assertions"]["planned"] == 24
    notes = (tmp_path / "release-notes.md").read_text().splitlines()
    assert "<!-- honua-outcome:sentences-style --> 3 sentences" in notes


def test_outcome_receipts_match_the_contract_and_keep_every_trial(monkeypatch, tmp_path):
    admit(monkeypatch, tmp_path)
    assert run() == 0
    rows = {row["id"]: row for row in producers.contract_rows(producers.OUTCOME_WORKFLOW)}
    assert "recovery-operates-restore" not in rows
    notes = (tmp_path / "release-notes.md").read_text()
    candidate = gate.Candidate(CANDIDATE["serverSha"], CANDIDATE["imageDigest"], CANDIDATE["platformLockDigest"])
    expected = {
        "sentences-install": 1, "sentences-publish-layer": 2, "sentences-migrate-service": 8,
        "clicks-install": 3, "clicks-style": 2,
        "developer-map-with-skills": 2, "developer-map-without-skills": 1,
        "developer-app-with-skills": 3, "developer-app-without-skills": 4,
        "install-console": 5, "studio-publish-layer": 6, "studio-build-app": 7,
        "unattended-person": 0, "recovery-operates-1": 1.5,
    }
    for row_id, row in rows.items():
        document = json.loads((tmp_path / row["receipt"]).read_text())
        document["releaseNotes"] = notes
        assert outcomes.valid(document)
        assert release_promise._receipt_valid(document, row, (candidate, *window(document)))
        if row_id in expected:
            assert document["value"] == expected[row_id]
    assert not (tmp_path / "recovery-operates-restore.json").exists()


def test_hostile_input_and_migrate_fail_closed(monkeypatch, tmp_path):
    admit(monkeypatch, tmp_path, blocked=False)
    (tmp_path / "ai-injection.json").write_text("stale")
    assert run("ai") == 1
    assert not (tmp_path / "ai-injection.json").exists()
    admit(monkeypatch, tmp_path, migrate=False)
    assert run("ai") == 1
    assert not (tmp_path / "ai-migrate.json").exists()


def test_installed_stack_mismatch_leaves_no_receipt(monkeypatch, tmp_path):
    _admission, proof = admit(monkeypatch, tmp_path)
    proof["stackName"] = "honua-gate-other"
    assert run("ai") == 1
    assert list(tmp_path.glob("ai-*.json")) == []


def test_ai_receipts_require_migrate_and_blocked_hostile_input(monkeypatch, tmp_path):
    admit(monkeypatch, tmp_path)
    assert run("ai") == 0
    migrate = json.loads((tmp_path / "ai-migrate.json").read_text())
    injection = json.loads((tmp_path / "ai-injection.json").read_text())
    assert migrate["assertions"]["live"] is True
    assert migrate["assertions"]["journey"] == "migrate-service"
    assert migrate["assertions"]["successfulArmD"] == 1
    assert injection["assertions"] == {"live": True, "hostileInputBlocked": True,
                                       "channels": ["logs", "layer-names", "metadata"]}
    assert migrate["stackName"] == STACK
    candidate = gate.Candidate(CANDIDATE["serverSha"], CANDIDATE["imageDigest"], CANDIDATE["platformLockDigest"])
    for row in producers.contract_rows(producers.AI_WORKFLOW):
        document = json.loads((tmp_path / row["receipt"]).read_text())
        assert release_promise._receipt_valid(document, row, (candidate, *window(document)))


def test_workflows_run_on_trunk_in_the_gate_environment_and_upload_contract_receipts():
    for name, mode, sample in (
            ("gate-ai-native.yml", "ai", "ai-migrate.json"),
            ("gate-release-outcomes.yml", "outcomes", "sentences-migrate-service.json")):
        workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        assert set(workflow[True]) == {"workflow_dispatch"}
        assert workflow["permissions"] == {"contents": "read"}
        job = workflow["jobs"]["produce"]
        assert job["environment"] == "gate"
        assert job["if"] == "github.repository == 'honua-io/honua-release' && github.ref == 'refs/heads/trunk'"
        step = next(item for item in job["steps"] if item.get("name") == "Produce receipts or leave none")
        install = next(i for i, item in enumerate(job["steps"]) if item.get("run") == "python -m pip install PyYAML")
        assert install < job["steps"].index(step)
        assert step["env"]["ADMISSION_DIR"] == "${{ inputs.admission_dir }}"
        assert f"python tools/authenticated_producers.py {mode}" in step["run"]
        upload = job["steps"][-1]
        assert upload["if"] == "always()"
        assert upload["with"]["name"] == "release-promise-receipts"
        assert sample in upload["with"]["path"]
        assert "release-notes.md" in upload["with"]["path"] or mode == "ai"
        assert upload["with"]["if-no-files-found"] == "error"
