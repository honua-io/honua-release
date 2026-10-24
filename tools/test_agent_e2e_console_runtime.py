"""Actual HTTP and crash controls; fixture principals are not live custody."""
import copy
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_e2e_budget import update
from agent_e2e_console_runtime import NativeControl, ExecutionJournal, secret_for
from agent_e2e_installed_container import InstalledBinding, bind_message, McpOnlyExecutor
from agent_e2e_worker import MCP_PROTOCOL, PROTOCOL, validate_command
from agent_e2e_installed_worker import InstalledSlot, sdk_original
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_state import preserve_history
from bundle_agent_e2e_container import bundle, RUNTIME
from test_agent_e2e_interactive_budget import interactive
from test_agent_e2e_setup_http import server
from test_agent_e2e_budget import KEY, BASIS, reserve_operation

E = 'execution-'+'1'*32
TASK = 'arn:aws:ecs:us-east-1:123456789012:task/fixture/'+'2'*32
CAP = 'private-execution-capability-'+'3'*32


class Retained:
    def __init__(self, state, path):
        self.state, self.path, self.live = state, path, True
        self.store = SimpleNamespace(candidate=KEY)
        self.previous = copy.deepcopy(state)
    def _require_live_lease(self):
        if not self.live: raise PackError('lost original lease')
    def _persist(self):
        self._require_live_lease()
        preserve_history(self.previous, self.state)
        self.previous = copy.deepcopy(self.state)
    def update(self, path, operation, **kwargs):
        self._require_live_lease()
        try: return update(path, operation, **kwargs)
        finally:
            self.state['ledger'] = json.loads(path.read_text())
            self._persist()


@pytest.fixture
def runtime(interactive, server, monkeypatch):
    path, binding, now, imported, configuration = interactive
    monkeypatch.setenv('HONUA_EXTERNAL_EXECUTION_CONTROL_KEY', 'private-control-key-'+'4'*32)
    reserve_operation(path, rid='runtime-infra', amount='1', purpose='shared-environment', operation_id='runtime')
    reserve_operation(path, rid='runtime-cleanup', amount='0.1', kind='cleanup', purpose='shared-environment', operation_id='runtime')
    durable = Retained({'ledger': json.loads(path.read_text()), 'bootstrapImport': imported,
        'interactiveFunding': {'binding': binding, 'configuration': json.dumps(configuration)}}, path)
    config = {'endpoint': server.url, 'runtimeId': 'runtime-original', 'targetId': 'target-original',
        'runtimeBindingDigest': BASIS, 'overhead': {'operationId': 'runtime', 'basisDigest': BASIS,
            'reservationIds': {'infrastructure': 'runtime-infra', 'cleanup': 'runtime-cleanup'}}}
    # Use the exact existing original fixture's operation, never introduce an
    # unreserved control cost or assign a guessed zero to it.
    for row in durable.state['ledger']['operations'][-2:]:
        assert row['operationId'] == config['overhead']['operationId']
    value = {'schemaVersion': 1, 'candidateKey': KEY, 'runtimeId': 'runtime-original', 'targetId': 'target-original',
        'requestDigest': BASIS, 'execution': {'executionId': E, 'state': 'queued', 'stateRevision': 0},
        'binding': {'workspaceId': 'workspace-original', 'targetId': 'target-original',
            'runtimeId': 'runtime-original', 'runtimeBindingDigest': BASIS,
            'turn': {'turnId': 'turn-original', 'prompt': 'Actual fixture question', 'context': {'targetId': 'target-original'}}}}
    server.respond = lambda *args: (200, {}, json.dumps(value).encode())
    return SimpleNamespace(durable=durable, config=config, value=value,
        control=NativeControl(durable=durable, configuration=config), now=now)


def original(runtime):
    return runtime.control.read(E, 'workspace-original', 'turn-original')


