"""Original HTTP joins with SDK/S3/provider doubles; not deployed qualification."""
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from agent_e2e_console_loop import ExecutionLoop
from agent_e2e_controller import BedrockModel
import agent_e2e_execution_witness as module
from agent_e2e_execution_witness import project_execution, read_witness, resolve_reference, witness_prefix, human_context_digest, publish_execution
from agent_e2e_pack import PackError, canonical_digest, digest_bytes, load_json
from agent_e2e_setup_http import SetupHttp
from agent_e2e_worker_lifecycle import WorkerLifecycle
from test_agent_e2e_console_loop import Provider, answer
from test_agent_e2e_console_runtime import runtime, slot, E
from test_agent_e2e_interactive_budget import interactive
from test_agent_e2e_setup_http import server
from test_agent_e2e_budget import KEY, BASIS

ROOT = Path(__file__).resolve().parents[1]


class Objects:
    def __init__(self): self.values = {}; self.calls = []
    def add(self, key, raw, version='original-version'):
        self.values[(key, version)] = raw
        return {'bucket': 'retained', 'key': key, 'versionId': version, 'sha256': digest_bytes(raw)}
    def get_object(self, **request):
        self.calls.append(request)
        version = request.get('VersionId')
        if version is None:
            matches = [version for key, version in self.values if key == request['Key']]
            if not matches: raise KeyError(request['Key'])
            assert len(matches) == 1
            version = matches[0]
        raw = self.values[(request['Key'], version)]
        return {'VersionId': version, 'ContentLength': len(raw), 'Body': io.BytesIO(raw)}


