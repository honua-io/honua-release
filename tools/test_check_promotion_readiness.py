import copy
import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import check_promotion_readiness as readiness
from candidate_binding import REQUIRED_RELEASE_GATES
import pytest
import yaml
from jsonschema import Draft202012Validator

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
OTHER_LOCK = "sha256:" + "f" * 64


def _stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _fixture(tmp_path, *, age=54):
    tmp_path.mkdir(parents=True, exist_ok=True)
    lock = tmp_path / "platform-lock.json"
    lock.write_text('{"lockVersion":"platform-lock.v1"}\n', encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(lock.read_bytes()).hexdigest()
    burn = NOW - timedelta(hours=age)
    evidence = tmp_path / "evidence"
    train = {"runId": "101", "completedAt": _stamp(burn), "status": "pass", "lockDigest": digest}
    report = {"overallStatus": "pass", "dry_run": False, "generatedAt": _stamp(burn),
              "candidate": {"train": {"runId": "101"}},
              "gates": [{"gate": gate, "status": "pass"} for gate in REQUIRED_RELEASE_GATES],
              "evidenceClasses": [key for key, tier in readiness.EVIDENCE_CLASSES.items() if tier == "nightly"]}
    root = evidence / "trains" / "101"
    _write(root / "gate-report.json", report)
    _write(root / "run.json", {"updated_at": train["completedAt"], "status": "completed", "conclusion": "success"})
    (root / "platform-lock.json").write_bytes(lock.read_bytes())
    canaries = []
    for index in range(7):
        run_id = str(201 + index)
        completed = NOW - timedelta(hours=6 * (6 - index))
        row = {"runId": run_id, "completedAt": _stamp(completed), "status": "pass", "lockDigest": digest}
        canaries.append(row)
        _write(evidence / "canaries" / run_id / "live-canary-evidence.json",
               {"runId": run_id, "status": "pass", "candidateLock": {"digest": digest}})
        _write(evidence / "canaries" / run_id / "run.json",
               {"updated_at": row["completedAt"], "status": "completed", "conclusion": "success"})
    _write(evidence / "canary-sequence.json", {"lockDigest": digest, "runs": canaries})
    rows = []
    for index, (name, tier) in enumerate(readiness.EVIDENCE_CLASSES.items()):
        run_id = "101" if tier == "nightly" else str(301 + index)
        completed = burn if tier == "nightly" else burn + timedelta(hours=12)
        row = {"class": name, "runId": run_id, "completedAt": _stamp(completed), "status": "pass", "lockDigest": digest}
        rows.append(row)
        receipt = {**row, "kind": tier, "freshUntil": _stamp(completed + timedelta(days=7))}
        if name in readiness.JOURNEYS:
            mode, cells = readiness.JOURNEYS[name]
            receipt["cells"] = [{"cell": cell, "mode": mode, "attemptCount": 1,
                                 "attempts": [{"attempt": 1, "status": "pass", "lockDigest": digest,
                                               "completedAt": row["completedAt"]}]} for cell in sorted(cells)]
        if name == "update-rollback":
            receipt.update(cells=sorted(readiness.GA_CELLS), updateStatus="pass", rollbackStatus="pass")
        root = evidence / "evidence" / name / run_id
        _write(root / "receipt.json", receipt)
        _write(root / "run.json", {"updated_at": row["completedAt"], "status": "completed", "conclusion": "success"})
    record = {"schemaVersion": "promotion-evidence.v1", "platformLabel": "2026.1-rc.3",
              "rcTrainRunId": "101", "lock": {"path": "platform-lock.json", "digest": digest,
              "burnStartedAt": _stamp(burn), "burnStartCommit": "a" * 40},
              "strictTrains": [train], "demoCanaries": canaries,
              "evidenceClasses": dict(readiness.EVIDENCE_CLASSES), "evidence": rows}
    history = tmp_path / "lock-history.txt"
    history.write_text("", encoding="utf-8")
    return record, lock, evidence, history


def _decision(fixture, *, now=NOW):
    record, lock, evidence, history = fixture
    return readiness.evaluate(record, lock_path=lock, evidence_dir=evidence, lock_history=history, now=now)[0]


def _edit(path, change):
    value = json.loads(path.read_text())
    change(value)
    _write(path, value)


def _receipt(fixture, name):
    row = next(row for row in fixture[0]["evidence"] if row["class"] == name)
    return fixture[2] / "evidence" / name / row["runId"] / "receipt.json"


def _failed(fixture, check):
    decision = _decision(fixture)
    assert decision["status"] == "refused"
    assert decision["checks"][check]["status"] == "fail"


def test_complete_record_promotes_exact_minting_lock(tmp_path):
    fixture = _fixture(tmp_path)
    assert _decision(fixture)["status"] == "pass"
    assert _decision(fixture)["rcTrainRunId"] == "101"


@pytest.mark.parametrize("age", [48, 73, 96])
def test_promotion_from_hour_48_has_no_72_hour_deadline(tmp_path, age):
    assert _decision(_fixture(tmp_path, age=age))["status"] == "pass"


def test_new_trunk_commits_new_locks_and_other_lock_failures_do_not_reset_burn(tmp_path):
    fixture = _fixture(tmp_path, age=96)
    fixture[3].write_text("new trunk commit\nnew platform lock\nrevert to previous lock\n")
    _write(fixture[2] / "train-sequence.json", [{"workflow_runs": [{"id": 999, "conclusion": "failure"}]}])
    _edit(fixture[2] / "canary-sequence.json", lambda sequence: sequence["runs"].append(
        {"runId": "999", "lockDigest": OTHER_LOCK, "status": "fail", "completedAt": _stamp(NOW)}))
    assert _decision(fixture)["status"] == "pass"


def test_selected_lock_bytes_must_match(tmp_path):
    fixture = _fixture(tmp_path)
    fixture[1].write_text('{"another":"lock"}')
    _failed(fixture, "lock-digest")


def test_burn_under_48_hours_refused(tmp_path):
    _failed(_fixture(tmp_path, age=47), "burn-duration")


@pytest.mark.parametrize("mutation", ["missing", "extra", "dry-run", "failed-gate", "wrong-lock", "after-start", "metadata", "source"])
def test_minting_train_rules(tmp_path, mutation):
    fixture = _fixture(tmp_path)
    record, _, evidence, _ = fixture
    root = evidence / "trains" / "101"
    check = "minting-train"
    if mutation == "missing": record["strictTrains"] = []
    elif mutation == "extra": record["strictTrains"].append(copy.deepcopy(record["strictTrains"][0]))
    elif mutation == "dry-run": _edit(root / "gate-report.json", lambda r: r.update(dry_run=True))
    elif mutation == "failed-gate": _edit(root / "gate-report.json", lambda r: r["gates"][0].update(status="skipped"))
    elif mutation == "wrong-lock": (root / "platform-lock.json").write_text('{}')
    elif mutation == "after-start":
        stamp = _stamp(NOW - timedelta(hours=53))
        record["strictTrains"][0]["completedAt"] = stamp
        _edit(root / "run.json", lambda r: r.update(updated_at=stamp))
    elif mutation == "metadata": _edit(root / "run.json", lambda r: r.update(conclusion="failure"))
    elif mutation == "source": record["rcTrainRunId"] = "999"; check = "exact-rc"
    _failed(fixture, check)


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong-lock", "receipt-other-lock", "failed", "cadence", "stale", "future", "omitted-failure", "missing-sequence"])
def test_canary_and_lock_failure_rules(tmp_path, mutation):
    fixture = _fixture(tmp_path)
    record, _, evidence, _ = fixture
    check = "demo-canaries"
    if mutation == "missing": record["demoCanaries"].pop()
    elif mutation == "duplicate": record["demoCanaries"][3] = copy.deepcopy(record["demoCanaries"][2])
    elif mutation == "wrong-lock": record["demoCanaries"][3]["lockDigest"] = OTHER_LOCK
    elif mutation == "receipt-other-lock":
        _edit(evidence / "canaries/204/live-canary-evidence.json", lambda r: r["candidateLock"].update(digest=OTHER_LOCK))
    elif mutation == "failed": _edit(evidence / "canaries/204/live-canary-evidence.json", lambda r: r.update(status="fail"))
    elif mutation in ("cadence", "stale", "future"):
        for index, row in enumerate(record["demoCanaries"]):
            shift = -7 if mutation == "stale" else (1 if mutation == "future" else (2 if index == 3 else 0))
            row["completedAt"] = _stamp(datetime.fromisoformat(row["completedAt"].replace("Z", "+00:00")) + timedelta(hours=shift))
            _edit(evidence / "canaries" / row["runId"] / "run.json", lambda r: r.update(updated_at=row["completedAt"]))
        _write(evidence / "canary-sequence.json", {"lockDigest": record["lock"]["digest"], "runs": record["demoCanaries"]})
    elif mutation == "omitted-failure":
        _edit(evidence / "canary-sequence.json", lambda r: r["runs"].insert(0,
              {"runId": "199", "completedAt": _stamp(NOW - timedelta(hours=48)), "status": "fail", "lockDigest": record["lock"]["digest"]}))
        check = "lock-burn-health"
    elif mutation == "missing-sequence": (evidence / "canary-sequence.json").unlink()
    _failed(fixture, check)


@pytest.mark.parametrize("name", list(readiness.EVIDENCE_CLASSES))
def test_every_required_class_is_required(tmp_path, name):
    fixture = _fixture(tmp_path)
    fixture[0]["evidence"] = [r for r in fixture[0]["evidence"] if r["class"] != name]
    _failed(fixture, "evidence-declarations")


@pytest.mark.parametrize("mutation", ["undeclared-receipt", "undeclared-train-class", "reclassified", "duplicate"])
def test_evidence_declaration_rules(tmp_path, mutation):
    fixture = _fixture(tmp_path)
    record, _, evidence, _ = fixture
    if mutation == "undeclared-receipt": record["evidence"].append({**record["evidence"][0], "class": "unreviewed"})
    elif mutation == "undeclared-train-class": _edit(evidence / "trains/101/gate-report.json", lambda r: r["evidenceClasses"].append("unreviewed"))
    elif mutation == "reclassified": record["evidenceClasses"]["cite"] = "nightly"
    elif mutation == "duplicate": record["evidence"].append(copy.deepcopy(record["evidence"][0]))
    _failed(fixture, "evidence-declarations")


@pytest.mark.parametrize("name", list(readiness.EVIDENCE_CLASSES))
def test_receipt_from_another_lock_is_refused_for_every_class(tmp_path, name):
    fixture = _fixture(tmp_path)
    _edit(_receipt(fixture, name), lambda r: r.update(lockDigest=OTHER_LOCK))
    _failed(fixture, f"evidence:{name}")


@pytest.mark.parametrize("mutation", ["missing", "failed", "stale", "no-bound", "before-burn", "wrong-run", "metadata", "over-14-days"])
def test_qualifying_receipt_rules(tmp_path, mutation):
    fixture = _fixture(tmp_path)
    path = _receipt(fixture, "cite")
    if mutation == "missing": path.unlink()
    elif mutation == "failed": _edit(path, lambda r: r.update(status="skipped"))
    elif mutation == "stale": _edit(path, lambda r: r.update(freshUntil=_stamp(NOW - timedelta(seconds=1))))
    elif mutation == "no-bound": _edit(path, lambda r: r.pop("freshUntil"))
    elif mutation == "before-burn":
        row = next(r for r in fixture[0]["evidence"] if r["class"] == "cite")
        row["completedAt"] = _stamp(NOW - timedelta(hours=55))
        _edit(path, lambda r: r.update(completedAt=row["completedAt"]))
        _edit(path.parent / "run.json", lambda r: r.update(updated_at=row["completedAt"]))
    elif mutation == "wrong-run": _edit(path, lambda r: r.update(runId="999"))
    elif mutation == "metadata": _edit(path.parent / "run.json", lambda r: r.update(conclusion="cancelled"))
    elif mutation == "over-14-days": _edit(path, lambda r: r.update(freshUntil=_stamp(NOW + timedelta(days=15))))
    _failed(fixture, "evidence:cite")


@pytest.mark.parametrize("name", list(readiness.JOURNEYS))
@pytest.mark.parametrize("mutation", ["missing-cell", "duplicate-cell", "preview-substitute", "wrong-mode", "third-attempt", "unrecorded-attempt", "unattributed-failure", "skipped", "wrong-lock", "stale-attempt", "gap"])
def test_journey_pass_rule_has_a_negative_per_rule_and_tier(tmp_path, name, mutation):
    fixture = _fixture(tmp_path)
    def change(receipt):
        cell = receipt["cells"][0]
        attempt = cell["attempts"][0]
        if mutation == "missing-cell": receipt["cells"].pop(0)
        elif mutation == "duplicate-cell": receipt["cells"].append(copy.deepcopy(cell))
        elif mutation == "preview-substitute": cell["cell"] = "aws-eks/redis-off"
        elif mutation == "wrong-mode": cell["mode"] = "replayed"
        elif mutation == "third-attempt":
            cell["attemptCount"] = 3
            cell["attempts"] = [{**attempt, "attempt": index} for index in (1, 2, 3)]
        elif mutation == "unrecorded-attempt": cell["attemptCount"] = 2
        elif mutation == "unattributed-failure":
            cell["attemptCount"] = 2
            cell["attempts"] = [{**attempt, "status": "fail"}, {**attempt, "attempt": 2}]
        elif mutation == "skipped": attempt["status"] = "skipped"
        elif mutation == "wrong-lock": attempt["lockDigest"] = OTHER_LOCK
        elif mutation == "stale-attempt": attempt["completedAt"] = _stamp(NOW - timedelta(days=8))
        elif mutation == "gap": attempt["attempt"] = 2
    _edit(_receipt(fixture, name), change)
    _failed(fixture, f"evidence:{name}")


@pytest.mark.parametrize("attribution", ["model", "infrastructure"])
def test_second_attempt_pass_and_preview_failure_are_allowed(tmp_path, attribution):
    fixture = _fixture(tmp_path)
    def change(receipt):
        for cell in receipt["cells"]:
            attempt = cell["attempts"][0]
            cell["attemptCount"] = 2
            cell["attempts"] = [{**attempt, "status": "fail", "failureAttribution": attribution}, {**attempt, "attempt": 2}]
        receipt["cells"].append({"cell": "aws-eks/redis-off", "status": "fail"})
    for name in readiness.JOURNEYS:
        _edit(_receipt(fixture, name), change)
    assert _decision(fixture)["status"] == "pass"


def test_update_and_rollback_must_both_pass_all_ga_cells(tmp_path):
    fixture = _fixture(tmp_path)
    _edit(_receipt(fixture, "update-rollback"), lambda r: r.update(rollbackStatus="fail"))
    _failed(fixture, "evidence:update-rollback")


def test_cli_refuses_missing_retained_evidence_and_writes_decision(tmp_path):
    fixture = _fixture(tmp_path)
    record, lock, evidence, _ = fixture
    (evidence / "canary-sequence.json").unlink()
    record_path, out = tmp_path / "record.json", tmp_path / "decision.json"
    _write(record_path, record)
    assert readiness.main(["--record", str(record_path), "--lock", str(lock), "--evidence-dir", str(evidence),
                           "--now", _stamp(NOW), "--out", str(out)]) == 1
    assert json.loads(out.read_text())["status"] == "refused"


@pytest.mark.parametrize("label,accepted", [
    ("2026.1-rc.1", True), ("2026.1.0-rc.1", True), ("2026.1.2-rc.3", True),
    ("2026.1.0-rc.0", False), ("2026.1.0", False), ("2026.1.0-rc.latest", False),
    ("../2026.1-rc.1", False), ("2026.1.0.0-rc.1", False),
])
def test_promotion_path_schema_and_readiness_agree_on_patch_rc(tmp_path, label, accepted):
    root = Path(__file__).resolve().parents[1]
    record, lock, evidence, history = _fixture(tmp_path)
    record["platformLabel"] = label
    decision, failures = readiness.evaluate(record, lock_path=lock, evidence_dir=evidence,
                                             lock_history=history, now=NOW)
    assert (decision["status"] == "pass") == accepted, failures
    schema = json.loads((root / "certification/promotion-evidence.v1.schema.json").read_text())
    assert Draft202012Validator(schema).is_valid(record) == accepted
    workflow = yaml.safe_load((root / ".github/workflows/promote.yml").read_text())
    command = next(s["run"] for s in workflow["jobs"]["promote"]["steps"]
                   if "canonical committed RC record path" in s.get("run", ""))
    # Execute the actual path admission condition without performing any GitHub calls.
    condition = next(line.strip().split(" ||", 1)[0] for line in command.splitlines()
                     if line.strip().startswith('[[ "$PROMOTION_RECORD"'))
    result = subprocess.run(["bash", "-c", condition], env={**os.environ,
                            "PROMOTION_RECORD": f"certification/promotions/{label}.json"})
    assert (result.returncode == 0) == accepted


@pytest.mark.parametrize("name", list(readiness.EVIDENCE_CLASSES))
def test_any_expired_evidence_closes_promotion_window(tmp_path, name):
    fixture = _fixture(tmp_path, age=96)
    _edit(_receipt(fixture, name), lambda receipt: receipt.update(freshUntil=_stamp(NOW - timedelta(seconds=1))))
    _failed(fixture, f"evidence:{name}")


def test_expiry_boundary_is_inclusive(tmp_path):
    fixture = _fixture(tmp_path)
    _edit(_receipt(fixture, "cite"), lambda receipt: receipt.update(freshUntil=_stamp(NOW)))
    assert _decision(fixture)["status"] == "pass"


@pytest.mark.parametrize("mutation", ["missing-cell", "preview-substitute", "update-failed"])
def test_update_rollback_coverage_cannot_be_substituted(tmp_path, mutation):
    fixture = _fixture(tmp_path)
    def change(receipt):
        if mutation == "missing-cell": receipt["cells"].pop()
        elif mutation == "preview-substitute": receipt["cells"][0] = "aws-eks/redis-off"
        else: receipt["updateStatus"] = "fail"
    _edit(_receipt(fixture, "update-rollback"), change)
    _failed(fixture, "evidence:update-rollback")


@pytest.mark.parametrize("content", ['{"status":"fail","status":"pass"}', '{"status":NaN}', '[]'])
def test_ambiguous_receipt_json_is_refused(tmp_path, content):
    path = tmp_path / "receipt.json"
    path.write_text(content)
    with pytest.raises(readiness.ReadinessError):
        readiness._load(path, "receipt")


def test_cli_writes_exact_minting_run_and_digest_outputs(tmp_path):
    record, lock, evidence, _ = _fixture(tmp_path, age=96)
    record_path, out, github_output = tmp_path / "record.json", tmp_path / "decision.json", tmp_path / "github-output"
    _write(record_path, record)
    assert readiness.main(["--record", str(record_path), "--lock", str(lock), "--evidence-dir", str(evidence),
                           "--now", _stamp(NOW), "--out", str(out), "--github-output", str(github_output)]) == 0
    assert json.loads(out.read_text())["status"] == "pass"
    assert github_output.read_text() == f"rc_train_run_id=101\nlock_digest={record['lock']['digest']}\n"


def test_schema_rejects_unclassified_and_reclassified_required_classes(tmp_path):
    record, *_ = _fixture(tmp_path)
    schema = json.loads((Path(__file__).resolve().parents[1] / "certification/promotion-evidence.v1.schema.json").read_text())
    validator = Draft202012Validator(schema)
    Draft202012Validator.check_schema(schema)
    assert validator.is_valid(record)
    record["evidenceClasses"]["cite"] = "nightly"
    assert not validator.is_valid(record)
    del record["evidenceClasses"]["cite"]
    assert not validator.is_valid(record)
