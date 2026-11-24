"""Lifecycle core for a disposable native ECS qualification stack."""
import re
from datetime import datetime, timezone

from image_platforms import DIGEST_RE, image_platform_digests
from launch_stack_observations import observe_stack, one, require

STACK_ARN = re.compile(
    r'arn:aws:cloudformation:[a-z0-9-]+:[0-9]{12}:stack/(honua-gate-[A-Za-z0-9-]+)/[0-9a-f-]+'
)


def resource_summaries(aws, stack_arg):
    response = aws('cloudformation', 'list-stack-resources', *stack_arg)
    resources = response.get('StackResourceSummaries')
    require(isinstance(resources, list) and not response.get('NextToken'),
            'Incomplete resource deletion response')
    return resources


def resource_identities(resources):
    return {(resource['LogicalResourceId'], resource.get('PhysicalResourceId')) for resource in resources}


def cleanup_stack(aws, stack_id):
    stack_arg = ('--stack-name', stack_id)
    try:
        known = resource_identities(resource_summaries(aws, stack_arg))
    finally:
        aws('cloudformation', 'delete-stack', *stack_arg)
        aws('cloudformation', 'wait', 'stack-delete-complete', *stack_arg)
    response = aws('cloudformation', 'describe-stacks', *stack_arg)
    stack = one(response.get('Stacks'), 'Missing or ambiguous deleted stack')
    require(stack.get('StackId') == stack_id and stack.get('StackStatus') == 'DELETE_COMPLETE',
            'Stack deletion was not confirmed')
    resources = resource_summaries(aws, stack_arg)
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


def qualify_installation(aws, candidate, *, stack_name, create_stack, run_journey):
    """Create an owned Redis-off stack and return its installation and deletion evidence.

    The injected creator binds the approved template and parameters and returns only
    the ARN of the stack it created for this attempt. The journey raises on failure.
    """
    image = candidate.get('serverImage')
    index_digest = candidate.get('imageDigest')
    require(isinstance(index_digest, str) and DIGEST_RE.fullmatch(index_digest)
            and isinstance(image, str) and image.count('@') == 1
            and bool(image.split('@')[0]) and image.endswith(f'@{index_digest}'),
            'Invalid candidate image pin')
    platform_digests = image_platform_digests(candidate.get('platformDigests'), index_digest=index_digest)
    require(isinstance(stack_name, str) and re.fullmatch(r'honua-gate-[A-Za-z0-9-]+', stack_name),
            'Expected an attempt-specific disposable stack name')
    stack_id = create_stack(stack_name, image)
    match = STACK_ARN.fullmatch(stack_id) if isinstance(stack_id, str) else None
    require(match is not None and match[1] == stack_name, 'Creator did not return the expected stack ARN')
    response = aws('cloudformation', 'describe-stacks', '--stack-name', stack_id)
    stack = one(response.get('Stacks'), 'Missing or ambiguous created stack')
    require(stack.get('StackId') == stack_id and stack.get('StackName') == stack_name
            and {'Key': 'HonuaGate', 'Value': 'true'} in stack.get('Tags', []),
            'Could not confirm the created stack identity and gate tag')
    try:
        aws('cloudformation', 'wait', 'stack-create-complete', '--stack-name', stack_id)
        observation = observe_stack(aws, stack_name, stack_id=stack_id)
        installation = verify_installation(observation, image, platform_digests)
        run_journey(stack_id)
    finally:
        cleanup = cleanup_stack(aws, stack_id)
    return {'stackId': stack_id, 'installedAt': installation['completedAt'],
            'installation': installation, 'cleanup': cleanup}
