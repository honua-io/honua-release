"""Controller-only, fenced handoff to a reviewed private fixture provisioner task."""
from __future__ import annotations
import copy
import hashlib
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_pack import PackError, canonical_digest

COMMAND = ['python', 'tools/hosted_pilot/fixture_provisioner.py', 'transfer-ownership']
CONFIG_FIELDS = {'schemaVersion', 'region', 'accountId', 'clusterArn', 'taskDefinitionArn',
                 'containerName', 'imageUri', 'controllerRoleArn', 'executionRoleArn',
                 'subnetIds', 'securityGroupIds', 'requestBucket', 'requestPrefix',
                 'receiptPrefix', 'claimTable', 'provisionerConfigSha256'}


def require(value, reason):
    if not value:
        raise PackError(reason)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sdk(service, region):
    import boto3
    from botocore.config import Config
    return boto3.client(service, region_name=region,
        config=Config(connect_timeout=5, read_timeout=10, retries={'total_max_attempts': 1}))


def validate_request(request):
    require(isinstance(request, dict) and set(request) == {'schemaVersion', 'candidateKey', 'database',
            'baselineDigest', 'expectedCleanupRole', 'tables'} and request['schemaVersion'] == 1,
            'invalid fixture ownership request shape')
    for key in ('candidateKey', 'baselineDigest'):
        require(isinstance(request[key], str) and re.fullmatch(r'sha256:[a-f0-9]{64}', request[key]), 'fixture ownership digest missing')
    db = request['database']
    require(isinstance(db, dict) and set(db) == {'database', 'databaseOid', 'serverAddress', 'serverPort'} and
            re.fullmatch(r'honua_agent_[a-z0-9_]{1,40}', db['database']) and
            type(db['databaseOid']) is int and db['databaseOid'] > 0 and
            isinstance(db['serverAddress'], str) and db['serverAddress'] and
            type(db['serverPort']) is int and 1 <= db['serverPort'] <= 65535,
            'fixture ownership lacks observed isolated database identity')
    require(re.fullmatch(r'[a-z][a-z0-9_]{0,62}', request['expectedCleanupRole']), 'invalid cleanup SQL role')
    tables = request['tables']
    require(isinstance(tables, list) and len(tables) == 3, 'ownership transfer requires exactly three P10 tables')
    for table in tables:
        require(isinstance(table, dict) and set(table) == {'oid', 'schema', 'name', 'owner'} and
                table['schema'] == 'honua_data' and type(table['oid']) is int and table['oid'] > 0 and
                all(isinstance(table[key], str) and re.fullmatch(r'[a-z][a-z0-9_]{0,62}', table[key]) for key in ('name', 'owner')) and
                table['owner'] != request['expectedCleanupRole'], 'invalid or already-transferred observed fixture table')
    require(len({row['oid'] for row in tables}) == 3 and len({row['name'] for row in tables}) == 3,
            'duplicate fixture table ownership identity')


def validate_configuration(config):
    require(isinstance(config, dict) and set(config) == CONFIG_FIELDS and config['schemaVersion'] == 1,
            'invalid reviewed fixture provisioner configuration')
    require(re.fullmatch(r'\d{12}', config['accountId']) and re.fullmatch(r'[a-z]{2}-[a-z]+-\d', config['region']), 'fixture account/region binding required')
    arn = 'arn:aws:'
    regional = ':' + config['region'] + ':' + config['accountId'] + ':'
    require(config['clusterArn'].startswith(arn + 'ecs' + regional + 'cluster/honua-pilot-') and
            re.fullmatch(re.escape(arn + 'ecs' + regional) + r'task-definition/honua-pilot-[a-z0-9-]+-fixture-provisioner:[1-9][0-9]*', config['taskDefinitionArn']), 'fixture task must be an exact reviewed isolated ECS revision')
    for key in ('controllerRoleArn', 'executionRoleArn'):
        require(re.fullmatch('arn:aws:iam::' + config['accountId'] + r':role/honua-pilot-[a-z0-9-]+', config[key]), 'fixture IAM role must be an exact pilot role')
    require(config['controllerRoleArn'] != config['executionRoleArn'] and config['containerName'] == 'fixture-provisioner', 'fixture task and execution identities must differ')
    require(re.fullmatch(re.escape(config['accountId'] + '.dkr.ecr.' + config['region'] + '.amazonaws.com/') + r'honua-pilot-[a-z0-9-]+-trusted-controller@sha256:[a-f0-9]{64}', config['imageUri']), 'fixture provisioner needs a genuine digest-pinned trusted controller image')
    for key, prefix in [('subnetIds', 'subnet-'), ('securityGroupIds', 'sg-')]:
        require(isinstance(config[key], list) and config[key] and all(re.fullmatch(prefix + '[a-f0-9]+', value) for value in config[key]), 'fixture private network bindings missing')
    require(re.fullmatch(r'honua-pilot-[a-z0-9-]+-controller-[a-z0-9-]+', config['requestBucket']) and
            re.fullmatch(r'honua-pilot-[a-z0-9-]+-controller-fixture-claims', config['claimTable']), 'fixture handoff custody is outside the pilot namespace')
    require(re.fullmatch(r'fixture/requests/[a-f0-9]{64}/', config['requestPrefix']) and
            re.fullmatch(r'fixture/receipts/[a-f0-9]{64}/', config['receiptPrefix']), 'fixture handoff paths must bind the exact candidate')
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', config['provisionerConfigSha256']), 'fixture base configuration digest required')


