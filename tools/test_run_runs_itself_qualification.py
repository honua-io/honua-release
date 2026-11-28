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
from launch_stack_observations import ObservationError
import run_runs_itself_qualification as driver
import runs_itself_aws as ri_aws

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / '.github/workflows/produce-runs-itself-proof.yml'
SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
LOCK = 'sha256:' + 'c' * 64
STACK = 'honua-gate-1'
BUDGET_STACK = 'honua-gate-2'


def _receipt_for_row(row: int, recovery: float = 1.5, **overrides) -> dict:
    obs: list[dict] = [{'fault_started_at': '2026-11-28T10:00:00+00:00'}]
    if row == 1:
        obs.append({'service_recovered_without_capacity_change': True})
    if row in driver.RESTORED_ROWS:
        obs.append({'fault_restored': True})
    if row == 11:
        obs.append({'notify_only_diagnosed': True, 'zero_mutations_observed': True})
    if row == 12:
        obs.append({
            'human_needed_notified': True, 'plain_language_guidance_verified': True,
            'zero_mutations_observed': True, 'expired_safely': True,
            'initial_sns_message_id': 'sns-1', 'initial_sqs_message_id': 'sqs-1',
            'renotify_sns_message_id': 'sns-2', 'renotify_sqs_message_id': 'sqs-2',
            'expiry_sns_message_id': 'sns-3', 'expiry_sqs_message_id': 'sqs-3',
        })
    base = {
        'row': row, 'executed': True,
        'result': 'OBSERVED_EMAIL_AND_INBOX_VERIFICATION_REQUIRED',
        'recovery_minutes': recovery, 'observations': obs,
    }
    base.update(overrides)
    return base


class FakeRunsItselfAws:
    """In-memory AWS control plane covering notification checks and stack safety restoration."""

    def __init__(self) -> None:
        self.confirmed = True
        self.sns_published = 2.0
        self.sns_failed = 0.0
        self.sqs_sent = 2.0
        self.dlq_count = 0
        self.outbox: list[dict] = []
        self.compressed_state = True
        self.watchdog_mode = 'NotifyOnly'
        self.fault = 'None'
        self.db_status = 'available'
        self.storage_alarm = 'OK'
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []

    def _state_raw(self) -> str:
        doc = json.dumps({'version': 1, 'actions': [], 'outbox': self.outbox})
        if self.compressed_state:
            packed = base64.b64encode(zlib.compress(doc.encode())).decode()
            return json.dumps({'zlib_v1': packed})
        return doc

    def _handle_metrics(self, args: tuple[str, ...]) -> dict:
        metric = args[args.index('--metric-name') + 1]
        values = {
            'NumberOfMessagesPublished': self.sns_published,
            'NumberOfNotificationsFailed': self.sns_failed,
            'NumberOfMessagesSent': self.sqs_sent,
        }
        return {'Datapoints': [{'Sum': values[metric]}]}

    def _apply_change_set_params(self, raw_params: str) -> None:
        updates = {
            item['ParameterKey']: item['ParameterValue']
            for item in json.loads(raw_params)
            if 'ParameterValue' in item
        }
        if 'WatchdogMode' in updates:
            self.watchdog_mode = updates['WatchdogMode']
        if 'QualificationFault' in updates:
            self.fault = updates['QualificationFault']

    def _handle_cloudformation(self, operation: str, args: tuple[str, ...]) -> dict:
        if operation == 'list-stack-resources':
            return {'StackResourceSummaries': [
                {'LogicalResourceId': 'OpsTopic', 'PhysicalResourceId': 'arn:aws:sns:us-east-1:000000000000:honua-gate-1-operations'},
                {'LogicalResourceId': 'OpsEvents', 'PhysicalResourceId': 'https://sqs.us-east-1.amazonaws.com/000000000000/ops-events'},
                {'LogicalResourceId': 'OpsDeadLetters', 'PhysicalResourceId': 'https://sqs.us-east-1.amazonaws.com/000000000000/ops-dlq'},
                {'LogicalResourceId': 'WatchdogState', 'PhysicalResourceId': '/honua/state'},
                {'LogicalResourceId': 'Database', 'PhysicalResourceId': 'db-gate-1'},
                {'LogicalResourceId': 'OpsAlarmDatabaseStorage', 'PhysicalResourceId': 'honua-gate-1-ops-DatabaseStorage'},
            ]}
        if operation == 'describe-stacks':
            return {'Stacks': [{'Parameters': [
                {'ParameterKey': 'WatchdogMode', 'ParameterValue': self.watchdog_mode},
                {'ParameterKey': 'QualificationFault', 'ParameterValue': self.fault},
            ]}]}
        if operation == 'create-change-set':
            self._apply_change_set_params(args[args.index('--parameters') + 1])
        return {}

    def __call__(self, *command: str) -> dict:
        service, operation, *args = command
        self.calls.append((service, operation, tuple(args)))
        if service == 'cloudformation':
            return self._handle_cloudformation(operation, tuple(args))
        if operation == 'list-subscriptions-by-topic':
            arn = 'sub-confirmed' if self.confirmed else 'PendingConfirmation'
            return {'Subscriptions': [{'SubscriptionArn': arn}]}
        if operation == 'get-metric-statistics':
            return self._handle_metrics(tuple(args))
        if operation == 'get-queue-attributes':
            return {'Attributes': {'ApproximateNumberOfMessages': str(self.dlq_count)}}
        if operation == 'get-parameter':
            return {'Parameter': {'Value': self._state_raw()}}
        if operation == 'describe-db-instances':
            return {'DBInstances': [{'DBInstanceStatus': self.db_status}]}
        if operation == 'start-db-instance':
            self.db_status = 'available'
            return {}
        if operation == 'describe-alarms':
            return {'MetricAlarms': [{'StateValue': self.storage_alarm}]}
        if operation == 'set-alarm-state':
            self.storage_alarm = 'OK'
            return {}
        return {}


