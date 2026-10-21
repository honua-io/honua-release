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
        completed = '\n'.join((inspect, install, result('install-1', {'state': 'attached', 'target': target}),
                               status, result('s', {'state': 'attached', 'target': target})))
        self.assertEqual(runner.observed_install_call(completed)['toolCallId'], 'install-1')
        for bad in ('\n'.join((inspect, status)), '\n'.join((install, install, status)),
                    '\n'.join((inspect, install)),
                    '\n'.join((install, result('install-1', {'state': 'attached', 'target': target}),
                               status, result('s', {'state': 'unattached', 'target': target})))):
            with self.assertRaises(runner.Blocked):
                runner.observed_install_call(bad)

    def test_default_old_manifest_refused_before_any_install(self):
        with self.assertRaisesRegex(runner.Blocked, '0.2.7'):
            runner.candidate(runner.ROOT / 'platform-manifest.yaml')

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
            (root / '.env').write_text('HONUA_AGENT_API_KEY=a\nHONUA_AGENT_KEY_ID=id\nPOSTGRES_PASSWORD=private\nHONUA_SERVER_IMAGE=server-image\nHONUA_CONSOLE_IMAGE=console-image\nHONUA_AGENT_RELEASE_ID=release\n')
            binding = {'server': {'image': 'server-image'}, 'console': {'image': 'console-image'}, 'releaseId': 'release'}
            self.assertEqual(runner.installed_target(root, binding)['agentKeyId'], 'id')
            with self.assertRaisesRegex(runner.Blocked, 'immutable images'):
                runner.installed_target(root, {**binding, 'server': {'image': 'other'}})
            (root / '.env').write_text('HONUA_AGENT_API_KEY=a\nPOSTGRES_PASSWORD=private\n')
            with self.assertRaisesRegex(runner.Blocked, 'credentials'):
                runner.installed_target(root, binding)

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
                     'installDirectory': str(root), 'target': {'agentKeyId': 'id'}}
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
                     'installDirectory': str(root), 'target': {'agentKeyId': 'id'}}
            args = SimpleNamespace(operator_state=operator, origin='http://127.0.0.1:8081',
                                   server_origin='http://127.0.0.1:8080', console_root=root)
            with patch.object(runner, 'installed_target', return_value=state['target']), patch.object(runner, 'playwright_command', return_value=['playwright']), patch.object(runner.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='', stderr='')):
                with self.assertRaisesRegex(runner.Blocked, 'witness absent'):
                    runner.phase_witness(args, state, {}, root)
            self.assertEqual(state['stages']['approval'], 'awaiting-separate-human-approval')
            self.assertNotIn('status', state)


if __name__ == '__main__':
    unittest.main()
