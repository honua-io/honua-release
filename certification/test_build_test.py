"""Tests for the per-repo build/test fan-out gate.

The gate's job is to fail the train when a pinned component's CI is red or was never run — so each
classification must be proven, including that a green run is the ONLY thing that passes (AGENTS.md).

Run: python -m pytest certification/test_build_test.py    (or: python certification/test_build_test.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_build_test as bt  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def _runs(*conclusions, status="completed"):
    return {"check_runs": [{"status": status, "conclusion": c} for c in conclusions]}


# ---- classify -------------------------------------------------------------------------------------
def test_all_green_passes():
    assert bt.classify(_runs("success", "success", "skipped", "neutral"))[0] == "pass"


def test_any_red_fails():
    assert bt.classify(_runs("success", "failure"))[0] == "fail"
    assert bt.classify(_runs("timed_out"))[0] == "fail"
    assert bt.classify(_runs("cancelled"))[0] == "fail"


def test_no_runs_is_blocked_not_pass():
    assert bt.classify({"check_runs": []})[0] == "blocked"
    assert bt.classify({})[0] == "blocked"


def test_incomplete_is_blocked():
    assert bt.classify(_runs("success", None, status="in_progress"))[0] == "blocked"


def test_not_found_is_blocked():
    assert bt.classify(bt.NOT_FOUND)[0] == "blocked"


def test_unknown_conclusion_is_blocked_not_pass():
    # An unrecognised conclusion must never be optimistically treated as green.
    assert bt.classify(_runs("success", "weird_new_state"))[0] == "blocked"


# ---- evaluate -------------------------------------------------------------------------------------
def _fetch_map(mapping):
    return lambda repo, sha: mapping.get(repo, bt.NOT_FOUND)


def test_evaluate_all_green_is_pass():
    manifest = {"components": {"a": {"sha": "x" * 40}, "b": {"sha": "y" * 40}}}
    rep = bt.evaluate(manifest, _fetch_map({"a": _runs("success"), "b": _runs("success")}))
    assert rep["overallStatus"] == "pass" and rep["summary"]["pass"] == 2


def test_evaluate_one_red_is_fail():
    manifest = {"components": {"a": {"sha": "x" * 40}, "b": {"sha": "y" * 40}}}
    rep = bt.evaluate(manifest, _fetch_map({"a": _runs("success"), "b": _runs("failure")}))
    assert rep["overallStatus"] == "fail"


def test_evaluate_blocked_tolerated_in_bootstrap_fails_in_strict():
    manifest = {"components": {"a": {"sha": "x" * 40}, "b": {"sha": "y" * 40}}}
    fetch = _fetch_map({"a": _runs("success")})  # b -> NOT_FOUND -> blocked
    assert bt.evaluate(manifest, fetch, "bootstrap")["overallStatus"] == "blocked"
    assert bt.evaluate(manifest, fetch, "strict")["overallStatus"] == "fail"


def test_evaluate_component_without_sha_is_blocked():
    manifest = {"components": {"a": {"version": "1.0.0"}}}  # no sha
    rep = bt.evaluate(manifest, _fetch_map({}))
    assert rep["components"][0]["status"] == "blocked" and "no sha" in rep["components"][0]["why"]


def test_real_manifest_every_component_has_a_sha_to_resolve():
    import yaml
    manifest = yaml.safe_load((REPO_ROOT / "platform-manifest.yaml").read_text(encoding="utf-8"))
    # The Phase 0 snapshot pins every component by sha; the gate must have something to resolve CI
    # against for each (else it is structurally blocked and can never certify).
    missing = [n for n, c in (manifest.get("components") or {}).items() if not (c or {}).get("sha")]
    assert not missing, f"components with no sha to resolve CI against: {missing}"


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
