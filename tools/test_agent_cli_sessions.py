"""Execute fake CLI binaries over the real loopback recording proxy. No model calls."""
import json
import os
from pathlib import Path
import textwrap

import pytest

from agent_cli_sessions import (CliFailure, CliModelSession, RecordingProxy, cli_environment,
                                codex_forbidden_diagnostics, digest, encoded, internal_events, parse_transcript)
from agent_e2e_local_recorder import read_logged_action

TOOLS = {'tools': [{'name': 'honua_query_features', 'description': 'Query the candidate',
                    'inputSchema': {'type': 'object', 'properties': {}}}]}


def test_rehearsal_discovery_hash_identifies_server_discover():
    assert digest(b'server/discover') == 'sha256:79580444954c2b593a013c1449220b917e7b6e09687dc2c48cf0dd7370457c02'


def test_codex_debug_detects_calls_missing_from_json_events():
    names = {'honua': {'honua_ops_health'}}
    allowed = b'event.name="codex.tool_call_received" tool_name="honua_ops_health" tool_namespace="mcp__honua"'
    rejected = b'event.name="codex.tool_call_received" tool_name="tool_search_tool" tool_namespace="tool_search"'
    assert not codex_forbidden_diagnostics(allowed, names)
    assert codex_forbidden_diagnostics(rejected, names)
    plan = b'event.name="codex.tool_call_received" tool_name="update_plan" tool_namespace="functions" tool_source="direct"'
    assert not codex_forbidden_diagnostics(plan, names, 'codex-cli 0.165.0')
    assert codex_forbidden_diagnostics(plan, names, 'codex-cli 0.165.1')
    assert codex_forbidden_diagnostics(plan.replace(b'update_plan', b'exec'), names, 'codex-cli 0.165.0')
    assert codex_forbidden_diagnostics(plan.replace(b'functions', b''), names, 'codex-cli 0.165.0')
    assert internal_events('codex-cli', 'codex-cli 0.165.0', b'', plan)[0]['kind'] == 'update_plan'
    assert internal_events('codex-cli', 'codex-cli 0.165.1', b'', plan) == []
    plan_event = json.dumps({'type': 'item.updated', 'item': {'type': 'todo_list', 'id': 'item_0',
                            'items': [{'text': 'Check health', 'completed': False}]}}).encode()
    assert not parse_transcript('codex-cli', plan_event, names, 'codex-cli 0.165.0')[-1]
    assert parse_transcript('codex-cli', plan_event, names, 'codex-cli 0.165.1')[-1]
    assert internal_events('codex-cli', 'codex-cli 0.165.0', plan_event, b'')[0]['kind'] == 'todo_list'
    assert internal_events('codex-cli', 'codex-cli 0.165.1', plan_event, b'') == []


