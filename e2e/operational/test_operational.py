"""Tests for the rollback-restoration assertion + that every operational scenario imports + is BLOCKED.

The rollback check is the trustworthy core of every operational scenario, so it must itself be proven:
a clean rollback restores exactly, and ANY drift/orphan is caught.

Run: python -m pytest e2e/operational/test_operational.py
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(E2E_DIR))

from operational import rollback_check as rc  # noqa: E402
from runner.report import Status  # noqa: E402


def test_identical_state_is_restored():
    snap = {"services": ["a", "b"], "schema": 44}
    assert rc.assert_restored(snap, dict(snap)).restored


def test_value_drift_is_caught():
    r = rc.assert_restored({"schema": 44}, {"schema": 45})
    assert not r.restored and any("schema" in d for d in r.drift)


def test_orphan_left_by_rollback_is_caught():
    # rollback left an extra service behind -> not a clean rollback.
    r = rc.assert_restored({"services": ["a"]}, {"services": ["a"], "leftover": ["x"]})
    assert not r.restored and any("leftover" in d for d in r.drift)


def test_dropped_key_is_caught():
    r = rc.assert_restored({"services": ["a"], "schema": 44}, {"services": ["a"]})
    assert not r.restored


def test_ignored_keys_do_not_count_as_drift():
    r = rc.assert_restored({"ts": 1, "schema": 44}, {"ts": 999, "schema": 44}, ignore=("ts",))
    assert r.restored


def test_every_operational_scenario_imports_and_is_blocked_without_a_candidate():
    for mod_name in ("publish_rollback", "upgrade_rollback", "gp_deploy_execute", "release_ops"):
        mod = importlib.import_module(f"operational.{mod_name}")
        assert mod.META["requires_candidate"] is True
        result = mod.run(ctx=None)              # no live candidate in CI
        assert result.status is Status.BLOCKED  # honest: never a fake green
        assert mod.META["workflow"]             # references a corpus workflow


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{'OK' if not failures else 'FAILED'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)
