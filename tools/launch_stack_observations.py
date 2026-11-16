"""Read-only ECS observations for the authenticated Launch Stack producer.

The caller supplies a region-bound AWS JSON client, credentials and run lifecycle.
These observations do not certify AOT, security controls or candidate provenance.
"""
import re
from datetime import datetime, timezone
from ipaddress import IPv4Address


class ObservationError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise ObservationError(reason)


def one(values, reason):
    require(isinstance(values, list) and len(values) == 1, reason)
    return values[0]


def stack_state(aws, stack_name):
    response = aws('cloudformation', 'describe-stacks', '--stack-name', stack_name)
    stack = one(response.get('Stacks'), 'Missing or ambiguous stack')
    require(stack.get('StackName') == stack_name, 'Stack identity differs')
    require(stack.get('StackStatus') in ('CREATE_COMPLETE', 'UPDATE_COMPLETE'),
            'Stack has not completed the requested deployment')
    require({'Key': 'HonuaGate', 'Value': 'true'} in stack.get('Tags', []),
            'Stack lacks the required gate tag')
    return stack


def stack_resources(aws, stack_name):
    response = aws('cloudformation', 'list-stack-resources', '--stack-name', stack_name)
    require(not response.get('NextToken'), 'Incomplete stack resource response')
    resources = {}
    for logical, kind in (('Cluster', 'AWS::ECS::Cluster'), ('Service', 'AWS::ECS::Service'),
                          ('ServerTask', 'AWS::ECS::TaskDefinition'),
                          ('Targets', 'AWS::ElasticLoadBalancingV2::TargetGroup')):
        resource = one([r for r in response.get('StackResourceSummaries', [])
                        if r.get('LogicalResourceId') == logical], f'Missing stack resource {logical}')
        require(resource.get('ResourceType') == kind and resource.get('PhysicalResourceId'),
                f'Invalid stack resource {logical}')
        resources[logical] = resource['PhysicalResourceId']
    return resources


def service_state(aws, resources):
    response = aws('ecs', 'describe-services', '--cluster', resources['Cluster'],
                   '--services', resources['Service'])
    require(not response.get('failures'), 'ECS service lookup failed')
    service = one(response.get('services'), 'Missing or ambiguous ECS service')
    require(resources['Service'] in (service.get('serviceArn'), service.get('serviceName')),
            'ECS service identity differs')
    require(service.get('status') == 'ACTIVE' and service.get('launchType') == 'FARGATE',
            'Service is not active Fargate')
    desired = service.get('desiredCount')
    require(type(desired) is int and desired > 0 and service.get('runningCount') == desired
            and service.get('pendingCount') == 0, 'Service has not converged')
    require(service.get('taskDefinition') == resources['ServerTask'], 'Service uses another task revision')
    require({'targetGroupArn': resources['Targets'], 'containerName': 'honua', 'containerPort': 8080}
            in service.get('loadBalancers', []), 'Service is not bound to the stack readiness target group')
    deployment = one(service.get('deployments'), 'Service has mixed deployments')
    require(deployment.get('status') == 'PRIMARY' and deployment.get('rolloutState') == 'COMPLETED'
            and deployment.get('taskDefinition') == resources['ServerTask'], 'Deployment is incomplete')
    return service


def task_definition(aws, resources):
    response = aws('ecs', 'describe-task-definition', '--task-definition', resources['ServerTask'])
    definition = response.get('taskDefinition', {})
    require(definition.get('taskDefinitionArn') == resources['ServerTask'], 'Task definition identity differs')
    require(definition.get('status') == 'ACTIVE' and 'FARGATE' in definition.get('requiresCompatibilities', [])
            and definition.get('networkMode') == 'awsvpc', 'Task definition is not active Fargate')
    platform = definition.get('runtimePlatform', {})
    require(platform.get('cpuArchitecture') in ('ARM64', 'X86_64')
            and platform.get('operatingSystemFamily') == 'LINUX', 'Missing Linux platform identity')
    container = server_container(definition.get('containerDefinitions', []))
    image = container.get('image', '')
    require(container.get('essential') is True and
            re.fullmatch(r'[a-zA-Z0-9./:_-]+@sha256:[0-9a-f]{64}', image),
            'Gate server image must be digest pinned')
    return {'declaredImage': image, 'cpuArchitecture': platform['cpuArchitecture']}


def running_tasks(aws, resources):
    response = aws('ecs', 'list-tasks', '--cluster', resources['Cluster'],
                   '--service-name', resources['Service'], '--desired-status', 'RUNNING')
    require(not response.get('nextToken') and not response.get('NextToken'),
            'Incomplete task list response')
    arns = response.get('taskArns', [])
    require(arns and len(set(arns)) == len(arns), 'Missing or duplicate running task identities')
    tasks = []
    for offset in range(0, len(arns), 100):
        batch = arns[offset:offset + 100]
        response = aws('ecs', 'describe-tasks', '--cluster', resources['Cluster'], '--tasks', *batch)
        require(not response.get('failures'), 'ECS task lookup failed')
        observed = response.get('tasks', [])
        identities = [task.get('taskArn') for task in observed]
        require(len(identities) == len(batch) and set(identities) == set(batch),
                'Incomplete or duplicate task observations')
        tasks.extend(observed)
    return tasks


