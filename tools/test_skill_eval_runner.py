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
STAMP = '2026-11-25T05:00:00Z'
OLDER_STAMP = '2026-11-24T05:00:00Z'
STALE_STAMP = '2026-10-01T00:00:00Z'
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


def test_staging_exports_committed_head_bytes_and_enforces_isolation(tmp_path):
    repo = tmp_path / 'server'
    _commit_server(repo, ('honua-ops',))
    (repo / 'skills/honua-ops/SKILL.md').write_text('# DIRTY WORKING TREE\n', encoding='utf-8')
    (repo / 'skills/honua-ops/untracked.md').write_text('extra', encoding='utf-8')
    isolated = tmp_path / 'scratch' / 'work'
    isolated.mkdir(parents=True)
    req = runner.CampaignRequest(repo, 'honua-ops', SHA, EVIDENCE)

    digest = runner.stage_skill_workspace(req, runner.StageTarget(isolated, 'opus', 'withSkill'))
    assert digest.startswith('sha256:')
    staged = isolated / '.claude/skills/honua-ops'
    assert (staged / 'SKILL.md').read_text(encoding='utf-8') == '# honua-ops\n'
    assert not (staged / 'untracked.md').exists()

    runner.stage_skill_workspace(req, runner.StageTarget(isolated, 'opus', 'withoutSkill'))
    assert not staged.exists()

    (tmp_path / 'scratch' / 'AGENTS.md').write_text('leak', encoding='utf-8')
    with pytest.raises(ValueError, match='repository marker'):
        runner.stage_skill_workspace(req, runner.StageTarget(isolated, 'sol', 'withSkill'))


def test_campaign_passes_keep_rule_and_rejects_regression_early(tmp_path):
    repo = tmp_path / 'server'
    _commit_server(repo, ('honua-ops',))
    req = runner.CampaignRequest(repo, 'honua-ops', SHA, EVIDENCE)

    def faster_with_skill(model: str, trial: int, arm: str) -> runner.TrialOutcome:
        seconds = 180.0 if (model == 'opus' and arm == 'withSkill') else 300.0
        stamp = OLDER_STAMP if (model == 'opus' and trial == 1 and arm == 'withoutSkill') else STAMP
        return runner.TrialOutcome(model, arm, trial, True, seconds, 20, stamp)

    record, verdict, gap = runner.run_paired_campaign(req, faster_with_skill, now=NOW)
    assert verdict == 'KEEP'
    assert gap is None
    assert record['observedAt'] == OLDER_STAMP
    assert record['models']['opus']['withSkill']['medianSeconds'] == 180.0

    calls: list[tuple[str, int, str]] = []

    def regressing_opus(model: str, trial: int, arm: str) -> runner.TrialOutcome:
        calls.append((model, trial, arm))
        passed = False if (model == 'opus' and arm == 'withSkill') else True
        return runner.TrialOutcome(model, arm, trial, passed, 200.0, 15, STAMP)

    _, verdict_bad, gap_bad = runner.run_paired_campaign(req, regressing_opus, now=NOW)
    assert verdict_bad == 'DELETE'
    assert gap_bad is not None and 'opus' in gap_bad
    assert all(model == 'opus' for model, _, _ in calls)


def test_rejects_duplicate_trial_ids_unpaired_arms_and_stale_observations(tmp_path):
    repo = tmp_path / 'server'
    _commit_server(repo, ('honua-ops',))
    req = runner.CampaignRequest(repo, 'honua-ops', SHA, EVIDENCE)

    dup = [
        runner.TrialOutcome('opus', 'withSkill', 1, True, 100.0, 10, STAMP),
        runner.TrialOutcome('opus', 'withSkill', 1, True, 100.0, 10, STAMP),
    ]
    with pytest.raises(ValueError, match='duplicate trial'):
        runner.build_eval_record(req, dup, now=NOW)

    unpaired = [
        runner.TrialOutcome('opus', 'withSkill', 1, True, 100.0, 10, STAMP),
        runner.TrialOutcome('opus', 'withoutSkill', 2, True, 200.0, 20, STAMP),
    ]
    with pytest.raises(ValueError, match='unpaired trial'):
        runner.build_eval_record(req, unpaired, now=NOW)

    stale_rows = [
        runner.TrialOutcome(m, arm, t, True, 70.0 if arm == 'withSkill' else 100.0, 10, STALE_STAMP)
        for m in ('opus', 'sol') for t in (1, 2, 3) for arm in ('withoutSkill', 'withSkill')
    ]
    record, verdict, gap = runner.build_eval_record(req, stale_rows, now=NOW)
    assert record['observedAt'] == STALE_STAMP
    assert verdict == 'DELETE'
    assert gap is not None and 'observedAt' in gap


def test_campaign_aborts_immediately_on_infrastructure_error(tmp_path):
    repo = tmp_path / 'server'
    _commit_server(repo, ('honua-ops',))
    req = runner.CampaignRequest(repo, 'honua-ops', SHA, EVIDENCE)

    def broken(model: str, trial: int, arm: str) -> runner.TrialOutcome:
        return runner.TrialOutcome(model, arm, trial, False, 1.0, 0, STAMP, infra_error='docker down')

    with pytest.raises(RuntimeError, match='docker down'):
        runner.run_paired_campaign(req, broken, now=NOW)


def test_cli_aggregate_writes_valid_record_and_exit_code(tmp_path):
    repo = tmp_path / 'server'
    _commit_server(repo, ('honua-ops',))
    trials_file = tmp_path / 'trials.jsonl'
    fresh = (dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(minutes=5))
    fresh_stamp = fresh.isoformat().replace('+00:00', 'Z')
    rows = []
    for model in ('opus', 'sol'):
        for trial in (1, 2, 3):
            rows.append({'model': model, 'arm': 'withoutSkill', 'trial': trial,
                         'passed': True, 'seconds': 100.0, 'toolCalls': 20, 'observedAt': fresh_stamp})
            rows.append({'model': model, 'arm': 'withSkill', 'trial': trial,
                         'passed': True, 'seconds': 70.0, 'toolCalls': 14, 'observedAt': fresh_stamp})
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
    assert written['observedAt'] == fresh_stamp
    assert written['models']['sol']['withSkill']['medianToolCalls'] == 14.0
