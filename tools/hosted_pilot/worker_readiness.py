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
from agent_e2e_worker_lifecycle import terraform_definition_digest
from deploy import ROOT, aws, private_json, require, run
from image_manifest import manifest_bytes


def resources(module):
    result = list(module.get('resources', []))
    for child in module.get('child_modules', []):
        result.extend(resources(child))
    return result


def verify_claim_policy(policy, *, table_arn, roles, required, account_id, installed_slots=None, controller_role_arn=None, candidate=None):
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
    slots = installed_slots or []
    if slots:
        require(isinstance(slots, list) and 1 <= len(slots) <= 4 and
            re.fullmatch(r'sha256:[a-f0-9]{64}', str(candidate)) and
            re.fullmatch(r'arn:aws:iam::'+re.escape(account_id)+r':role/[A-Za-z0-9+=,.@_/-]+', str(controller_role_arn)) and
            len({row['slot_id'] for row in slots}) == len(slots) and all(
                set(row) == {'runtime_id', 'slot_id'} and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', row['runtime_id']) and
                re.fullmatch(r'[a-z0-9-]{3,40}', row['slot_id']) and
                'arn:aws:iam::'+account_id+':role/honua-agent-'+row['slot_id']+'-lifecycle' in writers for row in slots),
                'complete exact installed allocation policy binding missing')
        lifecycle_roles = ['arn:aws:iam::'+account_id+':role/honua-agent-'+row['slot_id']+'-lifecycle' for row in slots]
        slot_keys = [candidate+'/runtime/'+row['runtime_id']+'/slot/'+row['slot_id'] for row in slots]
        retired_keys = list(dict.fromkeys(candidate+'/runtime/'+row['runtime_id']+'/execution/execution-'+'?'*32+'/retired' for row in slots))
        expected['Statement'][0]['NotAction'].append('dynamodb:UpdateItem')
        for index in (1, 2):
            expected['Statement'][index]['Condition']['ArnNotEquals']['aws:PrincipalArn'] = sorted(set(all_writers+[controller_role_arn]))
        expected['Statement'] += [
            statement('OnlyInstalledPrincipalsMayUpdateSlots', {'Action':'dynamodb:UpdateItem'}, sorted(set(admins+lifecycle_roles+[controller_role_arn]))),
            statement('SlotUpdatesMustBeTransactional', {'Action':'dynamodb:UpdateItem'}, admins, ('StringNotEquals','TransactWriteItems')),
            {'Sid':'ControllerWritesOnlyInstalledRetirementKeys','Effect':'Deny','Principal':'*',
             'Action':['dynamodb:PutItem','dynamodb:UpdateItem'],'Resource':table_arn,
             'Condition':{'ArnEquals':{'aws:PrincipalArn':controller_role_arn},
                          'ForAnyValue:StringNotLike':{'dynamodb:LeadingKeys':slot_keys+retired_keys}}},
        ]
        for index, row in enumerate(slots):
            expected['Statement'].append({'Sid':'Slot'+str(index)+'LifecycleUpdatesOnlyOwnSlot','Effect':'Deny','Principal':'*',
                'Action':'dynamodb:UpdateItem','Resource':table_arn,'Condition':{
                    'ArnEquals':{'aws:PrincipalArn':lifecycle_roles[index]},
                    'ForAnyValue:StringNotEquals':{'dynamodb:LeadingKeys':[slot_keys[index]]}}})
    # DDB may reorder statements, but cannot add an Allow, omit a guard or
    # broaden any field/exemption while retaining the same familiar Sid.
    def normalized(value):
        require(isinstance(value, dict) and set(value) == {'Version', 'Statement'} and isinstance(value['Statement'], list),
            'claim policy envelope differs from reviewed source')
        return value | {'Statement': sorted(value['Statement'], key=lambda row: row.get('Sid', ''))}
    require(normalized(policy) == normalized(expected), 'complete actual claim-table policy differs from reviewed writer/administrator controls')


