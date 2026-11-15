"""The findings verifier called by gate-security must not share a concurrency group with its own
scheduled run.

gate-security calls gate-security-findings.yml as a reusable workflow, and both workflows also run
on their own nightly schedules. In a called workflow the `github` context is the caller's, so a
group built only from `github.ref` is identical for the standalone run and the call. GitHub delays
schedules, so the two can start together; cancel-in-progress then cancels the called findings job
and the gate-security report fails with no finding at all.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"
FINDINGS = WORKFLOWS / "gate-security-findings.yml"
SECURITY = WORKFLOWS / "gate-security.yml"
TRAIN = WORKFLOWS / "release-train.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _name(path: Path) -> str:
    return _load(path)["name"]


def _group(path: Path, workflow: str, ref: str = "refs/heads/trunk") -> str:
    values = {"github.workflow": workflow, "github.ref": ref, "inputs.platform_label": ""}

    def evaluate(match: re.Match) -> str:
        term = match.group(1).strip()
        if term not in values:
            raise AssertionError(f"unsupported concurrency expression term {term!r}")
        return values[term]

    return re.sub(r"\$\{\{(.*?)\}\}", evaluate, _load(path)["concurrency"]["group"])


def test_gate_security_still_calls_the_findings_verifier():
    assert _load(SECURITY)["jobs"]["findings"]["uses"] == "./.github/workflows/gate-security-findings.yml"


def test_train_still_calls_gate_security():
    uses = {job.get("uses") for job in _load(TRAIN)["jobs"].values()}
    assert "./.github/workflows/gate-security.yml" in uses


def test_called_findings_cannot_cancel_or_be_cancelled_by_the_standalone_run():
    standalone = _group(FINDINGS, _name(FINDINGS))
    assert _group(FINDINGS, _name(SECURITY)) != standalone
    assert _group(FINDINGS, _name(TRAIN)) != standalone


def test_standalone_findings_runs_are_scoped_per_ref():
    assert _group(FINDINGS, _name(FINDINGS), "refs/pull/1/merge") != _group(FINDINGS, _name(FINDINGS))


def test_called_findings_group_differs_from_its_caller_group():
    assert _group(FINDINGS, _name(SECURITY)) != _group(SECURITY, _name(SECURITY))
