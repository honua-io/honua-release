"""Actual HTTP transport tests with fixture servers, never live qualification."""
import base64
import hashlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import agent_e2e_mcp as mcp


def result(request, value):
    return 200, {}, mcp.encode({'jsonrpc':'2.0', 'id':request['id'], 'result':value})


def standard(request):
    if request is None:
        return 204, {}, b''
    if request['method'] == 'initialize':
        status, headers, raw = result(request, {'protocolVersion':mcp.VERSION, 'capabilities':{'tools':{}},
                                               'serverInfo':{'name':'fixture', 'version':'1'}})
        return status, {**headers, 'Mcp-Session-Id':'fixture-session'}, raw
    if request['method'] == 'notifications/initialized':
        return 202, {}, b''
    return result(request, {'content':[{'type':'text', 'text':'observed'}], 'isError':False})


@pytest.fixture
def server():
    class State:
        respond = staticmethod(standard)
        seen = []
        errors = []
    state = State()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.handle_request()

        def do_DELETE(self):
            self.handle_request()

        def handle_request(self):
            try:
                raw = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                request = json.loads(raw) if raw else None
                state.seen.append((self.command, self.path, dict(self.headers), request))
                response = state.respond(request)
                if response is None:  # Commit observed, then lose the reply.
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                status, headers, body = response
                self.send_response(status)
                self.send_header('Content-Type', headers.get('Content-Type', 'application/json'))
                length = len(body[0])+len(body[1]) if isinstance(body, tuple) else len(body)
                self.send_header('Content-Length', str(length))
                for key, value in headers.items():
                    if key != 'Content-Type':
                        self.send_header(key, value)
                self.end_headers()
                if isinstance(body, tuple):
                    first, second, acknowledgement = body
                    self.wfile.write(first)
                    self.wfile.flush()
                    assert acknowledgement.wait(timeout=5), 'Client did not answer the ping while reading SSE'
                    self.wfile.write(second)
                else:
                    self.wfile.write(body)
            except (ConnectionResetError, BrokenPipeError):
                pass  # Client intentionally stops at its bounded response limit.
            except Exception as exc:
                state.errors.append(exc)

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    state.url = f'http://127.0.0.1:{httpd.server_port}/mcp'
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not state.errors


@pytest.mark.parametrize('kind,header,prefix', [('bearer','Authorization','Bearer '), ('api-key','X-Api-Key','')])
def test_session_catalog_call_and_observation_binding(server, monkeypatch, kind, header, prefix):
    monkeypatch.setenv('TEST_MCP_TOKEN', 'only-the-agent-identity')
    # Environment proxies must not receive credentials or alter the pinned origin.
    monkeypatch.setenv('http_proxy', 'http://127.0.0.1:1')
    first = {'name':'find', 'description':'observed schema', 'inputSchema':{'type':'object'}}
    second = {'name':'publish', 'inputSchema':{'type':'object'}, 'annotations':{'readOnlyHint':False}}
    def respond(request):
        if request and request.get('method') == 'tools/list':
            return result(request, {'tools':[first], 'nextCursor':'opaque+/='} if not request['params']
                          else {'tools':[second]})
        status, headers, raw = standard(request)
        return status, {**headers, 'X-Request-Id':'request-7', 'traceparent':'00-observed'}, raw
    server.respond = respond
    client = mcp.Client(server.url, credential_env='TEST_MCP_TOKEN', credential_kind=kind)
    assert client.initialize()['serverInfo']['name'] == 'fixture'
    assert client.list_tools() == {'tools':[first, second]}
    arguments = {'title':'Selected by model', 'layers':[{'id':'runtime-17'}]}
    assert client.call_tool('publish', arguments)['isError'] is False
    client.close()
    client.close()
    assert len(server.seen) == 6
    assert server.seen[3][3]['params'] == {'cursor':'opaque+/='}
    assert server.seen[4][3]['params'] == {'name':'publish', 'arguments':arguments}
    assert server.seen[0][3]['params']['capabilities'] == {}
    for index, (method, path, headers, request) in enumerate(server.seen):
        assert headers[header] == prefix+'only-the-agent-identity'
        assert headers['Accept'] == 'application/json, text/event-stream'
        assert path == '/mcp'
        assert headers.get('Mcp-Session-Id') == ('fixture-session' if index else None)
        observation = client.observations[index]
        assert observation['method'] == method
        assert observation['requestDigest'] == 'sha256:'+hashlib.sha256(mcp.encode(request) if request else b'').hexdigest()
        raw = base64.b64decode(observation['bodyBase64'])
        assert observation['bodyDigest'] == 'sha256:'+hashlib.sha256(raw).hexdigest()
    assert 'only-the-agent-identity' not in json.dumps(client.observations)
    assert 'fixture-session' not in json.dumps(client.observations)
    assert client.observations[-1]['headers']['X-Request-Id'] == 'request-7'


