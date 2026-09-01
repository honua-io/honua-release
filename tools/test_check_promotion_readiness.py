import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import check_promotion_readiness as readiness


NOW = datetime(2026, 9, 3, 12, tzinfo=timezone.utc)


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _fixture(tmp_path: Path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    lock = tmp_path / "platform-lock.json"
    lock.write_text('{"lockVersion":"platform-lock.v1"}\n', encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(lock.read_bytes()).hexdigest()
    burn = NOW - timedelta(hours=54)
    trains = []
    for phase, run_id, completed in (
        ("freeze", "101", burn), ("during", "102", burn + timedelta(hours=24)),
        ("after", "103", burn + timedelta(hours=50)),
    ):
        row = {"phase": phase, "runId": run_id, "completedAt": completed.isoformat().replace("+00:00", "Z"),
               "status": "pass", "lockDigest": digest}
        trains.append(row)
        root = tmp_path / "evidence" / "trains" / run_id
        _write(root / "gate-report.json", {"overallStatus": "pass", "dry_run": False,
                                            "candidate": {"train": {"runId": run_id}}})
        _write(root / "run.json", {"updated_at": row["completedAt"]})
        (root / "platform-lock.json").write_bytes(lock.read_bytes())
    canaries = []
    for index in range(7):
        run_id = str(201 + index)
        completed = burn + timedelta(hours=6 * (index + 3))
        canaries.append({"runId": run_id, "completedAt": completed.isoformat().replace("+00:00", "Z"),
                         "status": "pass", "lockDigest": digest})
        _write(tmp_path / "evidence" / "canaries" / run_id / "live-canary-evidence.json",
               {"runId": run_id, "status": "pass", "candidateLock": {"digest": digest}})
        _write(tmp_path / "evidence" / "canaries" / run_id / "run.json",
               {"updated_at": canaries[-1]["completedAt"]})
    record = {
        "schemaVersion": "promotion-evidence.v1", "platformLabel": "2026.1-rc.1",
        "rcTrainRunId": "101",
        "lock": {"path": "platform-lock.json", "digest": digest,
                 "burnStartedAt": burn.isoformat().replace("+00:00", "Z"), "burnStartCommit": "a" * 40},
        "strictTrains": trains, "demoCanaries": canaries,
    }
    _write(tmp_path / "evidence" / "train-sequence.json", [{"workflow_runs": [
        {"id": int(row["runId"]), "display_title": "release-train 2026.1-rc.1 (dry_run=false)",
         "updated_at": row["completedAt"], "status": "completed", "conclusion": "success"}
        for row in trains
    ]}])
    history = tmp_path / "lock-history.txt"
    history.write_text("", encoding="utf-8")
    return record, lock, tmp_path / "evidence", history


def _evaluate(tmp_path: Path, record: dict):
    _, lock, evidence, history = _fixture(tmp_path)
    return readiness.evaluate(record, lock_path=lock, evidence_dir=evidence, lock_history=history, now=NOW)


def test_complete_record_promotes_exact_freeze_rc(tmp_path):
    record, lock, evidence, history = _fixture(tmp_path)
    decision, failures = readiness.evaluate(record, lock_path=lock, evidence_dir=evidence,
                                             lock_history=history, now=NOW)
    assert failures == []
    assert decision["status"] == "pass"
    assert decision["rcTrainRunId"] == "101"
    assert set(decision["checks"]) == {"record-schema", "platform-label", "lock-digest", "lock-unchanged",
                                        "burn-window", "strict-trains", "demo-canaries", "exact-rc"}


def test_lock_digest_change_resets_every_candidate_bound_condition(tmp_path):
    record, lock, evidence, history = _fixture(tmp_path)
    lock.write_text('{"lockVersion":"platform-lock.v1","changed":true}\n', encoding="utf-8")
    history.write_text("deadbeef\n", encoding="utf-8")
    decision, failures = readiness.evaluate(record, lock_path=lock, evidence_dir=evidence,
                                             lock_history=history, now=NOW)
    assert decision["checks"]["lock-digest"]["status"] == "fail"
    assert decision["checks"]["lock-unchanged"]["status"] == "fail"
    assert failures


def test_reverted_lock_still_resets_because_history_is_not_empty(tmp_path):
    record, lock, evidence, history = _fixture(tmp_path)
    history.write_text("abc123 changed platform lock\ndef456 reverted platform lock\n", encoding="utf-8")
    decision, _ = readiness.evaluate(record, lock_path=lock, evidence_dir=evidence,
                                     lock_history=history, now=NOW)
    assert decision["checks"]["lock-digest"]["status"] == "pass"
    assert decision["checks"]["lock-unchanged"]["status"] == "fail"


def test_refuses_too_short_or_expired_burn(tmp_path):
    for hours in (47, 73):
        record, lock, evidence, history = _fixture(tmp_path / str(hours))
        record["lock"]["burnStartedAt"] = (NOW - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")
        decision, _ = readiness.evaluate(record, lock_path=lock, evidence_dir=evidence,
                                         lock_history=history, now=NOW)
        assert decision["checks"]["burn-window"]["status"] == "fail"


def test_refuses_nonconsecutive_or_wrong_lock_canary(tmp_path):
    record, lock, evidence, history = _fixture(tmp_path)
    record["demoCanaries"][3]["completedAt"] = record["demoCanaries"][4]["completedAt"]
    record["demoCanaries"][5]["lockDigest"] = "sha256:" + "f" * 64
    decision, _ = readiness.evaluate(record, lock_path=lock, evidence_dir=evidence,
                                     lock_history=history, now=NOW)
    assert decision["checks"]["demo-canaries"]["status"] == "fail"


def test_refuses_stale_canary_coverage(tmp_path):
    record, lock, evidence, history = _fixture(tmp_path)
    for row in record["demoCanaries"]:
        stale = datetime.fromisoformat(row["completedAt"].replace("Z", "+00:00")) - timedelta(hours=7)
        row["completedAt"] = stale.isoformat().replace("+00:00", "Z")
        _write(evidence / "canaries" / row["runId"] / "run.json", {"updated_at": row["completedAt"]})
    decision, _ = readiness.evaluate(record, lock_path=lock, evidence_dir=evidence,
                                     lock_history=history, now=NOW)
    assert decision["checks"]["demo-canaries"]["status"] == "fail"


def test_refuses_missing_or_duplicate_strict_train(tmp_path):
    record, lock, evidence, history = _fixture(tmp_path)
    record["strictTrains"][2]["runId"] = "102"
    decision, _ = readiness.evaluate(record, lock_path=lock, evidence_dir=evidence,
                                     lock_history=history, now=NOW)
    assert decision["checks"]["strict-trains"]["status"] == "fail"
    assert decision["checks"]["exact-rc"]["status"] == "pass"


def test_refuses_an_omitted_intervening_strict_train(tmp_path):
    record, lock, evidence, history = _fixture(tmp_path)
    sequence = json.loads((evidence / "train-sequence.json").read_text(encoding="utf-8"))
    sequence[0]["workflow_runs"].insert(2, {
        "id": 999, "display_title": "release-train 2026.1-rc.1 (dry_run=false)",
        "updated_at": (NOW - timedelta(hours=20)).isoformat().replace("+00:00", "Z"),
        "status": "completed", "conclusion": "failure",
    })
    _write(evidence / "train-sequence.json", sequence)
    decision, _ = readiness.evaluate(record, lock_path=lock, evidence_dir=evidence,
                                     lock_history=history, now=NOW)
    assert decision["checks"]["strict-trains"]["status"] == "fail"
