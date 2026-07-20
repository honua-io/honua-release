#!/usr/bin/env python3
"""Freeze-phase evidence lineage + freshness gate (honua-release#60).

The release train's freeze job pins platform-manifest.yaml's honua-server SHA as the release
candidate's identity. Nothing previously checked that honua-evidence's capability-matrix.v1.json —
the evidence backing every `capability-key` docs-gate claim (honua-release#59) — is actually ABOUT
that pinned SHA, or is even recent. A candidate could be cut whose evidence chain is silently broken
at its very first link (plan §8 traceability). This gate makes that decidable and fail-able:

  lineage    the matrix's `server-matrix` producer sourceVersion sha must share commit history with
             the candidate's pinned honua-server sha (identical, an ancestor of it, or a descendant of
             it — i.e. NOT diverged onto an unrelated branch/fork). Ancestor-or-descendant (not just
             ancestor) is accepted deliberately: honua-evidence refreshes on its own cadence, so its
             observed sha is very often AHEAD of a manifest frozen earlier in time — that is normal
             lineage continuity, not a break. A true fork (diverged) means the evidence isn't actually
             about this release's history at all, which IS the failure this catches.
  freshness  each producer configured in certification/evidence-freshness.yaml must be no older than
             its maxAgeHours, per the matrix's own `freshness` block (fetchedAt/sourceVersion/ageDays/
             status — see honua-io/honua-evidence#8, "per-producer freshness/sourceVersion metadata
             documented as a stable contract for gate consumers").

Both the ancestor/descendant relationship (git history) and the matrix fetch are network/VCS
operations performed by the THIN WORKFLOW SHIM (gate-evidence.yml: raw.githubusercontent fetch + a
GitHub compare-API call) and handed in as plain values — this module makes no network calls itself,
so the decision core stays pure and unit-testable (same pattern as check_slo.py / check_upgrade.py).

A missing/unfetchable matrix, an unparseable/absent evidence sha, an undecidable lineage (compare
unreachable), or a producer entirely absent from the freshness block (most commonly because
honua-io/honua-evidence#8 hasn't landed that producer yet) -> BLOCKED, never a fake pass. A genuinely
DIVERGED lineage or a stale producer -> FAIL in both dry-run and real cuts (a gate that can't fail is
worse than no gate); only BLOCKED is tolerated in bootstrap mode.

  evaluate_evidence_freshness(candidate_sha, matrix, lineage_status, thresholds, now=None)
      -> (rows, overall)   # pure, unit-tested; overall in {pass, fail, blocked}
  python tools/check_evidence_freshness.py --manifest platform-manifest.yaml \
      --matrix path/to/capability-matrix.v1.json --lineage-status ancestor
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
THRESHOLDS_PATH = REPO_ROOT / "certification" / "evidence-freshness.yaml"

# Relations that mean "same commit history, not forked" — see module docstring on why descendant
# (evidence observed AFTER the candidate pin) is accepted alongside identical/ancestor.
LINEAGE_OK = {"identical", "ancestor", "descendant"}
SOURCE_VERSION_RE = re.compile(r"^(?P<sha>[0-9a-f]{6,40})@(?P<ts>.+)$")


def load_matrix(path: str | Path | None) -> dict | None:
    """Same fail-closed contract as check_capabilities.load_capability_matrix: never raises, returns
    None on anything missing/unreadable/malformed, or lacking a `freshness` block entirely."""
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("freshness"), dict):
        return None
    return data


def load_thresholds(path: Path = THRESHOLDS_PATH) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("producers") or {}


def _age_hours(entry: dict, now: datetime | None = None) -> float | None:
    now = now or datetime.now(timezone.utc)
    fetched = entry.get("fetchedAt")
    if not fetched:
        return None
    try:
        ts = datetime.fromisoformat(str(fetched).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - ts).total_seconds() / 3600.0


def _source_sha(entry: dict) -> str | None:
    sv = entry.get("sourceVersion")
    if not sv:
        return None
    m = SOURCE_VERSION_RE.match(str(sv))
    return m.group("sha") if m else None


def evaluate_evidence_freshness(candidate_sha: str | None, matrix: dict | None,
                                lineage_status: str | None, thresholds: dict,
                                now: datetime | None = None) -> tuple[list[dict], str]:
    rows: list[dict] = []

    if matrix is None:
        rows.append({"check": "matrix", "status": "blocked",
                     "why": "honua-evidence capability-matrix.v1.json unavailable/unfetchable — "
                            "cannot verify evidence lineage or freshness"})
        return rows, "blocked"

    freshness = matrix.get("freshness") or {}

    # --- lineage: the server-matrix producer's observed sha must share history with the candidate ---
    server_entry = freshness.get("server-matrix") or {}
    evidence_sha = _source_sha(server_entry)
    if not candidate_sha:
        rows.append({"check": "lineage", "status": "blocked",
                     "why": "no honua-server sha pinned in the candidate manifest"})
    elif not evidence_sha:
        rows.append({"check": "lineage", "status": "blocked",
                     "why": "server-matrix producer has no parseable sourceVersion sha in the evidence freshness block"})
    elif lineage_status is None:
        rows.append({"check": "lineage", "status": "blocked",
                     "why": f"could not determine ancestry between evidence sha {evidence_sha[:12]} "
                            f"and candidate sha {candidate_sha[:12]} (compare unreachable)"})
    elif lineage_status in LINEAGE_OK:
        rows.append({"check": "lineage", "status": "pass",
                     "why": f"evidence sha {evidence_sha[:12]} is {lineage_status} with candidate sha {candidate_sha[:12]}"})
    else:
        rows.append({"check": "lineage", "status": "fail",
                     "why": f"evidence sha {evidence_sha[:12]} has DIVERGED from candidate sha "
                            f"{candidate_sha[:12]} ({lineage_status}) — evidence is not about this "
                            f"release's history"})

    # --- freshness: each configured producer must be within its threshold -----------------------------
    for producer, cfg in (thresholds or {}).items():
        max_age = (cfg or {}).get("maxAgeHours")
        entry = freshness.get(producer)
        if entry is None:
            rows.append({"check": f"freshness:{producer}", "status": "blocked",
                         "why": f"producer {producer!r} not present in the evidence freshness metadata "
                                f"yet (see honua-io/honua-evidence#8)"})
            continue
        if entry.get("status") == "missing":
            rows.append({"check": f"freshness:{producer}", "status": "blocked",
                         "why": f"producer {producer!r} freshness status=missing: {entry.get('detail', 'no detail')}"})
            continue
        age = _age_hours(entry, now)
        if age is None:
            rows.append({"check": f"freshness:{producer}", "status": "blocked",
                         "why": f"producer {producer!r} has no parseable fetchedAt timestamp"})
            continue
        if max_age is not None and age > max_age:
            rows.append({"check": f"freshness:{producer}", "status": "fail",
                         "why": f"producer {producer!r} is {age:.1f}h old, exceeds threshold {max_age}h"})
        else:
            rows.append({"check": f"freshness:{producer}", "status": "pass",
                         "why": f"producer {producer!r} is {age:.1f}h old (threshold {max_age}h)"})

    if any(r["status"] == "fail" for r in rows):
        overall = "fail"
    elif any(r["status"] == "blocked" for r in rows):
        overall = "blocked"
    else:
        overall = "pass"
    return rows, overall


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", default=str(REPO_ROOT / "platform-manifest.yaml"))
    ap.add_argument("--matrix", default=None, help="path to a locally-fetched capability-matrix.v1.json")
    ap.add_argument("--lineage-status", default=None,
                    help="identical|ancestor|descendant|diverged (computed by the workflow shim); "
                         "omitted/empty = undecidable -> BLOCKED")
    ap.add_argument("--thresholds", default=str(THRESHOLDS_PATH))
    ap.add_argument("--require-real", action="store_true", help="promote BLOCKED to FAIL (real train cuts)")
    args = ap.parse_args(argv)

    manifest = yaml.safe_load(Path(args.manifest).read_text(encoding="utf-8")) or {}
    candidate_sha = ((manifest.get("components") or {}).get("honua-server") or {}).get("sha") or None
    matrix = load_matrix(args.matrix)
    thresholds = load_thresholds(Path(args.thresholds))
    lineage_status = args.lineage_status or None

    rows, overall = evaluate_evidence_freshness(candidate_sha, matrix, lineage_status, thresholds)
    print(f"== evidence lineage/freshness — {overall.upper()} ==")
    for r in rows:
        print(f"  [{r['status'].upper():7}] {r['check']}: {r['why']}")
    if overall == "fail":
        return 1
    if overall == "blocked" and args.require_real:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
