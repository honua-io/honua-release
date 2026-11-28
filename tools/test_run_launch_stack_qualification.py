"""Unit tests for the disposable Launch Stack qualification producer and workflow."""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
import subprocess

import pytest

import emit_live_promise_receipts as emitter
from launch_stack_aws import disable_deletion_protection
from launch_stack_https import HttpsObservationError
from launch_stack_observations import ObservationError
import run_launch_stack_qualification as driver
from test_launch_stack_observations import FakeAws

SHA = 'a' * 40
INDEX_DIGEST = 'sha256:' + 'b' * 64
LOCK_DIGEST = 'sha256:' + 'c' * 64
AMD64_DIGEST = 'sha256:' + 'd' * 64
ARM64_DIGEST = 'sha256:' + 'e' * 64
IMAGE_REPO = 'ghcr.io/honua-io/honua-server'
IMAGE_REF = f'{IMAGE_REPO}@{INDEX_DIGEST}'
STACK = 'honua-gate-ls-42-1'
STACK_ID = f'arn:aws:cloudformation:us-east-1:000000000000:stack/{STACK}/00000000-0000-0000-0000-000000000001'
TEMPLATE_URL = 'https://templates.example.test/deploy.yaml'
CFN_ROLE_ARN = 'arn:aws:iam::000000000000:role/honua-gate-cfn'
FRONTEND_ORIGIN = 'https://d111111abcdef8.cloudfront.net'
REVISION = 'arn:aws:ecs:us-east-1:000000000000:task-definition/server:3'


def _hardened_resources() -> list[dict]:
    entries = (
        ('Cluster', 'AWS::ECS::Cluster', 'cluster'),
        ('Service', 'AWS::ECS::Service', 'service'),
        ('ServerTask', 'AWS::ECS::TaskDefinition', REVISION),
        ('Targets', 'AWS::ElasticLoadBalancingV2::TargetGroup', 'targets'),
        ('WebAcl', 'AWS::WAFv2::WebACL', 'web-acl-1'),
        ('Distribution', 'AWS::CloudFront::Distribution', 'dist-1'),
        ('Database', 'AWS::RDS::DBInstance', 'db-honua-1'),
        ('Alb', 'AWS::ElasticLoadBalancingV2::LoadBalancer', 'alb-arn-1'),
        ('LogsEndpoint', 'AWS::EC2::VPCEndpoint', 'vpce-1'),
    )
    return [{'LogicalResourceId': logical, 'ResourceType': kind,
             'PhysicalResourceId': physical, 'ResourceStatus': 'CREATE_COMPLETE'}
            for logical, kind, physical in entries]


