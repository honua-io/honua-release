"""Release dispatch cannot provision Redis; orphan recovery remains bounded."""
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.safe_load((ROOT / '.github/workflows' / name).read_text())


def test_release_dispatches_only_redis_off():
    cloud = workflow('e2e-cloud-aws.yml')
    triggers = cloud.get('on', cloud.get(True))
    assert triggers['workflow_dispatch']['inputs']['redis_mode']['options'] == ['off']
    assert triggers['workflow_call']['inputs']['redis_mode']['default'] == 'off'
    assert cloud['jobs']['parity']['strategy']['matrix']['redis'] == ['off']


@pytest.mark.parametrize('mode', ['', 'off', 'on', 'both', 'invalid'])
def test_redis_input_is_rejected_before_any_cloud_step(tmp_path, mode):
    steps = workflow('e2e-cloud-aws.yml')['jobs']['candidate']['steps']
    guard = next(step for step in steps if step.get('name') == 'Require Redis disabled')
    assert steps.index(guard) == 0
    result = subprocess.run(['bash', '-c', guard['run']], env={**os.environ, 'REDIS_MODE': mode},
                            cwd=tmp_path, capture_output=True, text=True)
    assert (result.returncode == 0) == (mode in ('', 'off'))


@pytest.mark.parametrize('scope', ['', 'gha-42665961293-aws-ecs-redis-on', 'invalid', 'gha-1-aws-ecs-redis-on extra'])
def test_scoped_reaper_preserves_limits_and_forwards_exact_cell(tmp_path, scope):
    job = workflow('release-cell-reaper.yml')['jobs']['reap']
    sweep = next(step for step in job['steps'] if step.get('name') == 'Sweep orphaned release cells')
    helper = tmp_path / '_honua-iac/infrastructure/terraform/validation/scripts/aws/sweep-orphaned-validation-infra.sh'
    helper.parent.mkdir(parents=True)
    helper.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > called\n')
    helper.chmod(0o755)
    env = {**os.environ, 'VALIDATION_RUN_ID': scope, 'DRY_RUN': 'true', 'GRACE_HOURS': '48',
           'MAX_DELETE': '400', 'GITHUB_REPOSITORY': 'honua-io/honua-release',
           'GITHUB_STEP_SUMMARY': str(tmp_path / 'summary')}
    result = subprocess.run(['bash', '-c', sweep['run']], cwd=tmp_path, env=env,
                            capture_output=True, text=True)
    valid = not scope or scope == 'gha-42665961293-aws-ecs-redis-on'
    assert (result.returncode == 0) == valid
    called = tmp_path / 'called'
    assert called.exists() == valid
    if valid:
        args = called.read_text().splitlines()
        assert args[args.index('--max-delete') + 1] == '400'
        assert '--dry-run' in args and '--this-run' not in args
        assert ('--run-id' in args) == bool(scope)
        if scope:
            assert args[args.index('--run-id') + 1] == scope


def test_direct_cloud_cell_rejects_redis_before_credentials(tmp_path):
    steps = workflow('e2e-cloud-aws-cell.yml')['jobs']['candidate-lock']['steps']
    guard = steps[0]
    assert guard['name'] == 'Require Redis disabled'
    for mode in ('on', 'both', '', 'off'):
        result = subprocess.run(['bash', '-c', guard['run']], cwd=tmp_path,
                                env={**os.environ, 'REDIS_MODE': mode}, capture_output=True)
        assert (result.returncode == 0) == (mode == 'off')


def test_private_redis_on_campaign_cannot_dispatch(tmp_path):
    guard = workflow('agent-e2e-campaign.yml')['jobs']['ecs']['steps'][0]
    assert guard['name'] == 'Require Redis-free private campaign binding'
    result = subprocess.run(['bash', '-c', guard['run']], cwd=tmp_path, capture_output=True)
    assert result.returncode != 0
    assert b'Redis-on qualification is retired' in result.stdout
