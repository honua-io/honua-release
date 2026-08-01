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
import re
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
ROLLUP_PATH = REPO_ROOT / "certification" / "rollup-checks.yaml"
SECURITY_PATH = REPO_ROOT / "certification" / "security-checks.yaml"
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


def _load_check_names(path) -> dict[str, frozenset[str]]:
    """Shared loader: YAML keyed by component -> [{name, why}, ...] -> {component: frozenset(names)}.
    Missing file => empty map."""
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out: dict[str, frozenset[str]] = {}
    for comp, entries in data.items():
        names = {str((e or {}).get("name", "")).strip() for e in (entries or [])}
        out[comp] = frozenset(n for n in names if n)
    return out


def load_rollup(path=ROLLUP_PATH) -> dict[str, frozenset[str]]:
    """Load per-component AGGREGATE / roll-up check-run NAMES (see rollup-checks.yaml).

    These are pure `needs:`-based summary jobs (e.g. honua-server's "CI Gate" and "Test Suite
    Summary") that run NO tests of their own — they only mirror the collective result of the leaf
    shards. They therefore go RED whenever a leaf shard is starvation-CANCELLED, even though no test
    actually failed. The gate excludes them from the decisive verdict so a roll-up "follows its leaf
    shards": every genuine failure still surfaces on a leaf shard (which IS evaluated), so excluding a
    redundant aggregator can never mask a real red — while a leaf-cancellation artifact no longer
    manufactures a false fail. Missing file => empty map => original behaviour (roll-ups counted)."""
    return _load_check_names(path)


def load_security(path=SECURITY_PATH) -> dict[str, frozenset[str]]:
    """Load per-component DEPENDENCY-SECURITY check-run NAMES (see security-checks.yaml).

    These are supply-chain signals — Dependabot dependency updates, SCA/advisory scanners — that run
    NO build/compile/unit/integration of the component. Whether a dependency has an available update
    or an open advisory is the security gate's (gate_security) concern, not a statement about whether
    the pinned commit builds or its tests pass, so the build/test gate EXCLUDES them from the decisive
    verdict and decides on the component's real build/unit/integration lanes only. A genuine build/test
    red still surfaces on a core lane (which stays in the verdict), so excluding a security signal can
    never mask it. Missing file => empty map => original behaviour (security checks counted)."""
    return _load_check_names(path)


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


def _latest_named_runs(runs: list[dict]) -> list[dict]:
    """Keep the latest check-run attempt for each named app lane.

    GitHub retains scheduled runs and reruns on the same commit, so the check-runs endpoint can return
    several historical verdicts for one lane. Certification must use that lane's latest attempt: an
    old red must not poison a later green forever, and a newer red must not be hidden by an old green.
    Unnamed runs stay independent because there is no stable lane identity by which to supersede them.
    """
    unnamed: list[tuple[int, dict]] = []
    latest: dict[tuple[str, str, str], tuple[tuple[int, str, int], dict]] = {}
    for index, run in enumerate(runs):
        name = str(run.get("name", "")).strip()
        if not name:
            unnamed.append((index, run))
            continue
        app = run.get("app") or {}
        app_slug = str(app.get("slug", "")).strip() if isinstance(app, dict) else ""
        workflow_id = str(run.get("_workflow_id", "")).strip()
        suite = run.get("check_suite") or {}
        suite_id = str(suite.get("id", "")).strip() if isinstance(suite, dict) else ""
        # Workflow IDs distinguish independent Actions workflows that happen to use the same job
        # name. If enrichment was unavailable, keep each Actions check suite independent rather than
        # risk hiding one workflow behind another; this conservative fallback may retain stale runs,
        # but can never manufacture a pass.
        lane_scope = workflow_id or (f"suite:{suite_id}" if app_slug == "github-actions" else "")
        timestamp = str(run.get("started_at") or run.get("completed_at") or "")
        try:
            run_id = int(run.get("id") or 0)
        except (TypeError, ValueError):
            run_id = 0
        marker = (run_id, timestamp, index)
        key = (name, app_slug, lane_scope)
        if key not in latest or marker > latest[key][0]:
            latest[key] = (marker, run)

    selected = unnamed + [(marker[2], run) for marker, run in latest.values()]
    return [run for _, run in sorted(selected, key=lambda item: item[0])]


