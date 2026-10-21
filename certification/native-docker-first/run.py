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
SERVICE_PROMPT = 'Create a service from this imported dataset.'
MAP_PROMPT = 'Create and style a map from the service I just published.'


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
                       'HONUA_HTTP_PORT', 'HONUA_CONSOLE_PORT', 'HONUA_COMPOSE_PROJECT',
                       'HONUA_NATIVE_MODEL_ENABLED', 'HONUA_AGENT_GATEWAY_URL'):
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
            'nativeModelEnabled': values.get('HONUA_NATIVE_MODEL_ENABLED'),
            'nativeGatewayConfigured': bool(values.get('HONUA_AGENT_GATEWAY_URL')),
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


def ordered_tool_events(transcript: str):
    calls, results, call_positions, result_positions = [], {}, {}, {}
    position = 0
    for line in transcript.splitlines():
        try: event = json.loads(line)
        except ValueError: continue
        if not isinstance(event, dict): continue
        message = event.get('message')
        for item in message.get('content', []) if isinstance(message, dict) else []:
            if not isinstance(item, dict): continue
            position += 1
            if item.get('type') == 'tool_use':
                identifier = item.get('id')
                if not isinstance(identifier, str) or not identifier or identifier in call_positions:
                    raise Blocked('external client transcript has missing or duplicate tool call IDs')
                calls.append(item)
                call_positions[identifier] = position
            elif item.get('type') == 'tool_result':
                identifier = item.get('tool_use_id')
                if not isinstance(identifier, str) or identifier not in call_positions or identifier in results:
                    raise Blocked('external client transcript has unmatched or duplicate tool results')
                results[identifier] = item
                result_positions[identifier] = position
    return calls, results, call_positions, result_positions


def require_results_before_dependencies(sequence, results, call_positions, result_positions):
    if (any(call.get('id') not in result_positions or results[call['id']].get('is_error') or
           not call_positions[call['id']] < result_positions[call['id']] < call_positions[next_call['id']]
           for call, next_call in zip(sequence, sequence[1:])) or
            sequence[-1].get('id') not in result_positions or results[sequence[-1]['id']].get('is_error')):
        raise Blocked('external client did not observe each successful tool result before its dependent call')


def observed_agent_task(transcript: str, workspace_id: str, turn_id: str,
                        intent_id: str, input_file: Path) -> dict:
    calls, results, call_positions, result_positions = ordered_tool_events(transcript)
    names = ('honua_workspace_conversation', 'honua_workspace_get_turn',
             'honua_agent_connect', 'honua_workspace_create_dataset_import_task')
    matches = [[call for call in calls if call.get('name') == 'mcp__honua__' + name] for name in names]
    if any(len(group) != 1 for group in matches):
        raise Blocked('same external client did not execute the exact read, bind and task sequence once')
    conversation, turn, bind, task = (group[0] for group in matches)
    if not (calls.index(conversation) < calls.index(turn) < calls.index(bind) < calls.index(task)):
        raise Blocked('external client task sequence was not ordered after the human turn read')
    require_results_before_dependencies((conversation, turn, bind, task), results,
                                        call_positions, result_positions)
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
            not isinstance(created.get('ownerActorId'), str) or not created['ownerActorId'] or
            not created.get('taskId') or
            not isinstance(created.get('revision'), int) or not created.get('contentHash')):
        raise Blocked('created import task did not bind the observed browser selection and intent')
    return {'workspaceId': workspace_id, 'turnId': turn_id, 'intentId': intent_id,
            'targetId': context['targetId'], 'tenantId': context['tenantId'],
            'selection': selection, 'taskId': created['taskId'],
            'revision': created['revision'], 'contentHash': created['contentHash'],
            'destination': created.get('destination'), 'ownerActorId': created['ownerActorId']}


def phase_start(args, manifest, binding, harness, attempt):
    if attempt.exists() or args.install_dir.exists() or args.install_dir.is_symlink():
        raise Blocked('fresh attempt and install directories are required')
    attempt.mkdir(mode=0o700, parents=True)
    state = {'schemaVersion': SCHEMA, 'candidate': binding, 'harness': harness,
             'installDirectory': str(args.install_dir),
             'conversationMode': args.conversation_mode or 'joined-native',
             'stages': {'prepare': 'started'}}
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
    joined = state['conversationMode'] == 'joined-native'
    gateway_key_env = args.gateway_key_env
    gateway_url = urlsplit(args.gateway_url or '')
    if joined:
        if not gateway_key_env or len(os.environ.get(gateway_key_env, '')) < 32:
            raise Blocked('independent gateway key environment reference is missing or too short')
        if (gateway_url.scheme != 'http' or not gateway_url.hostname or not gateway_url.port or
                gateway_url.path not in ('', '/') or gateway_url.query or gateway_url.fragment or gateway_url.username):
            raise Blocked('private gateway HTTP origin is required')
        if args.gateway_listen_host not in ('127.0.0.1', '0.0.0.0', '::1'):
            raise Blocked('explicit supported gateway listen host is required')
        if not args.model_kind or not args.model_name:
            raise Blocked('real native model kind and name are required')
    elif any((args.gateway_url, args.gateway_key_env, args.gateway_listen_host,
              args.model_kind, args.model_name, args.model_region, args.model_endpoint,
              args.model_key_env)):
        raise Blocked('external-agent mode cannot configure or start a second native model/gateway')
    mcp_config = {'mcpServers': {'honua': {'command': state['executables']['mcp'], 'args': [],
                  'env': {'HONUA_AGENT_INSTALL_DIRECTORY': str(install_dir),
                          'HONUA_AGENT_PREVIEW_SERVER_IMAGE': binding['server']['image'],
                          'HONUA_AGENT_PREVIEW_CONSOLE_IMAGE': binding['console']['image'],
                          'HONUA_AGENT_PREVIEW_RELEASE_ID': binding['releaseId'],
                          'HONUA_AGENT_PREVIEW_GATEWAY_COMMAND': state['executables']['gateway'],
                          **({'HONUA_AGENT_PREVIEW_GATEWAY_HTTP_URL': args.gateway_url,
                              'HONUA_AGENT_PREVIEW_GATEWAY_HTTP_KEY_ENV': gateway_key_env,
                              'HONUA_AGENT_PREVIEW_MODEL_KIND': args.model_kind,
                              'HONUA_AGENT_PREVIEW_MODEL_NAME': args.model_name} if joined else {})}}}}
    if joined and args.model_region:
        mcp_config['mcpServers']['honua']['env']['HONUA_AGENT_PREVIEW_MODEL_REGION'] = args.model_region
    if joined and args.model_endpoint:
        mcp_config['mcpServers']['honua']['env']['HONUA_AGENT_PREVIEW_MODEL_ENDPOINT'] = args.model_endpoint
    if joined and args.model_key_env:
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
        if (state['target']['nativeModelEnabled'] != ('true' if joined else 'false') or
                state['target']['nativeGatewayConfigured'] != joined):
            raise Blocked('installed target native model/gateway differs from the persisted conversation mode')
        if state['target']['consoleSourceSha'] != binding['console']['sha']:
            raise Blocked('running Console source differs from the selected image candidate')
        if state['connectorTarget']['credentialId'] != state['target']['agentKeyId']:
            raise Blocked('external connector attached with a different workspace credential identity')
        if urlsplit(state['connectorTarget'].get('endpoint', ''))._replace(path='', query='', fragment='').geturl().rstrip('/') != state['target']['origins']['server']:
            raise Blocked('external connector points to a different running server origin')
        if joined:
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
    if state.get('conversationMode') != 'joined-native':
        raise Blocked('native browser smoke requires the separate joined-native installation mode')
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


