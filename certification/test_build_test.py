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


def _iac_payload(*runs):
    return {"check_runs": [{"id": n, "name": name, "status": "completed", "conclusion": conclusion,
                            "started_at": f"2026-11-02T19:{n:02d}:00Z"} for n, (name, conclusion) in enumerate(runs, 1)]}


def test_iac_live_drift_plans_are_env_gated_but_core_iac_checks_still_decide():
    """Ruling 2026-11-03: honua-iac's scheduled Bootstrap Drift Plan jobs plan against live AWS (honua-iac#338)."""
    gated = bt.load_env_gated()["honua-iac"]
    drift = [name for name in gated if name.startswith("Drift plan (")]
    assert len(drift) == 18 and "Open, update or close the drift issue" in gated
    assert not {"Terraform Validate", "Root coverage"} & gated      # static IaC checks stay core
    status, why = bt.classify(_iac_payload(("Terraform Validate", "success"), ("Root coverage", "success"),
                                           ("Drift plan (aws-demo-deploy)", "failure"),
                                           ("Drift plan (aws-update-rollback-controller)", "failure"),
                                           ("Open, update or close the drift issue", "failure")), gated)
    assert status == "pass" and "env-gated" in why
    status, _ = bt.classify(_iac_payload(("Terraform Validate", "failure"),
                                         ("Drift plan (aws-demo-deploy)", "failure")), gated)
    assert status == "fail"                                         # a core IaC red still fails
    status, _ = bt.classify(_iac_payload(("Terraform Validate", "success"),
                                         ("Drift plan (aws-new-root)", "failure")), gated)
    assert status == "fail"                                         # exact names only, no pattern


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


# dependency-security check-runs
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


GOVERNANCE = frozenset({"Publish SHA-bound Codex review attestation"})


def test_governance_red_is_excluded_when_core_green():
    payload = _named(("Restore and Build", "success"), ("Server Tests (Core)", "success"),
                     ("Publish SHA-bound Codex review attestation", "failure"))
    status, why = bt.classify(payload, frozenset(), frozenset(), frozenset(), GOVERNANCE)
    assert status == "pass", why
    assert "non-build governance" in why


def test_governance_never_masks_real_core_red():
    payload = _named(("Server Tests (Core)", "failure"),
                     ("Publish SHA-bound Codex review attestation", "failure"))
    assert bt.classify(payload, frozenset(), frozenset(), frozenset(), GOVERNANCE)[0] == "fail"


def test_only_governance_checks_is_blocked_never_pass():
    payload = _named(("Publish SHA-bound Codex review attestation", "failure"))
    assert bt.classify(payload, frozenset(), frozenset(), frozenset(), GOVERNANCE)[0] == "blocked"


def test_load_governance_never_lists_build_or_test_lane():
    import re

    governance = bt.load_governance()
    banned = re.compile(r"\b(?:restore|build|compile|unit|tests?|integration|playwright|validate)\b")
    for comp, names in governance.items():
        for name in names:
            assert not banned.search(name.lower()), f"{comp}: '{name}' looks like a core lane"


def test_load_advisory_never_hides_a_product_build_test_or_contract_lane():
    import re

    advisory = bt.load_advisory()
    banned = re.compile(
        r"\b(?:restore|build|compile|unit|tests?|integration|playwright|validate|certify|smoke|coverage)\b"
    )
    # These two names are product-shaped words, and the 2026-10-09 pin-lag / Firefox-sample
    # ruling still excludes them by name. tools/test_check_build_test.py requires that.
    # A workflow-wide exclusion of sdk-certification.yml or first-map-release-smoke.yml stays
    # forbidden below, and any other name containing these words still fails the regex.
    ruled_names = {
        "certify",
        "Three-engine smoke and source-packed parity",
        # Word "validate" only. The job checks an open-PR envelope and builds nothing.
        "Validate normalization envelope",
    }
    forbidden_names = {
        "Build site",
        "Build & Format Check",
        "release-please-ci",
        "Server Tests (FeatureServer Tiles and Replica)",
    }
    forbidden_workflows = {".github/workflows/ci.yml", ".github/workflows/sdk-certification.yml",
                           ".github/workflows/first-map-release-smoke.yml"}
    for comp, policy in advisory.items():
        for name in policy["names"]:
            assert name not in forbidden_names, f"{comp}: '{name}' is a product lane"
            if name not in ruled_names:
                assert not banned.search(name.lower()), f"{comp}: '{name}' looks like a core lane"
        assert not (policy["workflows"] & forbidden_workflows), comp


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


