"""Native resource transport fixtures; no approved profile or P16 qualification."""
import base64
import copy
import json

import pytest

import agent_e2e_mcp as mcp
from agent_e2e_controller import BedrockModel, ControllerError, ModelFailure, TOOLS, validate_action
from agent_e2e_controller import run_goal
from agent_e2e_mcp_backend import McpActions
from agent_e2e_mcp_runner import run
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_worker import validate_command
from test_agent_e2e_mcp import server, standard, result
from test_agent_e2e_mcp_backend import wiring, rig
from test_agent_e2e_mcp_worker import request
from test_agent_e2e_model_runtime import configuration
from test_agent_e2e_worker import CONFIG
from test_agent_e2e_controller import environment, Model

URI = 'honua://proposals/native-proposal-17'
POLICY = {'maxTurns': 12, 'maxActions': 8, 'allowedActions': [
    'mcp_list_tools', 'mcp_call_tool', 'ask_user', *mcp.RESOURCE_ACTIONS]}


def actor(server):
    return {'endpoint': server.url, 'credentialKind': 'api-key',
            'credential': 'private-original-agent', 'workflowView': None}


@pytest.mark.parametrize('name,method,field,identity', [
    ('mcp_list_resources', 'resources/list', 'resources', 'uri'),
    ('mcp_list_resource_templates', 'resources/templates/list', 'resourceTemplates', 'uriTemplate')])
def test_native_discovery_pages_and_metadata(server, name, method, field, identity):
    def respond(call):
        if call and call['method'] == method:
            second = bool(call['params'])
            assert call['params'] == ({'cursor': 'opaque+/='} if second else {})
            page = {field: [{'name': 'second' if second else 'first', identity:
                    'honua://proposals/'+('second' if second else '{proposalId}' if identity == 'uriTemplate' else 'first')}],
                    '_meta': {'observed': True}}
            if not second:
                page['nextCursor'] = 'opaque+/='
            return result(call, page)
        return standard(call)
    server.respond = respond
    observed = run({'name': name, 'input': {}}, actor(server), 30)
    assert observed['error'] is None
    assert [row['name'] for row in observed['result'][field]] == ['first', 'second']
    assert observed['result']['_meta'] == {'observed': True}
    assert observed['sessionCleanup'] == {'status': 'pass'}


@pytest.mark.parametrize('sse', [False, True])
def test_read_preserves_actual_pending_content_and_original_credential(server, sse):
    native = {'contents': [{'uri': URI, 'mimeType': 'application/json',
                           'text': '{"status":"PendingApproval","operationInstanceId":"native-23"}'}],
              '_meta': {'native': 'unchanged'}}
    def respond(call):
        if call and call['method'] == 'resources/read':
            assert call['params'] == {'uri': URI}
            status, headers, raw = result(call, native)
            return (status, {'Content-Type': 'text/event-stream'}, b'data: '+raw+b'\n\n') if sse else (status, headers, raw)
        return standard(call)
    server.respond = respond
    observed = run({'name': 'mcp_read_resource', 'input': {'uri': URI}}, actor(server), 30)
    assert observed['result'] == native and observed['error'] is None
    assert all(row[2]['X-Api-Key'] == 'private-original-agent' for row in server.seen)
    assert 'private-original-agent' not in json.dumps(observed)


@pytest.mark.parametrize('uri', ['https://example.com', 'file:///tmp/key', 'honua://user:pass@proposals/x',
    'honua://proposals/{id}', 'honua://proposals/x#fragment', 'honua://proposals/%0a', '', None])
def test_invalid_identifier_never_reaches_worker_or_http(server, uri):
    action = {'name': 'mcp_read_resource', 'input': {'uri': uri}}
    with pytest.raises(ModelFailure):
        validate_action({'type': 'tool_use', 'id': 'model-call', **action})
    with pytest.raises(PackError):
        validate_command({**request(), 'action': action}, CONFIG['attemptId'], CONFIG['expiresEpoch'], 1000)
    observed = run(action, actor(server), 30)
    assert observed['error']['category'] == 'invalid-resource-uri'
    assert not any(row[3] and row[3]['method'] == 'resources/read' for row in server.seen)


