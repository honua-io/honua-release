#!/usr/bin/env python3
"""AI workflow corpus gate — enforce that every operation the AI may run is reversible and tested.

This is the enforceable core of *safe AI-driven ops* (corpus/README.md): the AI only runs workflows
from corpus/workflows/, and a workflow may not enter the corpus unless it is:
  - well-formed: id, intent, category, autonomy_tier in {1,2,3}, non-empty preconditions/steps/verify;
  - ROLLBACK-SAFE: a non-empty rollback.procedure AND rollback.verify — you do not hand an autonomous
    operator an action it cannot prove it can undo. THIS is the rule that makes the rest safe;
  - integration-tested: integration_test points at a scenario file that exists (the test exercises the
    workflow AND its rollback against a real candidate).

`check_corpus` is pure (the file-existence check is injected) so it is unit-tested; the gate fails
closed on any violation.

  python tools/check_corpus.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = REPO_ROOT / "corpus" / "workflows"
VALID_TIERS = {1, 2, 3}
VALID_CATEGORIES = {"data-ops", "platform-ops", "gp-ops", "release-ops"}


def validate_workflow(wf: dict, test_exists: Callable[[str], bool]) -> list[str]:
    """Return a list of errors for one workflow definition (empty = valid)."""
    errs: list[str] = []
    wid = wf.get("id", "<no-id>")

    for field in ("id", "intent", "category"):
        if not str(wf.get(field, "")).strip():
            errs.append(f"{wid}: missing/empty {field!r}")
    if wf.get("category") and wf["category"] not in VALID_CATEGORIES:
        errs.append(f"{wid}: category {wf['category']!r} not in {sorted(VALID_CATEGORIES)}")
    if wf.get("autonomy_tier") not in VALID_TIERS:
        errs.append(f"{wid}: autonomy_tier {wf.get('autonomy_tier')!r} must be one of {sorted(VALID_TIERS)}")

    for field in ("preconditions", "steps", "verify"):
        val = wf.get(field)
        if not isinstance(val, list) or not val:
            errs.append(f"{wid}: {field!r} must be a non-empty list")

    # THE safety rule: a tested rollback is mandatory.
    rb = wf.get("rollback")
    if not isinstance(rb, dict):
        errs.append(f"{wid}: NO rollback — not AI-runnable (every workflow must be reversible)")
    else:
        for sub in ("procedure", "verify"):
            v = rb.get(sub)
            if not isinstance(v, list) or not v:
                errs.append(f"{wid}: rollback.{sub} must be a non-empty list "
                            f"(a rollback you can't verify is not a rollback)")

    test = str(wf.get("integration_test", "")).strip()
    if not test:
        errs.append(f"{wid}: missing integration_test (a workflow must be exercised by a scenario)")
    elif not test_exists(test):
        errs.append(f"{wid}: integration_test {test!r} does not exist")
    return errs


def check_corpus(corpus_dir: Path, test_exists: Callable[[str], bool]) -> tuple[list[dict], str]:
    rows: list[dict] = []
    files = sorted(corpus_dir.glob("*.yaml")) if corpus_dir.is_dir() else []
    if not files:
        return [], "fail"   # an empty corpus is a misconfig, not a pass
    seen_ids: set[str] = set()
    for f in files:
        wf = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        errs = validate_workflow(wf, test_exists)
        wid = wf.get("id", f.stem)
        if wid in seen_ids:
            errs.append(f"{wid}: duplicate workflow id")
        seen_ids.add(wid)
        if wf.get("id") and wf["id"] != f.stem:
            errs.append(f"{wid}: id must match filename ({f.stem})")
        rows.append({"workflow": wid, "file": f.name,
                     "status": "fail" if errs else "pass",
                     "errors": errs,
                     "tier": wf.get("autonomy_tier")})
    overall = "fail" if any(r["status"] == "fail" for r in rows) else "pass"
    return rows, overall


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus-dir", default=str(CORPUS_DIR))
    args = ap.parse_args(argv)

    rows, overall = check_corpus(Path(args.corpus_dir), lambda p: (REPO_ROOT / p).exists())
    print(f"== AI workflow corpus — {overall.upper()} ({len(rows)} workflows) ==")
    for r in rows:
        print(f"  [{r['status'].upper():4}] {r['workflow']} (tier {r['tier']})")
        for e in r["errors"]:
            print(f"         - {e}")
    return 1 if overall == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
