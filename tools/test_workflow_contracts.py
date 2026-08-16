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
    steps = promote["steps"]
    commands = [str(step.get("run", "")) for step in steps]
    joined = "\n".join(commands)
    # The approval gate is settings-as-code and runs before anything that could tag/sign/release.
    assert "tools/check_production_approval.py" in joined
    assert "certification/production-approval.yaml" in joined
    approval_index = next(
        index for index, command in enumerate(commands) if "check_production_approval.py" in command
    )
    for later in ("finalize_release.py", "cosign sign-blob", "gh release create"):
        assert later not in "\n".join(commands[:approval_index]), later
    # Promotion identity is bound to the certified train run and the protected default branch.
    assert "candidate_binding.py validate-run" in joined
    assert "--promotion-ref" in joined and "--promotion-actor" in joined


def test_repo_control_drift_check_is_read_only():
    workflow = _workflow("repo-control-drift.yml")
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    commands = "\n".join(
        str(step.get("run", ""))
        for job in workflow["jobs"].values()
        for step in job["steps"]
    )
    assert "tools/check_production_approval.py" in commands
    # Read-only: no mutating gh api verb anywhere in the drift monitor.
    for verb in ("-X PUT", "-X POST", "-X PATCH", "-X DELETE", "--method PUT", "--method POST"):
        assert verb not in commands, verb


def test_contract_gate_certifies_the_breaking_change_suppression_state():
    workflow = _workflow("gate-contract.yml")
    jobs = workflow["jobs"]
    assert "breaking-change-suppression" in jobs
    # The suppression verdict must reach the gate report, not just the job log.
    assert "breaking-change-suppression" in jobs["report"]["needs"]
    commands = "\n".join(str(step.get("run", "")) for step in jobs["breaking-change-suppression"]["steps"])
    assert "tools/check_contract_suppression.py" in commands
    assert "certification/contract-suppression-policy.yaml" in commands
