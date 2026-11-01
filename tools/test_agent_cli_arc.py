"""Fake-CLI tests for stage-scoped MCP backends and remote quickstart commands."""
import json
from pathlib import Path
import socket
import subprocess
import threading

import pytest

from agent_cli_sessions import CliFailure, CliModelSession
from agent_cli_arc import run_stage, AWS_SETUP_TOOLS
from agent_cli_backends import (RemoteQuickstartShell, StdioMcpBackend, SshTunnel,
                                HttpMcpBackend, parse_quickstart)
from agent_e2e_local_recorder import read_logged_action


def fake_multi_cli(tmp_path, agent):
    path=tmp_path/('claude' if agent=='claude-cli' else 'codex')
    source='''#!/usr/bin/python3
import json,os,sys,pathlib,urllib.request
args=sys.argv[1:]
if args==['--version']: print('fake 2'); sys.exit()
if args[:3]==['debug','models','--bundled']:
 print(json.dumps({'models':[{'slug':'gpt-6.1-sol','shell_type':'unified_exec','apply_patch_tool_type':'freeform'}]})); sys.exit()
assert not any(k.startswith(('AWS_','HONUA_','DOCKER_','PG')) for k in os.environ)
assert sys.stdin.read() == 'Complete the stage'
if AGENT == 'claude-cli':
 config=json.loads(pathlib.Path(args[args.index('--mcp-config')+1]).read_text())['mcpServers']
 servers={name:item['url'] for name,item in config.items()}
else:
 overrides=dict(args[i+1].split('=',1) for i,a in enumerate(args) if a=='-c')
 servers={k.split('.')[1]:json.loads(v) for k,v in overrides.items() if k.startswith('mcp_servers.') and k.endswith('.url')}
for server,url in servers.items():
 def rpc(method,params):
  msg={'jsonrpc':'2.0','id':1,'method':method,'params':params}
  return json.load(urllib.request.urlopen(urllib.request.Request(url,data=json.dumps(msg).encode(),headers={'Content-Type':'application/json'})))['result']
 rpc('initialize',{'protocolVersion':'2025-03-26'})
 tool=rpc('tools/list',{})['tools'][0]['name']
 params={'command':'docker compose ps'} if server=='quickstart_shell' else {'query':'test'}
 rpc('tools/call',{'name':tool,'arguments':params})
 if AGENT == 'claude-cli':
  print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'mcp__'+server+'__'+tool}]}}))
 else:
  print(json.dumps({'type':'item.completed','item':{'type':'mcp_tool_call','server':server,'tool':tool}}))
if AGENT=='claude-cli': print(json.dumps({'type':'result','result':'done','usage':{'input_tokens':1,'output_tokens':1}}))
else:
 print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done'}}))
 print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))
'''
    path.write_text(source.replace('import json,os,sys,pathlib,urllib.request',
                                   f'import json,os,sys,pathlib,urllib.request\nAGENT={agent!r}'))
    path.chmod(0o700)
    return path


def view(name):
    return {'tools':[{'name':name,'inputSchema':{'type':'object','properties':{}}}]}


@pytest.mark.parametrize('agent,model',[('claude-cli','claude-opus-5-5'),('codex-cli','gpt-6.1-sol')])
def test_three_named_servers_proxy_and_record_actual_actions(tmp_path,monkeypatch,agent,model):
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY','temporary-secret')
    monkeypatch.setenv('HONUA_OPERATOR_TOKEN','operator-secret')
    calls=[]
    servers={}
    for server,tool in [('honua_devops','describe_environment'),('honua','honua_ops_health'),('studio','studio_snapshot')]:
        def execute(kind,action,server=server):
            calls.append((server,action))
            return {'status':'ok','result':{'content':[{'type':'text','text':'safe'}]}}
        servers[server]={'tool_view':view(tool),'execute':execute}
    cli=CliModelSession(agent,model,binary=fake_multi_cli(tmp_path,agent),auth_root=tmp_path)
    row=cli.run(prompt='Complete the stage',servers=servers,stage=1,target_kind='aws-ecs',log_dir=tmp_path/'log')
    assert {a['server'] for a in row['actions']}=={'honua_devops','honua','studio'}
    assert [a['sequence'] for a in row['actions']]==[1,2,3]
    assert [a[0] for a in calls]==[a['server'] for a in row['actions']]
    assert all(read_logged_action(a['logPath'],a['digest'])['server']==a['server'] for a in row['actions'])
    assert set(row['provenance'][0]['servedToolListsSha256'])==set(servers)
    assert set(row['provenance'][0]['mcpResponsesByServer'])==set(servers)
    assert 'temporary-secret' not in json.dumps(row['provenance'])