class FakeQualificationAws(FakeAws):
    """In-memory AWS control-plane fixture covering create, observe, unprotect, and delete."""

    def __init__(self) -> None:
        super().__init__()
        stack = self.data['describe-stacks']['Stacks'][0]
        stack['StackName'] = STACK
        stack['StackId'] = STACK_ID
        stack['Parameters'] = [
            {'ParameterKey': 'Protection', 'ParameterValue': 'Contain'},
            {'ParameterKey': 'WatchdogMode', 'ParameterValue': 'NotifyOnly'},
            {'ParameterKey': 'ServerImageUri', 'ParameterValue': IMAGE_REF},
        ]
        stack['Outputs'] = [
            {'OutputKey': 'ProtectionStage', 'OutputValue': 'Contain'},
            {'OutputKey': 'HonuaUrl', 'OutputValue': FRONTEND_ORIGIN},
            {'OutputKey': 'ConsoleUrl', 'OutputValue': f'{FRONTEND_ORIGIN}/console/'},
        ]
        self.data['list-stack-resources'] = {'StackResourceSummaries': _hardened_resources()}
        td = self.data['describe-task-definition']['taskDefinition']
        td['containerDefinitions'][0]['image'] = IMAGE_REF
        td['containerDefinitions'][0]['environment'] = [{'name': 'ASPNETCORE_ENVIRONMENT', 'value': 'Production'}]
        self.data['describe-tasks']['tasks'][0]['containers'][0]['imageDigest'] = ARM64_DIGEST
        self.db_protected = True
        self.alb_protected = True
        self.alb_scheme = 'internal'
        self.web_acl_id = 'web-acl-1'
        self.retained = False
        self.delete_count = 0
        self.create_count = 0
        self.unprotected_db = False
        self.unprotected_alb = False

    def scope(self) -> tuple[str, str]:
        return '000000000000', 'us-east-1'

    def prepare_delete(self, stack_id: str) -> None:
        disable_deletion_protection(self, stack_id, sleep=lambda _s: None)

    def create_stack(self, owned, image: str) -> str:
        assert owned.stack_name == STACK and image == IMAGE_REF
        self.create_count += 1
        self.data['describe-stacks']['Stacks'][0]['Tags'].extend(owned.tags)
        return STACK_ID

    def _delete_stack(self) -> dict:
        assert not self.db_protected and not self.alb_protected
        self.delete_count += 1
        self.data['describe-stacks']['Stacks'][0]['StackStatus'] = 'DELETE_COMPLETE'
        status = 'DELETE_SKIPPED' if self.retained else 'DELETE_COMPLETE'
        for item in self.data['list-stack-resources']['StackResourceSummaries']:
            item['ResourceStatus'] = status
        return {}

    def _handle_mutation(self, operation: str) -> dict | None:
        if operation == 'wait':
            return {}
        if operation == 'modify-db-instance':
            self.db_protected = False
            self.unprotected_db = True
            return {}
        if operation == 'modify-load-balancer-attributes':
            self.alb_protected = False
            self.unprotected_alb = True
            return {}
        if operation == 'delete-stack':
            return self._delete_stack()
        return None

    def _handle_hardened_read(self, operation: str) -> dict | None:
        readers = {
            'describe-db-instances': lambda: {'DBInstances': [{'DeletionProtection': self.db_protected}]},
            'describe-load-balancers': lambda: {'LoadBalancers': [{'Scheme': self.alb_scheme}]},
            'describe-load-balancer-attributes': lambda: {'Attributes': [
                {'Key': 'deletion_protection.enabled', 'Value': 'true' if self.alb_protected else 'false'}]},
            'get-distribution': lambda: {'Distribution': {
                'DistributionConfig': {'Enabled': True, 'WebACLId': self.web_acl_id}}},
        }
        return readers[operation]() if operation in readers else None

    def __call__(self, *command: str) -> dict:
        service, operation, *args = command
        self.calls.append((service, operation, tuple(args)))
        for handler in (self._handle_mutation, self._handle_hardened_read):
            result = handler(operation)
            if result is not None:
                return result
        return copy.deepcopy(self.data[operation])


def _config(tmp_path: Path, **overrides) -> driver.QualificationConfig:
    base = {
        'server_sha': SHA, 'server_digest': INDEX_DIGEST, 'platform_lock_digest': LOCK_DIGEST,
        'stack_name': STACK, 'template_url': TEMPLATE_URL, 'cfn_role_arn': CFN_ROLE_ARN,
        'attempt_id': 'run-42-attempt-1', 'journal_path': tmp_path / 'journal/attempt.json',
        'output_dir': tmp_path / 'proof',
    }
    base.update(overrides)
    return driver.QualificationConfig(**base)


def _measurement(**overrides) -> driver.ImageMeasurement:
    base = {
        'image_ref': IMAGE_REF, 'index_digest': INDEX_DIGEST,
        'platform_digests': {'amd64': AMD64_DIGEST, 'arm64': ARM64_DIGEST},
        'platform_sizes': {'amd64': 250_000_000, 'arm64': 260_000_000},
        'published_aot': True, 'default_image': 'postgis-only',
    }
    base.update(overrides)
    return driver.ImageMeasurement(**base)


def _deps(aws: FakeQualificationAws, **overrides) -> driver.QualificationDeps:
    ticks = iter([100.0, 970.0])
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    base = {
        'aws': aws, 'inspect_image': lambda _cfg: _measurement(),
        'probe_https': lambda _o: {'responses': [{'path': '/console/', 'status': 200},
                                                 {'path': '/healthz/ready', 'status': 200}]},
        'clock': lambda: next(ticks), 'utc_now': lambda: now, 'sleep': lambda _s: None,
    }
    base.update(overrides)
    return driver.QualificationDeps(**base)


