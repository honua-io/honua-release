"""Integration scenario for corpus workflows `cut-release-candidate` + `promote-release` (+ rollbacks).

Exercises the release ops the AI may run: dry-run cut -> assert gate-report all-green -> (rollback:
discard, nothing promoted); and promote -> assert signed Release created -> (rollback: roll the
platform label back to the prior release; prior provenance still verifies).

The cut/gate-report path is real (release-train.yml); promote + label-rollback need a deployed env.
"""
from __future__ import annotations

from runner.report import Result, Status  # noqa: E402

META = {"name": "release-ops-rollback", "workflow": "cut-release-candidate", "requires_candidate": True}


def run(ctx) -> Result:
    return Result(
        scenario=META["name"], status=Status.BLOCKED, seeded_from="corpus:cut-release-candidate,promote-release",
        why="cut/gate-report path is real; promote + platform-label rollback need a deployed environment",
        evidence={"sequence": ["dry-run cut -> gate-report overallStatus==pass", "ROLLBACK: discard RC (no tag/release)",
                               "promote -> signed GitHub Release + BOM", "ROLLBACK: redeploy prior release",
                               "verify deployments on prior release + prior provenance verifies"]})
