"""One immutable unbound MCP base for a fixed installation; never activation.

Original issued-key/source observations precede the private secret write. Actual
worker resources and immutable native registration must follow before a launch.
"""
from __future__ import annotations

import base64
import copy
import hmac
import json
import os
import re
import time
import sys
from pathlib import Path
import urllib.request
import urllib.error
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from agent_e2e_pack import canonical_digest
from agent_e2e_interactive_budget import validate_retained_funding
from fixture_handoff import encoded, require, sdk
from worker_mcp_configuration import authorize, verify_injected_secret
from native_bootstrap import NoRedirect


STUDIO = {'createMap': 'studio-create-map-runtime', 'createDashboard': 'studio-create-dashboard-runtime',
          'compose': 'studio-compose-runtime', 'proposePublication': 'studio-propose-publication-runtime',
          'proposeRestore': 'studio-restore-runtime'}


def validate(configuration):
    c = configuration
    require(isinstance(c, dict) and set(c) == {'region', 'secretArn', 'endpoint', 'workflowView', 'runtimeId', 'slotId',
            'targetId', 'nativeBootstrapOperationId', 'originalFundingExpiresEpoch', 'mutationCeilings'},
            'installed MCP base requires closed installation configuration')
    secret = re.fullmatch(r'arn:aws:secretsmanager:([a-z0-9-]+):([0-9]{12}):secret:honua-agent-([a-z0-9-]{3,40})-mcp-[A-Za-z0-9]{6}', str(c['secretArn']))
    require(secret is not None and secret[1] == c['region'] and secret[3] == c['slotId'] and
            re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(c['runtimeId'])) and
            re.fullmatch(r'[A-Za-z0-9_-]{1,128}', str(c['targetId'])) and
            re.fullmatch(r'https://[a-z0-9-]+\.pilot\.demo\.honua\.io/mcp', str(c['endpoint'])) and
            isinstance(c['nativeBootstrapOperationId'], str) and 0 < len(c['nativeBootstrapOperationId']) <= 200 and
            type(c['originalFundingExpiresEpoch']) is int and c['originalFundingExpiresEpoch'] > 0 and
            (c['workflowView'] is None or re.fullmatch(r'[A-Za-z0-9._:-]{1,200}', str(c['workflowView']))),
            'installed MCP base differs from exact target, allocation, deadline or secret custody')
    ceilings = c['mutationCeilings']
    require(isinstance(ceilings, dict) and set(ceilings) == set(STUDIO) | {'datasetImportScopes', 'managedProcessIds'} and
            all(type(ceilings[k]) is bool for k in STUDIO) and
            all(isinstance(ceilings[k], list) and all(isinstance(v, str) for v in ceilings[k]) and
                len(set(ceilings[k])) == len(ceilings[k]) for k in ('datasetImportScopes', 'managedProcessIds')),
            'installed MCP mutation ceilings must be explicit and duplicate-free')
    require(all(re.fullmatch(re.escape(c['targetId'])+r'/[a-z_][a-z0-9_]{0,62}/[a-z][a-z0-9_]{0,62}', scope)
                for scope in ceilings['datasetImportScopes']) and
            all(value == 'analytics.buffer-aggregate-managed' for value in ceilings['managedProcessIds']),
            'installed MCP import/process ceilings must preserve exact reviewed native destinations')
    return secret[2]


def guard(durable, operation, configuration, clock):
    authorize(durable, operation)
    validate_retained_funding(durable.state)
    funding = durable.state['ledger'].get('interactiveAccounting')
    require(isinstance(funding, dict) and funding.get('originalExpiresEpoch') == configuration['originalFundingExpiresEpoch'] and
            clock() < funding['originalExpiresEpoch'], 'installed MCP base cannot invent or renew original funding expiry')


