#!/usr/bin/env python3
"""Observe deployed campaign infrastructure; never emits campaign qualification."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from deploy import ROOT, aws, private_json, require, run
from readiness import observed_service
from controller_handoff import validate_configuration, validate_observation, verify_lifecycle
from fixture_handoff import verify_task
from agent_e2e_pack import canonical_digest


class NativeCLI:
    """Use the runner's already authenticated CLI without installing an SDK."""
    def __init__(self, service):
        self.service = service

    def __getattr__(self, operation):
        def call(**kwargs):
            arguments = [self.service, operation.replace('_', '-')]
            for key, value in kwargs.items():
                option = '--' + re.sub(r'(?<!^)(?=[A-Z])', '-', key).lower()
                if isinstance(value, bool):
                    if value:
                        arguments.append(option)
                else:
                    arguments.extend([option, json.dumps(value) if isinstance(value, (dict, list)) else str(value)])
            return aws(*arguments)
        return call


def observe_disabled_dispatcher(stage, cfg):
    role_arn = f"arn:aws:iam::{cfg['account_id']}:role/{cfg['name_prefix']}-controller-dispatch"
    require(stage['dispatcherRoleArn'] == role_arn, 'staging dispatcher differs from reviewed identity')
    iam = NativeCLI('iam')
    name = role_arn.rsplit('/', 1)[1]
    role = iam.get_role(RoleName=name)['Role']
    require(role['Arn'] == role_arn and role.get('PermissionsBoundary', {}).get('PermissionsBoundaryArn') == cfg['permissions_boundary_arn'],
            'staging dispatcher identity or permissions boundary changed')
    attached = iam.list_attached_role_policies(RoleName=name)
    require(not attached.get('IsTruncated') and not attached.get('Marker') and not attached.get('AttachedPolicies'),
            'staging dispatcher has additional managed policy authority')
    inline = iam.list_role_policies(RoleName=name)
    require(not inline.get('IsTruncated') and not inline.get('Marker') and inline['PolicyNames'] and
            len(inline['PolicyNames']) == len(set(inline['PolicyNames'])), 'staging dispatcher policy inventory is incomplete')
    expected = stage['dispatcherPolicyDigests']
    require(isinstance(expected, dict) and expected and set(expected) == set(inline['PolicyNames']) and
            all(isinstance(value, str) and re.fullmatch(r'sha256:[a-f0-9]{64}', value) for value in expected.values()),
            'staging dispatcher policy set differs from reviewed Terraform output')
    policies = {}
    for policy in inline['PolicyNames']:
        value = iam.get_role_policy(RoleName=name, PolicyName=policy)
        require(value['RoleName'] == name and value['PolicyName'] == policy and isinstance(value['PolicyDocument'], dict),
                'staging dispatcher policy readback differs')
        document = value['PolicyDocument']
        policies[policy] = canonical_digest(document)
        require(policies[policy] == expected[policy], 'staging dispatcher policy differs from reviewed Terraform output')
    return {'roleArn': role_arn, 'inlinePolicyDigests': policies, 'managedPolicies': [], 'reviewedPolicyMapVerified': True, 'dispatchEnabled': False}


def observe_initial_accounting(stage, cfg):
    state, candidate = stage['state'], cfg['candidate_digest']
    name = 'honua-agent-'+candidate[7:31]
    require(stage['candidateKey'] == candidate and state == {'region':cfg['region'], 'bucket':name+'-'+cfg['account_id'], 'table':name+'-state'},
            'staging accounting namespace differs from the original candidate')
    table = aws('dynamodb', 'describe-table', '--table-name', state['table'])['Table']
    require(table['DeletionProtectionEnabled'] is True and table['TableStatus'] == 'ACTIVE', 'staging accounting is not retained and active')
    require(aws('s3api', 'get-bucket-versioning', '--bucket', state['bucket']).get('Status') == 'Enabled', 'staging accounting custody is unversioned')
    head = aws('dynamodb', 'get-item', '--table-name', state['table'], '--consistent-read',
               '--key', json.dumps({'candidateKey':{'S':candidate}})).get('Item', {})
    versions = aws('s3api', 'list-object-versions', '--bucket', state['bucket'], '--prefix', 'agent-e2e/'+candidate[7:]+'/', '--max-keys', '1')
    history_present = bool(head or versions.get('Versions') or versions.get('DeleteMarkers') or versions.get('IsTruncated') or versions.get('NextKeyMarker'))
    return {'state': state, 'candidateKey':candidate, 'historyPresent':history_present,
            'status':'existing-or-partial-history-reconciliation-required' if history_present else 'no-head-or-object-history-observed',
            'initializationPerformed':False}


