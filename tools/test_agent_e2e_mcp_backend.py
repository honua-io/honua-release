"""Controller/worker/HTTP integration with fixture services, not qualification."""
import copy
import json
from types import SimpleNamespace

import pytest

from agent_e2e_container import Executor
from agent_e2e_mcp_backend import McpActions, public_result, telemetry
from agent_e2e_mcp_runner import run
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_state import DurableExecution, preserve_history
from agent_e2e_telemetry import Journal
from agent_e2e_worker import Worker
from agent_e2e_worker_evidence import ReplyEvidence
from agent_e2e_worker_lifecycle import WorkerLifecycle
from test_agent_e2e_mcp import server, standard, result
from test_agent_e2e_mcp_worker import Exchange
from test_agent_e2e_worker import CONFIG as TRANSPORT
from test_agent_e2e_worker_evidence import S3
from test_agent_e2e_worker_lifecycle import rig, CONFIG, launch
from test_agent_e2e_controller import environment, Model


@pytest.fixture
def wiring(rig, server, tmp_path):
    server.respond = lambda request: (result(request, {'tools': []}) if request and request.get('method') == 'tools/list' else standard(request))
    transport = {**TRANSPORT, 'expiresEpoch': CONFIG['expiresEpoch']}
    config = {**CONFIG, 'transportDigest': canonical_digest(transport)}
    rig.worker = WorkerLifecycle(config, states=rig.states, ecs=rig.ecs, db=rig.db, clock=lambda: 1000)
    launched = launch(rig)
    context = {**CONFIG['context'], 'componentBindingDigest': CONFIG['candidateKey'], 'knowledge': {}}
    journal = Journal(tmp_path/'journals', context, sink=rig.durable.event)
    journal.emit('start', modelId='fixture', returnedModelId='fixture')
    journal.emit('context', campaignMode=context['campaignMode'], knowledge=context['knowledge'], conceptIds=[], workflowIds=[])
    action = {'type': 'tool_use', 'id': 'model-chosen-id', 'name': 'mcp_list_tools', 'input': {}}
    def emit_intent(call):
        digest = canonical_digest({k: call[k] for k in ('name', 'input')})
        journal.emit('action-intent', action=1, kind=call['name'], requestDigest=digest)
        return {'ordinal': 1, 'requestDigest': digest, 'intentDigest': journal.previous,
                'actionId': canonical_digest({**{k: context[k] for k in ('campaignId', 'caseId', 'attempt', 'componentBindingDigest')}, 'ordinal': 1})[7:]}
    dispatch = emit_intent(action)
    actor = {'endpoint': server.url, 'credentialKind': 'bearer', 'credential': 'actor-token-private', 'workflowView': None}
    executor = Executor(transport, tmp_path/'worker.sqlite', tmp_path, clock=lambda: 1000,
                        execute_mcp=lambda call, seconds, _: {'status': 'completed', 'payload': run(call, actor, seconds)})
    queue = Exchange(executor)
    s3 = S3()
    evidence_factory = lambda action_id: ReplyEvidence(s3, 'controller-evidence', CONFIG['candidateKey'], 'attempt-1', action_id)
    worker = Worker(queue, transport, clock=lambda: 1000)
    router = McpActions(durable=rig.durable, lifecycle=rig.worker, launch=launched, worker=worker, evidence_factory=evidence_factory)
    value = SimpleNamespace(rig=rig, context=context, journal=journal, action=action, dispatch=dispatch,
                            executor=executor, queue=queue, s3=s3, factory=evidence_factory, worker=worker, router=router, launch=launched)
    yield value
    journal.close()
    executor.db.close()


def execute(wiring):
    return wiring.router.execute(wiring.context, wiring.action, wiring.dispatch)


@pytest.mark.parametrize('name', ['', ' \t\n', 'x'*257, 'x'*256])
def test_public_catalog_rejects_uncallable_worker_names(name):
    catalog = {'tools': [{'name': name, 'inputSchema': {}}]}
    reply = {'status': 'completed', 'payload': {'result': catalog, 'error': None,
             'observations': [], 'sessionCleanup': {'status': 'pass'}}}
    action = {'name': 'mcp_list_tools', 'input': {}}
    if name.strip() and len(name) <= 256:
        assert public_result(reply, action)['content'] == catalog
    else:
        with pytest.raises(PackError, match='invalid or duplicate worker tool descriptor'):
            public_result(reply, action)