def test_actual_authenticated_native_original_creates_append_only_same_e_journal(runtime, server):
    observed = original(runtime)
    journal = ExecutionJournal(runtime.durable, observed)
    journal.append('native-observation', {'reference': observed['reference']})
    recovered = ExecutionJournal(runtime.durable, original(runtime))
    assert recovered.record == journal.record and len(recovered.record['events']) == 1
    assert len(runtime.durable.state['interactiveExecutions']) == 1
    assert {k.lower(): v for k, v in server.seen[0][2].items()}['x-api-key'].startswith('private-control-key-')
    before = copy.deepcopy(runtime.durable.previous)
    journal.record['events'][0]['value']['reference'] = {'key': 'changed'}
    with pytest.raises(PackError): preserve_history(before, runtime.durable.state)


def test_lost_http_mutation_ack_cannot_start_another_native_claim(runtime, server):
    observed = original(runtime)
    server.respond = lambda *args: None
    with pytest.raises(PackError, match='reconcile'):
        runtime.control.mutate('claim', observed, {'claimGeneration': 1}, capability=CAP)
    count = len(server.seen)
    restarted = NativeControl(durable=runtime.durable, configuration=runtime.config)
    with pytest.raises(PackError, match='already exists'):
        restarted.mutate('claim', observed, {'claimGeneration': 1}, capability=CAP)
    assert len(server.seen) == count
    assert CAP not in json.dumps(runtime.durable.state)


def test_original_native_reply_is_retained_if_funding_expires_on_the_wire(runtime, server):
    def expire(*_):
        runtime.control.clock = lambda: runtime.durable.state['ledger']['interactiveAccounting']['originalExpiresEpoch']+1
        return 200, {}, json.dumps(runtime.value).encode()
    server.respond = expire
    observed = original(runtime)
    assert observed['reference']['key'] in runtime.durable.state['nativeRuntimeHttp']
    assert len(runtime.durable.state['nativeRuntimeHttp']) == 2
    with pytest.raises(PackError, match='funding expired'): original(runtime)
    assert len(server.seen) == 1


@pytest.mark.parametrize('field,value', [('candidateKey', 'sha256:'+'9'*64), ('targetId', 'other'),
    ('runtimeId', 'other'), ('schemaVersion', 2)])
def test_wrong_actual_native_response_stays_retained_but_does_not_create_execution(runtime, field, value):
    runtime.value[field] = value
    with pytest.raises(PackError): original(runtime)
    assert not runtime.durable.state.get('interactiveExecutions')
    assert len(runtime.durable.state['nativeRuntimeHttp']) == 2


def test_lost_lease_prevents_http_and_private_control_material_is_domain_separated(runtime, server):
    first = secret_for('admission-owner/v1', candidate=KEY, runtime='runtime-original', execution=E)
    second = secret_for('execution-capability/v1', candidate=KEY, runtime='runtime-original', execution=E, generation=1)
    assert first != second
    assert first == secret_for('admission-owner/v1', candidate=KEY, runtime='runtime-original', execution=E)
    runtime.durable.live = False
    with pytest.raises(PackError, match='lease'): original(runtime)
    assert not server.seen


def test_model_intent_recovery_never_buys_another_invocation(runtime):
    journal = ExecutionJournal(runtime.durable, original(runtime))
    runtime.durable.update(runtime.durable.path, 'acquire-interactive-attempt', candidate_key=KEY,
        execution_id=E, target_id='target-original', workspace_id='workspace-original', turn_id='turn-original',
        owner_token='a'*32, expires_epoch=runtime.now+600)
    runtime.durable.update(runtime.durable.path, 'reserve-interactive', candidate_key=KEY,
        execution_id=E, target_id='target-original', workspace_id='workspace-original', turn_id='turn-original',
        owner_token='a'*32, reservation_id='original-model-reservation', kind='model', amount='1', basis_digest=BASIS)
    journal.before_model(0, BASIS, 'original-model-reservation', {'ownerToken': 'a'*32})
    recovered = ExecutionJournal(runtime.durable, original(runtime))
    with pytest.raises(PackError, match='already intended'):
        recovered.before_model(0, BASIS, 'replacement-reservation', {'ownerToken': 'a'*32})
    with pytest.raises(PackError, match='prior model outcome'):
        recovered.before_model(1, BASIS, 'replacement-reservation', {'ownerToken': 'a'*32})