def fake_cli(tmp_path, agent, mode='ok'):
    path = tmp_path / ('claude' if agent == 'claude-cli' else 'codex')
    body = '''
import json, os, pathlib, sys, urllib.request, time
args = sys.argv[1:]
if args == ['--version']:
    print('codex-cli 0.165.0' if MODE == 'plan' else 'fake-cli 1.0'); sys.exit(0)
if args[:3] == ['debug', 'models', '--bundled']:
    print(json.dumps({'models':[{'slug':'gpt-6.1-sol', 'apply_patch_tool_type':'freeform',
                                'shell_type':'unified_exec', 'tool_mode':'code_mode_only', 'supports_search_tool':True,
                                'experimental_supported_tools':['clock']}]}))
    sys.exit(0)
assert all(not any(s in k.upper() for s in ('AWS', 'DATABASE', 'POSTGRES', 'DOCKER', 'HONUA', 'SECRET')) for k in os.environ)
assert pathlib.Path.home() == pathlib.Path.cwd()
assert not list(pathlib.Path.cwd().glob('**/AGENTS.md'))
assert not list(pathlib.Path.cwd().glob('**/CLAUDE.md'))
prompt = sys.stdin.read()
assert prompt == 'Customer goal only'
if AGENT == 'claude-cli':
    assert int(os.environ['MCP_TOOL_TIMEOUT']) >= 2700000
    assert args[args.index('--tools') + 1] == ''
    assert args[args.index('--permission-mode') + 1] == 'dontAsk'
    assert '--strict-mcp-config' in args and '--restricted' in args
    assert 'Bash' not in args[args.index('--allowedTools') + 1]
    config = json.loads(pathlib.Path(args[args.index('--mcp-config') + 1]).read_text())
    assert set(config['mcpServers']) == {'honua'}
    url = config['mcpServers']['honua']['url']
else:
    assert '--ignore-user-config' in args and '--ignore-rules' in args
    assert args[args.index('--sandbox') + 1] == 'read-only'
    overrides = dict(args[i+1].split('=',1) for i,a in enumerate(args) if a == '-c')
    assert overrides['web_search'] == '"disabled"'
    assert overrides['project_doc_max_bytes'] == '0'
    assert 'shell_tool' in args and 'multi_agent' in args and 'browser_use' in args
    catalog = json.loads(pathlib.Path(json.loads(overrides['model_catalog_json'])).read_text())
    assert catalog['models'][0]['apply_patch_tool_type'] is None
    assert catalog['models'][0]['shell_type'] == 'disabled'
    assert catalog['models'][0]['tool_mode'] is None
    assert catalog['models'][0]['supports_search_tool'] is False
    assert catalog['models'][0]['experimental_supported_tools'] == []
    url = json.loads(overrides['mcp_servers.honua.url'])
    assert json.loads(overrides['mcp_servers.honua.tool_timeout_sec']) >= 2700
    assert json.loads(overrides['mcp_servers.honua.default_tools_approval_mode']) == 'approve'
    assert json.loads(overrides['mcp_servers.honua.startup_timeout_sec']) >= 2700
    assert os.environ['RUST_LOG'] == 'codex=debug'
    assert json.loads(overrides['tools.update_plan.enabled']) == (MODE == 'plan')
    if MODE == 'plan': print('event.name="codex.tool_call_received" tool_name="update_plan" tool_namespace="functions" tool_source="direct"', file=sys.stderr)
def rpc(method, params):
    raw = json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params}).encode()
    return json.load(urllib.request.urlopen(urllib.request.Request(url,data=raw,headers={'Content-Type':'application/json'})))
rpc('initialize', {'protocolVersion':'2025-03-26'})
assert rpc('prompts/list', {})['result'] == {'prompts': []}
assert rpc('server/discover', {})['error']['code'] == -32601
assert rpc('tools/list', {})['result']['tools'][0]['name'] == 'honua_query_features'
if MODE == 'item_failure':
    print(json.dumps({'type':'item.completed','item':{'type':'error','message':'tool host unavailable'}})); sys.exit(1)
if MODE == 'cli_failure':
    print(json.dumps({'type':'turn.failed','error':{'message':'provider unavailable'}})); sys.exit(1)
if MODE == 'timeout': time.sleep(20)
if MODE == 'unknown': rpc('tools/call', {'name':'Bash','arguments':{'command':'touch bad'}})
else: rpc('tools/call', {'name':'honua_query_features','arguments':{'actual':True}})
if MODE == 'malformed': print('[]')
if AGENT == 'claude-cli':
    name = 'Bash' if MODE == 'forbidden' else 'mcp__honua__honua_query_features'
    print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':name,'input':{'forged':True}}]}}))
    print(json.dumps({'type':'result','result':'Customer result','usage':{'input_tokens':5,'output_tokens':3}}))
else:
    item = {'type':'command_execution','command':'touch bad'} if MODE == 'forbidden' else {'type':'mcp_tool_call','server':'honua','tool':'honua_query_features','arguments':{'forged':True}}
    print(json.dumps({'type':'item.completed','item':item}))
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Customer result'}}))
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':5,'output_tokens':3}}))
'''
    path.write_text('#!/usr/bin/python3\n' + f'AGENT={agent!r}\nMODE={mode!r}\n' + textwrap.dedent(body))
    path.chmod(0o700)
    return path


