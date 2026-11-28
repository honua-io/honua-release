"""Lifecycle core for a disposable native ECS qualification stack."""
from datetime import datetime, timezone

from image_platforms import DIGEST_RE, image_platform_digests
from launch_stack_observations import ObservationError, observe_stack, one, require, resource_summaries

from native_ecs_attempt import AmbiguousStackCreate, describe_with_retry, open_attempt


def resource_identities(resources):
    return {(resource['LogicalResourceId'], resource.get('PhysicalResourceId')) for resource in resources}


def cleanup_stack(aws, owned, stack):
    stack_id = stack['StackId']
    status = stack['StackStatus']
    stack_arg = ('--stack-name', stack_id)
    deleting = status in ('DELETE_IN_PROGRESS', 'DELETE_COMPLETE')
    known = owned.deletion_inventory(
        stack_id, lambda: resource_identities(resource_summaries(aws, stack_id)),
        already_deleting=deleting,
    )
    if not deleting:
        prepare = getattr(aws, 'prepare_delete', None)
        if prepare is not None:
            prepare(stack_id)
        aws('cloudformation', 'delete-stack', *stack_arg)
    if status != 'DELETE_COMPLETE':
        aws('cloudformation', 'wait', 'stack-delete-complete', *stack_arg)
    response = describe_with_retry(aws, stack_id)
    stack = one(response.get('Stacks'), 'Missing or ambiguous deleted stack')
    require(stack.get('StackId') == stack_id and stack.get('StackStatus') == 'DELETE_COMPLETE',
            'Stack deletion was not confirmed')
    resources = resource_summaries(aws, stack_id)
    require(known <= resource_identities(resources), 'Incomplete resource deletion inventory')
    require(all(resource.get('ResourceStatus') == 'DELETE_COMPLETE' for resource in resources),
            'Stack resource deletion was not confirmed')
    return {'stackId': stack_id, 'stackStatus': 'DELETE_COMPLETE', 'remainingResources': [],
            'observedAt': datetime.now(timezone.utc).isoformat()}


def verify_installation(installation, image, platform_digests):
    require(installation['declaredImage'] == image, 'Installed image differs from candidate image')
    architecture = {'X86_64': 'amd64', 'ARM64': 'arm64'}.get(installation['cpuArchitecture'])
    expected = platform_digests.get(architecture)
    require(expected is not None and installation['observedPlatformImageDigest'] == expected,
            'Running platform digest differs from candidate pin')
    return {**installation, 'resolvedPlatformImageDigest': expected}


def qualify_installation(aws, candidate, *, attempt, run_journey):
    """Create an owned Redis-off stack and return its installation and deletion evidence.

    The creator uses the owned attempt's name, tags and token (as the CreateStack
    client request token), plus the approved template, parameters and gate tag.
    The AWS adapter owns creation, reads and deletion using one session and
    region. Its scope() returns the verified caller account and effective region. The attempt
    journal must survive runner replay.
    Replay performs cleanup only, because it cannot attest a complete fresh journey.
    """
    image = candidate.get('serverImage')
    index_digest = candidate.get('imageDigest')
    require(isinstance(index_digest, str) and DIGEST_RE.fullmatch(index_digest)
            and isinstance(image, str) and image.count('@') == 1
            and bool(image.split('@')[0]) and image.endswith(f'@{index_digest}'),
            'Invalid candidate image pin')
    platform_digests = image_platform_digests(candidate.get('platformDigests'), index_digest=index_digest)
    require(aws.scope() == (attempt.account, attempt.region),
            'AWS adapter effective account or region differs from the admitted attempt')
    with open_attempt(attempt, image) as owned:
        stack_id = owned.retained_arn()
        if owned.replay:
            cleanup_stack(aws, owned, owned.observe(aws, stack_id=stack_id))
            raise ObservationError('Recovered interrupted attempt and verified cleanup')
        owned.begin_create()
        try:
            stack_id = aws.create_stack(owned, image)
        except (AmbiguousStackCreate, TimeoutError, ConnectionError):
            stack_id = owned.observe(aws)['StackId']
        else:
            owned.observe(aws, stack_id=stack_id)
        try:
            aws('cloudformation', 'wait', 'stack-create-complete', '--stack-name', stack_id)
            observation = observe_stack(
                aws, owned.stack_name, stack_id=stack_id,
                describe=lambda identity: describe_with_retry(aws, identity),
            )
            installation = verify_installation(observation, image, platform_digests)
            run_journey(stack_id)
        finally:
            cleanup = cleanup_stack(aws, owned, owned.observe(aws, stack_id=stack_id))
    return {'stackId': stack_id, 'installedAt': installation['completedAt'],
            'installation': installation, 'cleanup': cleanup}
