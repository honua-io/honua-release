"""Offline unit tests for Runs-Itself qualification proof production and workflow contract."""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import zlib

import pytest
import yaml

import emit_live_promise_receipts as emitter
from launch_stack_aws import AwsCommandError
from launch_stack_observations import ObservationError
import run_launch_stack_qualification as ls_driver
import run_runs_itself_qualification as driver
import runs_itself_aws as ri_aws

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / '.github/workflows/produce-runs-itself-proof.yml'
SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
LOCK = 'sha256:' + 'c' * 64
AMD64_DIGEST = 'sha256:' + 'd' * 64
ARM64_DIGEST = 'sha256:' + 'e' * 64
IMAGE_REPO = 'ghcr.io/honua-io/honua-server'
IMAGE_REF = f'{IMAGE_REPO}@{DIGEST}'
STACK = 'honua-gate-1'
BUDGET_STACK = 'honua-gate-2'
TASK_DEF_ARN = 'arn:aws:ecs:us-east-1:000000000000:task-definition/server:3'


def _receipt_for_row(row: int, recovery: float = 1.5, **overrides) -> dict:
    obs: list[dict] = [{'fault_started_at': '2026-11-28T10:00:00+00:00'}]
    if row == 1:
        obs.append({'service_recovered_without_capacity_change': True})
    if row in driver.RESTORED_ROWS:
        obs.append({'fault_restored': True})
    if row == 11:
        obs.append({'notify_only_diagnosed': True, 'zero_mutations_observed': True})
    if row == 12:
        obs.append({'human_needed_notified': True, 'plain_language_guidance_verified': True,
                    'zero_mutations_observed': True, 'expired_safely': True,
                    'initial_sns_message_id': 'sns-1', 'initial_sqs_message_id': 'sqs-1',
                    'renotify_sns_message_id': 'sns-2', 'renotify_sqs_message_id': 'sqs-2',
                    'expiry_sns_message_id': 'sns-3', 'expiry_sqs_message_id': 'sqs-3'})
    payload = {'row': row, 'executed': True, 'result': 'OBSERVED_EMAIL_AND_INBOX_VERIFICATION_REQUIRED',
               'recovery_minutes': recovery, 'observations': obs}
    if row in driver.UNATTENDED_ROWS:
        payload['person_minutes'] = 0
    return {**payload, **overrides}


def _measurement(**overrides) -> ls_driver.ImageMeasurement:
    return ls_driver.ImageMeasurement(**{
        'image_ref': IMAGE_REF, 'index_digest': DIGEST,
        'platform_digests': {'amd64': AMD64_DIGEST, 'arm64': ARM64_DIGEST},
        'platform_sizes': {'amd64': 250_000_000, 'arm64': 260_000_000},
        'published_aot': True, 'default_image': 'postgis-only', **overrides,
    })