def observed_agent_submission(transcript: str, original: dict) -> dict:
    calls, results, call_positions, result_positions = ordered_tool_events(transcript)
    binds = [call for call in calls if call.get('name') == 'mcp__honua__honua_agent_connect']
    names = ('get_dataset_import_authority', 'get_dataset_import_submission',
             'submit_dataset_import')
    matches = [[call for call in calls if call.get('name') == 'mcp__honua__honua_workspace_' + name]
               for name in names]
    authority_calls, read_calls, submits = matches
    if len(binds) != 1 or len(authority_calls) != 1 or len(read_calls) != 2 or len(submits) != 1:
        raise Blocked('external client did not inspect authority, submit once, and read back the original task')
    bind, authority_call, before, after, submit = binds[0], authority_calls[0], read_calls[0], read_calls[1], submits[0]
    if not (calls.index(bind) < calls.index(authority_call) < calls.index(before) <
            calls.index(submit) < calls.index(after)):
        raise Blocked('external client submission sequence is not ordered')
    require_results_before_dependencies((bind, authority_call, before, submit, after),
                                        results, call_positions, result_positions)
    reference = {'workspaceId': original['workspaceId'], 'taskId': original['taskId'],
                 'revision': original['revision'], 'contentHash': original['contentHash']}
    if any(call.get('input') != reference for call in (authority_call, before, after)):
        raise Blocked('external client inspected a different immutable import task')
    def output(call):
        result = results.get(call.get('id'))
        if not result or result.get('is_error'):
            raise Blocked('external client lacks a successful authoritative submission result')
        return json.loads(result['content'])
    bound, authority, prepared, submitted, accepted = (output(call) for call in
                                                       (bind, authority_call, before, submit, after))
    if (bind.get('input') != {'workspaceId': original['workspaceId'], 'turnId': original['turnId']} or
            bound.get('state') != 'attached' or bound.get('turnBinding') != bind['input']):
        raise Blocked('reconnected client did not bind the original authorized turn')
    identity = {'taskId': original['taskId'], 'taskRevision': original['revision'],
                'taskContentHash': original['contentHash']}
    if (any(authority.get(key) != value or prepared.get(key) != value or
            submitted.get(key) != value or accepted.get(key) != value
            for key, value in identity.items()) or
            authority.get('originatingTurnId') != original['turnId'] or
            not original.get('ownerActorId') or authority.get('executorActorId') != original['ownerActorId'] or
            not authority.get('preparedOperationId') or
            prepared.get('state') != 'prepared' or prepared.get('operationId') != authority['preparedOperationId']):
        raise Blocked('prepared task does not bind the original agent and human turn')
    reviewed = {key: authority.get(key) for key in
                ('authorityRevision', 'authorityFingerprint', 'policyContextFingerprint')}
    if (not isinstance(reviewed['authorityRevision'], int) or reviewed['authorityRevision'] < 1 or
            any(not isinstance(reviewed[key], str) or not DIGEST.fullmatch(reviewed[key])
                for key in ('authorityFingerprint', 'policyContextFingerprint')) or
            any(prepared.get(key) != value for key, value in reviewed.items())):
        raise Blocked('prepared submission authority differs from the observed task authority')
    request = {'taskContentHash': original['contentHash'], **reviewed}
    if submit.get('input') != {key: reference[key] for key in ('workspaceId', 'taskId', 'revision')} | {'request': request}:
        raise Blocked('external client submitted a different task or authority')
    if (submitted.get('state') != 'accepted' or accepted.get('state') != 'accepted' or
            submitted.get('operationId') != authority['preparedOperationId'] or
            accepted.get('operationId') != authority['preparedOperationId'] or
            any(submitted.get(key) != value or accepted.get(key) != value
                for key, value in reviewed.items())):
        raise Blocked('submitted import lacks matching authoritative accepted readback')
    submitted_job, job = submitted.get('job') or {}, accepted.get('job') or {}
    if (not job.get('jobId') or job.get('jobId') != authority['preparedOperationId'] or
            submitted_job.get('jobId') != job['jobId'] or submitted_job.get('leaseId') != job.get('leaseId') or
            not job.get('leaseId') or job.get('status') not in
            ('queued', 'provisioning', 'running', 'succeeded')):
        raise Blocked('submitted import lacks an actual durable accepted job')
    return {'taskId': original['taskId'], 'revision': original['revision'],
            'contentHash': original['contentHash'], 'jobId': job['jobId'],
            'leaseId': job['leaseId'], 'status': job['status'],
            'operationId': authority['preparedOperationId']}


def phase_agent_submit(args, state, binding, attempt):
    if state['stages'].get('agentSubmission') != 'awaiting-real-human-upload-and-prepare':
        raise Blocked('external client submission stage is not ready or its outcome is uncertain')
    original = state.get('agentImportTask') or {}
    if not original.get('taskId') or not original.get('contentHash') or not original.get('ownerActorId'):
        raise Blocked('original external-client import task is missing')
    if installed_target(Path(state['installDirectory']), binding) != state.get('target'):
        raise Blocked('installed target changed before the external client submission')
    mcp_config = attempt / 'mcp.json'
    mcp_executable = Path(state['executables']['mcp'])
    if (not mcp_config.is_file() or not mcp_executable.is_file() or
            sha256(mcp_config) != state.get('mcpConfigSha256') or
            sha256(mcp_executable) != state.get('mcpExecutableSha256')):
        raise Blocked('external client registration/configuration changed before submission')
    state['stages']['agentSubmission'] = 'started'
    private_json(attempt / 'receipt.json', state)
    prompt = (f'The human has uploaded and prepared the original browser-file task. In this same '
              f'Honua conversation, first reconnect with honua_agent_connect using workspaceId '
              f'{original["workspaceId"]} and turnId {original["turnId"]}. Then inspect exact authority for '
              f'workspaceId {original["workspaceId"]}, taskId {original["taskId"]}, revision '
              f'{original["revision"]}, contentHash {original["contentHash"]}. Read the original '
              f'submission receipt; proceed only if prepared, never if already accepted or unknown. '
              f'Explicitly call honua_workspace_submit_dataset_import once with the exact current '
              f'authority revision and fingerprints, then read back that same submission receipt. '
              f'Do not create another task, upload, create a service, map, or publish in this step. '
              f'If denied or uncertain, stop without retrying the mutation.')
    allowed = ['ToolSearch', 'ListMcpResourcesTool', 'ReadMcpResourceTool', 'WaitForMcpServers'] + [
        'mcp__honua__' + name for name in ('honua_agent_connect', 'honua_agent_status', 'honua_workspace_get_dataset_import_authority',
            'honua_workspace_get_dataset_import_submission', 'honua_workspace_submit_dataset_import')]
    command = [args.claude, '--restricted', '--tools', 'default', '--allowedTools', ','.join(allowed),
               '--disallowedTools', 'Bash,PowerShell,Agent,Read,Write,Edit,NotebookEdit,Glob,Grep,LSP,WebFetch,WebSearch,Workflow,Task',
               '--setting-sources', '', '--settings', '{"disableAllHooks":true}', '--strict-mcp-config',
               '--mcp-config', str(mcp_config), '--permission-mode', 'dontAsk', '--permission-prompts', 'none',
               '--resume', state['externalClientSessionId'], '--output-format', 'stream-json', '--verbose', '--print']
    try:
        result = subprocess.run(command, input=prompt, text=True, capture_output=True,
                                timeout=900, cwd=attempt, check=False)
        transcript = attempt / 'agent-submission-transcript.ndjson'
        transcript.write_text(result.stdout)
        transcript.chmod(0o600)
        (attempt / 'agent-submission-stderr.log').write_text(result.stderr)
        (attempt / 'agent-submission-stderr.log').chmod(0o600)
        state['agentSubmissionTranscriptSha256'] = sha256(transcript)
        if result.returncode:
            raise Blocked('resumed external client failed; reconcile the original submission without replay')
        observed_client_session(result.stdout, state['externalClientSessionId'])
        state['agentSubmission'] = observed_agent_submission(result.stdout, original)
        state['stages']['agentSubmission'] = 'pass'
        private_json(attempt / 'receipt.json', state)
        print('Same external client submitted the prepared original task; the actual durable job was observed.')
    except Exception as exc:
        state['stages']['agentSubmission'] = 'blocked'
        state['blocker'] = str(exc)
        private_json(attempt / 'receipt.json', state)
        raise


def observed_agent_import(transcript: str, original: dict, submission: dict) -> dict:
    """Observe the original durable job; an accepted request is not imported data."""
    calls, results, call_positions, result_positions = ordered_tool_events(transcript)
    bind_name = 'mcp__honua__honua_agent_connect'
    read_name = 'mcp__honua__honua_workspace_get_dataset_import_submission'
    allowed = {bind_name, read_name, 'mcp__honua__honua_agent_status', 'ToolSearch',
               'ListMcpResourcesTool', 'ReadMcpResourceTool', 'WaitForMcpServers'}
    if any(call.get('name') not in allowed for call in calls):
        raise Blocked('import observation contains a tool outside the read-only continuation')
    binds = [call for call in calls if call.get('name') == bind_name]
    reads = [call for call in calls if call.get('name') == read_name]
    if len(binds) != 1 or len(reads) != 1:
        raise Blocked('external client must bind and read the original import exactly once')
    bind, read = binds[0], reads[0]
    require_results_before_dependencies((bind, read), results, call_positions, result_positions)
    reference = {'workspaceId': original['workspaceId'], 'taskId': original['taskId'],
                 'revision': original['revision'], 'contentHash': original['contentHash']}
    turn = {'workspaceId': original['workspaceId'], 'turnId': original['turnId']}
    bound, receipt = (json.loads(results[call['id']]['content']) for call in (bind, read))
    if (bind.get('input') != turn or bound.get('state') != 'attached' or
            bound.get('turnBinding') != turn or read.get('input') != reference):
        raise Blocked('external client observed a different import task or authorized turn')
    identity = {'taskId': original['taskId'], 'taskRevision': original['revision'],
                'taskContentHash': original['contentHash'], 'state': 'accepted',
                'operationId': submission['operationId']}
    job = receipt.get('job') or {}
    if (any(receipt.get(key) != value for key, value in identity.items()) or
            job.get('jobId') != submission['jobId'] or
            job.get('leaseId') != submission['leaseId'] or
            submission['operationId'] != submission['jobId']):
        raise Blocked('import observation does not bind the originally accepted durable job and lease')
    status = job.get('status')
    if status not in ('queued', 'provisioning', 'running', 'succeeded', 'failed', 'cancelled'):
        raise Blocked('original import has an unknown durable job status')
    observed = {'jobId': job['jobId'], 'leaseId': job['leaseId'], 'status': status}
    if status == 'succeeded':
        output = job.get('output') or {}
        if (job.get('outcomePending') is not False or not job.get('completedAt') or
                any(not isinstance(output.get(key), str) or not output[key].strip()
                    for key in ('physicalTableName', 'schema')) or
                type(output.get('featureCount')) is not int or output['featureCount'] <= 0 or
                (output.get('detectedSrid') is not None and
                 (type(output['detectedSrid']) is not int or output['detectedSrid'] <= 0))):
            raise Blocked('successful import lacks a confirmed physical dataset and nonempty feature count')
        observed['output'] = {key: output[key] for key in
                              ('physicalTableName', 'schema', 'featureCount', 'detectedSrid')
                              if key in output}
        observed['completedAt'] = job['completedAt']
    return observed