def test_actual_transport_retains_reply_before_public_catalog_and_emits_correlation(wiring, server):
    tools = [{'name': 'find', 'inputSchema': {'type': 'object'}}]
    def respond(request):
        if request and request.get('method') == 'tools/list':
            status, headers, body = result(request, {'tools': tools, '_meta': {'view': 'setup'}})
            return status, {**headers, 'X-Request-Id': 'request-1', 'traceparent': '00-'+'a'*32+'-'+'b'*16+'-01'}, body
        return standard(request)
    server.respond = respond
    observed = execute(wiring)
    assert observed['agentResult']['content'] == {'tools': tools, '_meta': {'view': 'setup'}}
    assert observed['agentResult']['isError'] is False
    assert [e['event'] for e in observed['telemetry']] == ['discovery', 'runtime-trace']
    retained = wiring.rig.store.state['workerActions'][wiring.dispatch['actionId']]
    assert retained['launchDigest'] == canonical_digest(wiring.launch)
    assert retained['intentDigest'] == wiring.dispatch['intentDigest']
    assert retained['chunks'] and retained['dispatch']['version']
    assert retained['evidenceBucket'] == 'controller-evidence'
    recovered = wiring.factory(wiring.dispatch['actionId']).recover()
    assert retained['replyDigest'] == canonical_digest(recovered['reply'])
    assert 'observations' not in json.dumps(observed['agentResult'])
    assert 'actor-token-private' not in json.dumps(observed)
    assert len(wiring.queue.requests) == 1
    assert wiring.queue.requests[0]['actionId'] != wiring.action['id']


@pytest.mark.parametrize('field,value', [('ordinal', 2), ('requestDigest', 'sha256:'+'f'*64),
    ('intentDigest', 'sha256:'+'f'*64), ('actionId', 'f'*64)])
def test_changed_dispatch_cannot_reach_worker(wiring, field, value):
    wiring.dispatch[field] = value
    with pytest.raises(PackError):
        execute(wiring)
    assert not wiring.queue.requests and not wiring.s3.puts


@pytest.mark.parametrize('field,value', [('campaignId', 'other'), ('caseId', 'P11:A:aws-ecs-redis-on'),
    ('attempt', 2), ('campaignMode', 'qualification'), ('componentBindingDigest', 'sha256:'+'f'*64), ('knowledge', {'other': 'bundle'})])
def test_foreign_context_cannot_reach_worker(wiring, field, value):
    wiring.context[field] = value
    with pytest.raises(PackError):
        execute(wiring)
    assert not wiring.queue.requests


def test_model_cannot_change_arguments_after_intent(wiring):
    wiring.action.update(name='mcp_call_tool', input={'name': 'publish', 'arguments': {}})
    with pytest.raises(PackError):
        execute(wiring)
    assert not wiring.queue.requests


def test_completed_attempt_cannot_dispatch(wiring):
    wiring.journal.emit('end', status='blocked', failureAttribution='undetermined', reason='interrupted', cleanupStatus='blocked')
    with pytest.raises(PackError):
        execute(wiring)
    assert not wiring.queue.requests


@pytest.mark.parametrize('status', ['STOPPED', 'PENDING'])
def test_nonrunning_worker_cannot_receive_action(wiring, status):
    wiring.rig.ecs.task['lastStatus'] = status
    with pytest.raises(PackError, match='running'):
        execute(wiring)
    assert not wiring.queue.requests


def test_transport_change_rejected(wiring):
    wiring.worker.config['commandQueueUrl'] = TRANSPORT['commandQueueUrl'].replace('commands', 'foreign')
    with pytest.raises(PackError, match='transport'):
        McpActions(durable=wiring.rig.durable, lifecycle=wiring.rig.worker, launch=wiring.launch,
                   worker=wiring.worker, evidence_factory=wiring.factory)


def test_transport_mutation_after_construction_blocks_send(wiring):
    wiring.worker.config['replyQueueUrl'] = TRANSPORT['replyQueueUrl'].replace('replies', 'foreign')
    with pytest.raises(PackError, match='transport'):
        execute(wiring)
    assert not wiring.queue.requests


