"""Fixed local container smoke checks, not model execution or qualification."""
import os
from pathlib import Path
import sys
import time
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, "/app")
from agent_e2e_container import terminal, Executor, mcp_action
from agent_e2e_worker import MCP_PROTOCOL, ReplyChunks, reply_messages


assert os.geteuid() == 10001
assert not Path("/var/run/docker.sock").exists()
assert not Path("/app/agent_e2e_pack.py").exists()
assert not Path("/app/corpus").exists()
assert os.access("/workspace", os.W_OK) and os.access("/journal", os.W_OK)
workspace = Path("/workspace")
normal = terminal("id -u; printf '%s' \"$HONUA_AGENT_TRANSPORT$AWS_SECRET_ACCESS_KEY\"", 3, workspace)
assert normal["status"] == "completed" and normal["exitCode"] == 0
assert normal["stdout"] == "10001\n", normal
root_write = terminal("touch /app/should-not-exist", 3, workspace)
assert root_write["exitCode"] != 0 and not Path("/app/should-not-exist").exists()
started = time.monotonic()
timeout = terminal("sleep 10", 0.2, workspace)
assert timeout["status"] == "timed-out" and time.monotonic() - started < 3
oversized = terminal("python -c 'print(\"x\" * 2000000)'", 3, workspace)
assert len(oversized["stdout"].encode()) <= 16384 and len(oversized["stderr"].encode()) <= 4096
background = terminal("(sleep 1; touch survivor) &", 3, workspace)
time.sleep(1.2)
assert background["exitCode"] == 0 and not (workspace/"survivor").exists()
print("Container smoke passed: non-root, immutable app, isolated environment, bounded output, deadline, descendant cleanup.")

# Real subprocess and HTTP client in the actual worker image. This fixture is
# deliberately not Honua and cannot produce a qualifying scenario receipt.
catalog = [{'name':f'fixture-{index}', 'inputSchema':{'type':'object','description':'schema '*100}}
           for index in range(40)]
observed, errors = [], []
delay = [False]
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass
    def do_POST(self):
        try:
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert self.headers['Authorization'] == 'Bearer worker-only-canary'
            assert self.path == '/mcp'
            observed.append(request)
            method = request['method']
            if method == 'initialize':
                if delay[0]:
                    time.sleep(1)
                assert request['params']['_meta'] == {'honua.io/workflow-view':'setup'}
                value = {'protocolVersion':'2025-03-26','capabilities':{'tools':{}},'serverInfo':{'name':'fixture','version':'1'}}
            elif method == 'notifications/initialized':
                self.send_response(202)
                self.send_header('Content-Length','0')
                self.end_headers()
                return
            elif method == 'tools/list':
                assert request['params'] == {'view':'setup'}
                value = {'tools':catalog,'_meta':{'view':'setup','toolCount':len(catalog)}}
            else:
                assert method == 'tools/call'
                value = {'content':[{'type':'text','text':'observed'}],'structuredContent':request['params'],'isError':False}
            raw = json.dumps({'jsonrpc':'2.0','id':request['id'],'result':value}).encode()
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            errors.append(type(exc).__name__)

httpd = ThreadingHTTPServer(('127.0.0.1',0), Handler)
thread = threading.Thread(target=httpd.serve_forever, daemon=True)
thread.start()
os.environ['HONUA_AGENT_MCP_CONFIG'] = json.dumps({'endpoint':f'http://127.0.0.1:{httpd.server_port}/mcp',
    'credentialKind':'bearer','credential':'worker-only-canary','workflowView':'setup'})
# A model-created workspace module must not shadow the immutable client.
(workspace/'agent_e2e_mcp.py').write_text('raise RuntimeError("workspace import escaped")\n')
try:
    expires = int(time.time())+120
    config = {'attemptId':'mcp-smoke','expiresEpoch':expires}
    executor = Executor(config, '/journal/mcp-smoke.sqlite', workspace)
    command = {'protocol':MCP_PROTOCOL,'attemptId':'mcp-smoke','actionId':'discovery',
        'deadlineEpoch':int(time.time())+30,'timeoutSeconds':30,'action':{'name':'mcp_list_tools','input':{}}}
    reply = executor.handle(command)
    assert reply['status'] == 'completed'
    assert reply['payload']['result']['tools'] == catalog
    assert len(json.dumps(reply).encode()) > 16384
    assert 'worker-only-canary' not in json.dumps(reply)
    assert not reply['payload']['error'] and reply['payload']['sessionCleanup'] == {'status':'pass'}
    chunks = ReplyChunks(command)
    for part in reply_messages(reply, command):
        restored = chunks.accept(part)
    assert restored == reply
    count = len(observed)
    assert executor.handle(command) == reply and len(observed) == count
    call = {**command,'actionId':'call','deadlineEpoch':int(time.time())+30,
            'action':{'name':'mcp_call_tool','input':{'name':'model-selected','arguments':{'value':17}}}}
    called = executor.handle(call)
    assert called['payload']['result']['structuredContent'] == call['action']['input']
    delay[0] = True
    started = time.monotonic()
    timed_out = mcp_action(command['action'], 0.2, workspace)
    assert timed_out == {'status':'timed-out','payload':None} and time.monotonic()-started < 3
    assert not errors, errors
finally:
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)
    os.environ.pop('HONUA_AGENT_MCP_CONFIG', None)
print('MCP container smoke passed: immutable subprocess imports, HTTP discovery/call, complete catalog, chunk binding, replay, hard deadline.')
