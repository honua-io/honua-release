#!/usr/bin/env python3
"""Launch the exact reviewed private native-bootstrap task once per operation."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from controller_archive import canonical, digest, require
from controller_budget import read_allowance, consume
from controller_handoff import ROOT, git, sdk
from native_bootstrap import validate

CONFIG_PATH = 'certification/agent-e2e/native-bootstrap-handoff.json'
COMMAND = ['python', 'tools/hosted_pilot/native_bootstrap.py']


def verify_task(ecs, config):
    task = ecs.describe_task_definition(taskDefinition=config['taskDefinitionArn'])['taskDefinition']
    require(task['taskDefinitionArn'] == config['taskDefinitionArn'] and task['taskRoleArn'] == config['taskRoleArn'] and
            task['executionRoleArn'] == config['executionRoleArn'] and task['networkMode'] == 'awsvpc' and
            task['runtimePlatform'] == {'cpuArchitecture': 'X86_64', 'operatingSystemFamily': 'LINUX'} and
            len(task['containerDefinitions']) == 1, 'native bootstrap task identities differ')
    container = task['containerDefinitions'][0]
    require(container['name'] == 'native-bootstrap' and container['image'] == config['imageUri'] and
            container.get('command') == COMMAND and container.get('user') == '10001:10001' and
            container.get('readonlyRootFilesystem') is True and not container.get('privileged', False) and
            not container.get('entryPoint') and not container.get('portMappings'), 'native bootstrap command/isolation differs')
    environment = {row['name']: row['value'] for row in container.get('environment', [])}
    require(set(environment) == {'PILOT_NATIVE_BOOTSTRAP_CONFIG'} and
            json.loads(environment['PILOT_NATIVE_BOOTSTRAP_CONFIG']) == config['configuration'], 'native bootstrap task configuration differs')
    expected_secrets = {'PILOT_FIXTURE_MASTER_CONNECTION', 'PILOT_FIXTURE_ADMIN_PASSWORD',
        'HONUA_AGENT_FIXTURE_CLEANUP_DSN', 'HONUA_AGENT_FIXTURE_OBSERVER_DSN'}
    memberships = config['configuration'].get('humanMemberships')
    if memberships is not None:
        expected_secrets.add('HONUA_NATIVE_SCIM_TOKEN')
        for name, kind in (('HONUA_NATIVE_SCIM_TOKEN', 'scim'), ('PILOT_FIXTURE_ADMIN_PASSWORD', 'root')):
            reference = memberships['credentials'][kind]
            require([row['valueFrom'] for row in container['secrets'] if row['name'] == name] ==
                [reference['secretArn']+':::'+reference['versionId']], 'native bootstrap human credential version differs')
    require({row['name'] for row in container['secrets']} == expected_secrets and
            len(container['secrets']) == len(expected_secrets), 'native bootstrap secret injection differs')
    require(container.get('mountPoints') == [{'sourceVolume': 'work', 'containerPath': '/work', 'readOnly': False}], 'native bootstrap writable mounts differ')


def dispatch(config, *, timeout=600):
    require(set(config) == {'schemaVersion', 'region', 'accountId', 'clusterArn', 'taskDefinitionArn', 'taskRoleArn',
        'executionRoleArn', 'containerName', 'imageUri', 'subnetIds', 'securityGroupIds', 'configuration', 'configurationSha256', 'dispatchBudget'} and
        config['schemaVersion'] == 1 and config['containerName'] == 'native-bootstrap', 'native bootstrap handoff differs')
    setup = config['configuration']
    validate(setup)
    require(digest(canonical(setup)) == config['configurationSha256'] and
            setup['region'] == config['region'] and setup['accountId'] == config['accountId'] and
            setup['clusterArn'] == config['clusterArn'] and setup['imageUri'] == config['imageUri'] and
            config['dispatchBudget']['candidateKey'] == setup['candidateKey'] and config['dispatchBudget']['state'] == setup['state'] and
            config['dispatchBudget']['generationDigest'] == setup['generationDigest'], 'native bootstrap accounting binding differs')
    require(type(timeout) is int and 60 <= timeout <= 600, 'native bootstrap deadline must be bounded')
    ecs, s3, db = (sdk(service, config['region']) for service in ('ecs', 's3', 'dynamodb'))
    require(sdk('sts', config['region']).get_caller_identity()['Account'] == config['accountId'], 'native bootstrap dispatch AWS account differs')
    verify_task(ecs, config)
    row = read_allowance(config['dispatchBudget'], s3, db)
    invocation = digest(canonical({'kind': 'native-bootstrap', 'configuration': config}))[7:]
    claim_key = {'claimKey': {'S': 'dispatch:'+invocation}}
    created = consume(config['dispatchBudget'], db=db, row=row, invocation=invocation,
                      binding_digest=digest(canonical(config)), execution_arn='native-bootstrap:'+invocation)
    if created:
        # A lost launch acknowledgement never authorizes another paid launch.
        launched = ecs.run_task(cluster=config['clusterArn'], taskDefinition=config['taskDefinitionArn'], launchType='FARGATE',
            count=1, clientToken=invocation, enableExecuteCommand=False,
            networkConfiguration={'awsvpcConfiguration': {'subnets': config['subnetIds'], 'securityGroups': config['securityGroupIds'], 'assignPublicIp': 'DISABLED'}})
        require(not launched.get('failures') and len(launched.get('tasks', [])) == 1, 'native bootstrap launch failed; retain claim/liability')
        task_arn = launched['tasks'][0]['taskArn']
        db.update_item(TableName=config['dispatchBudget']['claimTable'], Key=claim_key,
            ConditionExpression='bindingDigest = :binding AND attribute_not_exists(taskArn)', UpdateExpression='SET taskArn = :task',
            ExpressionAttributeValues={':binding': {'S': digest(canonical(config))}, ':task': {'S': task_arn}})
    else:
        claim = db.get_item(TableName=config['dispatchBudget']['claimTable'], Key=claim_key, ConsistentRead=True)['Item']
        require(claim.get('taskArn', {}).get('S'), 'native bootstrap launch acknowledgement unknown; reconcile without relaunch')
        task_arn = claim['taskArn']['S']
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        observed = ecs.describe_tasks(cluster=config['clusterArn'], tasks=[task_arn])
        require(not observed.get('failures') and len(observed.get('tasks', [])) == 1, 'native bootstrap task cannot be observed; retain liability')
        task = observed['tasks'][0]
        require(task['taskDefinitionArn'] == config['taskDefinitionArn'] and len(task['containers']) == 1 and
                task['containers'][0]['image'] == config['imageUri'] and not task.get('enableExecuteCommand', True), 'native bootstrap task/image changed')
        if task['lastStatus'] == 'STOPPED':
            require(task['containers'][0].get('exitCode') == 0, 'native bootstrap failed; reconcile effects without replay')
            break
        time.sleep(3)
    else:
        ecs.stop_task(cluster=config['clusterArn'], task=task_arn, reason='Reviewed native bootstrap deadline exhausted; retain original liabilities')
        raise ValueError('native bootstrap deadline exhausted; observe task before recovery')
    response = s3.get_object(Bucket=setup['bucket'], Key=setup['receiptKey'])
    raw = response['Body'].read(1024*1024+1)
    require(response.get('VersionId') not in (None, '', 'null') and len(raw) <= 1024*1024, 'native bootstrap receipt lacks immutable custody')
    receipt = json.loads(raw)
    require(receipt['schema'] == 'honua.private-native-bootstrap/v1' and receipt['candidateKey'] == setup['candidateKey'] and
            receipt['configurationDigest'] == config['configurationSha256'] and receipt['operation'] == setup['operation'] and
            receipt['native']['taskArn'] == task_arn and receipt['native']['taskDefinitionArn'] == config['taskDefinitionArn'] and
            receipt['native']['imageUri'] == config['imageUri'] and receipt['qualification'] is False, 'native bootstrap receipt differs')
    return {'schema': 'honua.private-native-bootstrap-outcome/v1', 'status': 'completed', 'qualification': False,
        'taskArn': task_arn, 'configurationDigest': config['configurationSha256'],
        'receiptObject': {'bucket': setup['bucket'], 'key': setup['receiptKey'], 'versionId': response['VersionId'], 'sha256': digest(raw)},
        'issuedKeys': {kind: {name: record[name] for name in ('apiKeyId', 'secretArn', 'versionId', 'permissions')} for kind, record in receipt['keys'].items()},
        'requiresReviewedAgentPrincipalInstallation': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(os.environ.get('GITHUB_REPOSITORY') == 'honua-io/honua-release' and os.environ.get('GITHUB_REF') == 'refs/heads/trunk' and
            os.environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and re.fullmatch(r'[a-f0-9]{40}', os.environ.get('GITHUB_SHA', '')), 'native bootstrap requires checked trunk workflow')
    require(git('rev-parse', 'HEAD').decode().strip() == os.environ['GITHUB_SHA'], 'native bootstrap source differs')
    path = ROOT/CONFIG_PATH
    require(path.is_file() and not path.is_symlink(), 'review actual native bootstrap handoff into source before dispatch')
    git('ls-files', '--error-unmatch', '--', CONFIG_PATH)
    config = json.loads(path.read_text())
    require(config == json.loads(git('show', os.environ['GITHUB_SHA']+':'+CONFIG_PATH)), 'native bootstrap handoff differs from immutable source')
    result = dispatch(config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical(result)+b'\n')
    print('Native bootstrap outcome retained; install actual issued agent principal by reviewed saved plan before workspace setup.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('native bootstrap refused; retain original claim/task/costs for reconciliation', file=sys.stderr)
        sys.exit(1)