@pytest.fixture
def installed(server, tmp_path, interactive):
    _, marker, now, *_ = interactive
    configuration = {'schemaVersion': 2, 'mode': 'installed-slot', 'candidateKey': KEY, 'runtimeId': 'runtime-original',
        'slotId': 'slot-original', 'commandQueueUrl': 'https://sqs.us-east-1.amazonaws.com/123456789012/command.fifo',
        'replyQueueUrl': 'https://sqs.us-east-1.amazonaws.com/123456789012/reply.fifo',
        'originalFundingExpiresEpoch': marker['originalExpiresEpoch'], 'maxExecutionSeconds': 6000}
    mcp = {'endpoint': server.url+'/mcp', 'credentialKind': 'api-key', 'credential': 'private-agent-key',
        'workflowView': 'workspace', 'bindingMode': 'native-execution/v1', 'runtimeId': 'runtime-original', 'slotId': 'slot-original'}
    value = {'protocol': 'honua.installed-worker-bind/v1', 'candidateKey': KEY, 'runtimeId': 'runtime-original',
        'slotId': 'slot-original', 'executionId': E, 'workspaceId': 'workspace-original', 'turnId': 'turn-original',
        'claimGeneration': 1, 'expiresEpoch': now+600, 'workerId': TASK, 'nativeBindingDigest': BASIS, 'capability': CAP}
    expected = {'schemaVersion': 1, **{k: v for k, v in value.items() if k not in ('protocol', 'capability')}, 'state': 'running'}
    server.respond = lambda *args: (200, {}, json.dumps(expected).encode())
    binding = InstalledBinding(configuration, mcp, tmp_path/'binding.sqlite', worker_id=TASK)
    yield SimpleNamespace(binding=binding, value=value, expected=expected, path=tmp_path/'binding.sqlite')
    binding.close()


def test_actual_private_first_bind_replays_same_native_authority_without_rebinding(installed, server, monkeypatch):
    monkeypatch.delenv('HONUA_AGENT_MCP_CONFIG', raising=False)
    reply = installed.binding.accept(installed.value)
    replay = installed.binding.accept(copy.deepcopy(installed.value))
    assert reply['workerId'] == replay['workerId'] == TASK
    assert len(server.seen) == 2
    headers = {k.lower(): v for k, v in server.seen[0][2].items()}
    assert headers['x-honua-execution-capability'] == CAP and headers['x-honua-worker'] == TASK
    assert CAP not in json.dumps(reply)
    private = json.loads(os.environ['HONUA_AGENT_MCP_CONFIG'])
    assert private['agentContext'] == {'workspaceId': 'workspace-original', 'turnId': 'turn-original'}
    assert private['executionContext']['executionId'] == E
    changed = copy.deepcopy(installed.value); changed['executionId'] = 'execution-'+'5'*32
    with pytest.raises(PackError, match='cannot rebind'): installed.binding.accept(changed)
    assert len(server.seen) == 2 and installed.path.stat().st_mode & 0o777 == 0o600


def test_bind_specific_bound_does_not_reject_a_valid_large_mcp_command(installed):
    value = {'protocol': MCP_PROTOCOL, 'attemptId': E, 'actionId': 'action-original',
        'deadlineEpoch': installed.value['expiresEpoch']-10, 'timeoutSeconds': 120,
        'action': {'name': 'mcp_call_tool', 'input': {'name': 'native_tool', 'arguments': {'text': 'x'*33000}}}}
    validate_command(value, E, installed.value['expiresEpoch'], value['deadlineEpoch']-120)
    assert bind_message(None, {}, {'Body': json.dumps(value)}, installed.binding) is False
    value['action']['input']['arguments']['text'] = 'x'*100001
    with pytest.raises(PackError, match='command envelope'):
        bind_message(None, {}, {'Body': json.dumps(value)}, installed.binding)