@pytest.mark.parametrize('mode', ['wrong-uri', 'both', 'invalid-blob', 'empty', 'partial', 'oversized'])
def test_read_rejects_incomplete_foreign_and_malformed_results(server, mode):
    value = {'contents': [{'uri': URI, 'text': 'pending'}]}
    if mode == 'wrong-uri': value['contents'][0]['uri'] += '-other'
    elif mode == 'both': value['contents'][0]['blob'] = 'eA=='
    elif mode == 'invalid-blob': value['contents'][0] = {'uri': URI, 'blob': '!'}
    elif mode == 'empty': value['contents'] = []
    elif mode == 'partial': value['nextCursor'] = 'next'
    else: value['contents'][0]['text'] = 'x'*(mcp.MAX_BODY+1)
    server.respond = lambda call: result(call, value) if call and call['method'] == 'resources/read' else standard(call)
    observed = run({'name': 'mcp_read_resource', 'input': {'uri': URI}}, actor(server), 30)
    assert observed['result'] is None and observed['error'] is not None
    assert len([row for row in server.seen if row[3] and row[3]['method'] == 'resources/read']) == 1


@pytest.mark.parametrize('mode', ['text', 'blob', 'percent-uri'])
@pytest.mark.parametrize('sse', [False, True])
def test_credential_echo_is_screened_before_raw_response_retention(server, mode, sse):
    secret = actor(server)['credential']
    content = {'uri': URI, 'text': secret}
    if mode == 'blob': content = {'uri': URI, 'blob': base64.b64encode(secret.encode()).decode()}
    elif mode == 'percent-uri': content = {'uri': URI, 'text': ''.join('%%%02x' % ord(c) for c in secret)}
    def respond(call):
        if call and call['method'] == 'resources/read':
            _, _, raw = result(call, {'contents': [content]})
            return (200, {'Content-Type': 'text/event-stream'}, b'data: '+raw+b'\n\n') if sse else (200, {}, raw)
        return standard(call)
    server.respond = respond
    observed = run({'name': 'mcp_read_resource', 'input': {'uri': URI}}, actor(server), 30)
    assert observed['error']['category'] == 'credential-disclosed-by-server'
    assert observed['result'] is None
    assert not any(row['rpcMethod'] == 'resources/read' for row in observed['observations'])


@pytest.mark.parametrize('mode', ['duplicate', 'repeated-cursor', 'metadata-change', 'page-limit'])
def test_discovery_rejects_bad_pagination_without_retry(server, mode):
    count = 0
    def respond(call):
        nonlocal count
        if call and call['method'] == 'resources/list':
            count += 1
            page = {'resources': [{'name': 'proposal', 'uri': URI if mode == 'duplicate' else URI+str(count)}],
                    'nextCursor': 'same' if mode == 'repeated-cursor' else str(count)}
            if mode == 'metadata-change': page['_meta'] = {'generation': count}
            return result(call, page)
        return standard(call)
    server.respond = respond
    observed = run({'name': 'mcp_list_resources', 'input': {}}, actor(server), 30)
    assert observed['error'] is not None and observed['result'] is None
    assert count == (32 if mode == 'page-limit' else 2)


def test_native_rpc_error_is_not_fabricated_completion(server):
    error = {'code': -32601, 'message': 'Method not found', 'data': {'native': True}}
    def respond(call):
        return (200, {}, mcp.encode({'jsonrpc': '2.0', 'id': call['id'], 'error': error})) if call and call['method'] == 'resources/read' else standard(call)
    server.respond = respond
    observed = run({'name': 'mcp_read_resource', 'input': {'uri': URI}}, actor(server), 30)
    assert observed['result'] is None
    assert observed['error'] == {'category': 'rpc-error', 'rpcError': error}


