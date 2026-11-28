"""AWS candidate binding, notification checks, and restoration for Runs-Itself qualification."""
from __future__ import annotations

import base64
import datetime as dt
import json
import time
from typing import Any
import zlib

from launch_stack_aws import AwsJsonTransport
from launch_stack_observations import (
    ObservationError, require, resource_summaries, running_tasks, service_state,
    stack_resources as ecs_stack_resources, task_definition, task_snapshot,
)

AWS_CMD_TIMEOUT_SECONDS = 300
METRIC_PADDING_SECONDS = 60
METRIC_PERIOD_SECONDS = 60
ECS_ARCH_TO_OCI = {'X86_64': 'amd64', 'ARM64': 'arm64'}
UNCONFIRMED_SUBSCRIPTION_STATES = frozenset({'', 'PendingConfirmation', 'Deleted'})


class MetricPendingError(ObservationError):
    """Transient CloudWatch notification metric lag eligible for bounded retry."""


def make_aws_client(region: str) -> AwsJsonTransport:
    return AwsJsonTransport(region, timeout=AWS_CMD_TIMEOUT_SECONDS)


def stack_resources(aws: Any, stack_name: str) -> dict[str, str]:
    summaries = resource_summaries(aws, stack_name)
    return {
        item['LogicalResourceId']: item['PhysicalResourceId']
        for item in summaries
        if item.get('LogicalResourceId') and item.get('PhysicalResourceId')
    }


def verify_stack_candidate_binding(aws: Any, stack_name: str, candidate: dict) -> dict:
    ecs_res = ecs_stack_resources(aws, stack_name)
    service = service_state(aws, ecs_res)
    definition = task_definition(aws, ecs_res)
    expected_digest = candidate['server_digest']
    require(definition['declaredImage'].endswith('@' + expected_digest),
            f'Stack {stack_name} task definition image does not match candidate server_digest')
    arch = ECS_ARCH_TO_OCI.get(definition['cpuArchitecture'])
    expected_platform = candidate['platform_digests'].get(arch)
    require(bool(expected_platform), f'Candidate lacks verified digest for stack {stack_name} architecture')
    tasks = running_tasks(aws, ecs_res)
    require(len(tasks) == service['desiredCount'],
            f'Stack {stack_name} running task count differs from desired capacity')
    digests = {item['digest'] for item in task_snapshot(tasks, ecs_res).values()}
    require(digests == {expected_platform},
            f'Stack {stack_name} running tasks do not match candidate platform digest')
    return {'stackName': stack_name, 'cpuArchitecture': definition['cpuArchitecture'],
            'resolvedPlatformImageDigest': expected_platform}


def decode_watchdog_state(raw: str) -> dict:
    value = json.loads(raw)
    if isinstance(value, dict) and 'zlib_v1' in value:
        packed = base64.b64decode(value['zlib_v1'], validate=True)
        decoder = zlib.decompressobj()
        decoded = decoder.decompress(packed, 64001)
        require(len(decoded) <= 64000 and decoder.eof and not decoder.unused_data,
                'Invalid or oversized watchdog state parameter')
        value = json.loads(decoded)
    require(isinstance(value, dict) and isinstance(value.get('outbox'), list),
            'Watchdog state parameter must contain a readable outbox list')
    return value


def _metric_sum(aws: Any, spec: dict, window: tuple[dt.datetime, dt.datetime]) -> float:
    start, end = window
    query_start = start.replace(microsecond=0)
    query_end = (end + dt.timedelta(seconds=METRIC_PADDING_SECONDS)).replace(microsecond=0)
    response = aws(
        'cloudwatch', 'get-metric-statistics',
        '--namespace', spec['namespace'],
        '--metric-name', spec['metric'],
        '--dimensions', f"Name={spec['dim_name']},Value={spec['dim_value']}",
        '--start-time', query_start.isoformat().replace('+00:00', 'Z'),
        '--end-time', query_end.isoformat().replace('+00:00', 'Z'),
        '--period', str(METRIC_PERIOD_SECONDS),
        '--statistics', 'Sum',
    )
    return float(sum(float(point.get('Sum', 0.0)) for point in response.get('Datapoints', [])))


EMAIL_SUBSCRIPTION_PROTOCOLS = frozenset({'email', 'email-json'})


def _verify_sns_channel(aws: Any, topic_arn: str, window: tuple[dt.datetime, dt.datetime]) -> dict:
    subs = aws('sns', 'list-subscriptions-by-topic', '--topic-arn', topic_arn).get('Subscriptions', [])
    confirmed = [
        item for item in subs
        if item.get('Protocol') in EMAIL_SUBSCRIPTION_PROTOCOLS
        and str(item.get('SubscriptionArn') or '') not in UNCONFIRMED_SUBSCRIPTION_STATES
    ]
    require(len(confirmed) >= 1, 'OpsTopic has no confirmed email SNS subscription')
    topic_name = topic_arn.rsplit(':', 1)[-1]
    published = _metric_sum(
        aws, {'namespace': 'AWS/SNS', 'metric': 'NumberOfMessagesPublished',
              'dim_name': 'TopicName', 'dim_value': topic_name}, window,
    )
    failed = _metric_sum(
        aws, {'namespace': 'AWS/SNS', 'metric': 'NumberOfNotificationsFailed',
              'dim_name': 'TopicName', 'dim_value': topic_name}, window,
    )
    require(failed == 0.0, 'SNS reported failed notification delivery during fault window')
    if published < 1.0:
        raise MetricPendingError('No SNS notification published to OpsTopic during fault window')
    return {'confirmedSubscriptions': len(confirmed), 'snsPublished': int(published)}