def issued_original(durable, configuration, *, ecs):
    c = configuration
    records = durable.state.get('scenarioOperations', {})
    prefix = canonical_digest({'kind': 'native-bootstrap', 'candidateKey': durable.store.candidate,
                               'operationId': c['nativeBootstrapOperationId']})
    completed, issued = records.get(prefix+'/completed'), records.get(prefix+'/key-agent')
    require(all(isinstance(row, dict) and row.get('producer') == 'private-native-bootstrap' and
                row.get('candidateKey') == durable.store.candidate for row in (completed, issued)),
            'installed MCP base requires actual completed native bootstrap and issued-agent originals')
    value, key = completed['value'], issued['value']
    require(value.get('schema') == 'honua.private-native-bootstrap/v1' and value.get('candidateKey') == durable.store.candidate and
            value.get('operation', {}).get('operationId') == c['nativeBootstrapOperationId'] and
            value.get('keys', {}).get('agent') == key and value.get('target') == {'targetId': c['targetId']},
            'installed MCP key does not join its original native bootstrap')
    require(set(key) == {'apiKeyId', 'secretArn', 'versionId', 'permissions', 'effectivePermissions'} and
            re.fullmatch(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', key['apiKeyId']) and
            re.fullmatch(r'[A-Za-z0-9-]{32,64}', key['versionId']) and
            key['effectivePermissions'].get('id') == key['apiKeyId'] and key['effectivePermissions'].get('canAuthenticate') is True,
            'installed MCP issued key has no original active native identity')
    permissions = key['permissions']
    require(isinstance(permissions, list) and all(isinstance(v,str) for v in permissions) and len(set(permissions)) == len(permissions) and
            {'admin:read', 'agent:workspace', 'agent:runtime-task:'+c['runtimeId']} <= set(permissions) and
            [v for v in permissions if v.startswith('agent:runtime-task:')] == ['agent:runtime-task:'+c['runtimeId']] and all(isinstance(v, str) and v != '*' and
                not v.startswith('agent:runtime-control:') and (not v.startswith('admin:') or v == 'admin:read') for v in permissions) and
            sorted(key['effectivePermissions']['permissions']) == sorted(permissions),
            'installed worker cannot carry setup, control or approval credentials')
    for name, operation in STUDIO.items():
        permission = 'agent:'+operation+':'+c['runtimeId']
        require((permission in permissions) == c['mutationCeilings'][name], 'installed Studio permission differs from explicit original ceiling')
    require(sorted(v.removeprefix('agent:dataset-import:') for v in permissions if v.startswith('agent:dataset-import:')) ==
                sorted(c['mutationCeilings']['datasetImportScopes']) and
            sorted(v.removeprefix('agent:managed-process:') for v in permissions if v.startswith('agent:managed-process:')) ==
                sorted(c['mutationCeilings']['managedProcessIds']), 'installed native import/process grants differ from original exact ceilings')
    arn = value['native']['taskDefinitionArn']
    definition = ecs.describe_task_definition(taskDefinition=arn)['taskDefinition']
    containers = definition['containerDefinitions']
    require(definition['taskDefinitionArn'] == arn and len(containers) == 1 and containers[0]['image'] == value['native']['imageUri'],
            'original native bootstrap task definition/image differs')
    values = [row['value'] for row in containers[0].get('environment', []) if row['name'] == 'PILOT_NATIVE_BOOTSTRAP_CONFIG']
    require(len(values) == 1 and len(values[0]) <= 200000, 'original native bootstrap source configuration cannot be observed')
    original = json.loads(values[0])
    require(canonical_digest(original) == value['configurationDigest'] and original['candidateKey'] == durable.store.candidate and
            original['operation'] == value['operation'] and original['endpoint']+'/mcp' == c['endpoint'] and
            original['targetId'] == c['targetId'] and original['secretArns']['agent'] == key['secretArn'] and
            original.get('installedRuntime') == {'runtimeId':c['runtimeId'], 'mutationCeilings':c['mutationCeilings']},
            'installed MCP source does not match actual original bootstrap configuration')
    return key, {'key':prefix+'/key-agent','sha256':canonical_digest(issued)}, {'key':prefix+'/completed','sha256':canonical_digest(completed)}


def native_http(configuration, credential, method, path):
    key_id = configuration['setupKey']['apiKeyId']
    require(method == 'GET' and path in ('/api/v1/agent/target','/api/v1/admin/api-keys/'+key_id+'/effective-permissions'),
        'installed MCP metadata transport is GET-only and exact-key bound')
    request = urllib.request.Request(configuration['endpoint']+path,method='GET',
        headers={'X-API-Key':credential,'Accept':'application/json'})
    try:
        response = urllib.request.build_opener(NoRedirect).open(request,timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        raw = response.read(1024*1024+1)
        require(len(raw) <= 1024*1024 and credential.encode() not in raw, 'installed native metadata leaked a credential or exceeded its bound')
        try: body=json.loads(raw)
        except (UnicodeDecodeError,json.JSONDecodeError): body=None
        return {'httpStatus':response.status,'body':body,'bodyBase64':base64.b64encode(raw).decode(),
            'bodyBytes':len(raw),'bodySha256':'sha256:'+__import__('hashlib').sha256(raw).hexdigest()}


def provision(durable, operation, configuration, *, secrets=None, ecs=None, http=native_http, clock=time.time):
    c = copy.deepcopy(configuration)
    operation = copy.deepcopy(operation)
    account = validate(c)
    check = lambda: guard(durable, operation, c, clock)
    check()
    ecs = ecs or sdk('ecs', c['region'])
    issued, issued_reference, completed_reference = issued_original(durable, c, ecs=ecs)
    require(issued['secretArn'].startswith('arn:aws:secretsmanager:'+c['region']+':'+account+':secret:'),
            'installed source and destination credentials must remain in original account/region')
    verify_injected_secret(issued, 'HONUA_AGENT_REQUESTER_KEY', c['region'], ecs=ecs)
    credential = os.environ.get('HONUA_AGENT_REQUESTER_KEY')
    require(isinstance(credential, str) and len(credential) >= 32, 'original private agent credential missing')
    token = canonical_digest({'kind':'installed-worker-mcp-base/v1','candidateKey':durable.store.candidate,
        'runtimeId':c['runtimeId'],'slotId':c['slotId'],'destination':c['secretArn']})[7:]
    prefix = 'installed-worker-mcp:'+token
    records = durable.state.setdefault('scenarioOperations', {})
    public = {'configuration':c,'operation':operation,'issuedAgentKey':issued_reference,
              'nativeBootstrap':completed_reference,'versionId':token}
    require(credential.encode() not in encoded(public), 'installed public custody cannot contain credential material')
    intent = {'producer':'private-installed-worker-mcp','candidateKey':durable.store.candidate,'value':public}
    require(prefix+'/intent' not in records or records[prefix+'/intent'] == intent,
            'installed allocation already has a different immutable MCP intent')
    count = sum(1 for name in records if name.startswith(prefix+'/observation/') and name.endswith('/target'))
    def observe(stage, path):
        check()
        answer = http({'endpoint':c['endpoint'].removesuffix('/mcp'),'setupKey':issued}, credential, 'GET', path)
        require(answer.get('httpStatus') == 200 and credential.encode() not in encoded(answer), 'installed key native observation failed or disclosed credential')
        key = prefix+'/observation/'+str(count)+'/'+stage
        require(key not in records, 'installed native observation would overwrite original custody')
        records[key] = {'producer':'private-installed-worker-mcp','candidateKey':durable.store.candidate,
                       'value':{'endpoint':c['endpoint'].removesuffix('/mcp'),'path':path,'credentialKind':'api-key',
                                'issuedAgentKey':issued_reference,'response':copy.deepcopy(answer)}}
        check(); durable._persist()
        return answer['body'], {'key':key,'sha256':canonical_digest(records[key])}
    target, target_ref = observe('target','/api/v1/agent/target')
    principal = target.get('principal', {})
    require(target.get('targetId') == c['targetId'] and principal.get('authenticationKind') == 'api-key' and
            principal.get('isHumanSession') is False and principal.get('studioOwnerId') == issued['apiKeyId'] and
            isinstance(principal.get('actorId'), str) and 0 < len(principal['actorId']) <= 256,
            'installed agent target lacks an actual matching machine principal')
    effective, effective_ref = observe('effective','/api/v1/admin/api-keys/'+issued['apiKeyId']+'/effective-permissions')
    value = effective.get('data', {})
    require(effective.get('success') is True and value.get('id') == issued['apiKeyId'] and value.get('status') == 'active' and
            value.get('canAuthenticate') is True and sorted(value.get('permissions', [])) == sorted(issued['permissions']),
            'installed original native key is revoked, inactive or has changed grants')
    payload = {'endpoint':c['endpoint'],'credentialKind':'api-key','credential':credential,'workflowView':c['workflowView'],
               'bindingMode':'native-execution/v1','runtimeId':c['runtimeId'],'slotId':c['slotId']}
    secrets = secrets or sdk('secretsmanager',c['region'])
    check()
    if prefix+'/intent' in records:
        observed = secrets.get_secret_value(SecretId=c['secretArn'],VersionId=token)
        require(observed.get('ARN') == c['secretArn'] and observed.get('VersionId') == token and
                isinstance(observed.get('SecretString'),str) and 'SecretBinary' not in observed and
                hmac.compare_digest(observed['SecretString'].encode(),encoded(payload)),
                'original installed immutable MCP version or bytes differ; no second write allowed')
    else:
        require(prefix+'/completed' not in records, 'installed MCP completion has no original intent')
        records[prefix+'/intent'] = intent; durable._persist(); check()
        observed = secrets.put_secret_value(SecretId=c['secretArn'],ClientRequestToken=token,SecretString=encoded(payload).decode())
        require(observed.get('ARN') == c['secretArn'] and observed.get('VersionId') == token, 'installed MCP immutable write identity differs')
    check()
    result = {'schema':'honua.private-installed-worker-mcp-binding/v1','status':'registration-pending','candidateKey':durable.store.candidate,
        'runtimeId':c['runtimeId'],'slotId':c['slotId'],'targetId':c['targetId'],'secretArn':c['secretArn'],'versionId':token,
        'originalFundingExpiresEpoch':c['originalFundingExpiresEpoch'],'publicBindingDigest':canonical_digest(public),
        'issuedAgentKey':issued_reference,'nativeBootstrap':completed_reference,'principal':principal,
        'targetReference':target_ref,'effectiveReference':effective_ref,'activation':False,'qualification':False}
    # Reconciliation appends fresh observations; the first completion remains
    # immutable rather than being overwritten by a newer last-used timestamp.
    completed = {'producer':'private-installed-worker-mcp','candidateKey':durable.store.candidate,'value':result}
    if prefix+'/completed' not in records:
        records[prefix+'/completed'] = completed; durable._persist()
    else:
        original = records[prefix+'/completed']
        require(original.get('producer') == completed['producer'] and original.get('candidateKey') == durable.store.candidate and
                all(original['value'].get(k) == result[k] for k in result if k not in ('targetReference','effectiveReference')),
                'installed MCP original completion no longer matches immutable allocation')
        result = original['value']
    return {'binding':copy.deepcopy(result),'reference':{'key':prefix+'/completed','sha256':canonical_digest(records[prefix+'/completed'])}}


def execute_preparation(bundle, output):
    from agent_e2e_pack import load_json
    from agent_e2e_state import DurableExecution, open_store
    from controller_handoff import bundle_identity
    from controller_archive import canonical
    from prepare import source_configuration
    from datetime import datetime, timezone
    document=load_json(Path(bundle)/'preparation.json')
    manifest, identity, state=bundle_identity(bundle, operation=document['operation'], case_id=None, target=None, shard=0, shards=6)
    outer=source_configuration(manifest['configuration'])
    require(identity['operation']=='prepare-installed-mcp-base' and outer['action']==identity['operation'] and
        outer['candidateKey']==identity['candidateKey'] and outer['state']==state and
        set(outer['configuration'])=={'operation','installation'}, 'installed MCP preparation differs from exact source/accounting')
    require(datetime.fromisoformat(manifest['expiresAt'].replace('Z','+00:00')) > datetime.now(timezone.utc),'installed MCP preparation expired')
    durable=DurableExecution(open_store(state,outer['candidateKey']),Path(bundle)/'budget-ledger.json')
    try:
        require(durable.state.get('bootstrapImport',{}).get('artifactDigest')==outer['bootstrapArtifactDigest'],
            'installed MCP preparation cannot substitute authenticated original history')
        result=provision(durable,outer['configuration']['operation'],outer['configuration']['installation'])
        binding={'key':result['reference']['key'],'sha256':result['reference']['sha256'],'binding':result['binding']}
        answer={'schema':'honua.private-controller-preparation-result/v1','operation':outer['action'],
            'candidateKey':outer['candidateKey'],'sourceConfiguration':manifest['configuration'],
            'producerTaskArn':os.environ['PILOT_CONTROLLER_TASK_ARN'],'result':binding,
            'requiresSourceReviewBeforeFreeze':True,'qualification':False}
        Path(output).write_bytes(canonical(answer)+b'\n')
    finally:
        durable.close()


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['execute'])
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    try:
        args=parser.parse_args();execute_preparation(args.bundle,args.output)
    except Exception:
        print('private installed MCP preparation refused; retain original evidence and liabilities',file=sys.stderr)
        sys.exit(1)