def phase_agent_observe_import(args, state, binding, attempt):
    # This phase is repeatable: it only binds the same turn and reads its job.
    if state['stages'].get('agentSubmission') != 'pass':
        raise Blocked('the original external-client submission has not been observed')
    original, submission = state.get('agentImportTask') or {}, state.get('agentSubmission') or {}
    if not all(submission.get(key) for key in ('operationId', 'jobId', 'leaseId')):
        raise Blocked('original accepted import job binding is missing')
    if installed_target(Path(state['installDirectory']), binding) != state.get('target'):
        raise Blocked('installed target changed before the read-only import observation')
    config, executable = attempt / 'mcp.json', Path(state['executables']['mcp'])
    if (not config.is_file() or not executable.is_file() or
            sha256(config) != state.get('mcpConfigSha256') or
            sha256(executable) != state.get('mcpExecutableSha256')):
        raise Blocked('external client registration/configuration changed before import observation')
    observation_id = uuid.uuid4().hex
    state['stages']['agentImportObservation'] = 'started-read-only'
    private_json(attempt / 'receipt.json', state)
    prompt = (f'Observe the original accepted import in this same Honua conversation. First bind '
              f'with honua_agent_connect to workspaceId {original["workspaceId"]} and '
              f'turnId {original["turnId"]}, then call honua_workspace_get_dataset_import_submission '
              f'exactly once for taskId {original["taskId"]}, revision {original["revision"]}, '
              f'contentHash {original["contentHash"]}. Read the actual job {submission["jobId"]}, '
              f'lease {submission["leaseId"]} and its current output. An accepted job alone is not '
              f'a successful import. Do not submit, retry, create, publish or mutate any GIS resource. '
              f'If denied, pending or uncertain, report that exact state and stop.')
    allowed = ['ToolSearch', 'ListMcpResourcesTool', 'ReadMcpResourceTool', 'WaitForMcpServers'] + [
        'mcp__honua__' + name for name in ('honua_agent_connect', 'honua_agent_status',
                                        'honua_workspace_get_dataset_import_submission')]
    command = [args.claude, '--restricted', '--tools', 'default', '--allowedTools', ','.join(allowed),
               '--disallowedTools', 'Bash,PowerShell,Agent,Read,Write,Edit,NotebookEdit,Glob,Grep,LSP,WebFetch,WebSearch,Workflow,Task',
               '--setting-sources', '', '--settings', '{"disableAllHooks":true}', '--strict-mcp-config',
               '--mcp-config', str(config), '--permission-mode', 'dontAsk', '--permission-prompts', 'none',
               '--resume', state['externalClientSessionId'], '--output-format', 'stream-json', '--verbose', '--print']
    try:
        result = subprocess.run(command, input=prompt, text=True, capture_output=True,
                                timeout=900, cwd=attempt, check=False)
        transcript = attempt / f'agent-import-observation-{observation_id}.ndjson'
        transcript.write_text(result.stdout)
        transcript.chmod(0o600)
        stderr = attempt / f'agent-import-observation-{observation_id}.stderr.log'
        stderr.write_text(result.stderr)
        stderr.chmod(0o600)
        state['agentImportObservationTranscriptSha256'] = sha256(transcript)
        state['agentImportObservationTranscriptFile'] = transcript.name
        if result.returncode:
            raise Blocked('read-only external-client observation failed; no import was replayed')
        observed_client_session(result.stdout, state['externalClientSessionId'])
        observed = observed_agent_import(result.stdout, original, submission)
        state['agentImportObservation'] = observed
        state['stages']['agentImportObservation'] = ('pass' if observed['status'] == 'succeeded'
                                                    else observed['status'])
        private_json(attempt / 'receipt.json', state)
        print('Original external-client import job observed: ' + observed['status'] +
              '. Service, map and publication qualification remain separate.')
        if observed['status'] in ('failed', 'cancelled'):
            raise Blocked('the originally accepted import did not succeed; never resubmit it automatically')
    except Exception as exc:
        state['stages']['agentImportObservation'] = 'blocked'
        state['blocker'] = str(exc)
        private_json(attempt / 'receipt.json', state)
        raise


def observed_agent_service(transcript: str, original: dict, imported: dict,
                           turn_id: str, names: dict) -> dict:
    """Join actual MCP results, never the model's account of its work."""
    calls, results, positions, result_positions = ordered_tool_events(transcript)
    sequence = ('honua_workspace_conversation', 'honua_workspace_get_turn', 'honua_agent_connect',
                'honua_admin_connections_create', 'honua_admin_connections_test',
                'honua_admin_layer_publish', 'honua_describe_layer', 'honua_query_features')
    allowed = {'mcp__honua__' + name for name in sequence} | {
        'ToolSearch', 'ListMcpResourcesTool', 'ReadMcpResourceTool', 'WaitForMcpServers',
        'mcp__honua__honua_agent_status'}
    if any(call.get('name') not in allowed for call in calls):
        raise Blocked('service continuation contains an unrelated or approval tool')
    matches = [[call for call in calls if call.get('name') == 'mcp__honua__' + name]
               for name in sequence]
    if any(len(group) != 1 for group in matches):
        raise Blocked('service continuation must bind, connect, publish and query exactly once')
    ordered = [group[0] for group in matches]
    require_results_before_dependencies(ordered, results, positions, result_positions)

    # Claude's model-visible text can summarize query results. Its raw MCP
    # tool_use_result retains structuredContent; bind it to the actual result ID.
    structured = {}
    for line in transcript.splitlines():
        event = json.loads(line)
        if not isinstance(event, dict) or event.get('type') != 'user':
            continue
        raw = event.get('tool_use_result')
        message = event.get('message')
        items = message.get('content', []) if isinstance(message, dict) else []
        result_items = [item for item in items if isinstance(item, dict) and item.get('type') == 'tool_result']
        if isinstance(raw, dict) and isinstance(raw.get('structuredContent'), dict):
            if len(result_items) != 1 or raw.get('isError'):
                raise Blocked('ambiguous or failed raw MCP service result')
            structured[result_items[0]['tool_use_id']] = raw['structuredContent']
    if any(call['id'] not in structured for call in ordered):
        raise Blocked('service continuation lacks original structured MCP results')
    conversation, turn, bound, created, tested, published, described, queried = (
        structured[call['id']] for call in ordered)
    workspace = original['workspaceId']
    locator = {'workspaceId': workspace, 'turnId': turn_id}
    def has_human_request(value):
        messages = [item for item in value.get('messages', [])
                    if item.get('role') == 'user' and item.get('turnId') == turn_id]
        return len(messages) == 1 and messages[0].get('content') == SERVICE_PROMPT
    task = turn.get('context', {}).get('task') or {}
    if (ordered[0].get('input') != {'workspaceId': workspace} or
            ordered[1].get('input') != locator or ordered[2].get('input') != locator or
            not has_human_request(conversation) or not has_human_request(turn) or
            task.get('kind') != 'dataset-import' or
            any(original.get(source) is None or task.get(key) != original[source]
                for key, source in (('id', 'taskId'), ('revision', 'revision'), ('contentHash', 'contentHash'))) or
            turn.get('turnId') != turn_id or turn_id == original['turnId'] or
            any(not original.get(key) or turn.get('context', {}).get(key) != original[key]
                for key in ('workspaceId', 'targetId', 'tenantId')) or
            bound.get('state') != 'attached' or bound.get('turnBinding') != locator):
        raise Blocked('service continuation does not bind a fresh human turn on the original target')

    def completed_data(value):
        if (value.get('status') != 'Completed' or value.get('requiresApproval') is not False or
                not isinstance(value.get('operationInstanceId'), str) or not value['operationInstanceId']):
            raise Blocked('service operation is pending, denied or lacks a durable identity')
        response = json.loads(value.get('details', {}).get('response', 'null'))
        if not isinstance(response, dict) or response.get('success') is not True or not isinstance(response.get('data'), dict):
            raise Blocked('service operation lacks its actual successful API response')
        return response['data']

    connection, health, layer = (completed_data(value) for value in (created, tested, published))
    connection_request = {'name': names['connection'], 'secretReference': 'env:HONUA_LOCAL_DB_PASSWORD',
                          'secretType': 'env', 'sslRequired': False, 'sslMode': 'Disable'}
    connection_id = connection.get('connectionId')
    output = imported['output']
    publish_request = {'connectionId': connection_id, 'schema': output['schema'],
                       'table': output['physicalTableName'], 'layerName': names['layer'],
                       'serviceName': names['service'], 'enabled': True}
    if (ordered[3].get('input') != connection_request or
            not isinstance(connection_id, str) or not connection_id or
            connection.get('name') != names['connection'] or
            ordered[4].get('input') != {'id': connection_id} or
            health.get('connectionId') != connection_id or health.get('isHealthy') is not True or
            ordered[5].get('input') != publish_request or
            any(layer.get(key) != publish_request[key] for key in ('schema', 'table', 'layerName', 'serviceName')) or
            layer.get('enabled') is not True or type(layer.get('layerId')) is not int or layer['layerId'] <= 0):
        raise Blocked('published service does not match the original imported table and secret reference')
    identity = {'serviceId': described.get('serviceId'), 'layerId': layer['layerId']}
    if (ordered[6].get('input') != {'serviceId': names['service'], 'layerId': layer['layerId'],
                                  'includeRowCount': True} or
            described.get('serviceName') != names['service'] or
            not isinstance(identity['serviceId'], str) or not identity['serviceId'] or
            ordered[7].get('input') != {**identity, 'returnCountOnly': True} or
            any(described.get(key) != value or queried.get(key) != value for key, value in identity.items()) or
            not isinstance(described.get('studioSourceId'), str) or not described['studioSourceId'] or
            not isinstance(described.get('fields'), list) or not described['fields'] or
            type(described.get('rowCount')) is not int or described['rowCount'] != output['featureCount'] or
            type(queried.get('count')) is not int or queried['count'] != output['featureCount']):
        raise Blocked('actual service schema and query do not verify the imported feature count')
    return {**identity, 'serviceName': names['service'],
            'connectionId': connection_id, 'studioSourceId': described['studioSourceId'],
            'schema': output['schema'], 'table': output['physicalTableName'],
            'featureCount': queried['count'], 'fields': described['fields'],
            'workspaceId': workspace, 'turnId': turn_id,
            'operationInstanceIds': [value['operationInstanceId'] for value in (created, tested, published)]}