class FakeRunsItselfAws:
    """In-memory AWS control plane covering candidate binding, notifications, and cleanup."""

    def __init__(self) -> None:
        self.confirmed, self.sub_protocol, self.sns_published, self.sns_failed = True, 'email', 2.0, 0.0
        self.sqs_sent, self.dlq_count, self.outbox = 2.0, 0, []
        self.watchdog_mode = {STACK: 'NotifyOnly', BUDGET_STACK: 'NotifyOnly'}
        self.fault = {STACK: 'None', BUDGET_STACK: 'None'}
        self.db_status = {STACK: 'available', BUDGET_STACK: 'available'}
        self.storage_alarm = {STACK: 'OK', BUDGET_STACK: 'OK'}
        self.declared_image = {STACK: IMAGE_REF, BUDGET_STACK: IMAGE_REF}
        self.running_digest = {STACK: ARM64_DIGEST, BUDGET_STACK: ARM64_DIGEST}
        self.fail_describe_stack: str | None = None
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def _state_raw(self) -> str:
        doc = json.dumps({'version': 1, 'actions': [], 'outbox': self.outbox})
        packed = base64.b64encode(zlib.compress(doc.encode())).decode()
        return json.dumps({'zlib_v1': packed})

    def _stack_resources(self, stack_name: str) -> list[dict]:
        rows = (
            ('Cluster', 'AWS::ECS::Cluster', f'cluster-{stack_name}'),
            ('Service', 'AWS::ECS::Service', f'service-{stack_name}'),
            ('ServerTask', 'AWS::ECS::TaskDefinition', f'{TASK_DEF_ARN}-{stack_name}'),
            ('Targets', 'AWS::ElasticLoadBalancingV2::TargetGroup', f'targets-{stack_name}'),
            ('OpsTopic', 'AWS::SNS::Topic', f'arn:aws:sns:us-east-1:000000000000:{stack_name}-operations'),
            ('OpsEvents', 'AWS::SQS::Queue', f'https://sqs.us-east-1.amazonaws.com/000000000000/{stack_name}-ops'),
            ('OpsDeadLetters', 'AWS::SQS::Queue', f'https://sqs.us-east-1.amazonaws.com/000000000000/{stack_name}-dlq'),
            ('WatchdogState', 'AWS::SSM::Parameter', f'/honua/{stack_name}/state'),
            ('Database', 'AWS::RDS::DBInstance', f'db-{stack_name}'),
            ('OpsAlarmDatabaseStorage', 'AWS::CloudWatch::Alarm', f'{stack_name}-ops-DatabaseStorage'),
        )
        return [{'LogicalResourceId': l, 'ResourceType': t, 'PhysicalResourceId': p} for l, t, p in rows]

    def _apply_change_set_params(self, stack: str, raw_params: str) -> None:
        updates = {item['ParameterKey']: item['ParameterValue']
                   for item in json.loads(raw_params) if 'ParameterValue' in item}
        if 'WatchdogMode' in updates:
            self.watchdog_mode[stack] = updates['WatchdogMode']
        if 'QualificationFault' in updates:
            self.fault[stack] = updates['QualificationFault']

    def _handle_cloudformation(self, operation: str, args: tuple[str, ...]) -> dict:
        stack = args[args.index('--stack-name') + 1]
        if operation == 'list-stack-resources':
            return {'StackResourceSummaries': self._stack_resources(stack)}
        if operation == 'describe-stacks':
            if stack == self.fail_describe_stack:
                raise AwsCommandError('aws cloudformation describe-stacks failed (AccessDenied)', 'AccessDenied')
            return {'Stacks': [{'Parameters': [
                {'ParameterKey': 'WatchdogMode', 'ParameterValue': self.watchdog_mode[stack]},
                {'ParameterKey': 'QualificationFault', 'ParameterValue': self.fault[stack]},
            ]}]}
        if operation == 'create-change-set':
            self._apply_change_set_params(stack, args[args.index('--parameters') + 1])
        return {}

    def _handle_ecs(self, operation: str, args: tuple[str, ...]) -> dict:
        if operation == 'describe-services':
            stack = args[args.index('--cluster') + 1].removeprefix('cluster-')
            td = f'{TASK_DEF_ARN}-{stack}'
            return {'services': [{
                'serviceName': f'service-{stack}', 'status': 'ACTIVE', 'launchType': 'FARGATE',
                'desiredCount': 1, 'runningCount': 1, 'pendingCount': 0, 'taskDefinition': td,
                'loadBalancers': [{'targetGroupArn': f'targets-{stack}', 'containerName': 'honua', 'containerPort': 8080}],
                'deployments': [{'status': 'PRIMARY', 'rolloutState': 'COMPLETED', 'taskDefinition': td}],
            }]}
        if operation == 'describe-task-definition':
            td = args[args.index('--task-definition') + 1]
            stack = td.removeprefix(f'{TASK_DEF_ARN}-')
            return {'taskDefinition': {
                'taskDefinitionArn': td, 'status': 'ACTIVE', 'requiresCompatibilities': ['FARGATE'],
                'networkMode': 'awsvpc',
                'runtimePlatform': {'cpuArchitecture': 'ARM64', 'operatingSystemFamily': 'LINUX'},
                'containerDefinitions': [{'name': 'honua', 'essential': True, 'image': self.declared_image[stack]}],
            }}
        if operation == 'list-tasks':
            stack = args[args.index('--cluster') + 1].removeprefix('cluster-')
            return {'taskArns': [f'task-{stack}-1']}
        stack = args[args.index('--cluster') + 1].removeprefix('cluster-')
        return {'tasks': [{
            'taskArn': f'task-{stack}-1', 'taskDefinitionArn': f'{TASK_DEF_ARN}-{stack}',
            'launchType': 'FARGATE', 'lastStatus': 'RUNNING', 'desiredStatus': 'RUNNING', 'healthStatus': 'HEALTHY',
            'containers': [{'name': 'honua', 'lastStatus': 'RUNNING', 'healthStatus': 'HEALTHY',
                            'imageDigest': self.running_digest[stack]}],
        }]}

    def _handle_ops(self, operation: str, args: tuple[str, ...]) -> dict:
        if operation == 'list-subscriptions-by-topic':
            arn = 'sub-confirmed' if self.confirmed else 'PendingConfirmation'
            return {'Subscriptions': [{'Protocol': self.sub_protocol, 'SubscriptionArn': arn}]}
        if operation == 'get-metric-statistics':
            metric = args[args.index('--metric-name') + 1]
            vals = {'NumberOfMessagesPublished': self.sns_published,
                    'NumberOfNotificationsFailed': self.sns_failed, 'NumberOfMessagesSent': self.sqs_sent}
            return {'Datapoints': [{'Sum': vals[metric]}]}
        if operation == 'get-queue-attributes':
            return {'Attributes': {'ApproximateNumberOfMessages': str(self.dlq_count)}}
        if operation == 'get-parameter':
            return {'Parameter': {'Value': self._state_raw()}}
        if operation == 'describe-db-instances':
            stack = args[args.index('--db-instance-identifier') + 1].removeprefix('db-')
            return {'DBInstances': [{'DBInstanceStatus': self.db_status[stack]}]}
        if operation == 'start-db-instance':
            stack = args[args.index('--db-instance-identifier') + 1].removeprefix('db-')
            self.db_status[stack] = 'available'
        if operation == 'describe-alarms':
            stack = args[args.index('--alarm-names') + 1].removesuffix('-ops-DatabaseStorage')
            return {'MetricAlarms': [{'StateValue': self.storage_alarm[stack]}]}
        if operation == 'set-alarm-state':
            stack = args[args.index('--alarm-names' if '--alarm-names' in args else '--alarm-name') + 1].removesuffix('-ops-DatabaseStorage')
            self.storage_alarm[stack] = 'OK'
        return {}

    def __call__(self, *command: str) -> dict:
        service, operation, *args = command
        self.calls.append((service, operation, tuple(args)))
        if service == 'cloudformation':
            return self._handle_cloudformation(operation, tuple(args))
        if service == 'ecs':
            return self._handle_ecs(operation, tuple(args))
        return self._handle_ops(operation, tuple(args))


