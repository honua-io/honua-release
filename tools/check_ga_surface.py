#!/usr/bin/env python3
"""Advertised-GA ⊆ evidenced-GA gate (docs gate, sibling of check_capabilities.py's `capability-key`
evidence kind — honua-release#59).

docs/capabilities.yaml only hand-picks a handful of claims. This check widens the net to the WHOLE
honua-evidence capability-matrix.v1.json: every key the server surfaces as GA must meet the same bar
a `capability-key` claim does, or the gate is red — a capability advertised GA without qualifying
evidence is exactly the audit's recurring credibility risk (fabricated/hollow claims), just caught
mechanically across all ~110 keys instead of the ones someone remembered to write down.

Corpus selection — why a key IS "advertised GA" here:
  - `noSurface` is falsy: a `noSurface` key (config-flag, cross-cutting-gate, sdk-only, ...) rides on
    ANOTHER key's route rather than advertising a distinct one of its own, so it is checked (if at all)
    via that other key.
  - `maturity.implemented > 0`: the key has at least one shipped (non-experimental-only) entry. A
    purely `experimental` key (e.g. editing.branch-versioning) is honestly NOT advertised as GA yet —
    its experimental label already discloses that, so it is out of this corpus. (This also covers a
    future `deferred`-only maturity state the same way, since `implemented` stays 0.)

Every key in that corpus is then run through the identical criteria as `capability-key` evidence
(tools/check_capabilities.resolve_capability_key): implemented > 0 (true by corpus construction),
provingTestCount >= floor, and 100% CITE pass rate wherever CITE is joined.

  evaluate_ga_surface(matrix, min_proving_tests) -> (rows, overall)   # pure, unit-tested
  python tools/check_ga_surface.py [--matrix path/to/capability-matrix.v1.json]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_capabilities as cc  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def advertised_ga_keys(matrix: dict) -> list[dict]:
    """Every capability key the server implicitly advertises as GA (see module docstring)."""
    out = []
    for entry in matrix.get("capabilities") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("noSurface"):
            continue
        maturity = entry.get("maturity") or {}
        if (maturity.get("implemented") or 0) <= 0:
            continue
        out.append(entry)
    return out


def evaluate_ga_surface(matrix: dict | None,
                        min_proving_tests: int = cc.DEFAULT_MIN_PROVING_TESTS) -> tuple[list[dict], str]:
    """-> (rows, overall) with overall in {pass, fail, blocked}.

    blocked — the matrix itself is unavailable, OR (defensively) a real matrix parsed to zero
              advertised-GA keys, which is itself a suspicious signal, not a clean pass.
    """
    if matrix is None:
        return [], "blocked"
    rows = []
    for entry in advertised_ga_keys(matrix):
        status, why = cc.resolve_capability_key(entry["key"], matrix, min_proving_tests)
        rows.append({"key": entry["key"], "status": status, "why": why})
    if not rows:
        return rows, "blocked"
    if any(r["status"] == "fail" for r in rows):
        overall = "fail"
    elif any(r["status"] == "blocked" for r in rows):
        overall = "blocked"
    else:
        overall = "pass"
    return rows, overall


def _load_min_proving_tests(capabilities_path: Path) -> int:
    """Shares the exact same floor as the `capability-key` evidence kind (docs/capabilities.yaml's
    top-level `defaults.minProvingTests`) so the two checks can never silently disagree."""
    try:
        import yaml
        data = yaml.safe_load(capabilities_path.read_text(encoding="utf-8")) or {}
        return int((data.get("defaults") or {}).get("minProvingTests", cc.DEFAULT_MIN_PROVING_TESTS))
    except (OSError, ValueError, TypeError):
        return cc.DEFAULT_MIN_PROVING_TESTS


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--matrix", default=None,
                    help="path to a locally-fetched honua-evidence capability-matrix.v1.json "
                         "(env HONUA_CAPABILITY_MATRIX also honored)")
    ap.add_argument("--min-proving-tests", type=int, default=None)
    ap.add_argument("--require-real", action="store_true", help="promote BLOCKED to FAIL (real train cuts)")
    args = ap.parse_args(argv)

    matrix_path = args.matrix or os.environ.get("HONUA_CAPABILITY_MATRIX")
    matrix = cc.load_capability_matrix(matrix_path)
    min_proving = args.min_proving_tests
    if min_proving is None:
        min_proving = _load_min_proving_tests(cc.CAPABILITIES_PATH)

    rows, overall = evaluate_ga_surface(matrix, min_proving)
    print(f"== advertised-GA ⊆ evidenced-GA — {overall.upper()} ({len(rows)} keys checked, "
          f"floor={min_proving}, matrix={'loaded' if matrix else 'unavailable'}) ==")
    for r in rows:
        print(f"  [{r['status'].upper():4}] {r['key']}: {r['why']}")
    if not rows and matrix is not None:
        print("  (matrix parsed but zero advertised-GA keys found — suspicious, treated as blocked)")
    if overall == "fail":
        return 1
    if overall == "blocked" and args.require_real:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