def phase_agent_service(args, state, binding, attempt):
    if state['stages'].get('agentImportObservation') != 'pass' or state['stages'].get('agentService'):
        raise Blocked('service continuation requires a confirmed import and cannot replay an uncertain mutation')
    original, imported = state['agentImportTask'], state['agentImportObservation']
    if (args.workspace_id != original['workspaceId'] or
            not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', args.turn_id or '') or
            args.turn_id == original['turnId']):
        raise Blocked('a fresh actual human Console turn in the same workspace is required')
    if installed_target(Path(state['installDirectory']), binding) != state.get('target'):
        raise Blocked('installed target changed before the service continuation')
    config, executable = attempt / 'mcp.json', Path(state['executables']['mcp'])
    if (not config.is_file() or not executable.is_file() or
            sha256(config) != state.get('mcpConfigSha256') or
            sha256(executable) != state.get('mcpExecutableSha256')):
        raise Blocked('external client registration/configuration changed before service continuation')
    suffix = uuid.uuid4().hex[:20]
    names = {'connection': 'native-' + suffix, 'service': 'native-' + suffix, 'layer': 'Imported ' + suffix}
    state['stages']['agentService'] = 'started'
    state['agentServiceIntent'] = {'turnId': args.turn_id, **names}
    private_json(attempt / 'receipt.json', state)
    output = imported['output']
    prompt = (f'The human has sent a new Console turn requesting a service from their imported data. '
              f'Continue this same Honua conversation and registration. Read honua_workspace_conversation '
              f'for workspaceId {args.workspace_id}, then honua_workspace_get_turn for turnId {args.turn_id}. '
              f'The exact retained human request must be "{SERVICE_PROMPT}" and its context must '
              f'retain the original import task {original["taskId"]}; stop if either differs. '
              f'Bind that exact workspace and turn with honua_agent_connect. Create exactly one connection '
              f'named {names["connection"]}, secretReference env:HONUA_LOCAL_DB_PASSWORD, secretType env, '
              f'sslRequired false and sslMode Disable. Never read or supply a password. Test its returned '
              f'connectionId with honua_admin_connections_test using id. If healthy, publish exactly once '
              f'with honua_admin_layer_publish using connectionId, schema {output["schema"]}, '
              f'table {output["physicalTableName"]}, layerName "{names["layer"]}", '
              f'serviceName {names["service"]}, enabled true. Inspect each successful result before the '
              f'next call. Describe the published serviceName as serviceId and its returned layerId '
              f'with includeRowCount true. The description returns the canonical serviceId, which may '
              f'differ from serviceName. Query that exact canonical serviceId/layerId with '
              f'returnCountOnly true. The original import contained '
              f'{output["featureCount"]} features. Do not set anonymous access, approve anything, create '
              f'a map or retry any mutation. If approval is required or a result is uncertain, stop.')
    tools = ('honua_workspace_conversation', 'honua_workspace_get_turn', 'honua_agent_connect',
             'honua_agent_status', 'honua_admin_connections_create', 'honua_admin_connections_test',
             'honua_admin_layer_publish', 'honua_describe_layer', 'honua_query_features')
    allowed = ['ToolSearch', 'ListMcpResourcesTool', 'ReadMcpResourceTool', 'WaitForMcpServers'] + [
        'mcp__honua__' + name for name in tools]
    command = [args.claude, '--restricted', '--tools', 'default', '--allowedTools', ','.join(allowed),
               '--disallowedTools', 'Bash,PowerShell,Agent,Read,Write,Edit,NotebookEdit,Glob,Grep,LSP,WebFetch,WebSearch,Workflow,Task',
               '--setting-sources', '', '--settings', '{"disableAllHooks":true}', '--strict-mcp-config',
               '--mcp-config', str(config), '--permission-mode', 'dontAsk', '--permission-prompts', 'none',
               '--resume', state['externalClientSessionId'], '--output-format', 'stream-json', '--verbose', '--print']
    try:
        result = subprocess.run(command, input=prompt, text=True, capture_output=True,
                                timeout=900, cwd=attempt, check=False)
        transcript, stderr = attempt / 'agent-service-transcript.ndjson', attempt / 'agent-service-stderr.log'
        for path, content in ((transcript, result.stdout), (stderr, result.stderr)):
            path.write_text(content)
            path.chmod(0o600)
        state['agentServiceTranscriptSha256'] = sha256(transcript)
        if result.returncode:
            raise Blocked('external service continuation failed; inspect existing operations without replay')
        observed_client_session(result.stdout, state['externalClientSessionId'])
        state['agentService'] = observed_agent_service(result.stdout, original, imported, args.turn_id, names)
        state['stages']['agentService'] = 'pass'
        state['oneExternalAgentConversationQualified'] = False
        private_json(attempt / 'receipt.json', state)
        print('Same external client published and queried the imported table. Map and audience qualification remain.')
    except Exception as exc:
        state['stages']['agentService'] = 'blocked'
        state['blocker'] = str(exc)
        private_json(attempt / 'receipt.json', state)
        raise


