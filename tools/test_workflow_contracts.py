"""Trust-boundary contracts for release workflow triggers and gates."""
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_contract_suppression as ccs  # noqa: E402
import check_release_promotion_approval as cpa  # noqa: E402


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


def _logical_lines(text: str) -> list[str]:
    """Shell lines with continuations joined, so one command is scanned as one unit.

    Scanning raw lines was an evasion: a `gh api` call split across `\\` continuations put its
    mutating flag on a following line, where the per-line check never looked for it.
    """
    lines: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.endswith("\\"):
            buffer += stripped[:-1].rstrip() + " "
            continue
        joined = (buffer + stripped).strip()
        if joined:
            lines.append(joined)
        buffer = ""
    if buffer.strip():
        lines.append(buffer.strip())
    return lines


def _gh_api_arguments(line: str) -> str | None:
    """The argument tail of a `gh api` invocation, or None when the line is not one."""
    match = re.search(r"\bgh\s+api\b", line)
    return line[match.end():] if match else None


def assert_read_only(commands: str, where: str) -> None:
    """Fail if any shell command in `commands` can write through the GitHub API."""
    for line in _logical_lines(commands):
        arguments = _gh_api_arguments(line)
        if arguments is not None:
            for pattern, name in MUTATING_GH_API_FLAGS:
                assert not re.search(pattern, arguments), (
                    f"mutating gh api flag {name} in {where}: {line}"
                )
        for command in MUTATING_GH_COMMANDS:
            assert command not in line, f"mutating gh command in {where}: {line}"


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
#
# These are matched as PATTERNS against the argument tail that follows `gh api`, not as substrings of
# the whole line. Two evasions made the literal form insufficient:
#   * `-f `/`-F ` with a trailing space missed pflag's ATTACHED shorthand (`-fwait_timer=0`), and
#   * matching anywhere on the line would flag an innocent `[ -f file ]` test that happens to share a
#     line with a read-only call.
# Scanning the tail after `gh api` catches the attached form without the false positive.
MUTATING_GH_API_FLAGS = (
    (r"(?:^|\s)-X(?=\s|=|[A-Za-z])", "-X"),
    (r"(?:^|\s)--method\b", "--method"),
    (r"(?:^|\s)-[fF](?=\s|=|[A-Za-z0-9_])", "-f/-F (including the attached form)"),
    (r"(?:^|\s)--(?:raw-)?field\b", "--field/--raw-field"),
    (r"(?:^|\s)--input\b", "--input"),
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
    assert promote["environment"] == "release-promotion"
    assert not _neutralised(promote), "the promote job must not be continue-on-error"

    steps = promote["steps"]
    texts = [_step_text(step) for step in steps]
    joined = "\n".join(texts)

    # The gate runs, from settings-as-code, in its enforcing mode, with the promotion identity.
    assert "tools/check_release_promotion_approval.py" in joined
    assert "certification/release-promotion-approval.yaml" in joined
    assert "--mode promote" in joined
    assert "--promotion-ref" in joined and "--promotion-actor" in joined

    approval_index = next(
        index for index, text in enumerate(texts) if "check_release_promotion_approval.py" in text
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
            for guard in ("check_release_promotion_approval.py", "candidate_binding.py", "finalize_release.py")
        ):
            assert not _neutralised(step), f"guard step is neutralised: {step.get('name')}"


def test_repo_control_drift_check_is_read_only():
    workflow = _workflow("repo-control-drift.yml")
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}

    jobs = workflow["jobs"].values()
    commands = "\n".join(_step_text(step) for job in jobs for step in job["steps"])
    assert "tools/check_release_promotion_approval.py" in commands
    assert "--mode drift" in commands

    assert_read_only(commands, "the drift monitor")


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


# ── the read-only scanner must itself be proven, not assumed ────────────────────────────────────
#
# A clean shipped workflow is not evidence that the check works — it is equally consistent with a
# check that never fires. These cases exercise the scanner directly, including the two evasions that
# survived the first version of it.

