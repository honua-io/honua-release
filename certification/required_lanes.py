"""The build and test lanes a component's pinned commit must have run and passed (required-checks.yaml).

A lane is an Actions workflow path plus a job (check-run) name. Only its latest attempt counts, and only a
`success` satisfies it.
"""
from __future__ import annotations

from pathlib import Path

import yaml

REQUIRED_PATH = Path(__file__).resolve().parent / "required-checks.yaml"


def load_required_lanes(path=REQUIRED_PATH) -> dict[str, frozenset[tuple[str, str]]]:
    """Per-component required (workflow path, job name) lanes from required-checks.yaml.

    A malformed entry raises instead of being skipped, so a typo cannot quietly drop a requirement."""
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{p.name}: expected a mapping of component to workflow lanes")
    return {str(component): _required_component_lanes(component, workflows)
            for component, workflows in data.items()}


def _required_component_lanes(component: object, workflows: object) -> frozenset[tuple[str, str]]:
    if not isinstance(workflows, dict) or not workflows:
        raise ValueError(f"required lanes for {component}: expected a non-empty mapping of workflow path to jobs")
    lanes = set()
    for workflow, jobs in workflows.items():
        if not (isinstance(workflow, str) and workflow.startswith(".github/workflows/")):
            raise ValueError(f"required lanes for {component}: {workflow!r} is not a .github/workflows/ path")
        if (not isinstance(jobs, list) or not jobs
                or any(not isinstance(job, str) or not job.strip() for job in jobs)):
            raise ValueError(f"required lanes for {component}: {workflow} needs a non-empty list of job names")
        lanes |= {(workflow, job.strip()) for job in jobs}
    return frozenset(lanes)


def classify_required_lanes(runs: list[dict], lanes: frozenset[tuple[str, str]]) -> tuple[str, str]:
    """Every required (workflow path, job name) lane's latest attempt in `runs` completed with success.

    A missing lane, or one whose latest attempt was skipped, neutral, cancelled or is still running, has not
    proved the build or tests ran, so the component is blocked. A red lane is already a core failure."""
    if not lanes:
        return "blocked", "no required build/test lanes declared (certification/required-checks.yaml)"
    found: dict[tuple[str, str], list[dict]] = {}
    for run in runs:
        found.setdefault((str(run.get("_workflow_path") or ""), str(run.get("name") or "")), []).append(run)
    missing = sorted(f"{name} [{workflow}]" for workflow, name in lanes if (workflow, name) not in found)
    unproven = sorted(f"{name}={_lane_outcome(found[(workflow, name)])}" for workflow, name in lanes
                      if (workflow, name) in found and _lane_outcome(found[(workflow, name)]) != "success")
    if missing or unproven:
        return "blocked", ("required build/test lane(s) not proven: "
                           + "; ".join(([f"missing {missing}"] if missing else [])
                                       + ([f"not successful {unproven}"] if unproven else [])))
    return "pass", f"all {len(lanes)} required build/test lane(s) succeeded"


def _lane_outcome(attempts: list[dict]) -> str:
    if len(attempts) != 1:
        return "ambiguous"
    run = attempts[0]
    return str(run.get("conclusion")) if run.get("status") == "completed" else str(run.get("status"))