def _config(tmp_path: Path, **overrides) -> driver.RunsItselfConfig:
    gate_script = tmp_path / 'gate'
    gate_script.write_text('#!/usr/bin/env python3\n', encoding='utf-8')
    base = {'server_sha': SHA, 'server_digest': DIGEST, 'platform_lock_digest': LOCK,
            'stack_name': STACK, 'budget_stack_name': BUDGET_STACK, 'gate_script': gate_script,
            'artifacts_dir': tmp_path / 'receipts', 'output_dir': tmp_path / 'proof', **overrides}
    return driver.RunsItselfConfig(**base)


def _fake_gate_runner(overrides: dict[int, dict] | None = None, fail_row: int | None = None):
    invoked: list[tuple[int, str]] = []

    def runner(cmd: list[str]) -> subprocess.CompletedProcess:
        row, stack = int(cmd[cmd.index('run') + 1]), cmd[cmd.index('--stack') + 1]
        out_dir = Path(cmd[cmd.index('--artifacts') + 1])
        invoked.append((row, stack))
        if row == fail_row:
            return subprocess.CompletedProcess(cmd, 1, '', 'gate failure')
        payload = (overrides or {}).get(row, _receipt_for_row(row, recovery=round(1.0 + row * 0.25, 2)))
        if payload is not None:
            (out_dir / f'row-{row}-{stack}-20261128T100000000000Z.json').write_text(json.dumps(payload), encoding='utf-8')
        return subprocess.CompletedProcess(cmd, 0, '', '')

    return runner, invoked


