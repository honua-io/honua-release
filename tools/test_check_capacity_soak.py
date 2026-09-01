import copy
import hashlib
import json
from pathlib import Path

import check_capacity_soak as gate


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "certification" / "capacity-envelope.v1.json"
LOCK = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
REVISION = "a" * 40


def receipt():
    values = {
        "availability": 1.0, "errorRate": 0.0, "p95LatencyMs": 600,
        "p99LatencyMs": 630, "throughputRps": 1800, "queueAgeSeconds": 5,
        "saturationRatio": 0.7, "recoveryTimeSeconds": 120,
    }
    return {
        "status": "completed", "candidateRevision": REVISION, "observedRevision": REVISION,
        "lockSha256": hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest(),
        "startedAt": "2026-09-01T10:06:00Z", "profile": "soak", "steadyStateSeconds": 3600,
        "envelope": copy.deepcopy(LOCK["supportedEnvelope"]), "signingIdentity": "github-actions",
        "signature": "opaque-sigstore-bundle", "signals": {
            name: {"status": "observed", "revision": REVISION, "value": value}
            for name, value in values.items()
        },
    }


def failures(value):
    return gate.evaluate(LOCK, value, gate.lock_digest(LOCK_PATH))


def test_complete_candidate_bound_receipt_passes():
    assert failures(receipt()) == []


def test_skipped_signal_fails():
    value = receipt(); value["signals"]["p99LatencyMs"]["status"] = "skipped"
    assert any("p99LatencyMs" in failure for failure in failures(value))


def test_revision_mismatch_fails():
    value = receipt(); value["signals"]["queueAgeSeconds"]["revision"] = "b" * 40
    assert any("revision mismatch" in failure for failure in failures(value))


def test_threshold_cannot_be_selected_after_soak_starts():
    value = receipt(); value["startedAt"] = LOCK["frozenAt"]
    assert any("after the threshold freeze" in failure for failure in failures(value))


def test_regression_beyond_frozen_allowance_fails():
    value = receipt(); value["signals"]["throughputRps"]["value"] = 1789.99
    assert any("throughputRps" in failure for failure in failures(value))


def test_lock_or_envelope_drift_fails():
    value = receipt(); value["lockSha256"] = "0" * 64; value["envelope"]["tenants"] = 2
    result = failures(value)
    assert any("exact committed" in failure for failure in result)
    assert any("capacity envelope" in failure for failure in result)


def test_unsigned_receipt_fails():
    value = receipt(); value["signature"] = ""
    assert any("signature" in failure for failure in failures(value))