@pytest.fixture
def witness(slot, runtime, server, tmp_path, monkeypatch, request):
    monkeypatch.setenv('WITNESS_REGISTRATION_FIXTURE_KEY', 'private-observer-fixture-'+'8'*32)
    objects = Objects()
    body = runtime.value
    if getattr(request, 'param', None) == 'prelaunch-restart':
        slot.journal.append('worker-installation-observation', slot.worker.inspect())
    slot.worker.start(slot.journal, slot.launch)
    worker_binding = {'schemaVersion': 1, 'state': 'running', 'candidateKey': KEY, 'runtimeId': 'runtime-original',
        'slotId': slot.config['slotId'], 'executionId': E, 'workspaceId': 'workspace-original', 'turnId': 'turn-original',
        'claimGeneration': 1, 'expiresEpoch': slot.launch['input']['expiresEpoch'],
        'workerId': slot.ecs.task['taskArn'], 'nativeBindingDigest': BASIS,
        'context': copy.deepcopy(body['binding']['turn']['context']),
        'contextDigest': canonical_digest(body['binding']['turn']['context']),
        'acknowledgement': {'actorId': 'actual-task-fixture', 'credentialVersion': 'actual-key-version-fixture'}}
    if getattr(request, 'param', None) == 'wrong-task-context':
        worker_binding['context']['viewId'] = 'not-the-acknowledged-original'
    body['workerBinding'] = worker_binding
    body['execution']['state'] = 'running'
    registered_slot = {'slotId': slot.config['slotId'], 'clusterArn': slot.config['clusterArn'],
        'taskDefinitionArn': slot.config['taskDefinitionArn'], 'taskRoleArn': slot.config['taskRoleArn'],
        'image': slot.config['image'], 'mcpSecretArn': slot.config['mcpSecret']['secretArn'],
        'mcpSecretVersionId': slot.config['mcpSecret']['versionId']}
    pricing = ROOT/'certification/agent-e2e/model-pricing.json'
    model_config = {**load_json(pricing), 'pricingDigest': digest_bytes(pricing.read_bytes()), 'maxOutputTokens': 1024}
    policy = {'maxTurns': 4, 'maxActions': 8, 'allowedActions': ['mcp_list_tools', 'mcp_call_tool', 'ask_user']}
    declaration = {'schema': 'honua.installed-console-declaration/v1', 'model': model_config, 'operationPolicy': policy, 'candidateKey': KEY,
        'runtimeId': 'runtime-original', 'targetId': 'target-original', 'endpoint': server.url,
        'fundingBinding': copy.deepcopy(runtime.durable.state['ledger']['interactiveAccounting']),
        'evidence': {'region': 'us-east-1', 'bucket': 'retained'}}
    source = tmp_path/'declaration.json'
    source.write_text(json.dumps(declaration))
    source_ref = {'path': 'certification/agent-e2e/installed-runtimes/runtime-original.json', 'sha256': digest_bytes(source.read_bytes())}
    registration = {'candidateKey': KEY, 'runtimeId': 'runtime-original', 'targetId': 'target-original',
        'configurationDigest': canonical_digest(declaration), 'registrationDigest': BASIS,
        'originalFundingExpiresEpoch': declaration['fundingBinding']['originalExpiresEpoch'],
        'taskPrincipal': copy.deepcopy(worker_binding['acknowledgement']), 'slots': [registered_slot]}
    def respond(method, path, raw):
        if path.endswith('/registration'): return 200, {}, json.dumps(registration).encode()
        if method == 'POST':
            payload = json.loads(raw)
            assert path.endswith('/final-response')
            body['execution']['stateRevision'] += 1
            body['execution']['finalMessageId'] = payload['messageId']
            body['finalMessage'] = {'id': payload['messageId'], 'turnId': payload['turnId'], 'role': 'assistant',
                'content': payload['content'], 'evidenceKind': 'conversation'}
        if '/effects?' in path:
            return 200, {}, json.dumps({**body, 'effects': {'operations': body['execution'].get('operations', []),
                'artifacts': body['execution'].get('artifacts', [])}}).encode()
        return 200, {}, json.dumps(body).encode()
    server.respond = respond
    bound = runtime.control.read(E, 'workspace-original', 'turn-original')
    slot.journal.append('worker-binding-observation', {'reference': bound['reference']})
    originals = runtime.durable.state.setdefault('scenarioOperations', {})
    def retain(kind, step, row):
        key = 'witness-registration/'+kind+'/'+step
        assert key not in originals
        originals[key] = copy.deepcopy(row)
        runtime.durable._persist()
        return {'key': key, 'sha256': canonical_digest(row)}
    http = SetupHttp(server.url, 'WITNESS_REGISTRATION_FIXTURE_KEY', identity={'candidateKey': KEY, 'attemptId': 'registration-fixture'},
        authorize=runtime.durable._require_live_lease, retain=retain)
    actual_registration = http.json('registration-final', 'GET', '/api/v1/agent/runtimes/runtime-original/registration')
    prefix = 'native-runtime-registration:'+canonical_digest({'candidateKey': KEY, 'runtimeId': 'runtime-original'})[7:]
    originals[prefix+'/completed'] = {'producer': 'private-native-runtime-registration', 'candidateKey': KEY, 'value': {
        'schema': 'honua.private-native-runtime-registration/v1', 'registration': registration,
        'nativeOriginals': {'registration-final': actual_registration['reference']},
        'provenance': {'sourceConfiguration': source_ref, 'evidence': declaration['evidence']},
        'activation': False, 'qualification': False}}
    runtime.durable._persist()
    response = answer()
    response['model'] = model_config['returnedModelId']
    provider = Provider([response])
    dispatcher = type('NoActions', (), {'capability': 'private-capability-'+'a'*32})()
    loop = ExecutionLoop(native=runtime.control, journal=slot.journal, original=bound,
        model=BedrockModel(model_config, client=provider), dispatcher=dispatcher, configuration=policy)
    loop.run()
    body['execution'].update(state='completed', stateRevision=2, operations=[], artifacts=[])
    effect = runtime.control.effects(runtime.control.read(E, 'workspace-original', 'turn-original'))
    final = runtime.control.read(E, 'workspace-original', 'turn-original')
    slot.journal.append('effect-observation', {'reference': effect['reference'], 'executionReference': final['reference']})
    human = {'turnId': 'turn-original', 'originatingActorId': 'human-original',
        'context': copy.deepcopy(body['binding']['turn']['context']), 'submission': copy.deepcopy(body['submission']),
        'execution': copy.deepcopy(body['execution'])}
    def read_source(path, sha):
        selected = source if path == source_ref['path'] else ROOT/path
        assert digest_bytes(selected.read_bytes()) == sha
        return selected
    state = {'candidateKey': KEY, 'version': 1, 'generationDigest': BASIS, 'payload': copy.deepcopy(runtime.durable.state)}
    key = 'agent-e2e/'+KEY[7:]+'/1-11111111-1111-4111-8111-111111111111.json'
    def snapshot():
        raw = json.dumps(state, separators=(',', ':')).encode()
        return objects.add(key, raw)
    reference = snapshot()
    options = {'s3': objects, 'candidate_key': KEY, 'runtime_id': 'runtime-original', 'workspace_id': 'workspace-original',
        'turn_id': 'turn-original', 'execution_id': E, 'registration_digest': BASIS,
        'human_turn_response_text': json.dumps(human), 'source_reader': read_source}
    return type('Fixture', (), {'state': state, 'objects': objects, 'human': human, 'options': options,
        'reference': reference, 'snapshot': staticmethod(snapshot), 'body': body, 'slot': slot})()