def _deps(aws: FakeRunsItselfAws, runner, **overrides) -> driver.RunsItselfDeps:
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    return driver.RunsItselfDeps(**{
        'aws': aws, 'inspect_image': lambda _cfg: _measurement(), 'run_gate': runner,
        'verify_notifications': ri_aws.verify_row_notifications, 'utc_now': lambda: now,
        'sleep': lambda _s: None, **overrides,
    })


def test_round_trip_emits_proof_and_all_21_receipts_plus_release_notes(tmp_path, monkeypatch):
    aws = FakeRunsItselfAws()
    runner, invoked = _fake_gate_runner()
    config = _config(tmp_path)
    proof = driver.run_qualification(config, _deps(aws, runner))

    assert dict(invoked)[3] == BUDGET_STACK
    assert all(stack == STACK for row, stack in invoked if row != 3)
    assert proof['unattendedPersonMinutes'] == 0 and tuple(proof['rows'].keys()) == emitter.OPERATES_IDS
    encoded = (config.output_dir / 'runs-itself-proof.json').read_text(encoding='utf-8')
    assert '000000000000' not in encoded and 'arn:aws:' not in encoded

    monkeypatch.chdir(tmp_path)
    assert emitter.main([
        'runs-itself', '--proof-dir', 'proof', '--server-sha', SHA,
        '--server-digest', DIGEST, '--lock-digest', LOCK, '--out', 'emitted',
    ]) == 0
    emitted_dir = tmp_path / 'emitted'
    assert len(list(emitted_dir.glob('*.json'))) == 21
    for row_id in emitter.OPERATES_IDS:
        assert (emitted_dir / f'{row_id}.json').is_file() and (emitted_dir / f'recovery-{row_id}.json').is_file()
    notes = (emitted_dir / 'release-notes.md').read_text(encoding='utf-8')
    assert '<!-- honua-outcome:unattended-person --> 0 minutes' in notes
    assert '<!-- honua-outcome:recovery-operates-12-notify --> 4.0 minutes' in notes


@pytest.mark.parametrize('case', [
    ('declared_image', STACK, f'{IMAGE_REPO}@sha256:' + '9' * 64, 'task definition image'),
    ('running_digest', BUDGET_STACK, 'sha256:' + '8' * 64, 'running tasks do not match'),
])
def test_mismatched_deployed_candidate_fails_before_running_gate(tmp_path, case):
    field, target_stack, bad_value, pattern = case
    aws = FakeRunsItselfAws()
    getattr(aws, field)[target_stack] = bad_value
    runner, invoked = _fake_gate_runner()
    with pytest.raises(ObservationError, match=pattern):
        driver.run_qualification(_config(tmp_path), _deps(aws, runner))
    assert not invoked


@pytest.mark.parametrize('case', [
    ({'confirmed': False}, 'confirmed email SNS subscription'),
    ({'sub_protocol': 'sqs'}, 'confirmed email SNS subscription'),
    ({'sns_published': 0.0}, 'No SNS notification published'),
    ({'sns_failed': 1.0}, 'failed notification delivery'),
    ({'sqs_sent': 0.0}, 'No ops-inbox event queued'),
    ({'dlq_count': 2}, 'OpsDeadLetters'),
    ({'outbox': [{'pending': True}]}, 'Watchdog outbox'),
])
def test_notification_verification_fails_closed(case):
    mutation, pattern = case
    aws = FakeRunsItselfAws()
    for key, value in mutation.items():
        setattr(aws, key, value)
    now = dt.datetime(2026, 11, 28, 10, 0, tzinfo=dt.timezone.utc)
    with pytest.raises(ObservationError, match=pattern):
        ri_aws.verify_row_notifications(aws, STACK, (now, now + dt.timedelta(minutes=2)))


