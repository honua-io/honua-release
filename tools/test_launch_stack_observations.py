"""Offline fault cases; fixtures are not live deployment evidence."""
import copy

import pytest

from launch_stack_observations import ObservationError, observe_stack


STACK = 'honua-gate-observation-test'
REVISION = 'arn:aws:ecs:us-east-1:000000000000:task-definition/server:3'
DIGEST = 'sha256:' + 'a' * 64
IMAGE = 'registry.example/honua@sha256:' + 'b' * 64


def responses():
    return {
        'describe-stacks': {'Stacks': [{'StackName': STACK, 'StackStatus': 'CREATE_COMPLETE',
                                       'Tags': [{'Key': 'HonuaGate', 'Value': 'true'}],
                                       'Parameters': [{'ParameterKey': 'Private', 'ParameterValue': 'secret'}]}]},
        'list-stack-resources': {'StackResourceSummaries': [
            {'LogicalResourceId': logical, 'ResourceType': kind, 'PhysicalResourceId': physical}
            for logical, kind, physical in [('Cluster', 'AWS::ECS::Cluster', 'cluster'),
                                            ('Service', 'AWS::ECS::Service', 'service'),
                                            ('ServerTask', 'AWS::ECS::TaskDefinition', REVISION),
                                            ('Targets', 'AWS::ElasticLoadBalancingV2::TargetGroup', 'targets')]]},
        'describe-services': {'services': [{'serviceName': 'service', 'status': 'ACTIVE',
                                          'launchType': 'FARGATE', 'desiredCount': 1,
                                          'runningCount': 1, 'pendingCount': 0, 'taskDefinition': REVISION,
                                          'loadBalancers': [{'targetGroupArn': 'targets', 'containerName': 'honua',
                                                             'containerPort': 8080}],
                                          'deployments': [{'status': 'PRIMARY', 'rolloutState': 'COMPLETED',
                                                           'taskDefinition': REVISION}]}]},
        'describe-task-definition': {'taskDefinition': {
            'taskDefinitionArn': REVISION, 'status': 'ACTIVE', 'networkMode': 'awsvpc',
            'requiresCompatibilities': ['FARGATE'],
            'runtimePlatform': {'cpuArchitecture': 'ARM64', 'operatingSystemFamily': 'LINUX'},
            'containerDefinitions': [{'name': 'honua', 'essential': True, 'image': IMAGE,
                                      'environment': [{'name': 'PASSWORD', 'value': 'secret'}]}]}},
        'list-tasks': {'taskArns': ['task-1']},
        'describe-tasks': {'tasks': [{'taskArn': 'task-1', 'taskDefinitionArn': REVISION,
                                     'launchType': 'FARGATE', 'lastStatus': 'RUNNING',
                                     'desiredStatus': 'RUNNING', 'healthStatus': 'UNKNOWN',
                                     'containers': [{'name': 'honua', 'lastStatus': 'RUNNING',
                                                     'networkInterfaces': [{'privateIpv4Address': '10.0.0.7'}],
                                                     'healthStatus': 'UNKNOWN', 'imageDigest': DIGEST}]}]},
        'describe-target-health': {'TargetHealthDescriptions': [
            {'Target': {'Id': '10.0.0.7', 'Port': 8080}, 'TargetHealth': {'State': 'healthy'}}]},
        'describe-target-groups': {'TargetGroups': [{'TargetGroupArn': 'targets', 'TargetType': 'ip',
            'Port': 8080, 'Protocol': 'HTTP', 'HealthCheckEnabled': True, 'HealthCheckPath': '/healthz/ready',
            'HealthCheckProtocol': 'HTTP', 'HealthCheckPort': 'traffic-port', 'Matcher': {'HttpCode': '200'}}]},
    }


class FakeAws:
    def __init__(self):
        self.data = responses()
        self.calls = []

    def __call__(self, service, operation, *args):
        self.calls.append((service, operation, args))
        expected = {'describe-stacks': 'cloudformation', 'list-stack-resources': 'cloudformation',
                    'describe-target-health': 'elbv2', 'describe-target-groups': 'elbv2'}
        assert service == expected.get(operation, 'ecs')
        return copy.deepcopy(self.data[operation])


def test_observes_platform_digest_without_leaking_raw_responses_or_claiming_certification():
    aws = FakeAws()
    result = observe_stack(aws, STACK)
    assert set(result) == {'stackName', 'startedAt', 'completedAt', 'declaredImage',
                           'cpuArchitecture', 'observedPlatformImageDigest', 'healthyTaskCount'}
    assert result['declaredImage'] == IMAGE
    assert result['observedPlatformImageDigest'] == DIGEST
    assert result['cpuArchitecture'] == 'ARM64'
    assert result['healthyTaskCount'] == 1
    assert result['startedAt'] <= result['completedAt']
    assert 'secret' not in str(result)
    assert all(operation.startswith(('describe-', 'list-')) for _, operation, _ in aws.calls)


