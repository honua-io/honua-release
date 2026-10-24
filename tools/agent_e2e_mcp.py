"""Bounded MCP runtime transport for the test agent's own identity.

This is a test-backend client, not an oracle or an infrastructure provisioner.
It never retries a request, grants approval, or treats a transport failure as
evidence that a tool did not execute. The caller supplies isolated agent custody,
a hard action deadline and durable dispatch intent before invoking this client.
"""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


VERSION = '2025-03-26'
MAX_BODY = 512 * 1024
MAX_TOTAL = 2 * 1024 * 1024


class McpError(ValueError):
    def __init__(self, category, *, public_error=None):
        super().__init__(category)  # Never include credentials or SDK exception text.
        self.category = category
        self.public_error = public_error


def require(condition, category):
    if not condition:
        raise McpError(category)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def parse(raw, *, secret=None):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, 'duplicate-json-field')
            result[key] = value
        return result
    def finite(value):
        raise McpError('nonfinite-json')
    try:
        result = json.loads(raw.decode('utf-8'), object_pairs_hook=unique, parse_constant=finite)
        pending = [result]
        while pending and secret:
            value = pending.pop()
            if isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, str):
                require(secret not in value, 'credential-disclosed-by-server')
        return result
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise McpError('invalid-json') from exc


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise McpError('endpoint-redirect-refused')


