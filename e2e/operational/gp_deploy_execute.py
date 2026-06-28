"""Integration scenario for corpus workflows `gp-deploy` + `gp-execute` (+ their rollbacks).

Exercises: deploy a GP tool -> verify in catalog -> execute with inputs -> verify result ->
ROLLBACK (revert writes from snapshot + deregister tool) -> verify dataset == pre-exec snapshot and
tool gone (assert_restored).

BLOCKED until a deployed candidate + the GP control-plane API exist.
"""
from __future__ import annotations

from runner.report import Result, Status  # noqa: E402

META = {"name": "gp-deploy-execute-rollback", "workflow": "gp-deploy", "requires_candidate": True}


def run(ctx) -> Result:
    return Result(
        scenario=META["name"], status=Status.BLOCKED, seeded_from="corpus:gp-deploy,gp-execute",
        why="needs a deployed candidate + GP control-plane API; rollback-restoration assertion is wired",
        evidence={"sequence": ["register GP tool + verify catalog", "snapshot target dataset", "execute job -> succeeded",
                               "verify result shape + no error-metric rise", "ROLLBACK: revert writes + deregister",
                               "verify dataset == pre-exec snapshot + tool gone"]})