def test_settled_reservation_prevents_more_actions(wiring):
    wiring.rig.durable.update(wiring.rig.path, 'settle', candidate_key=CONFIG['candidateKey'], reservation_id='infra-1', amount='1')
    with pytest.raises(PackError, match='prepaid'):
        execute(wiring)
    assert not wiring.queue.requests


def test_dispatch_retention_failure_does_not_send(wiring):
    wiring.s3.after_write = lambda _: (_ for _ in ()).throw(TimeoutError('lost retention acknowledgement'))
    with pytest.raises(TimeoutError):
        execute(wiring)
    assert not wiring.queue.requests


def test_lease_lost_after_dispatch_retention_does_not_send(wiring):
    def lose_lease(_):
        wiring.rig.durable.heartbeat_error = 'lost'
    wiring.s3.after_write = lose_lease
    with pytest.raises(PackError, match='lease'):
        execute(wiring)
    assert not wiring.queue.requests
    wiring.rig.durable.heartbeat_error = None


@pytest.mark.parametrize('committed', [False, True])
def test_lost_inventory_ack_blocks_public_result_and_new_dispatch(wiring, tmp_path, committed):
    commit = wiring.rig.store.commit
    def fail(payload):
        if committed:
            commit(payload)
        if payload.get('workerActions'):
            raise TimeoutError('inventory acknowledgement lost')
    wiring.rig.store.commit = fail
    with pytest.raises(TimeoutError):
        execute(wiring)
    assert (wiring.dispatch['actionId'] in wiring.rig.store.state.get('workerActions', {})) == committed
    wiring.rig.durable.close()
    wiring.rig.store.commit = commit
    recovered = DurableExecution(wiring.rig.store, tmp_path/'recovered-ledger.json')
    try:
        fresh = McpActions(durable=recovered, lifecycle=wiring.rig.worker, launch=wiring.launch,
                           worker=Worker(wiring.queue, wiring.worker.config, clock=lambda: 1000), evidence_factory=wiring.factory)
        with pytest.raises(PackError, match='retained evidence|dispatch already retained'):
            fresh.execute(wiring.context, wiring.action, wiring.dispatch)
        assert len(wiring.queue.requests) == 1
    finally:
        recovered.close()


def test_session_cleanup_failure_keeps_evidence_and_blocks_public_result(wiring, server):
    def respond(request):
        if request is None:
            return None
        return standard(request)
    server.respond = respond
    with pytest.raises(PackError, match='session cleanup'):
        execute(wiring)
    assert wiring.dispatch['actionId'] in wiring.rig.store.state['workerActions']


@pytest.mark.parametrize('mutation', ['erase', 'rewrite'])
def test_action_evidence_cannot_be_rewritten(wiring, mutation):
    execute(wiring)
    before = copy.deepcopy(wiring.rig.store.state)
    after = copy.deepcopy(before)
    if mutation == 'erase':
        after.pop('workerActions')
    else:
        after['workerActions'][wiring.dispatch['actionId']]['replyDigest'] = 'sha256:'+'f'*64
    with pytest.raises(PackError, match='action evidence'):
        preserve_history(before, after)


@pytest.mark.parametrize('category,expected', [('http-403', 'authorization'), ('http-429', 'rate-limit'),
    ('transport-outcome-unknown', 'other'), ('rpc-error', 'other')])
def test_public_protocol_error_does_not_invent_success_or_diagnosis(category, expected):
    reply = {'status': 'completed', 'payload': {'result': None, 'error': {'category': category, 'rpcError': None},
                                              'observations': [], 'sessionCleanup': {'status': 'pass'}}}
    public = public_result(reply, {'name': 'mcp_call_tool'})
    assert public['isError'] is True and public['errorCategory'] == expected
    assert 'failureAttribution' not in public


def test_tool_result_preserves_server_error_and_structured_content():
    result = {'content': [{'type': 'text', 'text': 'permission denied'}], 'structuredContent': {'code': 'denied'}, 'isError': True}
    reply = {'status': 'completed', 'payload': {'result': result, 'error': None,
                                              'observations': [], 'sessionCleanup': {'status': 'pass'}}}
    assert public_result(reply, {'name': 'mcp_call_tool'}) == {'content': result, 'isError': True, 'errorCategory': 'other'}


