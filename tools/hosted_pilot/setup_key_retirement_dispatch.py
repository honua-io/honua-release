#!/usr/bin/env python3
"""Launch one exact private setup retirement; never replay an unknown revoke."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from controller_archive import canonical, digest, require
from controller_budget import consume, read_allowance, read_version
from controller_handoff import ROOT, git, sdk
from setup_key_retirement import validate

CONFIG_PATH = 'certification/agent-e2e/setup-key-retirement-handoff.json'
COMMAND = ['python', 'tools/hosted_pilot/setup_key_retirement.py', 'run']


def verify_definition(task, config):
    require(task['taskDefinitionArn'].rsplit(':', 1)[0] == config['taskDefinitionFamily'] and
            task['taskRoleArn'] == config['taskRoleArn'] and task['executionRoleArn'] == config['executionRoleArn'] and
            task['networkMode'] == 'awsvpc' and task['runtimePlatform'] == {'cpuArchitecture': 'X86_64', 'operatingSystemFamily': 'LINUX'} and
            len(task['containerDefinitions']) == 1, 'retirement native task identities differ')
    container = task['containerDefinitions'][0]
    require(container['name'] == 'setup-key-retirement' and container['image'] == config['imageUri'] and
            container.get('command') == COMMAND and container.get('user') == '10001:10001' and
            container.get('readonlyRootFilesystem') is True and not container.get('privileged', False) and
            not container.get('entryPoint') and not container.get('portMappings') and
            container.get('mountPoints') == [{'sourceVolume': 'work', 'containerPath': '/work', 'readOnly': False}],
            'retirement private command, image or isolation differs')
    require(container.get('environment') == [{'name': 'PILOT_SETUP_RETIREMENT_CONFIG', 'value': canonical(config).decode()}],
            'retirement environment differs from exact reviewed nonsecret configuration')
    secrets = {row['name']: row['valueFrom'] for row in container.get('secrets', [])}
    require(len(container.get('secrets', [])) == 2 and secrets == {
        'PILOT_SETUP_RETIRE_ROOT_KEY': config['rootSecretArn'],
        'PILOT_SETUP_RETIRE_KEY': config['setupKey']['secretArn']+':::'+config['setupKey']['versionId']},
        'retirement must receive only independent issuer and original immutable setup credentials, never SQL or model secrets')


def dispatch(config, *, timeout=600):
    require(set(config) == {'schemaVersion', 'region', 'accountId', 'clusterArn', 'taskDefinitionArn', 'taskRoleArn',
        'executionRoleArn', 'imageUri', 'subnetIds', 'securityGroupIds', 'configuration', 'configurationSha256', 'dispatchBudget'} and
        config['schemaVersion'] == 1 and type(timeout) is int and 60 <= timeout <= 600, 'reviewed bounded setup retirement handoff required')
    setup = config['configuration']
    validate(setup)
    require(digest(canonical(setup)) == config['configurationSha256'] and
            all(config[key] == setup[key] for key in ('region', 'accountId', 'clusterArn', 'taskRoleArn', 'executionRoleArn', 'imageUri')) and
            config['dispatchBudget']['candidateKey'] == setup['candidateKey'] and config['dispatchBudget']['state'] == setup['state'] and
            config['dispatchBudget']['generationDigest'] == setup['generationDigest'] and
            config['dispatchBudget']['claimTable'] == setup['claimTable'], 'retirement handoff changed original identities/accounting')
    ecs, s3, db = (sdk(service, config['region']) for service in ('ecs', 's3', 'dynamodb'))
    require(sdk('sts', config['region']).get_caller_identity()['Account'] == config['accountId'], 'retirement dispatcher account differs')
    definition = ecs.describe_task_definition(taskDefinition=config['taskDefinitionArn'])['taskDefinition']
    require(definition['taskDefinitionArn'] == config['taskDefinitionArn'], 'retirement task revision differs')
    verify_definition(definition, setup)
    row = read_allowance(config['dispatchBudget'], s3, db)
    invocation = digest(canonical({'kind': 'setup-key-retirement', 'configuration': config}))[7:]
    claim_key = {'claimKey': {'S': 'dispatch:'+invocation}}
    created = consume(config['dispatchBudget'], db=db, row=row, invocation=invocation,
        binding_digest=digest(canonical(config)), execution_arn='setup-key-retirement:'+invocation)
    if created:
        launched = ecs.run_task(cluster=config['clusterArn'], taskDefinition=config['taskDefinitionArn'], launchType='FARGATE', count=1,
            clientToken=invocation, enableExecuteCommand=False,
            networkConfiguration={'awsvpcConfiguration': {'subnets': config['subnetIds'], 'securityGroups': config['securityGroupIds'], 'assignPublicIp': 'DISABLED'}})
        require(not launched.get('failures') and len(launched.get('tasks', [])) == 1, 'retirement launch failed; retain original claim and liability')
        task_arn = launched['tasks'][0]['taskArn']
        db.update_item(TableName=config['dispatchBudget']['claimTable'], Key=claim_key,
            ConditionExpression='bindingDigest = :binding AND attribute_not_exists(taskArn)', UpdateExpression='SET taskArn = :task',
            ExpressionAttributeValues={':binding': {'S': digest(canonical(config))}, ':task': {'S': task_arn}})
    else:
        claim = db.get_item(TableName=config['dispatchBudget']['claimTable'], Key=claim_key, ConsistentRead=True)['Item']
        require(claim.get('taskArn', {}).get('S'), 'retirement launch acknowledgement unknown; observe original claim without relaunch')
        task_arn = claim['taskArn']['S']
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        observed = ecs.describe_tasks(cluster=config['clusterArn'], tasks=[task_arn])
        require(not observed.get('failures') and len(observed.get('tasks', [])) == 1, 'retirement task cannot be independently observed')
        task = observed['tasks'][0]
        require(task['taskArn'] == task_arn and task['taskDefinitionArn'] == config['taskDefinitionArn'] and
                len(task['containers']) == 1 and task['containers'][0]['image'] == config['imageUri'] and
                not task.get('enableExecuteCommand', True), 'retirement native task identity changed')
        if task['lastStatus'] == 'STOPPED':
            terminal = retain_terminal(config, db=db, claim_key=claim_key, task=task)
            require(task['containers'][0].get('exitCode') == 0, 'retirement failed; reconcile actual observations without repeating revoke')
            break
        time.sleep(3)
    else:
        ecs.stop_task(cluster=config['clusterArn'], task=task_arn, reason='Reviewed setup retirement deadline exhausted; retain original liability')
        raise ValueError('retirement deadline exhausted; observe original task before recovery')
    response = s3.get_object(Bucket=setup['bucket'], Key=setup['receiptKey'])
    require(response.get('VersionId') not in (None, '', 'null'), 'retirement receipt is not immutable')
    raw = response['Body'].read(4*1024*1024+1)
    require(len(raw) <= 4*1024*1024, 'retirement receipt exceeds bound')
    receipt = json.loads(raw)
    require(receipt['schema'] == 'honua.private-setup-key-retirement/v1' and receipt['candidateKey'] == setup['candidateKey'] and
            receipt['configurationDigest'] == config['configurationSha256'] and receipt['operation'] == setup['operation'] and
            receipt['setupKey'] == setup['setupKey'] and receipt['fixtureReference'] == setup['fixtureReference'] and
            receipt['native']['taskArn'] == task_arn and receipt['native']['taskDefinitionArn'] == config['taskDefinitionArn'] and
            receipt['native']['imageUri'] == config['imageUri'] and receipt['postRevocationProbeStatus'] == 401 and
            receipt['revokedKeyId'] == setup['setupKey']['apiKeyId'] and receipt['qualification'] is False and receipt['mode'] == setup['mode'],
            'retirement receipt differs from original setup identity or native outcome')
    return {'schema': 'honua.private-setup-key-retirement-outcome/v1', 'status': 'completed', 'qualification': False,
        'taskArn': task_arn, 'taskDefinitionArn': config['taskDefinitionArn'], 'terminalObservation': terminal,
        'claim': {'table': config['dispatchBudget']['claimTable'], 'key': claim_key['claimKey']['S'], 'bindingDigest': digest(canonical(config))},
        'receiptObject': {'bucket': setup['bucket'], 'key': setup['receiptKey'], 'versionId': response['VersionId'], 'sha256': digest(raw)},
        'revokedKeyId': setup['setupKey']['apiKeyId'], 'postRevocationProbeStatus': 401}


def retain_terminal(config, *, db, claim_key, task):
    """Keep native termination before inspecting application or S3 success."""
    code = task['containers'][0].get('exitCode')
    require(task['lastStatus'] == 'STOPPED' and type(code) is int and 0 <= code <= 255,
            'retirement has no actual terminal container exit')
    terminal = {'taskArn': task['taskArn'], 'taskDefinitionArn': task['taskDefinitionArn'],
                'imageUri': task['containers'][0]['image'], 'lastStatus': 'STOPPED', 'exitCode': code}
    # Repeating a read may acknowledge the same terminal bytes. Neither an
    # application failure nor a lost receipt acknowledgement erases this proof.
    db.update_item(TableName=config['dispatchBudget']['claimTable'], Key=claim_key,
        ConditionExpression='bindingDigest = :binding AND taskArn = :task AND (attribute_not_exists(terminalDigest) OR terminalDigest = :terminal)',
        UpdateExpression='SET terminalDigest = :terminal, terminalObservation = :observation',
        ExpressionAttributeValues={':binding': {'S': digest(canonical(config))}, ':task': {'S': task['taskArn']},
            ':terminal': {'S': digest(canonical(terminal))}, ':observation': {'S': canonical(terminal).decode()}})
    return terminal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    require(os.environ.get('GITHUB_REPOSITORY') == 'honua-io/honua-release' and os.environ.get('GITHUB_REF') == 'refs/heads/trunk' and
            os.environ.get('GITHUB_WORKFLOW_REF') == 'honua-io/honua-release/.github/workflows/hosted-pilot-setup-retirement.yml@refs/heads/trunk',
            'setup retirement requires the fixed protected workflow')
    require(git('rev-parse', 'HEAD').decode().strip() == os.environ['GITHUB_SHA'], 'retirement source differs from immutable checkout')
    path = ROOT/CONFIG_PATH
    git('ls-files', '--error-unmatch', '--', CONFIG_PATH)
    require(path.is_file() and not path.is_symlink(), 'review the actual retirement handoff into source first')
    config = json.loads(path.read_text())
    require(config == json.loads(git('show', os.environ['GITHUB_SHA']+':'+CONFIG_PATH)), 'retirement handoff differs from checked source')
    result = dispatch(config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical(result)+b'\n')
    print('Original setup key revoked; actual private task stopped. Fresh agent worker and independent case custody evidence remain required.')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('setup retirement refused; retain original claims, observations and costs for reconciliation', file=sys.stderr)
        sys.exit(1)