def test_default_model_tools_unchanged_and_only_reviewed_policy_advertises_resources():
    config = configuration()
    default = BedrockModel(config, client=object())
    assert default.request([])['tools'] == TOOLS
    assert not default.resource_actions
    policy = copy.deepcopy(POLICY)
    policy['allowedActions'] = ['mcp_read_resource']
    model = BedrockModel(config, client=object(), operation_policy=policy)
    assert [row['name'] for row in model.request([])['tools']] == [row['name'] for row in TOOLS]+['mcp_read_resource']
    assert policy == {**POLICY, 'allowedActions': ['mcp_read_resource']}
    with pytest.raises(ControllerError):
        BedrockModel(config, client=object(), operation_policy={**policy, 'allowedActions': ['mcp_unknown']})


def test_unadvertised_model_resource_call_cannot_reach_backend(environment):
    environment['model'] = Model([{'type': 'tool_use', 'id': 'selected', 'name': 'mcp_read_resource',
                                  'input': {'uri': URI}}])
    observed = run_goal(**environment)
    assert observed['reason'] == 'resource-action-not-admitted'
    assert not any(op == 'agent_action' for op, _ in environment['backend'].operations)


def resource_dispatch(wiring, name='mcp_read_resource'):
    wiring.action.update(name=name, input={'uri': URI} if name == 'mcp_read_resource' else {})
    digest = canonical_digest({k: wiring.action[k] for k in ('name', 'input')})
    # Keep the fixture's original intent intact; this is a distinct next action.
    wiring.journal.emit('action-intent', action=2, kind=name, requestDigest=digest)
    identity = {k: wiring.context[k] for k in ('campaignId', 'caseId', 'attempt', 'componentBindingDigest')}
    wiring.dispatch.update(ordinal=2, requestDigest=digest, intentDigest=wiring.journal.previous,
                           actionId=canonical_digest({**identity, 'ordinal': 2})[7:])


def router(wiring, policy):
    return McpActions(durable=wiring.rig.durable, lifecycle=wiring.rig.worker, launch=wiring.launch,
                      worker=wiring.worker, evidence_factory=wiring.factory, operation_policy=policy)


def test_missing_policy_refuses_before_any_worker_or_http(wiring, server):
    resource_dispatch(wiring)
    with pytest.raises(PackError, match='another action interface'):
        wiring.router.execute(wiring.context, wiring.action, wiring.dispatch)
    assert not wiring.queue.requests and not server.seen and not wiring.s3.puts


def test_real_worker_chunk_roundtrip_retains_pending_resource_and_refuses_replay(wiring, server):
    resource_dispatch(wiring)
    value = {'contents': [{'uri': URI, 'text': '{"status":"PendingApproval"}'+(' '*250000)}]}
    server.respond = lambda call: result(call, value) if call and call['method'] == 'resources/read' else standard(call)
    admitted = router(wiring, POLICY)
    observed = admitted.execute(wiring.context, wiring.action, wiring.dispatch)
    assert observed['agentResult']['content'] == value
    assert observed['agentResult']['isError'] is False and observed['operationCategory'] == 'read'
    retained = wiring.rig.store.state['workerActions'][wiring.dispatch['actionId']]
    assert len(retained['chunks']) > 1
    recovered = wiring.factory(wiring.dispatch['actionId']).recover()
    assert retained['replyDigest'] == canonical_digest(recovered['reply'])
    assert recovered['reply']['payload']['result'] == value
    with pytest.raises(PackError, match='do not redispatch'):
        admitted.execute(wiring.context, wiring.action, wiring.dispatch)
    assert len(wiring.queue.requests) == 1


