"""Tests for the advertised-vs-actual docs gate.

The point of the gate is to FAIL on a fabricated capability (advertised `shipped` with no real
backing). Proven here, plus that the committed docs/capabilities.yaml is itself all-backed against
the REAL canonical checks + wired gates (so the platform's own claims are honest).

Run: python -m pytest tools/test_check_capabilities.py    (or: python tools/test_check_capabilities.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_capabilities as cap  # noqa: E402

CHECKS = {"health", "geoservices-error", "service-catalog"}
GATES = {"conformance", "build-test", "artifact-consume", "cloud-parity"}


def test_roadmap_passes_without_evidence():
    rows, overall = cap.check([{"id": "future", "status": "roadmap"}], CHECKS, GATES)
    assert overall == "pass" and rows[0]["status"] == "pass"


def test_shipped_with_resolving_gate_passes():
    rows, overall = cap.check(
        [{"id": "x", "status": "shipped", "evidence": {"kind": "gate", "ref": "build-test"}}], CHECKS, GATES)
    assert overall == "pass"


def test_shipped_with_resolving_canonical_check_passes():
    rows, overall = cap.check(
        [{"id": "x", "status": "shipped", "evidence": {"kind": "canonical-check", "ref": "health"}}], CHECKS, GATES)
    assert overall == "pass"


def test_fabricated_shipped_claim_fails():
    # The audit's exact failure mode: advertised shipped, evidence points at nothing real.
    rows, overall = cap.check(
        [{"id": "ar-utility-viz", "status": "shipped",
          "evidence": {"kind": "gate", "ref": "totally-made-up"}}], CHECKS, GATES)
    assert overall == "fail" and "no actual evidence" in rows[0]["why"]


def test_shipped_with_no_evidence_fails():
    rows, overall = cap.check([{"id": "x", "status": "shipped"}], CHECKS, GATES)
    assert overall == "fail"


def test_unknown_status_fails():
    rows, overall = cap.check([{"id": "x", "status": "kinda-shipped"}], CHECKS, GATES)
    assert overall == "fail" and "unknown capability status" in rows[0]["why"]


def test_test_kind_resolves_to_a_real_file():
    ok, _ = cap._resolve({"kind": "test", "ref": "tools/test_generate_bom.py"}, CHECKS, GATES)
    assert ok
    bad, _ = cap._resolve({"kind": "test", "ref": "tools/nope_missing.py"}, CHECKS, GATES)
    assert not bad


# ---- the committed capabilities file must itself be fully backed (real checks + real gates) --------
def test_committed_capabilities_are_all_backed():
    import yaml
    data = yaml.safe_load((Path(__file__).resolve().parents[1] / "docs" / "capabilities.yaml").read_text(encoding="utf-8"))
    rows, overall = cap.check(data["capabilities"], cap.known_canonical_checks(), cap.known_gates())
    unbacked = [r for r in rows if r["status"] == "fail"]
    assert overall == "pass", f"committed capabilities have unbacked claims: {unbacked}"


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{'OK' if not failures else 'FAILED'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