def verify_schedule(actual, expected, *, schedule_arn, cleanup, expires_epoch, installed=False):
    def instant(value):
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        require(parsed.tzinfo is not None, 'cleanup schedule date has no timezone')
        return parsed.timestamp()
    target = expected['target'][0]
    require(actual['Arn'] == schedule_arn and actual['State'] == expected.get('state', 'ENABLED') == 'ENABLED' and
        actual['ScheduleExpression'] == expected['schedule_expression'] == 'rate(1 minute)' and
        actual['ScheduleExpressionTimezone'] == expected['schedule_expression_timezone'] == 'UTC' and
        actual['FlexibleTimeWindow'] == {'Mode': expected['flexible_time_window'][0]['mode']} == {'Mode': 'OFF'} and
        ((expected.get('start_date') is None and actual.get('StartDate') is None) if installed else
            instant(actual['StartDate']) == instant(expected['start_date']) == expires_epoch) and
        instant(actual['EndDate']) == instant(expected['end_date']) == expires_epoch+86400 and
        actual.get('ActionAfterCompletion', 'NONE') == (expected.get('action_after_completion') or 'NONE') and
        actual['Target']['Arn'] == target['arn'] == cleanup['state_machine_arn'] and
        actual['Target']['RoleArn'] == target['role_arn'] and json.loads(actual['Target']['Input']) == json.loads(target['input']) and
        actual['Target']['DeadLetterConfig'] == {'Arn': target['dead_letter_config'][0]['arn']} == {'Arn': cleanup['dead_letter_arn']} and
        actual['Target']['RetryPolicy'] == {'MaximumEventAgeInSeconds': target['retry_policy'][0]['maximum_event_age_in_seconds'],
            'MaximumRetryAttempts': target['retry_policy'][0]['maximum_retry_attempts']},
        'actual independent expiry schedule differs from fixed expiry, window, role, input or delivery controls')


