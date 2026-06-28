"""Tests for the AI workflow corpus gate.

The gate's reason to exist is to FAIL CLOSED on an unsafe workflow — above all, one with no tested
rollback (you can't let an autonomous operator run an action it can't undo). Each rule is proven, and
the committed corpus is asserted to itself pass (every shipped workflow is reversible + tested).

Run: python -m pytest tools/test_check_corpus.py    (or: python tools/test_check_corpus.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_corpus as cc  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
_EXISTS = lambda p: True   # noqa: E731 - tests inject test-file existence


def _wf(**over):
    base = {
        "id": "x", "intent": "do x", "category": "data-ops", "autonomy_tier": 2,
        "preconditions": ["p"], "steps": ["s"], "verify": ["v"],
        "rollback": {"procedure": ["undo"], "verify": ["confirm undone"]},
        "integration_test": "e2e/operational/publish_rollback.py",
    }
    base.update(over)
    return base


def test_well_formed_workflow_validates():
    assert cc.validate_workflow(_wf(), _EXISTS) == []


def test_missing_rollback_is_rejected():
    errs = cc.validate_workflow(_wf(rollback=None), _EXISTS)
    assert any("NO rollback" in e for e in errs)


def test_rollback_without_verify_is_rejected():
    errs = cc.validate_workflow(_wf(rollback={"procedure": ["undo"]}), _EXISTS)
    assert any("rollback.verify" in e for e in errs)


def test_empty_rollback_procedure_is_rejected():
    errs = cc.validate_workflow(_wf(rollback={"procedure": [], "verify": ["x"]}), _EXISTS)
    assert any("rollback.procedure" in e for e in errs)


def test_bad_autonomy_tier_is_rejected():
    assert any("autonomy_tier" in e for e in cc.validate_workflow(_wf(autonomy_tier=5), _EXISTS))
    assert any("autonomy_tier" in e for e in cc.validate_workflow(_wf(autonomy_tier=None), _EXISTS))


def test_unknown_category_is_rejected():
    assert any("category" in e for e in cc.validate_workflow(_wf(category="random-ops"), _EXISTS))


def test_empty_steps_or_verify_is_rejected():
    assert any("steps" in e for e in cc.validate_workflow(_wf(steps=[]), _EXISTS))
    assert any("verify" in e for e in cc.validate_workflow(_wf(verify=[]), _EXISTS))


def test_missing_integration_test_file_is_rejected():
    errs = cc.validate_workflow(_wf(integration_test="e2e/operational/nope.py"), lambda p: False)
    assert any("does not exist" in e for e in errs)
    assert cc.validate_workflow(_wf(integration_test=""), _EXISTS)  # empty -> error list non-empty


# ---- the committed corpus must itself pass --------------------------------------------------------
def test_committed_corpus_is_all_rollback_safe_and_tested():
    rows, overall = cc.check_corpus(REPO_ROOT / "corpus" / "workflows", lambda p: (REPO_ROOT / p).exists())
    failed = [r for r in rows if r["status"] == "fail"]
    assert overall == "pass", f"committed corpus has invalid workflows: {failed}"
    assert len(rows) >= 6   # publish, upgrade, gp-deploy, gp-execute, cut-rc, promote


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
