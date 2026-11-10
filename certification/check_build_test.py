#!/usr/bin/env python3
"""Per-repo build/test fan-out (release gate 'a') — confirm every component in the pinned manifest
has GREEN CI on its EXACT pinned SHA.

This is the "consume continuous-green" model from docs/TEST-STRATEGY.md: the release train does NOT
re-run every repo's suite synchronously at cut time (days-long, $$$). Each component keeps its suite
green continuously (its own per-PR/nightly CI); the train confirms the *candidate's* pinned commit is
the one that passed. A red or never-built pin must fail the gate (AGENTS.md: a gate that can't fail is
worse than no gate) — proven by tools-style tests in test_build_test.py.

Per-component verdict from the GitHub check-runs of the pinned SHA:
  pass     — every required build/test lane (required-checks.yaml, or the component's full-matrix policy)
             succeeded on its latest attempt, and every other core check-run is in {success, neutral, skipped}
  fail     — any core conclusion in {failure, timed_out, action_required, startup_failure}
  blocked  — sha not found / repo unreadable / no check-runs / CI still in progress / a cancelled core lane /
             a required lane missing, skipped or otherwise not successful (no verdict yet)

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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NamedTuple

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

from build_test_github import NOT_FOUND, Fetcher, github_fetcher, live_validated_examples
from build_test_policy import BuildTestPolicy, ComponentPolicy, PolicyFiles
from required_lanes import classify_required_lanes

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "platform-manifest.yaml"

GREEN = {"success", "neutral", "skipped"}
RED = {"failure", "timed_out", "action_required", "startup_failure"}
# A cancelled check-run carries NO pass/fail verdict — it is a re-runnable flake (runner starvation,
# matrix fail-fast siblings, manual cancel). It must NOT be treated as a build/test FAILURE (cancelled
# != failed), but it is also not a green pass. So it decides `blocked` (re-runnable), tolerated under
# bootstrap and surfaced under strict. A real failing sibling (matrix fail-fast) is itself `failure`
# and is still caught, so downgrading cancelled cannot mask a genuine red.
FLAKE = {"cancelled"}


# Release-gate replays (release-session ruling 2026-11-03, rc.2 redo). honua-release's Studio customer gate
# (tools/studio_customer_gate.py) dispatches honua-console's console-live-journey.yml with suite
# studio-customer and a `release-<uuid hex>` dispatch id from a fresh `studio-customer-gate/<id>` tag, and
# judges that run itself. GitHub files the run's checks on the Console commit, so without this they would
# count twice in build-test. Only that exact replay shape is dropped, and BEFORE latest-run selection, so a
# replay can neither hide nor supersede the commit's own schedule, push or pull-request runs of the same
# job. Missing enrichment keeps the check (core).
# Scoped by component: the Studio gate dispatches and judges only honua-io/honua-console.
_RELEASE_GATE_REPLAYS = {
    "honua-console": ((".github/workflows/console-live-journey.yml",
                       re.compile(r"Console live studio-customer (release-[0-9a-f]{32})")),),
}


def _is_release_gate_replay(run: dict, component: str = "") -> bool:
    actions = run.get("_workflow_run")
    if not isinstance(actions, dict) or actions.get("event") != "workflow_dispatch":
        return False
    for path, title in _RELEASE_GATE_REPLAYS.get(component, ()):
        match = title.fullmatch(actions.get("title", ""))
        if (run.get("_workflow_path") == path and match
                and actions.get("branch") == f"studio-customer-gate/{match.group(1)}"):
            return True
    return False


def _validated_examples_replaced(run: dict, payload: object, component: str) -> bool:
    """A pin check-run of honua-server's validated-examples workflow that a verified descendant run replaces.

    Only when validated_examples_descendant.resolve verified the descendant run (attached by evaluate as
    payload["_validated_examples"]), only for honua-server, and only for the workflow's three scenario
    jobs identified by enriched workflow path AND name. Missing enrichment keeps the check (core)."""
    import validated_examples_descendant as vx
    record = payload.get("_validated_examples") if isinstance(payload, dict) else None
    return (component == vx.COMPONENT and isinstance(record, dict) and record.get("status") == "verified"
            and run.get("_workflow_path") == vx.WORKFLOW_PATH and str(run.get("name", "")) in vx.SCENARIOS)


def _validated_examples_note(payload: object, replaced: int) -> str:
    record = payload.get("_validated_examples") if isinstance(payload, dict) else None
    if not isinstance(record, dict):
        return ""
    if record.get("status") != "verified":
        return f"; validated-examples descendant not used ({record.get('reason')})"
    return (f"; {replaced} validated-examples check(s) on the pin replaced by verified descendant run "
            f"{record.get('runId')} at {str(record.get('runSha'))[:12]} (paths {record.get('changedPaths')}, "
            f"image {record.get('imageDigest')}, ruling {record.get('ruling')})")


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


class RunPartition(NamedTuple):
    """A payload's check-runs split by the rules applied before any verdict."""
    selected: list[dict]       # latest attempt of each lane that the verdict judges
    replaced: list[dict]       # validated-examples checks replaced by a verified descendant run
    replayed: list[dict]       # release-gate replays, judged by their own gate
    event_gated: list[dict]    # env-gated (workflow, event) runs


