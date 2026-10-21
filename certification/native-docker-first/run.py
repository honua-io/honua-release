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
from urllib.request import urlopen

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
                       'HONUA_SERVER_IMAGE', 'HONUA_CONSOLE_IMAGE', 'HONUA_AGENT_RELEASE_ID',
                       'HONUA_HTTP_PORT', 'HONUA_CONSOLE_PORT', 'HONUA_COMPOSE_PROJECT'):
                values[key] = value.strip('"\'')
    if any(not values.get(key) for key in ('HONUA_AGENT_API_KEY', 'HONUA_AGENT_KEY_ID', 'POSTGRES_PASSWORD')):
        raise Blocked('installer did not issue all separate workspace/database credentials')
    if (values.get('HONUA_SERVER_IMAGE') != binding['server']['image'] or
            values.get('HONUA_CONSOLE_IMAGE') != binding['console']['image'] or
            values.get('HONUA_AGENT_RELEASE_ID') != binding['releaseId'] or
            '${HONUA_SERVER_IMAGE}' not in compose.read_text() or
            '${HONUA_CONSOLE_IMAGE}' not in compose.read_text()):
        raise Blocked('installed Compose/environment do not bind the selected immutable images and release')
    project = values.get('HONUA_COMPOSE_PROJECT', '')
    if not re.fullmatch(r'honua-local-[a-f0-9]{24}', project):
        raise Blocked('fresh installer Compose project identity missing')
    if any(not re.fullmatch(r'[0-9]{1,5}', values.get(key, '')) or
           not 0 < int(values[key]) < 65536 for key in ('HONUA_HTTP_PORT', 'HONUA_CONSOLE_PORT')):
        raise Blocked('installed server and Console ports missing')
    origins = {'server': f"http://127.0.0.1:{values['HONUA_HTTP_PORT']}",
               'console': f"http://127.0.0.1:{values['HONUA_CONSOLE_PORT']}"}
    return {'installDirectory': str(directory), 'composeSha256': sha256(compose),
            'agentKeyId': values['HONUA_AGENT_KEY_ID'], 'composeProject': project,
            'origins': origins, 'containers': running_containers(directory, project, binding,
                {'server': values['HONUA_HTTP_PORT'], 'console': values['HONUA_CONSOLE_PORT']}),
            'consoleSourceSha': live_console_sha(origins['console'])}