class Backend:
    def __init__(self,name): self.name=name; self.calls=[]
    def tool_view(self):
        if self.name == 'describe_environment':
            return {'tools':[{'name':name,'inputSchema':{'type':'object','properties':{}}}
                             for name in ['describe_environment',*sorted(AWS_SETUP_TOOLS-{'describe_environment'})]]}
        return view(self.name)
    def check_health(self): return {'status':'pass'}
    def execute(self,kind,action):
        self.calls.append(action)
        return {'status':'ok','result':{'content':[{'type':'text','text':'ok'}]}}


@pytest.mark.parametrize('stage,target,name,tool',[(1,'aws-ecs','honua_devops','describe_environment'),
    (1,'remote-vm','quickstart_shell','run_quickstart'),(2,'remote-vm','honua','honua_ops_health'),
    (3,'aws-ecs','honua','honua_query_features'),(4,'remote-vm','studio','studio_snapshot')])
def test_arc_stage_only_exposes_its_own_surface(tmp_path,stage,target,name,tool):
    backend=Backend(tool)
    def factory(agent,model): return CliModelSession(agent,model,binary=fake_multi_cli(tmp_path,agent),auth_root=tmp_path)
    result=run_stage(agent='claude-cli',model='claude-opus-5-5',stage=stage,target_kind=target,
                     prompt='Complete the stage',backends={name:backend},log_dir=tmp_path/'log',cli_factory=factory)
    assert len(backend.calls)==1 and result['actions'][0]['server']==name
    assert result['actions'][0]['kind']==('shell_command' if name=='quickstart_shell' else 'tool_call')


def test_shell_refused_outside_vm_setup_before_cli(tmp_path):
    cli=CliModelSession('claude-cli','claude-opus-5-5',binary=fake_multi_cli(tmp_path,'claude-cli'),auth_root=tmp_path)
    with pytest.raises(CliFailure):
        cli.run(prompt='Complete the stage',servers={'quickstart_shell':{'tool_view':view('run_quickstart'),
            'execute':lambda *_:None}},stage=2,target_kind='remote-vm',log_dir=tmp_path/'log')
    assert not list((tmp_path/'log').glob('action-*'))


def test_studio_stage_rejects_server_mcp_bypass(tmp_path):
    with pytest.raises(ValueError,match='only its own product surface'):
        run_stage(agent='claude-cli',model='claude-opus-5-5',stage=4,target_kind='aws-ecs',
                  prompt='Complete the stage',backends={'studio':Backend('studio_snapshot'),
                  'honua':Backend('honua_ops_health')},log_dir=tmp_path)


def test_legacy_shell_surface_is_rejected_before_cli(tmp_path):
    cli=CliModelSession('claude-cli','claude-opus-5-5',binary=fake_multi_cli(tmp_path,'claude-cli'),auth_root=tmp_path)
    with pytest.raises(CliFailure) as failure:
        cli.run(prompt='Complete the stage',tool_view=view('run_command'),execute=lambda *_:None,log_dir=tmp_path/'log')
    assert failure.value.category=='infrastructure'
    assert not failure.value.progress['actions']


def test_quickstart_backend_cannot_advertise_file_tools(tmp_path):
    cli=CliModelSession('claude-cli','claude-opus-5-5',binary=fake_multi_cli(tmp_path,'claude-cli'),auth_root=tmp_path)
    with pytest.raises(CliFailure):
        cli.run(prompt='Complete the stage',servers={'quickstart_shell':{'tool_view':view('write_file'),
            'execute':lambda *_:None}},stage=1,target_kind='remote-vm',log_dir=tmp_path/'log')


@pytest.mark.parametrize('command',[
 'docker run -v /:/host image', 'docker compose exec server sh', 'docker compose down --volumes',
 'docker compose -f ../../other.yml up -d', 'docker compose up -d; cat /etc/passwd',
 'docker compose up $(touch bad)', 'curl https://example.com', 'curl http://169.254.169.254/',
 'curl -L http://localhost:8080', 'curl -o docker-compose.yml http://localhost:8080/',
 'curl http://user:pass@localhost:8080', 'bash -c id'])