def _partition_runs(payload: object, policy: ComponentPolicy) -> RunPartition:
    runs = (payload.get("check_runs") or []) if isinstance(payload, dict) else []

    def env_gated_event(run: dict) -> bool:
        actions = run.get("_workflow_run")
        return (isinstance(actions, dict)
                and (str(run.get("_workflow_path") or ""), str(actions.get("event") or "")) in policy.env_gated_events)

    replaced = [r for r in runs if _validated_examples_replaced(r, payload, policy.component)]
    runs = [r for r in runs if not _validated_examples_replaced(r, payload, policy.component)]
    replayed = [r for r in runs if _is_release_gate_replay(r, policy.component)]
    runs = [r for r in runs if not _is_release_gate_replay(r, policy.component)]
    event_gated = [r for r in runs if env_gated_event(r)]
    selected = _latest_named_runs([r for r in runs if not env_gated_event(r)])
    return RunPartition(selected, replaced, replayed, event_gated)


class Exclusion(NamedTuple):
    """A category of check-runs kept out of the core verdict, and how a non-green one is reported."""
    applies: Callable[[ComponentPolicy, dict], bool]
    kind: str
    disposition: str
    reason: str


def _run_name(run: dict) -> str:
    return str(run.get("name", ""))


EXCLUSIONS = (
    Exclusion(lambda policy, run: _run_name(run) in policy.env_gated_names,
              "env-gated integration lane(s)", "skipped", "backend not provisioned"),
    Exclusion(lambda policy, run: _run_name(run) in policy.rollup_names,
              "aggregate roll-up check(s)", "excluded", "follows leaf shards"),
    Exclusion(lambda policy, run: _run_name(run) in policy.security_names,
              "dependency-security check(s)", "excluded", "owned by gate_security"),
    Exclusion(lambda policy, run: _run_name(run) in policy.governance_names,
              "non-build governance check(s)", "excluded", "no build/test work"),
    Exclusion(ComponentPolicy.is_advisory, "advisory check(s)", "excluded", "not a verdict on this sha"),
)


def _is_excluded(policy: ComponentPolicy, run: dict) -> bool:
    return any(exclusion.applies(policy, run) for exclusion in EXCLUSIONS)


def _is_green(run: dict) -> bool:
    return run.get("status") == "completed" and str(run.get("conclusion")) in GREEN


def _exclusion_notes(runs: list[dict], policy: ComponentPolicy) -> str:
    """Audit notes for excluded runs that did not finish green; a green excluded run needs no note."""
    notes = []
    for exclusion in EXCLUSIONS:
        non_green = [r for r in runs if exclusion.applies(policy, r) and not _is_green(r)]
        if non_green:
            notes.append(f"; {len(non_green)} {exclusion.kind} {exclusion.disposition} "
                         f"({sorted({str(r.get('name')) for r in non_green})}) — {exclusion.reason}")
    return "".join(notes)


def _partition_notes(partition: RunPartition, payload: object) -> str:
    notes = (f"; {len(partition.replayed)} release-gate replay check(s) excluded (judged by their own gate)"
             if partition.replayed else "")
    if partition.event_gated:
        non_green = [r for r in partition.event_gated if r.get("conclusion") not in GREEN]
        lanes = sorted({f"{r.get('name')} [{r['_workflow_run'].get('event')}]" for r in non_green})
        notes += (f"; {len(partition.event_gated)} env-gated scheduled check(s) skipped"
                  + (f" ({len(non_green)} not green: {lanes})" if non_green else "")
                  + " — external floating backend, see env-gated-checks.yaml")
    return notes + _validated_examples_note(payload, len(partition.replaced))


