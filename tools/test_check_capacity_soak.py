import copy
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import check_capacity_soak as gate


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "certification" / "capacity-envelope.v1.json"
LOCK = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
REVISION = "a" * 40


def receipt():
    values = {name: threshold["value"] for name, threshold in LOCK["thresholds"].items()}
    after_freeze = datetime.fromisoformat(LOCK["frozenAt"].replace("Z", "+00:00")) + timedelta(seconds=1)
    return {
        "status": "completed", "candidateRevision": REVISION, "observedRevision": REVISION,
        "lockSha256": hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest(),
        "startedAt": after_freeze.isoformat(), "profile": "soak", "steadyStateSeconds": 3600,
        "envelope": copy.deepcopy(LOCK["supportedEnvelope"]), "signingIdentity": "github-actions",
        "signature": "opaque-sigstore-bundle", "signals": {
            name: {"status": "observed", "revision": REVISION, "value": value}
            for name, value in values.items()
        },
    }


def failures(value):
    return gate.evaluate(LOCK, value, gate.lock_digest(LOCK_PATH), REVISION)


def test_complete_candidate_bound_receipt_passes():
    assert failures(receipt()) == []


def test_skipped_signal_fails():
    value = receipt(); value["signals"]["p99LatencyMs"]["status"] = "skipped"
    assert any("p99LatencyMs" in failure for failure in failures(value))


def test_revision_mismatch_fails():
    value = receipt(); value["signals"]["queueAgeSeconds"]["revision"] = "b" * 40
    assert any("revision mismatch" in failure for failure in failures(value))


def test_receipt_cannot_select_an_unrelated_candidate_revision():
    value = receipt()
    value["candidateRevision"] = value["observedRevision"] = "b" * 40
    for signal in value["signals"].values():
        signal["revision"] = "b" * 40
    assert any("manifest-pinned" in failure for failure in failures(value))


def test_missing_candidate_revision_fails():
    value = receipt()
    value.pop("candidateRevision")
    value.pop("observedRevision")
    assert any("manifest-pinned" in failure for failure in failures(value))


def test_threshold_cannot_be_selected_after_soak_starts():
    value = receipt(); value["startedAt"] = LOCK["frozenAt"]
    assert any("after the threshold freeze" in failure for failure in failures(value))


def test_regression_beyond_frozen_allowance_fails():
    value = receipt(); value["signals"]["throughputRps"]["value"] = LOCK["thresholds"]["throughputRps"]["value"] - 0.01
    assert any("throughputRps" in failure for failure in failures(value))


def test_lock_or_envelope_drift_fails():
    value = receipt(); value["lockSha256"] = "0" * 64; value["envelope"]["tenants"] = 2
    result = failures(value)
    assert any("exact committed" in failure for failure in result)
    assert any("capacity envelope" in failure for failure in result)


def test_unsigned_receipt_fails():
    value = receipt(); value["signature"] = ""
    assert any("signature" in failure for failure in failures(value))


def test_preview_dimensions_are_informational():
    value = receipt()
    for name in ("activeSubscriptions", "alertEvaluationsPerSecond"):
        value["envelope"][name] = 0
        value["signals"][name] = {"status": "unobserved", "value": None}
    assert failures(value) == []
    assert set(gate.informational_dimensions(LOCK, value)) == {"activeSubscriptions", "alertEvaluationsPerSecond"}


def test_undeclared_non_preview_dimension_fails():
    value = receipt()
    value["envelope"]["serverReplicas"] = 3
    assert any("neither declares nor excludes: serverReplicas" in failure for failure in failures(value))


def test_preview_dimension_is_undeclared_without_the_lock_ruling():
    lock = copy.deepcopy(LOCK)
    lock.pop("rulings")
    value = receipt()
    value["envelope"]["activeSubscriptions"] = 0
    result = gate.evaluate(lock, value, gate.lock_digest(LOCK_PATH), REVISION)
    assert any("neither declares nor excludes: activeSubscriptions" in failure for failure in result)
    assert gate.informational_dimensions(lock, value) == {}


def test_missing_ga_dimension_fails():
    value = receipt()
    del value["envelope"]["featuresPerLayer"]
    assert any("capacity envelope" in failure for failure in failures(value))


def test_present_preview_dimension_is_still_required_by_an_older_lock():
    lock = copy.deepcopy(LOCK)
    lock["supportedEnvelope"]["activeSubscriptions"] = 1000
    assert any("capacity envelope" in failure for failure in gate.evaluate(lock, receipt(), gate.lock_digest(LOCK_PATH), REVISION))


def test_allowance_is_not_applied_twice():
    value = receipt()
    value["signals"]["p95LatencyMs"]["value"] = LOCK["thresholds"]["p95LatencyMs"]["value"] + 0.01
    assert any("p95LatencyMs" in failure for failure in failures(value))


@pytest.mark.parametrize("name", LOCK["soak"]["requiredSignals"])
def test_each_ga_signal_fails_beyond_its_frozen_limit(name):
    value = receipt()
    threshold = LOCK["thresholds"][name]
    value["signals"][name]["value"] = threshold["value"] + (0.01 if threshold["operator"] == "<=" else -0.01)
    assert any(name in failure for failure in failures(value))


def test_cli_echoes_preview_as_informational(tmp_path, monkeypatch, capsys):
    value = receipt()
    value["envelope"]["activeSubscriptions"] = 0
    value["signals"]["alertEvaluationsPerSecond"] = {"status": "skipped"}
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(value))
    monkeypatch.setattr("sys.argv", ["check_capacity_soak.py", "--lock", str(LOCK_PATH),
                                   "--receipt", str(path), "--expected-revision", REVISION])
    assert gate.main() == 0
    report = capsys.readouterr().out
    assert "Preview informational (not gated): activeSubscriptions" in report
    assert "Preview informational (not gated): alertEvaluationsPerSecond" in report
