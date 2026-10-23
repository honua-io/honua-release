"""Setup transport failure boundaries; fixture servers never qualify scenarios."""
import copy
import json
import socket
import threading
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_setup_http import SetupHttp

IDENTITY = {'candidateKey': 'sha256:'+'a'*64, 'attemptId': 'setup-1'}


@pytest.fixture
def server():
    state = SimpleNamespace(seen=[], respond=lambda *args: (200, {}, b'{"success":true}'))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def handle_request(self):
            raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            state.seen.append((self.command, self.path, dict(self.headers), raw))
            reply = state.respond(self.command, self.path, raw)
            if reply is None:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            code, headers, body = reply
            self.send_response(code)
            self.send_header('Content-Length', str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        do_GET = do_POST = do_PUT = do_DELETE = handle_request
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    state.url = f'http://127.0.0.1:{httpd.server_port}'
    yield state
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.fixture
def wiring(server, monkeypatch):
    monkeypatch.setenv('SETUP_TEST_TOKEN', 'private-setup-token')
    records = {}
    def retain(kind, step, value):
        key = (kind, step)
        if key in records:
            raise PackError('existing setup intent; no replay')
        records[key] = copy.deepcopy(value)
        return {'key': '/'.join(key), 'sha256': canonical_digest(value)}
    def client(**kwargs):
        return SetupHttp(server.url, 'SETUP_TEST_TOKEN', identity=IDENTITY,
                         authorize=kwargs.pop('authorize', lambda: None), retain=kwargs.pop('retain', retain), **kwargs)
    return SimpleNamespace(records=records, retain=retain, client=client)


def test_upload_sends_source_bytes_without_overwrite_and_retains_before_http(wiring, server, monkeypatch):
    monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
    def respond(method, path, body):
        assert ('intent', 'upload') in wiring.records
        assert method == 'POST' and path == '/api/v1/admin/import/upload'
        return 200, {}, b'{ "success": true }'
    server.respond = respond
    raw = b'{"type":"FeatureCollection","features":[]}'
    reply = wiring.client().upload('upload', raw, table='ae_points', schema='public', srid=3857)
    _, _, headers, body = server.seen[0]
    parsed = BytesParser(policy=default).parsebytes(('Content-Type: '+headers['Content-Type']+'\r\n\r\n').encode()+body)
    parts = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True)
             for part in parsed.iter_parts()}
    assert parts == {'file':raw, 'TableName':b'ae_points', 'TargetSchema':b'public', 'SourceSrid':b'3857',
                     'TargetSrid':b'3857', 'OverwriteExisting':b'false'}
    assert reply['observation']['httpStatus'] == 200
    assert reply['observation']['body'] == {'success': True}
    assert wiring.records['response','upload']['intentDigest'] == canonical_digest(wiring.records['intent','upload'])
    assert 'private-setup-token' not in json.dumps(list(wiring.records.values()))
    assert 'requestBodyDigest' in wiring.records['intent','upload']


@pytest.mark.parametrize('when', ['before-write', 'after-write', 'lost-response-retention', 'lost-http-reply'])
def test_ambiguous_mutation_is_not_resent(wiring, server, when):
    def retain(kind, step, value):
        if kind == 'intent' and when == 'before-write':
            raise TimeoutError()
        reference = wiring.retain(kind, step, value)
        if kind == 'intent' and when == 'after-write' or kind == 'response' and when == 'lost-response-retention':
            raise TimeoutError()
        return reference
    if when == 'lost-http-reply':
        server.respond = lambda *args: None
    client = wiring.client(retain=retain)
    with pytest.raises((TimeoutError, PackError)):
        client.json('publish', 'POST', '/api/v1/admin/connections/c/layers', {'table':'owned'})
    count = len(server.seen)
    with pytest.raises(PackError, match='already attempted'):
        client.json('publish', 'POST', '/api/v1/admin/connections/c/layers', {'table':'owned'})
    assert len(server.seen) == count
    if when != 'before-write':
        with pytest.raises(PackError, match='existing setup intent'):
            wiring.client().json('publish', 'POST', '/api/v1/admin/connections/c/layers', {'table':'owned'})
        assert len(server.seen) == count