def _studio_check(check_id, conclusion, *, event, title, branch, path=".github/workflows/console-live-journey.yml"):
    run = {"id": check_id, "name": "Studio customer journey on real images", "status": "completed",
           "conclusion": conclusion, "started_at": f"2026-11-03T{check_id:02d}:00:00Z",
           "app": {"slug": "github-actions"},
           "details_url": f"https://github.com/honua-io/honua-console/actions/runs/{1000 + check_id}/job/{check_id}"}
    workflow = {"id": 1000 + check_id, "workflow_id": 77, "path": path, "event": event,
                "display_title": title, "head_branch": branch}
    return run, workflow


def _console_payload(*studio):
    core = {"id": 1, "name": "Validate Console", "status": "completed", "conclusion": "success",
            "started_at": "2026-11-03T00:00:00Z", "app": {"slug": "github-actions"},
            "details_url": "https://github.com/honua-io/honua-console/actions/runs/900/job/1"}
    runs = [core] + [run for run, _ in studio]
    workflows = [{"id": 900, "workflow_id": 1, "path": ".github/workflows/ci.yml", "event": "push",
                  "display_title": "ci", "head_branch": "trunk"}] + [wf for _, wf in studio]
    return bt._enrich_action_workflow_ids({"check_runs": runs}, {"workflow_runs": workflows})


GATE_ID = "release-" + "ab" * 16


def test_release_gate_replays_are_not_counted_but_independent_studio_ci_stays_core():
    """Ruling 2026-11-03: the Studio gate's own dispatched replays are judged by that gate; a scheduled or
    PR run of the same job on the pinned Console commit stays core and is never superseded by a replay."""
    replay = _studio_check(5, "failure", event="workflow_dispatch", title=f"Console live studio-customer {GATE_ID}",
                           branch=f"studio-customer-gate/{GATE_ID}")
    status, why = bt.classify(_console_payload(replay), component="honua-console")
    assert status == "pass" and "release-gate replay" in why
    scheduled = _studio_check(3, "failure", event="schedule", title="Console live nightly 3", branch="trunk")
    green_replay = _studio_check(9, "success", event="workflow_dispatch",
                                 title=f"Console live studio-customer {GATE_ID}", branch=f"studio-customer-gate/{GATE_ID}")
    status, _ = bt.classify(_console_payload(scheduled, green_replay), component="honua-console")
    assert status == "fail"        # a later green replay cannot hide the scheduled source-building failure


import pytest as _pytest


@_pytest.mark.parametrize("change", [
    dict(event="schedule"), dict(title="Console live studio-customer manual-1"), dict(branch="trunk"),
    dict(branch="studio-customer-gate/release-" + "cd" * 16), dict(path=".github/workflows/ci.yml")])
def test_anything_short_of_the_exact_replay_shape_stays_core(change):
    shape = dict(event="workflow_dispatch", title=f"Console live studio-customer {GATE_ID}",
                 branch=f"studio-customer-gate/{GATE_ID}")
    path = change.pop("path", ".github/workflows/console-live-journey.yml")
    shape.update(change)
    status, _ = bt.classify(_console_payload(_studio_check(5, "failure", path=path, **shape)), component="honua-console")
    assert status == "fail"


def test_unenriched_replay_stays_core():
    run, _ = _studio_check(5, "failure", event="workflow_dispatch", title=f"Console live studio-customer {GATE_ID}",
                           branch=f"studio-customer-gate/{GATE_ID}")
    core = {"id": 1, "name": "Validate Console", "status": "completed", "conclusion": "success",
            "started_at": "2026-11-03T00:00:00Z", "app": {"slug": "github-actions"}}
    assert bt.classify({"check_runs": [core, run]}, component="honua-console")[0] == "fail"


