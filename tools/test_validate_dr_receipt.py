import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from validate_dr_receipt import ReceiptError, SUBSTRATES, validate

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures" / "dr"


def complete():
    return json.loads((FIXTURES / "complete.json").read_text())


@pytest.mark.parametrize("fixture,code,message", [
    ("missing-redis.json", 1, "missing enabled substrate(s): redis"),
    ("numbers-without-restart.json", 1, "job-queue.restartRecovery"),
    ("complete.json", 0, "DR receipt PASS"),
])
def test_receipt_cli_acceptance(fixture, code, message):
    result = subprocess.run([
        sys.executable, str(HERE / "validate_dr_receipt.py"),
        "--candidate", str(FIXTURES / "candidate.json"),
        "--receipt", str(FIXTURES / fixture),
    ], capture_output=True, text=True)
    assert result.returncode == code, result.stdout + result.stderr
    assert message in result.stdout + result.stderr


@pytest.mark.parametrize("name", SUBSTRATES)
def test_each_enabled_substrate_cannot_be_omitted(name):
    receipt = complete()
    del receipt["substrates"][name]
    # A receipt-provided denominator cannot shrink the candidate-owned set.
    receipt["requiredSubstrates"] = list(receipt["substrates"])
    with pytest.raises(ReceiptError, match=f"missing enabled substrate.*{name}"):
        validate(FIXTURES / "candidate.json", receipt)


@pytest.mark.parametrize("name", SUBSTRATES)
@pytest.mark.parametrize("field", ["restartRecovery", "writtenBeforeRestart", "readAfterRestart"])
def test_every_substrate_requires_both_restart_observations(name, field):
    receipt = complete()
    entry = receipt["substrates"][name]
    del (entry if field == "restartRecovery" else entry["restartRecovery"])[field]
    with pytest.raises(ReceiptError, match=f"{name}.*{field}"):
        validate(FIXTURES / "candidate.json", receipt)


@pytest.mark.parametrize("field,value,message", [
    ("instanceAfter", "postgresql-old", "instance identity"),
    ("readyAt", "2026-09-04T00:04:01Z", "bracket restart"),
    ("stoppedAt", "2026-09-04T00:00:01Z", "bracket restart"),
    ("readyAt", "2026-09-04T00:03:00", "timezone-aware"),
    ("readAfterRestart.sha256", "sha256:" + "d" * 64, "differs"),
    ("readAfterRestart.stateId", "different-state", "differs"),
    ("readAfterRestart.count", 2, "differs"),
    ("readAfterRestart.runtimeSurface", "", "runtimeSurface"),
    ("writtenBeforeRestart.count", 0, "nonempty observed state"),
    ("writtenBeforeRestart.sha256", "", "SHA-256"),
])
def test_rejects_invalid_restart_proof(field, value, message):
    receipt = complete()
    target = receipt["substrates"]["postgresql"]["restartRecovery"]
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    with pytest.raises(ReceiptError, match=message):
        validate(FIXTURES / "candidate.json", receipt)


def candidate_file(tmp_path, config, receipt):
    candidate = json.loads((FIXTURES / "candidate.json").read_text())
    candidate["disasterRecovery"] = config
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(candidate), encoding="utf-8")
    receipt["candidateLockDigest"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return path


def config():
    return json.loads((FIXTURES / "candidate.json").read_text())["disasterRecovery"]


@pytest.mark.parametrize("value", [None, {}, {"topology": "fixture-all-stores"}])
def test_missing_candidate_configuration_fails_closed(tmp_path, value):
    receipt = complete()
    path = candidate_file(tmp_path, value, receipt)
    with pytest.raises(ReceiptError, match="candidate.disasterRecovery"):
        validate(path, receipt)


def test_omitted_enablement_is_not_disabled(tmp_path):
    deployment = config()
    del deployment["substrates"]["redis"]
    receipt = complete()
    path = candidate_file(tmp_path, deployment, receipt)
    with pytest.raises(ReceiptError, match="redis: missing enablement"):
        validate(path, receipt)


@pytest.mark.parametrize("value", ["false", 0, None])
def test_enablement_requires_boolean(tmp_path, value):
    deployment = config()
    deployment["substrates"]["redis"] = value
    receipt = complete()
    path = candidate_file(tmp_path, deployment, receipt)
    with pytest.raises(ReceiptError, match="redis: enablement must be boolean"):
        validate(path, receipt)


def test_disabled_substrates_and_preview_journeys_are_not_required(tmp_path):
    deployment = config()
    deployment["substrates"] = {name: name == "postgresql" for name in SUBSTRATES}
    receipt = complete()
    receipt["substrates"] = {"postgresql": receipt["substrates"]["postgresql"]}
    path = candidate_file(tmp_path, deployment, receipt)
    assert validate(path, receipt) == ["postgresql"]


def test_additional_configured_store_is_required(tmp_path):
    deployment = config()
    deployment["substrates"]["additional-store"] = True
    receipt = complete()
    path = candidate_file(tmp_path, deployment, receipt)
    with pytest.raises(ReceiptError, match="missing enabled substrate.*additional-store"):
        validate(path, receipt)


@pytest.mark.parametrize("field,value,message", [
    ("candidateLockDigest", "sha256:" + "f" * 64, "candidateLockDigest"),
    ("topology", "another-topology", "topology"),
    ("schema", "honua.postgresql-restore-receipt/v1", "not full-platform DR"),
    ("scope", "postgresql-restore", "scope=full-platform"),
    ("status", "skipped", "status=pass"),
])
def test_candidate_binding_and_scope(field, value, message):
    receipt = complete()
    receipt[field] = value
    with pytest.raises(ReceiptError, match=message):
        validate(FIXTURES / "candidate.json", receipt)


def test_configuration_change_invalidates_old_receipt(tmp_path):
    path = tmp_path / "candidate.json"
    path.write_bytes((FIXTURES / "candidate.json").read_bytes() + b"\n")
    with pytest.raises(ReceiptError, match="candidateLockDigest"):
        validate(path, complete())


@pytest.mark.parametrize("field", ["primaryStateDestroyed", "restoredIntoCleanStore"])
def test_requires_destructive_restore(field):
    receipt = complete()
    receipt["substrates"]["redis"]["backup"][field] = False
    with pytest.raises(ReceiptError, match="redis.backup.*destroyed primary state"):
        validate(FIXTURES / "candidate.json", receipt)


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), None])
def test_measurements_cannot_lie(value):
    receipt = complete()
    receipt["measurements"]["rtoMs"] = value
    with pytest.raises(ReceiptError, match="rtoMs"):
        validate(FIXTURES / "candidate.json", receipt)
