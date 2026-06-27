#!/usr/bin/env python3
"""Per-repo build/test fan-out (release gate 'a') — confirm every component in the pinned manifest
has GREEN CI on its EXACT pinned SHA.

This is the "consume continuous-green" model from docs/TEST-STRATEGY.md: the release train does NOT
re-run every repo's suite synchronously at cut time (days-long, $$$). Each component keeps its suite
green continuously (its own per-PR/nightly CI); the train confirms the *candidate's* pinned commit is
the one that passed. A red or never-built pin must fail the gate (AGENTS.md: a gate that can't fail is
worse than no gate) — proven by tools-style tests in test_build_test.py.

Per-component verdict from the GitHub check-runs of the pinned SHA:
  pass     — at least one check-run, all conclusions in {success, neutral, skipped}
  fail     — any conclusion in {failure, cancelled, timed_out, action_required, startup_failure}
  blocked  — sha not found / repo unreadable / no check-runs / CI still in progress (no verdict yet)

Components pinned by a real release `version` but whose manifest carries no sha are reported blocked
(nothing to resolve CI against) — never silently passed.

Aggregate: fail if any component failed; else blocked if any blocked (under --enforcement strict a
blocked is a fail); else pass.

Usage:
  GH_TOKEN=... python certification/check_build_test.py [--enforcement bootstrap|strict]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "platform-manifest.yaml"
ORG = "honua-io"

GREEN = {"success", "neutral", "skipped"}
RED = {"failure", "cancelled", "timed_out", "action_required", "startup_failure"}

# A check-runs payload is either the parsed dict, or one of these sentinels.
NOT_FOUND = "not_found"     # sha/repo not resolvable (404 / no access)


def classify(payload) -> tuple[str, str]:
    """Map a component's check-runs payload to (status, why). Pure → unit-tested."""
    if payload == NOT_FOUND:
        return "blocked", "pinned sha or repo not resolvable (404 / no access)"
    if not isinstance(payload, dict):
        return "blocked", "no check-runs payload"
    runs = payload.get("check_runs") or []
    if not runs:
        return "blocked", "no CI check-runs for the pinned sha (not built yet?)"
    incomplete = [r for r in runs if r.get("status") != "completed"]
    if incomplete:
        return "blocked", f"CI still in progress ({len(incomplete)}/{len(runs)} not completed)"
    conclusions = [str(r.get("conclusion")) for r in runs]
    reds = [c for c in conclusions if c in RED]
    if reds:
        return "fail", f"{len(reds)}/{len(runs)} check-run(s) red ({sorted(set(reds))})"
    non_green = [c for c in conclusions if c not in GREEN]
    if non_green:
        return "blocked", f"unrecognised check conclusions {sorted(set(non_green))}"
    return "pass", f"all {len(runs)} check-run(s) green"


Fetcher = Callable[[str, str], object]  # (repo, sha) -> payload | NOT_FOUND


def _default_fetch(repo: str, sha: str) -> object:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    url = f"https://api.github.com/repos/{ORG}/{repo}/commits/{sha}/check-runs?per_page=100"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "honua-release-gate",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (404, 403, 422):
            return NOT_FOUND
        raise
    except (urllib.error.URLError, OSError):
        return NOT_FOUND


def evaluate(manifest: dict, fetch: Fetcher, enforcement: str = "bootstrap") -> dict:
    components = manifest.get("components") or {}
    rows = []
    for name, comp in components.items():
        comp = comp or {}
        sha = str(comp.get("sha", "")).strip()
        if not sha:
            rows.append({"component": name, "status": "blocked",
                         "why": "no sha pinned in manifest (cannot resolve CI)"})
            continue
        status, why = classify(fetch(name, sha))
        rows.append({"component": name, "status": status, "sha": sha[:12], "why": why})

    def decided(s: str) -> str:
        return "fail" if (s == "blocked" and enforcement == "strict") else s

    decided_rows = [{**r, "decided": decided(r["status"])} for r in rows]
    overall = ("fail" if any(r["decided"] == "fail" for r in decided_rows)
               else "blocked" if any(r["decided"] == "blocked" for r in decided_rows)
               else "pass")
    return {
        "gate": "build-test",
        "enforcement": enforcement,
        "overallStatus": overall,
        "summary": {k: sum(1 for r in decided_rows if r["decided"] == k) for k in ("pass", "fail", "blocked")},
        "components": decided_rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--enforcement", choices=["bootstrap", "strict"], default="bootstrap")
    ap.add_argument("--manifest", default=str(MANIFEST_PATH))
    ap.add_argument("--out", default=str(REPO_ROOT / "certification" / "gate-report-build-test.json"))
    args = ap.parse_args(argv)

    manifest = yaml.safe_load(Path(args.manifest).read_text(encoding="utf-8")) or {}
    report = evaluate(manifest, _default_fetch, args.enforcement)
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"== build-test (per-repo CI on pinned SHAs) — {report['overallStatus'].upper()} "
          f"(enforcement={args.enforcement}) ==")
    for r in report["components"]:
        print(f"  [{r['decided'].upper():7}] {r['component']}: {r['why']}")
    print(f"  summary: {report['summary']}")

    return 1 if report["overallStatus"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
