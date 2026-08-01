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


def test_latest_success_supersedes_an_older_failure_for_the_same_lane():
    payload = {"check_runs": [
        {"id": 10, "name": "Build", "status": "completed", "conclusion": "failure",
         "completed_at": "2026-07-01T00:00:00Z"},
        {"id": 20, "name": "Build", "status": "completed", "conclusion": "success",
         "completed_at": "2026-07-02T00:00:00Z"},
    ]}
    assert bt.classify(payload)[0] == "pass"


def test_latest_failure_supersedes_an_older_success_for_the_same_lane():
    payload = {"check_runs": [
        {"id": 10, "name": "Build", "status": "completed", "conclusion": "success",
         "completed_at": "2026-07-01T00:00:00Z"},
        {"id": 20, "name": "Build", "status": "completed", "conclusion": "failure",
         "completed_at": "2026-07-02T00:00:00Z"},
    ]}
    assert bt.classify(payload)[0] == "fail"


def test_latest_in_progress_attempt_blocks_instead_of_reusing_an_older_green():
    payload = {"check_runs": [
        {"id": 10, "name": "Build", "status": "completed", "conclusion": "success",
         "completed_at": "2026-07-01T00:00:00Z"},
        {"id": 20, "name": "Build", "status": "in_progress", "conclusion": None,
         "started_at": "2026-07-02T00:00:00Z"},
    ]}
    assert bt.classify(payload)[0] == "blocked"


def test_higher_id_queued_attempt_blocks_even_without_timestamps():
    payload = {"check_runs": [
        {"id": 20, "name": "Build", "status": "completed", "conclusion": "success",
         "started_at": "2026-07-02T00:00:00Z", "completed_at": "2026-07-02T00:01:00Z"},
        {"id": 30, "name": "Build", "status": "queued", "conclusion": None,
         "started_at": None, "completed_at": None},
    ]}
    assert bt.classify(payload)[0] == "blocked"


def test_distinct_named_lanes_remain_independently_decisive():
    payload = _named(("Build", "success"), ("Unit Tests", "failure"))
    assert bt.classify(payload)[0] == "fail"


def test_same_name_from_distinct_apps_remains_independently_decisive():
    payload = {"check_runs": [
        {"name": "Analyze", "app": {"slug": "codeql"}, "status": "completed", "conclusion": "success"},
        {"name": "Analyze", "app": {"slug": "another-scanner"}, "status": "completed", "conclusion": "failure"},
    ]}
    assert bt.classify(payload)[0] == "fail"


def test_same_actions_job_name_from_distinct_workflows_remains_decisive():
    payload = {"check_runs": [
        {"id": 10, "name": "Build", "app": {"slug": "github-actions"}, "_workflow_id": "100",
         "status": "completed", "conclusion": "success"},
        {"id": 20, "name": "Build", "app": {"slug": "github-actions"}, "_workflow_id": "200",
         "status": "completed", "conclusion": "failure"},
    ]}
    assert bt.classify(payload)[0] == "fail"


def test_unenriched_actions_suites_are_kept_independent_conservatively():
    payload = {"check_runs": [
        {"id": 10, "name": "Build", "app": {"slug": "github-actions"}, "check_suite": {"id": 1000},
         "status": "completed", "conclusion": "success"},
        {"id": 20, "name": "Build", "app": {"slug": "github-actions"}, "check_suite": {"id": 2000},
         "status": "completed", "conclusion": "failure"},
    ]}
    assert bt.classify(payload)[0] == "fail"


def test_actions_run_metadata_enriches_jobs_with_stable_workflow_identity():
    payload = {"check_runs": [
        {"name": "Build", "app": {"slug": "github-actions"},
         "details_url": "https://github.com/honua-io/repo/actions/runs/42/job/99"},
    ]}
    workflows = {"workflow_runs": [{"id": 42, "workflow_id": 1234}]}
    enriched = bt._enrich_action_workflow_ids(payload, workflows)
    assert enriched["check_runs"][0]["_workflow_id"] == "1234"


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


# ---- aggregate / roll-up check-runs ---------------------------------------------------------------
ROLLUP = frozenset({"CI Gate", "Test Suite Summary"})