def test_lost_authority_after_retention_blocks_request(wiring, server):
    def authorize():
        if wiring.records:
            raise PackError('lease lost')
    with pytest.raises(PackError, match='lease lost'):
        wiring.client(authorize=authorize).json('publish','POST','/publish',{})
    assert not server.seen and ('intent','publish') in wiring.records


def test_initial_authorization_denial_prevents_retention_and_http(wiring, server):
    allowed = [False]
    def authorize():
        if not allowed[0]:
            raise PackError('lease denied')
    client = wiring.client(authorize=authorize)
    with pytest.raises(PackError, match='lease denied'):
        client.json('publish', 'POST', '/publish', {})
    assert not wiring.records and not server.seen
    allowed[0] = True
    client.json('publish', 'POST', '/publish', {})
    assert len(server.seen) == 1


def test_expired_after_retention_cannot_send(wiring, server):
    now = [0]
    def retain(*args):
        result = wiring.retain(*args)
        now[0] = 301
        return result
    with pytest.raises(PackError, match='deadline'):
        wiring.client(retain=retain, clock=lambda: now[0]).json('publish','POST','/publish',{})
    assert not server.seen


@pytest.mark.parametrize('raw', [b'{"token":"private-setup-token"}', b'{"token":"private-setup-\\u0074oken"}',
                               b'{"token":"private-setup-%74oken"}',
                               b'{"x":1,"x":2}', b'{"x":NaN}'])
def test_unsafe_or_ambiguous_response_is_not_retained(wiring, server, raw):
    server.respond = lambda *args: (200, {}, raw)
    with pytest.raises(PackError):
        wiring.client().json('read','GET','/read')
    assert ('response','read') not in wiring.records and ('intent','read') in wiring.records


def test_additional_private_values_are_excluded(wiring, server):
    server.respond = lambda *args: (200, {}, b'{"echo":"private-database-password"}')
    with pytest.raises(PackError):
        wiring.client(private_values=['private-database-password']).json('read','GET','/read')
    assert ('response','read') not in wiring.records


def test_protocol_failure_is_retained_without_claiming_success(wiring, server):
    server.respond = lambda *args: (404, {}, b'{"success":false,"message":"Layer not found"}')
    reply = wiring.client().json('read','GET','/read')
    assert reply['observation']['httpStatus'] == 404
    assert reply['observation']['body']['success'] is False
    assert ('response','read') in wiring.records


def test_redirect_never_forwards_setup_identity(wiring, server):
    server.respond = lambda *args: (302, {'Location':server.url+'/foreign'}, b'{}')
    with pytest.raises(PackError, match='redirect'):
        wiring.client().json('read','GET','/read')
    assert len(server.seen) == 1


@pytest.mark.parametrize('endpoint', ['http://remote.invalid', 'https://user:password@example.test',
                                    'https://example.test?token=x', 'https://example.test/base',
                                    'https://example.test?', 'https://example.test#'])
def test_bad_endpoint_rejected_before_authorization_or_retention(wiring, endpoint):
    with pytest.raises(PackError):
        SetupHttp(endpoint,'SETUP_TEST_TOKEN',identity=IDENTITY,authorize=lambda:None,retain=wiring.retain)
    assert not wiring.records


@pytest.mark.parametrize('path', ['/read?token=private-setup-token', '/read?token=private-setup-%74oken'])
def test_request_url_cannot_disclose_credentials_to_retention(wiring, server, path):
    with pytest.raises(PackError, match='credential'):
        wiring.client().json('read', 'GET', path)
    assert not wiring.records and not server.seen