def test_quickstart_rejects_non_allowlisted_commands(command):
    with pytest.raises(ValueError): parse_quickstart(command)


@pytest.mark.parametrize('command',['docker compose up -d --wait --wait-timeout 180',
    'docker compose --profile console up -d','docker compose ps','curl -fsS http://localhost:8080/healthz/ready'])
def test_quickstart_accepts_published_compose_and_local_health(command):
    assert parse_quickstart(command)


def remote_shell(tmp_path, executor):
    identity=tmp_path/'identity'; identity.write_text('dummy')
    hosts=tmp_path/'known_hosts'; hosts.write_text('dummy')
    return RemoteQuickstartShell(host='vm123.example',user='runner',identity_file=identity,known_hosts=hosts,
        remote_dir='/home/runner/honua-arc/arc-12345678',slot='arc-12345678',executor=executor)


def test_remote_shell_never_invokes_local_docker_or_exposes_secret_output(tmp_path):
    calls=[]
    def fake(command,**kwargs):
        calls.append((command,kwargs))
        return subprocess.CompletedProcess(command,0,b'DATABASE_PASSWORD=hidden-secret',b'')
    shell=remote_shell(tmp_path,fake)
    result=shell.execute('shell_command',{'tool':'run_quickstart','arguments':{'command':'docker compose config'}})
    assert calls[0][0][0]=='ssh' and 'hidden-secret' not in json.dumps(result)
    assert b'--project-name arc-12345678' in calls[0][1]['input']
    assert result['_recording']['commandSha256'].startswith('sha256:')
    assert '--' in calls[0][0] and 'StrictHostKeyChecking=yes' in calls[0][0]


def test_remote_health_preflight_uses_ssh_and_refuses_unhealthy_docker(tmp_path):
    calls=[]
    def fake(command,**kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command,0,b'29.0\nserver Up (unhealthy)',b'')
    shell=remote_shell(tmp_path,fake)
    with pytest.raises(RuntimeError,match='unhealthy'): shell.check_health()
    assert calls[0][0]=='ssh'


def test_compose_ps_exposes_only_sanitized_status_fields(tmp_path):
    def fake(command,**kwargs):
        raw=json.dumps([{'Name':'slot-server','Service':'server','State':'running','Health':'healthy',
                         'Command':'SECRET=hidden','Publishers':'private'}]).encode()
        return subprocess.CompletedProcess(command,0,raw,b'')
    shell=remote_shell(tmp_path,fake)
    answer=shell.execute('shell_command',{'tool':'run_quickstart','arguments':{'command':'docker compose ps'}})
    assert json.loads(answer['result']['content'][0]['text'])==[
        {'Name':'slot-server','Service':'server','State':'running','Health':'healthy'}]
    assert 'SECRET' not in json.dumps(answer)


def test_inline_compose_must_match_reviewed_published_bytes(tmp_path):
    from agent_cli_sessions import digest
    calls=[]
    def fake(command,**kwargs):
        calls.append(kwargs['input'])
        return subprocess.CompletedProcess(command,0,b'',b'')
    shell=remote_shell(tmp_path,fake)
    source='services:\n  honua:\n    image: pinned\n'
    shell.approved_compose_sha256=digest(source.encode())
    action={'tool':'run_quickstart','arguments':{'command':'docker compose -f - up -d','stdin':source}}
    assert shell.execute('shell_command',action)['status']=='ok'
    assert source.encode() in calls[0]
    action['arguments']['stdin']+='  privileged: true\n'
    with pytest.raises(ValueError,match='approved'): shell.execute('shell_command',action)
    no_newline=source.rstrip('\n')
    shell.approved_compose_sha256=digest(no_newline.encode())
    action['arguments']['stdin']=no_newline
    with pytest.raises(ValueError,match='approved'): shell.execute('shell_command',action)


def test_curl_displays_only_approved_public_document_bytes(tmp_path):
    from agent_cli_sessions import digest
    document=b'Published quickstart text'
    def fake(command,**kwargs): return subprocess.CompletedProcess(command,0,document,b'')
    shell=remote_shell(tmp_path,fake)
    action={'tool':'run_quickstart','arguments':{'command':'curl http://localhost:8080/quickstart'}}
    assert 'Published' not in shell.execute('shell_command',action)['result']['content'][0]['text']
    shell.visible_document_sha256=(digest(document),)
    assert shell.execute('shell_command',action)['result']['content'][0]['text']=='Published quickstart text'


