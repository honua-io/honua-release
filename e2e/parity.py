"""Deploy-target parity — assert the canonical set produces IDENTICAL results across targets.

This is the executable half of "behaves the same everywhere" (docs/TEST-STRATEGY.md). Given the
canonical CheckResults from two or more targets, a target is in parity with the reference iff every
canonical check reaches the same verdict (name + pass/fail/blocked) on both. A check that is BLOCKED
on a target (e.g. that target was never provisioned) is reported as such, never silently passed.

Pure + side-effect free, so it is unit-tested without any cloud.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from canonical_checks import CheckResult


@dataclass
class TargetRun:
    target: str
    provisioned: bool
    results: list[CheckResult] = field(default_factory=list)
    note: str = ""


@dataclass
class ParityVerdict:
    status: str                       # pass | fail | blocked
    why: str
    reference: str
    target: str
    diffs: list[str] = field(default_factory=list)


def compare(reference: TargetRun, other: TargetRun) -> ParityVerdict:
    """Compare one target against the reference target."""
    if not reference.provisioned or not reference.results:
        return ParityVerdict("blocked", f"reference {reference.target!r} was not provisioned ({reference.note})",
                             reference.target, other.target)
    if not other.provisioned or not other.results:
        return ParityVerdict("blocked", f"{other.target!r} was not provisioned ({other.note})",
                             reference.target, other.target)

    ref_by_name = {r.name: r for r in reference.results}
    oth_by_name = {r.name: r for r in other.results}
    diffs: list[str] = []

    for name in sorted(set(ref_by_name) | set(oth_by_name)):
        rk = ref_by_name.get(name)
        ok = oth_by_name.get(name)
        if rk is None:
            diffs.append(f"{name}: missing on reference {reference.target}")
        elif ok is None:
            diffs.append(f"{name}: missing on {other.target}")
        elif rk.status != ok.status:
            diffs.append(f"{name}: {reference.target}={rk.status} but {other.target}={ok.status}")

    # A target is only "in parity" if the reference itself is clean — otherwise parity is meaningless.
    ref_failed = [r.name for r in reference.results if r.status == "fail"]
    if diffs:
        return ParityVerdict("fail", f"{len(diffs)} canonical check(s) diverged from {reference.target}",
                             reference.target, other.target, diffs)
    if ref_failed:
        return ParityVerdict("fail",
                             f"identical to {reference.target} but the canonical set is failing there too: {ref_failed}",
                             reference.target, other.target)
    return ParityVerdict("pass", f"all canonical checks match {reference.target}",
                         reference.target, other.target)
