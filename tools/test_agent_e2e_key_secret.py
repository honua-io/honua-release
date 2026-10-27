"""Real loopback secret consumption; no credentials or model campaigns."""
import copy
import json
import pickle
from types import SimpleNamespace

import pytest

from agent_e2e_key_secret import OperationKeyConsumer
from agent_e2e_pack import PackError
from agent_e2e_scenario_backend import ScenarioHttp
from test_agent_e2e_setup_http import server

SECRET = 'issued-private-key-'+'z'*40
PRINCIPAL = {'actorId': 'api-key:task', 'studioOwnerId': 'owner', 'tenantId': None,
             'authenticationKind': 'api-key', 'isHumanSession': False}


@pytest.fixture
def consumer(server, monkeypatch):
    monkeypatch.setenv('P16_ORIGINAL_TOKEN', 'original-task-token')
    durable = SimpleNamespace(store=SimpleNamespace(candidate='sha256:'+'a'*64), state={'scenarioOperations': {}})
    context = {'componentBindingDigest': durable.store.candidate, 'campaignId': 'development', 'caseId': 'P16:A:aws-ecs-redis-on', 'attempt': 1}
    http = ScenarioHttp(durable=durable, context=context, endpoint=server.url,
                        credential_env='P16_ORIGINAL_TOKEN', attempt_id='p16-task',
                        agent_context={'workspaceId': 'workspace', 'turnId': 'turn'})
    http.authorize = lambda: None
    http.client.authorize = http.authorize
    durable._persist = lambda: None
    state = SimpleNamespace(change=None, durable=durable, http=http)
    def respond(method, path, raw):
        if path.endswith('/agent/target'):
            principal = copy.deepcopy(PRINCIPAL)
            if state.change == 'principal': principal['actorId'] = 'other'
            body = {'principal': principal}
        elif path.endswith('/handles/issuance'):
            body = {'operationInstanceId': 'issuance', 'operationId': 'admin.api-key.create',
                    'status': 'Completed', 'result': {'secretReferences': [{'name': 'key', 'referenceId': 'reference'}]}}
            if state.change == 'handle': body['operationId'] = 'admin.api-key.rotate'
            if state.change == 'approval': body['status'] = 'RequiresApproval'
            if state.change == 'reference': body['result']['secretReferences'][0]['referenceId'] = 'other'
        elif path.endswith('/consume'):
            intents = [v for v in durable.state['scenarioOperations'].values()
                       if v.get('producer') == 'p16-private-key-consume']
            assert len(intents) == 1 and intents[0]['method'] == 'POST'
            assert method == 'POST' and raw == b''
            if state.change == 'lost': return None
            if state.change == 'denied': return 403, {}, SECRET.encode()
            if state.change == 'redirect': return 302, {'Location': server.url+'/probe'}, b''
            if state.change == 'duplicate-json': return 200, {}, b'{"success":true,"success":true}'
            if state.change == 'oversized': return 200, {}, b'z'*16385
            body = {'name': 'key', 'value': SECRET}
            return 200, {}, json.dumps({'success': True, 'data': body, 'timestamp': '2026-10-26T00:00:00Z'}).encode()
        else:
            assert path == '/probe'
            assert server.seen[-1][2]['X-Api-Key'] == SECRET
            if state.change == 'echo': body = {'escaped': SECRET}
            else: body = {'features': []}
        return 200, {}, json.dumps({'success': True, 'data': body}).encode()
    server.respond = respond
    state.instance = OperationKeyConsumer(http, principal=PRINCIPAL)
    return state


def test_exact_secret_is_memory_only_and_reused_for_screened_probe(consumer, server):
    key = consumer.instance.consume('issuance', 'reference')
    assert repr(key) == '<IssuedCredential private>'
    assert key.probe(consumer.http, 'read', 'GET', '/probe')['observation']['httpStatus'] == 200
    raw = json.dumps(consumer.durable.state)
    assert SECRET not in raw and 'original-task-token' not in raw
    assert 'consumed' in raw
    with pytest.raises(PackError, match='serialized'): pickle.dumps(key)
    with pytest.raises(PackError, match='already attempted'): consumer.instance.consume('issuance', 'reference')
    key.close()
    with pytest.raises(PackError, match='no longer'): key.probe(consumer.http, 'after-close', 'GET', '/probe')


