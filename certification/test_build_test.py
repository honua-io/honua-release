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


def _named(*pairs, status="completed"):
    """pairs = (name, conclusion) tuples → a check-runs payload with names."""
    return {"check_runs": [{"name": n, "status": status, "conclusion": c} for n, c in pairs]}


# ---- classify -------------------------------------------------------------------------------------
def test_all_green_passes():
    assert bt.classify(_runs("success", "success", "skipped", "neutral"))[0] == "pass"


def test_any_red_fails():
    assert bt.classify(_runs("success", "failure"))[0] == "fail"
    assert bt.classify(_runs("timed_out"))[0] == "fail"
    assert bt.classify(_runs("startup_failure"))[0] == "fail"


def test_cancelled_is_blocked_flake_not_fail():
    # Cancelled != failed — runner-starvation / fail-fast siblings are re-runnable, not build reds.
    status, why = bt.classify(_runs("success", "cancelled"))
    assert status == "blocked" and "cancelled" in why and "re-runnable" in why


def test_cancelled_never_masks_a_real_red():
    # GUARDRAIL: a genuine failure alongside a cancelled sibling still fails (fail-fast case).
    assert bt.classify(_runs("failure", "cancelled"))[0] == "fail"


def test_no_runs_is_blocked_not_pass():
    assert bt.classify({"check_runs": []})[0] == "blocked"
    assert bt.classify({})[0] == "blocked"


def test_incomplete_is_blocked():
    assert bt.classify(_runs("success", None, status="in_progress"))[0] == "blocked"


def test_red_is_not_masked_by_in_progress_siblings():
    # GUARDRAIL: a completed core FAILURE must fail even while other shards are still running — the
    # mid-run window must not let a live train read tolerate a real red as blocked-in-progress.
    payload = {"check_runs": [
        {"name": "Server Tests (FileImport)", "status": "completed", "conclusion": "failure"},
        {"name": "Server Tests (Scene)", "status": "in_progress", "conclusion": None},
        {"name": "Build & Format Check", "status": "completed", "conclusion": "success"},
    ]}
    assert bt.classify(payload)[0] == "fail"


def test_not_found_is_blocked():
    assert bt.classify(bt.NOT_FOUND)[0] == "blocked"


def test_unknown_conclusion_is_blocked_not_pass():
    # An unrecognised conclusion must never be optimistically treated as green.
    assert bt.classify(_runs("success", "weird_new_state"))[0] == "blocked"


# ---- env-gated integration lanes ------------------------------------------------------------------
LIVE = frozenset({"Live Integration (Testcontainers)"})


def test_env_gated_red_is_skipped_not_fail_when_core_green():
    # Core build+unit lanes green; only the env-gated live lane red → component PASSES (env skipped).
    payload = _named(("Build & Format Check", "success"), ("Unit Tests", "success"),
                     ("Live Integration (Testcontainers)", "failure"))
    status, why = bt.classify(payload, LIVE)
    assert status == "pass"
    assert "env-gated" in why and "skipped" in why


def test_core_red_still_fails_even_with_env_gated_lane_present():
    # GUARDRAIL: a real compile/unit-test red must fail even alongside an env-gated red — never masked.
    payload = _named(("Restore and Build", "failure"),
                     ("Live Integration (Testcontainers)", "failure"))
    assert bt.classify(payload, LIVE)[0] == "fail"


def test_only_env_gated_lanes_is_blocked_never_pass():
    # No core build/test signal left after removing env-gated lanes → blocked, never optimistic pass.
    payload = _named(("Live Integration (Testcontainers)", "failure"))
    assert bt.classify(payload, LIVE)[0] == "blocked"


def test_env_gated_name_not_matching_a_component_still_fails():
    # A red lane that is NOT in the env-gated set (e.g. proto lint) is a real red — still fails.
    payload = _named(("Lint and Validate Protobuf", "failure"), ("Build", "success"))
    assert bt.classify(payload, LIVE)[0] == "fail"


def test_env_gated_lane_green_is_ignored_and_core_decides():
    # Env-gated lane happening to be green is simply excluded; core decides (here: pass).
    payload = _named(("Build", "success"), ("Live Integration (Testcontainers)", "success"))
    status, why = bt.classify(payload, LIVE)
    assert status == "pass" and "env-gated" not in why  # nothing skipped → no env note


def test_load_env_gated_never_lists_a_build_or_unit_lane():
    # GUARDRAIL: the shipped policy file must not down-grade any compile/build/restore/unit lane.
    gated = bt.load_env_gated()
    banned = ("build", "restore", "format", "unit", "compile", ".net sdk", "analyze", "coverage")
    for comp, names in gated.items():
        for n in names:
            low = n.lower()
            assert not any(b in low for b in banned), f"{comp}: '{n}' looks like a core lane"


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
