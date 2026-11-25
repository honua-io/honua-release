"""Offline unit tests for the prove-or-delete skill evaluation runner."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import subprocess

import pytest

import skill_eval_runner as runner

SHA = 'a' * 40
NOW = dt.datetime(2026, 11, 25, 6, 0, tzinfo=dt.timezone.utc)
EVIDENCE = 'https://github.com/honua-io/honua-release/releases/download/rc2/honua-ops-eval.tar.gz'


def _commit_server(repo: Path, skills: tuple[str, ...] = ('honua-ops', 'honua-exports')) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    git = ['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@example.test']
    subprocess.run([*git, 'init', '-q'], check=True)
    for name in skills:
        skill_dir = repo / 'skills' / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / 'SKILL.md').write_text(f'# {name}\n', encoding='utf-8')
    subprocess.run([*git, 'add', '-A'], check=True)
    subprocess.run([*git, 'commit', '-q', '-m', 'seed skills'], check=True)


def test_plan_lists_all_shipped_skills_and_interleaved_run_counts(tmp_path):
    repo = tmp_path / 'server'
    _commit_server(repo, tuple(sorted(runner.SCENARIOS)))
    planned = runner.plan_campaign(repo)
    assert len(planned['skills']) == len(runner.SCENARIOS)
    assert planned['totalPlannedRuns'] == 72
    assert all(item['skillTreeSha256'].startswith('sha256:') for item in planned['skills'])


def test_staging_enforces_isolation_and_exact_skill_bytes(tmp_path):
    repo = tmp_path / 'server'
    _commit_server(repo, ('honua-ops',))
    isolated = tmp_path / 'scratch' / 'work'
    isolated.mkdir(parents=True)
    req = runner.CampaignRequest(repo, 'honua-ops', SHA, EVIDENCE)

    digest = runner.stage_skill_workspace(req, isolated, 'opus', 'withSkill')
    assert digest.startswith('sha256:')
    assert (isolated / '.claude/skills/honua-ops/SKILL.md').is_file()

    runner.stage_skill_workspace(req, isolated, 'opus', 'withoutSkill')
    assert not (isolated / '.claude/skills/honua-ops').exists()

    (tmp_path / 'scratch' / 'AGENTS.md').write_text('leak', encoding='utf-8')
    with pytest.raises(ValueError, match='AGENTS.md'):
        runner.stage_skill_workspace(req, isolated, 'sol', 'withSkill')


def test_campaign_passes_keep_rule_on_time_cut_and_rejects_regression_early(tmp_path):
    repo = tmp_path / 'server'
    _commit_server(repo, ('honua-ops',))
    req = runner.CampaignRequest(repo, 'honua-ops', SHA, EVIDENCE)

    def faster_with_skill(model: str, trial: int, arm: str) -> runner.TrialOutcome:
        seconds = 180.0 if (model == 'opus' and arm == 'withSkill') else 300.0
        return runner.TrialOutcome(model, arm, trial, True, seconds, 20)

    record, verdict, gap = runner.run_paired_campaign(req, faster_with_skill, now=NOW)
    assert verdict == 'KEEP'
    assert gap is None
    assert record['models']['opus']['withSkill']['medianSeconds'] == 180.0

    calls: list[tuple[str, int, str]] = []

    def regressing_opus(model: str, trial: int, arm: str) -> runner.TrialOutcome:
        calls.append((model, trial, arm))
        passed = False if (model == 'opus' and arm == 'withSkill') else True
        return runner.TrialOutcome(model, arm, trial, passed, 200.0, 15)

    _, verdict_bad, gap_bad = runner.run_paired_campaign(req, regressing_opus, now=NOW)
    assert verdict_bad == 'DELETE'
    assert gap_bad is not None and 'opus' in gap_bad
    assert all(model == 'opus' for model, _, _ in calls)


def test_campaign_aborts_immediately_on_infrastructure_error(tmp_path):
    repo = tmp_path / 'server'
    _commit_server(repo, ('honua-ops',))
    req = runner.CampaignRequest(repo, 'honua-ops', SHA, EVIDENCE)

    def broken(model: str, trial: int, arm: str) -> runner.TrialOutcome:
        return runner.TrialOutcome(model, arm, trial, False, 1.0, 0, infra_error='docker down')

    with pytest.raises(RuntimeError, match='docker down'):
        runner.run_paired_campaign(req, broken, now=NOW)


def test_cli_aggregate_writes_valid_record_and_exit_code(tmp_path):
    repo = tmp_path / 'server'
    _commit_server(repo, ('honua-ops',))
    trials_file = tmp_path / 'trials.jsonl'
    rows = []
    for model in ('opus', 'sol'):
        for trial in (1, 2, 3):
            rows.append({'model': model, 'arm': 'withoutSkill', 'trial': trial,
                         'passed': True, 'seconds': 100.0, 'toolCalls': 20})
            rows.append({'model': model, 'arm': 'withSkill', 'trial': trial,
                         'passed': True, 'seconds': 70.0, 'toolCalls': 14})
    trials_file.write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
    out_file = tmp_path / 'honua-ops.json'
    code = runner.main([
        'aggregate', '--server-checkout', str(repo), '--skill', 'honua-ops',
        '--server-sha', SHA, '--evidence', EVIDENCE, '--trials', str(trials_file),
        '--out', str(out_file),
    ])
    assert code == 0
    written = json.loads(out_file.read_text(encoding='utf-8'))
    assert written['schema'] == 'honua.skill-eval/v1'
    assert written['models']['sol']['withSkill']['medianToolCalls'] == 14.0
