"""Canonical scenario: Sync round-trip / no-duplicates  (STUB).

Seeded from honua-collect#102.

Assert: edit -> sync -> edit -> sync -> restart -> sync  =>  EXACTLY ONE server feature (no duplicate
rows from replayed/idempotent sync). This is the offline-collect seam (honua-collect <-> honua-server)
and is the highest-value data-integrity regression test.

STATUS: stub. The full implementation needs the honua-collect sync client (or its sync protocol) driving
the composed server, plus a feature-count query over the GeoServices/OGC surface. Wiring it up:

  1. create a layer / feature collection on the composed server
  2. via the collect sync client: create one feature offline -> sync
  3. edit the same feature offline -> sync
  4. simulate an app restart (drop local sync state / re-init client) -> sync again
  5. query the server feature count for that stable feature id -> assert == 1

This returns BLOCKED until the collect sync client is installable from staging and the server image is
real. Under E2E_REQUIRE_REAL the BLOCKED becomes a hard failure (the gate can FAIL).
"""
from __future__ import annotations

from runner.harness import Ctx
from runner.report import Result, Status

META = {
    "name": "sync-no-duplicates",
    "seeded_from": "honua-collect#102",
    "requires_server": True,
}


def run(ctx: Ctx) -> Result:
    # TODO(#7): implement the edit/sync/restart/sync round-trip via the honua-collect sync client and
    # assert a single server feature. Until that client + a real server image exist, do not fabricate
    # a pass.
    return Result(
        scenario=META["name"],
        status=Status.BLOCKED,
        seeded_from=META["seeded_from"],
        why="stub: needs honua-collect sync client installable from staging + real server image",
        evidence={"todo": "edit->sync->edit->sync->restart->sync => exactly 1 server feature"},
    )
