"""Build/test passes only when every required build and test lane of a component ran and succeeded."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "certification"))
import check_build_test as bt  # noqa: E402

CI = ".github/workflows/ci.yml"
LANES = frozenset({(CI, "build"), (CI, "test")})
SHA = "a" * 40


def _check(name, conclusion="success", *, workflow=CI, run_id=1, status="completed"):
    run = {"id": run_id, "name": name, "status": status, "conclusion": conclusion,
           "app": {"slug": "github-actions"}, "_workflow_id": workflow}
    if workflow:
        run["_workflow_path"] = workflow
    return run


def _verdict(*checks, lanes=LANES):
    report = bt.evaluate({"components": {"lib": {"sha": SHA}}}, lambda *_: {"check_runs": list(checks)},
                         "strict", env_gated_events={}, required_lanes={"lib": lanes})
    row = report["components"][0]
    return row["status"], row["why"]


def test_all_required_lanes_succeeded_passes():
    status, why = _verdict(_check("build"), _check("test"), _check("lint"))
    assert status == "pass", why
    assert "all 2 required build/test lane(s) succeeded" in why


def test_a_lint_only_payload_does_not_pass():
    status, why = _verdict(_check("lint"))
    assert status == "blocked"
    assert "missing" in why and "build [.github/workflows/ci.yml]" in why and "test [" in why


@pytest.mark.parametrize("conclusion", ["skipped", "neutral", "cancelled"])
def test_a_required_lane_that_did_not_succeed_does_not_pass(conclusion):
    status, why = _verdict(_check("build", conclusion), _check("test"))
    assert status == "blocked"
    assert f"build={conclusion}" in why


def test_a_skipped_build_alone_does_not_pass():
    assert _verdict(_check("build", "skipped"))[0] == "blocked"


def test_the_latest_attempt_decides_a_required_lane():
    rerun_skipped = _verdict(_check("build", run_id=1), _check("build", "skipped", run_id=2), _check("test"))
    assert rerun_skipped[0] == "blocked"
    rerun_green = _verdict(_check("build", "skipped", run_id=1), _check("build", run_id=2), _check("test"))
    assert rerun_green[0] == "pass", rerun_green[1]


def test_a_required_lane_still_running_is_blocked():
    status, why = _verdict(_check("build", None, status="in_progress"), _check("test"))
    assert status == "blocked"


def test_a_red_required_lane_fails():
    assert _verdict(_check("build", "failure"), _check("test"))[0] == "fail"


def test_a_same_named_job_in_another_workflow_does_not_satisfy_the_lane():
    status, why = _verdict(_check("build", workflow=".github/workflows/docs.yml"), _check("test"))
    assert status == "blocked" and "missing" in why


def test_a_lane_without_a_recorded_workflow_path_is_not_proven():
    status, why = _verdict(_check("build", workflow=""), _check("test"))
    assert status == "blocked" and "missing" in why


def test_a_component_without_declared_lanes_is_blocked():
    status, why = _verdict(_check("build"), _check("test"), lanes=frozenset())
    assert status == "blocked" and "no required build/test lanes declared" in why


def test_an_unknown_component_is_blocked_by_the_default_policy():
    report = bt.evaluate({"components": {"not-a-component": {"sha": SHA}}},
                         lambda *_: {"check_runs": [_check("build")]}, "strict", env_gated_events={})
    assert report["components"][0]["status"] == "blocked"


@pytest.mark.parametrize("document", [
    "lib: [build]",
    "lib: {}",
    "lib: {ci.yml: [build]}",
    "lib: {.github/workflows/ci.yml: []}",
    "lib: {.github/workflows/ci.yml: build}",
    "lib: {.github/workflows/ci.yml: ['']}",
    "- lib",
])
def test_a_malformed_requirement_is_rejected(tmp_path, document):
    path = tmp_path / "required.yaml"
    path.write_text(document, encoding="utf-8")
    with pytest.raises(ValueError):
        bt.load_required_lanes(path)


def test_the_requirement_file_parses_to_workflow_and_job_pairs(tmp_path):
    path = tmp_path / "required.yaml"
    path.write_text("lib:\n  .github/workflows/ci.yml: [build, test]\n", encoding="utf-8")
    assert bt.load_required_lanes(path) == {"lib": LANES}


def test_every_manifest_component_declares_its_lanes_exactly_once():
    manifest = yaml.safe_load((ROOT / "platform-manifest.yaml").read_text(encoding="utf-8"))
    required, full_matrix = bt.load_required_lanes(), bt.load_full_matrix()
    assert set(manifest["components"]) == set(required) | set(full_matrix)
    assert not set(required) & set(full_matrix)


def test_no_required_lane_is_also_excluded_from_the_verdict():
    exclusions = (bt.load_env_gated(), bt.load_rollup(), bt.load_security(), bt.load_governance())
    advisory, events = bt.load_advisory(), bt.load_env_gated_events()
    for component, lanes in bt.load_required_lanes().items():
        excluded = set().union(*(names.get(component, frozenset()) for names in exclusions))
        advice = advisory.get(component) or {}
        gated_workflows = {workflow for workflow, _ in events.get(component, frozenset())}
        for workflow, name in lanes:
            assert name not in excluded | set(advice.get("names", ())), (component, name)
            assert workflow not in set(advice.get("workflows", ())) | gated_workflows, (component, workflow)
