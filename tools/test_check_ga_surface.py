"""Tests for the advertised-GA ⊆ evidenced-GA gate (honua-release#59).

The point of this gate is to catch a capability the server surfaces as GA (a real, implemented,
non-noSurface route) without qualifying evidence — the SAME failure mode check_capabilities.py's
`capability-key` evidence kind catches for a single hand-picked claim, applied across the WHOLE
capability matrix instead.

Run: python -m pytest tools/test_check_ga_surface.py    (or: python tools/test_check_ga_surface.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_ga_surface as ga  # noqa: E402


def _entry(key, implemented=1, proving=10, cite=None, no_surface=None, experimental=None):
    maturity = {}
    if implemented is not None:
        maturity["implemented"] = implemented
    if experimental is not None:
        maturity["experimental"] = experimental
    return {"key": key, "maturity": maturity, "provingTestCount": proving,
             "noSurface": no_surface, "cite": cite or []}


def test_all_ga_keys_evidenced_passes():
    matrix = {"capabilities": [
        _entry("serve.wfs", proving=109, cite=[{"suite": "WFS 2.0", "passRate": 100.0}]),
        _entry("serve.vector-tiles", proving=35),
    ]}
    rows, overall = ga.evaluate_ga_surface(matrix, min_proving_tests=5)
    assert overall == "pass" and len(rows) == 2


def test_under_evidenced_ga_key_fails():
    # The exact demonstration honua-release#59's acceptance criteria calls for: a deliberately
    # under-evidenced GA key fails the gate.
    matrix = {"capabilities": [
        _entry("serve.wfs", proving=109),
        _entry("serve.new-thing", proving=1),   # advertised implemented, but too few proving tests
    ]}
    rows, overall = ga.evaluate_ga_surface(matrix, min_proving_tests=5)
    assert overall == "fail"
    bad = next(r for r in rows if r["key"] == "serve.new-thing")
    assert bad["status"] == "fail"


def test_no_surface_key_excluded_from_corpus():
    matrix = {"capabilities": [
        _entry("serve.wfs", proving=109),
        _entry("caching.redis", implemented=None, proving=0, no_surface={"reasonCode": "config-flag"}),
    ]}
    rows, overall = ga.evaluate_ga_surface(matrix, min_proving_tests=5)
    assert {r["key"] for r in rows} == {"serve.wfs"}
    assert overall == "pass"


def test_experimental_only_key_excluded_from_corpus():
    matrix = {"capabilities": [
        _entry("serve.wfs", proving=109),
        _entry("editing.branch-versioning", implemented=None, proving=27, experimental=15),
    ]}
    rows, overall = ga.evaluate_ga_surface(matrix, min_proving_tests=5)
    assert {r["key"] for r in rows} == {"serve.wfs"}


def test_missing_matrix_is_blocked_never_pass():
    rows, overall = ga.evaluate_ga_surface(None, min_proving_tests=5)
    assert overall == "blocked" and rows == []


def test_cite_below_100_fails():
    matrix = {"capabilities": [
        _entry("serve.wfs", proving=109, cite=[{"suite": "WFS 2.0", "passRate": 92.0}]),
    ]}
    rows, overall = ga.evaluate_ga_surface(matrix, min_proving_tests=5)
    assert overall == "fail"


def test_advertised_ga_keys_selection():
    matrix = {"capabilities": [
        _entry("serve.wfs"),
        _entry("caching.redis", implemented=None, no_surface={"reasonCode": "config-flag"}),
        _entry("editing.branch-versioning", implemented=None, experimental=15),
    ]}
    assert {e["key"] for e in ga.advertised_ga_keys(matrix)} == {"serve.wfs"}


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
