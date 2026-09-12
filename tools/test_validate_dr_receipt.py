import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from validate_dr_receipt import ReceiptError, SUBSTRATES, validate as validate_at_time

HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures" / "dr"
NOW = datetime(2026, 9, 4, 0, 6, tzinfo=timezone.utc)


def validate(candidate_path, receipt):
    return validate_at_time(candidate_path, receipt, now=NOW)


def complete():
    return json.loads((FIXTURES / "complete.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("fixture,code,message,stale", [
    ("missing-redis.json", 1, "missing enabled substrate(s): redis", False),
    ("numbers-without-restart.json", 1, "job-queue.restartRecovery", False),
    ("complete.json", 0, "DR receipt PASS", False),
    ("complete.json", 1, "stale DR evidence", True),
])
def test_receipt_cli_acceptance(tmp_path, fixture, code, message, stale):
    # Translate the entire fixture interval equally; keep every restart ordering
    # and observation intact while exercising the CLI's actual wall clock.
    raw = (FIXTURES / fixture).read_text(encoding="utf-8")
    shift = datetime.now(timezone.utc) - NOW - timedelta(days=2 if stale else 0)
    for value in set(re.findall(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", raw)):
        at = datetime.fromisoformat(value.replace("Z", "+00:00")) + shift
        raw = raw.replace(value, at.isoformat())
    receipt_path = tmp_path / fixture
    receipt_path.write_text(raw, encoding="utf-8")
    result = subprocess.run([
        sys.executable, str(HERE / "validate_dr_receipt.py"),
        "--candidate", str(FIXTURES / "candidate.json"),
        "--receipt", str(receipt_path),
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
    assert "candidate-input/platform-lock.json" in validate_step["run"]
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


@pytest.mark.parametrize("field", ["components", "sourceInputs"])
def test_frozen_lock_change_invalidates_receipt(tmp_path, field):
    from generate_platform_lock import generate

    matrix = tmp_path / "compatibility-matrix.yaml"
    matrix.write_text("contracts: {}\n", encoding="utf-8")
    lock = generate(FIXTURES / "candidate.json", matrix).lock
    lock["components"]["honua-server"]["artifacts"] = [
        {"kind": "image", "digest": "sha256:" + "b" * 64}]
    path = tmp_path / "platform-lock.json"
    path.write_text(json.dumps(lock), encoding="utf-8")
    receipt = complete()
    receipt["candidateLockDigest"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    assert validate(path, receipt) == sorted(SUBSTRATES)
    if field == "components":
        lock[field]["honua-server"]["artifacts"][0]["digest"] = "sha256:" + "e" * 64
    else:
        lock[field]["compatibilityMatrix"]["sha256"] = "sha256:" + "e" * 64
    path.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(ReceiptError, match="candidateLockDigest"):
        validate(path, receipt)


def test_missing_frozen_lock_fails_closed(tmp_path):
    with pytest.raises(OSError):
        validate(tmp_path / "platform-lock.json", complete())


@pytest.mark.parametrize("age,error", [
    (timedelta(minutes=5), None),
    (timedelta(hours=24), None),
    (timedelta(hours=24, microseconds=1), "stale"),
    (timedelta(days=90), "stale"),
    (timedelta(minutes=4, seconds=59), "future"),
    (timedelta(hours=-1), "future"),
])
def test_receipt_freshness_boundaries(age, error):
    receipt = complete()
    start = datetime.fromisoformat(receipt["startedAt"].replace("Z", "+00:00"))
    if error:
        with pytest.raises(ReceiptError, match=error):
            validate_at_time(FIXTURES / "candidate.json", receipt, now=start + age)
    else:
        assert validate_at_time(FIXTURES / "candidate.json", receipt, now=start + age) == sorted(SUBSTRATES)


def test_fresh_completion_cannot_launder_old_drill():
    receipt = complete()
    receipt["startedAt"] = "2026-09-01T00:00:00Z"
    with pytest.raises(ReceiptError, match="stale"):
        validate(FIXTURES / "candidate.json", receipt)


def test_reversed_drill_interval_is_rejected():
    receipt = complete()
    receipt["startedAt"] = "2026-09-04T00:05:01Z"
    with pytest.raises(ReceiptError, match="invalid drill interval"):
        validate(FIXTURES / "candidate.json", receipt)


def objectives():
    return json.loads((FIXTURES / "candidate.json").read_text(encoding="utf-8"))["disasterRecovery"]["objectives"]


def test_complete_fixture_meets_candidate_objectives():
    receipt = complete()
    limits = objectives()
    assert receipt["measurements"]["rpoMs"] <= limits["rpoMs"]
    assert receipt["measurements"]["rtoMs"] <= limits["rtoMs"]
    assert validate(FIXTURES / "candidate.json", receipt) == sorted(SUBSTRATES)


@pytest.mark.parametrize("value", [0, 1, 2000, 113999, 126001, 10 ** 12, 10.0 ** 12])
def test_reported_recovery_time_must_match_the_observed_window(value):
    # The fixture records a 120000 ms window (earliest stoppedAt to latest read-back);
    # the allowance is max(1000 ms, 5%) = 6000 ms, so 114000..126000 is the agreeing band.
    receipt = complete()
    receipt["measurements"]["rtoMs"] = value
    expected = "exceeds the candidate objective" if value > objectives()["rtoMs"] else "disagrees with the"
    with pytest.raises(ReceiptError, match=expected):
        validate(FIXTURES / "candidate.json", receipt)


@pytest.mark.parametrize("value", [114000, 120000, 126000, 120000.0])
def test_recovery_time_within_the_documented_tolerance_is_accepted(value):
    receipt = complete()
    receipt["measurements"]["rtoMs"] = value
    assert validate(FIXTURES / "candidate.json", receipt) == sorted(SUBSTRATES)


def test_recovery_time_tracks_the_receipt_timestamps(tmp_path):
    # Stretching the observed outage without restating the measurement must fail; restating
    # it agrees again. A producer cannot keep a flattering number over a longer outage.
    receipt = complete()
    for entry in receipt["substrates"].values():
        entry["restartRecovery"]["readAfterRestart"]["observedAt"] = "2026-09-04T00:04:30Z"
    with pytest.raises(ReceiptError, match="disagrees with the 150000 ms recovery window"):
        validate(FIXTURES / "candidate.json", receipt)
    receipt["measurements"]["rtoMs"] = 150000
    assert validate(FIXTURES / "candidate.json", receipt) == sorted(SUBSTRATES)


def test_recovery_window_spans_the_slowest_substrate():
    receipt = complete()
    receipt["substrates"]["redis"]["restartRecovery"]["readAfterRestart"]["observedAt"] = "2026-09-04T00:04:40Z"
    with pytest.raises(ReceiptError, match="disagrees with the 160000 ms recovery window"):
        validate(FIXTURES / "candidate.json", receipt)


@pytest.mark.parametrize("value,message", [
    (60001, "exceeds the candidate objective of 60000 ms"),
    (10 ** 12, "exceeds the candidate objective"),
])
def test_data_loss_beyond_the_candidate_objective_is_rejected(value, message):
    receipt = complete()
    receipt["measurements"]["rpoMs"] = value
    with pytest.raises(ReceiptError, match=re.escape(message)):
        validate(FIXTURES / "candidate.json", receipt)


@pytest.mark.parametrize("value", [0, 60000])
def test_zero_data_loss_remains_a_valid_recovery_point(value):
    # Zero RPO is zero data loss, not a missing measurement; only the limit bounds it.
    receipt = complete()
    receipt["measurements"]["rpoMs"] = value
    assert validate(FIXTURES / "candidate.json", receipt) == sorted(SUBSTRATES)


@pytest.mark.parametrize("value,message", [
    (None, "objectives: missing or invalid object"),
    ({}, "objectives.rpoMs"),
    ({"rpoMs": 60000}, "objectives.rtoMs"),
    ({"rtoMs": 300000}, "objectives.rpoMs"),
    ({"rpoMs": 60000, "rtoMs": 0}, "objectives.rtoMs"),
    ({"rpoMs": -1, "rtoMs": 300000}, "objectives.rpoMs"),
    ({"rpoMs": "60000", "rtoMs": 300000}, "objectives.rpoMs"),
    ({"rpoMs": True, "rtoMs": 300000}, "objectives.rpoMs"),
    ({"rpoMs": 60000, "rtoMs": float("inf")}, "objectives.rtoMs"),
])
def test_missing_or_invalid_objectives_fail_closed(tmp_path, value, message):
    deployment = config()
    if value is None:
        del deployment["objectives"]
    else:
        deployment["objectives"] = value
    receipt = complete()
    path = candidate_file(tmp_path, deployment, receipt)
    with pytest.raises(ReceiptError, match=re.escape(message)):
        validate(path, receipt)


def test_candidate_owns_the_objectives_not_the_receipt(tmp_path):
    # A tighter deployment objective must redden a receipt the looser fixture accepts.
    deployment = config()
    deployment["objectives"] = {"rpoMs": 60000, "rtoMs": 90000}
    receipt = complete()
    path = candidate_file(tmp_path, deployment, receipt)
    receipt["objectives"] = {"rpoMs": 10 ** 9, "rtoMs": 10 ** 9}
    with pytest.raises(ReceiptError, match="exceeds the candidate objective of 90000 ms"):
        validate(path, receipt)


def test_intake_pins_the_producer_identity_and_trusted_source_ref():
    import yaml

    root = HERE.parent
    gate = yaml.safe_load((root / ".github/workflows/gate-dr.yml").read_text(encoding="utf-8"))
    steps = gate["jobs"]["receipt"]["steps"]
    verify = next(step for step in steps if "gh attestation verify" in step.get("run", ""))
    command = " ".join(verify["run"].replace("`\n", " ").split())
    assert "--repo honua-io/honua-release" in command
    # A repository-only check accepts anything any workflow in this repository attested,
    # including one a pull request controls.
    assert "--signer-workflow honua-io/honua-release/.github/workflows/dr-drill-local-docker.yml" in command
    assert "--source-ref refs/heads/trunk" in command
    assert not verify.get("continue-on-error")
    assert gate["permissions"]["attestations"] == "read"


def test_pull_request_producer_cannot_mint_an_accepted_attestation():
    import yaml

    producer = yaml.safe_load((HERE.parent / ".github/workflows/dr-drill-local-docker.yml").read_text(encoding="utf-8"))
    # EVERY job that executes pull-request-controlled drill code holds no signing authority; adding
    # a second drill job must not open a second path to the producer identity.
    drill_jobs = ["full-platform", "restore"]
    for name in drill_jobs:
        drill = producer["jobs"][name]
        drill_text = str(drill)
        assert "attest" not in drill_text and "id-token" not in drill_text
        assert drill["permissions"] == {"contents": "read", "packages": "read"}
    assert producer["permissions"] == {"contents": "read", "packages": "read"}
    attest = producer["jobs"]["attest"]
    assert attest["permissions"]["attestations"] == "write"
    assert attest["permissions"]["id-token"] == "write"
    assert attest["if"] == "github.event_name != 'pull_request'"
    assert sorted(attest["needs"]) == sorted(drill_jobs)
    # Publication of a receipt is likewise off-limits to a pull request run.
    publish = producer["jobs"]["publish"]
    assert publish["if"] == "github.event_name != 'pull_request'"
    assert publish["needs"] == "attest"
