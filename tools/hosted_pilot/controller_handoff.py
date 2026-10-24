#!/usr/bin/env python3
"""Dispatch a reserved private controller invocation and retain its native outcome."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
from controller_archive import BUNDLE_LIMIT, SOURCE_LIMIT, bundle_bytes, canonical, contract, digest, extract, portable, require
from controller_budget import consume, read_allowance, read_version

ROOT = Path(__file__).resolve().parents[2]
COMMAND = ['python', 'tools/hosted_pilot/controller_entry.py']
CONFIG_PATH = 'certification/agent-e2e/controller-handoff.json'
WORKFLOW = 'agent-e2e-campaign.yml'
PREPARE_WORKFLOW = 'hosted-pilot-prepare.yml'
PREPARATIONS = {'prepare-native-context', 'prepare-studio-fixture', 'capture-console-session', 'prepare-installed-mcp-base'}


def sdk(service, region):
    import boto3
    from botocore.config import Config
    return boto3.client(service, region_name=region,
        config=Config(connect_timeout=5, read_timeout=15, retries={'total_max_attempts': 1}))


def git(*args):
    result = subprocess.run(['git', *args], cwd=ROOT, check=True, capture_output=True)
    return result.stdout


OBSERVATION_FIELDS = {'schemaVersion', 'region', 'accountId', 'clusterArn', 'taskDefinitionArn', 'taskRoleArn', 'executionRoleArn', 'containerName', 'image', 'network', 'lifecycle', 'custody', 'maxSeconds'}


def model_runtime(value, region, account_id):
    require(isinstance(value, dict) and set(value) == {'modelId', 'returnedModelId', 'profileArn', 'tokenCountingModelArn', 'profileSha256'} and
            all(isinstance(value[name], str) and value[name] for name in value) and
            re.fullmatch(r'[A-Za-z0-9.:/_-]{1,256}', value['modelId']) and
            re.fullmatch(r'[A-Za-z0-9.:/_-]{1,256}', value['returnedModelId']) and
            re.fullmatch(r'arn:aws:bedrock:'+re.escape(region)+':'+re.escape(account_id)+r':inference-profile/[A-Za-z0-9.:-]+', value['profileArn']) and
            value['modelId'] in (value['profileArn'], value['profileArn'].split('/')[-1]) and
            re.fullmatch(r'arn:aws:bedrock:'+re.escape(region)+r'::foundation-model/[A-Za-z0-9.:-]+', value['tokenCountingModelArn']) and
            re.fullmatch(r'sha256:[a-f0-9]{64}', value['profileSha256']), 'invalid exact controller model runtime binding')
    return {'TERMINAL_MODEL_NAME': value['modelId'], 'HONUA_CONTROLLER_MODEL_RUNTIME': canonical(value).decode()}


def validate_observation(config):
    require(isinstance(config, dict) and set(config) in (OBSERVATION_FIELDS, OBSERVATION_FIELDS | {'model'}) and config['schemaVersion'] == 1,
            'invalid read-only controller observation')
    if 'model' in config:
        model_runtime(config['model'], config['region'], config['accountId'])
    require(re.fullmatch(r'[0-9]{12}', config['accountId']) and re.fullmatch(r'[a-z]{2}-[a-z]+-[0-9]', config['region']), 'controller account/region missing')
    regional = ':' + config['region'] + ':' + config['accountId'] + ':'
    require(re.fullmatch(re.escape('arn:aws:ecs' + regional) + r'cluster/honua-pilot-[a-z0-9-]+', config['clusterArn']) and re.fullmatch(re.escape('arn:aws:ecs' + regional) + r'task-definition/honua-pilot-[a-z0-9-]+-campaign-controller:[1-9][0-9]*', config['taskDefinitionArn']), 'controller task revision/cluster must be exact pilot resources')
    require(config['containerName'] == 'campaign-controller' and config['taskRoleArn'] != config['executionRoleArn'], 'controller task/execution identities must differ')
    for key in ('taskRoleArn', 'executionRoleArn'):
        require(re.fullmatch('arn:aws:iam::' + config['accountId'] + r':role/honua-pilot-[a-z0-9-]+', config[key]), 'controller role outside reviewed namespace')
    image = config['image']
    require(set(image) == {'uri', 'sourceSha', 'treeId', 'archiveSha256', 'runtimeContractSha256'}, 'controller image provenance incomplete')
    require(re.fullmatch(re.escape(config['accountId'] + '.dkr.ecr.' + config['region'] + '.amazonaws.com/') + r'honua-pilot-[a-z0-9-]+-trusted-controller@sha256:[a-f0-9]{64}', image['uri']), 'controller requires immutable reviewed environment image')
    for key, pattern in [('sourceSha', r'[a-f0-9]{40}'), ('treeId', r'[a-f0-9]{40}'), ('archiveSha256', r'[a-f0-9]{64}'), ('runtimeContractSha256', r'sha256:[a-f0-9]{64}')]:
        require(re.fullmatch(pattern, image[key]), 'controller image source identity invalid')
    require(set(config['network']) == {'subnetIds', 'securityGroupIds'}, 'controller private network shape differs')
    for key, prefix in [('subnetIds', 'subnet-'), ('securityGroupIds', 'sg-')]:
        rows = config['network'][key]
        require(isinstance(rows, list) and rows and len(rows) == len(set(rows)) and all(re.fullmatch(prefix + '[a-f0-9]+', value) for value in rows), 'controller private network IDs missing')
    lifecycle = config['lifecycle']
    require(set(lifecycle) == {'stateMachineArn', 'stateMachineRoleArn', 'definitionDigest'} and re.fullmatch(re.escape('arn:aws:states' + regional) + r'stateMachine:honua-pilot-[a-z0-9-]+-campaign-controller:[1-9][0-9]*', lifecycle['stateMachineArn']) and re.fullmatch('arn:aws:iam::' + config['accountId'] + r':role/honua-pilot-[a-z0-9-]+', lifecycle['stateMachineRoleArn']) and re.fullmatch(r'sha256:[a-f0-9]{64}', lifecycle['definitionDigest']), 'controller lifecycle must be an immutable published Standard state-machine version')
    custody = config['custody']
    require(set(custody) == {'bucket', 'requestPrefix', 'sourcePrefix', 'bundlePrefix', 'outcomePrefix', 'evidencePrefix'} and re.fullmatch(r'honua-pilot-[a-z0-9-]+-controller-[a-z0-9-]+', custody['bucket']), 'controller handoff custody differs')
    for key in set(custody) - {'bucket'}:
        portable(custody[key].rstrip('/'))
        require(custody[key].startswith('controller/') and custody[key].endswith('/'), 'controller custody prefix must be bounded and retained')
    require(type(config['maxSeconds']) is int and 60 <= config['maxSeconds'] <= 7200, 'controller observation deadline differs')


def validate_configuration(config):
    require(isinstance(config, dict) and set(config) in (OBSERVATION_FIELDS | {'dispatchBudget'}, OBSERVATION_FIELDS | {'dispatchBudget', 'model'}) and config['schemaVersion'] == 1,
            'invalid reviewed private controller configuration')
    validate_observation({key: value for key, value in config.items() if key != 'dispatchBudget'})
    budget = config['dispatchBudget']
    require(set(budget) == {'state', 'candidateKey', 'generationDigest', 'reservationId', 'operationId', 'basisDigest', 'perDispatchCeilingUsd', 'maximumDispatches', 'claimTable'} and set(budget['state']) == {'region', 'bucket', 'table'} and budget['state']['region'] == config['region'], 'controller dispatch requires an existing native allowance')
    for key in ('candidateKey', 'generationDigest', 'basisDigest'):
        require(re.fullmatch(r'sha256:[a-f0-9]{64}', budget[key]), 'controller accounting identity missing')
    require(re.fullmatch(r'honua-pilot-[a-z0-9-]+-controller-dispatch-claims', budget['claimTable']) and type(config['maxSeconds']) is int and 60 <= config['maxSeconds'] <= 7200, 'controller dispatch claims/deadline differ')


def invocation_identity(campaign, *, operation, case_id, target, shard, shards):
    require(operation in ('execute', 'finalize', 'reconcile-accounting'), 'invalid controller operation')
    require(type(shard) is int and type(shards) is int and shards == 6 and 0 <= shard < shards, 'qualification retains six bounded shards')
    require(case_id is None or isinstance(case_id, str) and re.fullmatch(r'[A-Za-z0-9:_-]{1,160}', case_id), 'invalid explicit development case')
    require(target is None or re.fullmatch(r'[a-z0-9-]{1,80}', target), 'invalid campaign target')
    require(operation == 'execute' or (case_id is None and target is None and shard == 0), 'finalization/accounting cannot select partial cases')
    require(campaign['campaignMode'] != 'qualification' or (case_id is None and (operation != 'execute' or target is not None)), 'qualification cannot become a partial campaign')
    require(campaign['campaignMode'] != 'development' or operation != 'execute' or case_id is not None, 'development must bind an explicit case')
    return {'schema': 'honua.private-controller-invocation/v1', 'candidateKey': campaign['componentBindingDigest'],
        'campaignDigest': digest(canonical(campaign)), 'controller': campaign['controller'],
        'operation': operation, 'caseId': case_id, 'target': target, 'shard': shard, 'shards': shards}


def bundle_identity(bundle, *, operation, case_id, target, shard, shards):
    """Preparation has its own manifest and workflow; it is never a frozen campaign."""
    bundle = Path(bundle)
    if operation not in PREPARATIONS:
        document = json.loads((bundle/'campaign.json').read_text())
        return document, invocation_identity(document, operation=operation, case_id=case_id, target=target, shard=shard, shards=shards), json.loads((bundle/'producer-inputs.json').read_text())['state']
    require(case_id is None and target is None and shard == 0 and shards == 6, 'preparation cannot select model cases or shards')
    document = json.loads((bundle/'preparation.json').read_text())
    require(set(document) == {'schema', 'componentBindingDigest', 'controller', 'expiresAt', 'state', 'operation', 'configuration'} and
            document['schema'] == 'honua.private-controller-preparation/v1' and document['operation'] == operation and
            document['controller']['workflow'] == PREPARE_WORKFLOW and
            re.fullmatch(r'sha256:[a-f0-9]{64}', document['componentBindingDigest']), 'invalid distinct private preparation manifest')
    reference = document['configuration']
    require(set(reference) == {'path', 'sha256'} and re.fullmatch(r'sha256:[a-f0-9]{64}', reference['sha256']), 'preparation needs exact reviewed source configuration')
    portable(reference['path'])
    require(reference['path'].startswith('certification/agent-e2e/') and reference['path'].endswith('.json'), 'preparation configuration escaped reviewed source namespace')
    return document, {'schema': 'honua.private-controller-preparation-invocation/v1',
        'candidateKey': document['componentBindingDigest'], 'preparationDigest': digest(canonical(document)),
        'controller': document['controller'], 'operation': operation, 'caseId': None, 'target': None, 'shard': 0, 'shards': 6}, document['state']


def verify_lifecycle(sm, ecs, config):
    observed = sm.describe_state_machine(stateMachineArn=config['lifecycle']['stateMachineArn'])
    require(observed['type'] == 'STANDARD' and observed['roleArn'] == config['lifecycle']['stateMachineRoleArn'] and digest(canonical(json.loads(observed['definition']))) == config['lifecycle']['definitionDigest'], 'controller lifecycle source/identity changed')
    definition = json.loads(observed['definition'])
    require(definition.get('TimeoutSeconds', 0) <= config['maxSeconds'] and definition.get('TimeoutSeconds', 0) >= 60 and set(definition['States']) == {'RunController'}, 'controller lifecycle must be one bounded no-redrive task')
    state = definition['States']['RunController']
    params = state['Parameters']
    require(state['Type'] == 'Task' and state['Resource'] == 'arn:aws:states:::ecs:runTask.sync' and not state.get('Retry') and state.get('End') is True and params['Cluster'] == config['clusterArn'] and params['TaskDefinition'] == config['taskDefinitionArn'] and params['LaunchType'] == 'FARGATE' and params.get('Count', 1) == 1 and params.get('EnableExecuteCommand', False) is False, 'controller lifecycle cannot run arbitrary or repeated tasks')
    require(params['NetworkConfiguration'] == {'AwsvpcConfiguration': {'Subnets': config['network']['subnetIds'], 'SecurityGroups': config['network']['securityGroupIds'], 'AssignPublicIp': 'DISABLED'}} and params['Overrides'] == {'ContainerOverrides': [{'Name': config['containerName'], 'Environment': [{'Name': 'PILOT_CONTROLLER_REQUEST', 'Value.$': '$.request'}]}]}, 'controller lifecycle cannot accept command, credential or network overrides')
    task = ecs.describe_task_definition(taskDefinition=config['taskDefinitionArn'])['taskDefinition']
    require(task['taskDefinitionArn'] == config['taskDefinitionArn'] and task['taskRoleArn'] == config['taskRoleArn'] and task['executionRoleArn'] == config['executionRoleArn'] and task['networkMode'] == 'awsvpc' and task.get('runtimePlatform') == {'cpuArchitecture': 'X86_64', 'operatingSystemFamily': 'LINUX'} and len(task['containerDefinitions']) == 1, 'controller task identity/platform differs')
    container = task['containerDefinitions'][0]
    require(container['name'] == config['containerName'] and container['image'] == config['image']['uri'] and container.get('command') == COMMAND and container.get('readonlyRootFilesystem') is True and container.get('user') == '10001:10001' and not container.get('privileged', False) and not container.get('entryPoint') and not container.get('portMappings'), 'controller immutable image/command/isolation differs')
    expected_model = model_runtime(config['model'], config['region'], config['accountId']) if 'model' in config else {}
    actual_model = [row for row in container.get('environment', []) if row.get('name') in ('TERMINAL_MODEL_NAME', 'HONUA_CONTROLLER_MODEL_RUNTIME')]
    require(len(actual_model) == len(expected_model) and {row['name']: row['value'] for row in actual_model} == expected_model,
            'controller task model environment differs from reviewed runtime binding')
    return observed


def put_once(s3, bucket, key, body):
    try:
        response = s3.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch='*', ServerSideEncryption='AES256')
        version = response.get('VersionId')
    except Exception as error:
        if getattr(error, 'response', {}).get('Error', {}).get('Code') != 'PreconditionFailed':
            raise
        response = s3.get_object(Bucket=bucket, Key=key)
        version = response.get('VersionId')
        require(response['Body'].read(len(body) + 1) == body, 'controller immutable custody object already has different bytes')
    require(version and version != 'null', 'controller handoff custody must retain immutable versions')
    return {'bucket': bucket, 'key': key, 'versionId': version, 'sha256': digest(body)}


def verify_runtime_task(ecs, ecr, config, task_arn):
    response = ecs.describe_tasks(cluster=config['clusterArn'], tasks=[task_arn])
    require(not response.get('failures') and len(response.get('tasks', [])) == 1, 'native controller task cannot be observed')
    task = response['tasks'][0]
    require(task['taskArn'] == task_arn and task['clusterArn'] == config['clusterArn'] and task['taskDefinitionArn'] == config['taskDefinitionArn'] and task['lastStatus'] == 'STOPPED' and len(task['containers']) == 1 and not task.get('enableExecuteCommand', False), 'native controller task identity/status differs')
    container = task['containers'][0]
    require(container['name'] == config['containerName'] and container['image'] == config['image']['uri'] and container.get('exitCode') == 0, 'private controller transport failed or image changed')
    repository, expected = config['image']['uri'].split('@')
    manifest_response = ecr.batch_get_image(registryId=config['accountId'], repositoryName=repository.split('/', 1)[1], imageIds=[{'imageDigest': expected}])
    require(not manifest_response.get('failures') and len(manifest_response.get('images', [])) == 1, 'native controller manifest cannot be observed')
    image = manifest_response['images'][0]
    require(image['imageId']['imageDigest'] == expected and digest(image['imageManifest'].encode()) == expected, 'controller registry manifest identity differs')
    manifest = json.loads(image['imageManifest'])
    allowed = [expected]
    if 'manifests' in manifest:
        allowed = [entry['digest'] for entry in manifest['manifests'] if entry.get('platform') == {'architecture': 'amd64', 'os': 'linux'}]
        require(len(allowed) == 1, 'controller index must bind exactly one Linux amd64 image')
    require(container.get('imageDigest') in allowed, 'actual controller image digest is not its reviewed platform manifest')
    return task


def restore_artifacts(outcome, *, configuration, bundle, evidence_directory):
    """Restore only version/hash-bound outputs from the authenticated native outcome."""
    import shutil
    import tempfile
    config, custody = configuration, configuration['custody']
    s3 = sdk('s3', config['region'])
    immutable_names = {'campaign.json', 'producer-inputs.json', 'qualification-lock.json', 'expectations.json', 'candidate-provenance.json', 'preparation.json', 'budget-bootstrap-original.json'}
    final_names = immutable_names | {'trusted-inventory.json', 'budget-ledger.json', 'attempt-registry.json', 'qualification-summary.json'}
    expected_inputs = {name: digest((Path(bundle) / name).read_bytes()) for name in immutable_names if (Path(bundle) / name).is_file()}
    prefix = custody['evidencePrefix'] + outcome['invocationId'] + '/'
    artifacts = outcome['artifacts']
    require(set(artifacts) == ({'evidence', 'bundle'} if outcome['invocation']['operation'] == 'finalize' and outcome['exitCode'] == 0 else {'evidence'}), 'private controller artifact set differs from its operation/outcome')
    Path(evidence_directory).mkdir(mode=0o700, parents=True, exist_ok=True)
    for kind, reference in artifacts.items():
        require(set(reference) == {'archive', 'manifest'}, 'private artifact references are incomplete')
        for object_ref in reference.values():
            require(set(object_ref) == {'bucket', 'key', 'versionId', 'sha256'} and object_ref['bucket'] == custody['bucket'] and object_ref['key'].startswith(prefix), 'private controller artifact escaped its invocation custody')
        manifest_raw = read_version(s3, custody['bucket'], reference['manifest']['key'], reference['manifest']['versionId'], limit=4 * 1024 * 1024)
        require(digest(manifest_raw) == reference['manifest']['sha256'], 'private artifact manifest digest differs')
        manifest = json.loads(manifest_raw)
        require(set(manifest) == {'schema', 'kind', 'invocationId', 'archive', 'immutableInputs', 'files'} and manifest['schema'] == 'honua.private-controller-artifacts/v1' and manifest['kind'] == ('finalized-bundle' if kind == 'bundle' else 'private-evidence') and manifest['invocationId'] == outcome['invocationId'] and manifest['archive'] == reference['archive'] and manifest['immutableInputs'] == expected_inputs, 'private artifact manifest does not bind frozen inputs/invocation')
        raw = read_version(s3, custody['bucket'], reference['archive']['key'], reference['archive']['versionId'], limit=BUNDLE_LIMIT)
        require(digest(raw) == reference['archive']['sha256'], 'private artifact archive digest differs')
        with tempfile.TemporaryDirectory(prefix='controller-verified-', dir=evidence_directory) as private:
            restored = extract(raw, Path(private) / 'verified', limit=BUNDLE_LIMIT)
            actual = [{'path': path.relative_to(restored).as_posix(), 'size': path.stat().st_size, 'sha256': digest(path.read_bytes())} for path in sorted(restored.rglob('*')) if path.is_file()]
            require(actual == manifest['files'], 'private artifact files differ from authenticated manifest')
            if kind == 'bundle':
                require(all(row['path'] in final_names or row['path'].startswith(('receipts/', 'evidence/')) for row in actual), 'finalized output contains an unapproved path')
                require(all((restored / name).is_file() and digest((restored / name).read_bytes()) == value for name, value in expected_inputs.items()), 'finalized output changed immutable producer inputs')
                for row in actual:
                    destination = Path(bundle) / row['path']
                    require(not destination.is_symlink() and all(not parent.is_symlink() for parent in destination.parents), 'finalized output destination contains a symbolic link')
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    temporary = destination.with_name(destination.name + '.controller-verified')
                    require(not temporary.exists(), 'finalized output staging collision')
                    shutil.copyfile(restored / row['path'], temporary)
                    os.replace(temporary, destination)
            else:
                target = Path(evidence_directory) / 'private-evidence'
                require(not target.exists(), 'private evidence destination must be fresh')
                shutil.copytree(restored, target)
    return outcome


def dispatch_campaign(bundle, *, configuration, operation='execute', case_id=None, target=None, shard=0, shards=6, timeout=7200):
    config = configuration
    validate_configuration(config)
    require(type(timeout) is int and 60 <= timeout <= config['maxSeconds'], 'controller caller timeout exceeds reviewed maximum')
    bundle = Path(bundle)
    campaign, identity, state = bundle_identity(bundle, operation=operation, case_id=case_id, target=target, shard=shard, shards=shards)
    producer = campaign['controller']
    expected_workflow = PREPARE_WORKFLOW if operation in PREPARATIONS else WORKFLOW
    require(producer['repository'] == 'honua-io/honua-release' and producer['workflow'] == expected_workflow and producer['sourceSha'] == os.environ['GITHUB_SHA'] and str(producer['runId']) == os.environ['GITHUB_RUN_ID'] and int(producer['runAttempt']) <= int(os.environ['GITHUB_RUN_ATTEMPT']) and os.environ['GITHUB_REF'] == 'refs/heads/trunk' and os.environ['GITHUB_EVENT_NAME'] == 'workflow_dispatch', 'controller dispatch differs from frozen trusted producer')
    require(campaign['componentBindingDigest'] == config['dispatchBudget']['candidateKey'] and state == config['dispatchBudget']['state'], 'private invocation accounting target differs')
    require(datetime.fromisoformat(campaign['expiresAt'].replace('Z', '+00:00')) > datetime.now(timezone.utc), 'frozen campaign expired')
    source_sha = git('rev-parse', 'HEAD').decode().strip()
    require(source_sha == producer['sourceSha'], 'controller execution source differs from frozen producer')
    git('ls-files', '--error-unmatch', '--', CONFIG_PATH)
    require(json.loads(git('show', source_sha + ':' + CONFIG_PATH)) == config, 'controller config differs from reviewed immutable source')
    require(contract(ROOT) == config['image']['runtimeContractSha256'], 'controller entry/dependencies changed; rebuild the environment image')
    source = git('archive', '--format=tar', source_sha)
    require(len(source) <= SOURCE_LIMIT, 'controller source archive exceeds bounded size')
    source_tree = git('rev-parse', source_sha + '^{tree}').decode().strip()
    frozen = bundle_bytes(bundle)
    s3, db, sm, ecs = (sdk(service, config['region']) for service in ('s3', 'dynamodb', 'stepfunctions', 'ecs'))
    require(sdk('sts', config['region']).get_caller_identity()['Account'] == config['accountId'], 'controller dispatch AWS account differs')
    verify_lifecycle(sm, ecs, config)
    row = read_allowance(config['dispatchBudget'], s3, db)
    require(s3.get_bucket_versioning(Bucket=config['custody']['bucket']).get('Status') == 'Enabled', 'controller handoff storage is not versioned')
    invocation = digest(canonical(identity))[7:]
    custody = config['custody']
    source_ref = put_once(s3, custody['bucket'], custody['sourcePrefix'] + digest(source)[7:] + '.tar', source)
    bundle_ref = put_once(s3, custody['bucket'], custody['bundlePrefix'] + digest(frozen)[7:] + '.tar', frozen)
    request = {'schema': 'honua.private-controller-request/v1', 'invocation': identity, 'invocationId': invocation,
        'configuration': config, 'configurationDigest': digest(canonical(config)), 'image': config['image'],
        'source': source_ref | {'sourceSha': source_sha, 'treeId': source_tree}, 'bundle': bundle_ref}
    raw = canonical(request)
    request_ref = put_once(s3, custody['bucket'], custody['requestPrefix'] + invocation + '.json', raw)
    machine = config['lifecycle']['stateMachineArn']
    execution_arn = machine.replace(':stateMachine:', ':execution:').rsplit(':', 1)[0] + ':' + invocation
    consume(config['dispatchBudget'], db=db, row=row, invocation=invocation,
            binding_digest=digest(raw), execution_arn=execution_arn)
    # The same Standard execution name AND input survive lost acknowledgements.
    # Never redrive or select a new invocation after an ambiguous response.
    execution_input = canonical({'request': canonical(request_ref).decode()}).decode()
    try:
        result = sm.start_execution(stateMachineArn=machine, name=invocation, input=execution_input)
        require(result['executionArn'] == execution_arn, 'native controller execution identity differs')
    except Exception as error:
        if getattr(error, 'response', {}).get('Error', {}).get('Code') != 'ExecutionAlreadyExists':
            raise
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        observed = sm.describe_execution(executionArn=execution_arn)
        require(observed['stateMachineArn'] == machine and observed['input'] == execution_input and observed.get('redriveCount', 0) == 0, 'native controller execution input/source/redrive differs')
        if observed['status'] != 'RUNNING':
            require(observed['status'] == 'SUCCEEDED', 'private controller lifecycle failed; retain task/runtime liability and native execution identity')
            break
        time.sleep(3)
    else:
        raise ValueError('private controller observer deadline expired; retain execution identity and reserved liability, never launch a replacement')
    outcome_key = custody['outcomePrefix'] + invocation + '.json'
    response = s3.get_object(Bucket=custody['bucket'], Key=outcome_key)
    version = response.get('VersionId')
    outcome_raw = response['Body'].read(65537)
    require(version and version != 'null' and len(outcome_raw) <= 65536, 'private controller outcome custody invalid')
    outcome = json.loads(outcome_raw)
    require(set(outcome) == {'schema', 'status', 'exitCode', 'invocationId', 'invocation', 'requestDigest', 'executionArn', 'taskArn', 'configurationDigest', 'source', 'image', 'startedAt', 'completedAt', 'artifacts'}, 'private outcome contains unapproved fields')
    require(outcome['schema'] == 'honua.private-controller-outcome/v1' and outcome['invocationId'] == invocation and outcome['invocation'] == identity and outcome['requestDigest'] == digest(raw) and outcome['executionArn'] == execution_arn and outcome['configurationDigest'] == request['configurationDigest'] and outcome['source'] == request['source'] and outcome['image'] == config['image'] and type(outcome['exitCode']) is int and 0 <= outcome['exitCode'] <= 255 and outcome['status'] == ('completed' if outcome['exitCode'] == 0 else 'failed'), 'private controller outcome differs from native immutable invocation')
    native_output = json.loads(observed['output'])
    require(outcome['taskArn'] in (native_output.get('TaskArn'), native_output.get('taskArn')), 'controller outcome task differs from native lifecycle output')
    verify_runtime_task(ecs, sdk('ecr', config['region']), config, outcome['taskArn'])
    return outcome | {'outcomeObject': {'bucket': custody['bucket'], 'key': outcome_key, 'versionId': version, 'sha256': digest(outcome_raw)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--configuration', type=Path, required=True)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--operation', choices=['execute', 'finalize', 'reconcile-accounting', *sorted(PREPARATIONS)], required=True)
    parser.add_argument('--case-id')
    parser.add_argument('--target')
    parser.add_argument('--shard', type=int, default=0)
    parser.add_argument('--shards', type=int, default=6)
    parser.add_argument('--timeout', type=int, default=7200)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--evidence-directory', type=Path, required=True)
    args = parser.parse_args()
    require(args.configuration.resolve() == (ROOT / CONFIG_PATH).resolve() and not args.configuration.is_symlink(), 'use the checked-in controller handoff configuration')
    outcome = dispatch_campaign(args.bundle, configuration=json.loads(args.configuration.read_text()), operation=args.operation, case_id=args.case_id, target=args.target, shard=args.shard, shards=args.shards, timeout=args.timeout)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical(outcome) + b'\n')
    restore_artifacts(outcome, configuration=json.loads(args.configuration.read_text()), bundle=args.bundle, evidence_directory=args.evidence_directory)
    return outcome['exitCode']


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (KeyError, ValueError, OSError, json.JSONDecodeError) as error:
        print('private controller refused: ' + str(error), file=sys.stderr)
        sys.exit(1)
