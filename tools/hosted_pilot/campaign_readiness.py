#!/usr/bin/env python3
"""Observe deployed campaign infrastructure; never emits campaign qualification."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re

from deploy import ROOT, aws, private_json, require, run
from readiness import observed_service
from controller_handoff import validate_configuration, verify_lifecycle
from fixture_handoff import verify_task


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


def inspect(outputs, cfg):
    config = outputs['controller_handoff']['value']
    fixture = outputs['fixture_handoff']['value']
    validate_configuration(config)
    require(config['accountId'] == cfg['account_id'] and config['region'] == cfg['region'] and
            config['image'] == cfg['controller_image'] and
            config['dispatchBudget']['candidateKey'] == cfg['candidate_digest'], 'native campaign outputs differ from reviewed configuration')
    verify_lifecycle(NativeCLI('stepfunctions'), NativeCLI('ecs'), config)
    definition = NativeCLI('ecs').describe_task_definition(taskDefinition=fixture['taskDefinitionArn'])
    verify_task(definition, fixture)
    target = outputs['target_readiness']['value']
    run(['aws', 'ecs', 'wait', 'services-stable', '--cluster', config['clusterArn'], '--services', target['service']])
    installed = observed_service(config['clusterArn'], target['service'], {'honua': cfg['server_image']})
    groups = aws('ec2', 'describe-security-groups', '--group-ids', *config['network']['securityGroupIds'], *target['workerSecurityGroupIds'])['SecurityGroups']
    require(len(groups) == len(set(config['network']['securityGroupIds'] + target['workerSecurityGroupIds'])) and
            all(not group['IpPermissions'] for group in groups), 'campaign worker/controller has unexpected inbound access')
    table = aws('dynamodb', 'describe-table', '--table-name', config['dispatchBudget']['claimTable'])['Table']
    require(table['DeletionProtectionEnabled'] is True, 'native dispatch claims lack deletion protection')
    require(aws('s3api', 'get-bucket-versioning', '--bucket', config['custody']['bucket']).get('Status') == 'Enabled', 'native campaign custody is unversioned')
    return {'schema': 'honua.hosted-pilot-campaign-readiness/v1', 'source_sha': os.environ['GITHUB_SHA'],
        'run_id': os.environ['GITHUB_RUN_ID'], 'candidate_key': cfg['candidate_digest'],
        'installed_images': installed, 'native_controller_lifecycle_verified': True,
        'private_fixture_task_verified': True, 'immutable_custody_verified': True,
        'native_agent_configured': outputs['fixture_staging']['value']['nativeAgentConfigured'],
        'sql_roles_and_credentials_qualified': False, 'human_approval_qualified': False,
        'agent_and_model_campaign': 'not-run', 'qualification': False,
        'status': 'infrastructure-ready-private-provisioning-and-genuine-campaign-required'}


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
