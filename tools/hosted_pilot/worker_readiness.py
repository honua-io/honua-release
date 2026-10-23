#!/usr/bin/env python3
"""Read actual staged worker controls; never launches a worker or qualifies a case."""
from __future__ import annotations
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_p10_backend import verify_worker_definition
from deploy import ROOT, aws, private_json, require, run
from image_manifest import manifest_bytes


def resources(module):
    result = list(module.get('resources', []))
    for child in module.get('child_modules', []):
        result.extend(resources(child))
    return result


def verify_claim_policy(policy, *, table_arn, roles, required, account_id):
    require(isinstance(roles, dict) and set(roles) == {'administrators', 'workers'}, 'complete reviewed accounting role sets are required')
    administrators, writers = roles['administrators'], roles['workers']
    require(isinstance(administrators, list) and administrators and len(set(administrators)) == len(administrators) and
        all(re.fullmatch(r'arn:aws:iam::'+re.escape(account_id)+r':role/[A-Za-z0-9+=,.@_/-]+', arn) for arn in administrators) and
        isinstance(writers, list) and 1 <= len(writers) <= 4 and len(set(writers)) == len(writers) and
        all(re.fullmatch(r'arn:aws:iam::'+re.escape(account_id)+r':role/honua-agent-[a-z0-9-]{3,40}-lifecycle', arn) for arn in writers) and
        set(required) <= set(writers) and not set(administrators).intersection(writers),
        'claim exemptions must equal complete reviewed administrator and fixed worker lifecycle sets')
    admins, all_writers = sorted(administrators), sorted(set(administrators) | set(writers))
    def statement(sid, permissions, principals, enclosing=None):
        condition = {'ArnNotEquals': {'aws:PrincipalArn': principals}}
        if enclosing:
            condition[enclosing[0]] = {'dynamodb:EnclosingOperation': enclosing[1]}
        return {'Sid': sid, 'Effect': 'Deny', 'Principal': '*', 'Resource': table_arn, 'Condition': condition, **permissions}
    expected = {'Version': '2012-10-17', 'Statement': [
        statement('RuntimeCanOnlyClaimOrObserve', {'NotAction': ['dynamodb:PutItem', 'dynamodb:GetItem', 'dynamodb:DescribeTable']}, admins),
        statement('OnlyFixedLifecycleRolesMayClaim', {'Action': 'dynamodb:PutItem'}, all_writers),
        statement('OnlyFixedLifecycleMayTransactWrites', {'Action': 'dynamodb:*'}, all_writers, ('StringEquals', 'TransactWriteItems')),
        statement('RuntimeCannotTransactReads', {'Action': 'dynamodb:*'}, admins, ('StringEquals', 'TransactGetItems')),
        statement('RuntimeClaimWritesMustBeTransactional', {'Action': 'dynamodb:PutItem'}, admins, ('StringNotEquals', 'TransactWriteItems'))]}
    # DDB may reorder statements, but cannot add an Allow, omit a guard or
    # broaden any field/exemption while retaining the same familiar Sid.
    def normalized(value):
        require(isinstance(value, dict) and set(value) == {'Version', 'Statement'} and isinstance(value['Statement'], list),
            'claim policy envelope differs from reviewed source')
        return value | {'Statement': sorted(value['Statement'], key=lambda row: row.get('Sid', ''))}
    require(normalized(policy) == normalized(expected), 'complete actual claim-table policy differs from reviewed writer/administrator controls')


def verify_schedule(actual, expected, *, schedule_arn, cleanup, expires_epoch):
    def instant(value):
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(parsed.tzinfo is not None, 'cleanup schedule date has no timezone')
        return parsed.timestamp()
    target = expected['target'][0]
    require(actual['Arn'] == schedule_arn and actual['State'] == expected.get('state', 'ENABLED') == 'ENABLED' and
        actual['ScheduleExpression'] == expected['schedule_expression'] == 'rate(1 minute)' and
        actual['ScheduleExpressionTimezone'] == expected['schedule_expression_timezone'] == 'UTC' and
        actual['FlexibleTimeWindow'] == {'Mode': expected['flexible_time_window'][0]['mode']} == {'Mode': 'OFF'} and
        instant(actual['StartDate']) == instant(expected['start_date']) == expires_epoch and
        instant(actual['EndDate']) == instant(expected['end_date']) == expires_epoch+86400 and
        actual.get('ActionAfterCompletion', 'NONE') == (expected.get('action_after_completion') or 'NONE') and
        actual['Target']['Arn'] == target['arn'] == cleanup['state_machine_arn'] and
        actual['Target']['RoleArn'] == target['role_arn'] and json.loads(actual['Target']['Input']) == json.loads(target['input']) and
        actual['Target']['DeadLetterConfig'] == {'Arn': target['dead_letter_config'][0]['arn']} == {'Arn': cleanup['dead_letter_arn']} and
        actual['Target']['RetryPolicy'] == {'MaximumEventAgeInSeconds': target['retry_policy'][0]['maximum_event_age_in_seconds'],
            'MaximumRetryAttempts': target['retry_policy'][0]['maximum_retry_attempts']},
        'actual independent expiry schedule differs from fixed expiry, window, role, input or delivery controls')


