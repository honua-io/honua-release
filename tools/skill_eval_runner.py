#!/usr/bin/env python3
"""Prove-or-delete paired skill evaluation runner and receipt aggregator."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import statistics
from typing import Callable

import skill_evals

DEFAULT_TIMEOUT_SECONDS = 600
TUNING_TIMEOUT_SECONDS = 900
DEFAULT_RUNS_PER_ARM = 3
DEFAULT_CUT_PERCENT = 20
DEFAULT_MAX_AGE_DAYS = 7
SCRUBBED_PATH = '/usr/local/bin:/usr/bin:/bin'
FORBIDDEN_ANCESTOR_MARKERS = ('.git', 'AGENTS.md', 'CLAUDE.md')

MODEL_SPECS: dict[str, dict[str, str]] = {
    'opus': {'cli': 'claude-cli', 'model': 'claude-opus-5-5', 'skillDir': '.claude/skills'},
    'sol': {'cli': 'codex-cli', 'model': 'gpt-6.1-sol', 'skillDir': '.agents/skills'},
}


@dataclass(frozen=True)
class SkillScenario:
    skill: str
    surface: str
    timeout_seconds: int
    prompt: str
    oracle_checks: tuple[str, ...]


SCENARIOS: dict[str, SkillScenario] = {
    'honua-exports': SkillScenario(
        skill='honua-exports',
        surface='serving:/mcp?view=exports',
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        prompt='Export seeded managed layer parcels as GeoParquet and PMTiles, pause for approval, verify signed-in and public readback, then delete and verify removal.',
        oracle_checks=('exports-create-used', 'approval-awaited', 'status-ready', 'public-readback', 'deleted'),
    ),
    'honua-install': SkillScenario(
        skill='honua-install',
        surface='quickstart-local',
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        prompt='Initialize and start the Honua quickstart package, verify image pins and readiness, confirm MCP discovery, and report pins without leaking secrets.',
        oracle_checks=('quickstart-started', 'zero-secret-leak', 'ready-200', 'mcp-discovery', 'pins-matched'),
    ),
    'honua-ops': SkillScenario(
        skill='honua-ops',
        surface='management:/mcp?view=operate',
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        prompt='Verify release co-versioning, inspect the active operational finding, propose remediation with subject.targetId, await approval, and verify completion.',
        oracle_checks=('dual-endpoint-used', 'coversion-checked', 'target-id-used', 'zero-secret-leak', 'succeeded'),
    ),
    'honua-services': SkillScenario(
        skill='honua-services',
        surface='serving:/mcp?view=configure',
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        prompt='Ingest the seeded GeoJSON dataset, publish as a managed PostGIS layer, await approval and completion, and verify exact count and paginated queries.',
        oracle_checks=('configure-view-used', 'single-publish', 'layer-listed', 'paginated-count-matched'),
    ),
    'honua-studio-maps': SkillScenario(
        skill='honua-studio-maps',
        surface='serving:/mcp?view=setup',
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        prompt='Compose a Studio map from the seeded catalog layer with inline style and extent, validate, preview, save an immutable version, and propose publication.',
        oracle_checks=('source-discovered', 'draft-validated', 'version-saved', 'proposal-matched'),
    ),
    'honua-tuning': SkillScenario(
        skill='honua-tuning',
        surface='quickstart-tuning',
        timeout_seconds=TUNING_TIMEOUT_SECONDS,
        prompt='Diagnose the layer publication materialization timeout, apply a bounded MaterializationTimeoutSeconds update via Compose config, verify readiness, and complete publish.',
        oracle_checks=('timeout-diagnosed', 'bounded-timeout-set', 'ready-200', 'no-reingest', 'published'),
    ),
}


@dataclass(frozen=True)
class TrialOutcome:
    model: str
    arm: str
    trial: int
    passed: bool
    seconds: float
    tool_calls: int
    infra_error: str = ''


@dataclass(frozen=True)
class CampaignRequest:
    server_checkout: Path
    skill: str
    server_sha: str
    evidence: str
    runs_per_arm: int = DEFAULT_RUNS_PER_ARM


def assert_isolated_workdir(workdir: Path) -> None:
    resolved = workdir.resolve()
    for parent in (resolved, *resolved.parents):
        for marker in FORBIDDEN_ANCESTOR_MARKERS:
            if (parent / marker).exists():
                raise ValueError(f'workdir leaks repository marker {marker} at {parent}')


def clean_child_env(home_dir: Path) -> dict[str, str]:
    return {
        'HOME': str(home_dir.resolve()),
        'PATH': SCRUBBED_PATH,
        'LC_ALL': 'C.UTF-8',
        'LANG': 'C.UTF-8',
    }


def _tree_bytes_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob('*') if p.is_file()):
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode('utf-8') + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def stage_skill_workspace(
    request: CampaignRequest, workdir: Path, model_key: str, arm: str,
) -> str:
    if model_key not in MODEL_SPECS:
        raise ValueError(f'unsupported model key: {model_key}')
    if arm not in ('withSkill', 'withoutSkill'):
        raise ValueError(f'unsupported arm: {arm}')
    assert_isolated_workdir(workdir)
    tree_sha = skill_evals.tree_digest(request.server_checkout, request.skill)
    skill_root = workdir / MODEL_SPECS[model_key]['skillDir'] / request.skill
    if skill_root.exists():
        shutil.rmtree(skill_root)
    if arm == 'withSkill':
        src = request.server_checkout / skill_evals.SKILLS_DIR / request.skill
        if not (src / 'SKILL.md').is_file():
            raise ValueError(f'missing skill source directory: {src}')
        shutil.copytree(src, skill_root)
        if _tree_bytes_hash(src) != _tree_bytes_hash(skill_root):
            raise ValueError('staged skill bytes diverged from candidate source')
    return tree_sha


def interleaved_schedule(runs_per_arm: int = DEFAULT_RUNS_PER_ARM) -> list[tuple[str, int, str]]:
    if runs_per_arm < skill_evals.MIN_RUNS_FLOOR:
        raise ValueError(f'runs_per_arm must be >= {skill_evals.MIN_RUNS_FLOOR}')
    schedule: list[tuple[str, int, str]] = []
    for model_key in ('opus', 'sol'):
        for trial in range(1, runs_per_arm + 1):
            schedule.append((model_key, trial, 'withoutSkill'))
            schedule.append((model_key, trial, 'withSkill'))
    return schedule


def aggregate_arm(outcomes: list[TrialOutcome]) -> dict:
    for item in outcomes:
        if item.infra_error:
            raise RuntimeError(f'infrastructure failure aborted campaign: {item.infra_error}')
    passed = [item for item in outcomes if item.passed]
    if not passed:
        return {
            'runs': len(outcomes),
            'successes': 0,
            'medianSeconds': None,
            'medianToolCalls': None,
        }
    return {
        'runs': len(outcomes),
        'successes': len(passed),
        'medianSeconds': round(float(statistics.median(item.seconds for item in passed)), 2),
        'medianToolCalls': round(float(statistics.median(item.tool_calls for item in passed)), 2),
    }


def model_regressed(with_arm: dict, without_arm: dict) -> bool:
    return with_arm['successes'] * without_arm['runs'] < without_arm['successes'] * with_arm['runs']


def build_eval_record(
    request: CampaignRequest, outcomes: list[TrialOutcome], now: dt.datetime | None = None,
) -> tuple[dict, str, str | None]:
    stamp = (now or dt.datetime.now(dt.timezone.utc)).replace(microsecond=0)
    models: dict[str, dict] = {}
    for model_key in ('opus', 'sol'):
        with_items = [o for o in outcomes if o.model == model_key and o.arm == 'withSkill']
        without_items = [o for o in outcomes if o.model == model_key and o.arm == 'withoutSkill']
        models[model_key] = {
            'withSkill': aggregate_arm(with_items),
            'withoutSkill': aggregate_arm(without_items),
        }
    tree_sha = skill_evals.tree_digest(request.server_checkout, request.skill)
    record = {
        'schema': skill_evals.RESULT_SCHEMA,
        'skill': request.skill,
        'skillTreeSha256': tree_sha,
        'serverSha': request.server_sha,
        'observedAt': stamp.isoformat().replace('+00:00', 'Z'),
        'evidence': request.evidence,
        'models': models,
    }
    policy = {
        'resultsDir': 'certification/skill-evals',
        'models': ['opus', 'sol'],
        'minRunsPerArm': request.runs_per_arm,
        'minCutPercent': DEFAULT_CUT_PERCENT,
        'maxAgeDays': DEFAULT_MAX_AGE_DAYS,
    }
    row = {'skill': request.skill, 'skillTreeSha256': tree_sha}
    context = skill_evals.SkillContext(policy, Path('.'), stamp)
    gap = skill_evals.result_gap(record, row, context)
    return record, ('KEEP' if gap is None else 'DELETE'), gap


def run_paired_campaign(
    request: CampaignRequest, executor: Callable[[str, int, str], TrialOutcome],
    now: dt.datetime | None = None,
) -> tuple[dict, str, str | None]:
    collected: list[TrialOutcome] = []
    for model_key in ('opus', 'sol'):
        model_outcomes: list[TrialOutcome] = []
        for trial in range(1, request.runs_per_arm + 1):
            for arm in ('withoutSkill', 'withSkill'):
                outcome = executor(model_key, trial, arm)
                if outcome.infra_error:
                    raise RuntimeError(f'campaign stopped on infra error: {outcome.infra_error}')
                model_outcomes.append(outcome)
                collected.append(outcome)
        with_arm = aggregate_arm([o for o in model_outcomes if o.arm == 'withSkill'])
        without_arm = aggregate_arm([o for o in model_outcomes if o.arm == 'withoutSkill'])
        if model_regressed(with_arm, without_arm):
            break
    return build_eval_record(request, collected, now=now)


def plan_campaign(server_checkout: Path, runs_per_arm: int = DEFAULT_RUNS_PER_ARM) -> dict:
    shipped = skill_evals.shipped_skills(server_checkout)
    items = []
    for name in shipped:
        scenario = SCENARIOS.get(name)
        items.append({
            'skill': name,
            'skillTreeSha256': skill_evals.tree_digest(server_checkout, name),
            'surface': scenario.surface if scenario else 'unregistered',
            'timeoutSeconds': scenario.timeout_seconds if scenario else DEFAULT_TIMEOUT_SECONDS,
            'plannedRuns': len(interleaved_schedule(runs_per_arm)),
        })
    return {'skills': items, 'totalPlannedRuns': sum(item['plannedRuns'] for item in items)}


def _load_trials(path: Path) -> list[TrialOutcome]:
    outcomes = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        outcomes.append(TrialOutcome(
            model=raw['model'], arm=raw['arm'], trial=int(raw['trial']),
            passed=bool(raw['passed']), seconds=float(raw['seconds']),
            tool_calls=int(raw['toolCalls']), infra_error=str(raw.get('infraError', '')),
        ))
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)
    p_plan = sub.add_parser('plan', help='Inspect shipped skills and interleaved trial counts')
    p_plan.add_argument('--server-checkout', type=Path, required=True)
    p_plan.add_argument('--runs-per-arm', type=int, default=DEFAULT_RUNS_PER_ARM)

    p_agg = sub.add_parser('aggregate', help='Aggregate trial receipts into honua.skill-eval/v1')
    p_agg.add_argument('--server-checkout', type=Path, required=True)
    p_agg.add_argument('--skill', required=True)
    p_agg.add_argument('--server-sha', required=True)
    p_agg.add_argument('--evidence', required=True)
    p_agg.add_argument('--trials', type=Path, required=True)
    p_agg.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.cmd == 'plan':
        print(json.dumps(plan_campaign(args.server_checkout, args.runs_per_arm), indent=2))
        return 0
    request = CampaignRequest(args.server_checkout, args.skill, args.server_sha, args.evidence)
    record, verdict, gap = build_eval_record(request, _load_trials(args.trials))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'skill': args.skill, 'verdict': verdict, 'gap': gap}))
    return 0 if verdict == 'KEEP' else 1


if __name__ == '__main__':
    raise SystemExit(main())