def test_sse_progress_and_tool_error_preserved(server):
    expected = {'content':[{'type':'text','text':'Permission denied'}], 'isError':True,
                'structuredContent':{'code':'forbidden'}}
    def respond(request):
        if request and request['method'] == 'tools/call':
            body = (b': keepalive\r\n\r\ndata: '+mcp.encode({'jsonrpc':'2.0', 'method':'notifications/progress',
                    'params':{'progress':1}})+b'\r\n\r\ndata: '+result(request, expected)[2]+b'\r\n\r\n')
            return 200, {'Content-Type':'text/event-stream'}, body
        return standard(request)
    server.respond = respond
    client = mcp.Client(server.url)
    client.initialize()
    assert client.call_tool('write', {}) == expected
    assert b'notifications/progress' in base64.b64decode(client.observations[-1]['bodyBase64'])
    client.close()


@pytest.mark.parametrize('failure', ['lost-reply', 'http-503', 'session-404', 'redirect'])
def test_ambiguous_write_never_retried_or_reinitialized(server, failure):
    committed = []
    def respond(request):
        if request and request['method'] == 'tools/call':
            committed.append(request['params'])
            if failure == 'lost-reply':
                return None
            return {'http-503':503, 'session-404':404, 'redirect':307}[failure], {'Location':server.url+'/elsewhere'}, b''
        return standard(request)
    server.respond = respond
    client = mcp.Client(server.url)
    client.initialize()
    with pytest.raises(mcp.McpError):
        client.call_tool('publish', {'map':'new-map'})
    assert committed == [{'name':'publish', 'arguments':{'map':'new-map'}}]
    assert [entry[3]['method'] for entry in server.seen] == ['initialize','notifications/initialized','tools/call']
    client.close()


@pytest.mark.parametrize('location', ['body', 'escaped-body', 'header', 'sse-notification', 'sse-partial', 'sse-comment'])
def test_credential_echo_never_enters_observations_or_errors(server, monkeypatch, location):
    secret = 'private-credential-canary'
    monkeypatch.setenv('TEST_MCP_TOKEN', secret)
    def respond(request):
        if request and request['method'] == 'tools/call':
            headers = {}
            raw = result(request, {'content':[{'type':'text','text':secret}]})[2]
            if location == 'escaped-body':
                raw = raw.replace(secret.encode(), b'\\u0070rivate-credential-canary')
            elif location == 'header':
                raw = standard(request)[2]
                headers['X-Request-Id'] = secret
            elif location == 'sse-notification':
                headers['Content-Type'] = 'text/event-stream'
                raw = (b'data: '+mcp.encode({'jsonrpc':'2.0','method':'notifications/message','params':{'text':secret}})
                       +b'\n\ndata: '+standard(request)[2]+b'\n\n')
            elif location in ('sse-partial','sse-comment'):
                headers['Content-Type'] = 'text/event-stream'
                echo = b'\\u0070rivate-credential-canary'
                suffix = b'data: {"partial":"'+echo if location == 'sse-partial' else b': '+echo+b'\n\n'
                raw = b'data: '+standard(request)[2]+b'\n\n'+suffix
            return 200, headers, raw
        return standard(request)
    server.respond = respond
    client = mcp.Client(server.url, credential_env='TEST_MCP_TOKEN')
    client.initialize()
    with pytest.raises(mcp.McpError, match='credential-disclosed-by-server') as error:
        client.call_tool('echo', {})
    assert error.value.public_error is None and secret not in str(error.value)
    assert len(client.observations) == 2
    assert secret not in json.dumps(client.observations)
    client.close()