def docker_json(command: list[str], directory: Path):
    result = subprocess.run(['docker', *command], cwd=directory, text=True,
                            capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise Blocked(f'Docker observation failed for {command[0]}')
    return json.loads(result.stdout)


def running_containers(directory: Path, project: str, binding: dict, ports: dict) -> dict:
    result = subprocess.run(['docker', 'compose', '--project-name', project,
                             '--project-directory', str(directory), '--file', str(directory / 'compose.yaml'),
                             'ps', '--format', 'json'], cwd=directory, text=True,
                            capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise Blocked('owned Compose project is not observable')
    raw = result.stdout.strip()
    try:
        parsed = json.loads(raw)
        entries = parsed if isinstance(parsed, list) else [parsed]
    except ValueError:
        entries = [json.loads(line) for line in raw.splitlines()]
    observed = {}
    for service, component in (('honua', 'server'), ('console', 'console')):
        matches = [entry for entry in entries if entry.get('Service') == service and
                   entry.get('State') == 'running']
        if len(matches) != 1 or not matches[0].get('ID'):
            raise Blocked(f'owned Compose {service} container is not uniquely running')
        inspect = docker_json(['container', 'inspect', matches[0]['ID']], directory)
        if not isinstance(inspect, list) or len(inspect) != 1:
            raise Blocked(f'Docker {service} inspect is incomplete')
        item = inspect[0]
        labels = item.get('Config', {}).get('Labels') or {}
        env = item.get('Config', {}).get('Env') or []
        published = item.get('NetworkSettings', {}).get('Ports', {}).get('8080/tcp') or []
        if len(published) != 1 or published[0].get('HostIp') != '127.0.0.1' or published[0].get('HostPort') != ports[component]:
            raise Blocked(f'running {service} is not bound to the installed loopback origin')
        releases = [value.split('=', 1)[1] for value in env if
                    value.startswith(('HONUA_AGENT_RELEASE_ID=', 'AgentWorkspace__ReleaseId='))]
        if (item.get('Id') != matches[0]['ID'] or not item.get('State', {}).get('Running') or
                item.get('Config', {}).get('Image') != binding[component]['image'] or
                labels.get('com.docker.compose.project') != project or
                labels.get('com.docker.compose.service') != service or
                binding['releaseId'] not in releases or not item.get('Image')):
            raise Blocked(f'running {service} container does not bind selected image/release/project')
        observed[component] = {'id': item['Id'], 'imageId': item['Image'],
                               'image': item['Config']['Image'], 'releaseId': binding['releaseId']}
    return observed


def live_console_sha(origin: str) -> str:
    try:
        with urlopen(origin + '/version.json', timeout=10) as response:
            version = json.load(response)
    except (OSError, ValueError) as exc:
        raise Blocked(f'installed Console version is not observable: {exc}') from exc
    if not isinstance(version, dict) or not SHA.fullmatch(version.get('commit', '')):
        raise Blocked('installed Console did not report an exact source SHA')
    return version['commit']


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
    inspections = [call for call in calls[:calls.index(installs[0])]
                   if call.get('name') == 'mcp__honua__honua_agent_inspect_local']
    if not inspections or inspections[-1].get('id') not in results or results[inspections[-1]['id']].get('is_error'):
        raise Blocked('client transcript lacks successful prerequisite inspection')
    statuses = [call for call in calls[calls.index(installs[0]) + 1:]
                if call.get('name') == 'mcp__honua__honua_agent_status']
    if not statuses:
        raise Blocked('client did not inspect native installer status after the install call')
    try:
        inspected = json.loads(results[inspections[-1]['id']]['content'])
        installed = json.loads(results[installs[0]['id']]['content'])
        status = json.loads(results[statuses[-1]['id']]['content'])
    except (KeyError, ValueError, TypeError):
        raise Blocked('client transcript lacks authoritative installer and status tool results') from None
    if (not isinstance(inspected, dict) or not inspected or
            results[installs[0]['id']].get('is_error') or results[statuses[-1]['id']].get('is_error') or
            installed.get('state') != 'attached' or status.get('state') != 'attached' or
            installed.get('target') != status.get('target') or not installed.get('target', {}).get('credentialId')):
        raise Blocked('real connector did not prove the same attached target after installation')
    return {'inspectionToolCallId': inspections[-1]['id'],
            'toolCallId': installs[0]['id'], 'target': installed['target']}


def observed_client_session(transcript: str, expected: str) -> None:
    sessions = {event.get('session_id') for line in transcript.splitlines()
                for event in [json.loads(line)] if isinstance(event, dict) and
                event.get('type') == 'system' and event.get('subtype') == 'init'}
    if sessions != {expected}:
        raise Blocked('external client did not use the same persistent conversation')


def observed_agent_task(transcript: str, workspace_id: str, turn_id: str,
                        intent_id: str, input_file: Path) -> dict:
    calls = []
    results = {}
    for line in transcript.splitlines():
        try: event = json.loads(line)
        except ValueError: continue
        if not isinstance(event, dict): continue
        message = event.get('message')
        for item in message.get('content', []) if isinstance(message, dict) else []:
            if isinstance(item, dict) and item.get('type') == 'tool_use': calls.append(item)
            elif isinstance(item, dict) and item.get('type') == 'tool_result': results[item.get('tool_use_id')] = item
    names = ('honua_workspace_conversation', 'honua_workspace_get_turn',
             'honua_agent_connect', 'honua_workspace_create_dataset_import_task')
    matches = [[call for call in calls if call.get('name') == 'mcp__honua__' + name] for name in names]
    if any(len(group) != 1 for group in matches):
        raise Blocked('same external client did not execute the exact read, bind and task sequence once')
    conversation, turn, bind, task = (group[0] for group in matches)
    if not (calls.index(conversation) < calls.index(turn) < calls.index(bind) < calls.index(task)):
        raise Blocked('external client task sequence was not ordered after the human turn read')
    if (conversation.get('input', {}).get('workspaceId') != workspace_id or
            turn.get('input') != {'workspaceId': workspace_id, 'turnId': turn_id} or
            bind.get('input') != {'workspaceId': workspace_id, 'turnId': turn_id}):
        raise Blocked('external client read or bound a different human workspace turn')
    def output(call):
        result = results.get(call.get('id'))
        if not result or result.get('is_error'):
            raise Blocked('external client lacks an authoritative successful tool result')
        return json.loads(result['content'])
    conversation_value, turn_value, bound, created = (output(call) for call in
                                                       (conversation, turn, bind, task))
    if (not any(item.get('turnId') == turn_id and item.get('role') == 'user' for item in
                conversation_value.get('messages', [])) or turn_value.get('turnId') != turn_id or
            bound.get('state') != 'attached' or bound.get('turnBinding') !=
            {'workspaceId': workspace_id, 'turnId': turn_id}):
        raise Blocked('conversation, immutable turn or connector binding was not observed')
    selection = turn_value.get('browserFileSelection') or {}
    expected = {'fileName': input_file.name, 'totalBytes': input_file.stat().st_size,
                'expectedSha256': sha256(input_file)}
    if (any(selection.get(key) != value for key, value in expected.items()) or
            not isinstance(selection.get('selectionId'), str) or not selection['selectionId']):
        raise Blocked('human browser selection does not match the original local input')
    context = turn_value.get('context') or {}
    request = task.get('input', {}).get('request') or {}
    source = request.get('source') or {}
    created_source = created.get('source') or {}
    source_keys = {'kind', 'fileName', 'totalBytes', 'expectedSha256', 'artifact',
                   'browserSelectionId', 'serverJobArtifact'}
    def without_nulls(value):
        return {key: item for key, item in value.items() if item is not None}
    if (task.get('input', {}).get('workspaceId') != workspace_id or
            context.get('workspaceId') != workspace_id or not context.get('targetId') or
            request.get('intentId') != intent_id or request.get('viewId') != context.get('viewId') or
            request.get('contextRevision') != context.get('revision') or source.get('kind') != 'browser-file' or
            source.get('browserSelectionId') != selection['selectionId'] or
            any(source.get(key) != value for key, value in expected.items()) or
            not set(source) <= source_keys or not set(created_source) <= source_keys or
            source.get('artifact') is not None or source.get('serverJobArtifact') is not None or
            created_source.get('artifact') is not None or created_source.get('serverJobArtifact') is not None or
            without_nulls(created_source) != without_nulls(source) or
            created.get('workspaceId') != workspace_id or created.get('targetId') != context['targetId'] or
            created.get('tenantId') != context.get('tenantId') or
            created.get('destination') != request.get('destination') or
            not created.get('taskId') or
            not isinstance(created.get('revision'), int) or not created.get('contentHash')):
        raise Blocked('created import task did not bind the observed browser selection and intent')
    return {'workspaceId': workspace_id, 'turnId': turn_id, 'intentId': intent_id,
            'selection': selection, 'taskId': created['taskId'],
            'revision': created['revision'], 'contentHash': created['contentHash'],
            'destination': created.get('destination')}


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
        bindir, reason, notes = pins.install_executables(
            workspace, attempt / 'client-install', require_default_peers=True)
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
    state['mcpConfigSha256'] = sha256(attempt / 'mcp.json')
    state['mcpExecutableSha256'] = sha256(Path(state['executables']['mcp']))
    state['stages']['install'] = 'started'
    state['externalClientSessionId'] = str(uuid.uuid4())
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
               '--permission-mode', 'dontAsk', '--permission-prompts', 'none',
               '--session-id', state['externalClientSessionId'],
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
        observed_client_session(result.stdout, state['externalClientSessionId'])
        observed = observed_install_call(result.stdout)
        state['installToolCallId'] = observed['toolCallId']
        state['connectorTarget'] = observed['target']
        state['target'] = installed_target(install_dir, binding)
        if state['target']['consoleSourceSha'] != binding['console']['sha']:
            raise Blocked('running Console source differs from the selected image candidate')
        if state['connectorTarget']['credentialId'] != state['target']['agentKeyId']:
            raise Blocked('external connector attached with a different workspace credential identity')
        if urlsplit(state['connectorTarget'].get('endpoint', ''))._replace(path='', query='', fragment='').geturl().rstrip('/') != state['target']['origins']['server']:
            raise Blocked('external connector points to a different running server origin')
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
        state['stages']['agentTask'] = 'awaiting-human-browser-file-turn'
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
    if args.origin != target['origins']['console'] or args.server_origin != target['origins']['server']:
        raise Blocked('browser origins differ from installed Compose ports')
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


def phase_agent_task(args, state, binding, attempt):
    if state['stages'].get('agentTask') != 'awaiting-human-browser-file-turn':
        raise Blocked('external client task stage is not ready or its outcome is uncertain')
    if (not args.input_file or not args.input_file.is_file() or
            not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', args.workspace_id or '') or
            not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', args.turn_id or '')):
        raise Blocked('actual local input and exact human workspace/turn IDs are required')
    if installed_target(Path(state['installDirectory']), binding) != state.get('target'):
        raise Blocked('installed target changed before the external client continuation')
    mcp_config = attempt / 'mcp.json'
    mcp_executable = Path(state['executables']['mcp'])
    if (not mcp_config.is_file() or not mcp_executable.is_file() or
            sha256(mcp_config) != state.get('mcpConfigSha256') or
            sha256(mcp_executable) != state.get('mcpExecutableSha256')):
        raise Blocked('external client registration/configuration changed before continuation')
    intent_id = 'native-first-' + uuid.uuid4().hex
    input_digest = sha256(args.input_file)
    state['stages']['agentTask'] = 'started'
    state['agentTaskIntentId'] = intent_id
    private_json(attempt / 'receipt.json', state)
    prompt = (f'The same paired human has selected a local GIS file and sent a new Console turn. '
              f'Use only the registered Honua MCP tools in this same conversation. First call '
              f'honua_workspace_conversation for workspaceId {args.workspace_id}, then '
              f'honua_workspace_get_turn for turnId {args.turn_id}. Inspect the actual '
              f'browserFileSelection; it must name {args.input_file.name}, contain '
              f'{args.input_file.stat().st_size} bytes and SHA-256 {input_digest}. '
              f'Call honua_agent_connect with exactly this workspaceId and turnId to bind the '
              f'authorized turn, then create exactly one browser-file dataset-import task '
              f'with intentId {intent_id}, that selection, its captured viewId/contextRevision, '
              f'and a new public schema/table destination. Do not upload, submit or publish yet. '
              f'If any result is denied or uncertain, stop without retrying the mutation.')
    allowed = ['ToolSearch', 'ListMcpResourcesTool', 'ReadMcpResourceTool', 'WaitForMcpServers'] + [
        'mcp__honua__' + name for name in ('honua_agent_status', 'honua_agent_connect',
            'honua_workspace_conversation', 'honua_workspace_get_turn',
            'honua_workspace_create_dataset_import_task', 'honua_workspace_get_dataset_import_intent')]
    command = [args.claude, '--restricted', '--tools', 'default', '--allowedTools', ','.join(allowed),
               '--disallowedTools', 'Bash,PowerShell,Agent,Read,Write,Edit,NotebookEdit,Glob,Grep,LSP,WebFetch,WebSearch,Workflow,Task',
               '--setting-sources', '', '--settings', '{"disableAllHooks":true}', '--strict-mcp-config',
               '--mcp-config', str(attempt / 'mcp.json'), '--permission-mode', 'dontAsk',
               '--permission-prompts', 'none', '--resume', state['externalClientSessionId'],
               '--output-format', 'stream-json', '--verbose', '--print']
    try:
        result = subprocess.run(command, input=prompt, text=True, capture_output=True,
                                timeout=900, cwd=attempt, check=False)
        transcript = attempt / 'agent-task-transcript.ndjson'
        transcript.write_text(result.stdout)
        transcript.chmod(0o600)
        (attempt / 'agent-task-stderr.log').write_text(result.stderr)
        (attempt / 'agent-task-stderr.log').chmod(0o600)
        state['agentTaskTranscriptSha256'] = sha256(transcript)
        if result.returncode:
            raise Blocked('resumed external client failed; reconcile the original import intent without replay')
        observed_client_session(result.stdout, state['externalClientSessionId'])
        task = observed_agent_task(result.stdout, args.workspace_id, args.turn_id,
                                   intent_id, args.input_file)
        if sha256(args.input_file) != input_digest:
            raise Blocked('original local input changed during external client task creation')
        state['agentImportTask'] = task
        state['stages']['agentTask'] = 'pass'
        state['stages']['agentSubmission'] = 'awaiting-real-human-upload-and-prepare'
        private_json(attempt / 'receipt.json', state)
        print('Same external client created a turn-bound import task. Human must upload and prepare its exact source.')
    except Exception as exc:
        state['stages']['agentTask'] = 'blocked'
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
    target = installed_target(install_dir, binding)
    if target != state.get('target'):
        raise Blocked('installed target changed before the audience witness')
    if args.origin != target['origins']['console'] or args.server_origin != target['origins']['server']:
        raise Blocked('witness origins differ from installed Compose ports')
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
    if (witness.get('status') != 'pass' or any(witness.get(key) != handoff.get(key) for key in
                                               ('proposalId', 'operationInstanceId', 'commandId', 'requestId', 'itemId', 'versionId',
                                                'contentHash', 'route')) or
            not isinstance(handoff.get('requestedBy'), str) or not handoff['requestedBy'].strip() or
            witness.get('requestedBy') != handoff['requestedBy'] or
            not isinstance(witness.get('resolvedBy'), str) or not witness['resolvedBy'].strip() or
            witness['requestedBy'] == witness['resolvedBy']):
        state['stages']['approval'] = 'blocked'
        private_json(attempt / 'receipt.json', state)
        raise Blocked('audience witness does not bind the native draft or distinct human approval')
    state['audience'] = witness
    state['stages']['approval'] = 'pass'
    state['stages']['audience'] = 'pass'
    state['status'] = 'joined-native-console-integration-pass'
    state['oneExternalAgentConversationQualified'] = False
    private_json(attempt / 'receipt.json', state)
    print('Joined native Console integration matched; one external-agent conversation remains unqualified.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('start', 'install', 'agent-task', 'browser', 'witness'))
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
    parser.add_argument('--workspace-id')
    parser.add_argument('--turn-id')
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
            elif args.phase == 'agent-task':
                phase_agent_task(args, state, binding, args.attempt)
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