def inspect(config, outputs, state):
    worker = outputs['worker_backend']['value']
    cleanup = outputs['worker_cleanup']['value']
    require(worker['attemptId'] == config['attempt_id'] and worker['imageUri'] == config['worker_image'] and
            worker['publicBundleDigest'] == config['public_bundle_digest'] and
            worker['lifecycle']['expiresEpoch'] == config['issued_epoch']+config['lifetime_seconds'] and
            60 <= config['lifetime_seconds'] <= 6000 and config['issued_epoch'] <= datetime.now(timezone.utc).timestamp() < worker['lifecycle']['expiresEpoch'],
            'actual worker configuration is expired, sliding or differs from reviewed source')
    definition = aws('ecs', 'describe-task-definition', '--task-definition', worker['lifecycle']['taskDefinitionArn'])['taskDefinition']
    require(definition['taskDefinitionArn'] == worker['lifecycle']['taskDefinitionArn'], 'native worker revision differs')
    container = verify_worker_definition(definition, worker)
    require(container['secrets'] == [{'name': 'HONUA_AGENT_MCP_CONFIG',
            'valueFrom': config['mcp_configuration']['secret_arn']+':::'+config['mcp_configuration']['version_id']}], 'actual worker secret is not its exact observed native MCP version')
    manifest_bytes(config['worker_image'], aws=aws)
    planned = {resource['address']: resource['values'] for resource in resources(state['values']['root_module'])}
    for name, arn in [('worker', worker['lifecycle']['stateMachineArn']), ('reaper', cleanup['state_machine_arn'])]:
        expected = planned['module.worker.aws_sfn_state_machine.'+name]
        actual = aws('stepfunctions', 'describe-state-machine', '--state-machine-arn', arn)
        require(actual['type'] == 'STANDARD' and actual['roleArn'] == expected['role_arn'] and
                json.loads(actual['definition']) == json.loads(expected['definition']), 'native worker/cleanup lifecycle differs from exact applied IaC')
    groups = aws('ec2', 'describe-security-groups', '--group-ids', *config['security_group_ids'])['SecurityGroups']
    require(len(groups) == len(config['security_group_ids']) and all(not group['IpPermissions'] for group in groups) and
            all(rule.get('FromPort') == rule.get('ToPort') and rule.get('ToPort') in (53, 443) and rule.get('IpProtocol') in ('tcp', 'udp')
                for group in groups for rule in group['IpPermissionsEgress']), 'native worker network has inbound or unreviewed non-DNS/HTTPS access')
    role_name = worker['taskRoleArn'].rsplit('/', 1)[1]
    role = aws('iam', 'get-role', '--role-name', role_name)['Role']
    require(role['Arn'] == worker['taskRoleArn'] and role.get('PermissionsBoundary', {}).get('PermissionsBoundaryArn') == config['permissions_boundary_arn'],
            'native worker role/boundary differs')
    require(not aws('iam', 'list-attached-role-policies', '--role-name', role_name)['AttachedPolicies'], 'native worker has unreviewed managed policies')
    allowed_actions = {'sqs:ReceiveMessage', 'sqs:DeleteMessage', 'sqs:SendMessage'}
    queues = {f"arn:aws:sqs:{config['region']}:{config['account_id']}:"+worker['transport'][key].rsplit('/', 1)[1]
              for key in ('commandQueueUrl', 'replyQueueUrl')}
    names = aws('iam', 'list-role-policies', '--role-name', role_name)['PolicyNames']
    require(names, 'native worker queue role is missing')
    granted = set()
    for name in names:
        policy = aws('iam', 'get-role-policy', '--role-name', role_name, '--policy-name', name)['PolicyDocument']
        policy = json.loads(unquote(policy)) if isinstance(policy, str) else policy
        for statement in policy['Statement']:
            actions = statement.get('Action', [])
            actions = [actions] if isinstance(actions, str) else actions
            targets = statement.get('Resource', [])
            targets = [targets] if isinstance(targets, str) else targets
            require(statement['Effect'] == 'Allow' and set(actions) <= allowed_actions and set(targets) <= queues and
                    not statement.get('NotAction') and not statement.get('NotResource'), 'worker can reach controller, model, secret, approval or provisioning authority')
            granted.update((action, target) for action in actions for target in targets)
    command_arn = f"arn:aws:sqs:{config['region']}:{config['account_id']}:"+worker['transport']['commandQueueUrl'].rsplit('/', 1)[1]
    reply_arn = f"arn:aws:sqs:{config['region']}:{config['account_id']}:"+worker['transport']['replyQueueUrl'].rsplit('/', 1)[1]
    require(granted == {('sqs:ReceiveMessage', command_arn), ('sqs:DeleteMessage', command_arn), ('sqs:SendMessage', reply_arn)},
            'native worker queue authority has the wrong command/reply direction')
    table = aws('dynamodb', 'describe-table', '--table-name', config['claim_table_name'])['Table']
    require(table['DeletionProtectionEnabled'] is True and table['TableArn'] ==
        f"arn:aws:dynamodb:{config['region']}:{config['account_id']}:table/{config['claim_table_name']}", 'actual worker claims lack deletion protection or original identity')
    backup = aws('dynamodb', 'describe-continuous-backups', '--table-name', config['claim_table_name'])['ContinuousBackupsDescription']
    require(backup.get('PointInTimeRecoveryDescription', {}).get('PointInTimeRecoveryStatus') == 'ENABLED', 'actual worker claims lack retained point-in-time recovery')
    policy = json.loads(aws('dynamodb', 'get-resource-policy', '--resource-arn', table['TableArn'])['Policy'])
    verify_claim_policy(policy, table_arn=table['TableArn'], roles=config['accounting_claim_roles'],
        required=outputs['required_accounting_claim_writers']['value'], account_id=config['account_id'])
    schedule_arn = cleanup['schedule_arn']
    group, name = schedule_arn.split(':schedule/', 1)[1].split('/', 1)
    schedule = aws('scheduler', 'get-schedule', '--group-name', group, '--name', name)
    verify_schedule(schedule, planned['module.worker.aws_scheduler_schedule.reaper'], schedule_arn=schedule_arn,
        cleanup=cleanup, expires_epoch=worker['lifecycle']['expiresEpoch'])
    alarms = aws('cloudwatch', 'describe-alarms', '--alarm-names', *[arn.split(':alarm:', 1)[1] for arn in cleanup['alarm_arns']])['MetricAlarms']
    require(len(alarms) == len(cleanup['alarm_arns']) and all(row['ActionsEnabled'] and row['AlarmActions'] == [config['cleanup_alarm_topic_arn']] for row in alarms),
            'independent cleanup alarms do not target the reviewed monitor')
    subscriptions = aws('sns', 'list-subscriptions-by-topic', '--topic-arn', config['cleanup_alarm_topic_arn'])['Subscriptions']
    require(any(row.get('SubscriptionArn', '').startswith('arn:aws:sns:') for row in subscriptions), 'cleanup topic has no actually confirmed subscriber')
    return {'schema': 'honua.hosted-worker-readiness/v1', 'source_sha': os.environ['GITHUB_SHA'], 'run_id': os.environ['GITHUB_RUN_ID'],
        'attempt_id': config['attempt_id'], 'registered_task_definition_verified': True, 'immutable_agent_mcp_binding_verified': True,
        'worker_role_queue_only': True, 'independent_expiry_schedule_verified': True, 'cleanup_monitor_confirmed': True,
        'worker_not_launched': True, 'actual_runtime_and_cleanup_qualification': 'not-run', 'qualification': False,
        'status': 'worker-defined-genuine-runtime-cleanup-and-model-evidence-required'}


def main():
    os.umask(0o077)
    work = Path(os.environ['RUNNER_TEMP'])/'hosted-pilot-private'
    config = json.loads((ROOT/os.environ['PILOT_CONFIG_PATH']).read_text())
    outputs = json.loads((work/'outputs.json').read_text())
    state = json.loads(run(['terraform', 'show', '-json']))
    result = inspect(config, outputs, state)
    private_json(work/'readiness.json', result)
    print('Registered private worker, bounded expiry and credential controls observed. Genuine runtime/model/cleanup evidence remains required.')


if __name__ == '__main__':
    main()