def test_private_projection_joins_actual_http_originals_and_provider_usage(witness):
    result = project_execution(witness.reference, **witness.options)
    assert result['worker']['contextDigest'] == result['humanTurn']['contextDigest']
    assert result['worker']['taskPrincipal']['actorId'] == 'actual-task-fixture'
    assert result['modelRounds'][0]['usage'] == {'inputTokens': 100, 'outputTokens': 10}
    assert result['modelRounds'][0]['costUsd'] == '0.000495'
    assert result['native']['state'] == 'completed' and result['qualification'] is False
    encoded = json.dumps(result)
    assert 'Actual fixture question' not in encoded and 'The actual original response' not in encoded
    assert 'private-' not in encoded


@pytest.mark.parametrize('witness', ['prelaunch-restart'], indirect=True)
def test_prelaunch_restart_still_projects_the_one_original_installation(witness):
    result = project_execution(witness.reference, **witness.options)
    assert result['native']['state'] == 'completed' and result['qualification'] is False


def test_human_response_text_uses_python_canonicalization_without_js_number_roundtrip():
    assert human_context_digest('{"context":{"value":1.0,"unicode":"😀"}}') == canonical_digest({'value': 1.0, 'unicode': '😀'})
    assert human_context_digest('{"context":{"value":1.0}}') != human_context_digest('{"context":{"value":1}}')
    with pytest.raises(PackError): human_context_digest('{"context":{"value":1,"value":1.0}}')


@pytest.mark.parametrize('witness', ['wrong-task-context'], indirect=True)
def test_original_http_task_ack_context_must_match_its_own_digest(witness):
    with pytest.raises(PackError, match='task-authenticated native context'):
        project_execution(witness.reference, **witness.options)


@pytest.mark.parametrize('change', ['human-context', 'human-execution', 'snapshot-hash', 'missing-worker-ack',
    'missing-provider-original', 'missing-aws-history', 'missing-registration-original', 'unknown-cost'])