@pytest.mark.parametrize('agent,model', [('claude-cli', 'claude-sonnet-5-5'), ('codex-cli', 'gpt-6.1-sol')])
def test_native_cli_isolation_and_proxy_custody(tmp_path, monkeypatch, agent, model):
    for key in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'DATABASE_URL', 'PGPASSWORD',
                'DOCKER_HOST', 'HONUA_ADMIN_PASSWORD', 'TERMINAL_MODEL_API_KEY'):
        monkeypatch.setenv(key, 'controller-only-secret')
    calls = []
    def execute(kind, action):
        calls.append(action)
        return {'status':'ok', 'result': {'content': [], 'structuredContent': {'mapId':'server-created'}}}
    cli = CliModelSession(agent, model, binary=fake_cli(tmp_path, agent), auth_root=tmp_path)
    out = cli.run(prompt='Customer goal only', tool_view=TOOLS, execute=execute, log_dir=tmp_path/'log')
    assert calls == [{'kind':'tool_call','tool':'honua_query_features','arguments':{'actual':True}}]
    assert len(out['actions']) == 1
    action = read_logged_action(out['actions'][0]['logPath'], out['actions'][0]['digest'])
    assert action['arguments'] == {'actual':True}
    assert out['resourceHints'] == [{'field':'mapId','value':'server-created'}]
    assert out['finalAnswer'] == 'Customer result'
    proof = out['provenance'][0]
    assert proof['cost'] is None and proof['temperature'] is None
    assert proof['mcpToolTimeoutSeconds'] >= 2700 and proof['exitCode'] == 0
    assert (tmp_path/'log/cli-stdout.jsonl').stat().st_mode & 0o777 == 0o600
    assert (tmp_path/'log/cli-stderr.log').stat().st_mode & 0o777 == 0o600
    assert proof['cliVersion'] == 'fake-cli 1.0'
    assert proof['promptSha256'] == digest(b'Customer goal only')
    assert proof['servedToolListSha256'] == digest(encoded(TOOLS))
    assert len(proof['mcpResponseSha256']) == 5
    assert [event['method'] for event in proof['mcpEvents']['honua']] == [
        'initialize', 'prompts/list', 'other', 'tools/list', 'tools/call']
    assert all(event['requestBytes'] > 0 and event['responseBytes'] > 0
               for event in proof['mcpEvents']['honua'])
    flags = json.loads((tmp_path/'log/cli-inputs.json').read_bytes())['flags']
    assert proof['cliFlagsSha256'] == digest(encoded(flags))
    assert 'controller-only-secret' not in json.dumps(proof)
    home = Path(flags[flags.index('--mcp-config')+1]).parent if agent == 'claude-cli' else None
    assert home is None or not home.exists()


@pytest.mark.parametrize('agent,model', [('claude-cli', 'claude-opus-5-5'), ('codex-cli', 'gpt-6.1-sol')])
@pytest.mark.parametrize('mode', ['forbidden', 'unknown', 'timeout'])
def test_forbidden_attempts_and_timeout_are_failures(tmp_path, agent, model, mode):
    calls=[]
    cli=CliModelSession(agent, model, binary=fake_cli(tmp_path,agent,mode), auth_root=tmp_path)
    with pytest.raises(CliFailure) as exc:
        cli.run(prompt='Customer goal only', tool_view=TOOLS,
                execute=lambda k,a: calls.append(a) or {'status':'ok','result':{'content':[]}},
                log_dir=tmp_path/'log', timeout=0.2 if mode=='timeout' else 5)
    assert exc.value.category == ('infrastructure' if mode == 'timeout' else 'model')
    assert len(calls) == (1 if mode=='forbidden' else 0)


