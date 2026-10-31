"""Runner-controlled recording MCP proxy for agents that speak MCP themselves (CLI sessions).

The agent connects to a loopback URL with a per-session token. The proxy forwards JSON-RPC to
the case stack's /mcp with the agent credential (which the agent never sees) and writes every
tools/call and resources/read to the runner's ActionLog before forwarding it, so the action log
is the runner's record of MCP traffic, never the agent's own transcript. Only the methods an MCP
client needs are forwarded; resources/read admits only the catalog URI, as on the default path.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CATALOG_URI = 'honua://catalog/processes'
FORWARDED = {'initialize', 'notifications/initialized', 'tools/list', 'tools/call', 'resources/read', 'ping',
             'notifications/cancelled'}
MAX_BODY = 512 * 1024


class RecordingMcpProxy:
    def __init__(self, upstream, credential_env, action_log, tool_names, injector=None):
        self.upstream, self.credential_env, self.injector = upstream, credential_env, injector
        self.action_log, self.names = action_log, set(tool_names)
        self.token = secrets.token_hex(24)
        self.server = None

    def start(self):
        from agent_e2e_local_api import McpEndpoint
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, status, raw=b'', content_type='application/json', session=None):
                self.send_response(status)
                if raw:
                    self.send_header('Content-Type', content_type)
                if session:
                    self.send_header('Mcp-Session-Id', session)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def authorized(self):
                presented = self.headers.get('Authorization', '')
                return secrets.compare_digest(presented, 'Bearer ' + proxy.token)

            def do_DELETE(self):
                if not self.authorized():
                    return self.send(401)
                status, raw, ctype, session = proxy.forward(None, self.headers, method='DELETE')
                self.send(status, raw, ctype, session)

            def do_GET(self):
                self.send(405)

            def do_POST(self):
                if not self.authorized():
                    return self.send(401, b'{"error":"unauthorized"}')
                length = int(self.headers.get('Content-Length') or 0)
                if length > MAX_BODY:
                    return self.send(413)
                body = self.rfile.read(length)
                try:
                    message = json.loads(body)
                except ValueError:
                    return self.send(400, b'{"error":"invalid json"}')
                if not isinstance(message, dict) or message.get('method') not in FORWARDED and 'method' in message:
                    return self.send(200, proxy.rpc_error(message, -32601, 'method not available'))
                method = message.get('method')
                if method in ('tools/call', 'resources/read'):
                    return self.send(*proxy.recorded(message, self.headers))
                status, raw, ctype, session = proxy.forward(body, self.headers)
                self.send(status, raw, ctype, session)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return McpEndpoint(url=f'http://127.0.0.1:{self.server.server_address[1]}/mcp',
                           header='Authorization', token='Bearer ' + self.token)

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    @staticmethod
    def rpc_error(message, code, text):
        identity = message.get('id') if isinstance(message, dict) else None
        return json.dumps({'jsonrpc': '2.0', 'id': identity, 'error': {'code': code, 'message': text}}).encode()

    def forward(self, body, headers, *, method='POST'):
        out = {'Content-Type': 'application/json', 'Accept': headers.get('Accept', 'application/json, text/event-stream'),
               'X-API-Key': os.environ[self.credential_env]}
        for name in ('Mcp-Session-Id', 'MCP-Protocol-Version'):
            if headers.get(name):
                out[name] = headers[name]
        request = urllib.request.Request(self.upstream, data=body, headers=out, method=method)
        try:
            response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=120)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read(MAX_BODY + 1)
            return (response.status if hasattr(response, 'status') else response.code, raw,
                    response.headers.get('Content-Type', 'application/json'), response.headers.get('Mcp-Session-Id'))

    def recorded(self, message, headers):
        """Log the model-selected action in the runner's ActionLog, then forward it."""
        from agent_e2e_local_recorder import parse_jsonrpc
        params = message.get('params') if isinstance(message.get('params'), dict) else {}
        if message['method'] == 'tools/call':
            kind = 'tool_call'
            selected = {'kind': kind, 'tool': params.get('name'), 'arguments': params.get('arguments')
                        if isinstance(params.get('arguments'), dict) else {}}
        else:
            kind = 'resource_read'
            selected = {'kind': kind, 'uri': params.get('uri')}
        reply = {}

        def execute(_kind, action):
            if kind == 'resource_read' and action.get('uri') != CATALOG_URI:
                reply['raw'] = self.rpc_error(message, -32602, 'only the admitted catalog resource can be read')
                return {'status': 'fail', 'refusal': 'invalid-action'}
            if kind == 'tool_call' and action.get('tool') not in self.names:
                reply['raw'] = self.rpc_error(message, -32602, 'tool is not in the server-authored tool list')
                return {'status': 'fail', 'refusal': 'invalid-action'}
            from agent_e2e_local_runner import ActionOutcomeUnknown
            if self.injector is not None and self.injector.intercept(kind, action) is not None:
                from agent_e2e_local_faults import UNAVAILABLE
                reply.update(status=503, raw=UNAVAILABLE, ctype='application/json', session=None)
                return {'status': 'fail', 'httpStatus': 503}
            try:
                status, raw, ctype, session = self.forward(json.dumps(message).encode(), headers)
            except OSError as exc:
                raise ActionOutcomeUnknown('proxy upstream ' + type(exc).__name__) from exc
            reply.update(status=status, raw=raw, ctype=ctype, session=session)
            try:
                rpc = parse_jsonrpc(raw)
            except ValueError as exc:
                # A dispatched request whose response cannot be interpreted has an unknown outcome:
                # keep the bytes as evidence and let the runner fail the case as undetermined.
                raise ActionOutcomeUnknown('uninterpretable upstream response', captured=(status, raw)) from exc
            result = rpc.get('result')
            failed = status != 200 or 'error' in rpc or (isinstance(result, dict) and result.get('isError') is True)
            execution = {'status': 'fail' if failed else 'ok', 'result': result if result is not None else rpc.get('error')}
            if self.injector is not None:
                self.injector.observe(kind, action, execution)
            execution['_rawResponse'] = (status, raw)
            return execution
        from agent_e2e_local_runner import ActionBoundExceeded, ActionOutcomeUnknown
        try:
            self.action_log.dispatch(kind, selected, execute)
        except ActionBoundExceeded:
            return 200, self.rpc_error(message, -32000, 'action bound exceeded'), 'application/json', None
        except ActionOutcomeUnknown:
            return 502, b'', 'application/json', None
        return (reply.get('status', 200), reply['raw'], reply.get('ctype', 'application/json'), reply.get('session'))