def test_private_projection_refuses_missing_or_rebound_originals(witness, change):
    options = dict(witness.options)
    state = witness.state['payload']
    events = state['interactiveExecutions'][E]['events']
    if change == 'human-context':
        witness.human['context']['differentSelection'] = 'not-original'
        options['human_turn_response_text'] = json.dumps(witness.human)
    elif change == 'human-execution':
        witness.human['execution']['executionId'] = 'execution-'+'f'*32
        options['human_turn_response_text'] = json.dumps(witness.human)
    elif change == 'snapshot-hash':
        witness.objects.values[(witness.reference['key'], witness.reference['versionId'])] += b' '
        with pytest.raises(PackError): project_execution(witness.reference, **options)
        return
    elif change == 'missing-registration-original':
        completed = next(row['value'] for row in state['scenarioOperations'].values() if row.get('producer') == 'private-native-runtime-registration')
        state['scenarioOperations'].pop(completed['nativeOriginals']['registration-final']['key'])
    elif change == 'unknown-cost': state['ledger']['interactiveEntries'][-1].update(state='reserved', actualUsd=None)
    else:
        kind = {'missing-worker-ack': 'worker-binding-observation', 'missing-provider-original': 'provider-response',
            'missing-aws-history': 'worker-history'}[change]
        if kind == 'worker-history':
            next(row for row in events if row['kind'] == 'worker-observation')['value']['launchHistory'] = []
        else: events[:] = [row for row in events if row['kind'] != kind]
        record = state['interactiveExecutions'][E]
        previous = canonical_digest({k: v for k, v in record.items() if k != 'events'})
        for ordinal, event in enumerate(events):
            event.update(ordinal=ordinal, previousDigest=previous)
            event['digest'] = canonical_digest({k: v for k, v in event.items() if k != 'digest'})
            previous = event['digest']
    with pytest.raises(PackError): project_execution(witness.snapshot(), **options)


def test_reader_replays_original_snapshot_instead_of_accepting_witness_metadata(witness):
    result = project_execution(witness.reference, **witness.options)
    raw = json.dumps(result, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    key = witness_prefix(KEY, 'runtime-original', E)+'1-'+digest_bytes(raw)[7:]+'.json'
    reference = witness.objects.add(key, raw)
    read = read_witness(reference, bucket='retained', **witness.options)
    assert read['witness'] == result
    result['worker']['contextDigest'] = 'sha256:'+'f'*64
    changed = json.dumps(result, sort_keys=True, separators=(',', ':')).encode()
    ref = witness.objects.add(witness_prefix(KEY, 'runtime-original', E)+'1-'+digest_bytes(changed)[7:]+'.json', changed)
    with pytest.raises(PackError): read_witness(ref, bucket='retained', **witness.options)


@pytest.mark.parametrize('failure', [None, 'lost-put-ack', 'put-unknown-no-object'])
def test_export_has_original_intent_and_never_repeats_an_unknown_put(witness, failure):
    durable, journal, objects = witness.slot.durable, witness.slot.journal, witness.objects
    store = durable.store
    store.bucket, store.table, store.owner, store.fence, store.version = 'retained', 'original-state', 'original-owner', 1, 1
    store.generation, store.clock = {'digest': BASIS}, lambda: 1000
    store.key = {'candidateKey': {'S': KEY}}
    head = {'leaseOwner': {'S': store.owner}, 'fence': {'N': '1'}, 'leaseUntil': {'N': '2000'}, 'stateVersion': {'N': '1'},
        'generationDigest': {'S': BASIS}, 'snapshotKey': {'S': witness.reference['key']},
        'snapshotVersion': {'S': witness.reference['versionId']}, 'snapshotDigest': {'S': witness.reference['sha256']}}
    store.db = SimpleNamespace(get_item=lambda **request: {'Item': copy.deepcopy(head)})
    calls = []
    def put(**request):
        assert journal.values('witness-export-intent')
        assert request['IfNoneMatch'] == '*'
        calls.append(request)
        if failure != 'put-unknown-no-object': objects.add(request['Key'], request['Body'], version='witness-v1')
        if failure is not None: raise TimeoutError('original immutable PUT reply lost')
        return {'VersionId': 'witness-v1'}
    objects.put_object = put
    def publish():
        return publish_execution(durable=durable, journal=journal, s3=objects, bucket='retained',
            guard=durable._require_live_lease, source_reader=witness.options['source_reader'])
    if failure == 'put-unknown-no-object':
        with pytest.raises(KeyError): publish()
        with pytest.raises(KeyError): publish()
        assert len(calls) == 1 and len(journal.values('witness-export-intent')) == 1
        assert not journal.values('witness-export-observation')
    else:
        reference = publish()
        assert publish() == reference and len(calls) == 1
        assert len(journal.values('witness-export-intent')) == len(journal.values('witness-export-observation')) == 1
        assert read_witness(reference, bucket='retained', **witness.options)['witness']['executionId'] == E
    assert durable.state['ledger']['interactivePaidAdmissions'][0]['state'] == 'active'
    assert all(row['state'] == 'reserved' for row in durable.state['ledger']['interactiveEntries'] if row['kind'] != 'model')


def test_fixed_cli_reads_exact_private_reference_and_emits_only_replayed_projection(witness, monkeypatch):
    import boto3
    proof = project_execution(witness.reference, **witness.options)
    raw = json.dumps(proof, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    reference = witness.objects.add(witness_prefix(KEY, 'runtime-original', E)+'1-'+digest_bytes(raw)[7:]+'.json', raw)
    request = {'schemaVersion': 1, 'candidateKey': KEY, 'runtimeId': 'runtime-original', 'executionId': E,
        'workspaceId': 'workspace-original', 'turnId': 'turn-original', 'registrationDigest': BASIS,
        'evidence': {'region': 'us-east-1', 'bucket': 'retained'},
        'humanTurnResponseText': witness.options['human_turn_response_text'], 'reference': reference}
    output = io.BytesIO()
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode())))
    monkeypatch.setattr(sys, 'stdout', SimpleNamespace(buffer=output))
    monkeypatch.setattr(boto3, 'client', lambda service, **_: witness.objects if service == 's3' else pytest.fail('non-read SDK client'))
    actual_reader = module.read_witness
    monkeypatch.setattr(module, 'read_witness', lambda *a, **k: actual_reader(*a, **k, source_reader=witness.options['source_reader']))
    module.main([])
    assert json.loads(output.getvalue()) == {'reference': reference, 'witness': proof}
    assert all('VersionId' in row for row in witness.objects.calls)
    assert b'Actual fixture question' not in output.getvalue()


