"""Tests for the advertised-vs-actual docs gate.

The point of the gate is to FAIL on a fabricated capability (advertised `shipped` with no real
backing). Proven here, plus that the committed docs/capabilities.yaml is itself all-backed against
the REAL canonical checks + wired gates (so the platform's own claims are honest).

Also covers the `capability-key` evidence kind (honua-release#59): a claim resolves against
honua-evidence's capability-matrix.v1.json GA criteria (implemented, provingTestCount floor, 100%
CITE) and fails CLOSED to `blocked` — never a pass — when the matrix itself is unavailable.

Run: python -m pytest tools/test_check_capabilities.py    (or: python tools/test_check_capabilities.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_capabilities as cap  # noqa: E402

CHECKS = {"health", "geoservices-error", "service-catalog"}
GATES = {"conformance", "build-test", "artifact-consume", "cloud-parity", "docs", "evidence"}


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
    assert ok == "pass"
    bad, _ = cap._resolve({"kind": "test", "ref": "tools/nope_missing.py"}, CHECKS, GATES)
    assert bad == "fail"


# ---- capability-key evidence kind (honua-release#59) ------------------------------------------------
FAKE_MATRIX = {
    "capabilities": [
        {"key": "serve.wfs", "maturity": {"implemented": 2}, "provingTestCount": 109, "noSurface": None,
         "cite": [{"suite": "WFS 1.0", "passRate": 100.0}, {"suite": "WFS 2.0", "passRate": 100.0}]},
        {"key": "serve.thin-surface", "maturity": {"implemented": 1}, "provingTestCount": 2,
         "noSurface": None, "cite": []},
        {"key": "editing.branch-versioning", "maturity": {"experimental": 15}, "provingTestCount": 27,
         "noSurface": None, "cite": []},
        {"key": "serve.bad-cite", "maturity": {"implemented": 3}, "provingTestCount": 50, "noSurface": None,
         "cite": [{"suite": "WFS 2.0", "passRate": 92.0}]},
    ]
}


def _claim(ref: str) -> list[dict]:
    return [{"id": "x", "status": "shipped", "evidence": {"kind": "capability-key", "ref": ref}}]


def test_capability_key_passing_key_passes():
    rows, overall = cap.check(_claim("serve.wfs"), CHECKS, GATES, capability_matrix=FAKE_MATRIX, min_proving_tests=5)
    assert overall == "pass"


def test_capability_key_under_floor_fails():
    rows, overall = cap.check(_claim("serve.thin-surface"), CHECKS, GATES, capability_matrix=FAKE_MATRIX, min_proving_tests=5)
    assert overall == "fail" and "below floor" in rows[0]["why"]


def test_capability_key_experimental_only_fails():
    rows, overall = cap.check(_claim("editing.branch-versioning"), CHECKS, GATES, capability_matrix=FAKE_MATRIX, min_proving_tests=5)
    assert overall == "fail" and "experimental" in rows[0]["why"]


def test_capability_key_missing_key_fails():
    rows, overall = cap.check(_claim("serve.does-not-exist"), CHECKS, GATES, capability_matrix=FAKE_MATRIX, min_proving_tests=5)
    assert overall == "fail" and "not found" in rows[0]["why"]


def test_capability_key_missing_matrix_is_blocked_never_pass():
    rows, overall = cap.check(_claim("serve.wfs"), CHECKS, GATES, capability_matrix=None)
    assert overall == "blocked" and rows[0]["status"] == "blocked"
    assert "unavailable" in rows[0]["why"]


def test_capability_key_cite_below_100_fails():
    rows, overall = cap.check(_claim("serve.bad-cite"), CHECKS, GATES, capability_matrix=FAKE_MATRIX, min_proving_tests=5)
    assert overall == "fail" and "CITE" in rows[0]["why"]


def test_advertised_ga_subset_evidenced_ga_violation_case():
    # The exact ⊆-violation shape: a claim resolves (matrix present) but the key itself doesn't meet
    # the GA bar — this is the "deliberately under-evidenced GA key fails the gate" demonstration
    # (honua-release#59 acceptance criteria), expressed at the single-claim (capability-key) level.
    rows, overall = cap.check(_claim("serve.thin-surface"), CHECKS, GATES, capability_matrix=FAKE_MATRIX, min_proving_tests=5)
    assert overall == "fail"


def test_resolve_capability_key_blocked_only_for_missing_matrix():
    status, _ = cap.resolve_capability_key("serve.wfs", None)
    assert status == "blocked"


def test_load_capability_matrix_missing_path_returns_none():
    assert cap.load_capability_matrix(None) is None
    assert cap.load_capability_matrix("tools/does_not_exist.json") is None


def test_load_capability_matrix_malformed_json_returns_none(tmp_path):
    bad = tmp_path / "matrix.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cap.load_capability_matrix(str(bad)) is None


def test_load_capability_matrix_valid_file_round_trips(tmp_path):
    good = tmp_path / "matrix.json"
    import json
    good.write_text(json.dumps(FAKE_MATRIX), encoding="utf-8")
    loaded = cap.load_capability_matrix(str(good))
    assert loaded == FAKE_MATRIX


# ---- the committed capabilities file must itself be fully backed (real checks + real gates) --------
#
# capability-key claims need a real capability matrix to resolve against, but this self-test must stay
# hermetic/offline (no network fetch of honua-evidence). This fixture is a point-in-time snapshot of
# the real fields (as of the 2026-07-17 matrix generatedAt) for exactly the keys docs/capabilities.yaml
# claims below — it proves the WIRING is correct. The LIVE gate (gate-docs.yml) fetches the actual
# current matrix and re-validates every one of these claims against reality on every PR/train run, so
# a real regression (e.g. a proving-test count dropping) is still caught — just not by this unit test.
_COMMITTED_MATRIX_FIXTURE = {
    "capabilities": [
        {"key": "serve.wfs", "maturity": {"implemented": 2}, "provingTestCount": 109, "noSurface": None,
         "cite": [{"suite": "WFS 1.0", "passRate": 100.0}, {"suite": "WFS 1.1", "passRate": 100.0},
                   {"suite": "WFS 2.0", "passRate": 100.0}]},
        {"key": "serve.ogc-api-features", "maturity": {"implemented": 21}, "provingTestCount": 242,
         "noSurface": None, "cite": [{"suite": "OGC API Features 1.0", "passRate": 100.0}]},
        {"key": "serve.vector-tiles", "maturity": {"implemented": 7}, "provingTestCount": 35,
         "noSurface": None, "cite": []},
        {"key": "serve.geoservices-featureserver", "maturity": {"implemented": 49}, "provingTestCount": 415,
         "noSurface": None, "cite": []},
    ]
}


def test_committed_capabilities_are_all_backed():
    data = yaml.safe_load((Path(__file__).resolve().parents[1] / "docs" / "capabilities.yaml").read_text(encoding="utf-8"))
    min_proving = (data.get("defaults") or {}).get("minProvingTests", cap.DEFAULT_MIN_PROVING_TESTS)
    rows, overall = cap.check(data["capabilities"], cap.known_canonical_checks(), cap.known_gates(),
                               capability_matrix=_COMMITTED_MATRIX_FIXTURE, min_proving_tests=min_proving)
    unbacked = [r for r in rows if r["status"] != "pass"]
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