@pytest.mark.parametrize('mutation,category', [
    (lambda r: r.update(id='another-request'), 'unbound-rpc-response'),
    (lambda r: r.update(error={'code':-1,'message':'error'}), 'unbound-rpc-response'),
    (lambda r: r.update(jsonrpc='1.0'), 'unbound-rpc-response'),
    (lambda r: r['result'].update(content='incorrect'), 'invalid-tool-result'),
    (lambda r: r['result'].update(isError='false'), 'invalid-tool-result'),
])
def test_malformed_tool_response_cannot_be_success(server, mutation, category):
    def respond(request):
        status, headers, raw = standard(request)
        if request and request['method'] == 'tools/call':
            value = json.loads(raw)
            mutation(value)
            raw = mcp.encode(value)
        return status, headers, raw
    server.respond = respond
    client = mcp.Client(server.url)
    client.initialize()
    with pytest.raises(mcp.McpError, match=category):
        client.call_tool('write', {})
    client.close()


@pytest.mark.parametrize('failure', ['duplicate-tool','repeated-cursor','page-limit'])
def test_discovery_fails_instead_of_silently_truncating_catalog(server, failure):
    pages = []
    def respond(request):
        if request and request['method'] == 'tools/list':
            pages.append(request['params'])
            name = 'same' if failure == 'duplicate-tool' else 'tool-'+str(len(pages))
            cursor = 'same' if failure == 'repeated-cursor' else str(len(pages))
            return result(request, {'tools':[{'name':name,'inputSchema':{}}], 'nextCursor':cursor})
        return standard(request)
    server.respond = respond
    client = mcp.Client(server.url)
    client.initialize()
    with pytest.raises(mcp.McpError, match={'duplicate-tool':'duplicate-tool','repeated-cursor':'repeated-cursor',
                                          'page-limit':'page-limit'}[failure]):
        client.list_tools()
    assert len(pages) == (32 if failure == 'page-limit' else 2)
    client.close()


@pytest.mark.parametrize('event,category', [
    ({'jsonrpc':'2.0','id':'server-request','method':'sampling/createMessage'}, 'unnegotiated-server-request'),
    ({'jsonrpc':'2.0','id':'foreign','result':{}}, 'foreign-rpc-response'),
])
def test_sse_does_not_grant_server_requested_authority(server, event, category):
    def respond(request):
        if request and request['method'] == 'tools/call':
            return 200, {'Content-Type':'text/event-stream'}, b'data: '+mcp.encode(event)+b'\n\n'
        return standard(request)
    server.respond = respond
    client = mcp.Client(server.url)
    client.initialize()
    with pytest.raises(mcp.McpError, match=category):
        client.call_tool('write', {})
    assert len(server.seen) == 3
    client.close()


@pytest.mark.parametrize('bound', ['body','total','request','deadline'])
def test_bounds_fail_before_further_dispatch(server, monkeypatch, bound):
    now = [0]
    client = mcp.Client(server.url, clock=lambda: now[0], timeout=10)
    client.initialize()
    if bound == 'total':
        monkeypatch.setattr(mcp, 'MAX_TOTAL', client.total_bytes+1)
    elif bound == 'body':
        def respond(request):
            return result(request, {'content':[{'type':'text','text':'x'*mcp.MAX_BODY}]})
        server.respond = respond
    elif bound == 'deadline':
        now[0] = 11
    with pytest.raises(mcp.McpError, match='too-large|deadline'):
        client.call_tool('write', {'large':'x'*mcp.MAX_BODY} if bound == 'request' else {})
    assert len(server.seen) == (2 if bound in ('request','deadline') else 3)
    # These failures exhausted the response/deadline bound; close must not hide it.
    if bound in ('total','deadline'):
        with pytest.raises(mcp.McpError):
            client.close()
    else:
        server.respond = standard
        client.close()