def classify(payload: object, policy: ComponentPolicy = ComponentPolicy()) -> tuple[str, str]:
    """Map a component's check-runs payload to (status, why). Pure, so unit-tested.

    The decision is made on the CORE check-runs only: every run that no EXCLUSIONS category of `policy`
    names. A real compile/unit-test red surfaces on a lane that stays core, so an excluded lane can never
    mask it; excluded runs that did not finish green are reported in `why` for audit."""
    if payload == NOT_FOUND:
        return "blocked", "pinned sha or repo not resolvable (404 / no access)"
    if not isinstance(payload, dict):
        return "blocked", "no check-runs payload"
    if not payload.get("check_runs"):
        return "blocked", "no CI check-runs for the pinned sha (not built yet?)"
    partition = _partition_runs(payload, policy)
    if not partition.selected:
        if partition.replayed or partition.event_gated:
            return "blocked", "only release-gate replay or env-gated scheduled check-runs on the pinned sha"
        return "blocked", "only replaced validated-examples check-runs on the pinned sha"
    core = [r for r in partition.selected if not _is_excluded(policy, r)]
    if not core:
        return "blocked", ("only env-gated / roll-up / security / governance / advisory "
                           "check-runs present; no core build/test signal")
    return _core_verdict(core, _exclusion_notes(partition.selected, policy) + _partition_notes(partition, payload))


def _core_verdict(core: list[dict], notes: str) -> tuple[str, str]:
    # A CORE (build/unit) red is decisive and is evaluated FIRST — before "still in progress" — so a
    # real failure is never masked as blocked-in-progress just because sibling shards are still
    # running (that mid-run window is exactly when a live train read could otherwise tolerate a red).
    completed = [r for r in core if r.get("status") == "completed"]
    conclusions = [str(r.get("conclusion")) for r in completed]
    reds = [c for c in conclusions if c in RED]
    if reds:
        red_names = sorted({str(r.get("name") or "(unnamed)") for r in completed
                            if str(r.get("conclusion")) in RED})
        return "fail", f"{len(reds)}/{len(core)} core check-run(s) red ({sorted(set(reds))}: {red_names}){notes}"
    incomplete = [r for r in core if r.get("status") != "completed"]
    if incomplete:
        return "blocked", f"CI still in progress ({len(incomplete)}/{len(core)} core not completed){notes}"
    flakes = [c for c in conclusions if c in FLAKE]
    if flakes:
        return "blocked", (f"{len(flakes)}/{len(core)} core check-run(s) cancelled "
                           f"(runner-starvation flake; re-runnable, not a failure){notes}")
    non_green = [c for c in conclusions if c not in GREEN]
    if non_green:
        return "blocked", f"unrecognised core check conclusions {sorted(set(non_green))}"
    return "pass", f"all {len(core)} core check-run(s) green{notes}"


def classify_full_matrix(payload: object, policy: dict, expected_sha: str | None = None) -> tuple[str, str]:
    """Require authentic Actions jobs and the complete immutable shard selection/execution."""
    import full_matrix_evidence as matrix
    if not isinstance(payload, dict):
        return "fail", "missing full-matrix evidence: no Actions payload"
    try:
        selected = matrix.selected_run(payload, policy, expected_sha)
    except ValueError as error:
        return "fail", str(error)
    problems = matrix.verify(payload, selected, policy, expected_sha)
    if selected.get("conclusion") != "success":
        problems.append("workflow conclusion=" + str(selected.get("conclusion") or "none"))
    if problems:
        return "fail", f"full-matrix run {selected.get('id')} is not certifiable: " + "; ".join(problems)
    return "pass", (f"full-matrix run {selected['id']} attempt {selected['run_attempt']} completed successfully "
                    "with authentic expected lanes and complete pinned shard execution")


def _proof_of_execution(payload: object, policy: ComponentPolicy, sha: str) -> tuple[str, str]:
    """A component proves its builds and tests ran through its full-matrix policy, or else its required lanes."""
    if policy.full_matrix is not None:
        return classify_full_matrix(payload, policy.full_matrix, sha)
    return classify_required_lanes(_partition_runs(payload, policy).selected, policy.required_lanes)


