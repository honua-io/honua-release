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

The PRIOR release is resolved by prior_platform_release(), the single definition of "the previous
platform release" shared with tools/release_rollback_target.py (ruling R26, honua-release#376): a
published, non-draft, non-pre-release `honua-*` GitHub Release carrying the signed platform lock that
promote.yml publishes. Engineering snapshots published as pre-releases (the 2026-08-20 `honua-2026.1`)
are never an upgrade or rollback baseline; with no qualifying release the gate takes the self-limiting
first-release basis.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Callable, Iterable
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))
import semver  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]


# What promote.yml publishes for every platform release: the lock bytes plus their Sigstore bundle.
SIGNED_LOCK_ASSETS = ("platform-lock.json", "platform-lock.sigstore.json")


class ReleaseLookupError(RuntimeError):
    """The release list could not be read; never treated as "no prior release exists"."""


def is_platform_release(release: dict) -> bool:
    """True for a promoted `honua-*` platform release: published, not a pre-release, signed lock attached."""
    if release.get("draft") or release.get("prerelease"):
        return False
    if not str(release.get("tag_name", "")).startswith("honua-"):
        return False
    names = {asset.get("name") for asset in release.get("assets") or []}
    return all(name in names for name in SIGNED_LOCK_ASSETS)


def prior_platform_release(releases: Iterable[dict],
                           eligible: Callable[[dict], bool] = lambda release: True) -> dict | None:
    """Return the newest (by published_at) eligible platform release, or None for the first release."""
    ordered = sorted((r for r in releases if eligible(r)), key=lambda r: r.get("published_at") or "", reverse=True)
    return next((release for release in ordered if is_platform_release(release)), None)


def _gh(*args: str) -> str:
    result = subprocess.run(["gh", *args], text=True, capture_output=True, check=False)
    if result.returncode:
        raise ReleaseLookupError(result.stderr or result.stdout)
    return result.stdout


def list_releases(repository: str, command: Callable[..., str] | None = None) -> list[dict]:
    """Every GitHub Release of `repository` (all pages; a lock on page 2 still counts)."""
    pages = json.loads((command or _gh)("api", "--paginate", "--slurp", f"repos/{repository}/releases?per_page=100"))
    return [release for page in pages for release in page]


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
    ap.add_argument("--prior-manifest", help="the previous platform release's manifest")
    ap.add_argument("--resolve-prior-release", metavar="OWNER/REPO",
                    help="print the prior platform release tag (empty line on the first release) and exit; "
                         "exit 2 when the release list cannot be read")
    ap.add_argument("--candidate-manifest", default=str(REPO_ROOT / "platform-manifest.yaml"))
    ap.add_argument("--candidate-matrix", default=str(REPO_ROOT / "compatibility-matrix.yaml"))
    ap.add_argument("--require-real", action="store_true")
    args = ap.parse_args(argv)

    if args.resolve_prior_release:
        try:
            release = prior_platform_release(list_releases(args.resolve_prior_release))
        except (ReleaseLookupError, ValueError, KeyError, TypeError) as exc:
            print(f"PRIOR_RELEASE_LOOKUP_FAILED: {exc}", file=sys.stderr)
            return 2
        print(release["tag_name"] if release else "")
        return 0
    if not args.prior_manifest:
        ap.error("--prior-manifest is required unless --resolve-prior-release is given")

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