def test_devops_mcp_gets_explicit_short_lived_credentials_only(tmp_path):
    binary=tmp_path/'operator'
    binary.write_text('''#!/usr/bin/python3
import json,os,sys
assert sys.argv[1:]==['--mcp']
assert os.environ['AWS_SESSION_TOKEN']=='temporary-token'
assert 'CLAUDE_CODE_OAUTH_TOKEN' not in os.environ and 'DOCKER_HOST' not in os.environ
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m: continue
 if m.get('params',{}).get('arguments',{}).get('leak_audit'): print(os.environ['AWS_SECRET_ACCESS_KEY'],file=sys.stderr,flush=True)
 if m.get('params',{}).get('arguments',{}).get('leak_dynamic_audit'): print(json.dumps({'sessionToken':'new-secret'}),file=sys.stderr,flush=True)
 result={'protocolVersion':'2025-03-26','capabilities':{},'serverInfo':{'name':'fake'}} if m['method']=='initialize' else ({'tools':[{'name':'describe_environment','inputSchema':{'type':'object'}}]} if m['method']=='tools/list' else {'content':[{'type':'text','text':os.environ['AWS_SECRET_ACCESS_KEY'] if m.get('params',{}).get('arguments',{}).get('leak') else 'ok'}]})
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
'''); binary.chmod(0o700)
    env={'AWS_ACCESS_KEY_ID':'temporary-id','AWS_SECRET_ACCESS_KEY':'temporary-secret','AWS_SESSION_TOKEN':'temporary-token'}
    operator=StdioMcpBackend(binary,slot_env=env,workdir=tmp_path).start()
    try:
        assert operator.tool_view()['tools'][0]['name']=='describe_environment'
        assert operator.execute('tool_call',{'tool':'describe_environment','arguments':{}})['status']=='ok'
        with pytest.raises(ValueError,match='disclosed'):
            operator.execute('tool_call',{'tool':'describe_environment','arguments':{'leak':True}})
    finally: operator.close()
    assert operator.audit_sha256 and operator.audit_path.is_file()
    assert operator.audit_path.stat().st_mode & 0o777 == 0o600
    other=tmp_path/'other'; other.mkdir()
    leaking=StdioMcpBackend(binary,slot_env=env,workdir=other).start()
    leaking.execute('tool_call',{'tool':'describe_environment','arguments':{'leak_audit':True}})
    with pytest.raises(ValueError,match='audit output'):
        leaking.close()
    assert not leaking.audit_path.exists()
    third=tmp_path/'third'; third.mkdir()
    dynamic=StdioMcpBackend(binary,slot_env=env,workdir=third).start()
    dynamic.execute('tool_call',{'tool':'describe_environment','arguments':{'leak_dynamic_audit':True}})
    with pytest.raises(ValueError,match='credential'):
        dynamic.close()
    assert not dynamic.audit_path.exists()
    with pytest.raises(ValueError,match='short-lived'):
        StdioMcpBackend(binary,slot_env={'AWS_ACCESS_KEY_ID':'long-lived','AWS_SECRET_ACCESS_KEY':'secret'},workdir=tmp_path)


def test_http_backend_refuses_unrelated_remote_origin():
    with pytest.raises(ValueError): HttpMcpBackend('https://attacker.example/mcp')


@pytest.mark.parametrize('unsafe',['AdministratorAccess','iam:*','iam:CreateUser','iam:CreateAccessKey'])
def test_operator_refuses_unsafe_identity_request_before_product_call(tmp_path,unsafe):
    binary=tmp_path/'operator'; binary.write_text('#!/bin/sh\nexit 0\n'); binary.chmod(0o700)
    backend=StdioMcpBackend(binary,slot_env={},workdir=tmp_path)
    def never(*_args,**_kwargs): raise AssertionError('unsafe request reached the operator')
    backend.rpc=never
    result=backend.execute('tool_call',{'tool':'provision_infrastructure','arguments':{'variablesJson':unsafe}})
    assert result['status']=='fail' and result['refusal']=='unsafe-iam-request'