def _verify_inbox_channel(aws: Any, resources: dict[str, str], window: tuple[dt.datetime, dt.datetime]) -> dict:
    dlq_url = resources.get('OpsDeadLetters')
    if dlq_url:
        attrs = aws('sqs', 'get-queue-attributes', '--queue-url', dlq_url,
                    '--attribute-names', 'ApproximateNumberOfMessages').get('Attributes', {})
        require(int(attrs.get('ApproximateNumberOfMessages', '0')) == 0,
                'OpsDeadLetters contains undelivered ops-inbox messages')
    param = aws('ssm', 'get-parameter', '--name', resources['WatchdogState']).get('Parameter', {})
    state = decode_watchdog_state(str(param.get('Value', '')))
    require(state.get('outbox') == [], 'Watchdog outbox still has undelivered notices')
    queue_name = resources['OpsEvents'].rstrip('/').rsplit('/', 1)[-1]
    sent = _metric_sum(
        aws, {'namespace': 'AWS/SQS', 'metric': 'NumberOfMessagesSent',
              'dim_name': 'QueueName', 'dim_value': queue_name}, window,
    )
    if sent < 1.0:
        raise MetricPendingError('No ops-inbox event queued to OpsEvents during fault window')
    return {'sqsSent': int(sent)}


def verify_row_notifications(
    aws: Any, stack_name: str, window: tuple[dt.datetime, dt.datetime],
) -> dict:
    resources = stack_resources(aws, stack_name)
    for logical in ('OpsTopic', 'OpsEvents', 'WatchdogState'):
        require(logical in resources, f'Missing required stack resource {logical}')
    sns_info = _verify_sns_channel(aws, resources['OpsTopic'], window)
    inbox_info = _verify_inbox_channel(aws, resources, window)
    return {'notificationVerified': True, 'inboxVerified': True, **sns_info, **inbox_info}


def _restore_stack_parameter(aws: Any, stack_name: str, update: tuple[str, str, dict[str, str]]) -> None:
    key, value, current_params = update
    params = [
        {'ParameterKey': name, **({'ParameterValue': value} if name == key else {'UsePreviousValue': True})}
        for name in current_params
    ]
    change_set = f'honua-cleanup-{key.lower()}-{time.time_ns()}'
    aws('cloudformation', 'create-change-set', '--stack-name', stack_name,
        '--change-set-name', change_set, '--change-set-type', 'UPDATE', '--use-previous-template',
        '--capabilities', 'CAPABILITY_IAM', '--parameters', json.dumps(params))
    aws('cloudformation', 'wait', 'change-set-create-complete',
        '--stack-name', stack_name, '--change-set-name', change_set)
    aws('cloudformation', 'execute-change-set', '--stack-name', stack_name, '--change-set-name', change_set)
    aws('cloudformation', 'wait', 'stack-update-complete', '--stack-name', stack_name)


def _restore_database_if_stopped(aws: Any, db_id: str) -> bool:
    instances = aws('rds', 'describe-db-instances', '--db-instance-identifier', db_id).get('DBInstances', [])
    if len(instances) == 1 and instances[0].get('DBInstanceStatus') == 'stopped':
        aws('rds', 'start-db-instance', '--db-instance-identifier', db_id)
        return True
    return False


def _restore_storage_alarm_if_active(aws: Any, alarm_name: str) -> bool:
    alarms = aws('cloudwatch', 'describe-alarms', '--alarm-names', alarm_name,
                 '--alarm-types', 'MetricAlarm').get('MetricAlarms', [])
    if len(alarms) == 1 and alarms[0].get('StateValue') == 'ALARM':
        aws('cloudwatch', 'set-alarm-state', '--alarm-name', alarm_name,
            '--state-value', 'OK', '--state-reason', 'Runs-Itself qualification cleanup')
        return True
    return False


def restore_stack_safety(aws: Any, stack_name: str) -> dict:
    stacks = aws('cloudformation', 'describe-stacks', '--stack-name', stack_name).get('Stacks', [])
    require(len(stacks) == 1, f'Cannot identify gate stack {stack_name}')
    params = {p['ParameterKey']: p.get('ParameterValue', '') for p in stacks[0].get('Parameters', [])}
    actions: list[str] = []
    for key, safe_value in (('WatchdogMode', 'NotifyOnly'), ('QualificationFault', 'None')):
        if key in params and params[key] != safe_value:
            _restore_stack_parameter(aws, stack_name, (key, safe_value, params))
            params[key] = safe_value
            actions.append(f'{key}={safe_value}')
    resources = stack_resources(aws, stack_name)
    if 'Database' in resources and _restore_database_if_stopped(aws, resources['Database']):
        actions.append('start-db-instance')
    if 'OpsAlarmDatabaseStorage' in resources and _restore_storage_alarm_if_active(
        aws, resources['OpsAlarmDatabaseStorage'],
    ):
        actions.append('reset-storage-alarm')
    return {'stackName': stack_name, 'restored': actions}