def inspect(config, outputs, state):
    installed = config.get('installed_slot') is not None
    if installed:
        from agent_e2e_installed_worker import InstalledSlot
        from types import SimpleNamespace
        descriptor = outputs['installed_worker_slot']['value']
        slot = config['installed_slot']
        require(descriptor['runtimeId'] == slot['runtime_id'] and descriptor['slotId'] == config['attempt_id'] == slot['slot_id'] and
            descriptor['candidateKey'] == config['candidate_digest'] and descriptor['image'] == config['worker_image'] and
            descriptor['originalFundingExpiresEpoch'] == slot['original_funding_expires_epoch'] and
            descriptor['maxExecutionSeconds'] == slot['max_execution_seconds'] and descriptor['liability'] == config['execution_liability'], 'installed descriptor differs from original source/quote')
        actual = InstalledSlot(descriptor,
            states=SimpleNamespace(describe_state_machine=lambda **kwargs: aws('stepfunctions','describe-state-machine','--state-machine-arn',kwargs['stateMachineArn'])),
            ecs=SimpleNamespace(describe_task_definition=lambda **kwargs: aws('ecs','describe-task-definition','--task-definition',kwargs['taskDefinition'])), db=None).inspect()
        worker = {'attemptId':descriptor['slotId'],'imageUri':descriptor['image'],'publicBundleDigest':config['public_bundle_digest'],
            'taskRoleArn':descriptor['taskRoleArn'],'transport':{key:descriptor[key] for key in ('commandQueueUrl','replyQueueUrl')},
            'lifecycle':{'stateMachineArn':descriptor['stateMachineArn'],'taskDefinitionArn':descriptor['taskDefinitionArn'],
                'expiresEpoch':descriptor['originalFundingExpiresEpoch']}}
        definition = actual['taskDefinition']['taskDefinition']
        container = definition['containerDefinitions'][0]
    else:
        worker = outputs['worker_backend']['value']
    cleanup = outputs['worker_cleanup']['value']
    require(worker['attemptId'] == config['attempt_id'] and worker['imageUri'] == config['worker_image'] and
            worker['publicBundleDigest'] == config['public_bundle_digest'] and
            worker['lifecycle']['expiresEpoch'] == (config['installed_slot']['original_funding_expires_epoch'] if installed else config['issued_epoch']+config['lifetime_seconds']) and
            datetime.now(timezone.utc).timestamp() < worker['lifecycle']['expiresEpoch'] and
            (installed or 60 <= config['lifetime_seconds'] <= 6000 and config['issued_epoch'] <= datetime.now(timezone.utc).timestamp()),
            'actual worker configuration is expired, sliding or differs from reviewed source')
    if not installed:
        definition = aws('ecs', 'describe-task-definition', '--task-definition', worker['lifecycle']['taskDefinitionArn'])['taskDefinition']
        container = verify_worker_definition(definition, worker)
    require(definition['taskDefinitionArn'] == worker['lifecycle']['taskDefinitionArn'], 'native worker revision differs')
    require(container['secrets'] == [{'name': 'HONUA_AGENT_MCP_CONFIG',
            'valueFrom': config['mcp_configuration']['secret_arn']+':::'+config['mcp_configuration']['version_id']}], 'actual worker secret is not its exact observed native MCP version')
    manifest_bytes(config['worker_image'], aws=aws)
    planned = {resource['address']: resource['values'] for resource in resources(state['values']['root_module'])}
    for name, arn in [('worker', worker['lifecycle']['stateMachineArn']), ('reaper', cleanup['state_machine_arn'])]:
        expected = planned['module.worker.aws_sfn_state_machine.'+name]
        actual = aws('stepfunctions', 'describe-state-machine', '--state-machine-arn', arn)
        require(actual['type'] == 'STANDARD' and actual['roleArn'] == expected['role_arn'] and
                terraform_definition_digest(actual['definition']) == terraform_definition_digest(expected['definition']), 'native worker/cleanup lifecycle differs from exact applied IaC')
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
        required=outputs['required_accounting_claim_writers']['value'], account_id=config['account_id'],
        installed_slots=config.get('accounting_installed_worker_slots'), controller_role_arn=config['controller_role_arn'], candidate=config['candidate_digest'])
    schedule_arn = cleanup['schedule_arn']
    group, name = schedule_arn.split(':schedule/', 1)[1].split('/', 1)
    schedule = aws('scheduler', 'get-schedule', '--group-name', group, '--name', name)
    verify_schedule(schedule, planned['module.worker.aws_scheduler_schedule.reaper'], schedule_arn=schedule_arn,
        cleanup=cleanup, expires_epoch=worker['lifecycle']['expiresEpoch'], installed=installed)
    alarms = aws('cloudwatch', 'describe-alarms', '--alarm-names', *[arn.split(':alarm:', 1)[1] for arn in cleanup['alarm_arns']])['MetricAlarms']
    require(len(alarms) == len(cleanup['alarm_arns']) and all(row['ActionsEnabled'] and row['AlarmActions'] == [config['cleanup_alarm_topic_arn']] for row in alarms),
            'independent cleanup alarms do not target the reviewed monitor')
    subscriptions = aws('sns', 'list-subscriptions-by-topic', '--topic-arn', config['cleanup_alarm_topic_arn'])['Subscriptions']
    require(any(row.get('SubscriptionArn', '').startswith('arn:aws:sns:') for row in subscriptions), 'cleanup topic has no actually confirmed subscriber')
    result = {'schema': 'honua.hosted-worker-readiness/v1', 'source_sha': os.environ['GITHUB_SHA'], 'run_id': os.environ['GITHUB_RUN_ID'],
        'attempt_id': config['attempt_id'], 'registered_task_definition_verified': True, 'immutable_agent_mcp_binding_verified': True,
        'worker_role_queue_only': True, 'independent_expiry_schedule_verified': True, 'cleanup_monitor_confirmed': True,
        'worker_not_launched': True, 'actual_runtime_and_cleanup_qualification': 'not-run', 'qualification': False,
        'status': 'worker-defined-genuine-runtime-cleanup-and-model-evidence-required'}
    if installed:
        result.pop('worker_not_launched')
        result.update(mode='installed-slot', runtime_execution_not_attempted_by_this_workflow=True)
    return result


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