def verify_task(definition, config):
    task = definition['taskDefinition']
    require(task['taskDefinitionArn'] == config['taskDefinitionArn'] and
            task['taskRoleArn'] == config['controllerRoleArn'] and task['executionRoleArn'] == config['executionRoleArn'] and
            task['networkMode'] == 'awsvpc' and task.get('runtimePlatform') == {'cpuArchitecture': 'X86_64', 'operatingSystemFamily': 'LINUX'},
            'fixture provisioner task identity or platform changed')
    require(len(task['containerDefinitions']) == 1, 'fixture provisioner cannot run sidecars')
    container = task['containerDefinitions'][0]
    require(container['name'] == config['containerName'] and container['image'] == config['imageUri'] and
            container.get('command') == COMMAND and container.get('user') == '10001:10001' and
            container.get('readonlyRootFilesystem') is True and not container.get('privileged', False) and
            not container.get('portMappings') and not container.get('entryPoint'), 'fixture provisioner command, image or isolation changed')
    environment = {row['name']: row['value'] for row in container.get('environment', [])}
    require(set(environment) == {'PILOT_PROVISIONER_CONFIG'} and
            canonical_digest(json.loads(environment['PILOT_PROVISIONER_CONFIG'])) == config['provisionerConfigSha256'],
            'fixture provisioner base configuration changed or contains ambient credentials')
    return container


