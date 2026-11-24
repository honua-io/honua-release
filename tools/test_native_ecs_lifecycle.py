"""Exercise the ECS lifecycle with CloudFormation and ECS fixtures."""
import copy

import pytest

from launch_stack_observations import ObservationError
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

    def __call__(self, service, operation, *args):
        if service == 'cloudformation':
            assert args[-2:] == ('--stack-name', STACK_ID)
        if operation == 'wait':
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


def test_returns_matching_platform_and_cleanup_evidence():
    aws = LifecycleAws()

    def create_stack(name, image):
        assert (name, image) == (STACK, IMAGE)
        return STACK_ID

    result = qualify_installation(aws, CANDIDATE, stack_name=STACK,
                                  create_stack=create_stack, run_journey=lambda _: None)
    assert result['stackId'] == STACK_ID
    assert result['installation']['resolvedPlatformImageDigest'] == DIGEST
    assert result['cleanup']['stackStatus'] == 'DELETE_COMPLETE'
    assert result['cleanup']['remainingResources'] == []
    assert aws.delete_count == 1


def test_failed_journey_deletes_on_first_attempt_and_keeps_original_error():
    aws = LifecycleAws()

    def fail_journey(stack_id):
        assert stack_id == STACK_ID
        assert aws.delete_count == 0
        raise RuntimeError('journey failed')

    with pytest.raises(RuntimeError, match='journey failed'):
        qualify_installation(aws, CANDIDATE, stack_name=STACK,
                             create_stack=lambda name, image: STACK_ID,
                             run_journey=fail_journey)
    assert aws.delete_count == 1
    operations = [call[1] for call in aws.calls]
    assert operations[0] == 'describe-stacks'
    assert operations[operations.index('delete-stack') - 1] == 'list-stack-resources'
    assert aws.calls[-1][1] == 'list-stack-resources'


def test_wrong_runtime_digest_skips_journey_and_still_deletes():
    aws = LifecycleAws()
    candidate = copy.deepcopy(CANDIDATE)
    candidate['platformDigests']['arm64'] = 'sha256:' + 'd' * 64
    with pytest.raises(ObservationError, match='platform digest'):
        qualify_installation(aws, candidate, stack_name=STACK,
                             create_stack=lambda name, image: STACK_ID,
                             run_journey=lambda _: pytest.fail('journey ran'))
    assert aws.delete_count == 1


def test_retained_resource_fails_cleanup():
    aws = LifecycleAws()
    aws.retained = True
    with pytest.raises(ObservationError, match='resource deletion'):
        qualify_installation(aws, CANDIDATE, stack_name=STACK,
                             create_stack=lambda name, image: STACK_ID,
                             run_journey=lambda _: None)
    assert aws.delete_count == 1


def test_inconsistent_candidate_pin_fails_before_creation():
    aws = LifecycleAws()
    candidate = {**CANDIDATE, 'imageDigest': DIGEST}
    with pytest.raises(ObservationError, match='candidate image'):
        qualify_installation(aws, candidate, stack_name=STACK,
                             create_stack=lambda name, image: pytest.fail('created'),
                             run_journey=lambda _: None)
    assert not aws.calls


def test_incomplete_terminal_inventory_fails_cleanup():
    aws = LifecycleAws()
    aws.drop_deleted_resource = True
    with pytest.raises(ObservationError, match='Incomplete resource deletion inventory'):
        qualify_installation(aws, CANDIDATE, stack_name=STACK,
                             create_stack=lambda name, image: STACK_ID, run_journey=lambda _: None)
    assert aws.delete_count == 1
