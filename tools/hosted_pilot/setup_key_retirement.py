#!/usr/bin/env python3
"""Retire only the original saved-fixture setup key in separate private custody."""
from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_pack import canonical_digest
from agent_e2e_state import DurableExecution, open_store
from controller_archive import BUNDLE_LIMIT, canonical, digest, extract, require
from controller_budget import read_version
from controller_entry import file_manifest
from fixture_handoff import sdk
from native_bootstrap import NoRedirect, authorize


def validate(config, *, allow_expired=False):
    fields = {'schemaVersion', 'candidateKey', 'generationDigest', 'bootstrapArtifactDigest', 'state', 'operation',
        'region', 'accountId', 'endpoint', 'nativeBootstrapOperationId', 'setupKey', 'fixtureReference',
        'preparationOutcome', 'bucket', 'receiptKey', 'clusterArn', 'taskDefinitionFamily', 'imageUri', 'rootSecretArn', 'taskRoleArn', 'executionRoleArn', 'mode', 'originalClaim', 'claimTable', 'expiresAt', 'retirementProtocol'}
    require(set(config) == fields and config['schemaVersion'] == 1 and config['accountId'] == '585192672263' and
            config['region'] == 'us-east-1' and config['state']['region'] == config['region'] and
            all(re.fullmatch(r'sha256:[a-f0-9]{64}', config[key]) for key in ('candidateKey', 'generationDigest', 'bootstrapArtifactDigest')),
            'setup retirement must bind original pilot accounting and private target')
    require(re.fullmatch(r'https://[a-z0-9-]+\.pilot\.demo\.honua\.io', config['endpoint']) and
            set(config['setupKey']) == {'apiKeyId', 'secretArn', 'versionId'} and
            re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', config['setupKey']['apiKeyId']) and
            re.fullmatch(r'[a-f0-9]{64}', config['setupKey']['versionId']) and
            re.fullmatch(r'arn:aws:secretsmanager:us-east-1:585192672263:secret:honua-pilot-[a-z0-9-]+-agent/native-agent/setup-[A-Za-z0-9]{6}', config['setupKey']['secretArn']),
            'setup retirement cannot select a new credential or another identity')
    require(isinstance(config['operation'], dict) and set(config['operation']) == {'operationId', 'liabilityBasisDigest', 'reservationIds'} and
            set(config['operation']['reservationIds']) == {'infrastructure', 'cleanup'} and
            len(set(config['operation']['reservationIds'].values())) == 2,
            'retirement requires both distinct original bootstrap infrastructure and cleanup reservations')
    require(set(config['fixtureReference']) == {'key', 'sha256'} and
            all(re.fullmatch(r'sha256:[a-f0-9]{64}', value) for value in config['fixtureReference'].values()),
            'actual retained saved fixture reference required')
    require(re.fullmatch(r'arn:aws:secretsmanager:us-east-1:585192672263:secret:honua-pilot-[a-z0-9-]+-[A-Za-z0-9]{6}', config['rootSecretArn']) and
            re.fullmatch(r'arn:aws:iam::585192672263:role/honua-pilot-[a-z0-9-]+-setup-retire', config['taskRoleArn']) and
            re.fullmatch(r'arn:aws:iam::585192672263:role/honua-pilot-[a-z0-9-]+-retire-execution', config['executionRoleArn']) and
            re.fullmatch(r'585192672263\.dkr\.ecr\.us-east-1\.amazonaws\.com/honua-pilot-[a-z0-9-]+-trusted-controller@sha256:[a-f0-9]{64}', config['imageUri']),
            'retirement must retain separate private issuer/task/execution custody')
    expiry = datetime.fromisoformat(config['expiresAt'].replace('Z', '+00:00'))
    require(expiry.tzinfo is not None and (allow_expired or datetime.now(timezone.utc) < expiry) and config['retirementProtocol'] == 2,
            'retirement requires its original immutable unexpired request protocol/deadline')
    require(config['mode'] in ('retire', 'observe', 'resume') and re.fullmatch(r'honua-pilot-[a-z0-9-]+-controller-dispatch-claims', config['claimTable']),
            'retirement needs a fixed effect/observation mode and original dispatch claims')
    original = config['originalClaim']
    if config['mode'] != 'retire':
        require(isinstance(original, dict) and set(original) == {'table', 'key', 'bindingDigest', 'taskArn', 'taskDefinitionArn'} and
                original['table'] == config['claimTable'] and re.fullmatch(r'dispatch:[a-f0-9]{64}', original['key']) and
                re.fullmatch(r'sha256:[a-f0-9]{64}', original['bindingDigest']) and
                original['taskArn'].startswith('arn:aws:ecs:us-east-1:585192672263:task/'+config['clusterArn'].rsplit('/', 1)[-1]+'/') and
                re.fullmatch(re.escape(config['taskDefinitionFamily'])+r':[1-9][0-9]*', original['taskDefinitionArn']),
                'observation-only recovery needs exact original immutable dispatch/task identity')
    else:
        require(original is None, 'a new revoke cannot select another original claim')
    candidate = config['candidateKey'][7:]
    receipt_suffix = 'retire' if original is None else config['mode']+'-'+original['key'].split(':', 1)[1]
    require(config['preparationOutcome']['bucket'] == config['bucket'] and
            config['preparationOutcome']['key'].startswith('controller/outcomes/'+candidate+'/') and
            config['receiptKey'] == 'native/setup-key-retirement/'+candidate+'/'+config['setupKey']['apiKeyId']+'/'+receipt_suffix+'.json' and
            config['clusterArn'].startswith('arn:aws:ecs:us-east-1:585192672263:cluster/honua-pilot-'),
            'setup retirement custody differs from the original candidate')


