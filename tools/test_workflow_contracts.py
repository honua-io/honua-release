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


def _step_text(step: dict) -> str:
    """Everything a step can DO: its shell, the action it invokes, and that action's inputs.

    Scanning only `run:` was the flaw in the previous version of these contracts — a release performed
    by a `uses:` action, or a gate neutralised by `continue-on-error`, was invisible to them.
    """
    parts = [str(step.get("run", "")), str(step.get("uses", "")), str(step.get("with", ""))]
    return "\n".join(parts)


def _neutralised(node: dict) -> bool:
    """True when a job/step is allowed to fail without failing the workflow."""
    value = node.get("continue-on-error", False)
    if isinstance(value, str):
        # An expression (`${{ ... }}`) or the string "true" both mean "may be neutralised".
        return value.strip().lower() not in {"", "false"}
    return bool(value)


# Anything that publishes, signs, tags, or finalises a release. None of it may run before, or
# independently of, the approval gate.
RELEASE_ACTIONS = (
    "finalize_release.py",
    "cosign",
    "sigstore/",
    "gh release create",
    "action-gh-release",
    "generate_bom.py",
)

# Every way `gh api` can be made to write. `-f`/`-F`/`--field`/`--raw-field` and `--input` switch gh
# to POST implicitly, so checking only `-X`/`--method` verbs is not enough.
MUTATING_GH_API_FLAGS = (
    "-X",
    "--method",
    "-f ",
    "-F ",
    "--field",
    "--raw-field",
    "--input",
)

# Subcommands that write regardless of flags.
MUTATING_GH_COMMANDS = (
    "gh secret",
    "gh variable",
    "gh release",
    "gh workflow run",
    "gh pr ",
    "gh issue ",
    "gh api graphql",
)


def test_promote_preflights_environment_policy_before_release_steps():
    workflow = _workflow("promote.yml")
    promote = workflow["jobs"]["promote"]
    assert promote["environment"] == "production"
    assert not _neutralised(promote), "the promote job must not be continue-on-error"

    steps = promote["steps"]
    texts = [_step_text(step) for step in steps]
    joined = "\n".join(texts)

    # The gate runs, from settings-as-code, in its enforcing mode, with the promotion identity.
    assert "tools/check_production_approval.py" in joined
    assert "certification/production-approval.yaml" in joined
    assert "--mode promote" in joined
    assert "--promotion-ref" in joined and "--promotion-actor" in joined

    approval_index = next(
        index for index, text in enumerate(texts) if "check_production_approval.py" in text
    )
    # A gate that is allowed to fail is not a gate.
    assert not _neutralised(steps[approval_index]), "the approval gate step must not be continue-on-error"
    # ...and neither is one that runs after the release has already happened.
    before = "\n".join(texts[:approval_index])
    for action in RELEASE_ACTIONS:
        assert action not in before, f"{action} runs before the approval gate"

    # Promotion identity is bound to the certified train run, and the policy/checkers are read from
    # the default branch rather than from whatever ref the promotion was dispatched on.
    assert "candidate_binding.py validate-run" in joined
    first_checkout = next(step for step in steps if "actions/checkout" in str(step.get("uses", "")))
    assert (first_checkout.get("with") or {}).get("ref") == "${{ github.event.repository.default_branch }}"


def test_every_promote_step_that_can_refuse_is_allowed_to_refuse():
    promote = _workflow("promote.yml")["jobs"]["promote"]
    for step in promote["steps"]:
        text = _step_text(step)
        if any(
            guard in text
            for guard in ("check_production_approval.py", "candidate_binding.py", "finalize_release.py")
        ):
            assert not _neutralised(step), f"guard step is neutralised: {step.get('name')}"


def test_repo_control_drift_check_is_read_only():
    workflow = _workflow("repo-control-drift.yml")
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}

    jobs = workflow["jobs"].values()
    commands = "\n".join(_step_text(step) for job in jobs for step in job["steps"])
    assert "tools/check_production_approval.py" in commands
    assert "--mode drift" in commands

    for line in commands.splitlines():
        stripped = line.strip()
        if "gh api" in stripped:
            for flag in MUTATING_GH_API_FLAGS:
                assert flag not in stripped, f"mutating gh api call in the drift monitor: {stripped}"
        for command in MUTATING_GH_COMMANDS:
            assert command not in stripped, f"mutating gh command in the drift monitor: {stripped}"


def test_contract_gate_certifies_the_breaking_change_suppression_state():
    workflow = _workflow("gate-contract.yml")
    jobs = workflow["jobs"]
    assert "breaking-change-suppression" in jobs
    # The suppression verdict must reach the gate report, not just the job log.
    assert "breaking-change-suppression" in jobs["report"]["needs"]

    suppression = jobs["breaking-change-suppression"]
    assert not _neutralised(suppression)
    commands = "\n".join(_step_text(step) for step in suppression["steps"])
    assert "tools/check_contract_suppression.py" in commands
    assert "certification/contract-suppression-policy.yaml" in commands
    # The fragment is emitted even when the job fails, and defaults to a non-green status.
    fragment = next(
        step for step in suppression["steps"] if "gate-fragment" in str(step.get("uses", ""))
    )
    assert fragment.get("if") == "always()"
    assert "blocked" in str(fragment["with"]["status"])


def test_contract_gate_report_cannot_be_assembled_from_survivors():
    """A cancelled check must not silently disappear from the gate report."""
    report = _workflow("gate-contract.yml")["jobs"]["report"]
    commands = "\n".join(_step_text(step) for step in report["steps"])
    for gate in ("contract-proto", "contract-rest-sdk", "contract-suppression"):
        assert gate in commands, f"{gate} is not required by name in the report job"
    assert '"fail"' in commands, "a missing fragment must be recorded as a failure"