@pytest.mark.parametrize('value',[
    {'secretAccessKey':'newly-issued-secret'}, {'session_token':'newly-issued-token'},
    {'content':[{'type':'text','text':'postgresql://user:password@localhost/db'}]},
    {'content':[{'type':'text','text':'AWS_SECRET_ACCESS_KEY=newly-issued-secret'}]},
    {'content':[{'type':'text','text':'ASIA1234567890ABCDEF'}]},
    {'content':[{'type':'text','text':'{"secretAccessKey":"newly-issued-secret"}'}]},
])
def test_dynamic_credentials_are_not_forwarded_to_model(value):
    from agent_cli_backends import _screen
    with pytest.raises(ValueError,match='credential'):
        _screen(value,[])


def test_tool_schema_and_redacted_credential_fields_are_allowed():
    from agent_cli_backends import _screen
    value={'inputSchema':{'properties':{'password':{'type':'string'}}},
           'structuredContent':{'password':'[redacted]'}}
    assert _screen(value,[])==value


def test_honua_http_backend_negotiates_paginated_full_tool_view():
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_): pass
        def do_POST(self):
            request=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(request)
            if 'id' not in request:
                self.send_response(202); self.end_headers(); return
            result=({'protocolVersion':'2025-03-26','capabilities':{},'serverInfo':{'name':'fake'}}
                    if request['method']=='initialize' else
                    {'tools':[{'name':'honua_ops_health','inputSchema':{'type':'object'}}]})
            raw=json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}).encode()
            self.send_response(200); self.send_header('Content-Length',str(len(raw))); self.end_headers(); self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        backend=HttpMcpBackend(f'http://127.0.0.1:{server.server_port}/mcp',header='X-API-Key',token='scoped-test-key',workflow_view='full').start()
        assert backend.tool_view()['tools'][0]['name']=='honua_ops_health'
        assert requests[0]['params']['_meta']['honua.io/workflow-view']=='full'
        assert requests[-1]['params']['view']=='full'
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_full_catalog_requires_auth_and_rejects_narrowed_view_metadata():
    with pytest.raises(ValueError, match='requires authentication'):
        HttpMcpBackend('http://127.0.0.1:8123/mcp', workflow_view='full')
    backend = HttpMcpBackend('http://127.0.0.1:8123/mcp', header='X-API-Key', token='scoped-test-key', workflow_view='full')
    backend.rpc = lambda *_args, **_kwargs: {'tools': [], '_meta': {'view': 'setup'}}
    with pytest.raises(ValueError, match='narrowed'):
        backend.tool_view()


def test_unhealthy_vm_blocks_before_cli_session(tmp_path):
    backend=Backend('run_quickstart')
    backend.check_health=lambda:{'status':'fail'}
    def never(*_): raise AssertionError('CLI must not start')
    with pytest.raises(ValueError,match='health'):
        run_stage(agent='claude-cli',model='claude-opus-5-5',stage=1,target_kind='remote-vm',
                  prompt='Complete the stage',backends={'quickstart_shell':backend},log_dir=tmp_path,cli_factory=never)


def test_incomplete_operator_catalog_blocks_before_cli_session(tmp_path):
    backend=Backend('describe_environment')
    backend.tool_view=lambda:view('describe_environment')
    def never(*_): raise AssertionError('CLI must not start')
    with pytest.raises(ValueError,match='lacks required'):
        run_stage(agent='claude-cli',model='claude-opus-5-5',stage=1,target_kind='aws-ecs',
                  prompt='Complete the stage',backends={'honua_devops':backend},log_dir=tmp_path,cli_factory=never)


def test_runner_owned_ssh_tunnel_forwards_loopback_only(tmp_path):
    remote=remote_shell(tmp_path,lambda *_a,**_kw:None)
    calls=[]
    class Process:
        def __init__(self,command,kwargs):
            calls.append((command,kwargs))
            forwarding=command[command.index('-L')+1]
            host,port,dest,dest_port=forwarding.split(':')
            assert (host,dest,dest_port)==('127.0.0.1','127.0.0.1','8080')
            self.socket=socket.socket(); self.socket.bind(('127.0.0.1',int(port))); self.socket.listen(1)
            self.running=True
            def accept():
                connection,_=self.socket.accept(); connection.close()
            threading.Thread(target=accept,daemon=True).start()
        def poll(self): return None if self.running else 0
        def terminate(self): self.running=False; self.socket.close()
        def wait(self,timeout): return 0
    tunnel=SshTunnel(remote,remote_port=8080,popen=lambda command,**kwargs:Process(command,kwargs)).start()
    try:
        assert tunnel.base_url.startswith('http://127.0.0.1:')
        assert 'ExitOnForwardFailure=yes' in calls[0][0]
        assert not any(name.startswith(('AWS_','HONUA_')) for name in calls[0][1]['env'])
    finally: tunnel.close()


