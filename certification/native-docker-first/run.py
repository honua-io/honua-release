#!/usr/bin/env python3
"""One-attempt, published-artifact native Docker first-use runner.

This is an operator-run live harness, never a release gate or synthetic model.
A blocked phase is retained and cannot be replayed as a successful phase.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'certification' / 'terminal-journey'))
import pins  # noqa: E402
import sdk  # noqa: E402

SCHEMA = 'honua.native-docker-first/v1'
SHA = re.compile(r'^[a-f0-9]{40}$')
DIGEST = re.compile(r'^[a-f0-9]{64}$')
IMAGE = re.compile(r'^ghcr\.io/honua-io/[a-z0-9-]+:[a-zA-Z0-9._-]+@sha256:[a-f0-9]{64}$')


class Blocked(RuntimeError):
    pass


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise Blocked(f'{path.name} must be an object')
    return value


def private_json(path: Path, value: dict, *, new: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise Blocked(f'{path.name} is a symlink')
    if new and path.exists():
        raise Blocked(f'{path.name} already exists')
    temp = path.parent / f'.{path.name}.{uuid.uuid4().hex}.tmp'
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
        if new:
            os.link(temp, path)
        else:
            os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def candidate(manifest_path: Path) -> tuple[dict, dict]:
    manifest = yaml.safe_load(manifest_path.read_text())
    if not isinstance(manifest, dict):
        raise Blocked('candidate manifest is invalid')
    components = manifest.get('components') or {}
    clients = manifest.get('clientArtifacts') or {}
    server, console = (components.get(key) or {} for key in ('honua-server', 'honua-console'))
    sdk_js, mcp = (clients.get(key) or {} for key in ('honua-sdk-js', 'honua-mcp-server'))
    dotnet = clients.get('honua-sdk-dotnet') or {}
    if sdk_js.get('version') != mcp.get('version') or tuple(int(x) for x in mcp.get('version', '0.0.0').split('.')[:3]) < (0, 2, 7):
        raise Blocked('candidate requires coherent published SDK/MCP 0.2.7 or later; the old release manifest is refused')
    for name, component in (('server', server), ('console', console)):
        if not SHA.fullmatch(component.get('sha', '')) or not IMAGE.fullmatch(f"{component.get('image', '')}@{component.get('digest', '')}"):
            raise Blocked(f'{name} needs a source SHA and immutable published GHCR image')
    for name, pin in (('sdk', sdk_js), ('mcp', mcp)):
        if pin.get('publicationState') not in ('published', 'promoted') or pin.get('ecosystem') != 'npm' or not pin.get('integrity', '').startswith('sha512-'):
            raise Blocked(f'{name} needs a published exact npm integrity pin')
    if (dotnet.get('publicationState') not in ('published', 'promoted') or
            dotnet.get('ecosystem') != 'nuget' or dotnet.get('registry') != 'nuget.org' or
            not re.fullmatch(r'sha256:[a-f0-9]{64}', dotnet.get('digest', ''))):
        raise Blocked('candidate needs a published NuGet.org .NET SDK digest pin')
    release_id = manifest.get('platformRelease')
    if not isinstance(release_id, str) or not release_id:
        raise Blocked('candidate release identity missing')
    binding = {'manifestSha256': sha256(manifest_path), 'releaseId': release_id,
               'server': {'sha': server['sha'], 'image': f"{server['image']}@{server['digest']}"},
               'console': {'sha': console['sha'], 'image': f"{console['image']}@{console['digest']}"},
               'clients': {key: {'package': clients[key]['package'], 'version': clients[key]['version'],
                                 'integrity': clients[key]['integrity']} for key in ('honua-sdk-js', 'honua-mcp-server')},
               'dotnet': {'package': dotnet['package'], 'version': dotnet['version'],
                          'digest': dotnet['digest'], 'registry': dotnet['registry']}}
    return manifest, binding


def check_harness(root: Path, expected: str) -> dict:
    if not SHA.fullmatch(expected):
        raise Blocked('Console browser producer needs exact source SHA')
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=root, text=True).strip()
    if git('rev-parse', 'HEAD') != expected or git('status', '--porcelain'):
        raise Blocked('Console browser producer must be clean at its exact expected HEAD')
    specs = [root / 'e2e/playwright/workspace-live/specs' / name for name in
             ('studio-map.live.spec.ts', 'publication-witness.live.spec.ts')]
    if any(not spec.is_file() for spec in specs):
        raise Blocked('Console browser producer or audience witness missing')
    return {'sourceSha': expected, 'specSha256': {spec.name: sha256(spec) for spec in specs}}


def playwright_command(root: Path, spec_name: str) -> list[str]:
    executable = root / 'e2e/playwright/node_modules/.bin/playwright'
    if not executable.exists():
        raise Blocked('Console Playwright dependencies are not installed from their lockfile')
    return [str(executable), 'test', '--config', 'playwright.workspace.config.ts',
            f'workspace-live/specs/{spec_name}']


def check_state(state: dict, binding: dict, harness: dict) -> None:
    if state.get('schemaVersion') != SCHEMA or state.get('candidate') != binding or state.get('harness') != harness:
        raise Blocked('attempt binding changed; start a new attempt')


def client_entry(prefix: Path, command: str) -> Path:
    path = prefix / 'node_modules/.bin' / command
    if not path.exists():
        raise Blocked(f'published package does not ship {command}')
    return path.resolve()


def installed_target(directory: Path, binding: dict) -> dict:
    envfile = directory / '.env'
    compose = directory / 'compose.yaml'
    if not envfile.is_file() or not compose.is_file():
        raise Blocked('installer did not create its private environment and Compose configuration')
    values = {}
    for line in envfile.read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            if key in ('HONUA_AGENT_API_KEY', 'HONUA_AGENT_KEY_ID', 'POSTGRES_PASSWORD',
                       'HONUA_SERVER_IMAGE', 'HONUA_CONSOLE_IMAGE', 'HONUA_AGENT_RELEASE_ID'):
                values[key] = value.strip('"\'')
    if any(not values.get(key) for key in ('HONUA_AGENT_API_KEY', 'HONUA_AGENT_KEY_ID', 'POSTGRES_PASSWORD')):
        raise Blocked('installer did not issue all separate workspace/database credentials')
    if (values.get('HONUA_SERVER_IMAGE') != binding['server']['image'] or
            values.get('HONUA_CONSOLE_IMAGE') != binding['console']['image'] or
            values.get('HONUA_AGENT_RELEASE_ID') != binding['releaseId'] or
            '${HONUA_SERVER_IMAGE}' not in compose.read_text() or
            '${HONUA_CONSOLE_IMAGE}' not in compose.read_text()):
        raise Blocked('installed Compose/environment do not bind the selected immutable images and release')
    return {'installDirectory': str(directory), 'composeSha256': sha256(compose),
            'agentKeyId': values['HONUA_AGENT_KEY_ID']}


def observed_install_call(transcript: str) -> dict:
    calls = []
    results = {}
    for line in transcript.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        message = event.get('message')
        content = message.get('content') if isinstance(message, dict) else []
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get('type') == 'tool_use':
                    calls.append(item)
                elif isinstance(item, dict) and item.get('type') == 'tool_result':
                    results[item.get('tool_use_id')] = item
    installs = [call for call in calls if call.get('name') == 'mcp__honua__honua_agent_install_local']
    if len(installs) != 1 or installs[0].get('input', {}).get('confirm') is not True:
        raise Blocked('client transcript lacks exactly one confirmed native installer tool call')
    statuses = [call for call in calls[calls.index(installs[0]) + 1:]
                if call.get('name') == 'mcp__honua__honua_agent_status']
    if not statuses:
        raise Blocked('client did not inspect native installer status after the install call')
    try:
        installed = json.loads(results[installs[0]['id']]['content'])
        status = json.loads(results[statuses[-1]['id']]['content'])
    except (KeyError, ValueError, TypeError):
        raise Blocked('client transcript lacks authoritative installer and status tool results') from None
    if (results[installs[0]['id']].get('is_error') or results[statuses[-1]['id']].get('is_error') or
            installed.get('state') != 'attached' or status.get('state') != 'attached' or
            installed.get('target') != status.get('target') or not installed.get('target', {}).get('credentialId')):
        raise Blocked('real connector did not prove the same attached target after installation')
    return {'toolCallId': installs[0]['id'], 'target': installed['target']}


def phase_start(args, manifest, binding, harness, attempt):
    if attempt.exists() or args.install_dir.exists() or args.install_dir.is_symlink():
        raise Blocked('fresh attempt and install directories are required')
    attempt.mkdir(mode=0o700, parents=True)
    state = {'schemaVersion': SCHEMA, 'candidate': binding, 'harness': harness,
             'installDirectory': str(args.install_dir), 'stages': {'prepare': 'started'}}
    private_json(attempt / 'receipt.json', state, new=True)
    try:
        workspace = pins.resolve_client_workspace(manifest, attempt / 'published-clients')
        if workspace.status != 'pass':
            raise Blocked(workspace.reason or 'published clients unavailable')
        bindir, reason, notes = pins.install_executables(workspace, attempt / 'client-install')
        if bindir is None:
            raise Blocked(reason)
        mcp = client_entry(attempt / 'client-install', 'honua-agent-mcp')
        gateway = client_entry(attempt / 'client-install', 'honua-agent-gateway')
        state['publishedClients'] = workspace.as_receipt()
        state['publishedClients']['installNotes'] = notes
        state['executables'] = {'mcp': str(mcp), 'gateway': str(gateway)}
        state['stages']['prepare'] = 'pass'
        state['stages']['install'] = 'ready'
    except Exception as exc:
        state['stages']['prepare'] = 'blocked'
        state['blocker'] = str(exc)
        private_json(attempt / 'receipt.json', state)
        raise
    private_json(attempt / 'receipt.json', state)
    print('Published clients verified. Run install with this same --attempt and --manifest.')


def phase_install(args, state, binding, attempt):
    if state['stages'].get('install') != 'ready':
        raise Blocked('install was already started or its outcome is uncertain; reconcile the owned target without replay')
    install_dir = Path(state['installDirectory'])
    if install_dir.exists():
        raise Blocked('fresh install directory no longer absent')
    gateway_key_env = args.gateway_key_env
    if not gateway_key_env or len(os.environ.get(gateway_key_env, '')) < 32:
        raise Blocked('independent gateway key environment reference is missing or too short')
    gateway_url = urlsplit(args.gateway_url or '')
    if (gateway_url.scheme != 'http' or not gateway_url.hostname or not gateway_url.port or
            gateway_url.path not in ('', '/') or gateway_url.query or gateway_url.fragment or gateway_url.username):
        raise Blocked('private gateway HTTP origin is required')
    if args.gateway_listen_host not in ('127.0.0.1', '0.0.0.0', '::1'):
        raise Blocked('explicit supported gateway listen host is required')
    if not args.model_kind or not args.model_name:
        raise Blocked('real native model kind and name are required')
    mcp_config = {'mcpServers': {'honua': {'command': state['executables']['mcp'], 'args': [],
                  'env': {'HONUA_AGENT_INSTALL_DIRECTORY': str(install_dir),
                          'HONUA_AGENT_PREVIEW_SERVER_IMAGE': binding['server']['image'],
                          'HONUA_AGENT_PREVIEW_CONSOLE_IMAGE': binding['console']['image'],
                          'HONUA_AGENT_PREVIEW_RELEASE_ID': binding['releaseId'],
                          'HONUA_AGENT_PREVIEW_GATEWAY_COMMAND': state['executables']['gateway'],
                          'HONUA_AGENT_PREVIEW_GATEWAY_HTTP_URL': args.gateway_url,
                          'HONUA_AGENT_PREVIEW_GATEWAY_HTTP_KEY_ENV': gateway_key_env,
                          'HONUA_AGENT_PREVIEW_MODEL_KIND': args.model_kind,
                          'HONUA_AGENT_PREVIEW_MODEL_NAME': args.model_name}}}}
    if args.model_region:
        mcp_config['mcpServers']['honua']['env']['HONUA_AGENT_PREVIEW_MODEL_REGION'] = args.model_region
    if args.model_endpoint:
        mcp_config['mcpServers']['honua']['env']['HONUA_AGENT_PREVIEW_MODEL_ENDPOINT'] = args.model_endpoint
    if args.model_key_env:
        mcp_config['mcpServers']['honua']['env']['HONUA_AGENT_PREVIEW_MODEL_KEY_ENV'] = args.model_key_env
    private_json(attempt / 'mcp.json', mcp_config, new=True)
    state['stages']['install'] = 'started'
    private_json(attempt / 'receipt.json', state)
    prompt = ('Use only the registered Honua MCP server. Inspect this fresh machine, then call '
              'honua_agent_install_local exactly once with confirm true and the quickstart profile. '
              'The operator authorized this isolated install. Read honua_agent_status afterwards. '
              'Report actual tool results; never claim human pairing, data, map, or publication. '
              'If an outcome is uncertain, stop; never replay install.')
    allowed_tools = ['ToolSearch', 'ListMcpResourcesTool', 'ReadMcpResourceTool', 'WaitForMcpServers'] + [
        'mcp__honua__' + name for name in ('honua_agent_inspect_local', 'honua_agent_install_local',
                                          'honua_agent_status', 'honua_agent_connect')]
    command = [args.claude, '--restricted', '--tools', 'default', '--allowedTools', ','.join(allowed_tools),
               '--disallowedTools', 'Bash,PowerShell,Agent,Read,Write,Edit,NotebookEdit,Glob,Grep,LSP,WebFetch,WebSearch,Workflow,Task',
               '--setting-sources', '', '--settings',
               '{"disableAllHooks":true}', '--strict-mcp-config', '--mcp-config', str(attempt / 'mcp.json'),
               '--permission-mode', 'dontAsk', '--permission-prompts', 'none', '--no-session-persistence',
               '--output-format', 'stream-json', '--verbose', '--print']
    try:
        result = subprocess.run(command, input=prompt, text=True, capture_output=True, timeout=900,
                                cwd=attempt, check=False)
        (attempt / 'client-transcript.ndjson').write_text(result.stdout)
        (attempt / 'client-stderr.log').write_text(result.stderr)
        for name in ('client-transcript.ndjson', 'client-stderr.log'):
            (attempt / name).chmod(0o600)
        state['clientExit'] = result.returncode
        state['clientTranscriptSha256'] = sha256(attempt / 'client-transcript.ndjson')
        if result.returncode:
            raise Blocked('external client exited unsuccessfully; inspect private transcript and reconcile')
        observed = observed_install_call(result.stdout)
        state['installToolCallId'] = observed['toolCallId']
        state['connectorTarget'] = observed['target']
        state['target'] = installed_target(install_dir, binding)
        if state['connectorTarget']['credentialId'] != state['target']['agentKeyId']:
            raise Blocked('external connector attached with a different workspace credential identity')
        gateway_env = {**os.environ, 'HONUA_AGENT_INSTALL_DIRECTORY': str(install_dir),
                       'HONUA_AGENT_GATEWAY_KEY': os.environ[gateway_key_env],
                       'HONUA_AGENT_GATEWAY_HOST': args.gateway_listen_host,
                       'HONUA_AGENT_GATEWAY_PORT': str(gateway_url.port)}
        gateway_log = open(attempt / 'gateway.log', 'x')
        os.chmod(attempt / 'gateway.log', 0o600)
        try:
            gateway = subprocess.Popen([state['executables']['gateway']], cwd=attempt, env=gateway_env,
                                       stdout=gateway_log, stderr=subprocess.STDOUT, start_new_session=True)
        finally:
            gateway_log.close()
        time.sleep(1)
        if gateway.poll() is not None:
            raise Blocked('published native gateway exited on startup; inspect private gateway log')
        state['gateway'] = {'pid': gateway.pid, 'listenHost': args.gateway_listen_host,
                            'port': gateway_url.port, 'url': args.gateway_url}
        state['stages']['install'] = 'pass'
        state['stages']['pairing'] = 'awaiting-real-human-session'
        private_json(attempt / 'receipt.json', state)
        print('Install observed; complete real human pairing and provide a private Playwright storage state.')
    except Exception as exc:
        state['stages']['install'] = 'blocked'
        state['blocker'] = str(exc)
        private_json(attempt / 'receipt.json', state)
        raise


def phase_browser(args, state, manifest, binding, harness, attempt):
    if state['stages'].get('pairing') != 'awaiting-real-human-session':
        raise Blocked('pairing/browser stage is not ready or its outcome is uncertain')
    if not args.operator_state or not args.operator_state.is_file() or not args.input_file or not args.input_file.is_file():
        raise Blocked('real paired browser state and a local input file are required')
    if not args.origin or not re.fullmatch(r'http://127\.0\.0\.1:\d+', args.origin):
        raise Blocked('installed Console loopback origin required')
    if not args.server_origin or not re.fullmatch(r'http://127\.0\.0\.1:\d+', args.server_origin):
        raise Blocked('installed server loopback origin required')
    if not args.route or not re.fullmatch(r'/[a-z0-9][a-z0-9/_-]{0,126}', args.route):
        raise Blocked('fresh local publication route required')
    install_dir = Path(state['installDirectory'])
    target = installed_target(install_dir, binding)
    if target != state.get('target'):
        raise Blocked('installed target changed since installer proof')
    input_digest = sha256(args.input_file)
    gateway = state.get('gateway') or {}
    try:
        os.kill(gateway['pid'], 0)
    except (KeyError, OSError):
        raise Blocked('owned native gateway is not running') from None
    values = dict(line.split('=', 1) for line in (install_dir / '.env').read_text().splitlines()
                  if '=' in line and not line.startswith('#'))
    state['stages']['pairing'] = 'observed-private-state-file'
    state['stages']['browser'] = 'started'
    private_json(attempt / 'receipt.json', state)
    try:
        sdk_binding = sdk.prepare(manifest, attempt)
        connection = sdk.invoke(sdk_binding, 'CreateConnectionAsync', [{
            'name': 'native-first-use-' + hashlib.sha256(str(attempt).encode()).hexdigest()[:12],
            'host': 'postgres', 'databaseName': 'honua', 'username': 'honua',
            'password': values['POSTGRES_PASSWORD'], 'sslRequired': False, 'sslMode': 'Disable'}],
            base_url=args.server_origin, credential=values['HONUA_AGENT_API_KEY'])
        connection_id = connection['connectionId']
        health = sdk.invoke(sdk_binding, 'TestConnectionAsync', [connection_id],
                            base_url=args.server_origin, credential=values['HONUA_AGENT_API_KEY'])
        if health.get('connectionId') != connection_id or health.get('isHealthy') is not True:
            raise Blocked('published SDK did not verify the new managed connection')
        state['connectionId'] = connection_id
        state['sdk'] = {key: sdk_binding[key] for key in ('package', 'version', 'sha256')}
        private_json(attempt / 'receipt.json', state)
        env = {**os.environ, 'HONUA_WORKSPACE_E2E_ORIGIN': args.origin,
               'HONUA_WORKSPACE_E2E_OPERATOR_STATE': str(args.operator_state),
               'HONUA_WORKSPACE_E2E_IMPORT_FILE': str(args.input_file),
               'HONUA_WORKSPACE_E2E_CONNECTION': connection_id,
               'HONUA_WORKSPACE_E2E_PUBLICATION_ROUTE': args.route,
               'HONUA_WORKSPACE_E2E_CONSOLE_SHA': binding['console']['sha'],
               'HONUA_WORKSPACE_E2E_PUBLICATION_HANDOFF': str(attempt / 'publication-handoff.json')}
        command = playwright_command(args.console_root, 'studio-map.live.spec.ts')
        result = subprocess.run(command, cwd=args.console_root / 'e2e/playwright', env=env,
                                capture_output=True, text=True, timeout=900, check=False)
        (attempt / 'browser.log').write_text(result.stdout + '\n' + result.stderr)
        (attempt / 'browser.log').chmod(0o600)
        if result.returncode:
            raise Blocked('actual Console browser producer failed; inspect private browser log')
        handoff = read_json(attempt / 'publication-handoff.json')
        if (handoff.get('connectionId') != connection_id or handoff.get('inputSha256') != input_digest or
                sha256(args.input_file) != input_digest or handoff.get('route') != args.route or
                handoff.get('status') != 'awaiting-separate-human-approval' or
                any(not isinstance(handoff.get(key), str) or not handoff[key] for key in
                    ('proposalId', 'operationInstanceId', 'requestId', 'itemId', 'versionId',
                     'draftId', 'reopenedDraftId', 'contentHash', 'bodySha256'))):
            raise Blocked('browser handoff does not bind the new data and pending publication')
        state['publication'] = handoff
        state['stages']['browser'] = 'pass'
        state['stages']['approval'] = 'awaiting-separate-human-approval'
        private_json(attempt / 'receipt.json', state)
        print('Native map and publication proposal observed. A different human must approve in the real Console inbox.')
    except Exception as exc:
        state['stages']['browser'] = 'blocked'
        state['blocker'] = str(exc)
        private_json(attempt / 'receipt.json', state)
        raise


def phase_witness(args, state, binding, attempt):
    if state['stages'].get('approval') != 'awaiting-separate-human-approval':
        raise Blocked('no pending separate-human approval to witness')
    if not args.operator_state or not args.operator_state.is_file():
        raise Blocked('original paired operator browser state is required for read-only reconciliation')
    if not args.origin or not re.fullmatch(r'http://127\.0\.0\.1:\d+', args.origin):
        raise Blocked('installed Console loopback origin required')
    if not args.server_origin or not re.fullmatch(r'http://127\.0\.0\.1:\d+', args.server_origin):
        raise Blocked('installed server loopback origin required')
    install_dir = Path(state['installDirectory'])
    if installed_target(install_dir, binding) != state.get('target'):
        raise Blocked('installed target changed before the audience witness')
    values = dict(line.split('=', 1) for line in (install_dir / '.env').read_text().splitlines()
                  if '=' in line and not line.startswith('#'))
    handoff = read_json(attempt / 'publication-handoff.json')
    if handoff != state.get('publication'):
        raise Blocked('publication handoff changed since producer receipt')
    if (attempt / 'audience-witness.json').exists():
        raise Blocked('audience witness already exists; inspect it without replay')
    env = {**os.environ, 'HONUA_WORKSPACE_E2E_ORIGIN': args.origin,
           'HONUA_WORKSPACE_E2E_OPERATOR_STATE': str(args.operator_state),
           'HONUA_WORKSPACE_E2E_SERVER_ORIGIN': args.server_origin,
           'HONUA_WORKSPACE_E2E_AGENT_KEY': values['HONUA_AGENT_API_KEY'],
           'HONUA_WORKSPACE_E2E_PUBLICATION_HANDOFF': str(attempt / 'publication-handoff.json'),
           'HONUA_WORKSPACE_E2E_PUBLICATION_WITNESS': str(attempt / 'audience-witness.json')}
    result = subprocess.run(playwright_command(args.console_root, 'publication-witness.live.spec.ts'),
                            cwd=args.console_root / 'e2e/playwright', env=env,
                            capture_output=True, text=True, timeout=300, check=False)
    (attempt / 'witness.log').write_text(result.stdout + '\n' + result.stderr)
    (attempt / 'witness.log').chmod(0o600)
    if result.returncode:
        state['stages']['approval'] = 'awaiting-separate-human-approval'
        state['lastWitness'] = 'blocked-or-pending'
        private_json(attempt / 'receipt.json', state)
        raise Blocked('separate approval or audience witness absent; inspect actual proposal and private log')
    witness = read_json(attempt / 'audience-witness.json')
    if witness.get('status') != 'pass' or any(witness.get(key) != handoff.get(key) for key in
                                               ('proposalId', 'operationInstanceId', 'commandId', 'requestId', 'itemId', 'versionId',
                                                'contentHash', 'route')) or witness.get('requestedBy') == witness.get('resolvedBy'):
        state['stages']['approval'] = 'blocked'
        private_json(attempt / 'receipt.json', state)
        raise Blocked('audience witness does not bind the native draft or distinct human approval')
    state['audience'] = witness
    state['stages']['approval'] = 'pass'
    state['stages']['audience'] = 'pass'
    state['status'] = 'pass'
    private_json(attempt / 'receipt.json', state)
    print('One-attempt first-use source, native draft, separate approval and anonymous audience matched.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('start', 'install', 'browser', 'witness'))
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--attempt', type=Path, required=True)
    parser.add_argument('--install-dir', type=Path)
    parser.add_argument('--console-root', type=Path, required=True)
    parser.add_argument('--console-sha', required=True)
    parser.add_argument('--claude', default='claude')
    parser.add_argument('--gateway-url')
    parser.add_argument('--gateway-key-env')
    parser.add_argument('--gateway-listen-host')
    parser.add_argument('--model-kind')
    parser.add_argument('--model-name')
    parser.add_argument('--model-region')
    parser.add_argument('--model-endpoint')
    parser.add_argument('--model-key-env')
    parser.add_argument('--operator-state', type=Path)
    parser.add_argument('--input-file', type=Path)
    parser.add_argument('--origin')
    parser.add_argument('--server-origin')
    parser.add_argument('--route')
    args = parser.parse_args()
    try:
        args.attempt = args.attempt.resolve()
        if args.install_dir:
            args.install_dir = args.install_dir.resolve()
        if args.operator_state:
            args.operator_state = args.operator_state.resolve()
        if args.input_file:
            args.input_file = args.input_file.resolve()
        args.console_root = args.console_root.resolve()
        manifest, binding = candidate(args.manifest)
        harness = check_harness(args.console_root, args.console_sha)
        if args.phase == 'start':
            if not args.install_dir:
                raise Blocked('--install-dir is required for start')
            phase_start(args, manifest, binding, harness, args.attempt)
        else:
            state = read_json(args.attempt / 'receipt.json')
            check_state(state, binding, harness)
            if args.phase == 'install':
                phase_install(args, state, binding, args.attempt)
            elif args.phase == 'browser':
                phase_browser(args, state, manifest, binding, harness, args.attempt)
            else:
                phase_witness(args, state, binding, args.attempt)
    except (Blocked, pins.PinError, OSError, KeyError, ValueError, subprocess.SubprocessError) as exc:
        print(f'blocked: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