def retained_authority(config, durable):
    authorize(config, durable)
    operation = canonical_digest({'kind': 'native-bootstrap', 'candidateKey': config['candidateKey'],
                                  'operationId': config['nativeBootstrapOperationId']})
    records = durable.state.get('scenarioOperations', {})
    def key(kind):
        value = records.get(operation+'/key-'+kind)
        require(isinstance(value, dict) and value.get('producer') == 'private-native-bootstrap' and
                value.get('candidateKey') == config['candidateKey'], 'original native key issuance is missing')
        return value['value']
    issued, agent = key('setup'), key('agent')
    require({name: issued[name] for name in config['setupKey']} == config['setupKey'] and
            'admin:write' in issued['permissions'] and 'agent:workspace' not in issued['permissions'] and
            issued['apiKeyId'] != agent['apiKeyId'], 'retirement key differs from original separate setup authority')
    reference = config['fixtureReference']
    fixture = durable.state.get('studioBootstrapFixtures', {}).get(reference['key'])
    require(isinstance(fixture, dict) and canonical_digest(fixture) == reference['sha256'] and
            fixture.get('schema') == 'honua.agent-e2e.studio-bootstrap/v1' and fixture.get('candidateKey') == config['candidateKey'] and
            fixture['fixture']['ownerPrincipalId'] == agent['apiKeyId'] and
            'agent:studio-compose:'+fixture['fixture']['itemId'] in agent['permissions'],
            'retirement must follow the actual native saved fixture owned by the issued agent')
    require(fixture['nativeOriginals'] and all(records.get(name) == value for name, value in fixture['nativeOriginals'].items()),
            'original fixture native observations were removed or changed')
    return issued, fixture


def read_object(s3, reference, *, limit):
    require(set(reference) == {'bucket', 'key', 'versionId', 'sha256'}, 'immutable original object reference required')
    raw = read_version(s3, reference['bucket'], reference['key'], reference['versionId'], limit=limit)
    require(digest(raw) == reference['sha256'], 'original private preparation object changed')
    return raw