def test_parallel_remote_sessions_have_separate_recording_state(tmp_path):
    binary=fake_multi_cli(tmp_path,'claude-cli')
    outputs=[]; errors=[]
    def work(number):
        try:
            backend=Backend('honua_ops_health')
            cli=CliModelSession('claude-cli','claude-opus-5-5',binary=binary,auth_root=tmp_path)
            outputs.append(cli.run(prompt='Complete the stage',servers={'honua':{'tool_view':backend.tool_view(),
                'execute':backend.execute}},stage=2,target_kind='remote-vm',log_dir=tmp_path/str(number)))
        except Exception as exc: errors.append(exc)
    threads=[threading.Thread(target=work,args=(n,)) for n in range(3)]
    for thread in threads: thread.start()
    for thread in threads: thread.join()
    assert not errors and len(outputs)==3
    assert all(row['actions'][0]['sequence']==1 for row in outputs)
    assert len({row['actions'][0]['logPath'] for row in outputs})==3


def test_operator_oidc_credentials_are_slot_scoped_and_screened(tmp_path):
    binary = tmp_path / 'operator'
    binary.write_text('#!/bin/sh\nexit 0\n'); binary.chmod(0o700)
    token = tmp_path / 'token'; token.write_text('oidc-fixture-token-1234567890'); token.chmod(0o600)
    role = 'arn:aws:iam::123456789012:role/cert/slot'
    env = {'AWS_ROLE_ARN': role, 'AWS_WEB_IDENTITY_TOKEN_FILE': str(token)}
    backend = StdioMcpBackend(binary, slot_env=env, workdir=tmp_path)
    assert backend.credential_mode == 'web-identity'
    assert token.read_text() in backend.secrets
    assert 'AWS_SECRET_ACCESS_KEY' not in backend.env
    with pytest.raises(ValueError, match='paired'):
        StdioMcpBackend(binary, slot_env={'AWS_ROLE_ARN': role}, workdir=tmp_path)
    with pytest.raises(ValueError, match='mix'):
        StdioMcpBackend(binary, slot_env={**env, 'AWS_ACCESS_KEY_ID': 'a', 'AWS_SECRET_ACCESS_KEY': 'b', 'AWS_SESSION_TOKEN': 'c'}, workdir=tmp_path)
    token.chmod(0o644)
    with pytest.raises(ValueError, match='private'):
        StdioMcpBackend(binary, slot_env=env, workdir=tmp_path)
    token.chmod(0o600)
    link = tmp_path / 'link'; link.symlink_to(token)
    with pytest.raises(ValueError, match='private'):
        StdioMcpBackend(binary, slot_env={**env, 'AWS_WEB_IDENTITY_TOKEN_FILE': str(link)}, workdir=tmp_path)
    with pytest.raises(ValueError, match='private'):
        StdioMcpBackend(binary, slot_env={**env, 'AWS_WEB_IDENTITY_TOKEN_FILE': '/etc/hosts'}, workdir=tmp_path)


def test_aws_stage_requires_temporary_operator_credentials(tmp_path):
    binary = tmp_path / 'operator'
    binary.write_text('#!/bin/sh\nexit 0\n'); binary.chmod(0o700)
    backend = StdioMcpBackend(binary, slot_env={}, workdir=tmp_path)
    def never(*_): raise AssertionError('CLI must not start')
    with pytest.raises(ValueError, match='temporary operator credentials'):
        run_stage(agent='claude-cli', model='claude-opus-5-5', stage=1, target_kind='aws-ecs',
                  prompt='Complete the stage', backends={'honua_devops': backend}, log_dir=tmp_path, cli_factory=never)


@pytest.mark.parametrize('command',['docker compose config --images','docker compose config --format json',
    'docker compose config --services','docker compose images','docker compose images --format json',
    'docker compose ps -a --format json','docker compose ps --services','docker compose logs --tail 50 honua',
    'docker compose port honua 8080','docker compose top'])
def test_quickstart_admits_read_only_compose_inspection(command):
    """Rehearsal A1-r113: read-only compose subcommands a customer's shell allows were refused."""
    assert parse_quickstart(command)[:2] == ['docker','compose']


