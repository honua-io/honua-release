#!/usr/bin/env python3
"""rc admission: every release-train gate has one green trunk run inside a rolling window.

Operator ruling 2026-10-22 (release#376 owner amendment): rc.N is admissible when every gate the
release train calls has at least one successful run on `trunk` within the last N days, against any
build — the gates do not have to agree on one pinned digest. Red and never-run gates block.

This tool reads GitHub Actions run history for each gate workflow and writes a JSON record plus a
Markdown table for the release notes. With --enforce it exits 1 when any gate lacks a green run in
the window, so the rc workflow fails closed; a gate that has never run is a failure, not a skip.

Usage:
  GH_TOKEN=... python3 tools/rc_gate_window.py --window-days 7 --out gate-window.json \
      --markdown gate-window.md --enforce
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"

# The gates release-train.yml calls (its `uses: ./.github/workflows/...` lines), plus the AI gates the
# operator added to the rc set on 2026-10-22. Keep this list in step with release-train.yml; the
# regression test pins it against the workflow file.
TRAIN_GATES: tuple[str, ...] = (
    "manifest-validate.yml",
    "gate-build-test.yml",
    "gate-artifact-consume.yml",
    "e2e-local-docker.yml",
    "e2e-cloud-aws.yml",
    "certification.yml",
    "gate-security.yml",
    "gate-contract.yml",
    "gate-contract-live.yml",
    "gate-sbom.yml",
    "gate-observability.yml",
    "gate-docs.yml",
    "gate-upgrade.yml",
    "rollback-certification.yml",
    "gate-evidence.yml",
    "gate-dr.yml",
    "capacity-soak.yml",
    "gate-executable-docs.yml",
    "terminal-journey-contract.yml",
    "gate-journey.yml",
    "gate-installed-clients.yml",
)
AI_GATES: tuple[str, ...] = (
    "gate-agent-e2e.yml",
    "terminal-model-canary.yml",
)
# Operator triage 2026-10-22 (release#376): these gates are deferred to GA. They are reported, never
# required, for rc.N. capacity-soak needs a producer bundle the nightly cannot yet mint; rollback-
# certification needs a signed lock (gate-upgrade's rollback leg covers rc); gate-journey needs cloud-cell
# receipts plus the data-driven roster; gate-agent-e2e has no campaign producer yet (#619).
GA_DEFERRED: tuple[str, ...] = (
    "capacity-soak.yml",
    "rollback-certification.yml",
    "gate-journey.yml",
    "gate-agent-e2e.yml",
)
RC_GATES: tuple[str, ...] = tuple(g for g in TRAIN_GATES + AI_GATES if g not in GA_DEFERRED)


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "honua-release rc_gate_window",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 (fixed https host)
        return json.load(resp)


def fetch_gate_runs(repo: str, workflow: str, token: str, branch: str = "trunk") -> dict:
    """Return {'latest_success': run|None, 'latest': run|None} for a workflow on a branch."""
    base = f"{API}/repos/{repo}/actions/workflows/{urllib.parse.quote(workflow)}/runs"
    out: dict[str, dict | None] = {"latest_success": None, "latest": None}
    for key, query in (("latest_success", {"branch": branch, "status": "success", "per_page": 1}),
                       ("latest", {"branch": branch, "per_page": 1})):
        try:
            data = _get(base + "?" + urllib.parse.urlencode(query), token)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # workflow file does not exist on the default branch
                out[key] = None
                continue
            raise
        runs = data.get("workflow_runs") or []
        out[key] = _slim(runs[0]) if runs else None
    return out


def _slim(run: dict) -> dict:
    return {
        "id": run.get("id"),
        "html_url": run.get("html_url"),
        "created_at": run.get("created_at"),
        "conclusion": run.get("conclusion"),
        "status": run.get("status"),
        "event": run.get("event"),
        "head_sha": (run.get("head_sha") or "")[:12],
    }


def evaluate(records: dict[str, dict], now: dt.datetime, window_days: float) -> dict:
    """Pure verdict over fetched records: {gate: {'latest_success':…, 'latest':…}}.

    A gate is green when its newest successful trunk run is inside the window. Never-run gates and
    gates whose only runs are failures are red. The aggregate is 'admissible' only when every gate
    is green — there is no advisory tier here; advisory gates simply are not in the list.
    """
    cutoff = now - dt.timedelta(days=window_days)
    gates = {}
    for name, rec in records.items():
        ok = rec.get("latest_success") or {}
        ok_at = _parse(ok.get("created_at")) if ok else None
        if ok_at is None:
            verdict, why = "red", "never green on trunk"
        elif ok_at < cutoff:
            verdict, why = "red", f"last green {ok_at.date()} is outside the {window_days:g}-day window"
        else:
            verdict, why = "green", f"green on trunk {ok_at.date()}"
        latest = rec.get("latest") or {}
        if verdict == "green" and latest and latest.get("conclusion") not in (None, "success"):
            why += f"; latest run {latest.get('conclusion')}"
        gates[name] = {
            "verdict": verdict,
            "reason": why,
            "last_green_at": ok.get("created_at") if ok else None,
            "last_green_run": ok.get("html_url") if ok else None,
            "last_green_build": ok.get("head_sha") if ok else None,
            "latest_run": latest.get("html_url") if latest else None,
            "latest_conclusion": latest.get("conclusion") if latest else None,
        }
    red = sorted(n for n, g in gates.items() if g["verdict"] != "green")
    return {
        "evaluated_at": now.replace(microsecond=0).isoformat(),
        "window_days": window_days,
        "admissible": not red,
        "red": red,
        "gates": gates,
    }


def _parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def render_markdown(result: dict) -> str:
    lines = [
        f"| Gate | Verdict | Last green on trunk | Build | Run |",
        f"|---|---|---|---|---|",
    ]
    for name in sorted(result["gates"]):
        g = result["gates"][name]
        when = (g["last_green_at"] or "never")[:10]
        run = f"[run]({g['last_green_run']})" if g["last_green_run"] else "—"
        build = f"`{g['last_green_build']}`" if g["last_green_build"] else "—"
        mark = "✅" if g["verdict"] == "green" else "❌"
        lines.append(f"| `{name}` | {mark} {g['reason']} | {when} | {build} | {run} |")
    head = (f"Window: {result['window_days']:g} days ending {result['evaluated_at']}. "
            f"Admissible: **{'yes' if result['admissible'] else 'no'}**"
            + (f" — red: {', '.join(result['red'])}" if result["red"] else "") + "\n\n")
    tail = ""
    if result.get("deferred_to_ga"):
        tail = ("\nDeferred to GA (reported, not required for rc): "
                + ", ".join(f"`{g}`" for g in result["deferred_to_ga"]) + "\n")
    return head + "\n".join(lines) + "\n" + tail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "honua-io/honua-release"))
    ap.add_argument("--branch", default="trunk")
    ap.add_argument("--window-days", type=float, default=7)
    ap.add_argument("--gate", action="append", default=None,
                    help="workflow file to evaluate (repeatable); default: train gates + AI gates")
    ap.add_argument("--without-ai-gates", action="store_true")
    ap.add_argument("--set", choices=("rc", "ga"), default="rc",
                    help="rc: train + AI gates minus the GA-deferred set (default); ga: every gate")
    ap.add_argument("--out", default="gate-window.json")
    ap.add_argument("--markdown", default=None)
    ap.add_argument("--enforce", action="store_true", help="exit 1 unless every gate is green")
    args = ap.parse_args(argv)
    if args.window_days <= 0:
        ap.error("--window-days must be positive")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GH_TOKEN is required (anonymous Actions API calls 404)", file=sys.stderr)
        return 2
    if args.gate:
        gates = tuple(args.gate)
    else:
        gates = TRAIN_GATES + (() if args.without_ai_gates else AI_GATES)
        if args.set == "rc":
            gates = tuple(g for g in gates if g not in GA_DEFERRED)
    records = {g: fetch_gate_runs(args.repo, g, token, args.branch) for g in gates}
    result = evaluate(records, dt.datetime.now(dt.timezone.utc), args.window_days)
    result["repo"], result["branch"], result["set"] = args.repo, args.branch, args.set
    result["deferred_to_ga"] = list(GA_DEFERRED) if args.set == "rc" and not args.gate else []
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
    md = render_markdown(result)
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(md)
    print(md)
    if args.enforce and not result["admissible"]:
        print(f"rc gate window: {len(result['red'])} gate(s) red: {', '.join(result['red'])}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