def test_installed_worker_keeps_capability_off_disk_and_cannot_start_an_untrusted_shell(installed, tmp_path, monkeypatch):
    monkeypatch.delenv('HONUA_AGENT_MCP_CONFIG', raising=False)
    installed.binding.accept(installed.value)
    stored = installed.binding.db.execute('SELECT value FROM installed_binding').fetchone()[0]
    assert CAP not in stored and installed.binding.mcp['credential'] not in stored
    attempts = []
    executor = McpOnlyExecutor({'attemptId': E, 'expiresEpoch': installed.value['expiresEpoch']},
        tmp_path/'actions.sqlite', tmp_path, execute=lambda *args: attempts.append(args))
    request = {'protocol': PROTOCOL, 'attemptId': E, 'actionId': 'terminal-original',
        'deadlineEpoch': installed.value['expiresEpoch']-10, 'timeoutSeconds': 120, 'command': 'true'}
    validate_command(request, E, installed.value['expiresEpoch'], request['deadlineEpoch']-120)
    with pytest.raises(PackError, match='native MCP actions only'): executor.handle(request)
    assert not attempts
    changed = copy.deepcopy(installed.value); changed['capability'] = 'other-private-capability-'+'8'*32
    with pytest.raises(PackError, match='cannot rebind'): installed.binding.accept(changed)
    executor.db.close()


@pytest.mark.parametrize('change', ['worker', 'turn', 'capability-echo', 'claim-generation'])
def test_first_bind_refuses_foreign_native_projection_and_disclosed_capability(installed, server, monkeypatch, change):
    monkeypatch.delenv('HONUA_AGENT_MCP_CONFIG', raising=False)
    if change == 'worker': installed.expected['workerId'] = TASK[:-1]+'3'
    elif change == 'turn': installed.expected['turnId'] = 'other'
    elif change == 'claim-generation': installed.expected['claimGeneration'] = 2
    else: installed.expected['echo'] = CAP
    with pytest.raises(ValueError): installed.binding.accept(installed.value)
    assert installed.binding.binding is None and 'HONUA_AGENT_MCP_CONFIG' not in os.environ
    assert sqlite3.connect(installed.path).execute('SELECT COUNT(*) FROM installed_binding').fetchone()[0] == 1


def test_worker_export_import_closure_contains_no_controller_or_oracle(tmp_path):
    root = tmp_path/'worker'
    result = bundle(root)
    assert 'agent_e2e_installed_container.py' in result['files']
    assert not any('budget' in name or 'console_runtime' in name or 'semantics' in name for name in RUNTIME)
    process = subprocess.run(
        [sys.executable, '-I', '-c', 'import sys; sys.path.insert(0, sys.argv[1]); import agent_e2e_installed_container', str(root)],
        cwd=tmp_path, capture_output=True, text=True, check=False, timeout=10)
    assert process.returncode == 0, process.stderr


