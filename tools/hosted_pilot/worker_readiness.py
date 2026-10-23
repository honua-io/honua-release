#!/usr/bin/env python3
"""Read actual staged worker controls; never launches a worker or qualifies a case."""
from __future__ import annotations
from datetime import datetime, timezone
import json
import os
from pathlib import Path
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
    require(table['DeletionProtectionEnabled'] is True, 'actual worker claims lack deletion protection')
    backup = aws('dynamodb', 'describe-continuous-backups', '--table-name', config['claim_table_name'])['ContinuousBackupsDescription']
    require(backup.get('PointInTimeRecoveryDescription', {}).get('PointInTimeRecoveryStatus') == 'ENABLED', 'actual worker claims lack retained point-in-time recovery')
    policy = json.loads(aws('dynamodb', 'get-resource-policy', '--resource-arn', table['TableArn'])['Policy'])
    claim = [entry for entry in policy['Statement'] if entry.get('Sid') == 'OnlyFixedLifecycleRolesMayClaim']
    require(len(claim) == 1 and set(outputs['required_accounting_claim_writers']['value']) <=
            set(claim[0]['Condition']['ArnNotEquals']['aws:PrincipalArn']), 'campaign accounting has not admitted these exact fixed lifecycle/expiry roles')
    schedule_arn = cleanup['schedule_arn']
    group, name = schedule_arn.split(':schedule/', 1)[1].split('/', 1)
    schedule = aws('scheduler', 'get-schedule', '--group-name', group, '--name', name)
    require(schedule['Arn'] == schedule_arn and schedule['State'] == 'ENABLED' and schedule['Target']['Arn'] == cleanup['state_machine_arn'] and
            schedule['Target']['DeadLetterConfig']['Arn'] == cleanup['dead_letter_arn'], 'independent expiry schedule or retained delivery DLQ differs')
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