@pytest.mark.parametrize('name', ['production', '', 'honua-gate-', None])
def test_refuses_non_gate_names_before_any_aws_call(name):
    aws = FakeAws()
    with pytest.raises(ObservationError, match='disposable'):
        observe_stack(aws, name)
    assert not aws.calls


@pytest.mark.parametrize('field,value', [('StackStatus', 'UPDATE_IN_PROGRESS'),
                                        ('StackStatus', 'UPDATE_ROLLBACK_COMPLETE'),
                                        ('StackName', 'another-stack'), ('Tags', []),
                                        ('Tags', [{'Key': 'honua:qualification', 'Value': 'true'}]),
                                        ('Tags', [{'Key': 'HonuaGate', 'Value': 'false'}])])
def test_refuses_wrong_unqualified_or_rolled_back_stack(field, value):
    aws = FakeAws()
    aws.data['describe-stacks']['Stacks'][0][field] = value
    with pytest.raises(ObservationError):
        observe_stack(aws, STACK)


@pytest.mark.parametrize('field,value', [('runningCount', 0), ('pendingCount', 1),
                                        ('desiredCount', 0), ('launchType', 'EC2'),
                                        ('taskDefinition', REVISION + '0'),
                                        ('serviceName', 'other-service'), ('deployments', [])])
def test_refuses_nonconverged_or_wrong_service(field, value):
    aws = FakeAws()
    aws.data['describe-services']['services'][0][field] = value
    with pytest.raises(ObservationError):
        observe_stack(aws, STACK)


@pytest.mark.parametrize('operation', ['describe-services', 'describe-tasks'])
def test_refuses_partial_aws_failures(operation):
    aws = FakeAws()
    aws.data[operation]['failures'] = [{'reason': 'MISSING'}]
    with pytest.raises(ObservationError, match='lookup failed'):
        observe_stack(aws, STACK)


@pytest.mark.parametrize('operation,key', [('list-stack-resources', 'NextToken'),
                                         ('list-tasks', 'nextToken'), ('list-tasks', 'NextToken')])
def test_refuses_unconsumed_pagination(operation, key):
    aws = FakeAws()
    aws.data[operation][key] = 'more'
    with pytest.raises(ObservationError, match='Incomplete'):
        observe_stack(aws, STACK)


@pytest.mark.parametrize('field,value', [('taskDefinitionArn', REVISION + '0'),
                                        ('launchType', 'EC2'), ('healthStatus', 'UNHEALTHY'),
                                        ('lastStatus', 'STOPPED'), ('desiredStatus', 'STOPPED')])
def test_refuses_stale_or_unhealthy_tasks(field, value):
    aws = FakeAws()
    aws.data['describe-tasks']['tasks'][0][field] = value
    with pytest.raises(ObservationError):
        observe_stack(aws, STACK)


@pytest.mark.parametrize('field,value', [('imageDigest', ''), ('imageDigest', 'latest'),
                                        ('healthStatus', 'UNHEALTHY'), ('lastStatus', 'STOPPED')])
def test_refuses_missing_digest_or_unhealthy_container(field, value):
    aws = FakeAws()
    aws.data['describe-tasks']['tasks'][0]['containers'][0][field] = value
    with pytest.raises(ObservationError):
        observe_stack(aws, STACK)


def test_refuses_mutable_image_even_with_a_resolved_running_digest():
    aws = FakeAws()
    aws.data['describe-task-definition']['taskDefinition']['containerDefinitions'][0]['image'] = 'repo:latest'
    with pytest.raises(ObservationError, match='digest pinned'):
        observe_stack(aws, STACK)


@pytest.mark.parametrize('mode', ['missing', 'duplicate', 'mixed'])
def test_refuses_incomplete_duplicate_and_mixed_running_task_sets(mode):
    aws = FakeAws()
    task = aws.data['describe-tasks']['tasks'][0]
    if mode == 'missing':
        aws.data['describe-tasks']['tasks'] = []
    elif mode == 'duplicate':
        aws.data['describe-tasks']['tasks'].append(copy.deepcopy(task))
    else:
        second = copy.deepcopy(task)
        second['taskArn'] = 'task-2'
        second['containers'][0]['imageDigest'] = 'sha256:' + 'c' * 64
        aws.data['describe-tasks']['tasks'].append(second)
        aws.data['list-tasks']['taskArns'].append('task-2')
        service = aws.data['describe-services']['services'][0]
        service['desiredCount'] = service['runningCount'] = 2
    with pytest.raises(ObservationError):
        observe_stack(aws, STACK)