def observe_preparation(config, *, s3, ecs, directory, states=None):
    """Bind fixture state to original producer output and independently stopped ECS."""
    outcome = json.loads(read_object(s3, config['preparationOutcome'], limit=4*1024*1024))
    invocation = outcome['invocation']
    require(outcome['schema'] == 'honua.private-controller-outcome/v1' and outcome['status'] == 'completed' and
            outcome['exitCode'] == 0 and invocation['operation'] == 'prepare-studio-fixture' and
            invocation['candidateKey'] == config['candidateKey'], 'setup retirement needs successful original fixture preparation')
    task = ecs.describe_tasks(cluster=config['clusterArn'], tasks=[outcome['taskArn']])
    if not task.get('failures') and len(task.get('tasks', [])) == 1:
        task = task['tasks'][0]
        require(task['taskArn'] == outcome['taskArn'] and task['lastStatus'] == 'STOPPED' and len(task['containers']) == 1 and
                task['containers'][0].get('exitCode') == 0 and not task.get('enableExecuteCommand', True) and
                task['containers'][0]['image'] == outcome['image']['uri'], 'original fixture bootstrap task is not positively stopped')
        stopped = {'taskArn': task['taskArn'], 'taskDefinitionArn': task['taskDefinitionArn'], 'lastStatus': 'STOPPED', 'exitCode': 0,
                   'observation': 'ecs-describe-tasks'}
    else:
        # ECS may expire stopped-task metadata before source review completes.
        # Native Step Functions retains its actual runTask.sync terminal output;
        # missing ECS metadata itself never becomes proof of termination.
        require(states is not None and re.fullmatch(r'arn:aws:states:us-east-1:585192672263:execution:honua-pilot-[a-z0-9-]+-campaign-controller:[a-f0-9]{64}',
                outcome['executionArn']), 'original fixture termination cannot be independently observed')
        execution = states.describe_execution(executionArn=outcome['executionArn'])
        output = json.loads(execution.get('output', '{}'))
        require(execution.get('executionArn') == outcome['executionArn'] and execution['status'] == 'SUCCEEDED' and
                output.get('TaskArn') == outcome['taskArn'] and output.get('LastStatus') == 'STOPPED' and
                len(output.get('Containers', [])) == 1 and output['Containers'][0].get('ExitCode') == 0 and
                output['Containers'][0]['Image'] == outcome['image']['uri'], 'native fixture lifecycle does not prove stopped successful bootstrap task')
        stopped = {'taskArn': output['TaskArn'], 'taskDefinitionArn': output['TaskDefinitionArn'], 'lastStatus': 'STOPPED', 'exitCode': 0,
                   'observation': 'native-runTask-sync', 'executionArn': outcome['executionArn']}
    references = outcome['artifacts']['evidence']
    prefix = 'controller/evidence/'+config['candidateKey'][7:]+'/'+outcome['invocationId']+'/'
    require(set(references) == {'manifest', 'archive'} and all(value['bucket'] == config['bucket'] and
            value['key'].startswith(prefix) for value in references.values()), 'fixture preparation artifact escaped original custody')
    manifest = json.loads(read_object(s3, references['manifest'], limit=4*1024*1024))
    require(manifest['schema'] == 'honua.private-controller-artifacts/v1' and manifest['kind'] == 'private-evidence' and
            manifest['invocationId'] == outcome['invocationId'] and manifest['archive'] == references['archive'] and
            'preparation.json' in manifest['immutableInputs'], 'original fixture artifact does not bind its source invocation')
    destination = Path(directory)/'original-preparation'
    extract(read_object(s3, references['archive'], limit=BUNDLE_LIMIT), destination, limit=BUNDLE_LIMIT)
    require(file_manifest(destination) == manifest['files'], 'original fixture artifact file inventory changed')
    result = json.loads((destination/'preparation-result.json').read_bytes())
    require(result['schema'] == 'honua.private-controller-preparation-result/v1' and result['operation'] == 'prepare-studio-fixture' and
            result['producerTaskArn'] == outcome['taskArn'] and result['candidateKey'] == config['candidateKey'] and
            result['qualification'] is False and
            {name: result['result'][name] for name in ('key', 'sha256')} == config['fixtureReference'],
            'retirement fixture differs from actual private producer output')
    return stopped | {'outcomeObject': config['preparationOutcome']}