def test_round_trip_emits_proof_accepted_by_live_promise_receipt_emitter(tmp_path, monkeypatch):
    aws = FakeQualificationAws()
    config = _config(tmp_path)
    proof = driver.run_qualification(config, _deps(aws))

    assert proof['stackName'] == STACK and proof['orchestrator'] == 'ecs'
    assert proof['redis'] is False and proof['defaultImage'] == 'postgis-only'
    assert proof['protectionProfile'] == 'Hardened' and proof['publishedAotImage'] is True
    assert proof['imageSizeBytes'] == 260_000_000 and proof['installConsoleMinutes'] == 14.5
    assert proof['cleanup'] == {'stackStatus': 'DELETE_COMPLETE', 'remainingResources': []}
    assert aws.create_count == aws.delete_count == 1
    assert aws.unprotected_db and aws.unprotected_alb
    encoded = (config.output_dir / 'launch-stack-proof.json').read_text(encoding='utf-8')
    assert '000000000000' not in encoded and 'arn:aws:' not in encoded

    monkeypatch.chdir(tmp_path)
    assert emitter.main([
        'launch-stack', '--proof-dir', 'proof', '--server-sha', SHA,
        '--server-digest', INDEX_DIGEST, '--lock-digest', LOCK_DIGEST, '--out', 'receipts',
    ]) == 0
    for receipt in ('cloud-launch-stack.json', 'cloud-image-aot.json',
                    'security-hardened.json', 'install-console.json', 'release-notes.md'):
        assert (tmp_path / 'receipts' / receipt).is_file()
    assert '<!-- honua-outcome:install-console --> 14.5 minutes' in (tmp_path / 'receipts/release-notes.md').read_text()


def test_teardown_runs_and_disables_protection_when_console_probe_fails(tmp_path):
    aws = FakeQualificationAws()
    config = _config(tmp_path)

    def fail_probe(_origin: str) -> dict:
        raise HttpsObservationError('missing HSTS')

    with pytest.raises(ObservationError, match='HTTPS endpoints'):
        driver.run_qualification(config, _deps(aws, probe_https=fail_probe))
    assert aws.create_count == aws.delete_count == 1
    assert aws.unprotected_db and aws.unprotected_alb
    assert not (config.output_dir / 'launch-stack-proof.json').exists()


def test_teardown_runs_when_running_task_digest_mismatches_candidate(tmp_path):
    aws = FakeQualificationAws()
    aws.data['describe-tasks']['tasks'][0]['containers'][0]['imageDigest'] = 'sha256:' + 'f' * 64
    config = _config(tmp_path)

    with pytest.raises(ObservationError, match='platform digest'):
        driver.run_qualification(config, _deps(aws))
    assert aws.create_count == aws.delete_count == 1
    assert not (config.output_dir / 'launch-stack-proof.json').exists()


@pytest.mark.parametrize('mode', ['elasticache', 'task-env'])
def test_redis_detected_fails_closed_and_tears_down_stack(tmp_path, mode):
    aws = FakeQualificationAws()
    if mode == 'elasticache':
        aws.data['list-stack-resources']['StackResourceSummaries'].append({
            'LogicalResourceId': 'Cache', 'ResourceType': 'AWS::ElastiCache::CacheCluster',
            'PhysicalResourceId': 'cache-1', 'ResourceStatus': 'CREATE_COMPLETE',
        })
    else:
        env = aws.data['describe-task-definition']['taskDefinition']['containerDefinitions'][0]['environment']
        env.append({'name': 'ConnectionStrings__Redis', 'value': 'redis://internal:6379'})
    config = _config(tmp_path)

    with pytest.raises(ObservationError, match='Redis'):
        driver.run_qualification(config, _deps(aws))
    assert aws.delete_count == 1
    assert not (config.output_dir / 'launch-stack-proof.json').exists()


@pytest.mark.parametrize('case', [
    ('stack_name', 'production', 'honua-gate-'),
    ('stack_name', 'honua-gate-', 'honua-gate-'),
    ('server_sha', 'not-a-sha', 'server_sha'),
    ('server_digest', 'sha256:short', 'server_digest'),
    ('platform_lock_digest', 'md5:abc', 'platform_lock_digest'),
    ('template_url', 'http://insecure.example/deploy.yaml', 'https'),
    ('cfn_role_arn', '', 'HONUA_GATE_CFN_ROLE_ARN'),
    ('console_bundle_version', 'off', 'console_bundle_version'),
])
def test_invalid_inputs_fail_closed_before_aws_calls(tmp_path, case):
    field, value, pattern = case
    aws = FakeQualificationAws()
    with pytest.raises(ObservationError, match=pattern):
        driver.run_qualification(_config(tmp_path, **{field: value}), _deps(aws))
    assert aws.create_count == 0 and not aws.calls