def test_rollup_failure_from_cancelled_leaf_is_blocked_not_fail():
    # The real honua-server case: all leaf shards green except two starvation-CANCELLED; the aggregator
    # roll-ups ("CI Gate", "Test Suite Summary") therefore go `failure`. Excluding the roll-ups, the
    # verdict follows the leaves → cancelled flake → BLOCKED (re-runnable), never a false fail.
    payload = _named(
        ("Server Tests (Core)", "success"),
        ("Server Tests (Server Features Misc)", "cancelled"),
        ("Server Tests (Server Features Admin and Console)", "cancelled"),
        ("Restore and Build", "success"),
        ("CI Gate", "failure"),
        ("Test Suite Summary", "failure"),
    )
    status, why = bt.classify(payload, frozenset(), ROLLUP)
    assert status == "blocked", why
    assert "cancelled" in why and "re-runnable" in why
    assert "roll-up" in why  # the excluded aggregators are surfaced for audit


def test_rollup_never_masks_a_real_leaf_red():
    # GUARDRAIL: a genuine test failure surfaces on a LEAF shard (which stays in core), so excluding the
    # aggregator roll-ups must NOT hide it — still fails.
    payload = _named(
        ("Server Tests (FileImport)", "failure"),   # real leaf red
        ("Server Tests (Core)", "success"),
        ("CI Gate", "failure"),
        ("Test Suite Summary", "failure"),
    )
    assert bt.classify(payload, frozenset(), ROLLUP)[0] == "fail"


def test_rollup_all_leaves_green_is_pass():
    # Leaves all green; the roll-ups (also green here) are simply excluded → pass.
    payload = _named(
        ("Server Tests (Core)", "success"), ("Restore and Build", "success"),
        ("CI Gate", "success"), ("Test Suite Summary", "success"),
    )
    assert bt.classify(payload, frozenset(), ROLLUP)[0] == "pass"


def test_only_rollup_checks_is_blocked_never_pass():
    # If nothing but aggregators are present, there is no leaf build/test signal → blocked, never pass.
    payload = _named(("CI Gate", "success"), ("Test Suite Summary", "success"))
    assert bt.classify(payload, frozenset(), ROLLUP)[0] == "blocked"


def test_load_rollup_never_lists_a_build_or_test_lane():
    # GUARDRAIL: the shipped roll-up policy must only name pure aggregators, never a real shard/build.
    rollup = bt.load_rollup()
    banned = ("restore", "build", "format", "compile", "unit", "server tests (", "analyze", "coverage",
              "integration")
    for comp, names in rollup.items():
        for n in names:
            low = n.lower()
            assert not any(b in low for b in banned), f"{comp}: '{n}' looks like a real lane, not an aggregator"


# ---- dependency-security check-runs ---------------------------------------------------------------
SECURITY = frozenset({"Dependabot"})


def test_security_red_is_excluded_when_core_green():
    # The real honua-console case: all real build/test lanes green; only the Dependabot dependency-
    # security check red → component PASSES (security is gate_security's concern, not build-test).
    payload = _named(
        ("Validate Console", "success"),
        ("Playwright smoke", "success"),
        ("Live Integration (Testcontainers, pinned server image)", "success"),
        ("Dependabot", "failure"),
    )
    status, why = bt.classify(payload, frozenset(), frozenset(), SECURITY)
    assert status == "pass", why
    assert "dependency-security" in why and "gate_security" in why  # surfaced for audit


def test_security_never_masks_a_real_core_red():
    # GUARDRAIL: a genuine build/unit red must fail even alongside a red security check — never masked.
    payload = _named(("Validate Console", "failure"), ("Dependabot", "failure"))
    assert bt.classify(payload, frozenset(), frozenset(), SECURITY)[0] == "fail"


def test_only_security_checks_is_blocked_never_pass():
    # If nothing but security checks are present, there is no build/test signal → blocked, never pass.
    payload = _named(("Dependabot", "success"))
    assert bt.classify(payload, frozenset(), frozenset(), SECURITY)[0] == "blocked"


def test_security_green_is_ignored_and_core_decides():
    # A security check happening to be green is simply excluded; core decides (here: pass, no note).
    payload = _named(("Validate Console", "success"), ("Dependabot", "success"))
    status, why = bt.classify(payload, frozenset(), frozenset(), SECURITY)
    assert status == "pass" and "dependency-security" not in why  # nothing excluded-nongreen → no note


def test_load_security_never_lists_a_build_or_test_lane():
    # GUARDRAIL: the shipped security policy must only name dependency-security signals, never a real
    # build/compile/unit/integration lane.
    security = bt.load_security()
    banned = ("restore", "build", "format", "compile", "unit", "server tests (", "analyze", "coverage",
              "integration", "playwright", "validate console")
    for comp, names in security.items():
        for n in names:
            low = n.lower()
            assert not any(b in low for b in banned), f"{comp}: '{n}' looks like a real lane, not a security signal"


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