@pytest.mark.parametrize('name,method,field,identity', [
    ('mcp_list_resources', 'resources/list', 'resources', 'uri'),
    ('mcp_list_resource_templates', 'resources/templates/list', 'resourceTemplates', 'uriTemplate')])
def test_discovery_worker_roundtrip_retains_native_catalog(wiring, server, name, method, field, identity):
    resource_dispatch(wiring, name)
    value = {field: [{'name': 'proposal', identity: URI}]}
    server.respond = lambda call: result(call, value) if call and call['method'] == method else standard(call)
    observed = router(wiring, POLICY).execute(wiring.context, wiring.action, wiring.dispatch)
    assert observed['agentResult']['content'] == value
    assert observed['operationCategory'] == 'discovery'
    assert observed['telemetry'][0]['operation'] == method
    recovered = wiring.factory(wiring.dispatch['actionId']).recover()
    assert recovered['reply']['payload']['result'] == value


def test_legacy_worker_refusal_stays_error_and_is_not_redispatched(wiring, server):
    resource_dispatch(wiring)
    # Compatibility fixture for a worker whose runner only knows the original
    # tools actions. It refuses instead of mapping the URI to a tool or HTTP URL.
    def legacy(call, seconds, workspace):
        assert call['name'] not in ('mcp_list_tools', 'mcp_call_tool')
        return {'status': 'completed', 'payload': {'result': None,
                'error': {'category': 'invalid-agent-mcp-action', 'rpcError': None},
                'observations': [], 'sessionCleanup': {'status': 'pass'}}}
    wiring.executor.execute_mcp = legacy
    admitted = router(wiring, POLICY)
    observed = admitted.execute(wiring.context, wiring.action, wiring.dispatch)
    assert observed['agentResult']['isError'] is True
    assert observed['agentResult']['content']['error']['category'] == 'invalid-agent-mcp-action'
    assert not server.seen
    assert wiring.dispatch['actionId'] in wiring.rig.store.state['workerActions']
    with pytest.raises(PackError, match='do not redispatch'):
        admitted.execute(wiring.context, wiring.action, wiring.dispatch)
    assert len(wiring.queue.requests) == 1


def test_changed_resource_after_durable_intent_cannot_reach_worker(wiring, server):
    resource_dispatch(wiring)
    wiring.action['input']['uri'] += '-other'
    with pytest.raises(PackError, match='retained controller intent'):
        router(wiring, POLICY).execute(wiring.context, wiring.action, wiring.dispatch)
    assert not wiring.queue.requests and not server.seen


def test_invalid_worker_resource_reply_is_retained_before_rejection(wiring, server):
    resource_dispatch(wiring)
    original = wiring.executor.execute_mcp
    server.respond = lambda call: result(call, {'contents': [{'uri': URI, 'text': 'native'}]}) if call and call['method'] == 'resources/read' else standard(call)
    def changed(call, seconds, workspace):
        observed = original(call, seconds, workspace)
        observed['payload']['result']['contents'][0]['uri'] += '-foreign'
        return observed
    wiring.executor.execute_mcp = changed
    admitted = router(wiring, POLICY)
    with pytest.raises(PackError, match='invalid worker resource result'):
        admitted.execute(wiring.context, wiring.action, wiring.dispatch)
    assert wiring.dispatch['actionId'] in wiring.rig.store.state['workerActions']
    recovered = wiring.factory(wiring.dispatch['actionId']).recover()
    assert recovered['reply']['payload']['result']['contents'][0]['uri'] == URI+'-foreign'
    with pytest.raises(PackError, match='do not redispatch'):
        admitted.execute(wiring.context, wiring.action, wiring.dispatch)
    assert len(wiring.queue.requests) == 1


def test_resource_catalog_count_bound(server):
    catalog = {'resources': [{'name': str(index), 'uri': URI+str(index)} for index in range(1025)]}
    server.respond = lambda call: result(call, catalog) if call and call['method'] == 'resources/list' else standard(call)
    observed = run({'name': 'mcp_list_resources', 'input': {}}, actor(server), 30)
    assert observed['error']['category'] == 'resource-catalog-too-large'
    assert observed['result'] is None


