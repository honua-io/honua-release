"""Evidence joins for the real external-client service continuation; not a live qualification."""
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('native_service_run', Path(__file__).with_name('run.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class AgentServiceTests(unittest.TestCase):
    def setUp(self):
        self.original = {'workspaceId': 'workspace-1', 'turnId': 'import-turn',
                         'targetId': 'target-1', 'tenantId': 'tenant-1',
                         'taskId': 'import-1', 'revision': 1, 'contentHash': 'a' * 64}
        self.imported = {'status': 'succeeded', 'output': {
            'schema': 'public', 'physicalTableName': 'imported_table', 'featureCount': 3}}
        self.names = {'connection': 'native-test', 'service': 'native-test', 'layer': 'Imported test'}
        self.locator = {'workspaceId': 'workspace-1', 'turnId': 'service-turn'}
        messages = [{'role': 'user', 'turnId': 'service-turn', 'content': runner.SERVICE_PROMPT}]
        identity = {'serviceId': 'svc-publish-native-test', 'layerId': 42}
        connection = {'name': 'native-test', 'secretReference': 'env:HONUA_LOCAL_DB_PASSWORD',
                      'secretType': 'env', 'sslRequired': False, 'sslMode': 'Disable'}
        publication = {'connectionId': 'connection-1', 'schema': 'public', 'table': 'imported_table',
                       'layerName': 'Imported test', 'serviceName': 'native-test', 'enabled': True}
        def handle(data, instance):
            return {'status': 'Completed', 'requiresApproval': False, 'operationInstanceId': instance,
                    'details': {'response': json.dumps({'success': True, 'data': data})}}
        exchanges = [
            ('honua_workspace_conversation', {'workspaceId': 'workspace-1'},
             {'messages': messages}),
            ('honua_workspace_get_turn', self.locator,
             {'turnId': 'service-turn', 'messages': messages, 'context': {
                 **{key: self.original[key] for key in ('workspaceId', 'targetId', 'tenantId')},
                 'task': {'kind': 'dataset-import', 'id': 'import-1', 'revision': 1, 'contentHash': 'a' * 64}}}),
            ('honua_agent_connect', self.locator, {'state': 'attached', 'turnBinding': self.locator}),
            ('honua_admin_connections_create', connection,
             handle({'connectionId': 'connection-1', 'name': 'native-test'}, 'create-1')),
            ('honua_admin_connections_test', {'id': 'connection-1'},
             handle({'connectionId': 'connection-1', 'isHealthy': True}, 'test-1')),
            ('honua_admin_layer_publish', publication,
             handle({**publication, 'layerId': 42}, 'publish-1')),
            ('honua_describe_layer', {'serviceId': 'native-test', 'layerId': 42, 'includeRowCount': True},
             {**identity, 'serviceName': 'native-test', 'studioSourceId': 'catalog-source-42', 'rowCount': 3,
              'fields': [{'name': 'id', 'type': 'int64'}]}),
            ('honua_query_features', {**identity, 'returnCountOnly': True}, {**identity, 'count': 3}),
        ]
        self.events = []
        for index, (name, arguments, output) in enumerate(exchanges):
            identifier = 'call-' + str(index)
            self.events.extend([
                {'type': 'assistant', 'message': {'content': [{'type': 'tool_use',
                    'name': 'mcp__honua__' + name, 'id': identifier, 'input': arguments}]}},
                {'type': 'user', 'message': {'content': [{'type': 'tool_result',
                    'tool_use_id': identifier, 'content': 'Model-visible summary is not the evidence.'}]},
                 'tool_use_result': {'structuredContent': output}},
            ])

    def observe(self, events=None):
        return runner.observed_agent_service('\n'.join(json.dumps(event) for event in
            (self.events if events is None else events)), self.original, self.imported,
            'service-turn', self.names)

    def test_joins_real_results_from_fresh_human_turn_to_exact_imported_table(self):
        observed = self.observe()
        self.assertEqual(observed['studioSourceId'], 'catalog-source-42')
        self.assertEqual(observed['featureCount'], 3)
        self.assertEqual(observed['serviceId'], 'svc-publish-native-test')
        self.assertEqual(observed['serviceName'], 'native-test')
        self.assertEqual(observed['operationInstanceIds'], ['create-1', 'test-1', 'publish-1'])

    def test_model_prose_cannot_replace_structured_query_or_success(self):
        for index in (1, 7, 15):
            broken = copy.deepcopy(self.events)
            del broken[index]['tool_use_result']
            broken[index]['message']['content'][0]['content'] = json.dumps({'count': 3, 'status': 'Completed'})
            with self.subTest(index=index), self.assertRaises(runner.Blocked):
                self.observe(broken)
        broken = copy.deepcopy(self.events)
        broken[15]['tool_use_result']['isError'] = True
        with self.assertRaises(runner.Blocked):
            self.observe(broken)

    def test_rejects_changed_identity_table_credential_health_and_query(self):
        changes = [(2, 'targetId', 'other-target'), (2, 'tenantId', 'other-tenant'),
                   (6, 'secretReference', 'env:ANOTHER_SECRET'), (6, 'password', 'must-not-be-used'),
                   (8, 'id', 'other-connection'), (10, 'table', 'other_table'),
                   (10, 'dryRun', True), (12, 'layerId', 99), (14, 'serviceId', 'other-service')]
        for index, key, value in changes:
            broken = copy.deepcopy(self.events)
            target = (broken[index + 1]['tool_use_result']['structuredContent']['context']
                      if index == 2 else broken[index]['message']['content'][0]['input'])
            target[key] = value
            with self.subTest(key=key), self.assertRaises(runner.Blocked):
                self.observe(broken)
        for index, key, value in ((13, 'rowCount', 0), (13, 'studioSourceId', ''),
                                   (13, 'serviceName', 'other-service'), (13, 'serviceId', ''),
                                   (15, 'serviceId', 'native-test'),
                                   (15, 'count', True), (15, 'count', 2)):
            broken = copy.deepcopy(self.events)
            broken[index]['tool_use_result']['structuredContent'][key] = value
            with self.subTest(key=key), self.assertRaises(runner.Blocked):
                self.observe(broken)
        broken = copy.deepcopy(self.events)
        broken[14]['message']['content'][0]['input']['serviceId'] = 'native-test'
        with self.assertRaises(runner.Blocked):
            self.observe(broken)
        for key, value in (('isHealthy', False), ('connectionId', 'other-connection')):
            broken = copy.deepcopy(self.events)
            health = broken[9]['tool_use_result']['structuredContent']
            health['details']['response'] = json.dumps({'success': True,
                'data': {'connectionId': 'connection-1', 'isHealthy': True, key: value}})
            with self.subTest(key=key), self.assertRaises(runner.Blocked):
                self.observe(broken)

    def test_pending_approval_wrong_turn_and_parallel_dependent_calls_never_pass(self):
        for index in (7, 9, 11):
            for status in ('RequiresApproval', 'Queued', 'Denied', 'Failed'):
                broken = copy.deepcopy(self.events)
                broken[index]['tool_use_result']['structuredContent']['status'] = status
                with self.subTest(status=status), self.assertRaises(runner.Blocked):
                    self.observe(broken)
        broken = copy.deepcopy(self.events)
        broken[1]['tool_use_result']['structuredContent']['messages'][0]['role'] = 'assistant'
        with self.assertRaises(runner.Blocked):
            self.observe(broken)
        for index in (1, 3):
            broken = copy.deepcopy(self.events)
            broken[index]['tool_use_result']['structuredContent']['messages'][0]['content'] = 'Just show me the data.'
            with self.assertRaises(runner.Blocked):
                self.observe(broken)
        for field, value in (('id', 'other-import'), ('contentHash', 'b' * 64), ('revision', 2)):
            broken = copy.deepcopy(self.events)
            broken[3]['tool_use_result']['structuredContent']['context']['task'][field] = value
            with self.assertRaises(runner.Blocked):
                self.observe(broken)
        for events in (self.events[:7] + [self.events[8], self.events[7]] + self.events[9:],
                       self.events + [self.events[11]], self.events + [self.events[10]]):
            with self.assertRaises(runner.Blocked):
                self.observe(events)

    def test_phase_refuses_replay_or_replaced_registration_before_launching_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, executable = root / 'mcp.json', root / 'mcp'
            config.write_text('{}')
            executable.write_text('original')
            state = {'stages': {'agentImportObservation': 'pass'}, 'agentImportTask': self.original,
                     'agentImportObservation': self.imported, 'installDirectory': str(root),
                     'target': {'id': 'original'}, 'executables': {'mcp': str(executable)},
                     'mcpConfigSha256': runner.sha256(config),
                     'mcpExecutableSha256': runner.sha256(executable)}
            args = SimpleNamespace(workspace_id='workspace-1', turn_id='service-turn')
            with patch.object(runner, 'installed_target', return_value=state['target']), \
                    patch.object(runner.subprocess, 'run') as client:
                config.write_text('{"changed":true}')
                with self.assertRaisesRegex(runner.Blocked, 'registration/configuration changed'):
                    runner.phase_agent_service(args, state, {}, root)
                client.assert_not_called()
                state['stages']['agentService'] = 'started'
                with self.assertRaisesRegex(runner.Blocked, 'cannot replay'):
                    runner.phase_agent_service(args, state, {}, root)
                client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
