"""Exercise the ECS lifecycle with CloudFormation and ECS fixtures."""
import copy

import pytest

from launch_stack_observations import ObservationError
from native_ecs_attempt import StackAttempt, open_attempt
from native_ecs_lifecycle import qualify_installation
from test_launch_stack_observations import DIGEST, IMAGE, STACK, FakeAws

STACK_ID = f'arn:aws:cloudformation:us-east-1:000000000000:stack/{STACK}/00000000-0000-0000-0000-000000000001'
CANDIDATE = {
    'serverImage': IMAGE,
    'imageDigest': IMAGE.split('@')[1],
    'platformDigests': {'amd64': 'sha256:' + 'c' * 64, 'arm64': DIGEST},
}


class LifecycleAws(FakeAws):
    def __init__(self):
        super().__init__()
        self.stack = self.data['describe-stacks']['Stacks'][0]
        self.stack['StackId'] = STACK_ID
        self.delete_count = 0
        self.retained = False
        self.drop_deleted_resource = False
        self.describe_failures = 0
        self.wait_interrupted = False
        self.creates = 0
        self.paginate_inventory = False
        self.inventory_continuations = 0

    def scope(self):
        return '000000000000', 'us-east-1'

    def create_stack(self, attempt, image):
        assert (attempt.stack_name, image) == (STACK, IMAGE)
        self.creates += 1
        self.stack['Tags'].extend(attempt.tags)
        return STACK_ID

    def __call__(self, service, operation, *args):
        if operation == 'list-stack-resources' and self.paginate_inventory:
            resources = self.data[operation]['StackResourceSummaries']
            if args[-2:] == ('--next-token', 'page-2'):
                self.inventory_continuations += 1
                return {'StackResourceSummaries': resources[1:]}
            return {'StackResourceSummaries': resources[:1], 'NextToken': 'page-2'}
        if service == 'cloudformation':
            assert args[-2:] in (('--stack-name', STACK_ID), ('--stack-name', STACK))
        if operation == 'describe-stacks' and self.describe_failures:
            self.describe_failures -= 1
            raise TimeoutError('describe timed out')
        if operation == 'wait':
            if self.wait_interrupted and args[0] == 'stack-create-complete':
                raise KeyboardInterrupt
            self.calls.append((service, operation, args))
            return {}
        if operation == 'delete-stack':
            self.calls.append((service, operation, args))
            self.delete_count += 1
            self.stack['StackStatus'] = 'DELETE_COMPLETE'
            for resource in self.data['list-stack-resources']['StackResourceSummaries']:
                resource['ResourceStatus'] = 'DELETE_SKIPPED' if self.retained else 'DELETE_COMPLETE'
            if self.drop_deleted_resource:
                self.data['list-stack-resources']['StackResourceSummaries'].pop()
            return {}
        return super().__call__(service, operation, *args)


@pytest.fixture
def attempt(tmp_path):
    return StackAttempt(tmp_path / 'attempt.json', STACK, '000000000000', 'us-east-1', 'run-12-attempt-1')


def test_returns_matching_platform_and_cleanup_evidence(attempt):
    aws = LifecycleAws()
    result = qualify_installation(aws, CANDIDATE, attempt=attempt,
                                  run_journey=lambda _: None)
    assert result['stackId'] == STACK_ID
    assert result['installation']['resolvedPlatformImageDigest'] == DIGEST
    assert result['cleanup']['stackStatus'] == 'DELETE_COMPLETE'
    assert result['cleanup']['remainingResources'] == []
    assert aws.delete_count == 1


def test_failed_journey_deletes_on_first_attempt_and_keeps_original_error(attempt):
    aws = LifecycleAws()

    def fail_journey(stack_id):
        assert stack_id == STACK_ID
        assert aws.delete_count == 0
        raise RuntimeError('journey failed')

    with pytest.raises(RuntimeError, match='journey failed'):
        qualify_installation(aws, CANDIDATE, attempt=attempt,
                             run_journey=fail_journey)
    assert aws.delete_count == 1
    operations = [call[1] for call in aws.calls]
    assert operations[0] == 'describe-stacks'
    assert operations[operations.index('delete-stack') - 1] == 'list-stack-resources'
    assert aws.calls[-1][1] == 'list-stack-resources'


def test_wrong_runtime_digest_skips_journey_and_still_deletes(attempt):
    aws = LifecycleAws()
    candidate = copy.deepcopy(CANDIDATE)
    candidate['platformDigests']['arm64'] = 'sha256:' + 'd' * 64
    with pytest.raises(ObservationError, match='platform digest'):
        qualify_installation(aws, candidate, attempt=attempt,
                             run_journey=lambda _: pytest.fail('journey ran'))
    assert aws.delete_count == 1


def test_retained_resource_fails_cleanup(attempt):
    aws = LifecycleAws()
    aws.retained = True
    with pytest.raises(ObservationError, match='resource deletion'):
        qualify_installation(aws, CANDIDATE, attempt=attempt,
                             run_journey=lambda _: None)
    assert aws.delete_count == 1


def test_inconsistent_candidate_pin_fails_before_creation(attempt):
    aws = LifecycleAws()
    candidate = {**CANDIDATE, 'imageDigest': DIGEST}
    with pytest.raises(ObservationError, match='candidate image'):
        qualify_installation(aws, candidate, attempt=attempt,
                             run_journey=lambda _: None)
    assert not aws.calls
    assert aws.creates == 0