def test_invalid_trace_header_is_retained_only_as_raw_observation():
    reply = {'payload': {'result': {'content': []}, 'error': None,
                         'observations': [{'rpcMethod': 'tools/call', 'headers': {'traceparent': '00-'+'0'*32+'-'+'1'*16+'-01',
                                                                                 'X-Request-Id': 'request-1'}}]}}
    assert telemetry(reply, {'name': 'mcp_call_tool'}, 1) == []


def test_model_loop_routes_selected_mcp_action_through_real_transport_and_retention(environment, rig, server, tmp_path):
    """Actual loop/transport, fixture model/setup/verifier/AWS; never a live receipt."""
    from agent_e2e_controller import run_goal
    server.respond = lambda request: (result(request, {'tools': []}) if request and request.get('method') == 'tools/list' else standard(request))
    env = environment
    env['case_id'] = 'P10:A:aws-ecs-redis-on'
    env['ledger'] = rig.path
    context = {'campaignId': env['campaign']['campaignId'], 'caseId': env['case_id'], 'attempt': 1,
               'campaignMode': env['campaign']['campaignMode']}
    transport = {**TRANSPORT, 'expiresEpoch': CONFIG['expiresEpoch']}
    config = {**CONFIG, 'context': context, 'transportDigest': canonical_digest(transport)}
    lifecycle = WorkerLifecycle(config, states=rig.states, ecs=rig.ecs, db=rig.db, clock=lambda: 1000)
    actor = {'endpoint': server.url, 'credentialKind': 'bearer', 'credential': 'actor-token-private', 'workflowView': None}
    executor = Executor(transport, tmp_path/'loop-worker.sqlite', tmp_path, clock=lambda: 1000,
                        execute_mcp=lambda call, seconds, _: {'status': 'completed', 'payload': run(call, actor, seconds)})
    queue, s3 = Exchange(executor), S3()
    adapter = env['backend']
    original = adapter.call
    adapter.preflight = lambda context: {'target': 'aws-ecs-redis-on', 'maximumInfrastructureUsd': '2',
                                         'maximumCleanupUsd': '1', 'liabilityBasisDigest': CONFIG['liability']['basisDigest']}
    handlers = []
    def backend_call(operation, payload, **kwargs):
        if operation == 'prepare':
            prepared = original(operation, payload, **kwargs)
            rows = [row for row in rig.durable.state['ledger']['entries'] if row['campaignId'] == context['campaignId']]
            launched = lifecycle.start(rig.durable, reservation_ids={row['kind']: row['id'] for row in rows})
            handlers.append(McpActions(durable=rig.durable, lifecycle=lifecycle, launch=launched,
                worker=Worker(queue, transport, clock=lambda: 1000),
                evidence_factory=lambda action_id: ReplyEvidence(s3, 'controller-evidence', CONFIG['candidateKey'], 'attempt-1', action_id)))
            return prepared
        if operation == 'agent_action':
            return handlers[0].execute(payload, payload['action'], payload['dispatch'])
        return original(operation, payload, **kwargs)
    adapter.call = backend_call
    class SelectedActionModel(Model):
        def invoke(self, body):
            self.content = ([{'type': 'tool_use', 'id': 'selected', 'name': 'mcp_list_tools', 'input': {}}]
                            if len(self.inputs) == 1 else None)
            return super().invoke(body)
    env['model'] = SelectedActionModel()
    try:
        observed = run_goal(**env, budget_update=rig.durable.update, journal_sink=rig.durable.event)
        assert observed['status'] == 'pass' and observed['qualification'] is False
        assert observed['usage']['executedActions'] == 1
        assert len(queue.requests) == len(rig.store.state['workerActions']) == 1
        raw_public = json.dumps(env['model'].inputs[1][-1])
        assert 'tools' in raw_public
        assert 'bodyBase64' not in raw_public and 'actor-token-private' not in raw_public
        rows = next(iter(rig.store.state['journals'].values()))
        assert [row['event'] for row in rows if row['event'] in ('action-intent', 'discovery', 'action')] == ['action-intent', 'discovery', 'action']
    finally:
        executor.db.close()
