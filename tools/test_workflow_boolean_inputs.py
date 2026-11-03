"""Boolean workflow_dispatch inputs are read through the typed `inputs` context, never `github.event.inputs`.

On workflow_dispatch `github.event.inputs.<name>` is a STRING, so "false" is truthy in `||`/`&&`
expressions and `if:` conditions. e2e-local-docker's seam job ran require_real on every dispatch that
way (rc.2 run 41683730172). This test fails on any expression that reads a boolean input that way.
"""
import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))
EXPRESSION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
IF_LINE = re.compile(r"^\s*(?:-\s*)?if:\s*(.+)$", re.M)
EVENT_INPUT = re.compile(r"github\.event\.inputs\.([A-Za-z0-9_-]+)")


def boolean_inputs(document):
    on = document.get(True) or document.get("on") or {}
    dispatch = (on.get("workflow_dispatch") or {}) if isinstance(on, dict) else {}
    return {name for name, spec in ((dispatch or {}).get("inputs") or {}).items()
            if isinstance(spec, dict) and spec.get("type") == "boolean"}


def offending(text):
    bools = boolean_inputs(yaml.safe_load(text) or {})
    found = []
    for match in EXPRESSION.finditer(text):
        found += [name for name in EVENT_INPUT.findall(match.group(1)) if name in bools]
    for match in IF_LINE.finditer(text):
        if "${{" not in match.group(1):
            found += [name for name in EVENT_INPUT.findall(match.group(1)) if name in bools]
    return found


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_boolean_dispatch_inputs_use_the_typed_inputs_context(workflow):
    assert offending(workflow.read_text()) == [], f"{workflow.name}: use inputs.<name>, not github.event.inputs"


def test_the_guard_catches_the_string_boolean_pattern():
    bad = ("on:\n  workflow_dispatch:\n    inputs:\n      require_real: {type: boolean, default: false}\n"
           "jobs:\n  a:\n    if: github.event.inputs.require_real\n    steps:\n      - run: echo\n"
           "        env:\n          X: ${{ (inputs.require_real || github.event.inputs.require_real) && '1' || '' }}\n")
    assert offending(bad) == ["require_real", "require_real"]
    good = bad.replace("github.event.inputs.require_real", "inputs.require_real")
    assert offending(good) == []
    assert offending("on:\n  workflow_dispatch:\n    inputs:\n      label: {type: string}\n"
                     "jobs: {a: {if: \"${{ github.event.inputs.label != '' }}\"}}\n") == []


def test_e2e_local_docker_seam_follows_the_typed_input_and_slices_keep_their_strictness():
    text = (Path(__file__).resolve().parents[1] / ".github/workflows/e2e-local-docker.yml").read_text()
    assert "E2E_REQUIRE_REAL: ${{ inputs.require_real == true && '1' || '' }}" in text
    assert text.count("(inputs.require_real == true || github.event_name == 'schedule' || "
                      "(github.event_name == 'workflow_dispatch' && github.workflow == 'e2e-local-docker')) "
                      "&& '1' || ''") == 2
    assert "\nname: e2e-local-docker\n" in text      # the direct-dispatch clause names this workflow
