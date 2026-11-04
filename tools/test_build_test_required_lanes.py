"""Build/test passes only when every required build and test lane of a component ran and succeeded."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "certification"))
import check_build_test as bt  # noqa: E402
from build_test_policy import BuildTestPolicy, ComponentPolicy  # noqa: E402
from required_lanes import load_required_lanes  # noqa: E402

CI = ".github/workflows/ci.yml"
LANES = frozenset({(CI, "build"), (CI, "test")})
SHA = "a" * 40


def _check(name, conclusion="success"):
    """A completed Actions check-run of the ci.yml workflow, enriched with its workflow path."""
    return {"id": 1, "name": name, "status": "completed", "conclusion": conclusion,
            "app": {"slug": "github-actions"}, "_workflow_id": CI, "_workflow_path": CI}


def _attempt(check, run_id):
    return {**check, "id": run_id}


def _in_workflow(check, workflow):
    return {**check, "_workflow_id": workflow, "_workflow_path": workflow}


def _verdict(*checks, lanes=LANES):
    policy = BuildTestPolicy({"lib": ComponentPolicy(component="lib", required_lanes=lanes)})
    report = bt.BuildTestGate(lambda *_: {"check_runs": list(checks)}, policy).evaluate(
        {"components": {"lib": {"sha": SHA}}}, "strict")
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
    rerun_skipped = _verdict(_check("build"), _attempt(_check("build", "skipped"), 2), _check("test"))
    assert rerun_skipped[0] == "blocked"
    rerun_green = _verdict(_check("build", "skipped"), _attempt(_check("build"), 2), _check("test"))
    assert rerun_green[0] == "pass", rerun_green[1]


def test_a_required_lane_still_running_is_blocked():
    status, why = _verdict({**_check("build", None), "status": "in_progress"}, _check("test"))
    assert status == "blocked"


def test_a_red_required_lane_fails():
    assert _verdict(_check("build", "failure"), _check("test"))[0] == "fail"


def test_a_same_named_job_in_another_workflow_does_not_satisfy_the_lane():
    status, why = _verdict(_in_workflow(_check("build"), ".github/workflows/docs.yml"), _check("test"))
    assert status == "blocked" and "missing" in why


def test_a_lane_without_a_recorded_workflow_path_is_not_proven():
    unenriched = {key: value for key, value in _check("build").items() if key != "_workflow_path"}
    status, why = _verdict(unenriched, _check("test"))
    assert status == "blocked" and "missing" in why


def test_a_component_without_declared_lanes_is_blocked():
    status, why = _verdict(_check("build"), _check("test"), lanes=frozenset())
    assert status == "blocked" and "no required build/test lanes declared" in why


def test_an_unknown_component_is_blocked_by_the_default_policy():
    report = bt.BuildTestGate(lambda *_: {"check_runs": [_check("build")]}, BuildTestPolicy.load()).evaluate(
        {"components": {"not-a-component": {"sha": SHA}}}, "strict")
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
        load_required_lanes(path)


def test_the_requirement_file_parses_to_workflow_and_job_pairs(tmp_path):
    path = tmp_path / "required.yaml"
    path.write_text("lib:\n  .github/workflows/ci.yml: [build, test]\n", encoding="utf-8")
    assert load_required_lanes(path) == {"lib": LANES}


def test_every_manifest_component_declares_its_lanes_exactly_once():
    manifest = yaml.safe_load((ROOT / "platform-manifest.yaml").read_text(encoding="utf-8"))
    components = BuildTestPolicy.load().components
    required = {name for name, policy in components.items() if policy.required_lanes}
    full_matrix = {name for name, policy in components.items() if policy.full_matrix is not None}
    assert set(manifest["components"]) == required | full_matrix
    assert not required & full_matrix


def test_no_required_lane_is_also_excluded_from_the_verdict():
    for component, policy in BuildTestPolicy.load().components.items():
        excluded = (policy.env_gated_names | policy.rollup_names | policy.security_names
                    | policy.governance_names | policy.advisory_names)
        gated_workflows = {workflow for workflow, _ in policy.env_gated_events}
        for workflow, name in policy.required_lanes:
            assert name not in excluded, (component, name)
            assert workflow not in policy.advisory_workflows | gated_workflows, (component, workflow)
