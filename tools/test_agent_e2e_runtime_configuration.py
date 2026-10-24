"""Checked declaration/custody controls; fixtures do not prove installation."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_e2e_pack import PackError, canonical_digest, digest_bytes, load_json
from agent_e2e_runtime_configuration import load_declaration
from test_agent_e2e_console_runtime import Retained
from test_agent_e2e_interactive_budget import interactive
from test_agent_e2e_budget import KEY, BASIS, reserve_operation

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def declaration(interactive, tmp_path):
    path, binding, now, imported, original_config = interactive
    for kind in ('infrastructure', 'cleanup'):
        reserve_operation(path, rid='runtime-'+kind, amount='1', kind=kind, purpose='shared-environment', operation_id='runtime')
    durable = Retained({'ledger': load_json(path), 'bootstrapImport': imported,
        'interactiveFunding': {'binding': binding, 'configuration': json.dumps(original_config)}, 'scenarioOperations': {}}, path)
    durable.store.bucket, durable.store.table = 'retained', 'state'
    records = durable.state['scenarioOperations']
    native_operation = {'operationId': 'bootstrap-original', 'liabilityBasisDigest': BASIS,
        'reservationIds': {'infrastructure': 'bootstrap-infra', 'cleanup': 'original-cleanup'}}
    prefix = canonical_digest({'kind': 'native-bootstrap', 'candidateKey': KEY, 'operationId': native_operation['operationId']})
    def record(stage, value):
        key = prefix+'/'+stage
        records[key] = {'producer': 'private-native-bootstrap', 'candidateKey': KEY, 'value': copy.deepcopy(value)}
        return {'key': key, 'sha256': canonical_digest(records[key])}
    record('start', {'configurationDigest': BASIS})
    keys = {}
    for kind, key_id, permissions in (
        ('control', '11111111-1111-4111-8111-111111111111', ['admin:read', 'agent:runtime-control:runtime-original']),
        ('agent', '22222222-2222-4222-8222-222222222222', ['admin:read', 'agent:workspace', 'agent:runtime-task:runtime-original',
            'agent:studio-create-map-runtime:runtime-original', 'agent:studio-compose-runtime:runtime-original'])):
        issued = {'apiKeyId': key_id, 'secretArn': 'arn:aws:secretsmanager:us-east-1:585192672263:secret:'+kind+'-abcdef',
            'versionId': ('1' if kind == 'control' else '2')*32, 'permissions': permissions,
            'effectivePermissions': {'id': key_id, 'status': 'active', 'canAuthenticate': True, 'permissions': permissions}}
        record('key-'+kind, issued)
        record('key-'+kind+'-intent', {'permissions': permissions, 'secretVersionId': issued['versionId'],
            'expiresAt': datetime.fromtimestamp(binding['originalExpiresEpoch'], timezone.utc).isoformat()})
        keys[kind] = issued
    completed = record('completed', {'schema': 'honua.private-native-bootstrap/v1', 'candidateKey': KEY,
        'configurationDigest': BASIS, 'operation': native_operation, 'qualification': False, 'target': {'targetId': 'target-original'}, 'keys': keys})
    pricing = ROOT/'certification/agent-e2e/model-pricing.json'
    model = {**load_json(pricing), 'pricingDigest': digest_bytes(pricing.read_bytes()), 'maxOutputTokens': 1024}
    region, account = 'us-east-1', '585192672263'
    native_pin = {k: model[k] for k in ('modelId', 'returnedModelId', 'profileArn', 'tokenCountingModelArn')}
    native_pin['profileSha256'] = model['profile']['sha256']
    controller = {'schemaVersion': 1, 'region': region, 'accountId': account,
        'clusterArn': f'arn:aws:ecs:{region}:{account}:cluster/honua-pilot-fixture',
        'taskDefinitionArn': f'arn:aws:ecs:{region}:{account}:task-definition/honua-pilot-fixture-campaign-controller:1',
        'taskRoleArn': f'arn:aws:iam::{account}:role/honua-pilot-fixture-controller',
        'executionRoleArn': f'arn:aws:iam::{account}:role/honua-pilot-fixture-controller-execution',
        'containerName': 'campaign-controller', 'image': {
            'uri': f'{account}.dkr.ecr.{region}.amazonaws.com/honua-pilot-fixture-trusted-controller@sha256:'+'a'*64,
            'sourceSha': 'a'*40, 'treeId': 'b'*40, 'archiveSha256': 'c'*64, 'runtimeContractSha256': BASIS},
        'network': {'subnetIds': ['subnet-abc'], 'securityGroupIds': ['sg-abc']},
        'lifecycle': {'stateMachineArn': f'arn:aws:states:{region}:{account}:stateMachine:honua-pilot-fixture-campaign-controller:1',
            'stateMachineRoleArn': f'arn:aws:iam::{account}:role/honua-pilot-fixture-controller-lifecycle', 'definitionDigest': BASIS},
        'custody': {'bucket': 'honua-pilot-fixture-controller-fixture', **{key: 'controller/'+key+'/' for key in
            ('requestPrefix', 'sourcePrefix', 'bundlePrefix', 'outcomePrefix', 'evidencePrefix')}}, 'maxSeconds': 7200,
        'dispatchBudget': {'state': {'region': region, 'bucket': 'retained', 'table': 'state'}, 'candidateKey': KEY,
            'generationDigest': BASIS, 'reservationId': 'runtime-infrastructure', 'operationId': 'runtime', 'basisDigest': BASIS,
            'perDispatchCeilingUsd': '1', 'maximumDispatches': 1, 'claimTable': 'honua-pilot-fixture-controller-dispatch-claims'},
        'model': native_pin}
    sources = {}
    def put(name, value):
        target = tmp_path/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value))
        sources[name] = target
        return {'path': name, 'sha256': digest_bytes(target.read_bytes())}
    controller_ref = put('certification/agent-e2e/controller-handoff.json', controller)
    worker_ref = {'bucket': 'retained', 'key': 'agent-e2e/'+KEY[7:]+'/installed-workers/runtime-original/slot-original/1/1/installation.json',
        'versionId': 'worker-v1', 'sha256': BASIS}
    ceiling = {'createMap': True, 'createDashboard': False, 'compose': True, 'proposePublication': False,
        'proposeRestore': False, 'datasetImportScopes': [], 'managedProcessIds': []}
    value = {'schema': 'honua.installed-console-declaration/v1', 'candidateKey': KEY, 'runtimeId': 'runtime-original',
        'targetId': 'target-original', 'endpoint': 'https://agent.pilot.demo.honua.io', 'controller': controller_ref,
        'nativeBootstrap': completed, 'workers': [worker_ref], 'mutationCeilings': ceiling, 'model': model,
        'operationPolicy': {'maxTurns': 4, 'maxActions': 8, 'allowedActions': ['mcp_list_tools', 'mcp_call_tool', 'ask_user']},
        'operation': {'operationId': 'runtime', 'liabilityBasisDigest': BASIS,
            'reservationIds': {'infrastructure': 'runtime-infrastructure', 'cleanup': 'runtime-cleanup'}},
        'fundingBinding': binding, 'evidence': {'region': region, 'bucket': 'retained'}}
    issued_ref = {'key': prefix+'/key-agent', 'sha256': canonical_digest(records[prefix+'/key-agent'])}
    verified = {'descriptor': {'schema': 'honua.installed-worker-slot/v1', 'candidateKey': KEY, 'runtimeId': 'runtime-original',
            'slotId': 'slot-original', 'originalFundingExpiresEpoch': binding['originalExpiresEpoch'],
            'clusterArn': f'arn:aws:ecs:{region}:{account}:cluster/worker-slot-original',
            'taskDefinitionArn': f'arn:aws:ecs:{region}:{account}:task-definition/worker-slot-original:1',
            'taskRoleArn': f'arn:aws:iam::{account}:role/honua-agent-slot-original', 'image': 'actual-digest-fixture',
            'mcpSecret': {'secretArn': 'actual-immutable-fixture-secret', 'versionId': 'fixture-version'}},
        'issuedAgentKey': issued_ref, 'nativeBootstrap': completed, 'mcpBootstrap': {'key': 'original-mcp', 'sha256': BASIS},
        'producer': {'fixture': True}, 'sourceConfiguration': {'fixture': True}, 'plan': {'fixture': True},
        'proof': {'nativeEndpoint': value['endpoint']}, 'activation': False, 'qualification': False}
    from test_agent_e2e_runtime_index import runtime_proof_fixture
    verified['proof']['runtime'] = runtime_proof_fixture(verified['descriptor']['image'])
    calls = []
    def verify(ref, **kwargs):
        calls.append((copy.deepcopy(ref), kwargs))
        return copy.deepcopy(verified)
    def read(name, sha):
        selected = sources.get(name, ROOT/name)
        if digest_bytes(selected.read_bytes()) != sha: raise PackError('source digest changed')
        return selected
    ref = put('certification/agent-e2e/installed-runtimes/runtime-original.json', value)
    def load():
        return load_declaration(ref, durable=durable, s3=None, ecs=None, states=None, guard=durable._require_live_lease,
            source_reader=read, worker_verifier=verify)
    def rewrite():
        ref.update(put(ref['path'], value))
    return SimpleNamespace(value=value, ref=ref, load=load, rewrite=rewrite, verified=verified, records=records,
        durable=durable, calls=calls, put=put, prefix=prefix)


def test_verified_declaration_derives_native_request_from_original_keys_and_worker_proof(declaration):
    result = declaration.load()
    assert result['configurationDigest'] == canonical_digest(declaration.value)
    assert result['configurationDigest'] != BASIS  # The native-bootstrap digest is a different original.
    assert result['registrationRequest']['controlApiKeyId'] == '11111111-1111-4111-8111-111111111111'
    assert result['registrationRequest']['taskApiKeyId'] == '22222222-2222-4222-8222-222222222222'
    assert result['keys']['task']['reference'] == declaration.verified['issuedAgentKey']
    assert result['registrationRequest']['slots'][0]['taskDefinitionArn'] == declaration.verified['descriptor']['taskDefinitionArn']
    assert result['registrationRequest']['slots'][0]['clusterArn'] == declaration.verified['descriptor']['clusterArn']
    assert declaration.calls[0][1]['runtime_id'] == 'runtime-original'
    assert 'registrationReceipt' not in declaration.value and 'registrationDigest' not in result


@pytest.mark.parametrize('change', ['candidate', 'expiry', 'scope', 'model-price', 'controller-ref', 'operation-policy', 'bare-worker-slot'])
def test_changed_source_cannot_select_another_funding_model_or_worker_authority(declaration, change):
    value = declaration.value
    if change == 'candidate': value['candidateKey'] = 'sha256:'+'9'*64
    elif change == 'expiry': value['fundingBinding'] = {**value['fundingBinding'], 'originalExpiresEpoch': value['fundingBinding']['originalExpiresEpoch']+1}
    elif change == 'scope': value['mutationCeilings']['proposeRestore'] = True
    elif change == 'model-price': value['model']['inputUsdPerMillion'] = '0'
    elif change == 'controller-ref': value['controller']['sha256'] = 'sha256:'+'9'*64
    elif change == 'operation-policy': value['operationPolicy']['allowedActions'].append('terminal')
    else: value['workers'][0] = {'slotId': 'caller-selected'}
    declaration.rewrite()
    with pytest.raises(PackError): declaration.load()


@pytest.mark.parametrize('change', ['other-origin', 'other-agent', 'other-bootstrap', 'activated', 'qualified', 'runtime-missing', 'runtime-older'])
def test_worker_proof_must_bind_original_inactive_issuance_and_exact_origin(declaration, change):
    if change == 'other-origin': declaration.verified['proof']['nativeEndpoint'] = 'https://api.pilot.demo.honua.io'
    elif change == 'other-agent': declaration.verified['issuedAgentKey']['sha256'] = 'sha256:'+'9'*64
    elif change == 'other-bootstrap': declaration.verified['nativeBootstrap']['key'] = 'other/completed'
    elif change == 'activated': declaration.verified['activation'] = True
    elif change == 'qualified': declaration.verified['qualification'] = True
    elif change == 'runtime-missing': del declaration.verified['proof']['runtime']
    else:
        proof = declaration.verified['proof']['runtime']
        proof['index']['files']['agent_e2e_mcp_runner.py'] = 'sha256:'+'f'*64
        proof['runtimeBundleDigest'] = canonical_digest(proof['index'])
    with pytest.raises(PackError): declaration.load()


def test_lost_lease_prevents_installation_source_or_worker_reads(declaration):
    declaration.durable.live = False
    with pytest.raises(PackError, match='lease'): declaration.load()
    assert not declaration.calls