@pytest.fixture
def slot(runtime):
    from test_agent_e2e_worker_lifecycle import States, Claims, DEFINITION, CONFIG, TASK as PHYSICAL_TASK
    states, claims = States(), Claims()
    c = {'schema': 'honua.installed-worker-slot/v1', 'candidateKey': KEY, 'runtimeId': 'runtime-original',
        'slotId': 'slot-original', **{key: CONFIG[key] for key in ('region', 'stateMachineArn', 'stateMachineRoleArn',
            'definitionDigest', 'clusterArn', 'taskDefinitionArn', 'claimTable')},
        'taskRoleArn': 'arn:aws:iam::123456789012:role/queue-only',
        'originalFundingExpiresEpoch': runtime.durable.state['ledger']['interactiveAccounting']['originalExpiresEpoch'],
        'maxExecutionSeconds': 6000, 'commandQueueUrl': 'https://sqs.us-east-1.amazonaws.com/123456789012/commands.fifo',
        'replyQueueUrl': 'https://sqs.us-east-1.amazonaws.com/123456789012/replies.fifo',
        'mcpSecret': {'secretArn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:fixture-abcdef', 'versionId': 'a'*32},
        'image': '123456789012.dkr.ecr.us-east-1.amazonaws.com/fixture@sha256:'+'b'*64,
        'liability': {'basisDigest': BASIS, 'maximumInfrastructureUsd': '2', 'maximumCleanupUsd': '0.5'}}
    transport = {'schemaVersion': 2, 'mode': 'installed-slot', **{key: c[key] for key in ('candidateKey', 'runtimeId', 'slotId',
        'commandQueueUrl', 'replyQueueUrl', 'originalFundingExpiresEpoch', 'maxExecutionSeconds')}}
    ecs = SimpleNamespace(definition={'taskDefinitionArn': c['taskDefinitionArn'], 'taskRoleArn': c['taskRoleArn'], 'status': 'ACTIVE',
        'containerDefinitions': [{'image': c['image'], 'environment': [{'name': 'HONUA_AGENT_TRANSPORT', 'value': json.dumps(transport)},
            {'name': 'AWS_DEFAULT_REGION', 'value': c['region']}],
            'secrets': [{'name': 'HONUA_AGENT_MCP_CONFIG', 'valueFrom': c['mcpSecret']['secretArn']+':::'+c['mcpSecret']['versionId']}]}]},
        task={'taskArn': PHYSICAL_TASK, 'clusterArn': c['clusterArn'], 'taskDefinitionArn': c['taskDefinitionArn'],
            'launchType': 'FARGATE', 'lastStatus': 'RUNNING', 'tags': [
                {'key': 'honua-agent-'+key, 'value': value} for key, value in
                {'candidate': KEY, 'attempt': c['slotId'], 'runtime': c['runtimeId'], 'execution': E}.items()]})
    ecs.describe_task_definition = lambda **kwargs: {'taskDefinition': copy.deepcopy(ecs.definition)}
    ecs.describe_tasks = lambda **kwargs: {'tasks': [copy.deepcopy(ecs.task)], 'failures': []}
    clock = [runtime.now]
    installed = InstalledSlot(c, states=states, ecs=ecs, db=claims, clock=lambda: clock[0])
    observed = original(runtime)
    journal = ExecutionJournal(runtime.durable, observed)
    runtime.durable.update(runtime.durable.path, 'acquire-interactive-attempt', candidate_key=KEY,
        execution_id=E, target_id='target-original', workspace_id='workspace-original', turn_id='turn-original',
        owner_token='a'*32, expires_epoch=runtime.now+600)
    journal.append('admission', {'executionId': E, 'ownerToken': 'a'*32})
    reservations = {'infrastructure': 'original-e-infra', 'cleanup': 'original-e-cleanup'}
    for kind, rid in reservations.items():
        runtime.durable.update(runtime.durable.path, 'reserve-interactive', candidate_key=KEY, execution_id=E,
            target_id='target-original', workspace_id='workspace-original', turn_id='turn-original', owner_token='a'*32,
            reservation_id=rid, kind=kind, amount='3' if kind == 'infrastructure' else '1', basis_digest=BASIS)
    launch = installed.bind(observed, generation=1, expiry=runtime.now+600, native_binding_digest=BASIS, reservation_ids=reservations)
    states.execution = {'executionArn': installed.execution_arn, 'stateMachineArn': c['stateMachineArn'],
        'name': installed.name, 'input': json.dumps(installed.input), 'inputDetails': {'included': True},
        'status': 'RUNNING', 'redriveCount': 0}
    values = {'runtimeId': c['runtimeId'], 'slotId': c['slotId'], 'executionArn': installed.execution_arn,
        'taskDefinitionArn': c['taskDefinitionArn'], 'inputDigest': canonical_digest(installed.input)}
    claims.rows[installed.claim_key] = {k: {'S': v} for k, v in {**values, 'executionId': E}.items()}
    claims.rows[installed.slot_key] = {k: {'S': v} for k, v in {**values, 'activeExecutionId': E}.items()}
    return SimpleNamespace(worker=installed, states=states, claims=claims, ecs=ecs, journal=journal,
        launch=launch, clock=clock, config=c, durable=runtime.durable)


def test_installed_inspector_accepts_only_actual_terraform_encoding(slot):
    from test_agent_e2e_worker_lifecycle import emitted_definition
    from agent_e2e_worker_lifecycle import terraform_definition_digest
    raw = emitted_definition()
    slot.states.definition['definition'] = raw
    slot.worker.config['definitionDigest'] = terraform_definition_digest(raw)
    assert slot.worker.inspect()['lifecycle']['definition'] == raw
    slot.worker.config['definitionDigest'] = canonical_digest(json.loads(raw))
    with pytest.raises(PackError, match='reviewed source'):
        slot.worker.inspect()
    assert not any(operation == 'start' for operation, _ in slot.states.calls)