def test_incomplete_terminal_inventory_fails_cleanup(attempt):
    aws = LifecycleAws()
    aws.drop_deleted_resource = True
    with pytest.raises(ObservationError, match='Incomplete resource deletion inventory'):
        qualify_installation(aws, CANDIDATE, attempt=attempt,
                             run_journey=lambda _: None)
    assert aws.delete_count == 1


def test_lost_create_response_recovers_by_name(attempt):
    aws = LifecycleAws()

    def create(identity, image):
        LifecycleAws.create_stack(aws, identity, image)
        raise TimeoutError('create acknowledgement lost')

    aws.create_stack = create
    result = qualify_installation(aws, CANDIDATE, attempt=attempt,
                                  run_journey=lambda _: None)
    assert result['stackId'] == STACK_ID
    assert aws.creates == aws.delete_count == 1
    assert aws.calls[0][2] == ('--stack-name', STACK)


def test_retries_transient_first_describe(attempt):
    aws = LifecycleAws()
    aws.describe_failures = 1
    qualify_installation(aws, CANDIDATE, attempt=attempt,
                         run_journey=lambda _: None)
    assert aws.delete_count == 1


def test_interrupted_wait_cleans_up_by_arn(attempt):
    aws = LifecycleAws()
    aws.wait_interrupted = True
    with pytest.raises(KeyboardInterrupt):
        qualify_installation(aws, CANDIDATE, attempt=attempt,
                             run_journey=lambda _: pytest.fail('journey ran'))
    assert aws.delete_count == 1


def test_replay_recovers_interrupted_create_without_creating_again(attempt):
    aws = LifecycleAws()

    def create(identity, image):
        LifecycleAws.create_stack(aws, identity, image)
        raise KeyboardInterrupt

    aws.create_stack = create
    with pytest.raises(KeyboardInterrupt):
        qualify_installation(aws, CANDIDATE, attempt=attempt,
                             run_journey=lambda _: pytest.fail('journey ran'))
    with pytest.raises(ObservationError, match='Recovered interrupted attempt'):
        qualify_installation(aws, CANDIDATE, attempt=attempt,
                             run_journey=lambda _: pytest.fail('replay certified the old attempt'))
    assert aws.creates == aws.delete_count == 1


def test_foreign_same_name_is_left_untouched(attempt):
    aws = LifecycleAws()

    def create(identity, image):
        LifecycleAws.create_stack(aws, identity, image)
        aws.stack['Tags'] = [tag for tag in aws.stack['Tags'] if tag['Key'] != 'HonuaGateOwner']
        raise TimeoutError('create acknowledgement lost')

    aws.create_stack = create
    with pytest.raises(ObservationError, match='ownership'):
        qualify_installation(aws, CANDIDATE, attempt=attempt,
                             run_journey=lambda _: pytest.fail('journey ran'))
    assert aws.delete_count == 0


def test_replay_requires_original_pre_delete_inventory(attempt):
    aws = LifecycleAws()
    aws.drop_deleted_resource = True
    for _ in range(2):
        with pytest.raises(ObservationError, match='Incomplete resource deletion inventory'):
            qualify_installation(aws, CANDIDATE, attempt=attempt,
                                 run_journey=lambda _: None)
    assert aws.creates == aws.delete_count == 1


def test_wrong_effective_scope_fails_before_create(attempt):
    aws = LifecycleAws()
    aws.scope = lambda: ('000000000000', 'us-west-2')
    with pytest.raises(ObservationError, match='effective account or region'):
        qualify_installation(aws, CANDIDATE, attempt=attempt,
                             run_journey=lambda _: pytest.fail('journey ran'))
    assert aws.creates == aws.delete_count == 0


def test_prepared_journal_can_start_create(attempt):
    aws = LifecycleAws()
    with open_attempt(attempt, IMAGE) as prepared:
        token = prepared.token

    def create(owned, image):
        assert owned.token == token
        return LifecycleAws.create_stack(aws, owned, image)

    aws.create_stack = create
    qualify_installation(aws, CANDIDATE, attempt=attempt,
                         run_journey=lambda _: None)
    assert aws.creates == aws.delete_count == 1


def test_retries_describe_during_install_observation(attempt, monkeypatch):
    monkeypatch.setattr('native_ecs_attempt.time.sleep', lambda _: None)

    class TransientInstallRead(LifecycleAws):
        def __call__(self, service, operation, *args):
            result = super().__call__(service, operation, *args)
            if operation == 'wait' and args[0] == 'stack-create-complete':
                self.describe_failures = 1
            return result

    aws = TransientInstallRead()
    qualify_installation(aws, CANDIDATE, attempt=attempt,
                         run_journey=lambda _: None)
    assert aws.creates == aws.delete_count == 1


def test_cleanup_follows_paginated_resource_inventory(attempt):
    aws = LifecycleAws()

    def fail_journey(_):
        aws.paginate_inventory = True
        raise RuntimeError('journey failed')

    with pytest.raises(RuntimeError, match='journey failed'):
        qualify_installation(aws, CANDIDATE, attempt=attempt,
                             run_journey=fail_journey)
    assert aws.delete_count == 1
    assert aws.inventory_continuations == 2