def _config(tmp_path: Path, **overrides) -> driver.RunsItselfConfig:
    gate_script = tmp_path / 'gate'
    gate_script.write_text('#!/usr/bin/env python3\n', encoding='utf-8')
    base = {
        'server_sha': SHA, 'server_digest': DIGEST, 'platform_lock_digest': LOCK,
        'stack_name': STACK, 'budget_stack_name': BUDGET_STACK,
        'gate_script': gate_script, 'artifacts_dir': tmp_path / 'receipts',
        'output_dir': tmp_path / 'proof',
    }
    base.update(overrides)
    return driver.RunsItselfConfig(**base)


def _fake_gate_runner(overrides: dict[int, dict] | None = None, fail_row: int | None = None):
    invoked: list[tuple[int, str]] = []

    def runner(cmd: list[str]) -> subprocess.CompletedProcess:
        row = int(cmd[cmd.index('run') + 1])
        stack = cmd[cmd.index('--stack') + 1]
        out_dir = Path(cmd[cmd.index('--artifacts') + 1])
        invoked.append((row, stack))
        if row == fail_row:
            return subprocess.CompletedProcess(cmd, 1, '', 'gate failure')
        payload = (overrides or {}).get(row, _receipt_for_row(row, recovery=round(1.0 + row * 0.25, 2)))
        if payload is not None:
            (out_dir / f'row-{row}-{stack}-20261128T100000000000Z.json').write_text(
                json.dumps(payload), encoding='utf-8',
            )
        return subprocess.CompletedProcess(cmd, 0, '', '')

    return runner, invoked


def test_round_trip_emits_proof_and_all_21_receipts_plus_release_notes(tmp_path, monkeypatch):
    aws = FakeRunsItselfAws()
    runner, invoked = _fake_gate_runner()
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    deps = driver.RunsItselfDeps(
        aws=aws, run_gate=runner,
        verify_notifications=ri_aws.verify_row_notifications,
        utc_now=lambda: now,
    )
    config = _config(tmp_path)
    proof = driver.run_qualification(config, deps)

    assert dict(invoked)[3] == BUDGET_STACK
    assert all(stack == STACK for row, stack in invoked if row != 3)
    assert proof['unattendedPersonMinutes'] == 0
    assert tuple(proof['rows'].keys()) == emitter.OPERATES_IDS
    encoded = (config.output_dir / 'runs-itself-proof.json').read_text(encoding='utf-8')
    assert '000000000000' not in encoded and 'arn:aws:' not in encoded

    monkeypatch.chdir(tmp_path)
    assert emitter.main([
        'runs-itself', '--proof-dir', 'proof', '--server-sha', SHA,
        '--server-digest', DIGEST, '--lock-digest', LOCK, '--out', 'emitted',
    ]) == 0
    emitted_dir = tmp_path / 'emitted'
    receipt_files = sorted(p.name for p in emitted_dir.glob('*.json'))
    assert len(receipt_files) == 21
    for row_id in emitter.OPERATES_IDS:
        assert (emitted_dir / f'{row_id}.json').is_file()
        assert (emitted_dir / f'recovery-{row_id}.json').is_file()
    assert (emitted_dir / 'unattended-person.json').is_file()
    notes = (emitted_dir / 'release-notes.md').read_text(encoding='utf-8')
    assert '<!-- honua-outcome:unattended-person --> 0 minutes' in notes
    assert '<!-- honua-outcome:recovery-operates-12-notify --> 4.0 minutes' in notes