def observed_agent_map(transcript: str, original: dict, service: dict, turn_id: str,
                       package_key: str, layer_key: str, route: str) -> dict:
    """Join the same client's actual Studio MCP results to the original service and human turn."""
    calls, results, positions, result_positions = ordered_tool_events(transcript)
    names = ('honua_workspace_conversation', 'honua_workspace_get_turn', 'honua_agent_connect',
             'honua_describe_layer', 'honua_admin_services_access_policy_set',
             'honua_studio_create_draft', 'honua_studio_add_layer',
             'honua_studio_set_layer_style', 'honua_studio_set_view',
             'honua_studio_get_draft', 'honua_studio_validate_draft',
             'honua_studio_save_version', 'honua_studio_get_version',
             'honua_studio_reopen_version', 'honua_studio_get_draft',
             'honua_workspace_get',
             'honua_workspace_set_context', 'honua_workspace_get')
    permitted = {'mcp__honua__' + name for name in names} | {
        'ToolSearch', 'ListMcpResourcesTool', 'ReadMcpResourceTool', 'WaitForMcpServers',
        'mcp__honua__honua_agent_status'}
    if any(call.get('name') not in permitted for call in calls):
        raise Blocked('map continuation contains an unrelated mutation or approval tool')
    matched = [[call for call in calls if call.get('name') == 'mcp__honua__' + name]
               for name in set(names)]
    if any(len(group) != names.count(group[0].get('name', '').removeprefix('mcp__honua__'))
           for group in matched if group) or any(not group for group in matched):
        raise Blocked('map continuation must execute the exact Studio sequence once')
    expected = []
    seen = {}
    for name in names:
        index = seen.get(name, 0)
        group = [call for call in calls if call.get('name') == 'mcp__honua__' + name]
        expected.append(group[index])
        seen[name] = index + 1
    if [call for call in calls if call in expected] != expected:
        raise Blocked('map continuation executed Studio calls out of order')
    require_results_before_dependencies(expected, results, positions, result_positions)
    structured = {}
    for line in transcript.splitlines():
        event = json.loads(line)
        if not isinstance(event, dict) or event.get('type') != 'user':
            continue
        raw, message = event.get('tool_use_result'), event.get('message')
        items = [item for item in message.get('content', []) if item.get('type') == 'tool_result'] \
            if isinstance(message, dict) and isinstance(message.get('content'), list) else []
        if isinstance(raw, dict) and isinstance(raw.get('structuredContent'), dict):
            if len(items) != 1 or raw.get('isError'):
                raise Blocked('ambiguous or failed raw Studio MCP result')
            structured[items[0]['tool_use_id']] = raw['structuredContent']
    if any(call['id'] not in structured for call in expected):
        raise Blocked('map continuation lacks original structured Studio MCP results')
    (conversation, turn, bound, described, policy, created, added, styled, viewed,
     draft, validation, saved, version, reopened, reopened_read,
     context_before, context_set, context_after) = (
        structured[call['id']] for call in expected)
    workspace = original['workspaceId']
    locator = {'workspaceId': workspace, 'turnId': turn_id}
    def human_request(value):
        messages = [item for item in value.get('messages', [])
                    if item.get('role') == 'user' and item.get('turnId') == turn_id]
        return len(messages) == 1 and messages[0].get('content') == MAP_PROMPT
    task = turn.get('context', {}).get('task') or {}
    if (expected[0].get('input') != {'workspaceId': workspace} or
            expected[1].get('input') != locator or expected[2].get('input') != locator or
            not human_request(conversation) or not human_request(turn) or
            turn.get('turnId') != turn_id or turn_id in (original['turnId'], service['turnId']) or
            any(turn.get('context', {}).get(key) != original[key]
                for key in ('workspaceId', 'targetId', 'tenantId')) or
            any(task.get(key) != original[source] for key, source in
                (('id', 'taskId'), ('revision', 'revision'), ('contentHash', 'contentHash'))) or
            task.get('kind') != 'dataset-import' or bound.get('state') != 'attached' or
            bound.get('turnBinding') != locator):
        raise Blocked('map continuation lacks a fresh exact human Console turn on the original import')
    layer = {'serviceId': service['serviceId'], 'layerId': service['layerId']}
    if (expected[3].get('input') != {'serviceId': service['serviceName'],
                                     'layerId': service['layerId'], 'includeRowCount': True} or
            any(described.get(key) != value for key, value in layer.items()) or
            described.get('serviceName') != service['serviceName'] or
            described.get('studioSourceId') != service['studioSourceId'] or
            described.get('rowCount') != service['featureCount'] or
            described.get('srid') != 4326 or not isinstance(described.get('extent'), dict) or
            expected[4].get('input') != {'serviceName': service['serviceName'],
                                         'allowAnonymous': True, 'allowAnonymousWrite': False} or
            policy.get('status') != 'Completed' or policy.get('requiresApproval') is not False or
            not policy.get('operationInstanceId')):
        raise Blocked('map source or public read policy is not the original published layer')
    policy_response = json.loads(policy.get('details', {}).get('response', 'null'))
    if not isinstance(policy_response, dict) or policy_response.get('success') is not True:
        raise Blocked('service read policy lacks a successful canonical API response')
    draft_id, item_id = created.get('draftId'), created.get('itemId')
    if (expected[5].get('input') != {'packageKey': package_key, 'family': 'map',
                                    'schemaVersion': '1.0'} or
            not isinstance(draft_id, str) or not draft_id or not isinstance(item_id, str) or
            not item_id or created.get('packageKey') != package_key or
            created.get('family') != 'map' or created.get('generation') != 1):
        raise Blocked('Studio draft creation lacks its real agent-owned identity')
    layer_input = expected[6].get('input') or {}
    composed = layer_input.get('layer') or {}
    if (layer_input.get('draftId') != draft_id or layer_input.get('generation') != 1 or
            composed.get('id') != layer_key or composed.get('sourceId') != service['studioSourceId'] or
            composed.get('visible') is not True or composed.get('type') not in ('circle', 'line', 'fill') or
            added.get('draftId') != draft_id or added.get('generation') != 2):
        raise Blocked('Studio composition did not add the original catalog source')
    style_input = expected[7].get('input') or {}
    style_body = style_input.get('styleBody') or {}
    if (style_input.get('draftId') != draft_id or style_input.get('generation') != 2 or
            style_input.get('layerId') != layer_key or 'styleRef' in style_input or
            not isinstance(style_body.get(layer_key), dict) or
            not isinstance(style_body[layer_key].get('paint'), dict) or
            not style_body[layer_key]['paint'] or styled.get('generation') != 3):
        raise Blocked('Studio layer has no actual map-local visual style edit')
    view_input = expected[8].get('input') or {}
    view = view_input.get('view') or {}
    extent = described['extent']
    bounds = [extent.get(key) for key in ('minX', 'minY', 'maxX', 'maxY')]
    if (view_input.get('draftId') != draft_id or view_input.get('generation') != 3 or
            not isinstance(view.get('bbox'), list) or len(view['bbox']) != 4 or
            any(type(value) not in (int, float) for value in view['bbox']) or
            any(type(value) not in (int, float) for value in bounds) or
            not (-180 <= bounds[0] <= bounds[2] <= 180 and
                 -90 <= bounds[1] <= bounds[3] <= 90) or
            not (view['bbox'][0] <= bounds[0] <= bounds[2] <= view['bbox'][2] and
                 view['bbox'][1] <= bounds[1] <= bounds[3] <= view['bbox'][3]) or
            view.get('crs') != 'EPSG:4326' or viewed.get('generation') != 4 or
            expected[9].get('input') != {'draftId': draft_id} or
            draft.get('draftId') != draft_id or draft.get('generation') != 4):
        raise Blocked('Studio map viewport/readback changed or was not saved')
    body = draft.get('envelope', {}).get('body') or {}
    layers = body.get('layers') or []
    references = body.get('styleRefs') or []
    if (len(layers) != 1 or any(layers[0].get(key) != value for key, value in
            {'id': layer_key, 'sourceId': service['studioSourceId'],
             'styleRef': 'honua-inline:' + layer_key}.items()) or
            body.get('view', {}).get('bbox') != view['bbox'] or
            not any(ref.get('styleId') == 'honua-inline:' + layer_key and
                    ref.get('body') == style_body for ref in references)):
        raise Blocked('canonical draft readback lost source, visual style or viewport')
    if (expected[10].get('input') != {'draftId': draft_id} or
            validation.get('status') not in ('valid', 'warning') or
            expected[11].get('input') != {'draftId': draft_id, 'generation': 4}):
        raise Blocked('Studio draft was not structurally validated before save')
    saved_version = saved.get('version') or {}
    version_id, content_hash = saved_version.get('versionId'), saved_version.get('contentHash')
    if (saved_version.get('itemId') != item_id or not isinstance(version_id, str) or
            not isinstance(content_hash, str) or not content_hash or
            expected[12].get('input') != {'itemId': item_id, 'versionId': version_id} or
            version.get('versionId') != version_id or version.get('contentHash') != content_hash or
            expected[13].get('input') != {'itemId': item_id, 'versionId': version_id}):
        raise Blocked('saved Studio version lacks exact immutable identity/readback')
    reopened_id = reopened.get('draftId')
    if (not isinstance(reopened_id, str) or not reopened_id or reopened_id == draft_id or
            reopened.get('itemId') != item_id or
            expected[14].get('input') != {'draftId': reopened_id} or
            reopened_read.get('draftId') != reopened_id or
            reopened_read.get('itemId') != item_id or
            reopened_read.get('envelope', {}).get('body') != body):
        raise Blocked('reopened version does not preserve the exact composed map')
    view_id = turn.get('context', {}).get('viewId')
    revision = context_before.get('revision')
    selected = context_before.get('selectedResources')
    task_before = context_before.get('task') or {}
    desired_task = {'kind': 'studio-composition', 'id': reopened_id,
                    'revision': reopened_read.get('generation'), 'packageKey': package_key}
    if (not isinstance(view_id, str) or not view_id or
            expected[15].get('input') != {'workspaceId': workspace, 'viewId': view_id} or
            expected[17].get('input') != expected[15].get('input') or
            type(revision) is not int or revision < 0 or not isinstance(selected, list) or
            context_before.get('workspaceId') != workspace or
            context_before.get('targetId') != original['targetId'] or
            any(task_before.get(key) != original[source] for key, source in
                (('id', 'taskId'), ('revision', 'revision'), ('contentHash', 'contentHash'))) or
            expected[16].get('input') != {'workspaceId': workspace, 'viewId': view_id,
                                          'expectedRevision': revision,
                                          'targetId': original['targetId'],
                                          'selectedResources': selected, 'task': desired_task} or
            context_set.get('task') != desired_task or context_after.get('task') != desired_task or
            context_after.get('revision') != revision + 1):
        raise Blocked('canonical workspace context did not attach the exact agent-created map for future human views')
    return {'workspaceId': workspace, 'turnId': turn_id, 'service': service,
            'packageKey': package_key, 'layerId': layer_key, 'sourceId': service['studioSourceId'],
            'draftId': draft_id, 'reopenedDraftId': reopened_id, 'itemId': item_id,
            'versionId': version_id, 'contentHash': content_hash, 'bodySha256': hashlib.sha256(
                json.dumps(body, separators=(',', ':')).encode()).hexdigest(), 'route': route,
            'policyOperationInstanceId': policy['operationInstanceId'],
            'styleBody': style_body, 'view': view,
            'workspaceViewId': view_id, 'workspaceContextRevision': revision + 1}


