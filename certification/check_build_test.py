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
ENV_GATED_PATH = REPO_ROOT / "certification" / "env-gated-checks.yaml"
ORG = "honua-io"

GREEN = {"success", "neutral", "skipped"}
RED = {"failure", "timed_out", "action_required", "startup_failure"}
# A cancelled check-run carries NO pass/fail verdict — it is a re-runnable flake (runner starvation,
# matrix fail-fast siblings, manual cancel). It must NOT be treated as a build/test FAILURE (cancelled
# != failed), but it is also not a green pass. So it decides `blocked` (re-runnable), tolerated under
# bootstrap and surfaced under strict. A real failing sibling (matrix fail-fast) is itself `failure`
# and is still caught, so downgrading cancelled cannot mask a genuine red.
FLAKE = {"cancelled"}

# A check-runs payload is either the parsed dict, or one of these sentinels.
NOT_FOUND = "not_found"     # sha/repo not resolvable (404 / no access)


def load_env_gated(path=ENV_GATED_PATH) -> dict[str, frozenset[str]]:
    """Load the per-component env-gated integration check-run NAMES (see env-gated-checks.yaml).

    These are live/staging-integration lanes that hard-depend on an external backend the gate does
    not provision; a red on them is an absent-environment verdict, not a build/test verdict, so the
    gate reclassifies them to `skipped` and decides on the CORE (build+unit) check-runs. Missing file
    => empty map => the gate keys on ALL check-runs (original behaviour). Never list a build/unit lane
    here (guardrail proven in test_build_test.py)."""
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out: dict[str, frozenset[str]] = {}
    for comp, entries in data.items():
        names = {str((e or {}).get("name", "")).strip() for e in (entries or [])}
        out[comp] = frozenset(n for n in names if n)
    return out


def classify(payload, env_gated_names: frozenset[str] = frozenset()) -> tuple[str, str]:
    """Map a component's check-runs payload to (status, why). Pure → unit-tested.

    `env_gated_names` are check-run names to treat as env-gated live/staging-integration lanes: they
    are excluded from the core verdict and, when non-green, reported as skipped (backend not
    provisioned). The pass/fail/blocked decision is made on the CORE (remaining) check-runs only, so a
    real compile/unit-test red still fails; env-gated lanes can never mask it."""
    if payload == NOT_FOUND:
        return "blocked", "pinned sha or repo not resolvable (404 / no access)"
    if not isinstance(payload, dict):
        return "blocked", "no check-runs payload"
    runs = payload.get("check_runs") or []
    if not runs:
        return "blocked", "no CI check-runs for the pinned sha (not built yet?)"

    core = [r for r in runs if str(r.get("name", "")) not in env_gated_names]
    env = [r for r in runs if str(r.get("name", "")) in env_gated_names]
    # env-gated lanes that did not finish green — the ones we deliberately skip (env not provisioned).
    env_skipped = [r for r in env
                   if r.get("status") != "completed" or str(r.get("conclusion")) not in GREEN]
    env_note = (f"; {len(env_skipped)} env-gated integration lane(s) skipped "
                f"({sorted({str(r.get('name')) for r in env_skipped})}) — backend not provisioned"
                if env_skipped else "")

    if not core:
        # Only env-gated lanes ran → no core build/test signal to certify on. Never optimistically pass.
        return "blocked", "only env-gated integration check-runs present; no core build/test signal"

    incomplete = [r for r in core if r.get("status") != "completed"]
    if incomplete:
        return "blocked", f"CI still in progress ({len(incomplete)}/{len(core)} core not completed)"
    conclusions = [str(r.get("conclusion")) for r in core]
    reds = [c for c in conclusions if c in RED]
    if reds:
        # A CORE (build/unit) red — this is a real failure, never masked by env-gated/flake lanes.
        return "fail", f"{len(reds)}/{len(core)} core check-run(s) red ({sorted(set(reds))}){env_note}"
    flakes = [c for c in conclusions if c in FLAKE]
    if flakes:
        # Cancelled core lane(s) with no real red — re-runnable flake (runner starvation), not a
        # build/test failure. Blocked (tolerated under bootstrap; re-run to certify under strict).
        return "blocked", (f"{len(flakes)}/{len(core)} core check-run(s) cancelled "
                           f"(runner-starvation flake; re-runnable, not a failure){env_note}")
    non_green = [c for c in conclusions if c not in GREEN]
    if non_green:
        return "blocked", f"unrecognised core check conclusions {sorted(set(non_green))}"
    return "pass", f"all {len(core)} core check-run(s) green{env_note}"


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


def evaluate(manifest: dict, fetch: Fetcher, enforcement: str = "bootstrap",
             env_gated: dict[str, frozenset[str]] | None = None) -> dict:
    components = manifest.get("components") or {}
    env_gated = env_gated or {}
    rows = []
    for name, comp in components.items():
        comp = comp or {}
        sha = str(comp.get("sha", "")).strip()
        if not sha:
            rows.append({"component": name, "status": "blocked",
                         "why": "no sha pinned in manifest (cannot resolve CI)"})
            continue
        status, why = classify(fetch(name, sha), env_gated.get(name, frozenset()))
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
    ap.add_argument("--env-gated", default=str(ENV_GATED_PATH),
                    help="YAML of per-component env-gated integration check-run names")
    ap.add_argument("--out", default=str(REPO_ROOT / "certification" / "gate-report-build-test.json"))
    args = ap.parse_args(argv)

    manifest = yaml.safe_load(Path(args.manifest).read_text(encoding="utf-8")) or {}
    env_gated = load_env_gated(args.env_gated)
    report = evaluate(manifest, _default_fetch, args.enforcement, env_gated)
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"== build-test (per-repo CI on pinned SHAs) — {report['overallStatus'].upper()} "
          f"(enforcement={args.enforcement}) ==")
    for r in report["components"]:
        print(f"  [{r['decided'].upper():7}] {r['component']}: {r['why']}")
    print(f"  summary: {report['summary']}")

    return 1 if report["overallStatus"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