def test_notification_polling_retries_pending_metrics_until_ingested():
    aws, sleeps = FakeRunsItselfAws(), []
    aws.sns_published = 0.0
    def delayed_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            aws.sns_published = 1.0
    runner, _ = _fake_gate_runner()
    now = dt.datetime(2026, 11, 28, 10, 0, tzinfo=dt.timezone.utc)
    evidence = driver._poll_row_notifications(STACK, (now, now + dt.timedelta(minutes=1)), _deps(aws, runner, sleep=delayed_sleep))
    assert evidence['notificationVerified'] is True and len(sleeps) == 2
    small_pm = {r: _receipt_for_row(r, person_minutes=0.004 if r == 1 else 0) for r in driver.UNATTENDED_ROWS}
    assert driver.compute_unattended_person_minutes(small_pm) == pytest.approx(0.004)
    small_pm[1].pop('person_minutes')
    with pytest.raises(ObservationError, match='Missing recorded person_minutes'):
        driver.compute_unattended_person_minutes(small_pm)


@pytest.mark.parametrize('case', [
    (1, None, 'Expected exactly one receipt'),
    (2, _receipt_for_row(2, result='FAILED'), 'OBSERVED_'),
    (4, _receipt_for_row(4, executed=False, result='PLAN_ONLY'), 'was not executed'),
    (5, _receipt_for_row(5, recovery=0.0), 'positive recovery_minutes'),
    (6, _receipt_for_row(6, recovery=-1.5), 'positive recovery_minutes'),
    (9, _receipt_for_row(9, recovery=True), 'positive recovery_minutes'),
    (3, _receipt_for_row(3, observations=[{'fault_started_at': 't', 'fault_restored': True, 'started_by_harness': True}]), 'manual harness recovery'),
    (11, _receipt_for_row(11, observations=[{'fault_started_at': 't', 'fault_restored': True}]), 'NotifyOnly diagnosis'),
    (12, _receipt_for_row(12, observations=[{'fault_started_at': 't', 'fault_restored': True, 'human_needed_notified': True}]), 'plain_language_guidance_verified'),
])
def test_bad_or_missing_row_receipt_fails_closed_without_proof(tmp_path, case):
    row, bad_receipt, pattern = case
    aws = FakeRunsItselfAws()
    runner, _ = _fake_gate_runner(overrides={row: bad_receipt})
    config = _config(tmp_path)
    with pytest.raises(ObservationError, match=pattern):
        driver.run_qualification(config, _deps(aws, runner))
    assert not (config.output_dir / 'runs-itself-proof.json').exists()


def test_unconfirmed_notification_or_failed_gate_exit_refuses_pass(tmp_path):
    aws = FakeRunsItselfAws()
    runner, _ = _fake_gate_runner()
    config = _config(tmp_path)
    with pytest.raises(ObservationError, match='SNS/ops-inbox confirmation'):
        driver.run_qualification(config, _deps(aws, runner, verify_notifications=lambda *_a: {'notificationVerified': True, 'inboxVerified': False}))
    assert not (config.output_dir / 'runs-itself-proof.json').exists()
    fail_runner, _ = _fake_gate_runner(fail_row=9)
    with pytest.raises(ObservationError, match='row 9'):
        driver.run_qualification(config, _deps(aws, fail_runner))