def _events():
    return bt.load_env_gated_events()["honua-console"]


def test_scheduled_floating_backend_studio_red_is_skipped_and_core_still_decides():
    """Ruling 2026-11-03: scheduled console-live-journey.yml runs (floating server nightly) are env-gated."""
    scheduled = _studio_check(3, "failure", event="schedule", title="Console live nightly 3", branch="trunk")
    status, why = bt.classify(_console_payload(scheduled), env_gated_events=_events())
    assert status == "pass" and "env-gated scheduled" in why and "[schedule]" in why


@_pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch"])
def test_the_same_workflow_on_other_events_stays_core(event):
    run = _studio_check(3, "failure", event=event, title="Console live 3", branch="trunk")
    assert bt.classify(_console_payload(run), env_gated_events=_events())[0] == "fail"


def test_event_gating_never_leaves_the_console_without_core_and_a_core_red_still_fails():
    scheduled = _studio_check(3, "failure", event="schedule", title="Console live nightly 3", branch="trunk")
    payload = _console_payload(scheduled)
    payload["check_runs"][0]["conclusion"] = "failure"          # Validate Console, a core lane
    assert bt.classify(payload, env_gated_events=_events())[0] == "fail"
    only = bt._enrich_action_workflow_ids({"check_runs": [scheduled[0]]}, {"workflow_runs": [scheduled[1]]})
    assert bt.classify(only, env_gated_events=_events())[0] == "blocked"


def test_event_gating_requires_workflow_and_event_and_is_console_only():
    other = _studio_check(3, "failure", event="schedule", title="x", branch="trunk", path=".github/workflows/ci.yml")
    assert bt.classify(_console_payload(other), env_gated_events=_events())[0] == "fail"
    assert set(bt.load_env_gated_events()) == {"honua-console"}


@_pytest.mark.parametrize("component", ["honua-studio", "honua-server", ""])
def test_the_replay_rule_is_scoped_to_honua_console(component):
    """Sol round 3: the same workflow path, event, title and branch in another component stays core."""
    replay = _studio_check(5, "failure", event="workflow_dispatch", title=f"Console live studio-customer {GATE_ID}",
                           branch=f"studio-customer-gate/{GATE_ID}")
    assert bt.classify(_console_payload(replay), component=component)[0] == "fail"
    assert bt.classify(_console_payload(replay), component="honua-console")[0] == "pass"


def test_evaluate_scopes_the_replay_rule_by_component():
    replay = _studio_check(5, "failure", event="workflow_dispatch", title=f"Console live studio-customer {GATE_ID}",
                           branch=f"studio-customer-gate/{GATE_ID}")
    manifest = {"components": {"honua-console": {"sha": "a" * 40}, "honua-studio": {"sha": "b" * 40}}}
    result = bt.evaluate(manifest, lambda name, sha: _console_payload(replay), enforcement="strict", env_gated_events={})
    verdicts = {row["component"]: row["status"] for row in result["components"]} if "components" in result else \
        {row["component"]: row["status"] for row in result.get("rows", result.get("gates", []))}
    assert verdicts["honua-console"] == "pass" and verdicts["honua-studio"] == "fail"


# honua-server validated-examples descendant rule (ruling 2026-11-03, rc.2 redo)
VX_PATH = ".github/workflows/validated-examples-nightly.yml"
VX_VERIFIED = {"status": "verified", "ruling": "validated-examples-descendant-2026-11-03", "pinSha": "8" * 40,
               "runSha": "a" * 40, "runId": 4242, "changedPaths": ["examples/manifest.json"],
               "imageDigest": "sha256:" + "2" * 64}