def inspect(outputs, cfg):
    initial = cfg.get('dispatch_budget') is None
    if initial:
        require(outputs['controller_handoff']['value'] is None and outputs['fixture_handoff']['value'] is None and
                outputs['accounting_staging']['value']['dispatchEnabled'] is False, 'unfunded staging exposes an executable handoff')
        stage = outputs['initialization_readiness']['value']
        config, fixture = stage['controller'], stage['fixture']
        validate_observation(config)
        disabled_dispatcher = observe_disabled_dispatcher(stage, cfg)
        accounting = observe_initial_accounting(stage, cfg)
        claim_table = stage['claimTable']
        require(claim_table == cfg['name_prefix']+'-controller-dispatch-claims', 'staging dispatch claim table changed')
    else:
        config = outputs['controller_handoff']['value']
        fixture = outputs['fixture_handoff']['value']
        validate_configuration(config)
        require(config['dispatchBudget']['candidateKey'] == cfg['candidate_digest'], 'native campaign candidate differs')
        claim_table = config['dispatchBudget']['claimTable']
    require(config['accountId'] == cfg['account_id'] and config['region'] == cfg['region'] and
            config['image'] == cfg['controller_image'], 'native campaign outputs differ from reviewed configuration')
    verify_lifecycle(NativeCLI('stepfunctions'), NativeCLI('ecs'), config)
    definition = NativeCLI('ecs').describe_task_definition(taskDefinition=fixture['taskDefinitionArn'])
    verify_task(definition, fixture)
    target = outputs['target_readiness']['value']
    run(['aws', 'ecs', 'wait', 'services-stable', '--cluster', config['clusterArn'], '--services', target['service']])
    installed = observed_service(config['clusterArn'], target['service'], {'honua': cfg['server_image']})
    groups = aws('ec2', 'describe-security-groups', '--group-ids', *config['network']['securityGroupIds'], *target['workerSecurityGroupIds'])['SecurityGroups']
    require(len(groups) == len(set(config['network']['securityGroupIds'] + target['workerSecurityGroupIds'])) and
            all(not group['IpPermissions'] for group in groups), 'campaign worker/controller has unexpected inbound access')
    table = aws('dynamodb', 'describe-table', '--table-name', claim_table)['Table']
    require(table['DeletionProtectionEnabled'] is True, 'native dispatch claims lack deletion protection')
    require(aws('s3api', 'get-bucket-versioning', '--bucket', config['custody']['bucket']).get('Status') == 'Enabled', 'native campaign custody is unversioned')
    result = {'schema': 'honua.hosted-pilot-campaign-readiness/v1', 'source_sha': os.environ['GITHUB_SHA'],
        'run_id': os.environ['GITHUB_RUN_ID'], 'candidate_key': cfg['candidate_digest'],
        'installed_images': installed, 'native_controller_lifecycle_verified': True,
        'private_fixture_task_verified': True, 'immutable_custody_verified': True,
        'native_agent_configured': outputs['fixture_staging']['value']['nativeAgentConfigured'],
        'sql_roles_and_credentials_qualified': False, 'human_approval_qualified': False,
        'agent_and_model_campaign': 'not-run', 'qualification': False,
        'status': 'infrastructure-ready-private-provisioning-and-genuine-campaign-required'}
    if initial:
        result.update(status='infrastructure-staged-accounting-uninitialized-or-reconciliation-required',
                      dispatch_enabled=False, dispatcher=disabled_dispatcher, accounting=accounting)
    return result


def main():
    os.umask(0o077)
    work = Path(os.environ['RUNNER_TEMP']) / 'hosted-pilot-private'
    outputs = json.loads((work / 'outputs.json').read_text())
    cfg = json.loads((ROOT / os.environ['PILOT_CONFIG_PATH']).read_text())
    result = inspect(outputs, cfg)
    private_json(work / 'readiness.json', result)
    print('Native campaign infrastructure observed. SQL identities, real approval and model qualification remain required.')


if __name__ == '__main__':
    main()
