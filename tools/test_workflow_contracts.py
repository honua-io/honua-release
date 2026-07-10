"""Trust-boundary contracts for release workflow triggers and gates."""
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _workflow(name: str) -> dict:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def _triggers(workflow: dict) -> dict:
    # PyYAML 1.1 parses the unquoted Actions key `on` as boolean true.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    return triggers


def test_manifest_validate_runs_for_every_pull_request_with_stable_check_name():
    workflow = _workflow("manifest-validate.yml")
    pull_request = _triggers(workflow).get("pull_request")
    assert pull_request is None or "paths" not in pull_request
    assert "validate" in workflow["jobs"]


def test_promote_preflights_environment_policy_before_release_steps():
    workflow = _workflow("promote.yml")
    promote = workflow["jobs"]["promote"]
    assert promote["environment"] == "production"
    commands = "\n".join(str(step.get("run", "")) for step in promote["steps"])
    assert "validate-environment" in commands
    assert "EXPECTED_REVIEWER_ID" in str(promote["steps"])