def test_lost_installed_start_ack_recovers_same_e_task_and_never_relaunches(slot):
    slot.states.lost_reply = True
    with pytest.raises(TimeoutError): slot.worker.start(slot.journal, slot.launch)
    replacement = InstalledSlot(slot.config, states=slot.states, ecs=slot.ecs, db=slot.claims, clock=lambda: slot.clock[0])
    replacement.bind({'value': {'candidateKey': KEY, 'execution': {'executionId': E}, 'binding': {
        'runtimeId': 'runtime-original', 'targetId': 'target-original', 'workspaceId': 'workspace-original',
        'turn': {'turnId': 'turn-original'}}}}, generation=1, expiry=slot.worker.input['expiresEpoch'],
        native_binding_digest=BASIS, reservation_ids=slot.launch['reservationIds'])
    with pytest.raises(PackError, match='already intended'): replacement.start(slot.journal, slot.launch)
    observed = replacement.observe(slot.journal)
    assert observed['taskStatus'] == 'RUNNING' and observed['retired'] is False
    assert [op for op, _ in slot.states.calls].count('start') == 1
    assert len(slot.durable.state['ledger']['interactivePaidAdmissions']) == 1


def test_actual_sdk_timestamp_shapes_and_launch_pages_survive_durable_recovery(slot):
    stamp = datetime(2026, 10, 23, 12, 34, 56, 123456, timezone.utc)
    slot.states.definition['creationDate'] = stamp
    slot.ecs.definition['registeredAt'] = stamp
    slot.states.execution['startDate'] = stamp
    slot.states.pages[0]['events'][0]['timestamp'] = stamp
    slot.ecs.task['createdAt'] = stamp
    inspected = slot.worker.inspect()
    assert inspected['lifecycle']['creationDate'] == stamp.isoformat()
    assert inspected['taskDefinition']['taskDefinition']['registeredAt'] == stamp.isoformat()
    observed = slot.worker.start(slot.journal, slot.launch)
    retained = slot.journal.values('worker-observation')[-1]
    pages = slot.journal.values('worker-history')
    assert observed['physicalTask']['createdAt'] == stamp.isoformat()
    assert retained['execution']['startDate'] == stamp.isoformat()
    assert retained['launchHistory'] == pages
    assert pages[0]['request']['executionArn'] == slot.worker.execution_arn
    assert pages[0]['response']['events'][0]['timestamp'] == stamp.isoformat()
    assert len(slot.journal.values('worker-start-response')) == 1
    json.dumps(slot.durable.state, allow_nan=False)


def test_invalid_launch_history_original_is_preserved_before_refusal(slot):
    slot.journal.append('worker-intent', slot.launch)
    details = slot.states.pages[0]['events'][0]['stateExitedEventDetails']
    details['outputDetails']['truncated'] = True
    with pytest.raises(PackError): slot.worker.observe(slot.journal)
    assert slot.journal.values('worker-history')[0]['response']['events'][0]['stateExitedEventDetails'] == details
    assert not slot.journal.values('worker-observation')
    assert slot.durable.state['ledger']['interactivePaidAdmissions'][0]['state'] == 'active'


@pytest.mark.parametrize('value', [datetime(2026, 10, 23), object(), {1, 2}])
def test_sdk_original_refuses_ambiguous_timestamp_or_unrecognized_objects(value):
    with pytest.raises(PackError): sdk_original({'original': value})


@pytest.mark.parametrize('failure', ['missing-history', 'truncated-history', 'wrong-input', 'wrong-e-claim',
    'wrong-slot-claim', 'wrong-role', 'wrong-region', 'extra-environment', 'extra-secret', 'missing-reservation'])