def phase_agent_map(args, state, binding, attempt):
    if state['stages'].get('agentService') != 'pass' or state['stages'].get('agentMap'):
        raise Blocked('map continuation requires the confirmed original external-agent service and cannot replay a mutation')
    original, service = state['agentImportTask'], state['agentService']
    if (args.workspace_id != original['workspaceId'] or
            not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', args.turn_id or '') or
            args.turn_id in (original['turnId'], service['turnId']) or
            not args.route or not re.fullmatch(r'/maps/[A-Za-z0-9._/-]{1,160}', args.route)):
        raise Blocked('fresh actual human map turn, original workspace and bounded map route are required')
    if installed_target(Path(state['installDirectory']), binding) != state.get('target'):
        raise Blocked('installed target changed before map continuation')
    config, executable = attempt / 'mcp.json', Path(state['executables']['mcp'])
    if (not config.is_file() or not executable.is_file() or
            sha256(config) != state.get('mcpConfigSha256') or
            sha256(executable) != state.get('mcpExecutableSha256')):
        raise Blocked('external client registration/configuration changed before map continuation')
    suffix = uuid.uuid4().hex[:20]
    package_key, layer_key = 'native-map-' + suffix, 'imported-' + suffix
    state['stages']['agentMap'] = 'started'
    state['agentMapIntent'] = {'turnId': args.turn_id, 'packageKey': package_key,
                               'layerId': layer_key, 'route': args.route}
    private_json(attempt / 'receipt.json', state)
    prompt = (f'The human sent a new Console turn requesting a map from the same service. '
              f'Continue the original Honua conversation and only its existing registration. '
              f'Read honua_workspace_conversation for {args.workspace_id}, then honua_workspace_get_turn '
              f'for {args.turn_id}; the exact human prompt must be "{MAP_PROMPT}" and its task must retain '
              f'original import {original["taskId"]}. Bind with honua_agent_connect to that exact turn. '
              f'Describe published service name {service["serviceName"]} layer {service["layerId"]} '
              f'with includeRowCount true; verify returned canonical serviceId {service["serviceId"]}. '
              f'it must return studioSourceId {service["studioSourceId"]} and {service["featureCount"]} rows. '
              f'Set only anonymous READ on published service name {service["serviceName"]} with '
              f'honua_admin_services_access_policy_set, allowAnonymous true, allowAnonymousWrite false. '
              f'If this policy is pending or denied, stop; never impersonate approval. '
              f'Create a map Studio draft with packageKey {package_key}, family map, schemaVersion 1.0. '
              f'Use the returned draftId and actual generation. Add one visible presentation layer id {layer_key} '
              f'with sourceId copied exactly from that described studioSourceId, selecting circle/line/fill for '
              f'the observed geometry. Apply one map-local visual style with honua_studio_set_layer_style '
              f'styleBody keyed by {layer_key}, with nonempty paint. Set a WGS84 bbox view from observed extent. '
              f'Read back the draft, validate, save its current generation, get that exact saved version, '
              f'reopen it, and read back the reopened draft. Do not propose publication yet: the human '
              f'must make a real visual edit in Console and send a new prompt first. '
              f'Finally honua_workspace_get current view context, '
              f'honua_workspace_set_context with its exact expectedRevision, original targetId and unchanged '
              f'selectedResources, setting task studio-composition to the reopened draftId, actual generation '
              f'and packageKey; then get context again. This attachment is for future human views and must not '
              f'retarget the frozen turn. Inspect each successful result before its dependent call; stop on '
              f'any uncertain mutation without replay.')
    tools = ('honua_workspace_conversation', 'honua_workspace_get_turn', 'honua_agent_connect',
             'honua_agent_status', 'honua_describe_layer', 'honua_admin_services_access_policy_set',
             'honua_studio_create_draft', 'honua_studio_add_layer',
             'honua_studio_set_layer_style', 'honua_studio_set_view', 'honua_studio_get_draft',
             'honua_studio_validate_draft', 'honua_studio_save_version', 'honua_studio_get_version',
             'honua_studio_reopen_version',
             'honua_workspace_get', 'honua_workspace_set_context')
    allowed = ['ToolSearch', 'ListMcpResourcesTool', 'ReadMcpResourceTool', 'WaitForMcpServers'] + [
        'mcp__honua__' + name for name in tools]
    command = [args.claude, '--restricted', '--tools', 'default', '--allowedTools', ','.join(allowed),
               '--disallowedTools', 'Bash,PowerShell,Agent,Read,Write,Edit,NotebookEdit,Glob,Grep,LSP,WebFetch,WebSearch,Workflow,Task',
               '--setting-sources', '', '--settings', '{"disableAllHooks":true}', '--strict-mcp-config',
               '--mcp-config', str(config), '--permission-mode', 'dontAsk', '--permission-prompts', 'none',
               '--resume', state['externalClientSessionId'], '--output-format', 'stream-json', '--verbose', '--print']
    try:
        result = subprocess.run(command, input=prompt, text=True, capture_output=True,
                                timeout=1200, cwd=attempt, check=False)
        transcript, stderr = attempt / 'agent-map-transcript.ndjson', attempt / 'agent-map-stderr.log'
        for path, content in ((transcript, result.stdout), (stderr, result.stderr)):
            path.write_text(content)
            path.chmod(0o600)
        state['agentMapTranscriptSha256'] = sha256(transcript)
        if result.returncode:
            raise Blocked('external map continuation failed; reconcile original Studio records without replay')
        observed_client_session(result.stdout, state['externalClientSessionId'])
        state['agentMap'] = observed_agent_map(result.stdout, original, service, args.turn_id,
                                                package_key, layer_key, args.route)
        state['stages']['agentMap'] = 'pass'
        state['stages']['agentMapFinish'] = 'awaiting-actual-human-visual-edit-and-turn'
        state['oneExternalAgentConversationQualified'] = False
        private_json(attempt / 'receipt.json', state)
        print('Same external client saved/reopened and attached its map. Human visual edit and fresh prompt remain before publication.')
    except Exception as exc:
        state['stages']['agentMap'] = 'blocked'
        state['blocker'] = str(exc)
        private_json(attempt / 'receipt.json', state)
        raise


def phase_agent_map_visual(args, state, binding, attempt):
    if (state.get('conversationMode') != 'external-agent' or
            state['stages'].get('agentMap') != 'pass' or
            state['stages'].get('agentMapVisual')):
        raise Blocked('actual human visual edit requires the external-only agent map and cannot replay')
    if not args.operator_state or not args.operator_state.is_file():
        raise Blocked('original paired human browser storage state is required')
    if not args.origin or not re.fullmatch(r'http://127\.0\.0\.1:\d+', args.origin):
        raise Blocked('installed Console origin is required')
    target = installed_target(Path(state['installDirectory']), binding)
    if target != state.get('target') or target['origins']['console'] != args.origin or \
            target['nativeModelEnabled'] != 'false' or target['nativeGatewayConfigured']:
        raise Blocked('external-only installed target changed before the visual edit')
    script = ROOT / 'certification/native-docker-first/agent-map-visual.mjs'
    receipt = attempt / 'agent-map-visual.json'
    if receipt.exists():
        raise Blocked('existing visual receipt requires read-only reconciliation')
    state['stages']['agentMapVisual'] = 'started'
    private_json(attempt / 'receipt.json', state)
    env = {**os.environ, 'HONUA_AGENT_MAP_CONSOLE_ROOT': str(args.console_root),
           'HONUA_AGENT_MAP_HANDOFF': str(attempt / 'agent-map-handoff.json'),
           'HONUA_AGENT_MAP_ORIGIN': args.origin,
           'HONUA_AGENT_MAP_OPERATOR_STATE': str(args.operator_state),
           'HONUA_AGENT_MAP_VISUAL_RECEIPT': str(receipt)}
    private_json(attempt / 'agent-map-handoff.json', state['agentMap'], new=True)
    try:
        result = subprocess.run(['node', str(script)], cwd=attempt, env=env,
                                capture_output=True, text=True, timeout=600, check=False)
        log = attempt / 'agent-map-visual.log'
        log.write_text(result.stdout + '\n' + result.stderr)
        log.chmod(0o600)
        if result.returncode:
            raise Blocked('actual paired-human visual edit/turn failed; inspect private browser log')
        value = read_json(receipt)
        prior = state['agentMap']
        if (value.get('status') != 'human-visual-edit-and-turn-observed' or
                any(value.get(key) != prior[key] for key in
                    ('workspaceId', 'itemId', 'packageKey', 'layerId', 'sourceId')) or
                value.get('draftId') != prior['reopenedDraftId'] or
                value.get('turnId') in (state['agentImportTask']['turnId'],
                                        state['agentService']['turnId'], prior['turnId']) or
                type(value.get('generation')) is not int or value['generation'] <= 0 or
                not isinstance(value.get('prompt'), str) or not value['prompt']):
            raise Blocked('visual browser receipt does not join exact agent map and fresh human turn')
        state['agentMapVisual'] = value
        state['agentMapVisualScriptSha256'] = sha256(script)
        state['agentMapVisualReceiptSha256'] = sha256(receipt)
        state['stages']['agentMapVisual'] = 'pass'
        private_json(attempt / 'receipt.json', state)
        print('Human visual edit and external-only Console turn observed; same agent must finish the map.')
    except Exception as exc:
        state['stages']['agentMapVisual'] = 'blocked'
        state['blocker'] = str(exc)
        private_json(attempt / 'receipt.json', state)
        raise