def classify(payload, env_gated_names: frozenset[str] = frozenset(),
             rollup_names: frozenset[str] = frozenset(),
             security_names: frozenset[str] = frozenset()) -> tuple[str, str]:
    """Map a component's check-runs payload to (status, why). Pure → unit-tested.

    `env_gated_names` are check-run names to treat as env-gated live/staging-integration lanes: they
    are excluded from the core verdict and, when non-green, reported as skipped (backend not
    provisioned). `rollup_names` are pure aggregate/summary check-runs (`needs:`-only jobs that run no
    tests of their own — e.g. "CI Gate", "Test Suite Summary"); they are ALSO excluded from the core
    verdict because they merely mirror the leaf shards and go red on a starvation-cancelled leaf.
    `security_names` are dependency-security check-runs (Dependabot updates, SCA/advisory scanners)
    that run no build/unit/integration of the component; they too are excluded from the core verdict
    because they are the security gate's concern (gate_security), not a build/test verdict. The
    pass/fail/blocked decision is made on the CORE (remaining) check-runs only, so a real compile/unit
    -test red still fails (it surfaces on a leaf shard that stays in core); env-gated, roll-up and
    security lanes can never mask it."""
    if payload == NOT_FOUND:
        return "blocked", "pinned sha or repo not resolvable (404 / no access)"
    if not isinstance(payload, dict):
        return "blocked", "no check-runs payload"
    runs = payload.get("check_runs") or []
    if not runs:
        return "blocked", "no CI check-runs for the pinned sha (not built yet?)"
    runs = _latest_named_runs(runs)

    excluded = env_gated_names | rollup_names | security_names
    core = [r for r in runs if str(r.get("name", "")) not in excluded]
    env = [r for r in runs if str(r.get("name", "")) in env_gated_names]
    # env-gated lanes that did not finish green — the ones we deliberately skip (env not provisioned).
    env_skipped = [r for r in env
                   if r.get("status") != "completed" or str(r.get("conclusion")) not in GREEN]
    env_note = (f"; {len(env_skipped)} env-gated integration lane(s) skipped "
                f"({sorted({str(r.get('name')) for r in env_skipped})}) — backend not provisioned"
                if env_skipped else "")
    # roll-up aggregators that went non-green (they follow their leaves; a red here without a red leaf
    # is a cancelled-leaf artifact, already reflected by the leaf's own flake/verdict in core).
    rollup_nongreen = [r for r in runs if str(r.get("name", "")) in rollup_names
                       and (r.get("status") != "completed" or str(r.get("conclusion")) not in GREEN)]
    rollup_note = (f"; {len(rollup_nongreen)} aggregate roll-up check(s) excluded "
                   f"({sorted({str(r.get('name')) for r in rollup_nongreen})}) — follows leaf shards"
                   if rollup_nongreen else "")
    # dependency-security checks that went non-green (excluded — owned by gate_security, not build/test).
    security_nongreen = [r for r in runs if str(r.get("name", "")) in security_names
                         and (r.get("status") != "completed" or str(r.get("conclusion")) not in GREEN)]
    security_note = (f"; {len(security_nongreen)} dependency-security check(s) excluded "
                     f"({sorted({str(r.get('name')) for r in security_nongreen})}) — owned by gate_security"
                     if security_nongreen else "")
    env_note = env_note + rollup_note + security_note

    if not core:
        # Only env-gated / roll-up / security lanes ran → no core build/test signal to certify on. Never pass.
        return "blocked", "only env-gated / roll-up / security check-runs present; no core build/test signal"

    # A CORE (build/unit) red is decisive and is evaluated FIRST — before "still in progress" — so a
    # real failure is never masked as blocked-in-progress just because sibling shards are still
    # running (that mid-run window is exactly when a live train read could otherwise tolerate a red).
    conclusions = [str(r.get("conclusion")) for r in core if r.get("status") == "completed"]
    reds = [c for c in conclusions if c in RED]
    if reds:
        return "fail", f"{len(reds)}/{len(core)} core check-run(s) red ({sorted(set(reds))}){env_note}"

    incomplete = [r for r in core if r.get("status") != "completed"]
    if incomplete:
        return "blocked", f"CI still in progress ({len(incomplete)}/{len(core)} core not completed){env_note}"
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


