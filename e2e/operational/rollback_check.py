"""The verifiable core shared by every operational rollback scenario.

A rollback is only trustworthy if you can PROVE the system returned to its pre-operation state. Each
operational scenario snapshots the relevant state before the operation, performs op + rollback, then
asserts the post-rollback snapshot equals the pre-op snapshot. That comparison is pure and unit-tested
here, so the rollback assertion itself is trustworthy with zero infrastructure.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RollbackResult:
    restored: bool
    why: str
    drift: list[str]


def assert_restored(before: dict, after: dict, *, ignore: tuple[str, ...] = ()) -> RollbackResult:
    """Did rollback return state to `before`? Compares two state snapshots key-by-key.

    `before`/`after` are normalised snapshots (e.g. {"services": [...], "schema": 44}). Any key that
    differs (and is not in `ignore`, e.g. a timestamp) is drift — the rollback did NOT fully restore.
    Missing/extra keys are drift too: a rollback that leaves an orphan, or drops something it should
    have kept, is not a clean rollback.
    """
    drift: list[str] = []
    keys = (set(before) | set(after)) - set(ignore)
    for k in sorted(keys):
        b, a = before.get(k, "<absent>"), after.get(k, "<absent>")
        if b != a:
            drift.append(f"{k}: before={b!r} after={a!r}")
    if drift:
        return RollbackResult(False, f"rollback left {len(drift)} difference(s) — state NOT fully restored", drift)
    return RollbackResult(True, "post-rollback state matches the pre-operation snapshot", [])