def transfer_ownership(request, *, durable, context, configuration, timeout=300):
    """One native task launch; an ambiguous result never authorizes a retry."""
    validate_request(request)
    config = copy.deepcopy(configuration)
    validate_configuration(config)
    require(type(timeout) is int and 30 <= timeout <= 300, 'fixture provisioning deadline must be bounded')
    require(request['candidateKey'] == durable.store.candidate and
            config['requestPrefix'].split('/')[2] == request['candidateKey'][7:] and
            config['receiptPrefix'].split('/')[2] == request['candidateKey'][7:], 'fixture handoff belongs to another candidate')
    # The retained baseline/request and reserved liabilities remain authoritative;
    # a request object alone grants no SQL privilege to the task.
    retained = durable.retain_fixture_transfer_request(context, request)
    require(retained['sha256'] == canonical_digest(request), 'fixture request was not durably retained')
    durable._require_live_lease()
    head = durable.store.db.get_item(TableName=durable.store.table, Key=durable.store.key, ConsistentRead=True)['Item']
    require(head['leaseOwner']['S'] == durable.store.owner and int(head['fence']['N']) == durable.store.fence,
            'fixture handoff lost its native controller fence')
    envelope = {'schema': 'honua.fixture-ownership-handoff/v1', 'request': request,
                'context': {key: context[key] for key in ('campaignId', 'caseId', 'attempt', 'campaignMode')},
                'retained': retained, 'fence': {'owner': durable.store.owner, 'fence': durable.store.fence,
                    'candidateKey': durable.store.candidate, 'stateTable': durable.store.table,
                    'stateBucket': durable.store.bucket, 'stateKey': durable.store.key},
                'provisionerConfigSha256': config['provisionerConfigSha256']}
    body = encoded(envelope)
    require(len(body) <= 65536, 'fixture ownership handoff exceeds bounded size')
    request_digest = 'sha256:' + hashlib.sha256(body).hexdigest()
    claim_key = canonical_digest({'candidateKey': request['candidateKey'], 'context': envelope['context'], 'kind': 'fixture-ownership'})
    s3, ecs = sdk('s3', config['region']), sdk('ecs', config['region'])
    verify_task(ecs.describe_task_definition(taskDefinition=config['taskDefinitionArn']), config)
    require(s3.get_bucket_versioning(Bucket=config['requestBucket']).get('Status') == 'Enabled', 'fixture handoff bucket must retain immutable versions')
    key = config['requestPrefix'] + claim_key[7:] + '.json'
    stored = s3.put_object(Bucket=config['requestBucket'], Key=key, Body=body,
                           IfNoneMatch='*', ServerSideEncryption='AES256', ContentType='application/json')
    require(stored.get('VersionId') not in (None, '', 'null'), 'fixture handoff lacks an immutable object version')
    pointer = {'bucket': config['requestBucket'], 'key': key, 'versionId': stored['VersionId'],
               'sha256': request_digest, 'claimKey': claim_key, 'timeoutSeconds': timeout}
    durable._require_live_lease()
    launched = ecs.run_task(cluster=config['clusterArn'], taskDefinition=config['taskDefinitionArn'],
        launchType='FARGATE', count=1, clientToken=claim_key[7:], enableExecuteCommand=False,
        networkConfiguration={'awsvpcConfiguration': {'subnets': config['subnetIds'], 'securityGroups': config['securityGroupIds'], 'assignPublicIp': 'DISABLED'}},
        overrides={'containerOverrides': [{'name': config['containerName'], 'environment': [{'name': 'PILOT_FIXTURE_REQUEST', 'value': encoded(pointer).decode()}]}]},
        tags=[{'key': 'honua-agent-candidate', 'value': request['candidateKey']}, {'key': 'honua-fixture-claim', 'value': claim_key}])
    require(not launched.get('failures') and len(launched.get('tasks', [])) == 1, 'fixture task launch unavailable; retain provisioning liability')
    task_arn = launched['tasks'][0]['taskArn']
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        durable._require_live_lease()
        observed = ecs.describe_tasks(cluster=config['clusterArn'], tasks=[task_arn])
        require(not observed.get('failures') and len(observed.get('tasks', [])) == 1, 'fixture task cannot be observed; retain liability')
        task = observed['tasks'][0]
        if task['lastStatus'] == 'STOPPED':
            require(task['taskDefinitionArn'] == config['taskDefinitionArn'] and
                    len(task['containers']) == 1 and task['containers'][0].get('exitCode') == 0,
                    'fixture task did not complete successfully; do not repeat SQL')
            break
        time.sleep(2)
    else:
        raise PackError('fixture provisioner deadline expired; observe task/claim before any recovery')
    receipt_key = config['receiptPrefix'] + claim_key[7:] + '.json'
    response = s3.get_object(Bucket=config['requestBucket'], Key=receipt_key)
    require(response.get('VersionId') not in (None, '', 'null'), 'fixture task receipt lacks immutable version')
    raw = response['Body'].read(65537)
    require(len(raw) <= 65536, 'fixture task receipt exceeds bounded size')
    receipt = json.loads(raw)
    require(receipt['schema'] == 'honua.fixture-ownership-receipt/v1' and receipt['status'] == 'completed' and receipt['requestDigest'] == request_digest and
            receipt['ownershipRequestDigest'] == canonical_digest(request) and
            receipt['claimKey'] == claim_key and receipt['taskArn'] == task_arn and
            receipt['taskDefinitionArn'] == config['taskDefinitionArn'] and receipt['imageUri'] == config['imageUri'] and
            receipt['provisionerConfigSha256'] == config['provisionerConfigSha256'] and
            receipt['candidateKey'] == request['candidateKey'] and receipt['context'] == envelope['context'] and
            receipt['database'] == request['database'] and receipt['before'] == request['tables'] and
            receipt['after'] == [row | {'owner': request['expectedCleanupRole']} for row in request['tables']],
            'fixture task receipt does not bind exact source/task/database/OID ownership effects')
    durable._require_live_lease()
    return receipt | {'receiptObject': {'bucket': config['requestBucket'], 'key': receipt_key,
            'versionId': response['VersionId'], 'sha256': 'sha256:' + hashlib.sha256(raw).hexdigest()}}
