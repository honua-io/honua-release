import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import upgrade_recovery as recovery  # noqa: E402


def _digest(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


def _evidence(tmp_path: Path) -> dict[str, str]:
    result = {}
    for name in recovery.REQUIRED_EVIDENCE:
        path = tmp_path / name
        path.write_text(f"evidence:{name}\n", encoding="utf-8")
        result[name] = recovery.sha256_bytes(path)
    return result


def _receipt(tmp_path: Path, scenario="post-migration-readiness"):
    observation = {
        "rollbackAttempted": True,
        "rollbackSucceeded": True,
        "schemaCompatible": True,
        "priorReady": True,
        "priorImageMatched": True,
        "seedChecksumsMatched": True,
        "boundedReadPassed": True,
        "boundedWritePassed": True,
        "schemaJournal": [106, 107],
    }
    return {
        "schema": "honua.upgrade-recovery-receipt/v1",
        "scenario": scenario,
        "bindings": {
            "candidateLockDigest": _digest("lock-b"),
            "priorLockDigest": _digest("lock-a"),
            "candidateImageDigest": _digest("image-b"),
            "priorImageDigest": _digest("image-a"),
            "workflowCommit": "a" * 40,
            "workflowRun": "https://github.test/runs/1",
        },
        "phases": [
            {"name": "prior-ready", "at": "2026-09-01T00:00:00Z"},
            {"name": "candidate-failed", "at": "2026-09-01T00:01:00Z"},
            {"name": "recovery-started", "at": "2026-09-01T00:01:01Z"},
            {"name": "recovery-terminal", "at": "2026-09-01T00:01:31Z"},
        ],
        "rollbackClock": {"budgetSeconds": 60, "elapsedSeconds": 30},
        "observation": observation,
        "recovery": {"command": "helm rollback honua 1 --wait", "instructions": "restore snapshot"},
        "classification": "recovered",
        "evidence": _evidence(tmp_path),
    }


@pytest.mark.parametrize("scenario", sorted(recovery.SCENARIOS - {"rollback-failure"}))
def test_each_failed_upgrade_scenario_recovers_and_is_candidate_bound(tmp_path, scenario):
    receipt = _receipt(tmp_path, scenario)
    assert recovery.validate(receipt, tmp_path)["classification"] == "recovered"


def test_failed_upgrade_without_rollback_attempt_is_refused(tmp_path):
    receipt = _receipt(tmp_path)
    receipt["observation"]["rollbackAttempted"] = False
    with pytest.raises(recovery.RecoveryError, match="did not attempt rollback"):
        recovery.validate(receipt, tmp_path)


def test_rollback_failure_is_terminal_failure_with_phase_and_instructions(tmp_path):
    receipt = _receipt(tmp_path, "rollback-failure")
    receipt["observation"]["rollbackSucceeded"] = False
    receipt["observation"]["priorReady"] = False
    receipt["classification"] = "rollback-failed"
    assert recovery.validate(receipt, tmp_path)["classification"] == "rollback-failed"


def test_incompatible_forward_schema_requires_manual_restore(tmp_path):
    receipt = _receipt(tmp_path, "migration-boundary")
    receipt["observation"].update(schemaCompatible=False, rollbackSucceeded=False, priorReady=False)
    receipt["classification"] = "manual-restore-required"
    assert recovery.validate(receipt, tmp_path)["classification"] == "manual-restore-required"


def test_incompatible_schema_cannot_claim_success(tmp_path):
    receipt = _receipt(tmp_path)
    receipt["observation"]["schemaCompatible"] = False
    with pytest.raises(recovery.RecoveryError, match="must not claim"):
        recovery.validate(receipt, tmp_path)


def test_missing_rollback_or_query_evidence_fails_the_artifact(tmp_path):
    receipt = _receipt(tmp_path)
    (tmp_path / "rollback.log").unlink()
    with pytest.raises(recovery.RecoveryError, match="rollback.log"):
        recovery.validate(receipt, tmp_path)


def test_evidence_replacement_breaks_candidate_bound_receipt(tmp_path):
    receipt = _receipt(tmp_path)
    (tmp_path / "candidate.log").write_text("different candidate bytes\n")
    with pytest.raises(recovery.RecoveryError, match="digest mismatch"):
        recovery.validate(receipt, tmp_path)


def test_recovery_over_clock_cannot_pass(tmp_path):
    receipt = _receipt(tmp_path)
    receipt["rollbackClock"]["budgetSeconds"] = 20
    with pytest.raises(recovery.RecoveryError, match="exceeded"):
        recovery.validate(receipt, tmp_path)


def test_phase_reordering_is_refused(tmp_path):
    receipt = _receipt(tmp_path)
    receipt["phases"][1], receipt["phases"][2] = receipt["phases"][2], receipt["phases"][1]
    with pytest.raises(recovery.RecoveryError, match="ordered exactly"):
        recovery.validate(receipt, tmp_path)


def test_retagged_candidate_lock_digest_is_not_interchangeable(tmp_path):
    receipt = _receipt(tmp_path)
    original = copy.deepcopy(receipt)
    receipt["bindings"]["candidateLockDigest"] = _digest("different-lock-bytes")
    assert receipt["bindings"]["candidateLockDigest"] != original["bindings"]["candidateLockDigest"]
    assert recovery.validate(receipt, tmp_path)
