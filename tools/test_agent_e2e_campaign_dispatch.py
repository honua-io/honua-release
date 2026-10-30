"""Offline workflow boundaries; these receipts are never live scenario evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/agent-e2e-campaign.yml'
JOBS = yaml.safe_load(WORKFLOW.read_text())['jobs']
TARGETS = {'docker': 'local-docker', 'ecs': 'aws-ecs-redis-on', 'eks': 'aws-eks',
           'lambda': 'aws-lambda', 'batch': 'aws-batch'}


def step(job, name):
    return next(value for value in JOBS[job]['steps'] if name in value.get('name', ''))


def test_development_dispatch_defaults_to_one_local_docker_case_and_sonnet_46():
    text = WORKFLOW.read_text()
    assert 'default: P10:A:local-docker' in text
    assert "vars.TERMINAL_MODEL_NAME || 'us.anthropic.claude-sonnet-4-6'" in text
    assert 'us.anthropic.claude-sonnet-5-5' not in text


@pytest.mark.parametrize('job', ['develop', *TARGETS, 'finalize'])
def test_only_fixed_private_handoff_runs_controller_operations(job):
    steps = JOBS[job]['steps']
    credentials = next(value for value in steps if 'configure-aws-credentials@' in value.get('uses', ''))
    assert credentials['with']['role-to-assume'] == '${{ vars.AGENT_E2E_DISPATCH_ROLE_ARN }}'
    runs = '\n'.join(value.get('run', '') for value in steps)
    assert 'agent_e2e_campaign.py execute' not in runs
    assert 'agent_e2e_campaign.py finalize' not in runs
    assert 'controller_handoff.py' in runs
    assert '--configuration certification/agent-e2e/controller-handoff.json' in runs
    assert '--bundle campaign --operation ' in runs
    assert 'umask 077' in runs
    assert '--output "$RUNNER_TEMP/agent-e2e-private/' in runs
    assert '--evidence-directory "$RUNNER_TEMP/agent-e2e-private/' in runs
    assert 'continue-on-error' not in json.dumps(steps)
    assert 'get-secret-value' not in runs and 'psycopg' not in runs
    handoff = next(i for i, value in enumerate(steps) if 'Require reviewed private' in value.get('name', ''))
    authentication = steps.index(credentials)
    assert handoff < authentication
    for value in steps:
        if 'upload-artifact@' in value.get('uses', '') and job != 'finalize':
            assert value['with']['path'].endswith('/outcome.json')
            assert '/evidence' not in value['with']['path']


@pytest.mark.parametrize('kind', ['missing', 'untracked', 'symlink', 'tracked'])
def test_read_only_source_preflight_refuses_absent_unreviewed_handoff(tmp_path, kind):
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    paths = ['tools/hosted_pilot/controller_handoff.py', 'certification/agent-e2e/controller-handoff.json']
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if kind != 'missing':
            path.write_text('offline source-contract fixture\n')
    if kind == 'symlink':
        helper = tmp_path / paths[0]
        helper.unlink()
        helper.symlink_to(tmp_path / paths[1])
    if kind in ('tracked', 'symlink'):
        subprocess.run(['git', '-C', str(tmp_path), 'add', '--', *paths], check=True)
    before = subprocess.check_output(['git', '-C', str(tmp_path), 'status', '--porcelain'])
    script = step('freeze', 'Require reviewed private')['run']
    result = subprocess.run(['bash', '-c', script], cwd=tmp_path, capture_output=True)
    assert (result.returncode == 0) is (kind == 'tracked')
    assert subprocess.check_output(['git', '-C', str(tmp_path), 'status', '--porcelain']) == before
    assert 'aws ' not in script and 'python' not in script


def run_dispatch(tmp_path, job, failure):
    """Exercise the exact checked-in shell using a recording CLI, without AWS."""
    executable = tmp_path / 'bin' / 'python3'
    executable.parent.mkdir()
    executable.write_text('''#!/usr/bin/python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['CALLS'], 'a') as stream:
    stream.write(json.dumps(args)+'\\n')
out = pathlib.Path(args[args.index('--output')+1])
out.write_text(json.dumps({'status': 'offline-test-only', 'exitCode': int(os.environ['FAILURE'])}))
if '--shard' not in args or args[args.index('--shard')+1] == '2':
    sys.exit(int(os.environ['FAILURE']))
''')
    executable.chmod(0o700)
    calls = tmp_path / 'calls.jsonl'
    environment = dict(os.environ, PATH=str(executable.parent)+':'+os.environ['PATH'],
                       RUNNER_TEMP=str(tmp_path), CALLS=str(calls), FAILURE=str(failure),
                       DEVELOPMENT_CASE='P10:A:aws-ecs-redis-on')
    script = step(job, 'Validate all real' if job == 'finalize' else 'Execute ')['run']
    result = subprocess.run(['bash', '-c', script], cwd=tmp_path, env=environment, capture_output=True)
    return result, [json.loads(line) for line in calls.read_text().splitlines()]


@pytest.mark.parametrize('job', ['develop', 'finalize'])
@pytest.mark.parametrize('failure', [0, 37])
def test_private_native_exit_is_propagated_without_success_substitution(tmp_path, job, failure):
    result, calls = run_dispatch(tmp_path, job, failure)
    assert result.returncode == failure
    assert len(calls) == 1
    assert calls[0][calls[0].index('--operation')+1] == ('finalize' if job == 'finalize' else 'execute')
    if job == 'develop':
        assert calls[0][calls[0].index('--case-id')+1] == 'P10:A:aws-ecs-redis-on'
    else:
        steps = JOBS[job]['steps']
        attest = next(value for value in steps if 'attest-build-provenance@' in value.get('uses', ''))
        assert 'if' not in attest  # Default success() blocks attestation on native failure.


@pytest.mark.parametrize('job,target', TARGETS.items())
@pytest.mark.parametrize('failure', [0, 37])
def test_each_original_target_runs_six_serial_shards_and_stops_at_first_failure(tmp_path, job, target, failure):
    result, calls = run_dispatch(tmp_path, job, failure)
    assert result.returncode == failure
    assert [args[args.index('--shard')+1] for args in calls] == list(map(str, range(3 if failure else 6)))
    assert all(args[args.index('--target')+1] == target for args in calls)
    assert all(args[args.index('--shards')+1] == '6' for args in calls)
    assert all(args[args.index('--operation')+1] == 'execute' for args in calls)