def test_rpc_error_retained_without_becoming_tool_success(server):
    def respond(request):
        if request and request['method'] == 'tools/call':
            return 200, {}, mcp.encode({'jsonrpc':'2.0','id':request['id'],
                'error':{'code':-32602,'message':'Invalid arguments','data':{'field':'layerId'}}})
        return standard(request)
    server.respond = respond
    client = mcp.Client(server.url)
    client.initialize()
    with pytest.raises(mcp.McpError, match='rpc-error') as error:
        client.call_tool('query', {})
    assert error.value.public_error['code'] == -32602
    client.close()


@pytest.mark.parametrize('endpoint', ['http://remote.example/mcp', 'https://user:pass@example.test/mcp',
    'https://example.test/mcp?token=secret', 'https://example.test:99999/mcp',
    'https://example.test/mcp\r\nInjected: header', None])
def test_invalid_endpoints_rejected(endpoint):
    with pytest.raises(mcp.McpError):
        mcp.Client(endpoint)


def test_missing_credential_prevents_handshake(server, monkeypatch):
    monkeypatch.delenv('MISSING_MCP_CREDENTIAL', raising=False)
    with pytest.raises(mcp.McpError, match='credential'):
        mcp.Client(server.url, credential_env='MISSING_MCP_CREDENTIAL')
    assert not server.seen


@pytest.mark.parametrize('raw', [b'{"x":1,"x":2}', b'{"x":NaN}', b'\xff', b'{'])
def test_strict_json(raw):
    with pytest.raises(mcp.McpError):
        mcp.parse(raw)


def test_session_cleanup_failure_is_reported_and_client_closed(server):
    server.respond = lambda request: (503, {}, b'') if request is None else standard(request)
    client = mcp.Client(server.url)
    client.initialize()
    with pytest.raises(mcp.McpError, match='termination-failed'):
        client.close()
    with pytest.raises(mcp.McpError, match='not-initialized'):
        client.call_tool('write', {})
    assert client.closed


@pytest.mark.parametrize('change', ['version','session','notification'])
def test_invalid_handshake_prevents_tool_dispatch(server, change):
    def respond(request):
        status, headers, raw = standard(request)
        if request['method'] == 'initialize':
            if change == 'version':
                value = json.loads(raw)
                value['result']['protocolVersion'] = 'unsupported'
                raw = mcp.encode(value)
            elif change == 'session':
                headers['Mcp-Session-Id'] = 'contains whitespace'
        elif change == 'notification':
            status = 200
        return status, headers, raw
    server.respond = respond
    client = mcp.Client(server.url)
    with pytest.raises(mcp.McpError):
        client.initialize()
    with pytest.raises(mcp.McpError, match='not-initialized'):
        client.call_tool('write', {})
    assert not any(entry[3]['method'] == 'tools/call' for entry in server.seen)


def test_stateless_session_has_no_invented_delete(server):
    def respond(request):
        status, headers, raw = standard(request)
        headers.pop('Mcp-Session-Id', None)
        return status, headers, raw
    server.respond = respond
    client = mcp.Client(server.url)
    client.initialize()
    client.call_tool('query', {})
    client.close()
    assert len(server.seen) == 3
    assert all('Mcp-Session-Id' not in entry[2] for entry in server.seen)