def _server_payload(record=None, *, scenario_conclusion="failure", core_conclusion="success", path=VX_PATH,
                    names=("stac-ops", "mobile-offline", "gp-local-dev"), enrich=True):
    runs = [{"id": 1, "name": "Restore and Build", "status": "completed", "conclusion": core_conclusion,
             "started_at": "2026-11-03T00:00:00Z", "app": {"slug": "github-actions"},
             "details_url": "https://github.com/honua-io/honua-server/actions/runs/900/job/1"}]
    for index, name in enumerate(names, start=2):
        runs.append({"id": index, "name": name, "status": "completed", "conclusion": scenario_conclusion,
                     "started_at": "2026-11-03T10:37:00Z", "app": {"slug": "github-actions"},
                     "details_url": "https://github.com/honua-io/honua-server/actions/runs/901/job/%d" % index})
    workflows = [{"id": 900, "workflow_id": 1, "path": ".github/workflows/trunk-sanity.yml", "event": "push"},
                 {"id": 901, "workflow_id": 2, "path": path, "event": "schedule"}]
    payload = {"check_runs": runs}
    if enrich:
        payload = bt._enrich_action_workflow_ids(payload, {"workflow_runs": workflows})
    if record is not None:
        payload["_validated_examples"] = record
    return payload


def test_verified_descendant_replaces_only_the_pins_validated_examples_runs():
    status, why = bt.classify(_server_payload(VX_VERIFIED), component="honua-server")
    assert status == "pass"
    assert "3 validated-examples check(s) on the pin replaced by verified descendant run 4242" in why
    assert "aaaaaaaaaaaa" in why and "examples/manifest.json" in why and VX_VERIFIED["imageDigest"] in why


def test_without_a_verified_descendant_the_pins_red_examples_still_fail():
    assert bt.classify(_server_payload(None), component="honua-server")[0] == "fail"
    unavailable = {"status": "unavailable", "reason": "descendant changes a path outside the examples allowlist"}
    status, why = bt.classify(_server_payload(unavailable), component="honua-server")
    assert status == "fail" and "validated-examples descendant not used" in why and "allowlist" in why


def test_verified_descendant_never_hides_a_core_red():
    payload = _server_payload(VX_VERIFIED, core_conclusion="failure")
    assert bt.classify(payload, component="honua-server")[0] == "fail"


@_pytest.mark.parametrize("component", ["honua-console", "honua-studio", ""])
def test_descendant_rule_is_scoped_to_honua_server(component):
    assert bt.classify(_server_payload(VX_VERIFIED), component=component)[0] == "fail"


def test_descendant_rule_needs_the_workflow_path_and_the_scenario_names():
    other_path = _server_payload(VX_VERIFIED, path=".github/workflows/ci.yml")
    assert bt.classify(other_path, component="honua-server")[0] == "fail"
    other_name = _server_payload(VX_VERIFIED, names=("stac-ops", "unit-tests"))
    assert bt.classify(other_name, component="honua-server")[0] == "fail"
    unenriched = _server_payload(VX_VERIFIED, enrich=False)
    assert bt.classify(unenriched, component="honua-server")[0] == "fail"


def test_evaluate_resolves_the_rule_for_honua_server_only_and_reports_the_record():
    calls = []

    def resolver(name, sha, component, manifest):
        calls.append((name, sha))
        return dict(VX_VERIFIED)

    manifest = {"components": {"honua-server": {"sha": "8" * 40}, "honua-console": {"sha": "c" * 40}}}
    payloads = {"honua-server": _server_payload(), "honua-console": _runs("success")}
    report = bt.evaluate(manifest, lambda name, sha: payloads[name], env_gated_events={},
                         validated_examples=resolver)
    rows = {row["component"]: row for row in report["components"]}
    assert calls == [("honua-server", "8" * 40)]
    assert rows["honua-server"]["status"] == "pass"
    assert rows["honua-server"]["validatedExamples"]["runSha"] == "a" * 40
    assert "validatedExamples" not in rows["honua-console"]
    # Disabled (the default for pure callers): the pin's own red runs decide.
    payloads["honua-server"] = _server_payload()
    report = bt.evaluate(manifest, lambda name, sha: payloads[name], env_gated_events={})
    assert {r["component"]: r["status"] for r in report["components"]}["honua-server"] == "fail"


def test_main_wires_the_live_resolver():
    import inspect
    assert "validated_examples=live_validated_examples" in inspect.getsource(bt.main)