READ_ONLY_COMMANDS = (
    'gh api "repos/$REPOSITORY/environments/$ENVIRONMENT" > environment.json 2>/tmp/env.err',
    'gh api "repos/$REPOSITORY" > repository.json 2>/dev/null || true',
    'gh api "repos/$REPOSITORY/actions/variables" --paginate > variables.json',
    'gh api "repos/$O/$R" --jq .default_branch',
    'if [ -f repository.json ]; then gh api "repos/$O/$R" > branch.json; fi',
    'gh api \\\n  "repos/$O/$R/environments/release-promotion" \\\n  --jq .name',
)

MUTATING_COMMANDS = (
    # the two evasions the re-review found
    'gh api \\\n  --method PUT \\\n  "repos/$O/$R/environments/release-promotion"',
    'gh api "repos/$O/$R/environments/release-promotion" -fwait_timer=0',
    # and the forms the literal list already covered, re-proven through the new scanner
    'gh api -X PATCH "repos/$O/$R/environments/release-promotion"',
    'gh api --method DELETE "repos/$O/$R/environments/release-promotion"',
    'gh api -f prevent_self_review=false "repos/$O/$R/environments/release-promotion"',
    'gh api -F reviewers[][id]=1 "repos/$O/$R/environments/release-promotion"',
    'gh api --field key=value "repos/$O/$R"',
    'gh api --raw-field key=value "repos/$O/$R"',
    'gh api --input payload.json "repos/$O/$R/environments/release-promotion"',
    'gh api \\\n  "repos/$O/$R/environments/release-promotion" \\\n  -Fprevent_self_review=false',
    'gh secret set RELEASE_GH_TOKEN --body "$X"',
    'gh variable set OPENAPI_ALLOW_BREAKING_CHANGES --body true',
    'gh release create honua-2026.1',
    'gh api graphql -f query="mutation {}"',
)


@pytest.mark.parametrize("command", READ_ONLY_COMMANDS)
def test_read_only_scanner_accepts_genuine_reads(command: str):
    assert_read_only(command, "a read-only fixture")


@pytest.mark.parametrize("command", MUTATING_COMMANDS)
def test_read_only_scanner_catches_every_mutating_form(command: str):
    with pytest.raises(AssertionError):
        assert_read_only(command, "a mutating fixture")


def test_logical_lines_join_continuations():
    joined = _logical_lines('gh api \\\n  --method PUT \\\n  "repos/o/r"')
    assert joined == ['gh api --method PUT "repos/o/r"']


# ── the committed registers must load through the checkers that consume them ────────────────────
#
# A policy file that the checker would refuse is otherwise only discovered at promote time — during a
# release, which is the worst possible moment to find out.

SHIPPED_REGISTERS = {
    "tools/check_release_promotion_approval.py": (
        "certification/release-promotion-approval.yaml",
        cpa.load_policy,
    ),
    "tools/check_contract_suppression.py": (
        "certification/contract-suppression-policy.yaml",
        ccs.load_policy,
    ),
}


@pytest.mark.parametrize("script", sorted(SHIPPED_REGISTERS))
def test_the_shipped_register_loads_through_its_checker(script: str):
    register, loader = SHIPPED_REGISTERS[script]
    assert (REPO_ROOT / script).is_file()
    loader(REPO_ROOT / register)  # raises if the committed file would be refused at release time


@pytest.mark.parametrize(
    "workflow_name", ["promote.yml", "repo-control-drift.yml", "gate-contract.yml"]
)
def test_workflows_point_their_checkers_at_the_shipped_register(workflow_name: str):
    workflow = _workflow(workflow_name)
    commands = "\n".join(
        _step_text(step) for job in workflow["jobs"].values() for step in job.get("steps", [])
    )
    for script, (register, _) in SHIPPED_REGISTERS.items():
        if script not in commands:
            continue
        referenced = set(re.findall(r"--policy\s+(\S+)", commands))
        assert referenced == {register}, (
            f"{workflow_name} runs {script} against {referenced}, not the shipped {register}"
        )