@pytest.mark.parametrize('case', [
    ({'platform_sizes': {'amd64': 250_000_000, 'arm64': 370_000_001}}, 'arm64'),
    ({'platform_sizes': {'amd64': 0, 'arm64': 260_000_000}}, 'amd64'),
    ({'platform_sizes': {'amd64': 250_000_000}}, 'every runnable platform'),
    ({'published_aot': False}, 'native-aot'),
    ({'default_image': 'with-redis'}, 'postgis-only'),
    ({'index_digest': 'sha256:' + '9' * 64}, 'differs'),
])
def test_bad_image_measurement_fails_closed_before_stack_create(tmp_path, case):
    bad_measurement, pattern = case
    aws = FakeQualificationAws()
    with pytest.raises(ObservationError, match=pattern):
        driver.run_qualification(_config(tmp_path),
                                 _deps(aws, inspect_image=lambda _c: _measurement(**bad_measurement)))
    assert aws.create_count == 0 and not aws.calls


def test_inspect_ghcr_image_validates_both_amd64_and_arm64_variants(tmp_path):
    pulled = []
    sizes = {AMD64_DIGEST: 240_000_000, ARM64_DIGEST: 265_000_000}

    def fake_docker(argv, **_kwargs):
        if argv[1] == 'pull':
            pulled.append((argv[4], argv[5]))
            return subprocess.CompletedProcess(argv, 0, '', '')
        ref = argv[3]
        digest = ref.split('@')[1]
        doc = [{'RepoDigests': [ref], 'Size': sizes[digest], 'Config': {'Labels': {
            'org.opencontainers.image.revision': SHA,
            'honua.runtime.compilation': 'native-aot',
            'honua.store.profile': 'postgis',
        }}}]
        return subprocess.CompletedProcess(argv, 0, json.dumps(doc), '')

    measured = driver.inspect_ghcr_image(
        _config(tmp_path),
        inspector=lambda _r, _d: {'amd64': AMD64_DIGEST, 'arm64': ARM64_DIGEST},
        runner=fake_docker,
    )
    assert pulled == [('linux/amd64', f'{IMAGE_REPO}@{AMD64_DIGEST}'),
                      ('linux/arm64', f'{IMAGE_REPO}@{ARM64_DIGEST}')]
    assert measured.platform_sizes == {'amd64': 240_000_000, 'arm64': 265_000_000}


def _break_hardened_control(aws: FakeQualificationAws, control: str) -> None:
    def drop_vpce() -> None:
        items = aws.data['list-stack-resources']['StackResourceSummaries']
        aws.data['list-stack-resources']['StackResourceSummaries'] = [
            r for r in items if r['ResourceType'] != 'AWS::EC2::VPCEndpoint'
        ]

    breakers = {
        'db-unprotected': lambda: setattr(aws, 'db_protected', False),
        'alb-public': lambda: setattr(aws, 'alb_scheme', 'internet-facing'),
        'no-waf-on-cf': lambda: setattr(aws, 'web_acl_id', ''),
        'no-vpce': drop_vpce,
    }
    breakers[control]()


@pytest.mark.parametrize('control', ['db-unprotected', 'alb-public', 'no-waf-on-cf', 'no-vpce'])
def test_missing_hardened_control_fails_closed_and_tears_down(tmp_path, control):
    aws = FakeQualificationAws()
    _break_hardened_control(aws, control)
    with pytest.raises(ObservationError):
        driver.run_qualification(_config(tmp_path), _deps(aws))
    assert aws.delete_count == 1


def test_retained_resource_during_teardown_fails_and_leaves_no_proof(tmp_path):
    aws = FakeQualificationAws()
    aws.retained = True
    config = _config(tmp_path)
    with pytest.raises(ObservationError, match='resource deletion'):
        driver.run_qualification(config, _deps(aws))
    assert aws.delete_count == 1
    assert not (config.output_dir / 'launch-stack-proof.json').exists()


def test_cleanup_only_is_noop_when_not_started_and_deletes_when_started(tmp_path):
    aws = FakeQualificationAws()
    config = _config(tmp_path)
    assert driver.cleanup_if_started(config, _deps(aws)) is None
    assert not aws.calls

    attempt = driver.StackAttempt(config.journal_path, STACK, '000000000000', 'us-east-1', config.attempt_id)
    with driver.open_attempt(attempt, IMAGE_REF) as owned:
        owned.begin_create()
        aws.create_stack(owned, IMAGE_REF)
        owned.observe(aws, stack_id=STACK_ID)
    cleanup = driver.cleanup_if_started(config, _deps(aws))
    assert cleanup['stackStatus'] == 'DELETE_COMPLETE' and cleanup['remainingResources'] == []
    assert aws.delete_count == 1

