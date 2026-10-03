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


def test_internal_key_advertised_as_ga_fails():
    matrix = {"capabilities": [_entry("serve.wfs"), _entry("admin.multi-tenancy")]}
    rows, overall = ga.evaluate_ga_surface(matrix, internal_keys={"admin.multi-tenancy"})
    assert overall == "fail"
    assert [r["key"] for r in rows if r["status"] == "fail"] == ["admin.multi-tenancy"]


def test_preview_internal_key_stays_out_of_the_denominator():
    preview = {"key": "admin.multi-tenancy", "maturity": {"preview": 7}, "provingTestCount": 12,
               "noSurface": None, "cite": []}
    rows, overall = ga.evaluate_ga_surface({"capabilities": [_entry("serve.wfs"), preview]},
                                           internal_keys={"admin.multi-tenancy"})
    assert overall == "pass" and [r["key"] for r in rows] == ["serve.wfs"]


def test_missing_matrix_is_blocked_never_pass():
    rows, overall = ga.evaluate_ga_surface(None, min_proving_tests=5)
    assert overall == "blocked" and rows == []


def test_cite_below_100_fails():
    matrix = {"capabilities": [
        _entry("serve.wfs", proving=109, cite=[{"suite": "WFS 2.0", "passRate": 92.0}]),
    ]}
    rows, overall = ga.evaluate_ga_surface(matrix, min_proving_tests=5)
    assert overall == "fail"


def test_committed_declarations_name_the_same_internal_key():
    platform, err = ga.load_compatibility_matrix(ga.REPO_ROOT / "compatibility-matrix.yaml")
    assert err is None and platform is not None
    keys, key_err = ga.internal_keys_from_compatibility_matrix(platform)
    assert key_err is None and keys == {"admin.multi-tenancy"}
    assert ga.reconcile_internal_keys(ga._load_internal_keys(ga.cc.CAPABILITIES_PATH), keys) is None


def test_internal_keys_come_from_the_compatibility_matrix():
    keys, err = ga.internal_keys_from_compatibility_matrix({
        "capabilities": {
            "multi-tenancy": {"lifecycle": "internal", "capabilityKeys": ["admin.multi-tenancy"]},
            "wfs": {"lifecycle": "ga", "capabilityKeys": ["serve.wfs"]},
        }
    })
    assert err is None and keys == {"admin.multi-tenancy"}


def test_internal_row_without_keys_fails_closed():
    for declared in ([], None, "admin.multi-tenancy"):
        keys, err = ga.internal_keys_from_compatibility_matrix({
            "capabilities": {"multi-tenancy": {"lifecycle": "internal", "capabilityKeys": declared}}
        })
        assert keys is None and err and "at least one" in err


def test_reconcile_fails_when_docs_and_candidate_matrix_diverge():
    assert ga.reconcile_internal_keys({"admin.multi-tenancy"}, {"admin.multi-tenancy"}) is None
    why = ga.reconcile_internal_keys({"admin.multi-tenancy"}, {"admin.other"})
    assert why and "admin.multi-tenancy" in why and "admin.other" in why
    why = ga.reconcile_internal_keys(set(), {"admin.multi-tenancy"})
    assert why and "admin.multi-tenancy" in why


def test_main_fails_when_candidate_matrix_disagrees_with_docs():
    import json
    import tempfile

    evidence = {"capabilities": [_entry("serve.wfs", proving=109), _entry("admin.multi-tenancy")]}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        matrix_path = root / "capability-matrix.v1.json"
        matrix_path.write_text(json.dumps(evidence), encoding="utf-8")
        compat = root / "compatibility-matrix.yaml"
        compat.write_text("capabilities: {}\n", encoding="utf-8")
        rc = ga.main(["--matrix", str(matrix_path), "--compatibility-matrix", str(compat)])
    assert rc == 1


def test_main_fails_when_candidate_internal_row_has_no_keys():
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        compat = Path(directory) / "compatibility-matrix.yaml"
        compat.write_text(
            "capabilities:\n  multi-tenancy:\n    lifecycle: internal\n    capabilityKeys: []\n",
            encoding="utf-8")
        rc = ga.main(["--compatibility-matrix", str(compat)])
    assert rc == 1


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