def server_container(containers):
    return one([container for container in containers if container.get("name") == "honua"],
               "Missing or ambiguous server container")


def server_digest(task, resources):
    require(task.get('taskDefinitionArn') == resources['ServerTask'], 'Running task uses another revision')
    require(task.get('launchType') == 'FARGATE' and task.get('lastStatus') == 'RUNNING'
            and task.get('desiredStatus') == 'RUNNING' and task.get('healthStatus') != 'UNHEALTHY',
            'Running task is not healthy Fargate')
    container = server_container(task.get('containers', []))
    require(container.get('lastStatus') == 'RUNNING' and container.get('healthStatus') != 'UNHEALTHY',
            'Running server container is not healthy')
    digest = container.get('imageDigest', '')
    require(re.fullmatch(r'sha256:[0-9a-f]{64}', digest), 'Missing observed server image digest')
    return digest



def task_snapshot(tasks, resources):
    observations = {}
    for task in tasks:
        digest = server_digest(task, resources)
        container = server_container(task.get('containers', []))
        observations[task['taskArn']] = {
            'digest': digest, 'health': task.get('healthStatus'),
            'containerHealth': container.get('healthStatus'),
            'network': container.get('networkInterfaces'),
        }
    return observations


def readiness_group(aws, resources):
    response = aws('elbv2', 'describe-target-groups', '--target-group-arns', resources['Targets'])
    group = one(response.get('TargetGroups'), 'Missing readiness target group')
    require(group.get('TargetGroupArn') == resources['Targets'] and group.get('TargetType') == 'ip'
            and group.get('Port') == 8080 and group.get('Protocol') == 'HTTP',
            'Readiness target group identity differs')
    require(group.get('HealthCheckEnabled') is True and group.get('HealthCheckPath') == '/healthz/ready'
            and group.get('HealthCheckProtocol') == 'HTTP' and group.get('HealthCheckPort') == 'traffic-port'
            and group.get('Matcher', {}).get('HttpCode') == '200', 'Readiness health check differs')


def ready_targets(aws, resources, tasks):
    readiness_group(aws, resources)
    addresses = []
    for task in tasks:
        container = server_container(task.get('containers', []))
        interface = one(container.get('networkInterfaces'), 'Missing server network interface')
        try:
            addresses.append(str(IPv4Address(interface.get('privateIpv4Address', ''))))
        except ValueError:
            raise ObservationError('Invalid server network address') from None
    require(len(set(addresses)) == len(tasks), 'Duplicate server network identities')
    response = aws('elbv2', 'describe-target-health', '--target-group-arn', resources['Targets'])
    targets = response.get('TargetHealthDescriptions', [])
    require(len(targets) == len(addresses), 'Readiness target count differs from running tasks')
    observed = []
    for target in targets:
        require(target.get('TargetHealth', {}).get('State') == 'healthy'
                and target.get('Target', {}).get('Port') == 8080, 'Readiness target is not healthy')
        observed.append(target['Target'].get('Id'))
    require(set(observed) == set(addresses), 'Readiness targets do not match running server tasks')
    readiness_group(aws, resources)


def observe_stack(aws, stack_name):
    require(isinstance(stack_name, str) and re.fullmatch(r'honua-gate-[a-zA-Z0-9-]+', stack_name),
            'Only disposable gate stacks may be observed')
    started = datetime.now(timezone.utc).isoformat()
    stack = stack_state(aws, stack_name)
    resources = stack_resources(aws, stack_name)
    service = service_state(aws, resources)
    definition = task_definition(aws, resources)
    tasks = running_tasks(aws, resources)
    require(len(tasks) == service['desiredCount'], 'Running task count differs from desired capacity')
    snapshot = task_snapshot(tasks, resources)
    digests = {task['digest'] for task in snapshot.values()}
    require(len(digests) == 1, 'Running tasks have mixed image digests')
    ready_targets(aws, resources, tasks)
    require(task_snapshot(running_tasks(aws, resources), resources) == snapshot,
            'Tasks changed during readiness observation')
    require(service_state(aws, resources) == service, 'Service changed during observation')
    require(stack_state(aws, stack_name) == stack, 'Stack changed during observation')
    return {'stackName': stack_name, 'startedAt': started,
            'completedAt': datetime.now(timezone.utc).isoformat(), **definition,
            'observedPlatformImageDigest': digests.pop(), 'healthyTaskCount': len(tasks)}