@pytest.mark.parametrize('mutation,pattern', [
    ({'confirmed': False}, 'confirmed SNS subscription'),
    ({'sns_published': 0.0}, 'No SNS notification published'),
    ({'sns_failed': 1.0}, 'failed notification delivery'),
    ({'sqs_sent': 0.0}, 'No ops-inbox event queued'),
    ({'dlq_count': 2}, 'OpsDeadLetters'),
    ({'outbox': [{'pending': True}]}, 'Watchdog outbox'),
])
def test_notification_verification_fails_closed(mutation, pattern):
    aws = FakeRunsItselfAws()
    for key, value in mutation.items():
        setattr(aws, key, value)
    now = dt.datetime(2026, 11, 28, 10, 0, tzinfo=dt.timezone.utc)
    with pytest.raises(ObservationError, match=pattern):
        ri_aws.verify_row_notifications(aws, STACK, (now, now + dt.timedelta(minutes=2)))


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
    now = dt.datetime(2026, 11, 28, 10, 0, tzinfo=dt.timezone.utc)
    deps = driver.RunsItselfDeps(
        aws=aws, run_gate=runner,
        verify_notifications=ri_aws.verify_row_notifications,
        utc_now=lambda: now,
    )
    config = _config(tmp_path)
    with pytest.raises(ObservationError, match=pattern):
        driver.run_qualification(config, deps)
    assert not (config.output_dir / 'runs-itself-proof.json').exists()


def test_unconfirmed_notification_or_failed_gate_exit_refuses_pass(tmp_path):
    aws = FakeRunsItselfAws()
    runner, _ = _fake_gate_runner()
    now = dt.datetime(2026, 11, 28, 10, 0, tzinfo=dt.timezone.utc)
    deps = driver.RunsItselfDeps(
        aws=aws, run_gate=runner,
        verify_notifications=lambda *_a: {'notificationVerified': True, 'inboxVerified': False},
        utc_now=lambda: now,
    )
    config = _config(tmp_path)
    with pytest.raises(ObservationError, match='SNS/ops-inbox confirmation'):
        driver.run_qualification(config, deps)
    assert not (config.output_dir / 'runs-itself-proof.json').exists()

    fail_runner, _ = _fake_gate_runner(fail_row=9)
    fail_deps = driver.RunsItselfDeps(
        aws=aws, run_gate=fail_runner,
        verify_notifications=ri_aws.verify_row_notifications, utc_now=lambda: now,
    )
    with pytest.raises(ObservationError, match='row 9'):
        driver.run_qualification(config, fail_deps)


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
    deps = driver.RunsItselfDeps(
        aws=aws, run_gate=runner,
        verify_notifications=ri_aws.verify_row_notifications,
        utc_now=lambda: dt.datetime(2026, 11, 28, 10, 0, tzinfo=dt.timezone.utc),
    )
    with pytest.raises(ObservationError, match=pattern):
        driver.run_qualification(_config(tmp_path, **{field: value}), deps)
    assert not invoked and not aws.calls


def test_cleanup_restores_mode_fault_database_and_storage_alarm(tmp_path):
    aws = FakeRunsItselfAws()
    aws.watchdog_mode = 'Act'
    aws.fault = 'WatchdogStopped'
    aws.db_status = 'stopped'
    aws.storage_alarm = 'ALARM'
    results = driver.cleanup_stacks(_config(tmp_path), aws)
    assert [item['stackName'] for item in results] == [STACK, BUDGET_STACK]
    assert 'WatchdogMode=NotifyOnly' in results[0]['restored']
    assert 'QualificationFault=None' in results[0]['restored']
    assert 'start-db-instance' in results[0]['restored']
    assert 'reset-storage-alarm' in results[0]['restored']
    assert aws.watchdog_mode == 'NotifyOnly' and aws.fault == 'None'
    assert aws.db_status == 'available' and aws.storage_alarm == 'OK'


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
    assert res.returncode == 1
    assert 'HONUA_GATE_RELEASE_ROLE_ARN' in res.stdout and 'STACK_NAME' in res.stdout

    cleanup_step = next(s for s in job['steps'] if '--cleanup-only' in str(s.get('run', '')))
    assert 'always()' in str(cleanup_step.get('if', ''))
    verify_step = next(s for s in job['steps'] if 'emit_live_promise_receipts.py runs-itself' in str(s.get('run', '')))
    assert verify_step is not None
    upload = next(s for s in job['steps'] if str(s.get('uses', '')).startswith('actions/upload-artifact@'))
    assert upload['with']['name'] == 'runs-itself-proof' and upload['with']['if-no-files-found'] == 'error'