def observed_agent_map_finish(transcript: str, original: dict, map_receipt: dict,
                              visual: dict) -> dict:
    """Require the same external client to preserve the real human visual edit before proposal."""
    calls, results, positions, result_positions = ordered_tool_events(transcript)
    names = ('honua_workspace_conversation', 'honua_workspace_get_turn', 'honua_agent_connect',
             'honua_studio_get_draft', 'honua_studio_set_map_style',
             'honua_studio_get_draft', 'honua_studio_validate_draft',
             'honua_studio_save_version', 'honua_studio_get_version',
             'honua_studio_reopen_version', 'honua_studio_get_draft',
             'honua_workspace_get', 'honua_workspace_set_context', 'honua_workspace_get',
             'honua_studio_propose_publication')
    allowed = {'mcp__honua__' + name for name in names} | {
        'ToolSearch', 'ListMcpResourcesTool', 'ReadMcpResourceTool', 'WaitForMcpServers',
        'mcp__honua__honua_agent_status'}
    if any(call.get('name') not in allowed for call in calls):
        raise Blocked('final map continuation contains unrelated mutation or approval')
    expected = []
    seen = {}
    for name in names:
        group = [call for call in calls if call.get('name') == 'mcp__honua__' + name]
        if len(group) != names.count(name):
            raise Blocked('final map continuation omitted or repeated a canonical tool')
        index = seen.get(name, 0)
        expected.append(group[index])
        seen[name] = index + 1
    if [call for call in calls if call in expected] != expected:
        raise Blocked('final map continuation executed tools out of order')
    require_results_before_dependencies(expected, results, positions, result_positions)
    structured = {}
    for line in transcript.splitlines():
        event = json.loads(line)
        if not isinstance(event, dict) or event.get('type') != 'user':
            continue
        raw, message = event.get('tool_use_result'), event.get('message')
        items = [item for item in message.get('content', []) if item.get('type') == 'tool_result'] \
            if isinstance(message, dict) and isinstance(message.get('content'), list) else []
        if isinstance(raw, dict) and isinstance(raw.get('structuredContent'), dict):
            if len(items) != 1 or raw.get('isError'):
                raise Blocked('ambiguous or failed raw final-map MCP result')
            structured[items[0]['tool_use_id']] = raw['structuredContent']
    if any(call['id'] not in structured for call in expected):
        raise Blocked('final map continuation lacks original structured MCP results')
    (conversation, turn, bound, before, changed, after, validation, saved, version,
     reopened, reopened_read, context_before, context_set, context_after, proposed) = (
        structured[call['id']] for call in expected)
    workspace, turn_id = map_receipt['workspaceId'], visual['turnId']
    locator = {'workspaceId': workspace, 'turnId': turn_id}
    messages = [item for item in conversation.get('messages', [])
                if item.get('role') == 'user' and item.get('turnId') == turn_id]
    turn_messages = [item for item in turn.get('messages', [])
                     if item.get('role') == 'user' and item.get('turnId') == turn_id]
    task = turn.get('context', {}).get('task') or {}
    if (expected[0].get('input') != {'workspaceId': workspace} or
            expected[1].get('input') != locator or expected[2].get('input') != locator or
            len(messages) != 1 or len(turn_messages) != 1 or
            messages[0].get('content') != visual['prompt'] or
            turn_messages[0].get('content') != visual['prompt'] or
            turn.get('turnId') != turn_id or
            any(turn.get('context', {}).get(key) != original[key]
                for key in ('workspaceId', 'targetId', 'tenantId')) or
            task != {'kind': 'studio-composition', 'id': visual['draftId'],
                     'revision': visual['generation'], 'packageKey': visual['packageKey']} or
            bound.get('state') != 'attached' or bound.get('turnBinding') != locator):
        raise Blocked('final map continuation lacks the fresh human visual-edit turn and binding')
    draft_id = visual['draftId']
    before_body = before.get('envelope', {}).get('body') or {}
    before_hash = hashlib.sha256(json.dumps(before_body, separators=(',', ':')).encode()).hexdigest()
    layers = before_body.get('mapSpec', {}).get('layers') or []
    render = [layer for layer in layers if layer.get('id') == visual['layerId']]
    if (expected[3].get('input') != {'draftId': draft_id} or before.get('draftId') != draft_id or
            before.get('generation') != visual['generation'] or
            before_hash != visual['bodySha256'] or len(render) != 1 or
            render[0].get('paint', {}).get(visual['colorProperty']) != visual['colorExpression'] or
            before.get('itemId') != map_receipt['itemId']):
        raise Blocked('external agent did not read the exact human-edited map')
    changed_spec = expected[4].get('input', {}).get('mapSpec') or {}
    expected_spec = json.loads(json.dumps(before_body['mapSpec']))
    expected_render = next(layer for layer in expected_spec['layers'] if layer['id'] == visual['layerId'])
    expected_render.setdefault('paint', {})[visual['opacityProperty']] = 0.55
    if (expected[4].get('input', {}).get('draftId') != draft_id or
            expected[4].get('input', {}).get('generation') != visual['generation'] or
            changed_spec != expected_spec or changed.get('generation') != visual['generation'] + 1 or
            expected[5].get('input') != {'draftId': draft_id} or
            after.get('draftId') != draft_id or after.get('generation') != visual['generation'] + 1 or
            after.get('envelope', {}).get('body', {}).get('mapSpec') != expected_spec):
        raise Blocked('prompted map style failed to preserve the real human edit and exact source')
    final_body = after['envelope']['body']
    if (expected[6].get('input') != {'draftId': draft_id} or
            validation.get('status') not in ('valid', 'warning') or
            expected[7].get('input') != {'draftId': draft_id, 'generation': after['generation']}):
        raise Blocked('final human-edited map was not validated before save')
    saved_version = saved.get('version') or {}
    item_id, version_id, content_hash = (saved_version.get(key) for key in
                                         ('itemId', 'versionId', 'contentHash'))
    if (item_id != map_receipt['itemId'] or not version_id or not content_hash or
            expected[8].get('input') != {'itemId': item_id, 'versionId': version_id} or
            version.get('versionId') != version_id or version.get('contentHash') != content_hash or
            expected[9].get('input') != {'itemId': item_id, 'versionId': version_id}):
        raise Blocked('final version did not bind the original agent map item')
    reopened_id = reopened.get('draftId')
    if (not reopened_id or reopened_id in (draft_id, map_receipt['draftId']) or
            reopened.get('itemId') != item_id or
            expected[10].get('input') != {'draftId': reopened_id} or
            reopened_read.get('draftId') != reopened_id or
            reopened_read.get('itemId') != item_id or
            reopened_read.get('envelope', {}).get('body') != final_body):
        raise Blocked('final saved version did not reopen as the same map content')
    view_id = turn.get('context', {}).get('viewId')
    revision = context_before.get('revision')
    selected = context_before.get('selectedResources')
    final_task = {'kind': 'studio-composition', 'id': reopened_id,
                  'revision': reopened_read.get('generation'), 'packageKey': visual['packageKey']}
    if (not view_id or expected[11].get('input') != {'workspaceId': workspace, 'viewId': view_id} or
            expected[13].get('input') != expected[11].get('input') or
            type(revision) is not int or not isinstance(selected, list) or
            context_before.get('task') != task or
            expected[12].get('input') != {'workspaceId': workspace, 'viewId': view_id,
                                           'expectedRevision': revision,
                                           'targetId': original['targetId'],
                                           'selectedResources': selected, 'task': final_task} or
            context_set.get('task') != final_task or context_after.get('task') != final_task or
            context_after.get('revision') != revision + 1):
        raise Blocked('final reopened map was not attached to the shared human workspace')
    proposal_input = {'itemId': item_id, 'versionId': version_id,
                      'contentHash': content_hash, 'route': map_receipt['route'],
                      'visibility': 'public'}
    if (expected[14].get('input') != proposal_input or
            proposed.get('status') != 'AwaitingApproval' or
            proposed.get('humanConfirmationRequired') is not True or
            not proposed.get('proposalId') or not proposed.get('operationInstanceId') or
            proposed.get('operation', {}).get('proposalId') != proposed['proposalId'] or
            proposed.get('operation', {}).get('operationInstanceId') != proposed['operationInstanceId'] or
            proposed.get('shareUrl')):
        raise Blocked('external agent did not open the exact final public map for separate approval')
    return {'workspaceId': workspace, 'turnId': turn_id,
            'draftId': draft_id, 'reopenedDraftId': reopened_id,
            'itemId': item_id, 'versionId': version_id, 'contentHash': content_hash,
            'bodySha256': hashlib.sha256(json.dumps(final_body, separators=(',', ':')).encode()).hexdigest(),
            'route': map_receipt['route'], 'audience': 'public',
            'proposalId': proposed['proposalId'],
            'operationInstanceId': proposed['operationInstanceId'],
            'requestedBy': proposed['operation'].get('requestedBy'),
            'visualTurnId': turn_id, 'visualDraftId': visual['draftId'],
            'visualColorExpression': visual['colorExpression'],
            'finalOpacity': 0.55}


