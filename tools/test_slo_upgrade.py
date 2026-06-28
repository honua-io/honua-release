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
import check_slo as slo  # noqa: E402
import check_upgrade as up  # noqa: E402


# ---- SLO -----------------------------------------------------------------------------------------
def test_slo_within_budget_passes():
    assert slo.evaluate_slo(5, 10000, 0.01)[0] == "pass"


def test_slo_over_budget_fails():
    status, why = slo.evaluate_slo(500, 10000, 0.01)   # 5% > 1%
    assert status == "fail" and "exceeds budget" in why


def test_slo_absent_metric_is_blocked_not_pass():
    # The server#2243 blindness: no metric => never a green.
    assert slo.evaluate_slo(None, None, 0.01)[0] == "blocked"
    assert slo.evaluate_slo(0, 0, 0.01)[0] == "blocked"      # no traffic


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