@pytest.mark.parametrize('kind', ['intent', 'response'])
@pytest.mark.parametrize('ack', [{'key': 'retained', 'sha256': 'sha256:'+'0'*64}, {'ok': True}])
def test_retention_acknowledgement_must_bind_exact_content(wiring, server, kind, ack):
    def retain(actual_kind, step, value):
        reference = wiring.retain(actual_kind, step, value)
        return ack if actual_kind == kind else reference
    with pytest.raises(PackError, match='retention acknowledgement'):
        wiring.client(retain=retain).json('read', 'GET', '/read')
    assert len(server.seen) == (0 if kind == 'intent' else 1)


@pytest.mark.parametrize('kind', ['intent', 'response'])
def test_retention_cannot_change_the_acknowledged_content(wiring, server, kind):
    def retain(actual_kind, step, value):
        if actual_kind == kind:
            value['step'] = 'different-step'
        return wiring.retain(actual_kind, step, value)
    with pytest.raises(PackError, match='retention acknowledgement'):
        wiring.client(retain=retain).json('read', 'GET', '/read')
    assert len(server.seen) == (0 if kind == 'intent' else 1)


def test_streaming_response_rechecks_budget_before_reading_more(wiring, monkeypatch):
    now = [0]
    client = wiring.client(clock=lambda: now[0])
    class Response:
        code = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read1(self, size):
            assert size > 0
            now[0] = 301
            return b'{'
    monkeypatch.setattr(client.opener, 'open', lambda *args, **kwargs: Response())
    with pytest.raises(PackError, match='deadline'):
        client.json('read', 'GET', '/read')
    assert ('response', 'read') not in wiring.records


def test_response_byte_limit_preserves_intent_without_success(wiring, server, monkeypatch):
    import agent_e2e_setup_http
    monkeypatch.setattr(agent_e2e_setup_http, 'MAX_RESPONSE_BYTES', 8)
    server.respond = lambda *args: (200, {}, b'{"long":1234}')
    with pytest.raises(PackError, match='bound'):
        wiring.client().json('read', 'GET', '/read')
    assert ('intent', 'read') in wiring.records and ('response', 'read') not in wiring.records


@pytest.mark.parametrize('token', ['quoted"token', 'back\\slash-token'])
def test_json_special_credentials_cannot_hide_in_retention_keys(wiring, server, monkeypatch, token):
    monkeypatch.setenv('SETUP_TEST_TOKEN', token)
    def retain(kind, step, value):
        return {'key':'retained/'+token, 'sha256':canonical_digest(value)}
    with pytest.raises(PackError, match='credential'):
        wiring.client(retain=retain).json('read', 'GET', '/read')
    assert not server.seen


@pytest.mark.parametrize('field', ['endpoint', 'step', 'identity'])
def test_all_intent_metadata_is_checked_before_retention(wiring, monkeypatch, field):
    client = wiring.client()
    step = 'read'
    if field == 'endpoint': client.endpoint = 'https://private-setup-token.example'
    elif field == 'step': step = 'private-setup-token'
    else: client.identity['attemptId'] = 'private-setup-token'
    monkeypatch.setattr(client.opener, 'open', lambda *args, **kwargs: pytest.fail('HTTP must not be attempted'))
    with pytest.raises(PackError, match='credential'):
        client.json(step, 'GET', '/read')
    assert not wiring.records


@pytest.mark.parametrize('path', ['/api/v1/admin/import/upload', '/API/v1/admin/import/upload/',
                                 '/api/v1/admin/import/%75pload', '/api/v1/admin/./import/upload'])
def test_json_import_cannot_bypass_create_only_upload(wiring, server, path):
    with pytest.raises(PackError, match='guarded upload'):
        wiring.client().json('upload', 'POST', path, {'OverwriteExisting': True})
    assert not wiring.records and not server.seen


@pytest.mark.parametrize('path', ['/read\x7f', '/r\u00e9ad'])
def test_unencodable_request_path_is_rejected_before_retention(wiring, server, path):
    with pytest.raises(PackError, match='invalid setup request'):
        wiring.client().json('read', 'GET', path)
    assert not wiring.records and not server.seen


