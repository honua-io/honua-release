"""Unit tests for the Launch Stack AWS CLI adapter and producer workflow contract."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
import yaml

from launch_stack_aws import AwsCliAdapter, AwsCommandError, disable_deletion_protection
import run_launch_stack_qualification as driver
from test_run_launch_stack_qualification import (
    IMAGE_REF, INDEX_DIGEST, LOCK_DIGEST, SHA, STACK, STACK_ID, _config,
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / '.github/workflows/produce-launch-stack-proof.yml'


def test_aws_cli_adapter_passes_hardened_params_and_redacts_stderr(tmp_path):
    captured = []

    def fake_runner(argv, **_kwargs):
        captured.append(argv)
        if 'create-stack' in argv and '--stack-name' in argv:
            return subprocess.CompletedProcess(argv, 0, f'{{"StackId": "{STACK_ID}"}}', '')
        return subprocess.CompletedProcess(
            argv, 254, '',
            'An error occurred (AccessDenied) when calling DescribeStacks: arn:aws:iam::999999999999:role/secret',
        )

    cfg = _config(tmp_path, console_bundle_version='2026.1-rc.2')
    adapter = AwsCliAdapter(cfg, runner=fake_runner)
    attempt = driver.StackAttempt(cfg.journal_path, STACK, '000000000000', 'us-east-1', cfg.attempt_id)
    with driver.open_attempt(attempt, IMAGE_REF) as owned:
        assert adapter.create_stack(owned, IMAGE_REF) == STACK_ID
    joined = ' '.join(captured[0])
    assert 'Key=HonuaGate,Value=true' in joined and 'ParameterKey=Protection,ParameterValue=Contain' in joined
    assert 'ParameterKey=WatchdogMode,ParameterValue=NotifyOnly' in joined
    assert f'ParameterKey=ServerImageUri,ParameterValue={IMAGE_REF}' in joined
    assert 'ParameterKey=ConsoleBundleVersion,ParameterValue=2026.1-rc.2' in joined

    with pytest.raises(AwsCommandError) as exc_info:
        adapter('cloudformation', 'describe-stacks', '--stack-name', STACK)
    assert 'AccessDenied' in str(exc_info.value) and '999999999999' not in str(exc_info.value)


def test_disable_deletion_protection_retries_busy_rds_instance():
    attempts = {'rds': 0}

    def busy_then_ready(_service, operation, *_args):
        if operation == 'list-stack-resources':
            return {'StackResourceSummaries': [{'LogicalResourceId': 'Database',
                                                'ResourceType': 'AWS::RDS::DBInstance',
                                                'PhysicalResourceId': 'db-1'}]}
        attempts['rds'] += 1
        if attempts['rds'] == 1:
            raise AwsCommandError('busy', error_code='InvalidDBInstanceState')
        return {}

    disable_deletion_protection(busy_then_ready, STACK_ID, sleep=lambda _s: None)
    assert attempts['rds'] == 2


def test_workflow_contract_and_preflight_fail_closed_on_missing_gate_vars(tmp_path):
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding='utf-8'))
    triggers = document.get('on', document.get(True))
    assert set(triggers) == {'workflow_dispatch'}
    assert {'server_sha', 'server_digest', 'platform_lock_digest',
            'stack_suffix', 'template_url', 'console_bundle_version'} <= set(triggers['workflow_dispatch']['inputs'])
    assert document['permissions'] == {'contents': 'read', 'id-token': 'write'}

    job = document['jobs']['produce']
    assert job['if'] == "github.repository == 'honua-io/honua-release' && github.ref == 'refs/heads/trunk'"
    assert job['environment'] == 'gate' and job['timeout-minutes'] == 75
    preflight = next(s for s in job['steps'] if s.get('id') == 'preflight')
    env = {
        'PATH': os.environ['PATH'], 'GITHUB_OUTPUT': str(tmp_path / 'gh_output'),
        'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '1',
        'HONUA_GATE_RELEASE_ROLE_ARN': '', 'HONUA_GATE_LANE_ROLE_ARN': '',
        'HONUA_GATE_CFN_ROLE_ARN': '', 'HONUA_GATE_TEMPLATE_URL': '',
        'SERVER_SHA': SHA, 'SERVER_DIGEST': INDEX_DIGEST, 'LOCK_DIGEST': LOCK_DIGEST,
        'STACK_SUFFIX': '', 'CONSOLE_BUNDLE_VERSION': '',
    }
    res = subprocess.run(['bash', '-c', preflight['run']], env=env, capture_output=True, text=True, check=False)
    assert res.returncode == 1
    assert 'HONUA_GATE_RELEASE_ROLE_ARN' in res.stdout and 'HONUA_GATE_TEMPLATE_URL' in res.stdout
    cleanup_step = next(s for s in job['steps'] if '--cleanup-only' in str(s.get('run', '')))
    assert 'always()' in str(cleanup_step.get('if', ''))
    upload = next(s for s in job['steps'] if str(s.get('uses', '')).startswith('actions/upload-artifact@'))
    assert upload['with']['name'] == 'launch-stack-proof' and upload['with']['if-no-files-found'] == 'error'
