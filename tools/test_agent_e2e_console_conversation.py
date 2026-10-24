"""Real local HTTP append/readback/crash windows; no live human qualification."""
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from unittest.mock import MagicMock, patch

import pytest

from agent_e2e_console_conversation import Conversation
from agent_e2e_pack import canonical_digest


@pytest.fixture
def conversation():
    messages, operations = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def answer(self, value):
            raw = json.dumps(value).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(raw)
        def do_GET(self):
            operations.append(('GET', self.path))
            self.answer({'messages': messages})
        def do_POST(self):
            operations.append(('POST', self.path))
            value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            message = {'id': value['messageId'], 'turnId': value['turnId'], 'role': 'assistant',
                'content': value['content'], 'evidenceKind': 'conversation'}
            messages.append(message)
            self.answer(message)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    class Http:
        lost = False
        crash = False
        def authorize(self):
            pass
        def json(self, step, method, path, data=None):
            import urllib.request
            request = urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+path,
                data=None if data is None else json.dumps(data).encode(), method=method,
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=2) as answer:
                result = {'observation': {'httpStatus': answer.status, 'body': json.load(answer)}}
            if self.lost and method == 'POST':
                raise TimeoutError('lost acknowledgement after actual persisted append')
            if self.crash and method == 'POST':
                raise SystemExit('controller interrupted after actual append')
            return result
    value = Conversation.__new__(Conversation)
    value.http = Http()
    value.context = {'campaignId': 'actual-local-contract', 'caseId': 'P14:A:aws-ecs-redis-on', 'attempt': 1,
        'campaignMode': 'development', 'componentBindingDigest': 'sha256:'+'a'*64}
    value.identity = copy.deepcopy(value.context)
    value.key = 'console-conversation:'+canonical_digest(value.identity)[7:]
    value.native = {'agentContext': {'workspaceId': 'original-workspace', 'turnId': 'original-human-turn'}}
    payload = {'message': {'id': 'original-model-response', 'stop_reason': 'end_turn', 'model': 'actual-pinned-model',
        'content': [{'type': 'text', 'text': 'Saved the requested map. 😀'}],
        'usage': {'input_tokens': 72, 'output_tokens': 12}}, 'providerRequestId': 'original-provider-request', 'turn': 2}
    key = canonical_digest({name: value.context[name] for name in ('campaignId', 'caseId', 'attempt')})
    value.durable = MagicMock(); value.durable.store.candidate = value.context['componentBindingDigest']
    value.durable.state = {'scenarioOperations': {}, 'journals': {key: [
        {'event': 'model', 'turn': 2, 'responseDigest': canonical_digest(payload['message']), 'costUsd': '0.01'},
        {'event': 'provider-request', 'turn': 2, 'requestId': payload['providerRequestId']}]}}
    yield value, payload, messages, operations
    server.shutdown(); server.server_close(); thread.join()


def test_original_text_is_appended_once_and_independently_visible_as_conversation(conversation):
    value, payload, messages, operations = conversation
    result = value.append(payload)
    assert result['status'] == 'delivered' and result['evidenceKind'] == 'conversation'
    assert messages[0]['content'] == payload['message']['content'][0]['text']
    assert [row[0] for row in operations] == ['GET', 'POST', 'GET']
    assert value.append(payload) == result
    assert [row[0] for row in operations].count('POST') == 1


def test_lost_native_post_reply_recovers_original_message_by_get_without_replay(conversation):
    value, payload, messages, operations = conversation
    value.http.lost = True
    assert value.append(payload)['status'] == 'delivered'
    assert len(messages) == 1
    value.http.lost = False
    assert value.append(payload)['messageId'] == messages[0]['id']
    assert [row[0] for row in operations] == ['GET', 'POST', 'GET', 'GET']


def test_controller_crash_after_append_recovers_original_identity_without_new_post(conversation):
    value, payload, messages, operations = conversation
    value.http.crash = True
    with pytest.raises(SystemExit):
        value.append(payload)
    value.http.crash = False
    assert value.append(payload)['messageId'] == messages[0]['id']
    assert [row[0] for row in operations] == ['GET', 'POST', 'GET']


def test_unknown_lost_append_outcome_never_creates_a_replacement_message(conversation):
    value, payload, messages, operations = conversation
    value.http.crash = True
    with pytest.raises(SystemExit):
        value.append(payload)
    messages.clear()  # An absent readback is unresolved, not permission to replay.
    value.http.crash = False
    with pytest.raises(ValueError, match='outcome unresolved'):
        value.append(payload)
    assert [row[0] for row in operations].count('POST') == 1