def test_sse_duplicate_and_incomplete_response_are_not_success():
    raw = b'data: '+mcp.encode({'jsonrpc':'2.0','id':'expected','result':{}})
    with pytest.raises(mcp.McpError, match='duplicate-rpc-response'):
        mcp.Client._sse((raw+b'\n\n')*2, 'expected')
    with pytest.raises(mcp.McpError, match='missing-rpc-response'):
        mcp.Client._sse(raw, 'expected')


@pytest.mark.parametrize('stateless', [False, True])
def test_explicit_setup_view_retains_metadata_and_full_override_is_per_request(server, stateless):
    metadata = {'view':'setup', 'revision':'setup.v1', 'revisionDigest':'sha256:'+'a'*64,
                'membershipDigest':'sha256:'+'b'*64, 'descriptorDigest':'sha256:'+'c'*64,
                'toolCount':2, 'descriptorBytes':100, 'estimatedTokens':25, 'stages':[{'id':'publish'}]}
    def respond(request):
        if request and request['method'] == 'tools/list':
            params = request['params']
            if params['view'] == 'full':
                return result(request, {'tools':[{'name':'expert','inputSchema':{}}]})
            page = {'tools':[{'name':'second' if 'cursor' in params else 'first', 'inputSchema':{}}], '_meta':metadata}
            if 'cursor' not in params:
                page['nextCursor'] = 'opaque-setup'
            return result(request, page)
        status, headers, raw = standard(request)
        if stateless:
            headers.pop('Mcp-Session-Id', None)
        return status, headers, raw
    server.respond = respond
    client = mcp.Client(server.url, workflow_view='setup')
    client.initialize()
    assert server.seen[0][3]['params']['_meta'] == {'honua.io/workflow-view':'setup'}
    catalog = client.list_tools()
    assert catalog['_meta'] == metadata
    assert [tool['name'] for tool in catalog['tools']] == ['first','second']
    assert server.seen[3][3]['params'] == {'view':'setup','cursor':'opaque-setup'}
    assert client.list_tools(view='full')['tools'][0]['name'] == 'expert'
    assert client.list_tools() == catalog
    client.close()


@pytest.mark.parametrize('failure', ['ignored-view','missing-metadata','changed-revision','wrong-count'])
def test_named_view_cannot_silently_fall_back_or_mix_revisions(server, failure):
    def respond(request):
        if request and request['method'] == 'tools/list':
            second = 'cursor' in request['params']
            metadata = {'view':'default' if failure == 'ignored-view' else 'setup', 'revision':'one',
                        'toolCount':3 if failure == 'wrong-count' else 2}
            if second and failure == 'changed-revision':
                metadata['revision'] = 'two'
            page = {'tools':[{'name':str(second),'inputSchema':{}}], '_meta':metadata}
            if failure == 'missing-metadata':
                page.pop('_meta')
            if not second:
                page['nextCursor'] = 'second'
            return result(request, page)
        return standard(request)
    server.respond = respond
    client = mcp.Client(server.url, workflow_view='setup')
    client.initialize()
    with pytest.raises(mcp.McpError, match='workflow-view-not-observed|metadata-changed|tool-count-mismatch'):
        client.list_tools()
    client.close()


def test_server_ping_answered_once_without_granting_capabilities(server):
    ping_responses = []
    acknowledged = threading.Event()
    def respond(request):
        if request and 'result' in request:
            ping_responses.append(request)
            acknowledged.set()
            return 202, {}, b''
        if request and request['method'] == 'tools/call':
            ping = {'jsonrpc':'2.0','id':'server-ping','method':'ping'}
            return 200, {'Content-Type':'text/event-stream'}, (b'data: '+mcp.encode(ping)+b'\n\n',
                b'data: '+standard(request)[2]+b'\n\n', acknowledged)
        return standard(request)
    server.respond = respond
    client = mcp.Client(server.url)
    client.initialize()
    assert client.call_tool('query', {})['isError'] is False
    assert ping_responses == [{'jsonrpc':'2.0','id':'server-ping','result':{}}]
    assert len(client.observations) == 4
    client.close()
