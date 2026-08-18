#!/usr/bin/env python3
"""Guardrail for certification/gate-exceptions.yaml (honua-release#83).

The ledger records WHY a currently-red release-train gate is red and where the fix is tracked. It is
annotation only — the train's report job asserts, at runtime, that attaching an exception changes no
verdict. What that runtime assert cannot catch is a ledger entry that names a gate id nothing emits:
it would silently annotate nothing while looking like documentation exists. These tests catch that,
plus the two ways an entry can be documentation-shaped but useless (no issue link, no reason).

The gate id list is not duplicated here — it is parsed out of .github/workflows/release-train.yml's
own `rows=` block, so renaming or deleting a gate breaks this test instead of leaving a dangling
entry that reads as covered.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
LEDGER_PATH = REPO_ROOT / "certification" / "gate-exceptions.yaml"
TRAIN_PATH = REPO_ROOT / ".github" / "workflows" / "release-train.yml"

ISSUE_RE = re.compile(r"^https://github\.com/honua-io/[a-z0-9-]+/issues/\d+$")


def _emitted_gate_ids() -> set[str]:
    """The gate ids the train's report job actually writes into gate-report.json."""
    text = TRAIN_PATH.read_text(encoding="utf-8")
    block = re.search(r"rows=\$\(cat <<EOF\n(.*?)\n\s*EOF", text, re.DOTALL)
    assert block, "could not locate the report job's `rows=` block in release-train.yml"
    ids = {line.strip().split("|", 1)[0] for line in block.group(1).splitlines() if "|" in line}
    assert len(ids) > 5, f"suspiciously few gate ids parsed: {ids}"
    return ids


def _ledger() -> dict:
    return (yaml.safe_load(LEDGER_PATH.read_text(encoding="utf-8")) or {}).get("gates") or {}


def test_every_exception_names_a_gate_the_train_emits():
    unknown = sorted(set(_ledger()) - _emitted_gate_ids())
    assert not unknown, (
        f"gate-exceptions.yaml documents gate ids the release train does not emit: {unknown}. "
        f"Emitted ids: {sorted(_emitted_gate_ids())}"
    )


@pytest.mark.parametrize("gate", sorted(_ledger()))
def test_exception_carries_an_issue_link_and_a_reason(gate):
    entry = _ledger()[gate]
    assert ISSUE_RE.match(str(entry.get("issue", ""))), (
        f"{gate}: 'issue' must be a honua-io issue URL, got {entry.get('issue')!r}"
    )
    assert len(" ".join(str(entry.get("why", "")).split())) >= 40, (
        f"{gate}: 'why' must actually explain the red, not restate the gate name"
    )


def test_ledger_cannot_carry_verdict_overrides():
    """No entry may declare anything that looks like a status/verdict field.

    The ledger is annotation. If someone adds `status: pass` or `suppress: true` expecting it to be
    honoured, it would be silently ignored by the report job — which is worse than failing here,
    because they would believe the gate was excused.
    """
    forbidden = {"status", "verdict", "suppress", "skip", "ignore", "override", "result"}
    for gate, entry in _ledger().items():
        offending = forbidden & set(entry or {})
        assert not offending, (
            f"{gate}: {sorted(offending)} is not honoured — an exception never changes a verdict. "
            f"Remove it, or fix the gate."
        )
