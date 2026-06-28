"""Integration scenario for corpus workflow `publish-service` (+ its rollback).

Exercises: snapshot catalog -> publish service -> verify queryable -> ROLLBACK (unpublish) ->
verify catalog restored (rollback_check.assert_restored vs the pre-publish snapshot).

BLOCKED until a deployed candidate + control-plane API exist; the rollback-restoration assertion is
real today (rollback_check), the publish/unpublish calls need the live admin API.
"""
from __future__ import annotations

from runner.report import Result, Status  # noqa: E402

META = {"name": "publish-service-rollback", "workflow": "publish-service", "requires_candidate": True}


def run(ctx) -> Result:
    return Result(
        scenario=META["name"], status=Status.BLOCKED, seeded_from="corpus:publish-service",
        why="needs a deployed candidate + control-plane publish API; rollback-restoration assertion is wired",
        evidence={"sequence": ["snapshot /rest/services", "POST publish", "verify queryable + no error-metric rise",
                               "ROLLBACK: DELETE service", "verify catalog == pre-publish snapshot"]})
