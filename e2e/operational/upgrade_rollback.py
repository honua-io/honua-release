"""Integration scenario for corpus workflow `upgrade-version` (+ its rollback).

Exercises: deploy prior release -> snapshot data -> upgrade to target (migrate forward) -> verify
parity + old-clients -> ROLLBACK (redeploy prior / contract migrations) -> verify prior version +
data intact (assert_restored). This is the keystone "safe upgrade" proof for AI-driven ops.

BLOCKED until two releases + a running, migration-capable candidate exist.
"""
from __future__ import annotations

from runner.report import Result, Status  # noqa: E402

META = {"name": "upgrade-version-rollback", "workflow": "upgrade-version", "requires_candidate": True}


def run(ctx) -> Result:
    return Result(
        scenario=META["name"], status=Status.BLOCKED, seeded_from="corpus:upgrade-version",
        why="needs prior+target releases + a migration-capable running candidate; rollback assertion is wired",
        evidence={"sequence": ["deploy prior + seed data + snapshot", "upgrade to target (forward migrations)",
                               "verify parity + N-1 clients + SLO", "ROLLBACK to prior (contract migrations)",
                               "verify prior version + data intact (no loss)"]})