@pytest.mark.parametrize('case', [
    ('stack_name', 'production', 'honua-gate-'),
    ('stack_name', 'honua-gate-0', 'honua-gate-'),
    ('stack_name', 'honua-gate-ls-1', 'honua-gate-'),
    ('budget_stack_name', 'honua-gate-0', 'budget_stack_name'),
    ('budget_stack_name', STACK, 'differ from stack_name'),
    ('server_sha', 'short', 'server_sha'),
    ('server_digest', 'sha256:bad', 'server_digest'),
    ('platform_lock_digest', 'md5:abc', 'platform_lock_digest'),
    ('region', 'us-west-2', 'us-east-1'),
])
def test_invalid_inputs_fail_closed_before_running_gate(tmp_path, case):
    field, value, pattern = case
    aws = FakeRunsItselfAws()
    runner, invoked = _fake_gate_runner()
    with pytest.raises(ObservationError, match=pattern):
        driver.run_qualification(_config(tmp_path, **{field: value}), _deps(aws, runner))
    assert not invoked and not aws.calls


def test_cleanup_restores_every_stack_independently(tmp_path):
    aws = FakeRunsItselfAws()
    for stack in (STACK, BUDGET_STACK):
        aws.watchdog_mode[stack], aws.fault[stack] = 'Act', 'WatchdogStopped'
        aws.db_status[stack], aws.storage_alarm[stack] = 'stopped', 'ALARM'
    results = driver.cleanup_stacks(_config(tmp_path), aws)
    assert [item['stackName'] for item in results] == [STACK, BUDGET_STACK]
    assert aws.watchdog_mode[STACK] == aws.watchdog_mode[BUDGET_STACK] == 'NotifyOnly'
    assert aws.db_status[STACK] == aws.db_status[BUDGET_STACK] == 'available'

    partial_aws = FakeRunsItselfAws()
    partial_aws.fail_describe_stack = STACK
    partial_aws.watchdog_mode[BUDGET_STACK] = 'Act'
    partial_aws.db_status[BUDGET_STACK] = 'stopped'
    with pytest.raises(ObservationError, match=STACK):
        driver.cleanup_stacks(_config(tmp_path), partial_aws)
    assert partial_aws.watchdog_mode[BUDGET_STACK] == 'NotifyOnly'
    assert partial_aws.db_status[BUDGET_STACK] == 'available'


def test_workflow_contract_and_preflight_fail_closed(tmp_path):
    document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding='utf-8'))
    triggers = document.get('on', document.get(True))
    assert set(triggers) == {'workflow_dispatch'}
    assert {'server_sha', 'server_digest', 'platform_lock_digest',
            'stack_name', 'budget_stack_name', 'marketplace_ref'} <= set(triggers['workflow_dispatch']['inputs'])
    assert document['permissions'] == {'contents': 'read', 'id-token': 'write'}
    job = document['jobs']['produce']
    assert job['if'] == "github.repository == 'honua-io/honua-release' && github.ref == 'refs/heads/trunk'"
    assert job['environment'] == 'gate'
    preflight = next(s for s in job['steps'] if s.get('id') == 'preflight')
    base_env = {
        'PATH': os.environ['PATH'], 'GITHUB_OUTPUT': str(tmp_path / 'gh_output'),
        'HONUA_GATE_RELEASE_ROLE_ARN': '', 'HONUA_GATE_LANE_ROLE_ARN': '',
        'STACK_NAME': '', 'BUDGET_STACK_NAME': '', 'MARKETPLACE_REF': 'trunk',
        'SERVER_SHA': SHA, 'SERVER_DIGEST': DIGEST, 'LOCK_DIGEST': LOCK,
    }
    res = subprocess.run(['bash', '-c', preflight['run']], env=base_env, capture_output=True, text=True, check=False)
    assert res.returncode == 1 and 'HONUA_GATE_RELEASE_ROLE_ARN' in res.stdout and 'STACK_NAME' in res.stdout
    assert 'always()' in str(next(s for s in job['steps'] if '--cleanup-only' in str(s.get('run', ''))).get('if', ''))
    assert next(s for s in job['steps'] if 'emit_live_promise_receipts.py runs-itself' in str(s.get('run', '')))
    upload = next(s for s in job['steps'] if str(s.get('uses', '')).startswith('actions/upload-artifact@'))
    assert upload['with']['name'] == 'runs-itself-proof' and upload['with']['if-no-files-found'] == 'error'