def test_unsafe_iam_refusal_is_retained_in_action_ledger(tmp_path):
    with RecordingProxy(TOOLS, lambda *_: {'status': 'fail', 'refusal': 'unsafe-iam-request',
                        'result': {'content': [], 'isError': True}}, tmp_path, 1, 2) as proxy:
        proxy.rpc('tools/call', {'name': 'honua_query_features', 'arguments': {}})
        assert proxy.actions[0]['refusal'] == 'unsafe-iam-request'
        assert proxy.actions[0]['status'] == 'fail'


def test_notifications_are_logged_and_cancellation_is_infrastructure(tmp_path):
    import urllib.request
    with RecordingProxy(TOOLS, lambda *_: {'status': 'ok'}, tmp_path, 1, 2) as proxy:
        for method in ('notifications/roots/list_changed', 'notifications/cancelled'):
            data = json.dumps({'jsonrpc': '2.0', 'method': method, 'params': {}}).encode()
            with urllib.request.urlopen(urllib.request.Request(proxy.url, data=data)) as response:
                assert response.status == 202
        assert proxy.failure_category == 'infrastructure'
        events = [json.loads(line) for line in proxy.event_path.read_text().splitlines()]
        assert [event['method'] for event in events] == ['notifications/roots/list_changed', 'notifications/cancelled']
        assert all(event['status'] == 202 and event['elapsedMs'] >= 0 for event in events)


def test_cancel_during_dispatched_action_is_undetermined(tmp_path):
    import threading, urllib.request
    entered, release = threading.Event(), threading.Event()
    def execute(*_):
        entered.set(); assert release.wait(5)
        return {'status': 'ok', 'result': {'content': []}}
    with RecordingProxy(TOOLS, execute, tmp_path, 1, 2) as proxy:
        thread = threading.Thread(target=lambda: proxy.rpc('tools/call',
            {'name': 'honua_query_features', 'arguments': {}}))
        thread.start(); assert entered.wait(5)
        data = json.dumps({'jsonrpc': '2.0', 'method': 'notifications/cancelled', 'params': {}}).encode()
        with urllib.request.urlopen(urllib.request.Request(proxy.url, data=data)) as response:
            assert response.status == 202
        assert proxy.failure_category == 'post-action-undetermined'
        release.set(); thread.join(5)


@pytest.mark.parametrize('mode', ['cli_failure', 'item_failure'])
def test_cli_failure_is_infrastructure_with_raw_diagnostics(tmp_path, mode):
    cli = CliModelSession('codex-cli', 'gpt-6.1-sol', binary=fake_cli(tmp_path, 'codex-cli', mode), auth_root=tmp_path)
    with pytest.raises(CliFailure) as caught:
        cli.run(prompt='Customer goal only', tool_view=TOOLS, execute=lambda *_: {}, log_dir=tmp_path/'log')
    assert caught.value.category == 'infrastructure'
    assert caught.value.progress['provenance'][0]['exitCode'] == 1
    assert ('provider unavailable' if mode == 'cli_failure' else 'tool host unavailable') in (tmp_path/'log/cli-stdout.jsonl').read_text()


def test_versioned_internal_plan_is_recorded_without_scored_action(tmp_path):
    cli = CliModelSession('codex-cli', 'gpt-6.1-sol', binary=fake_cli(tmp_path, 'codex-cli', 'plan'), auth_root=tmp_path)
    out = cli.run(prompt='Customer goal only', tool_view=TOOLS,
                  execute=lambda *_: {'status': 'ok', 'result': {'content': []}}, log_dir=tmp_path/'log')
    assert len(out['actions']) == 1 and out['actions'][0]['tool'] == 'honua_query_features'
    assert [event['kind'] for event in out['internalEvents']] == ['update_plan']
    assert json.loads((tmp_path/'log/internal-events.json').read_text()) == out['internalEvents']