def test_installed_launch_and_recovery_refuse_missing_or_conflicting_originals(slot, failure):
    if failure == 'wrong-role': slot.ecs.definition['taskRoleArn'] += '-other'
    elif failure == 'wrong-region': slot.ecs.definition['containerDefinitions'][0]['environment'][1]['value'] = 'us-west-2'
    elif failure == 'extra-environment': slot.ecs.definition['containerDefinitions'][0]['environment'].append({'name': 'ROOT_KEY', 'value': 'not-allowed'})
    elif failure == 'extra-secret': slot.ecs.definition['containerDefinitions'][0]['secrets'].append({'name': 'ROOT_KEY', 'valueFrom': 'other'})
    elif failure == 'missing-reservation': slot.durable.state['ledger']['interactiveEntries'].clear()
    if failure in ('wrong-role', 'wrong-region', 'extra-environment', 'extra-secret', 'missing-reservation'):
        with pytest.raises(PackError): slot.worker.start(slot.journal, slot.launch)
        assert not any(op == 'start' for op, _ in slot.states.calls)
        return
    slot.journal.append('worker-intent', slot.launch)
    if failure == 'missing-history': slot.states.pages = [{'events': []}]
    elif failure == 'truncated-history': slot.states.pages[0]['events'][0]['stateExitedEventDetails']['outputDetails']['truncated'] = True
    elif failure == 'wrong-input': slot.states.execution['input'] = '{}'
    elif failure == 'wrong-e-claim': slot.claims.rows[slot.worker.claim_key]['executionId'] = {'S': 'execution-'+'f'*32}
    elif failure == 'wrong-slot-claim': slot.claims.rows[slot.worker.slot_key]['activeExecutionId'] = {'S': 'execution-'+'f'*32}
    if failure == 'missing-history':
        observed = slot.worker.observe(slot.journal)
        assert observed['taskArn'] is None and observed['retired'] is False
    else:
        with pytest.raises(PackError): slot.worker.observe(slot.journal)
    assert slot.durable.state['ledger']['interactivePaidAdmissions'][0]['state'] == 'active'


def test_expired_installed_worker_can_be_observed_without_release_or_new_spend(slot):
    slot.journal.append('worker-intent', slot.launch)
    slot.clock[0] = slot.config['originalFundingExpiresEpoch']+10
    slot.ecs.task['lastStatus'] = 'STOPPED'
    observed = slot.worker.observe(slot.journal)
    assert observed['taskStatus'] == 'STOPPED' and observed['retired'] is False
    assert slot.durable.state['ledger']['interactivePaidAdmissions'][0]['state'] == 'active'
    assert all(row['state'] == 'reserved' for row in slot.durable.state['ledger']['interactiveEntries'])


def test_fresh_controller_restores_expired_original_launch_for_observation_only(slot):
    slot.journal.append('worker-intent', slot.launch)
    slot.clock[0] = slot.config['originalFundingExpiresEpoch']+10
    slot.ecs.task['lastStatus'] = 'STOPPED'
    replacement = InstalledSlot(slot.config, states=slot.states, ecs=slot.ecs, db=slot.claims, clock=lambda: slot.clock[0])
    replacement.restore(slot.journal, copy.deepcopy(slot.launch))
    assert replacement.observe(slot.journal)['taskStatus'] == 'STOPPED'
    with pytest.raises(PackError): replacement.start(slot.journal, slot.launch)
    assert not any(op == 'start' for op, _ in slot.states.calls)
    assert slot.durable.state['ledger']['interactivePaidAdmissions'][0]['state'] == 'active'


@pytest.mark.parametrize('failure', ['missing-original', 'changed-input'])
def test_expired_restore_refuses_missing_or_changed_native_launch_identity(slot, failure):
    slot.journal.append('worker-intent', slot.launch)
    slot.clock[0] = slot.config['originalFundingExpiresEpoch']+10
    if failure == 'missing-original':
        slot.durable.state['nativeRuntimeHttp'].pop(slot.journal.record['nativeOriginal']['key'])
    else:
        # An altered retained launch still cannot replace its native original.
        slot.launch['input']['executionId'] = 'execution-'+'9'*32
        slot.launch['inputDigest'] = canonical_digest(slot.launch['input'])
        slot.journal.record['events'][-1]['value'] = copy.deepcopy(slot.launch)
    replacement = InstalledSlot(slot.config, states=slot.states, ecs=slot.ecs, db=slot.claims, clock=lambda: slot.clock[0])
    with pytest.raises(PackError): replacement.restore(slot.journal, slot.launch)