_ACTIONS_RUN_URL = re.compile(r"/actions/runs/(\d+)(?:/|$)")


def _enrich_action_workflow_ids(payload: dict, workflow_payload: dict) -> dict:
    """Attach stable workflow IDs to Actions check-runs so same-named jobs stay independent."""
    workflows = {
        int(run["id"]): str(run.get("workflow_id") or run.get("path") or "")
        for run in (workflow_payload.get("workflow_runs") or [])
        if run.get("id") is not None
    }
    for run in payload.get("check_runs") or []:
        app = run.get("app") or {}
        if not isinstance(app, dict) or app.get("slug") != "github-actions":
            continue
        match = _ACTIONS_RUN_URL.search(str(run.get("details_url") or ""))
        workflow_id = workflows.get(int(match.group(1))) if match else None
        if workflow_id:
            run["_workflow_id"] = workflow_id
    return payload


def _default_fetch(repo: str, sha: str) -> object:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    url = f"https://api.github.com/repos/{ORG}/{repo}/commits/{sha}/check-runs?per_page=100"
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "honua-release-gate",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (404, 403, 422):
            return NOT_FOUND
        raise
    except (urllib.error.URLError, OSError):
        return NOT_FOUND

    # One additional request identifies the Actions workflow behind each job. This lets the
    # classifier supersede attempts of one workflow without merging an unrelated workflow that uses
    # the same generic job name. If metadata cannot be read, the classifier conservatively keys
    # Actions runs by check-suite ID and retains every suite.
    actions_url = f"https://api.github.com/repos/{ORG}/{repo}/actions/runs?head_sha={sha}&per_page=100"
    try:
        with urllib.request.urlopen(urllib.request.Request(actions_url, headers=headers), timeout=20) as r:
            workflow_payload = json.loads(r.read().decode("utf-8", "replace"))
        return _enrich_action_workflow_ids(payload, workflow_payload)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return payload


def evaluate(manifest: dict, fetch: Fetcher, enforcement: str = "bootstrap",
             env_gated: dict[str, frozenset[str]] | None = None,
             rollup: dict[str, frozenset[str]] | None = None,
             security: dict[str, frozenset[str]] | None = None) -> dict:
    components = manifest.get("components") or {}
    env_gated = env_gated or {}
    rollup = rollup or {}
    security = security or {}
    rows = []
    for name, comp in components.items():
        comp = comp or {}
        sha = str(comp.get("sha", "")).strip()
        if not sha:
            rows.append({"component": name, "status": "blocked",
                         "why": "no sha pinned in manifest (cannot resolve CI)"})
            continue
        status, why = classify(fetch(name, sha), env_gated.get(name, frozenset()),
                               rollup.get(name, frozenset()), security.get(name, frozenset()))
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
    ap.add_argument("--rollup", default=str(ROLLUP_PATH),
                    help="YAML of per-component aggregate/roll-up check-run names (excluded from the verdict)")
    ap.add_argument("--security", default=str(SECURITY_PATH),
                    help="YAML of per-component dependency-security check-run names (excluded from the verdict)")
    ap.add_argument("--out", default=str(REPO_ROOT / "certification" / "gate-report-build-test.json"))
    args = ap.parse_args(argv)

    manifest = yaml.safe_load(Path(args.manifest).read_text(encoding="utf-8")) or {}
    env_gated = load_env_gated(args.env_gated)
    rollup = load_rollup(args.rollup)
    security = load_security(args.security)
    report = evaluate(manifest, _default_fetch, args.enforcement, env_gated, rollup, security)
    Path(args.out).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"== build-test (per-repo CI on pinned SHAs) — {report['overallStatus'].upper()} "
          f"(enforcement={args.enforcement}) ==")
    for r in report["components"]:
        print(f"  [{r['decided'].upper():7}] {r['component']}: {r['why']}")
    print(f"  summary: {report['summary']}")

    return 1 if report["overallStatus"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