def test_action_bound_logs_only_actual_callbacks(tmp_path):
    calls=[]
    with RecordingProxy(TOOLS, lambda k,a: calls.append(a) or {'status':'ok'}, tmp_path, 1, 1) as proxy:
        proxy.rpc('tools/call', {'name':'honua_query_features','arguments':{}})
        with pytest.raises(ValueError): proxy.rpc('tools/call', {'name':'honua_query_features','arguments':{}})
        assert len(calls) == len(proxy.actions) == 1
        assert proxy.failure


def test_parser_crash_after_dispatch_keeps_proxy_actions(tmp_path):
    cli=CliModelSession('claude-cli','claude-sonnet-5-5',
                        binary=fake_cli(tmp_path,'claude-cli','malformed'),auth_root=tmp_path)
    with pytest.raises(CliFailure) as exc:
        cli.run(prompt='Customer goal only',tool_view=TOOLS,
                execute=lambda *args:{'status':'ok','result':{'content':[]}},log_dir=tmp_path/'log')
    assert exc.value.category=='post-action-undetermined'
    assert len(exc.value.progress['actions'])==1
    assert exc.value.progress['provenance'][0]['agent']=='claude-cli'


def test_unknown_outcome_retains_dispatch(tmp_path):
    def execute(*_): raise RuntimeError('connection lost after dispatch')
    with RecordingProxy(TOOLS, execute, tmp_path, 1, 1) as proxy:
        with pytest.raises(RuntimeError): proxy.rpc('tools/call', {'name':'honua_query_features','arguments':{}})
        assert proxy.actions[0]['status'] == 'unknown'
        assert proxy.failure_category == 'post-action-undetermined'


def test_failed_shell_validation_is_model_failure(tmp_path):
    def refuse(*_): raise ValueError('command refused')
    with RecordingProxy({'tools':[{'name':'run_quickstart'}]},refuse,tmp_path,1,1,
                        server_name='quickstart_shell') as proxy:
        with pytest.raises(ValueError):
            proxy.rpc('tools/call',{'name':'run_quickstart','arguments':{'command':'cat /etc/passwd'}})
        assert proxy.failure_category=='model'
        assert proxy.actions[0]['kind']=='shell_command'


def test_proxy_never_returns_dynamic_credentials_from_any_backend(tmp_path):
    def leak(*_):
        return {'status':'ok','result':{'content':[{'type':'text',
            'text':'{"secretAccessKey":"newly-issued-secret"}'}]}}
    with RecordingProxy(TOOLS,leak,tmp_path,1,1) as proxy:
        with pytest.raises(ValueError,match='credential'):
            proxy.rpc('tools/call',{'name':'honua_query_features','arguments':{}})
        assert proxy.failure_category=='post-action-undetermined'
        assert proxy.actions[0]['status']=='unknown'


def test_proxy_refuses_credential_bearing_tool_catalog(tmp_path):
    tools={'tools':[{'name':'honua_query_features','description':'AWS_SECRET_ACCESS_KEY=example-secret'}]}
    with pytest.raises(ValueError,match='credential'):
        RecordingProxy(tools,lambda *_:None,tmp_path,1,1)


def test_credential_bearing_prompt_is_refused_before_starting_cli(tmp_path):
    binary=fake_cli(tmp_path,'claude-cli')
    cli=CliModelSession('claude-cli','claude-opus-5-5',binary=binary,auth_root=tmp_path)
    with pytest.raises(CliFailure) as failure:
        cli.run(prompt='DATABASE_PASSWORD=private-value',tool_view=TOOLS,execute=lambda *_:None,log_dir=tmp_path/'log')
    assert failure.value.category=='infrastructure'
    assert not (tmp_path/'log').exists()