@pytest.mark.parametrize('operation', ['describe-services', 'describe-stacks'])
def test_refuses_deployment_changes_during_observation(operation):
    aws = FakeAws()

    def changing(service, action, *args):
        response = aws(service, action, *args)
        if action == operation and sum(call[1] == action for call in aws.calls) == 2:
            response.setdefault('services' if action == 'describe-services' else 'Stacks', [])[0]['changed'] = True
        return response

    with pytest.raises(ObservationError, match='changed during observation'):
        observe_stack(changing, STACK)


def test_propagates_access_denial_without_emitting_partial_observation():
    def denied(*args):
        raise PermissionError('AccessDenied')

    with pytest.raises(PermissionError, match='AccessDenied'):
        observe_stack(denied, STACK)


@pytest.mark.parametrize('change', ['missing', 'unhealthy', 'wrong-address', 'wrong-port'])
def test_requires_readiness_health_for_the_actual_running_task(change):
    aws = FakeAws()
    targets = aws.data['describe-target-health']['TargetHealthDescriptions']
    if change == 'missing':
        targets.clear()
    elif change == 'unhealthy':
        targets[0]['TargetHealth']['State'] = 'unhealthy'
    elif change == 'wrong-address':
        targets[0]['Target']['Id'] = '10.0.0.8'
    else:
        targets[0]['Target']['Port'] = 80
    with pytest.raises(ObservationError, match='Readiness'):
        observe_stack(aws, STACK)


@pytest.mark.parametrize('field,value', [('HealthCheckPath', '/'), ('HealthCheckEnabled', False),
                                        ('Matcher', {'HttpCode': '200-499'}), ('TargetType', 'instance')])
def test_refuses_health_checks_that_do_not_observe_server_readiness(field, value):
    aws = FakeAws()
    aws.data['describe-target-groups']['TargetGroups'][0][field] = value
    with pytest.raises(ObservationError, match='Readiness'):
        observe_stack(aws, STACK)


def test_describes_more_than_one_hundred_tasks_in_bounded_batches():
    aws = FakeAws()
    prototype = aws.data['describe-tasks']['tasks'][0]
    tasks, targets = [], []
    for number in range(101):
        task = copy.deepcopy(prototype)
        task['taskArn'] = f'task-{number}'
        address = f'10.0.0.{number + 1}'
        task['containers'][0]['networkInterfaces'][0]['privateIpv4Address'] = address
        tasks.append(task)
        targets.append({'Target': {'Id': address, 'Port': 8080}, 'TargetHealth': {'State': 'healthy'}})
    aws.data['list-tasks']['taskArns'] = [t['taskArn'] for t in tasks]
    aws.data['describe-target-health']['TargetHealthDescriptions'] = targets
    service = aws.data['describe-services']['services'][0]
    service['desiredCount'] = service['runningCount'] = len(tasks)

    def batched(service, operation, *args):
        result = aws(service, operation, *args)
        if operation == 'describe-tasks':
            assert 1 <= len(args[3:]) <= 100
            result['tasks'] = [t for t in tasks if t['taskArn'] in args[3:]]
        return result

    assert observe_stack(batched, STACK)['healthyTaskCount'] == 101
    assert sum(call[1] == 'describe-tasks' for call in aws.calls) == 4


@pytest.mark.parametrize('field,value', [('HealthCheckPath', '/'), ('Matcher', {'HttpCode': '200-499'})])
def test_refuses_readiness_configuration_changed_during_health_observation(field, value):
    aws = FakeAws()

    def changing(service, action, *args):
        response = aws(service, action, *args)
        if action == 'describe-target-health':
            aws.data['describe-target-groups']['TargetGroups'][0][field] = value
        return response

    with pytest.raises(ObservationError, match='Readiness health check differs'):
        observe_stack(changing, STACK)


@pytest.mark.parametrize('change', ['health', 'container-health', 'digest', 'address', 'replacement'])
def test_refuses_task_changes_during_readiness_observation(change):
    aws = FakeAws()

    def changing(service, operation, *args):
        response = aws(service, operation, *args)
        if operation == 'describe-target-health':
            task = aws.data['describe-tasks']['tasks'][0]
            container = task['containers'][0]
            if change == 'health':
                task['healthStatus'] = 'UNHEALTHY'
            elif change == 'container-health':
                container['healthStatus'] = 'UNHEALTHY'
            elif change == 'digest':
                container['imageDigest'] = 'sha256:' + 'c' * 64
            elif change == 'address':
                container['networkInterfaces'][0]['privateIpv4Address'] = '10.0.0.8'
            else:
                task['taskArn'] = 'task-2'
                aws.data['list-tasks']['taskArns'] = ['task-2']
        return response

    with pytest.raises(ObservationError):
        observe_stack(changing, STACK)