@pytest.mark.parametrize('change', ['body', 'provider', 'turn', 'tool', 'long', 'missing-model'])
def test_non_original_or_unbounded_response_cannot_write_conversation(conversation, change):
    value, payload, _, operations = conversation
    if change == 'body': payload['message']['content'][0]['text'] = 'synthetic success'
    if change == 'provider': payload['providerRequestId'] = 'replacement-request'
    if change == 'turn': payload['turn'] = 3
    if change == 'tool': payload['message']['content'] = [{'type': 'tool_use', 'id': 'call', 'name': 'fake', 'input': {}}]
    if change == 'long': payload['message']['content'][0]['text'] = '😀'*8001
    if change == 'missing-model': value.durable.state['journals'].clear()
    with pytest.raises(ValueError):
        value.append(payload)
    assert not operations


def test_conflicting_message_content_is_not_claimed_as_original_model_output(conversation):
    value, payload, messages, operations = conversation
    value.append(payload)
    messages[0]['content'] = 'unrelated saved response'
    with pytest.raises(ValueError, match='different content'):
        value.append(payload)
    assert [row[0] for row in operations].count('POST') == 1


@pytest.mark.parametrize('change', [None, 'secret-version', 'role', 'revision', 'completion', 'case'])
def test_delivery_admission_binds_actual_worker_definition_and_original_mcp_context(change):
    from test_worker_mcp_configuration import AdoptedWorkerMcp
    fixture = AdoptedWorkerMcp(); fixture.setUp()
    native = fixture.adopt()
    binding = fixture.provision()
    records = fixture.durable.state['scenarioOperations']
    key = next(key for key in records if key.startswith('native-worker-mcp:') and key.endswith('/completed'))
    config = {'nativeContext': fixture.reference, 'workerMcp': {'key': key, 'sha256': canonical_digest(records[key])}}
    role = 'arn:aws:iam::123456789012:role/honua-agent-fixture-worker'
    worker = {'attemptId': 'fixture', 'taskRoleArn': role, 'imageUri': 'image@sha256:'+'a'*64,
        'transport': {'actual': 'source-reviewed'}, 'lifecycle': {
            'clusterArn': 'arn:aws:ecs:us-east-1:123456789012:cluster/fixture', 'taskDefinitionArn': 'fixture:1'}}
    container = {'name': 'worker', 'image': worker['imageUri'], 'user': '10001:10001',
        'readonlyRootFilesystem': True, 'privileged': False, 'command': ['python', '-I', '/app/agent_e2e_container_entry.py'],
        'linuxParameters': {'initProcessEnabled': True, 'capabilities': {'drop': ['ALL']}},
        'environment': [{'name': 'HONUA_AGENT_TRANSPORT', 'value': json.dumps(worker['transport'])},
            {'name': 'AWS_DEFAULT_REGION', 'value': 'us-east-1'}],
        'secrets': [{'name': 'HONUA_AGENT_MCP_CONFIG', 'valueFrom': binding['secretArn']+':::'+binding['versionId']}]}
    definition = {'taskDefinitionArn': worker['lifecycle']['taskDefinitionArn'], 'taskRoleArn': role,
        'executionRoleArn': role.replace('-worker', '-execution'), 'containerDefinitions': [container]}
    ecs = MagicMock(); ecs.describe_task_definition.return_value = {'taskDefinition': definition}
    case = {'http': {'endpoint': fixture.cfg['endpoint'].removesuffix('/mcp')}, 'attempts': {'1': worker}}
    context = {'campaignId': 'original-campaign', 'caseId': 'P14:A:aws-ecs-redis-on', 'attempt': 1,
        'campaignMode': 'development', 'componentBindingDigest': fixture.candidate}
    if change == 'secret-version': container['secrets'][0]['valueFrom'] += 'different'
    if change == 'role': definition['taskRoleArn'] = role.replace('fixture', 'other')
    if change == 'revision': definition['taskDefinitionArn'] = 'fixture:2'
    if change == 'completion': del records[key]
    if change == 'case': context['caseId'] = 'P14:B:aws-ecs-redis-on'
    with patch('hosted_pilot.worker_mcp_configuration.verify_injected_credential'), \
            patch('hosted_pilot.console_turn.observe_session', return_value=native['consoleTurn']['sessionObservation']):
        def admit():
            return Conversation(durable=fixture.durable, context=context, configuration=config,
                case=case, ecs=ecs, http_factory=lambda **kwargs: MagicMock())
        if change:
            with pytest.raises(ValueError): admit()
        else:
            admitted = admit()
            assert admitted.native['agentContext'] == native['agentContext']
            assert any(key.startswith('console-turn-claim:') for key in records)