_SEVERITY = {"pass": 0, "blocked": 1, "fail": 2}


def _worst(verdict: tuple[str, str], requirement: tuple[str, str]) -> tuple[str, str]:
    """The more severe of two (status, why) verdicts, keeping both explanations."""
    status = max(verdict[0], requirement[0], key=_SEVERITY.__getitem__)
    return status, f"{verdict[1]}; {requirement[1]}"


ValidatedExamplesResolver = Callable[[str, str, dict, dict], dict]


@dataclass(frozen=True)
class BuildTestGate:
    """`validated_examples(name, sha, component, manifest)` resolves the honua-server validated-examples
    descendant rule (validated_examples_descendant.py); None disables it, so the pin's runs decide."""
    fetch: Fetcher
    policy: BuildTestPolicy
    validated_examples: ValidatedExamplesResolver | None = None

    def evaluate(self, manifest: dict, enforcement: str = "bootstrap") -> dict:
        rows = [self._component_row(name, component or {}, manifest)
                for name, component in (manifest.get("components") or {}).items()]
        return _report(rows, enforcement)

    def _component_row(self, name: str, component: dict, manifest: dict) -> dict:
        import validated_examples_descendant as vx
        sha = str(component.get("sha", "")).strip()
        if not sha:
            return {"component": name, "status": "blocked", "why": "no sha pinned in manifest (cannot resolve CI)"}
        payload = self.fetch(name, sha)
        record = None
        if self.validated_examples is not None and name == vx.COMPONENT and isinstance(payload, dict):
            record = payload["_validated_examples"] = self.validated_examples(name, sha, component, manifest)
        policy = self.policy.component(name)
        status, why = _worst(classify(payload, policy), _proof_of_execution(payload, policy, sha))
        row = {"component": name, "status": status, "sha": sha[:12], "why": why}
        if record is not None:
            row["validatedExamples"] = record
        return row


def _report(rows: list[dict], enforcement: str) -> dict:
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


def _policy_files(args: argparse.Namespace) -> PolicyFiles:
    return PolicyFiles(env_gated=Path(args.env_gated), rollup=Path(args.rollup), security=Path(args.security),
                       governance=Path(args.governance), advisory=Path(args.advisory),
                       full_matrix=Path(args.full_matrix), required=Path(args.required))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    files = PolicyFiles()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--enforcement", choices=["bootstrap", "strict"], default="bootstrap")
    ap.add_argument("--manifest", default=str(MANIFEST_PATH))
    ap.add_argument("--env-gated", default=str(files.env_gated),
                    help="YAML of per-component env-gated integration check-run names and workflow events")
    ap.add_argument("--rollup", default=str(files.rollup),
                    help="YAML of per-component aggregate/roll-up check-run names (excluded from the verdict)")
    ap.add_argument("--security", default=str(files.security),
                    help="YAML of per-component dependency-security check-run names (excluded from the verdict)")
    ap.add_argument("--governance", default=str(files.governance),
                    help="YAML of per-component non-build governance check-run names")
    ap.add_argument("--full-matrix", default=str(files.full_matrix),
                    help="YAML of components requiring a completed full-matrix Actions run")
    ap.add_argument("--advisory", default=str(files.advisory),
                    help="YAML of per-component advisory check names and workflow paths")
    ap.add_argument("--required", default=str(files.required),
                    help="YAML of per-component required build/test lanes (workflow path -> job names)")
    ap.add_argument("--out", default=str(REPO_ROOT / "artifacts" / "gate-report-build-test.json"))
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    manifest = yaml.safe_load(Path(args.manifest).read_text(encoding="utf-8")) or {}
    policy = BuildTestPolicy.load(_policy_files(args))
    gate = BuildTestGate(github_fetcher(policy), policy, validated_examples=live_validated_examples)
    report = gate.evaluate(manifest, args.enforcement)
    report_path = Path(args.out)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    print(f"== build-test (per-repo CI on pinned SHAs) — {report['overallStatus'].upper()} "
          f"(enforcement={args.enforcement}) ==")
    for r in report["components"]:
        print(f"  [{r['decided'].upper():7}] {r['component']}: {r['why']}")
    print(f"  summary: {report['summary']}")

    return 1 if report["overallStatus"] == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