@pytest.mark.parametrize('kind', ['api-key', 'bearer', 'execution-capability'])
@pytest.mark.parametrize('mode', ['incomplete-data', 'incomplete-blob', 'property-name', 'comment-blob', 'comment-percent'])
def test_encoded_resource_secrets_in_keys_comments_and_tails_never_retained(server, kind, mode):
    private = 'synthetic-private-resource-screening-secret'
    config = actor(server)
    if kind == 'execution-capability':
        config['agentContext'] = {'workspaceId': 'workspace-17', 'turnId': 'turn-23'}
        config['executionContext'] = {'executionId': 'execution-'+'a'*32, 'claimGeneration': 1,
                                      'workerId': 'worker-17', 'capability': private}
    else:
        config.update(credentialKind=kind, credential=private)
    percent = ''.join('%%%02x' % ord(c) for c in private)
    encoded_blob = base64.b64encode(('prefix-'+private+'-suffix').encode()).decode()
    def respond(call):
        if call and call['method'] == 'resources/read':
            native = {'contents': [{'uri': URI, 'text': 'pending'}]}
            if mode == 'property-name':
                native['_meta'] = {percent: 'value'}
                return result(call, native)
            _, _, raw = result(call, native)
            body = b'data: '+raw+b'\n\n'
            if mode == 'incomplete-data': body += b'data: '+mcp.encode({'blob': encoded_blob})
            elif mode == 'incomplete-blob': body += b'data: {"blob":"'+encoded_blob.encode()
            elif mode == 'comment-blob': body += b': '+encoded_blob.encode()+b'\n\n'
            else: body += b': '+percent.encode()+b'\n\n'
            return 200, {'Content-Type': 'text/event-stream'}, body
        return standard(call)
    server.respond = respond
    observed = run({'name': 'mcp_read_resource', 'input': {'uri': URI}}, config, 30,
                   action_id='resource-action' if kind == 'execution-capability' else None)
    assert observed['error'] is not None
    assert observed['result'] is None
    assert not any(row['rpcMethod'] == 'resources/read' for row in observed['observations'])


def test_complete_sse_comments_and_blob_are_retained_byte_exact(server):
    native = {'contents': [{'uri': URI, 'blob': base64.b64encode(b'native pending content').decode()}]}
    bodies = []
    def respond(call):
        if call and call['method'] == 'resources/read':
            _, _, raw = result(call, native)
            body = b': harmless keepalive\n\ndata: '+raw+b'\n\n: harmless trailing comment\n\n'
            bodies.append(body)
            return 200, {'Content-Type': 'text/event-stream'}, body
        return standard(call)
    server.respond = respond
    observed = run({'name': 'mcp_read_resource', 'input': {'uri': URI}}, actor(server), 30)
    assert observed['error'] is None and observed['result'] == native
    rows = [row for row in observed['observations'] if row['rpcMethod'] == 'resources/read']
    assert len(rows) == 1 and base64.b64decode(rows[0]['bodyBase64']) == bodies[0]


def test_nonsecret_incomplete_sse_tail_is_refused_instead_of_rewritten(server):
    def respond(call):
        if call and call['method'] == 'resources/read':
            _, _, raw = result(call, {'contents': [{'uri': URI, 'text': 'native pending'}]})
            return 200, {'Content-Type': 'text/event-stream'}, b'data: '+raw+b'\n\ndata: {"blob":"bmF0'
        return standard(call)
    server.respond = respond
    observed = run({'name': 'mcp_read_resource', 'input': {'uri': URI}}, actor(server), 30)
    assert observed['error']['category'] == 'incomplete-resource-sse-tail'
    assert observed['result'] is None
    assert not any(row['rpcMethod'] == 'resources/read' for row in observed['observations'])