def validate_agent_context(value):
    require(value is None or isinstance(value, dict) and set(value) == {'workspaceId', 'turnId'} and
            all(isinstance(item, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', item) for item in value.values()),
            'invalid-native-agent-context')
    return None if value is None else dict(value)


def validate_execution_context(value):
    """Trusted transport binding, never part of model tool arguments."""
    require(value is None or isinstance(value, dict) and
            set(value) == {'executionId', 'claimGeneration', 'workerId', 'capability'} and
            isinstance(value['executionId'], str) and re.fullmatch(r'execution-[0-9a-f]{32}', value['executionId']) and
            type(value['claimGeneration']) is int and value['claimGeneration'] > 0 and
            isinstance(value['workerId'], str) and 0 < len(value['workerId']) <= 512 and
            all(33 <= ord(c) <= 126 for c in value['workerId']) and
            isinstance(value['capability'], str) and 32 <= len(value['capability']) <= 8192 and
            all(33 <= ord(c) <= 126 for c in value['capability']), 'invalid-native-execution-context')
    return None if value is None else dict(value)


def execution_headers(value):
    value = validate_execution_context(value)
    return {} if value is None else {'X-Honua-Execution': value['executionId'],
        'X-Honua-Execution-Generation': str(value['claimGeneration']), 'X-Honua-Worker': value['workerId'],
        'X-Honua-Execution-Capability': value['capability']}


class Client:
    def __init__(self, endpoint, *, credential_env=None, credential_kind='bearer', workflow_view=None,
                 timeout=60, clock=time.monotonic, agent_context=None, execution_context=None):
        require(isinstance(endpoint, str) and all(ord(c) > 0x20 for c in endpoint), 'invalid-endpoint')
        try:
            parsed = urllib.parse.urlsplit(endpoint)
            parsed.port  # Validate malformed/out-of-range ports before any request.
        except ValueError as exc:
            raise McpError('invalid-endpoint') from exc
        require(bool(parsed.hostname) and not parsed.username and not parsed.password and not parsed.query
                and not parsed.fragment and '\\' not in endpoint, 'invalid-endpoint')
        require(parsed.scheme == 'https' or (parsed.scheme == 'http' and parsed.hostname in ('localhost','127.0.0.1','::1')),
                'endpoint-requires-tls-or-loopback')
        require(credential_kind in ('bearer','api-key'), 'invalid-credential-kind')
        self._validate_view(workflow_view)
        self.workflow_view = workflow_view
        self.agent_context = validate_agent_context(agent_context)
        self.execution_context = validate_execution_context(execution_context)
        require(self.execution_context is None or self.agent_context is not None, 'execution-requires-native-turn')
        require(type(timeout) in (int,float) and 0 < timeout <= 120, 'invalid-deadline')
        self.endpoint, self.clock, self.deadline = endpoint, clock, clock()+timeout
        self.secret = None
        if credential_env is not None:
            require(isinstance(credential_env, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', credential_env), 'invalid-credential-reference')
            self.secret = os.environ.get(credential_env)
            require(isinstance(self.secret, str) and self.secret and all(0x21 <= ord(c) <= 0x7e for c in self.secret),
                    'missing-or-invalid-agent-credential')
        self.credential_kind = credential_kind
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self.session = None
        self.ready = False
        self.closed = False
        self.total_bytes = 0
        self.observations = []

    def remaining(self):
        value = self.deadline-self.clock()
        require(value > 0, 'action-deadline')
        return value

    @staticmethod
    def _validate_view(view):
        require(view is None or (isinstance(view, str) and 0 < len(view) <= 64
                and view == view.strip() and all(ord(c) > 0x20 for c in view)), 'invalid-workflow-view')

    def _check_secret(self, raw):
        for secret in (self.secret, None if self.execution_context is None else self.execution_context['capability']):
            if secret:
                self._check_private_value(raw, secret)

    @staticmethod
    def _check_private_value(raw, secret):
        text = raw.decode('utf-8', errors='replace') if isinstance(raw, bytes) else raw
        escapes = {'"':'"', '\\':'\\', '/':'/', 'b':'\b', 'f':'\f', 'n':'\n', 'r':'\r', 't':'\t'}
        # Inspect all received bytes, including SSE comments/incomplete frames.
        # Parsing only complete JSON events misses escaped echoes in the tail.
        for _ in range(16):
            require(secret not in text, 'credential-disclosed-by-server')
            decoded = re.sub(r'\\(?:u([0-9a-fA-F]{4})|(["\\/bfnrt]))',
                lambda match: chr(int(match[1], 16)) if match[1] else escapes[match[2]], text)
            if decoded == text:
                return
            text = decoded
        raise McpError('excessively-escaped-response')

    def _http(self, payload=None, *, method='POST'):
        require(not self.closed, 'session-closed')
        require(self.total_bytes < MAX_TOTAL, 'response-too-large')
        body = None if payload is None else encode(payload)
        require(body is None or len(body) <= MAX_BODY, 'request-too-large')
        headers = {'Accept':'application/json, text/event-stream', 'Content-Type':'application/json',
                   'MCP-Protocol-Version':VERSION}
        if self.agent_context is not None:
            headers.update({'X-Honua-Workspace': self.agent_context['workspaceId'], 'X-Honua-Turn': self.agent_context['turnId']})
        headers.update(execution_headers(self.execution_context))
        if self.secret:
            headers['Authorization' if self.credential_kind == 'bearer' else 'X-API-Key'] = (
                'Bearer '+self.secret if self.credential_kind == 'bearer' else self.secret)
        if self.session:
            headers['Mcp-Session-Id'] = self.session
        request = urllib.request.Request(self.endpoint, data=body, method=method, headers=headers)
        replied_pings = set()
        def on_ping(message, position):
            if position in replied_pings:
                return
            require(len(replied_pings) < 16, 'server-ping-limit')
            replied_pings.add(position)  # Never resend after an ambiguous POST.
            status, _, raw = self._http({'jsonrpc':'2.0', 'id':message['id'], 'result':{}})
            require(status == 202 and not raw, 'server-ping-response-not-acknowledged')
        try:
            try:
                response = self.opener.open(request, timeout=min(10, self.remaining()))
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                raw = bytearray()
                while True:
                    self.remaining()
                    chunk = response.read1(min(65536, MAX_BODY+1-len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                    self.total_bytes += len(chunk)
                    require(len(raw) <= MAX_BODY and self.total_bytes <= MAX_TOTAL, 'response-too-large')
                    self._check_secret(bytes(raw))
                    # Honua's POST SSE response is finite. Stop as soon as the
                    # matching response frame arrives; never wait for GET streams.
                    if (response.headers.get_content_type() == 'text/event-stream' and payload
                            and 'id' in payload and 'method' in payload):
                        if self._sse(bytes(raw), payload['id'], partial=True, secret=self.secret, on_ping=on_ping) is not None:
                            break
                raw = bytes(raw)
                self._check_secret(raw)
                if raw and response.headers.get_content_type() == 'application/json':
                    parse(raw, secret=self.secret)
                observed_headers = {key:response.headers[key] for key in ('X-Request-Id','traceparent') if key in response.headers}
                require(all(len(v) <= 512 for v in observed_headers.values()), 'oversized-correlation-header')
                for value in observed_headers.values():
                    self._check_secret(value)
                self.observations.append({'method':method, 'rpcMethod':payload.get('method') if payload else None,
                    'requestDigest':'sha256:'+hashlib.sha256(body or b'').hexdigest(), 'httpStatus':response.code,
                    'bodyBase64':base64.b64encode(raw).decode(), 'bodyDigest':'sha256:'+hashlib.sha256(raw).hexdigest(),
                    'headers':observed_headers})
                return response.code, response.headers, raw
        except McpError:
            raise
        except (OSError, urllib.error.URLError, http.client.HTTPException) as exc:
            raise McpError('transport-outcome-unknown') from exc

    @staticmethod
    def _sse(raw, request_id, *, partial=False, secret=None, on_ping=None):
        # Accept CRLF and LF framing. Incomplete final events are never parsed.
        frames = raw.replace(b'\r\n', b'\n').split(b'\n\n')
        matches = []
        for frame_index, frame in enumerate(frames[:-1]):
            data = b'\n'.join(line[5:].lstrip(b' ') for line in frame.split(b'\n') if line.startswith(b'data:'))
            if not data:
                continue
            message = parse(data, secret=secret)
            for item_index, item in enumerate(message if isinstance(message, list) else [message]):
                require(isinstance(item, dict) and item.get('jsonrpc') == '2.0', 'invalid-sse-message')
                if 'method' in item:
                    if 'id' in item:
                        require(item['method'] == 'ping', 'unnegotiated-server-request')
                        require(type(item['id']) in (str, int) and item.get('params', {}) == {}, 'invalid-server-ping')
                        require(on_ping is not None, 'server-ping-handler-required')
                        on_ping(item, (frame_index, item_index))
                    continue  # Progress notifications are retained in the raw observation.
                require(type(item.get('id')) is str and item['id'] == request_id, 'foreign-rpc-response')
                matches.append(item)
        require(len(matches) <= 1, 'duplicate-rpc-response')
        if not matches and not partial:
            raise McpError('missing-rpc-response')
        return matches[0] if matches else None

    def _rpc(self, method, params):
        request_id = uuid.uuid4().hex
        status, headers, raw = self._http({'jsonrpc':'2.0', 'id':request_id, 'method':method, 'params':params})
        require(status == 200, 'http-'+str(status))
        content_type = headers.get_content_type()
        require(content_type in ('application/json','text/event-stream'), 'unsupported-response-type')
        # Pings were answered while streaming; this final parse never resends.
        message = self._sse(raw, request_id, on_ping=lambda *_: None) if content_type == 'text/event-stream' else parse(raw)
        require(isinstance(message, dict) and message.get('jsonrpc') == '2.0'
                and type(message.get('id')) is str and message['id'] == request_id
                and (('result' in message) != ('error' in message)), 'unbound-rpc-response')
        if 'error' in message:
            error = message['error']
            require(isinstance(error, dict) and type(error.get('code')) is int and isinstance(error.get('message'), str),
                    'invalid-rpc-error')
            raise McpError('rpc-error', public_error=error)
        require(isinstance(message['result'], dict), 'invalid-rpc-result')
        return message['result'], headers

    def initialize(self):
        require(not self.ready and not self.session, 'session-already-initialized')
        params = {'protocolVersion':VERSION, 'capabilities':{}, 'clientInfo':{'name':'honua-agent-e2e', 'version':'1'}}
        if self.workflow_view is not None:
            params['_meta'] = {'honua.io/workflow-view':self.workflow_view}
        result, headers = self._rpc('initialize', params)
        require(result.get('protocolVersion') == VERSION and isinstance(result.get('capabilities'), dict)
                and isinstance(result.get('serverInfo'), dict), 'unsupported-initialize-result')
        session = headers.get('Mcp-Session-Id')
        if session is not None:
            require(0 < len(session) <= 256 and all(0x21 <= ord(c) <= 0x7e for c in session), 'invalid-session-id')
        self.session = session
        status, _, raw = self._http({'jsonrpc':'2.0', 'method':'notifications/initialized'})
        require(status == 202 and not raw, 'initialization-not-acknowledged')
        self.ready = True
        return result

    def list_tools(self, *, view=None):
        require(self.ready, 'session-not-initialized')
        self._validate_view(view)
        selected = self.workflow_view if view is None else view
        # Repeat an explicit selector on every page. Stateless Honua sessions
        # cannot retain initialize metadata; cursors alone do not select a view.
        selector = {'view':selected} if selected is not None else {}
        tools, names, cursors, params, metadata = [], set(), set(), dict(selector), None
        for page_index in range(32):
            result, _ = self._rpc('tools/list', params)
            current_metadata = result.get('_meta')
            require(current_metadata is None or isinstance(current_metadata, dict), 'invalid-catalog-metadata')
            if page_index == 0:
                metadata = current_metadata
            else:
                require(current_metadata == metadata, 'catalog-metadata-changed')
            if selected is not None and selected != 'full':
                require(isinstance(metadata, dict) and metadata.get('view') == selected, 'workflow-view-not-observed')
            elif selected == 'full':
                require(not metadata or metadata.get('view') in (None, 'full'), 'workflow-view-not-observed')
            page = result.get('tools')
            require(isinstance(page, list), 'invalid-tool-catalog')
            for tool in page:
                require(isinstance(tool, dict) and isinstance(tool.get('name'), str)
                        and tool['name'].strip() and len(tool['name']) <= 256
                        and isinstance(tool.get('inputSchema'), dict) and tool['name'] not in names, 'invalid-or-duplicate-tool')
                names.add(tool['name'])
                tools.append(tool)
            catalog = {'tools':tools}
            if metadata is not None:
                catalog['_meta'] = metadata
            require(len(tools) <= 1024 and len(encode(catalog)) <= MAX_BODY, 'catalog-too-large')
            cursor = result.get('nextCursor')
            if cursor is None:
                if metadata is not None and 'toolCount' in metadata:
                    require(type(metadata['toolCount']) is int and metadata['toolCount'] == len(tools),
                            'catalog-tool-count-mismatch')
                return catalog
            require(isinstance(cursor, str) and 0 < len(cursor) <= 4096 and cursor not in cursors, 'invalid-or-repeated-cursor')
            cursors.add(cursor)
            params = {**selector, 'cursor':cursor}
        raise McpError('catalog-page-limit')

    def call_tool(self, name, arguments):
        require(self.ready, 'session-not-initialized')
        require(isinstance(name, str) and name.strip() and len(name) <= 256 and isinstance(arguments, dict), 'invalid-tool-call')
        result, _ = self._rpc('tools/call', {'name':name, 'arguments':arguments})
        require(isinstance(result.get('content'), list) and type(result.get('isError', False)) is bool, 'invalid-tool-result')
        return result  # Preserve server errors/structured content; do not invent success.

    def close(self):
        if self.closed:
            return
        try:
            if self.session:
                status, _, _ = self._http(method='DELETE')
                require(status in (200,202,204,404,405), 'session-termination-failed')
        finally:
            self.closed, self.ready = True, False