def phase_agent_map_finish(args, state, binding, attempt):
    if (state.get('conversationMode') != 'external-agent' or
            state['stages'].get('agentMapVisual') != 'pass' or
            state['stages'].get('agentMapFinish') != 'awaiting-actual-human-visual-edit-and-turn'):
        raise Blocked('final map requires the actual human visual edit and cannot replay')
    if (installed_target(Path(state['installDirectory']), binding) != state.get('target') or
            sha256(attempt / 'mcp.json') != state.get('mcpConfigSha256') or
            sha256(Path(state['executables']['mcp'])) != state.get('mcpExecutableSha256') or
            sha256(attempt / 'agent-map-visual.json') != state.get('agentMapVisualReceiptSha256')):
        raise Blocked('installed target, registration or actual human visual receipt changed')
    visual, first = state['agentMapVisual'], state['agentMap']
    state['stages']['agentMapFinish'] = 'started'
    private_json(attempt / 'receipt.json', state)
    prompt = (f'The same actual human visually edited your attached map in Console and sent a NEW turn. '
              f'Continue the original external Claude session and same Honua registration only. Read '
              f'honua_workspace_conversation for {first["workspaceId"]}, honua_workspace_get_turn for '
              f'{visual["turnId"]}, and bind with honua_agent_connect to that exact turn. Its prompt is '
              f'{json.dumps(visual["prompt"])} and its Studio task must be draft {visual["draftId"]} '
              f'generation {visual["generation"]}. Read honua_studio_get_draft. Preserve its exact current '
              f'mapSpec, sources and all layers; change ONLY render layer {visual["layerId"]} paint property '
              f'{visual["opacityProperty"]} to 0.55 via honua_studio_set_map_style at that generation. '
              f'In particular preserve the human-edited {visual["colorProperty"]} expression '
              f'{json.dumps(visual["colorExpression"])}. Read back, validate, save the exact generation, '
              f'get the saved version, reopen it, and read back the reopened draft. Use honua_workspace_get '
              f'and honua_workspace_set_context with its actual expectedRevision and unchanged selectedResources '
              f'to attach the final reopened draft for future human views; get context again. Finally call '
              f'honua_studio_propose_publication with exact returned itemId, versionId, contentHash, '
              f'route {first["route"]} and visibility public. A separate actual human must approve; you may '
              f'not approve, publish directly, invent a receipt, or retry an uncertain mutation. Inspect each '
              f'successful result before its dependent call.')
    tools = ('honua_workspace_conversation', 'honua_workspace_get_turn', 'honua_agent_connect',
             'honua_agent_status', 'honua_studio_get_draft', 'honua_studio_set_map_style',
             'honua_studio_validate_draft', 'honua_studio_save_version',
             'honua_studio_get_version', 'honua_studio_reopen_version',
             'honua_workspace_get', 'honua_workspace_set_context',
             'honua_studio_propose_publication')
    allowed = ['ToolSearch', 'ListMcpResourcesTool', 'ReadMcpResourceTool', 'WaitForMcpServers'] + [
        'mcp__honua__' + name for name in tools]
    command = [args.claude, '--restricted', '--tools', 'default', '--allowedTools', ','.join(allowed),
               '--disallowedTools', 'Bash,PowerShell,Agent,Read,Write,Edit,NotebookEdit,Glob,Grep,LSP,WebFetch,WebSearch,Workflow,Task',
               '--setting-sources', '', '--settings', '{"disableAllHooks":true}', '--strict-mcp-config',
               '--mcp-config', str(attempt / 'mcp.json'), '--permission-mode', 'dontAsk',
               '--permission-prompts', 'none', '--resume', state['externalClientSessionId'],
               '--output-format', 'stream-json', '--verbose', '--print']
    try:
        result = subprocess.run(command, input=prompt, text=True, capture_output=True,
                                timeout=1200, cwd=attempt, check=False)
        transcript, stderr = attempt / 'agent-map-finish-transcript.ndjson', attempt / 'agent-map-finish-stderr.log'
        for path, content in ((transcript, result.stdout), (stderr, result.stderr)):
            path.write_text(content)
            path.chmod(0o600)
        state['agentMapFinishTranscriptSha256'] = sha256(transcript)
        if result.returncode:
            raise Blocked('external final map continuation failed; reconcile without replay')
        observed_client_session(result.stdout, state['externalClientSessionId'])
        state['agentMapFinish'] = observed_agent_map_finish(
            result.stdout, state['agentImportTask'], first, visual)
        state['stages']['agentMapFinish'] = 'pass'
        state['stages']['agentMapApproval'] = 'awaiting-separate-human-approval'
        state['oneExternalAgentConversationQualified'] = False
        private_json(attempt / 'receipt.json', state)
        print('Same external agent proposed the exact final map; separate human approval and audience remain.')
    except Exception as exc:
        state['stages']['agentMapFinish'] = 'blocked'
        state['blocker'] = str(exc)
        private_json(attempt / 'receipt.json', state)
        raise


def phase_agent_map_audience(args, state, binding, attempt):
    if (state.get('conversationMode') != 'external-agent' or
            state['stages'].get('agentMapApproval') != 'awaiting-separate-human-approval'):
        raise Blocked('exact external-agent map proposal must await a separate human approval')
    if not args.operator_state or not args.operator_state.is_file():
        raise Blocked('actual originating human browser state required for read-only audience witness')
    target = installed_target(Path(state['installDirectory']), binding)
    if (target != state.get('target') or
            args.origin != target['origins']['console'] or
            args.server_origin != target['origins']['server'] or
            target['nativeModelEnabled'] != 'false' or target['nativeGatewayConfigured']):
        raise Blocked('external-only installed target/origin changed before audience witness')
    if (attempt / 'agent-map-audience.json').exists():
        raise Blocked('existing external map audience witness requires read-only inspection')
    install_dir = Path(state['installDirectory'])
    values = dict(line.split('=', 1) for line in (install_dir / '.env').read_text().splitlines()
                  if '=' in line and not line.startswith('#'))
    script = ROOT / 'certification/native-docker-first/agent-map-audience.mjs'
    handoff = attempt / 'agent-map-final-handoff.json'
    if handoff.exists():
        if read_json(handoff) != state['agentMapFinish']:
            raise Blocked('final external map handoff changed before audience reconciliation')
    else:
        private_json(handoff, state['agentMapFinish'], new=True)
    env = {**os.environ, 'HONUA_AGENT_MAP_CONSOLE_ROOT': str(args.console_root),
           'HONUA_AGENT_MAP_FINAL_HANDOFF': str(handoff),
           'HONUA_AGENT_MAP_INITIAL_HANDOFF': str(attempt / 'agent-map-handoff.json'),
           'HONUA_AGENT_MAP_VISUAL_RECEIPT': str(attempt / 'agent-map-visual.json'),
           'HONUA_AGENT_MAP_ORIGIN': args.origin,
           'HONUA_AGENT_MAP_SERVER_ORIGIN': args.server_origin,
           'HONUA_AGENT_MAP_OPERATOR_STATE': str(args.operator_state),
           'HONUA_AGENT_MAP_AGENT_KEY': values['HONUA_AGENT_API_KEY'],
           'HONUA_AGENT_MAP_AUDIENCE_RECEIPT': str(attempt / 'agent-map-audience.json')}
    result = subprocess.run(['node', str(script)], cwd=attempt, env=env,
                            capture_output=True, text=True, timeout=600, check=False)
    log = attempt / 'agent-map-audience.log'
    log.write_text(result.stdout + '\n' + result.stderr)
    log.chmod(0o600)
    if result.returncode:
        state['lastAgentMapAudience'] = 'blocked-or-pending'
        private_json(attempt / 'receipt.json', state)
        raise Blocked('separate approval, public data or rendered audience witness absent; inspect private log')
    witness = read_json(attempt / 'agent-map-audience.json')
    final, visual, first = state['agentMapFinish'], state['agentMapVisual'], state['agentMap']
    if (witness.get('status') != 'pass' or
            any(witness.get(key) != final.get(key) for key in
                ('proposalId', 'operationInstanceId', 'itemId', 'versionId', 'contentHash', 'route')) or
            witness.get('originConsolePartition') != visual['principalId'] or
            witness.get('serviceId') != first['service']['serviceId'] or
            witness.get('serviceName') != first['service']['serviceName'] or
            witness.get('sourceId') != first['sourceId'] or
            witness.get('publicFeatureCount') != first['service']['featureCount'] or
            type(witness.get('renderedFeatureCount')) is not int or
            witness['renderedFeatureCount'] <= 0 or
            not witness.get('requestId') or
            not witness.get('requestedBy') or not witness.get('resolvedBy') or
            witness['requestedBy'] == witness['resolvedBy']):
        state['stages']['agentMapApproval'] = 'blocked'
        private_json(attempt / 'receipt.json', state)
        raise Blocked('audience witness does not join exact service, real approver and final external map')
    state['agentMapAudience'] = witness
    state['agentMapAudienceScriptSha256'] = sha256(script)
    state['stages']['agentMapApproval'] = 'pass'
    state['stages']['agentMapAudience'] = 'pass'
    state['oneExternalAgentConversationQualified'] = True
    state['status'] = 'one-external-agent-conversation-pass'
    private_json(attempt / 'receipt.json', state)
    print('One external-agent conversation joined to actual human approval, public data and rendered map.')


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
    parser.add_argument('phase', choices=('start', 'install', 'agent-task', 'agent-submit',
                                         'agent-observe-import', 'agent-service', 'agent-map',
                                         'agent-map-visual', 'agent-map-finish', 'agent-map-audience',
                                         'browser', 'witness'))
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
    parser.add_argument('--conversation-mode', choices=('external-agent', 'joined-native'))
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
            if args.conversation_mode and args.conversation_mode != state.get('conversationMode'):
                raise Blocked('requested conversation mode differs from the persisted install mode')
            if args.phase.startswith('agent-') and state.get('conversationMode') != 'external-agent':
                raise Blocked('same external-agent conversation requires external-only installation mode')
            if args.phase == 'install':
                phase_install(args, state, binding, args.attempt)
            elif args.phase == 'agent-task':
                phase_agent_task(args, state, binding, args.attempt)
            elif args.phase == 'agent-submit':
                phase_agent_submit(args, state, binding, args.attempt)
            elif args.phase == 'agent-observe-import':
                phase_agent_observe_import(args, state, binding, args.attempt)
            elif args.phase == 'agent-service':
                phase_agent_service(args, state, binding, args.attempt)
            elif args.phase == 'agent-map':
                phase_agent_map(args, state, binding, args.attempt)
            elif args.phase == 'agent-map-visual':
                phase_agent_map_visual(args, state, binding, args.attempt)
            elif args.phase == 'agent-map-finish':
                phase_agent_map_finish(args, state, binding, args.attempt)
            elif args.phase == 'agent-map-audience':
                phase_agent_map_audience(args, state, binding, args.attempt)
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