@pytest.mark.parametrize('change', ['principal', 'handle', 'reference', 'approval'])
def test_wrong_native_binding_cannot_consume(consumer, server, change):
    consumer.change = change
    with pytest.raises(PackError): consumer.instance.consume('issuance', 'reference')
    assert not any(row[1].endswith('/consume') for row in server.seen)


@pytest.mark.parametrize('change', ['lost', 'denied', 'duplicate-json', 'oversized', 'redirect'])
def test_unknown_consume_never_retried_even_with_new_consumer(consumer, server, change):
    consumer.change = change
    with pytest.raises(PackError, match='outcome unknown') as error:
        consumer.instance.consume('issuance', 'reference')
    assert SECRET not in str(error.value)
    before = len(server.seen)
    with pytest.raises(PackError, match='already attempted'):
        OperationKeyConsumer(consumer.http, principal=PRINCIPAL).consume('issuance', 'reference')
    assert len(server.seen) == before
    assert SECRET not in json.dumps(consumer.durable.state)


def test_issued_secret_echo_is_rejected_before_retention(consumer):
    key = consumer.instance.consume('issuance', 'reference')
    consumer.change = 'echo'
    with pytest.raises((PackError, ValueError)): key.probe(consumer.http, 'echo', 'GET', '/probe')
    assert SECRET not in json.dumps(consumer.durable.state)


def test_intent_retention_failure_prevents_consume(consumer, server):
    original = consumer.http.retain
    def retain(kind, step, record):
        if record.get('producer') == 'p16-private-key-consume': raise PackError('retention failed')
        return original(kind, step, record)
    consumer.http.retain = retain
    with pytest.raises(PackError): consumer.instance.consume('issuance', 'reference')
    assert not any(row[1].endswith('/consume') for row in server.seen)


def test_changed_transport_alias_cannot_repeat_unknown_consume(consumer, server):
    consumer.change = 'lost'
    with pytest.raises(PackError): consumer.instance.consume('issuance', 'reference')
    before = len(server.seen)
    http = ScenarioHttp(durable=consumer.durable, context=consumer.http.context, endpoint=server.url,
                        credential_env='P16_ORIGINAL_TOKEN', attempt_id='different-requester-role',
                        agent_context={'workspaceId': 'workspace', 'turnId': 'turn'})
    with pytest.raises(PackError, match='already attempted'):
        OperationKeyConsumer(http, principal=PRINCIPAL).consume('issuance', 'reference')
    assert len(server.seen) == before


def test_secret_previously_disclosed_in_native_handle_cannot_go_unnoticed(consumer):
    consumer.durable.state['scenarioOperations']['earlier-record'] = {'text': SECRET}
    with pytest.raises(PackError, match='outcome unknown'):
        consumer.instance.consume('issuance', 'reference')
    assert not any(record.get('outcome') == 'consumed' for record in consumer.durable.state['scenarioOperations'].values())


def test_lost_lease_after_intent_prevents_consume(consumer, server):
    def authorize():
        if any(record.get('producer') == 'p16-private-key-consume' for record in consumer.durable.state['scenarioOperations'].values()):
            raise PackError('lease lost')
    consumer.http.authorize = authorize
    with pytest.raises(PackError): consumer.instance.consume('issuance', 'reference')
    assert not any(row[1].endswith('/consume') for row in server.seen)


def test_response_retention_failure_has_no_second_consume(consumer, server):
    original = consumer.http.retain
    def retain(kind, step, record):
        answer = original(kind, step, record)
        if record.get('outcome') == 'consumed': raise TimeoutError('private exception')
        return answer
    consumer.http.retain = retain
    with pytest.raises(PackError, match='outcome unknown'): consumer.instance.consume('issuance', 'reference')
    before = len(server.seen)
    with pytest.raises(PackError, match='already attempted'): consumer.instance.consume('issuance', 'reference')
    assert len(server.seen) == before
    assert SECRET not in json.dumps(consumer.durable.state)


def test_invalid_intent_acknowledgement_prevents_consume(consumer, server):
    original = consumer.http.retain
    def retain(kind, step, record):
        answer = original(kind, step, record)
        if record.get('producer') == 'p16-private-key-consume': answer['sha256'] = 'sha256:'+'0'*64
        return answer
    consumer.http.retain = retain
    with pytest.raises(PackError, match='acknowledgement'): consumer.instance.consume('issuance', 'reference')
    assert not any(row[1].endswith('/consume') for row in server.seen)