@pytest.mark.parametrize('command',['docker compose exec -T honua printenv HONUA_IMAGE','docker compose run honua id',
    'docker compose cp honua:/app/.env .','docker compose rm -f','docker compose stop','docker compose restart',
    'docker compose kill','docker compose config --output x.yml','docker compose logs --tail 99999',
    'docker compose ps --format \'{{.Command}}\'','docker compose images --format template'])
def test_quickstart_still_refuses_container_access_and_changes(command):
    with pytest.raises(ValueError): parse_quickstart(command)


def test_name_only_compose_output_is_visible_and_anything_else_is_withheld(tmp_path):
    outputs={'config --images':b'ghcr.io/honua-io/honua-server@sha256:'+b'a'*64+b'\npostgis/postgis:16-3.4\n',
             'images':json.dumps([{'ContainerName':'arc-1-honua-1','Repository':'ghcr.io/honua-io/honua-server',
                                   'Tag':'2026.1-rc','ID':'c8d7aef98761','Size':'1GB','Command':'SECRET=x'}]).encode(),
             'config --services':b'honua\nDATABASE_PASSWORD=hidden secret\n'}
    sent=[]
    def fake(command,**kwargs):
        sent.append(kwargs['input'])
        return subprocess.CompletedProcess(command,0,next(v for k,v in outputs.items() if k.encode() in kwargs['input']),b'')
    shell=remote_shell(tmp_path,fake)
    run=lambda c: shell.execute('shell_command',{'tool':'run_quickstart','arguments':{'command':c}})['result']['content'][0]['text']
    assert run('docker compose config --images').splitlines()[0].startswith('ghcr.io/honua-io/honua-server@sha256:')
    assert json.loads(run('docker compose images'))==[{'ContainerName':'arc-1-honua-1',
        'Repository':'ghcr.io/honua-io/honua-server','Tag':'2026.1-rc','ID':'c8d7aef98761'}]
    assert b'--format=json' in sent[1]
    withheld=run('docker compose config --services')
    assert 'hidden' not in withheld and 'output withheld' in withheld


@pytest.mark.parametrize('command,expected', [('docker compose run --help', ['docker','compose','run','--help']),
                                              ('docker compose --help', ['docker','compose','--help'])])
def test_quickstart_admits_compose_usage(command, expected):
    assert parse_quickstart(command) == expected


@pytest.mark.parametrize('command', ['docker compose run honua --help', 'docker compose frobnicate --help'])
def test_quickstart_refuses_help_that_is_not_usage(command):
    with pytest.raises(ValueError): parse_quickstart(command)


@pytest.mark.parametrize('command', [
    "docker compose up '#--project-name=foreign' '#--project-directory=/srv/foreign' -d",
    "docker compose up '#--file=/srv/foreign/compose.yaml' -d", "docker compose logs '#--tail' 5",
    "docker compose up -d --wait-timeout '#5'", "docker compose logs --tail 5 '#5'"])
def test_caller_supplied_marker_tokens_are_refused(command):
    """Sol b6a4079b: '#'-prefixed tokens bypassed validation and were stripped before dispatch."""
    with pytest.raises(ValueError): parse_quickstart(command)


def test_numeric_option_values_pass_unchanged():
    assert parse_quickstart('docker compose up -d --wait --wait-timeout 180')[-2:] == ['--wait-timeout', '180']
    assert parse_quickstart('docker compose logs --tail 50 honua')[-3:] == ['--tail', '50', 'honua']


@pytest.mark.parametrize('remote_dir,ok', [('/home/ubuntu/honua-arc/arc-12345678', True), ('/root/honua-arc/arc-12345678', True),
                                           ('/tmp/honua-arc-12345678', False), ('/home/ubuntu/honua-arc/../etc', False),
                                           ('/home/ubuntu/other/arc-12345678', False)])
def test_the_remote_shell_admits_only_the_slot_directory_on_real_disk(tmp_path, remote_dir, ok):
    """ARC v2 (Astra): the slot lives on the VM's real disk, which Studio's local stage also admits."""
    identity=tmp_path/'identity'; identity.write_text('dummy')
    hosts=tmp_path/'known_hosts'; hosts.write_text('dummy')
    make=lambda: RemoteQuickstartShell(host='vm123.example',user='ubuntu',identity_file=identity,known_hosts=hosts,
        remote_dir=remote_dir,slot='arc-12345678',executor=lambda *a, **k: None)
    if ok: make()
    else:
        with pytest.raises(ValueError): make()
