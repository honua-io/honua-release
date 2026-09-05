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
    return json.loads((FIXTURES / "complete.json").read_text(encoding="utf-8"))


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
    candidate = json.loads((FIXTURES / "candidate.json").read_text(encoding="utf-8"))
    candidate["disasterRecovery"] = config
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(candidate), encoding="utf-8")
    receipt["candidateLockDigest"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return path


def config():
    return json.loads((FIXTURES / "candidate.json").read_text(encoding="utf-8"))["disasterRecovery"]


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


def test_full_platform_gate_is_required_for_promotion():
    from candidate_binding import REQUIRED_RELEASE_GATES, validate_live_report
    from datetime import datetime, timezone

    assert "dr" in REQUIRED_RELEASE_GATES
    report = {
        "dry_run": False, "overallStatus": "pass",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "gates": [{"gate": name, "status": "pass"}
                  for name in sorted(REQUIRED_RELEASE_GATES - {"dr"})],
    }
    ok, reason = validate_live_report(report)
    assert not ok and "dr" in reason


def test_release_and_scheduled_workflows_enforce_validator():
    import yaml

    root = HERE.parent
    gate = yaml.safe_load((root / ".github/workflows/gate-dr.yml").read_text(encoding="utf-8"))
    # PyYAML's YAML 1.1 loader maps the Actions 'on' key to True.
    assert "schedule" in gate[True]
    steps = gate["jobs"]["receipt"]["steps"]
    validate_step = next(step for step in steps if "tools/validate_dr_receipt.py" in step.get("run", ""))
    assert "--candidate $candidatePath" in validate_step["run"]
    assert "candidate-input/platform-manifest.yaml" in validate_step["run"]
    assert not validate_step.get("continue-on-error")
    train = yaml.safe_load((root / ".github/workflows/release-train.yml").read_text(encoding="utf-8"))
    assert train["jobs"]["gate_dr"]["with"]["candidate_bundle"] is True
    assert "gate_dr" in train["jobs"]["report"]["needs"]
    assert "dr|$S_DR" in (root / ".github/workflows/release-train.yml").read_text(encoding="utf-8")


def test_legacy_producer_cannot_claim_full_platform_dr():
    script = (HERE.parent / "e2e/dr-drill/run.sh").read_text(encoding="utf-8")
    assert "'schema':'honua.postgresql-restore-receipt/v1'" in script
    assert "'scope':'postgresql-restore'" in script
    assert "'schema':'honua.dr-drill-receipt/" not in script
