#!/usr/bin/env python3
"""Offline contract checks; no Docker, model, human session, or publication is faked as passed."""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

MODULE = Path(__file__).with_name('run.py')
spec = importlib.util.spec_from_file_location('native_docker_run', MODULE)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class NativeFirstUseTests(unittest.TestCase):
    def test_agent_continuation_refuses_changed_mcp_registration_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, executable, source = (root / name for name in ('mcp.json', 'honua-agent-mcp', 'source.geojson'))
            config.write_text('{"mcpServers":{"honua":{}}}')
            executable.write_text('original executable')
            source.write_text('{}')
            state = {'stages': {'agentTask': 'awaiting-human-browser-file-turn'},
                     'installDirectory': str(root), 'target': {'id': 'installed'},
                     'executables': {'mcp': str(executable)},
                     'mcpConfigSha256': runner.sha256(config),
                     'mcpExecutableSha256': runner.sha256(executable)}
            args = SimpleNamespace(input_file=source, workspace_id='workspace-1', turn_id='turn-1')
            executable.write_text('replaced executable')
            with patch.object(runner, 'installed_target', return_value=state['target']):
                with self.assertRaisesRegex(runner.Blocked, 'registration/configuration changed'):
                    runner.phase_agent_task(args, state, {'server': 'pinned'}, root)

    def test_native_client_install_refuses_peer_bypass_before_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = runner.pins.ResolvedArtifact(
                name='honua-mcp-server', package='@honua/mcp-server', version='0.2.1',
                ecosystem='npm', registry_url=None, integrity_verified=True,
                tarball_sha256='a' * 64, bin={}, tarball=root / 'verified.tgz')
            workspace = runner.pins.ClientWorkspace('pass', root, None, [artifact])
            failed = SimpleNamespace(returncode=1, stderr='ERESOLVE peer dependency conflict', stdout='')
            with patch.object(runner.pins, '_npm_install', return_value=failed) as install:
                bindir, reason, notes = runner.pins.install_executables(
                    workspace, root / 'client', require_default_peers=True)
            self.assertIsNone(bindir)
            self.assertIn('default peer resolution', reason)
            self.assertEqual(notes, [])
            install.assert_called_once()

    def test_resumed_external_client_binds_original_human_file_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.geojson'
            source.write_text('{"type":"FeatureCollection","features":[]}')
            digest = runner.sha256(source)
            selection = {'selectionId': 'selected-1', 'fileName': source.name,
                         'totalBytes': source.stat().st_size, 'expectedSha256': digest}
            context = {'workspaceId': 'workspace-1', 'targetId': 'target-1', 'tenantId': 'tenant-1',
                       'viewId': 'view-1', 'revision': 2}
            request = {'intentId': 'intent-1', 'viewId': 'view-1', 'contextRevision': 2,
                       'destination': {'schema': 'public', 'table': 'new'},
                       'source': {'kind': 'browser-file', 'browserSelectionId': 'selected-1',
                                  **{key: selection[key] for key in ('fileName', 'totalBytes', 'expectedSha256')},
                                  'artifact': None}}
            def call(name, identifier, value):
                return json.dumps({'message': {'content': [{'type': 'tool_use', 'name': 'mcp__honua__' + name,
                                                           'id': identifier, 'input': value}]}})
            def result(identifier, value):
                return json.dumps({'message': {'content': [{'type': 'tool_result',
                                                           'tool_use_id': identifier, 'content': json.dumps(value)}]}})
            events = [json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 'session-1'}),
                      call('honua_workspace_conversation', 'c', {'workspaceId': 'workspace-1'}),
                      result('c', {'messages': [{'turnId': 'turn-1', 'role': 'user'}]}),
                      call('honua_workspace_get_turn', 't', {'workspaceId': 'workspace-1', 'turnId': 'turn-1'}),
                      result('t', {'turnId': 'turn-1', 'context': context, 'browserFileSelection': selection}),
                      call('honua_agent_connect', 'b', {'workspaceId': 'workspace-1', 'turnId': 'turn-1'}),
                      result('b', {'state': 'attached', 'turnBinding': {'workspaceId': 'workspace-1', 'turnId': 'turn-1'}}),
                      call('honua_workspace_create_dataset_import_task', 'i', {'workspaceId': 'workspace-1', 'request': request}),
                      result('i', {'taskId': 'task-1', 'revision': 1, 'contentHash': 'a'*64,
                                   'workspaceId': 'workspace-1', 'targetId': 'target-1', 'tenantId': 'tenant-1',
                                   'source': {key: value for key, value in request['source'].items() if value is not None},
                                   'destination': request['destination']})]
            transcript = '\n'.join(events)
            runner.observed_client_session(transcript, 'session-1')
            self.assertEqual(runner.observed_agent_task(transcript, 'workspace-1', 'turn-1', 'intent-1', source)['taskId'], 'task-1')
            with self.assertRaisesRegex(runner.Blocked, 'same persistent'):
                runner.observed_client_session(transcript, 'different-session')
            for bad in ('turnBinding', 'browserFileSelection', 'task', 'workspace', 'target',
                        'tenant', 'destination', 'artifact'):
                broken = list(events)
                index = {'turnBinding': 6, 'browserFileSelection': 4}.get(bad, 8)
                payload = json.loads(broken[index])
                output = json.loads(payload['message']['content'][0]['content'])
                if bad == 'turnBinding': output['turnBinding']['turnId'] = 'other-turn'
                elif bad == 'browserFileSelection': output['browserFileSelection']['expectedSha256'] = 'b'*64
                elif bad == 'task': output['source']['browserSelectionId'] = 'other-selection'
                elif bad == 'workspace': output['workspaceId'] = 'foreign-workspace'
                elif bad == 'target': output['targetId'] = 'foreign-target'
                elif bad == 'tenant': output['tenantId'] = 'foreign-tenant'
                elif bad == 'destination': output['destination']['table'] = 'foreign_table'
                else: output['source']['artifact'] = {'projectId': 'foreign'}
                payload['message']['content'][0]['content'] = json.dumps(output)
                broken[index] = json.dumps(payload)
                with self.assertRaises(runner.Blocked):
                    runner.observed_agent_task('\n'.join(broken), 'workspace-1', 'turn-1', 'intent-1', source)

    def test_external_client_requires_one_real_install_call_and_later_status(self):
        def event(name, identifier, input_value=None):
            return json.dumps({'type': 'assistant', 'message': {'content': [
                {'type': 'tool_use', 'name': name, 'id': identifier, 'input': input_value or {}}]}})
        inspect = event('mcp__honua__honua_agent_inspect_local', 'i')
        install = event('mcp__honua__honua_agent_install_local', 'install-1', {'confirm': True})
        status = event('mcp__honua__honua_agent_status', 's')
        target = {'id': 't', 'credentialId': 'k', 'endpoint': 'http://127.0.0.1:8080/mcp'}
        def result(identifier, value):
            return json.dumps({'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': identifier, 'content': json.dumps(value)}]}})
        completed = '\n'.join((inspect, result('i', {'installed': False}), install,
                               result('install-1', {'state': 'attached', 'target': target}),
                               status, result('s', {'state': 'attached', 'target': target})))
        self.assertEqual(runner.observed_install_call(completed)['toolCallId'], 'install-1')
        for bad in ('\n'.join((inspect, status)), '\n'.join((install, install, status)),
                    '\n'.join((inspect, install)),
                    '\n'.join((inspect, install, result('install-1', {'state': 'attached', 'target': target}),
                               status, result('s', {'state': 'attached', 'target': target}))),
                    '\n'.join((install, result('install-1', {'state': 'attached', 'target': target}),
                               status, result('s', {'state': 'unattached', 'target': target})))):
            with self.assertRaises(runner.Blocked):
                runner.observed_install_call(bad)

    def test_candidate_does_not_invent_sdk_mcp_version_coupling(self):
        manifest, _ = runner.candidate(runner.ROOT / 'platform-manifest.yaml')
        manifest['clientArtifacts']['honua-mcp-server']['version'] = '0.2.1'
        with patch.object(runner.yaml, 'safe_load', return_value=manifest):
            _, binding = runner.candidate(runner.ROOT / 'platform-manifest.yaml')
        self.assertEqual(binding['clients']['honua-sdk-js']['version'], '0.2.3')
        self.assertEqual(binding['clients']['honua-mcp-server']['version'], '0.2.1')

    def test_attempt_binding_rejects_changed_artifacts_or_browser_producer(self):
        state = {'schemaVersion': runner.SCHEMA, 'candidate': {'server': 'a'},
                 'harness': {'sourceSha': 'b'}}
        runner.check_state(state, state['candidate'], state['harness'])
        with self.assertRaisesRegex(runner.Blocked, 'binding changed'):
            runner.check_state(state, {'server': 'c'}, state['harness'])
        with self.assertRaisesRegex(runner.Blocked, 'binding changed'):
            runner.check_state(state, state['candidate'], {'sourceSha': 'c'})

    def test_installed_target_rejects_wrong_image_and_incomplete_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'compose.yaml').write_text('image: ${HONUA_SERVER_IMAGE}\nimage: ${HONUA_CONSOLE_IMAGE}\n')
            (root / '.env').write_text('HONUA_AGENT_API_KEY=a\nHONUA_AGENT_KEY_ID=id\nPOSTGRES_PASSWORD=private\nHONUA_SERVER_IMAGE=server-image\nHONUA_CONSOLE_IMAGE=console-image\nHONUA_AGENT_RELEASE_ID=release\nHONUA_HTTP_PORT=8080\nHONUA_CONSOLE_PORT=8081\nHONUA_COMPOSE_PROJECT=honua-local-' + 'a'*24 + '\n')
            binding = {'server': {'image': 'server-image'}, 'console': {'image': 'console-image', 'sha': 'b'*40}, 'releaseId': 'release'}
            with patch.object(runner, 'running_containers', return_value={'server': {'id': 's'}, 'console': {'id': 'c'}}), patch.object(runner, 'live_console_sha', return_value='b'*40):
                self.assertEqual(runner.installed_target(root, binding)['agentKeyId'], 'id')
            with self.assertRaisesRegex(runner.Blocked, 'immutable images'):
                runner.installed_target(root, {**binding, 'server': {'image': 'other'}})
            (root / '.env').write_text('HONUA_AGENT_API_KEY=a\nPOSTGRES_PASSWORD=private\n')
            with self.assertRaisesRegex(runner.Blocked, 'credentials'):
                runner.installed_target(root, binding)

    def test_running_containers_rejects_replaced_image_and_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            project = 'honua-local-' + 'a'*24
            binding = {'server': {'image': 'server@sha256:1'}, 'console': {'image': 'console@sha256:2'},
                       'releaseId': 'release'}
            rows = '\n'.join(json.dumps({'Service': service, 'State': 'running', 'ID': service + '-id'})
                             for service in ('honua', 'console'))
            def inspect(command, _directory):
                service = command[-1].removesuffix('-id')
                image = binding['server' if service == 'honua' else 'console']['image']
                return [{'Id': command[-1], 'Image': service + '-image-id', 'State': {'Running': True},
                         'NetworkSettings': {'Ports': {'8080/tcp': [{'HostIp': '127.0.0.1',
                            'HostPort': '8080' if service == 'honua' else '8081'}]}},
                         'Config': {'Image': image, 'Labels': {'com.docker.compose.project': project,
                                     'com.docker.compose.service': service},
                                    'Env': ['HONUA_AGENT_RELEASE_ID=release']}}]
            completed = SimpleNamespace(returncode=0, stdout=rows)
            with patch.object(runner.subprocess, 'run', return_value=completed), patch.object(runner, 'docker_json', side_effect=inspect):
                ports = {'server': '8080', 'console': '8081'}
                self.assertEqual(runner.running_containers(directory, project, binding, ports)['server']['id'], 'honua-id')
                with self.assertRaisesRegex(runner.Blocked, 'selected image'):
                    runner.running_containers(directory, project, {**binding, 'server': {'image': 'replacement'}}, ports)
                with self.assertRaisesRegex(runner.Blocked, 'selected image'):
                    runner.running_containers(directory, 'honua-local-' + 'b'*24, binding, ports)
                with self.assertRaisesRegex(runner.Blocked, 'loopback origin'):
                    runner.running_containers(directory, project, binding,
                                              {'server': '9090', 'console': '8081'})

    def test_witness_rejects_missing_or_different_human_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handoff = {'proposalId': 'p', 'operationInstanceId': 'o', 'commandId': 'c', 'requestId': 'r',
                       'itemId': 'i', 'versionId': 'v', 'contentHash': 'h', 'route': '/map', 'requestedBy': 'human-a'}
            runner.private_json(root / 'publication-handoff.json', handoff, new=True)
            operator = root / 'operator.json'
            operator.write_text('{}')
            (root / '.env').write_text('HONUA_AGENT_API_KEY=agent-only\n')
            target = {'origins': {'server': 'http://127.0.0.1:8080', 'console': 'http://127.0.0.1:8081'}}
            args = SimpleNamespace(operator_state=operator, origin=target['origins']['console'],
                                   server_origin=target['origins']['server'], console_root=root)
            for requested, resolved in ((None, 'human-b'), ('human-c', 'human-b'), ('human-a', None),
                                        ('human-a', 'human-a')):
                state = {'stages': {'approval': 'awaiting-separate-human-approval'}, 'publication': handoff,
                         'installDirectory': str(root), 'target': target}
                witness = {**handoff, 'status': 'pass', 'requestedBy': requested, 'resolvedBy': resolved}
                def fake_run(*_args, **_kwargs):
                    runner.private_json(root / 'audience-witness.json', witness)
                    return SimpleNamespace(returncode=0, stdout='', stderr='')
                with patch.object(runner, 'installed_target', return_value=target), patch.object(runner, 'playwright_command', return_value=['playwright']), patch.object(runner.subprocess, 'run', side_effect=fake_run):
                    with self.assertRaisesRegex(runner.Blocked, 'distinct human approval'):
                        runner.phase_witness(args, state, {}, root)
                (root / 'audience-witness.json').unlink()

    def test_witness_never_upgrades_mismatched_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handoff = {'proposalId': 'p', 'operationInstanceId': 'o', 'commandId': 'c', 'requestId': 'r', 'itemId': 'i',
                       'versionId': 'v', 'contentHash': 'h', 'route': '/new-map'}
            runner.private_json(root / 'publication-handoff.json', handoff, new=True)
            operator = root / 'operator.json'
            operator.write_text('{}')
            (root / '.env').write_text('HONUA_AGENT_API_KEY=agent-only\n')
            state = {'stages': {'approval': 'awaiting-separate-human-approval'}, 'publication': handoff,
                     'installDirectory': str(root), 'target': {'agentKeyId': 'id', 'origins': {'server': 'http://127.0.0.1:8080', 'console': 'http://127.0.0.1:8081'}}}
            args = SimpleNamespace(operator_state=operator, origin='http://127.0.0.1:8081',
                                   server_origin='http://127.0.0.1:8080', console_root=root)
            bad = {**handoff, 'status': 'pass', 'versionId': 'other',
                   'requestedBy': 'actor-a', 'resolvedBy': 'actor-b'}
            def fake_run(*_args, **_kwargs):
                runner.private_json(root / 'audience-witness.json', bad, new=True)
                return SimpleNamespace(returncode=0, stdout='', stderr='')
            with patch.object(runner, 'installed_target', return_value=state['target']), patch.object(runner, 'playwright_command', return_value=['playwright']), patch.object(runner.subprocess, 'run', side_effect=fake_run):
                with self.assertRaisesRegex(runner.Blocked, 'does not bind'):
                    runner.phase_witness(args, state, {}, root)
            self.assertNotEqual(state.get('status'), 'pass')
            self.assertEqual(state['stages']['approval'], 'blocked')

    def test_pending_approval_is_retriable_only_as_read_only_witness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handoff = {'proposalId': 'p'}
            runner.private_json(root / 'publication-handoff.json', handoff, new=True)
            operator = root / 'operator.json'
            operator.write_text('{}')
            (root / '.env').write_text('HONUA_AGENT_API_KEY=agent-only\n')
            state = {'stages': {'approval': 'awaiting-separate-human-approval'}, 'publication': handoff,
                     'installDirectory': str(root), 'target': {'agentKeyId': 'id', 'origins': {'server': 'http://127.0.0.1:8080', 'console': 'http://127.0.0.1:8081'}}}
            args = SimpleNamespace(operator_state=operator, origin='http://127.0.0.1:8081',
                                   server_origin='http://127.0.0.1:8080', console_root=root)
            with patch.object(runner, 'installed_target', return_value=state['target']), patch.object(runner, 'playwright_command', return_value=['playwright']), patch.object(runner.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='', stderr='')):
                with self.assertRaisesRegex(runner.Blocked, 'witness absent'):
                    runner.phase_witness(args, state, {}, root)
            self.assertEqual(state['stages']['approval'], 'awaiting-separate-human-approval')
            self.assertNotIn('status', state)


if __name__ == '__main__':
    unittest.main()