@pytest.mark.parametrize('agent', ['claude-cli','codex-cli'])
def test_provider_auth_only(tmp_path, agent):
    source=tmp_path/'source'; home=tmp_path/'home'; home.mkdir()
    folder=source/('.claude' if agent=='claude-cli' else '.codex'); folder.mkdir(parents=True)
    name='.credentials.json' if agent=='claude-cli' else 'auth.json'
    (folder/name).write_text('{"tokens":{"access_token":"subscription-token"}}')
    (folder/'config.toml').write_text('SECRET=do-not-copy')
    (folder/'AGENTS.md').write_text('do-not-copy')
    env=cli_environment(home, agent, auth_root=source)
    dest=Path(env['CLAUDE_CONFIG_DIR'] if agent=='claude-cli' else env['CODEX_HOME'])
    assert sorted(p.name for p in dest.iterdir()) == [name]
    assert (dest/name).stat().st_mode & 0o777 == 0o600


def test_api_key_auth_refused(tmp_path):
    (tmp_path/'.codex').mkdir(); (tmp_path/'.codex/auth.json').write_text('{"OPENAI_API_KEY":"key"}')
    home=tmp_path/'home'; home.mkdir()
    with pytest.raises(ValueError,match='subscription'): cli_environment(home,'codex-cli',auth_root=tmp_path)


def test_model_pairing_refused_before_process():
    with pytest.raises(ValueError): CliModelSession('codex-cli','claude-sonnet-5-5')


@pytest.mark.parametrize('item', ['file_change','web_search','command_execution','image_generation'])
def test_codex_builtin_attempts_fail(item):
    raw=encoded({'type':'item.started','item':{'type':item}})
    assert parse_transcript('codex-cli',raw,{'honua_query_features'})[-1]



def test_codex_0166_resource_listing_is_bookkeeping_but_other_builtins_are_not():
    """Rehearsal finding (A2): Codex 0.166 called its built-in list_mcp_resources (server "codex"),
    which reached the recording proxy as resources/list."""
    names = {'quickstart_shell': {'run_quickstart'}}
    v = 'codex-cli 0.166.0'
    listing = (b'{"type":"item.completed","item":{"id":"i","type":"mcp_tool_call","server":"codex",'
               b'"tool":"list_mcp_resources","arguments":{},"status":"completed"}}\n'
               b'{"type":"item.completed","item":{"id":"j","type":"agent_message","text":"ok"}}\n')
    assert not parse_transcript('codex-cli', listing, names, v)[-1]
    assert parse_transcript('codex-cli', listing, names, 'codex-cli 0.165.0')[-1]
    assert parse_transcript('codex-cli', listing.replace(b'list_mcp_resources', b'read_mcp_resource'), names, v)[-1]
    diag = b'event.name="codex.tool_call_received" tool_namespace="functions" tool_name="list_mcp_resources"\n'
    assert not codex_forbidden_diagnostics(diag, names, v)
    assert codex_forbidden_diagnostics(diag.replace(b'list_mcp_resources', b'exec_command'), names, v)


@pytest.mark.parametrize('agent, model', [('claude-cli', 'claude-opus-5-5'), ('codex-cli', 'gpt-6.1-sol')])
def test_a_cli_other_than_the_pinned_version_refuses_before_any_dispatch(tmp_path, agent, model):
    """Astra #1177 re-review item 2: the installed CLI ('fake-cli 1.0') is not the pinned one."""
    calls = []
    cli = CliModelSession(agent, model, binary=fake_cli(tmp_path, agent), auth_root=tmp_path,
                          expected_version='codex-cli 0.166.0' if agent == 'codex-cli' else '2.1.316 (Claude Code)')
    with pytest.raises(CliFailure, match='not the pinned') as failure:
        cli.run(prompt='Customer goal only', tool_view=TOOLS, execute=lambda k, a: calls.append(a),
                log_dir=tmp_path / 'log')
    assert calls == [] and failure.value.category == 'infrastructure'
    assert not (tmp_path / 'log' / 'cli-stdout.jsonl').exists(), 'the CLI was never launched'