def native_http(config, credential, method, path):
    root = '/api/v1/admin/api-keys/'+config['setupKey']['apiKeyId']
    require((method, path) in (('GET', root+'/effective-permissions'), ('POST', root+'/revoke')) and
            (method == 'GET' or config['mode'] in ('retire', 'resume')),
            'retirement HTTP may only observe or revoke the original setup key')
    request = urllib.request.Request(config['endpoint']+path, method=method,
        headers={'X-API-Key': credential, 'Accept': 'application/json'}, data=b'' if method == 'POST' else None)
    try:
        response = urllib.request.build_opener(NoRedirect).open(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read(1024*1024+1)
        require(len(raw) <= 1024*1024 and credential.encode() not in raw, 'retirement native response disclosed credentials or exceeded bound')
        try:
            value = json.loads(raw) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            value = None
        return {'httpStatus': response.status, 'body': value, 'bodyBytes': len(raw), 'bodySha256': digest(raw),
                'bodyFormat': 'json' if value is not None else 'empty' if not raw else 'non-json'}


def retention_prefix(config, native):
    base = canonical_digest({'kind': 'setup-key-retirement', 'candidateKey': config['candidateKey'],
        'operationId': config['operation']['operationId'], 'setupKeyId': config['setupKey']['apiKeyId'], 'fixture': config['fixtureReference']})
    return base if config['mode'] == 'retire' else base+'/recoveries/'+config['mode']+'/'+canonical_digest({'taskArn': native['taskArn']})[7:]


def retire(config, durable, *, root_credential, setup_credential, preparation, native, http=native_http, original=None):
    validate(config)
    issued, fixture = retained_authority(config, durable)
    require(len(root_credential) >= 32 and len(setup_credential) >= 32 and root_credential != setup_credential and
            preparation['taskArn'] != native['taskArn'] and preparation['lastStatus'] == 'STOPPED',
            'retirement requires distinct original private issuer/setup credentials and a fresh private task')
    records = durable.state.setdefault('scenarioOperations', {})
    prefix = retention_prefix(config, native)
    require(prefix+'/start' not in records, 'setup retirement already started; reconcile original task without replay')
    if config['mode'] != 'retire':
        require(original is not None and original['taskArn'] == config['originalClaim']['taskArn'] and original['lastStatus'] == 'STOPPED' and
                original.get('claim') == config['originalClaim'], 'retirement recovery needs exact independently stopped original task/claim')
        if config['mode'] == 'observe':
            initial = original['originalPrefix']
            before = records.get(initial+'/active-setup-read', {}).get('value', {})
            active = (before.get('body') or {}).get('data', {})
            require(before.get('httpStatus') == 200 and active.get('id') == issued['apiKeyId'] and active.get('status') == 'active' and
                    active.get('canAuthenticate') is True and sorted(active.get('permissions', [])) == sorted(issued['permissions']) and
                    initial+'/revoke-intent' in records, 'observation-only recovery lacks original positive control or revoke intent')
    def guard():
        authorize(config, durable)
        require(datetime.now(timezone.utc) < datetime.fromisoformat(config['expiresAt'].replace('Z', '+00:00')),
                'original retirement expiry exhausted; retain original observations and liabilities')
    def retain(stage, value):
        guard()
        require(prefix+'/'+stage not in records, 'setup retirement observation already retained')
        require(root_credential.encode() not in canonical(value) and setup_credential.encode() not in canonical(value), 'setup retirement cannot retain credential material')
        records[prefix+'/'+stage] = {'producer': 'private-setup-key-retirement', 'candidateKey': config['candidateKey'], 'value': value}
        durable._persist()
    def effective(answer, status, authenticates):
        value = (answer.get('body') or {}).get('data', {})
        return answer['httpStatus'] == 200 and value.get('id') == issued['apiKeyId'] and value.get('status') == status and \
            value.get('canAuthenticate') is authenticates and sorted(value.get('permissions', [])) == sorted(issued['permissions'])
    path = '/api/v1/admin/api-keys/'+issued['apiKeyId']
    retain('start', {'configurationDigest': canonical_digest(config), 'fixtureReference': config['fixtureReference'],
        'setupKey': config['setupKey'], 'preparation': preparation, 'native': native})
    if original is not None:
        retain('original-retirement', original)
    before_status = None
    if config['mode'] in ('retire', 'resume'):
        guard()
        before = http(config, setup_credential, 'GET', path+'/effective-permissions')
        before_status = before['httpStatus']
        if effective(before, 'active', True):
            retain('active-setup-read', before)
            if config['mode'] == 'resume':
                guard()
                independent = http(config, root_credential, 'GET', path+'/effective-permissions')
                require(effective(independent, 'active', True), 'resumed revoke lacks fresh independent exact-key active control')
                retain('active-independent-read', independent)
            retain('revoke-intent', {'method': 'POST', 'path': path+'/revoke', 'setupKeyId': issued['apiKeyId'],
                'credentialKind': 'independent-bootstrap-admin'})
            # Durable admission separates known pre-send failures from ambiguous
            # transmission. An admitted request is never described as unsent.
            # Exact-ID native revoke uses COALESCE(revoked_at,@now): recovery
            # preserves original RevokedAt while another audit/UpdatedAt may
            # occur. Keep that uncertainty and its original cost liabilities.
            retain('revoke-dispatch-admission', {'taskArn': native['taskArn'], 'setupKeyId': issued['apiKeyId'],
                'originalClaim': config['originalClaim'], 'priorSendState': original['sendState'] if original else 'not-issued',
                'possibleAdditionalAudit': config['mode'] == 'resume' and original['sendState'] == 'possibly-issued'})
            guard()
            try:
                revoked = http(config, root_credential, 'POST', path+'/revoke')
            except (TimeoutError, urllib.error.URLError):
                retain('revoke-acknowledgement', {'status': 'unknown', 'requestMayHaveBeenSent': True})
            else:
                value = (revoked.get('body') or {}).get('data', {})
                require(revoked['httpStatus'] == 200 and value.get('id') == issued['apiKeyId'] and value.get('status') == 'revoked',
                        'native setup key revocation did not confirm original identity')
                retain('revoke-response', revoked)
        else:
            require(config['mode'] == 'resume' and before['httpStatus'] == 401,
                    'original setup credential lacks its actual authenticated positive control')
            retain('already-denied-read', before)
    guard()
    after = http(config, root_credential, 'GET', path+'/effective-permissions')
    require(effective(after, 'revoked', False), 'independent native read did not prove original setup authority revoked')
    retain('revoked-independent-read', after)
    guard()
    denied = http(config, setup_credential, 'GET', path+'/effective-permissions')
    require(denied['httpStatus'] == 401, 'old setup credential still authenticates or only lacks scope; 403 is not revocation proof')
    retain('old-setup-denied', denied)
    receipt = {'schema': 'honua.private-setup-key-retirement/v1', 'candidateKey': config['candidateKey'],
        'configurationDigest': canonical_digest(config), 'operation': config['operation'], 'setupKey': config['setupKey'],
        'fixtureReference': config['fixtureReference'], 'preparation': preparation, 'native': native,
        'revokedKeyId': issued['apiKeyId'], 'beforeStatus': before_status, 'postRevocationProbeStatus': 401,
        'retainedPrefix': prefix, 'mode': config['mode'], 'originalRetirement': original,
        'possibleAdditionalAudit': bool(original and original['sendState'] == 'possibly-issued' and before_status == 200),
        'qualification': False}
    retain('completed', receipt)
    return receipt


def observe_original_retirement(config, durable, *, ecs, db, seen=()):
    """Read the exact old dispatch/terminal record; never start or revoke again."""
    authorize(config, durable)
    original = config['originalClaim']
    require(config['mode'] in ('observe', 'resume') and original is not None and len(seen) < 8 and original['key'] not in seen,
            'bounded exact original retirement recovery chain required')
    row = db.get_item(TableName=original['table'], Key={'claimKey': {'S': original['key']}}, ConsistentRead=True).get('Item', {})
    require(row.get('bindingDigest', {}).get('S') == original['bindingDigest'] and row.get('taskArn', {}).get('S') == original['taskArn'],
            'original retirement dispatch does not match its actual retained claim')
    terminal = json.loads(row.get('terminalObservation', {}).get('S', '{}'))
    require(digest(canonical(terminal)) == row.get('terminalDigest', {}).get('S') and terminal.get('taskArn') == original['taskArn'] and
            terminal.get('taskDefinitionArn') == original['taskDefinitionArn'] and terminal.get('lastStatus') == 'STOPPED' and
            type(terminal.get('exitCode')) is int and 0 <= terminal['exitCode'] <= 255,
            'original retirement has no retained independently observed terminal task')
    definition = ecs.describe_task_definition(taskDefinition=original['taskDefinitionArn'])['taskDefinition']
    require(definition['taskDefinitionArn'] == original['taskDefinitionArn'] and len(definition['containerDefinitions']) == 1,
            'original retirement task definition is missing or changed')
    environment = definition['containerDefinitions'][0].get('environment', [])
    require(len(environment) == 1 and environment[0]['name'] == 'PILOT_SETUP_RETIREMENT_CONFIG', 'original retirement task configuration is absent')
    initial = json.loads(environment[0]['value'])
    validate(initial, allow_expired=True)
    from setup_key_retirement_dispatch import verify_definition
    verify_definition(definition, initial)
    same = ('candidateKey', 'generationDigest', 'bootstrapArtifactDigest', 'state', 'operation', 'region', 'accountId',
        'endpoint', 'nativeBootstrapOperationId', 'setupKey', 'fixtureReference', 'preparationOutcome', 'bucket', 'rootSecretArn', 'claimTable',
        'clusterArn', 'taskDefinitionFamily', 'taskRoleArn', 'executionRoleArn', 'expiresAt', 'retirementProtocol')
    require(all(initial[name] == config[name] for name in same) and
            terminal['imageUri'] == initial['imageUri'], 'observation cannot substitute original setup, fixture, target or liabilities')
    prefix = retention_prefix(initial, {'taskArn': original['taskArn']})
    start = durable.state.get('scenarioOperations', {}).get(prefix+'/start', {})
    require((not start and config['mode'] == 'resume') or (start.get('producer') == 'private-setup-key-retirement' and start.get('candidateKey') == config['candidateKey'] and
            start.get('value', {}).get('configurationDigest') == canonical_digest(initial) and
            start['value']['native']['taskArn'] == original['taskArn'] and
            start['value']['native']['taskDefinitionArn'] == original['taskDefinitionArn']),
            'original retirement task/config differs from actual before-effect custody')
    observed = ecs.describe_tasks(cluster=config['clusterArn'], tasks=[original['taskArn']])
    if observed.get('tasks'):
        require(not observed.get('failures') and len(observed['tasks']) == 1, 'original retirement native task observation differs')
        task = observed['tasks'][0]
        require(task['taskArn'] == original['taskArn'] and task['taskDefinitionArn'] == original['taskDefinitionArn'] and
                task['lastStatus'] == 'STOPPED' and len(task['containers']) == 1 and
                task['containers'][0].get('exitCode') == terminal['exitCode'] and task['containers'][0]['image'] == terminal['imageUri'] and
                not task.get('enableExecuteCommand', True),
                'original retirement is not independently stopped')
    else:
        # The original checked dispatcher already persisted its actual STOPPED
        # response before inspecting application exit or receipt publication.
        require(observed.get('failures') and all(error.get('reason') == 'MISSING' for error in observed['failures']),
                'original retirement metadata failure cannot be reinterpreted as termination')
    ancestor = observe_original_retirement(initial, durable, ecs=ecs, db=db, seen=(*seen, original['key'])) if initial['mode'] != 'retire' else None
    admitted = prefix+'/revoke-dispatch-admission' in durable.state.get('scenarioOperations', {})
    return terminal | {'claim': original, 'originalConfigurationDigest': canonical_digest(initial), 'originalPrefix': prefix,
        'sendState': 'possibly-issued' if admitted or ancestor and ancestor['sendState'] == 'possibly-issued' else 'not-issued',
        'originalStartRetained': bool(start), 'ancestor': ancestor}


def run():
    require(os.getuid() == 10001 and sys.argv[1:] == ['run'], 'setup retirement requires its fixed private nonroot entry')
    config = json.loads(os.environ['PILOT_SETUP_RETIREMENT_CONFIG'])
    validate(config)
    require(sdk('sts', config['region']).get_caller_identity()['Account'] == config['accountId'], 'retirement AWS account differs')
    from controller_entry import http_json
    uri = os.environ['ECS_CONTAINER_METADATA_URI_V4']
    require(re.fullmatch(r'http://169\.254\.170\.2/v4/[A-Za-z0-9_-]+', uri), 'retirement lacks authentic native task metadata')
    metadata = http_json(uri+'/task', limit=65536)
    ecs = sdk('ecs', config['region'])
    observed = ecs.describe_tasks(cluster=config['clusterArn'], tasks=[metadata['TaskARN']])
    require(not observed.get('failures') and len(observed.get('tasks', [])) == 1, 'retirement task cannot be independently observed')
    task = observed['tasks'][0]
    require(task['taskArn'] == metadata['TaskARN'] and task['taskDefinitionArn'].rsplit(':', 1)[0] == config['taskDefinitionFamily'] and
            len(task['containers']) == 1 and task['containers'][0]['name'] == 'setup-key-retirement' and
            task['containers'][0]['image'] == config['imageUri'] and not task.get('enableExecuteCommand', True), 'retirement native task/image differs')
    definition = ecs.describe_task_definition(taskDefinition=task['taskDefinitionArn'])['taskDefinition']
    from setup_key_retirement_dispatch import verify_definition
    verify_definition(definition, config)
    native = {'taskArn': task['taskArn'], 'taskDefinitionArn': task['taskDefinitionArn'], 'imageUri': config['imageUri'],
              'imageSource': json.loads(Path('/opt/honua-controller-source.json').read_text())}
    s3 = sdk('s3', config['region'])
    preparation = observe_preparation(config, s3=s3, ecs=ecs, states=sdk('stepfunctions', config['region']), directory='/work')
    durable = DurableExecution(open_store(config['state'], config['candidateKey']), '/work/setup-retirement-ledger.json')
    try:
        original = observe_original_retirement(config, durable, ecs=ecs, db=sdk('dynamodb', config['region'])) if config['mode'] != 'retire' else None
        receipt = retire(config, durable, root_credential=os.environ['PILOT_SETUP_RETIRE_ROOT_KEY'],
                         setup_credential=os.environ['PILOT_SETUP_RETIRE_KEY'], preparation=preparation, native=native, original=original)
        s3.put_object(Bucket=config['bucket'], Key=config['receiptKey'], Body=canonical(receipt),
            IfNoneMatch='*', ServerSideEncryption='AES256', ContentType='application/json')
    finally:
        durable.close()
    print('Original setup authority revoked and denied by native API. Observe this private task stopped before fresh model work.')


if __name__ == '__main__':
    try:
        run()
    except Exception:
        print('private setup retirement refused; retain original task, observations and liabilities for reconciliation', file=sys.stderr)
        sys.exit(1)