def test_bearer_credential_is_header_only(wiring, server):
    wiring.client(credential_kind='bearer').json('read', 'GET', '/read')
    headers = server.seen[0][2]
    assert headers['Authorization'] == 'Bearer private-setup-token'
    assert 'X-Api-Key' not in headers
    assert 'private-setup-token' not in json.dumps(list(wiring.records.values()))


def test_api_key_credential_is_header_only(wiring, server):
    wiring.client().json('read', 'GET', '/read')
    headers = server.seen[0][2]
    assert headers['X-Api-Key'] == 'private-setup-token'
    assert 'Authorization' not in headers
    assert 'private-setup-token' not in json.dumps(list(wiring.records.values()))


def anonymous_client(wiring, server):
    return SetupHttp(server.url, None, credential_kind='anonymous', identity=IDENTITY,
                     authorize=lambda: None, retain=wiring.retain, private_values=['private-setup-token'])


def test_actual_anonymous_http_sends_no_identity_and_retains_native_denial(wiring, server):
    server.respond = lambda *_: (401, {}, b'{"status":401}')
    reply = anonymous_client(wiring, server).json('public-probe', 'GET', '/api/v1/studio/published/owned')
    assert reply['observation']['httpStatus'] == 401
    headers = {key.lower(): value for key, value in server.seen[0][2].items()}
    assert not ({'authorization', 'x-api-key', 'cookie'} & set(headers))
    assert wiring.records['intent', 'public-probe']['credentialKind'] == 'anonymous'


@pytest.mark.parametrize('method', ['POST', 'PUT'])
def test_anonymous_transport_cannot_mutate_before_intent_or_http(wiring, server, method):
    with pytest.raises(PackError, match='only GET'):
        anonymous_client(wiring, server).json('mutation', method, '/api/v1/operations/anything/submit', {})
    assert not wiring.records and not server.seen


def test_anonymous_transport_cannot_accept_a_credential_reference(wiring, server):
    with pytest.raises(PackError, match='credential reference'):
        wiring.client(credential_kind='anonymous')
    assert not wiring.records and not server.seen


@pytest.mark.parametrize('raw', [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"secret":"private-setup-token"}'])
def test_anonymous_response_keeps_strict_json_and_private_canary_checks(wiring, server, raw):
    server.respond = lambda *_: (200, {}, raw)
    with pytest.raises(PackError):
        anonymous_client(wiring, server).json('public-probe', 'GET', '/read')
    assert ('response', 'public-probe') not in wiring.records


def test_task_read_binds_native_workspace_turn_in_before_request_custody(wiring, server):
    context = {'workspaceId': 'native-workspace-17', 'turnId': 'native-turn-29'}
    def respond(*_):
        assert wiring.records['intent', 'task-version']['agentContext'] == context
        return 200, {}, b'{"data":{"saved":true}}'
    server.respond = respond
    reply = wiring.client(agent_context=context).json('task-version', 'GET', '/api/v1/studio/content-items/exact/versions/exact')
    assert reply['observation']['httpStatus'] == 200
    headers = {key.lower(): value for key, value in server.seen[0][2].items()}
    assert headers['x-honua-workspace'] == context['workspaceId'] and headers['x-honua-turn'] == context['turnId']
    assert 'private-setup-token' not in json.dumps(list(wiring.records.values()))


@pytest.mark.parametrize('context', [{'workspaceId': 'native', 'turnId': 'turn\r\nInjected:true'},
    {'workspaceId': 'native'}, {'workspaceId': 'native', 'turnId': 'turn', 'Authorization': 'other'}])
def test_task_context_cannot_inject_headers_or_omit_native_turn(wiring, server, context):
    with pytest.raises(ValueError):
        wiring.client(agent_context=context)
    assert not wiring.records and not server.seen
