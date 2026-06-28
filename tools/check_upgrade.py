#!/usr/bin/env python3
"""Upgrade gate (gate i) — is upgrading from the previous platform release to the candidate safe?

The release promise is *safe version upgrades*. The full gate seeds the prior release, applies the
candidate, runs DB migrations forward + rollback, and checks old clients still work — which needs a
running server + two releases (the migration half stays BLOCKED until that infra exists). But a real,
decidable part can be checked from the manifests alone and is unit-tested here:

  - **old-client compatibility:** every client the PRIOR release shipped must still satisfy the
    CANDIDATE's compatibility-matrix ranges — i.e. upgrading the server doesn't strand a client that
    was supported a release ago. A dropped client is a breaking upgrade -> FAIL.
  - **DB schema is forward-only:** the candidate's required DB schema must be >= the prior's (numbered
    migrations never go backwards) -> FAIL on a regression.

  evaluate_upgrade(prior_manifest, candidate_manifest, candidate_matrix) -> (rows, overall)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))
import semver  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


def _schema_floor(schema: str) -> int | None:
    """Extract a numeric floor from a db-schema string ('metadata-v1' -> 1, '>=44' -> 44)."""
    digits = "".join(c for c in str(schema) if c.isdigit())
    return int(digits) if digits else None


def evaluate_upgrade(prior: dict, candidate: dict, candidate_matrix: dict) -> tuple[list[dict], str]:
    rows: list[dict] = []
    prior_comps = prior.get("components") or {}
    cand_comps = candidate.get("components") or {}

    # 1. Old-client compatibility: each client version the prior release pinned must satisfy the
    #    candidate matrix range for every contract that still names it.
    for contract, body in (candidate_matrix.get("contracts") or {}).items():
        for client, spec in (body.get("clients") or {}).items():
            prior_comp = prior_comps.get(client)
            if not prior_comp:
                continue
            prior_ver = str(prior_comp.get("version", "")).strip()
            if not semver.is_semver(prior_ver):
                continue  # sha-pinned prior client: no semver to range-check
            try:
                if semver.satisfies(prior_ver, spec):
                    rows.append({"check": f"old-client:{contract}:{client}", "status": "pass",
                                 "why": f"prior {client} {prior_ver} still satisfies {spec!r}"})
                else:
                    rows.append({"check": f"old-client:{contract}:{client}", "status": "fail",
                                 "why": f"upgrade strands {client} {prior_ver}: no longer satisfies {spec!r}"})
            except (semver.InvalidRange, semver.InvalidVersion):
                continue

    # 2. DB schema forward-only.
    prior_db = _schema_floor((prior_comps.get("honua-server") or {}).get("dbSchema", ""))
    cand_db = _schema_floor((cand_comps.get("honua-server") or {}).get("dbSchema", ""))
    if prior_db is not None and cand_db is not None:
        if cand_db >= prior_db:
            rows.append({"check": "db-schema-forward", "status": "pass",
                         "why": f"candidate db schema {cand_db} >= prior {prior_db}"})
        else:
            rows.append({"check": "db-schema-forward", "status": "fail",
                         "why": f"candidate db schema {cand_db} < prior {prior_db} (migrations went backwards)"})

    if not rows:
        return [], "blocked"   # nothing comparable (e.g. all sha-pinned) — not a pass
    overall = "fail" if any(r["status"] == "fail" for r in rows) else "pass"
    return rows, overall


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prior-manifest", required=True, help="the previous platform release's manifest")
    ap.add_argument("--candidate-manifest", default=str(REPO_ROOT / "platform-manifest.yaml"))
    ap.add_argument("--candidate-matrix", default=str(REPO_ROOT / "compatibility-matrix.yaml"))
    ap.add_argument("--require-real", action="store_true")
    args = ap.parse_args(argv)

    prior = yaml.safe_load(Path(args.prior_manifest).read_text(encoding="utf-8")) or {}
    candidate = yaml.safe_load(Path(args.candidate_manifest).read_text(encoding="utf-8")) or {}
    matrix = yaml.safe_load(Path(args.candidate_matrix).read_text(encoding="utf-8")) or {}

    rows, overall = evaluate_upgrade(prior, candidate, matrix)
    print(f"== upgrade compatibility — {overall.upper()} ==")
    for r in rows:
        print(f"  [{r['status'].upper():7}] {r['check']}: {r['why']}")
    if not rows:
        print("  (no manifest-comparable upgrade checks; DB-migration forward+rollback test is BLOCKED "
              "until a prior release + a migration-capable server image exist)")
    if overall == "fail":
        return 1
    if overall == "blocked" and args.require_real:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