def test_actual_cli_process_refuses_oversized_stdin_without_echoing_private_input():
    secret = b'private-input-do-not-echo'
    result = subprocess.run([sys.executable, str(Path(module.__file__).resolve())],
        input=secret+(b'x'*(2*1024*1024)), capture_output=True, timeout=15,
        env={'PYTHONPATH': str(ROOT/'tools'), 'PYTHONDONTWRITEBYTECODE': '1'})
    assert result.returncode == 1 and result.stdout == b''
    assert secret not in result.stderr and len(result.stderr) < 256


@pytest.mark.parametrize('change', ['truncated', 'deleted', 'overwritten', 'ambiguous'])
def test_discovery_never_accepts_ambiguous_or_missing_original_versions(change):
    prefix = witness_prefix(KEY, 'runtime-original', E)
    rows = [{'Key': prefix+'1-'+'a'*64+'.json', 'VersionId': 'v1', 'IsLatest': True}]
    response = {'Versions': rows}
    if change == 'truncated': response['IsTruncated'] = True
    elif change == 'deleted': response['DeleteMarkers'] = [{'Key': rows[0]['Key']}]
    elif change == 'overwritten': rows.append({**rows[0], 'VersionId': 'v2', 'IsLatest': False})
    else: rows.append({**rows[0], 'Key': prefix+'1-'+'b'*64+'.json'})
    s3 = type('Discovery', (), {'list_object_versions': lambda self, **_: response})()
    with pytest.raises(PackError): resolve_reference(s3, bucket='retained', candidate_key=KEY, runtime_id='runtime-original', execution=E)
