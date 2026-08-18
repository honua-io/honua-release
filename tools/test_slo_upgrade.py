"""Tests for the SLO and upgrade gate verdict logic.

Both gates have real, decidable cores even while their deploy/migration halves stay BLOCKED — and
both must be able to FAIL: an over-budget error rate, and an upgrade that strands an old client or
runs DB migrations backwards.

Run: python -m pytest tools/test_slo_upgrade.py    (or: python tools/test_slo_upgrade.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
import check_slo as slo  # noqa: E402
import check_upgrade as up  # noqa: E402
from runner import harness  # noqa: E402


# ---- SLO -----------------------------------------------------------------------------------------
def test_slo_within_budget_passes():
    assert slo.evaluate_slo(5, 10000, 0.01)[0] == "pass"


def test_slo_over_budget_fails():
    status, why = slo.evaluate_slo(500, 10000, 0.01)   # 5% > 1%
    assert status == "fail" and "exceeds budget" in why


def test_slo_absent_denominator_is_blocked_not_pass():
    # No denominator => no candidate / nothing scrapeable / no in-scope traffic => never a green.
    assert slo.evaluate_slo(None, None, 0.01)[0] == "blocked"
    assert slo.evaluate_slo(5, None, 0.01)[0] == "blocked"
    assert slo.evaluate_slo(0, 0, 0.01)[0] == "blocked"      # no traffic


def test_slo_absent_error_counter_with_live_traffic_is_zero_errors_not_blocked():
    """A clean candidate exports no error counter at all — OTel omits a counter until its first
    measurement. Blocking on that made the gate unable to pass a healthy release, and made strict
    enforcement fail it outright."""
    status, why = slo.evaluate_slo(None, 10000, 0.01)
    assert status == "pass", why
    assert "zero errors" in why


def test_slo_absent_error_counter_still_blocks_without_a_denominator():
    # Absent numerator only means zero once the denominator proves the candidate is serving.
    assert slo.evaluate_slo(None, 0, 0.01)[0] == "blocked"


# ---- end-to-end: scrape + verdict, the fail-open regression ---------------------------------------
# A candidate up for 24h with liveness+readiness probes every 10s and a 15s Prometheus scrape, plus a
# 200-call GeoServices smoke suite in which 20 calls returned an error envelope: a real 10% error
# rate on the surface this gate exists to protect.
_CANDIDATE_EXPOSITION = (
    'honua_serving_request_duration_ms_count{honua_protocol="Health"} 17280 1786929468470\n'
    'honua_serving_request_duration_ms_count{honua_protocol="Monitoring"} 5760 1786929468470\n'
    'honua_serving_request_duration_ms_count{honua_protocol="FeatureServer"} 180 1786929468470\n'
    'honua_serving_request_duration_ms_count{honua_protocol="MapServer"} 20 1786929468470\n'
    'honua_geoservices_error_total{service_type="FeatureServer"} 20 1786929468470\n'
)
_GEOSERVICES_SELECTOR = (
    'honua_protocol=~"FeatureServer|MapServer|ImageServer|VectorTileServer|GPServer|NAServer|'
    'GeometryService|PrintingTools|StaticMap"'
)


def _slo_verdict(selector: str | None) -> str:
    errors = harness.parse_metric_total(_CANDIDATE_EXPOSITION, "honua_geoservices_error_total")
    requests = harness.parse_metric_total(
        _CANDIDATE_EXPOSITION,
        "honua_serving_request_duration_ms_count",
        harness.parse_label_selector(selector) if selector else None,
    )
    return slo.evaluate_slo(errors, requests, 0.01)[0]


def test_slo_gate_fails_a_ten_percent_geoservices_error_rate():
    """The hard requirement: this gate must not wave through a badly broken release."""
    assert _slo_verdict(_GEOSERVICES_SELECTOR) == "fail"


def test_slo_gate_would_be_fail_open_without_a_scoped_denominator():
    """Why the selector is mandatory rather than a nicety.

    A GeoServices-only numerator over an unscoped denominator is not merely imprecise — it inverts
    the gate's verdict on the exact scenario it exists to catch, which is worse than the no-signal
    state it replaced. Kept as a test so nobody 'simplifies' the selector away.
    """
    assert _slo_verdict(None) == "pass"


# ---- upgrade -------------------------------------------------------------------------------------
def _manifest(server_db, clients):
    comps = {"honua-server": {"version": "pre-release", "sha": "a" * 40, "dbSchema": server_db}}
    for name, ver in clients.items():
        comps[name] = {"version": ver, "sha": "b" * 40, "artifact": f"npm:{name}"}
    return {"components": comps}


def _matrix(contract, ranges):
    return {"contracts": {contract: {"version": "v1", "clients": ranges}}}


def test_upgrade_old_client_still_supported_passes():
    prior = _manifest("metadata-v1", {"honua-sdk-js": "0.0.14-alpha.0"})
    cand = _manifest("metadata-v1", {"honua-sdk-js": "0.0.20"})
    matrix = _matrix("geoservices", {"honua-sdk-js": ">=0.0.10 <0.1.0"})
    rows, overall = up.evaluate_upgrade(prior, cand, matrix)
    assert overall == "pass"


def test_upgrade_strands_old_client_fails():
    prior = _manifest("metadata-v1", {"honua-sdk-js": "0.0.14-alpha.0"})
    cand = _manifest("metadata-v1", {"honua-sdk-js": "0.1.0"})
    # candidate matrix dropped support for the prior's 0.0.14 client.
    matrix = _matrix("geoservices", {"honua-sdk-js": ">=0.1.0 <0.2.0"})
    rows, overall = up.evaluate_upgrade(prior, cand, matrix)
    assert overall == "fail" and any("strands" in r["why"] for r in rows)


def test_upgrade_db_schema_backwards_fails():
    prior = _manifest("metadata-v3", {})
    cand = _manifest("metadata-v2", {})        # migrations went backwards
    rows, overall = up.evaluate_upgrade(prior, cand, {})
    assert overall == "fail" and any("backwards" in r["why"] for r in rows)


def test_upgrade_db_schema_forward_passes():
    prior = _manifest("metadata-v2", {})
    cand = _manifest("metadata-v3", {})
    rows, overall = up.evaluate_upgrade(prior, cand, {})
    assert overall == "pass"


def test_upgrade_nothing_comparable_is_blocked():
    # All sha-pinned, no db schema -> nothing to decide from manifests alone.
    prior = {"components": {"honua-server": {"version": "pre-release", "sha": "a" * 40}}}
    cand = {"components": {"honua-server": {"version": "pre-release", "sha": "c" * 40}}}
    rows, overall = up.evaluate_upgrade(prior, cand, {})
    assert overall == "blocked" and rows == []


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
