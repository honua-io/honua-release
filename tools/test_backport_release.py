"""Backport admission refuses a missing release branch and an unlabelled or non-trunk change."""
from pathlib import Path

import pytest
import yaml

import backport_release as backport

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


def test_missing_release_branch_is_a_founder_step_not_a_create():
    refused = backport.plan(SHA, False)
    assert refused["action"] == "refuse"
    assert "will not create it" in refused["founder"]
    assert "git push origin release/2026.1" in refused["founder"]
    with pytest.raises(backport.BackportError):
        backport.plan("abc", True)


def test_existing_branch_plans_a_labelled_cherry_pick():
    planned = backport.plan(SHA, True)
    assert planned == {
        "action": "cherry-pick",
        "target": "release/2026.1",
        "label": "backport/2026.1",
        "head": "backport/" + SHA[:12],
        "sha": SHA,
    }


def test_admit_requires_the_label_and_trunk_patch_equivalence():
    assert backport.admit("release/2026.1", ["backport/2026.1", "bug"], ["- abc first", "- def second"]) == "admit"
    with pytest.raises(backport.BackportError, match="missing backport/2026.1"):
        backport.admit("release/2026.1", ["bug"], ["- abc"])
    with pytest.raises(backport.BackportError, match="not a cherry-pick"):
        backport.admit("release/2026.1", ["backport/2026.1"], ["- abc", "+ deadbeef local only"])
    with pytest.raises(backport.BackportError, match="no commits"):
        backport.admit("release/2026.1", ["backport/2026.1"], [])
    with pytest.raises(backport.BackportError, match="target release/2026.1"):
        backport.admit("trunk", ["backport/2026.1"], ["- abc"])


def test_workflow_does_not_create_the_release_branch_or_call_privileged_workflows():
    path = ROOT / ".github/workflows/backport-release.yml"
    text = path.read_text(encoding="utf-8")
    document = yaml.safe_load(text)
    # PyYAML 1.1 parses the unquoted Actions key `on` as boolean true.
    triggers = document.get("on", document.get(True))
    assert "workflow_dispatch" in triggers
    assert "git push origin release/2026.1" not in text
    assert "git checkout -b release/2026.1" not in text
    for forbidden in ("e2e-cloud-aws", "rc-release", "release-train", "nightly-certification",
                      "terminal-model-canary", "cloud-cell-retirement"):
        assert forbidden not in text
    assert "tools/backport_release.py plan" in text
    assert "tools/backport_release.py admit" in text
    assert "merge-base --is-ancestor" in text
    assert "backport/2026.1" in text
