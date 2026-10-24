"""Offline negative evidence joins for same-client map stages; never live qualification."""
import copy
import hashlib
import importlib.util
import json
import subprocess
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

spec = importlib.util.spec_from_file_location('native_map_run', Path(__file__).with_name('run.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def transcript(exchanges):
    events = []
    for index, (name, arguments, output) in enumerate(exchanges):
        identifier = f'call-{index}'
        events.extend((
            {'type': 'assistant', 'message': {'content': [{'type': 'tool_use',
                'name': 'mcp__honua__' + name, 'id': identifier, 'input': arguments}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result',
                'tool_use_id': identifier, 'content': 'Prose is not a receipt.'}]},
             'tool_use_result': {'structuredContent': output}},
        ))
    return events


def wire(events):
    return '\n'.join(json.dumps(event) for event in events)


class AgentMapEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.original = {'workspaceId': 'workspace-1', 'turnId': 'import-turn', 'targetId': 'target-1',
                         'tenantId': 'tenant-1', 'taskId': 'import-1', 'revision': 1,
                         'contentHash': 'a' * 64}
        self.service = {'workspaceId': 'workspace-1', 'turnId': 'service-turn',
                        'serviceId': 'svc-publish-map-1', 'serviceName': 'map-1',
                        'layerId': 42, 'studioSourceId': 'source-42', 'featureCount': 3}
        self.turn = {'workspaceId': 'workspace-1', 'turnId': 'map-turn'}
        task = {'kind': 'dataset-import', 'id': 'import-1', 'revision': 1,
                'contentHash': 'a' * 64}
        self.body = {'layers': [{'id': 'layer-1', 'sourceId': 'source-42',
                                 'styleRef': 'honua-inline:layer-1'}],
                     'styleRefs': [{'styleId': 'honua-inline:layer-1',
                                    'body': {'layer-1': {'paint': {'circle-color': '#123456'}}}}],
                     'view': {'bbox': [-1, -1, 1, 1], 'crs': 'EPSG:4326'}}
        self.exchanges = [
            ('honua_workspace_conversation', {'workspaceId': 'workspace-1'},
             {'messages': [{'role': 'user', 'turnId': 'map-turn', 'content': runner.MAP_PROMPT}]}),
            ('honua_workspace_get_turn', self.turn, {'turnId': 'map-turn',
             'messages': [{'role': 'user', 'turnId': 'map-turn', 'content': runner.MAP_PROMPT}],
             'context': {**{key: self.original[key] for key in ('workspaceId', 'targetId', 'tenantId')},
                         'viewId': 'view-1', 'task': task}}),
            ('honua_agent_connect', self.turn, {'state': 'attached', 'turnBinding': self.turn}),
            ('honua_describe_layer', {'serviceId': 'map-1', 'layerId': 42, 'includeRowCount': True},
             {'serviceId': 'svc-publish-map-1', 'serviceName': 'map-1', 'layerId': 42,
              'studioSourceId': 'source-42', 'rowCount': 3, 'srid': 4326,
              'extent': {'minX': -0.8, 'minY': -0.8, 'maxX': 0.8, 'maxY': 0.8}}),
            ('honua_studio_create_draft', {'packageKey': 'map-package', 'family': 'map',
             'schemaVersion': '1.0'}, {'draftId': 'draft-1', 'itemId': 'item-1',
             'packageKey': 'map-package', 'family': 'map', 'generation': 1}),
            ('honua_studio_add_layer', {'draftId': 'draft-1', 'generation': 1,
             'layer': {'id': 'layer-1', 'sourceId': 'source-42', 'visible': True, 'type': 'circle'}},
             {'draftId': 'draft-1', 'generation': 2}),
            ('honua_studio_set_layer_style', {'draftId': 'draft-1', 'generation': 2,
             'layerId': 'layer-1', 'styleBody': {'layer-1': {'paint': {'circle-color': '#123456'}}}},
             {'generation': 3}),
            ('honua_studio_set_view', {'draftId': 'draft-1', 'generation': 3,
             'view': {'bbox': [-1, -1, 1, 1], 'crs': 'EPSG:4326'}}, {'generation': 4}),
            ('honua_studio_get_draft', {'draftId': 'draft-1'},
             {'draftId': 'draft-1', 'generation': 4, 'envelope': {'body': self.body}}),
            ('honua_studio_validate_draft', {'draftId': 'draft-1'}, {'status': 'valid'}),
            ('honua_studio_save_version', {'draftId': 'draft-1', 'generation': 4},
             {'version': {'itemId': 'item-1', 'versionId': 'version-1', 'contentHash': 'b' * 64}}),
            ('honua_studio_get_version', {'itemId': 'item-1', 'versionId': 'version-1'},
             {'versionId': 'version-1', 'contentHash': 'b' * 64}),
            ('honua_studio_reopen_version', {'itemId': 'item-1', 'versionId': 'version-1'},
             {'draftId': 'draft-2', 'itemId': 'item-1'}),
            ('honua_studio_get_draft', {'draftId': 'draft-2'},
             {'draftId': 'draft-2', 'itemId': 'item-1', 'generation': 1,
              'envelope': {'body': self.body}}),
            ('honua_workspace_get', {'workspaceId': 'workspace-1', 'viewId': 'view-1'},
             {'workspaceId': 'workspace-1', 'viewId': 'view-1', 'targetId': 'target-1',
              'revision': 4, 'selectedResources': [], 'task': task}),
            ('honua_workspace_set_context', {'workspaceId': 'workspace-1', 'viewId': 'view-1',
             'expectedRevision': 4, 'targetId': 'target-1', 'selectedResources': [],
             'task': {'kind': 'studio-composition', 'id': 'draft-2', 'revision': 1,
                      'packageKey': 'map-package'}}, {'task': {'kind': 'studio-composition',
             'id': 'draft-2', 'revision': 1, 'packageKey': 'map-package'}}),
            ('honua_workspace_get', {'workspaceId': 'workspace-1', 'viewId': 'view-1'},
             {'revision': 5, 'task': {'kind': 'studio-composition', 'id': 'draft-2',
              'revision': 1, 'packageKey': 'map-package'}}),
        ]

    def observe(self, exchanges=None):
        return runner.observed_agent_map(wire(transcript(exchanges or self.exchanges)),
                                         self.original, self.service, 'map-turn',
                                         'map-package', 'layer-1', '/maps/map-1')

    def test_agent_map_is_exact_original_source_and_attached_reopened_draft(self):
        value = self.observe()
        self.assertEqual(value['reopenedDraftId'], 'draft-2')
        self.assertEqual(value['sourceId'], 'source-42')
        self.assertEqual(value['service']['serviceId'], 'svc-publish-map-1')
        self.assertEqual(value['service']['serviceName'], 'map-1')

    def test_rejects_model_summary_source_swap_missing_result_and_premature_publish(self):
        cases = []
        changed = copy.deepcopy(self.exchanges)
        changed[3][2]['studioSourceId'] = 'other-source'
        cases.append(changed)
        changed = copy.deepcopy(self.exchanges)
        changed[4][1]['packageKey'] = 'svc-publish-map-1'
        cases.append(changed)
        changed = copy.deepcopy(self.exchanges)
        changed[13][2]['envelope']['body']['layers'][0]['sourceId'] = 'other-source'
        cases.append(changed)
        changed = copy.deepcopy(self.exchanges)
        changed.append(('honua_studio_propose_publication', {}, {'status': 'AwaitingApproval'}))
        cases.append(changed)
        for changed in cases:
            with self.subTest(case=cases.index(changed)), self.assertRaises(runner.Blocked):
                self.observe(changed)
        events = transcript(self.exchanges)
        del events[7]['tool_use_result']
        with self.assertRaises(runner.Blocked):
            runner.observed_agent_map(wire(events), self.original, self.service,
                                      'map-turn', 'map-package', 'layer-1', '/maps/map-1')


class AgentMapRouteTests(unittest.TestCase):
    def test_supported_viewer_route_alphabet_and_canonical_segments(self):
        for route in ('/maps/Map_1', '/maps/a.b', '/maps/A_B/child-2', '/maps/a/ordinary..dots'):
            with self.subTest(route=route):
                self.assertTrue(runner.canonical_map_route(route))

    def test_bad_route_is_refused_before_stage_state_mutation(self):
        for route in ('/maps/a//b', '/maps/a/./b', '/maps/a/../b', '/maps/a/',
                      '/maps//a', '/maps/a%2Fb', '/maps/a\\b'):
            with self.subTest(route=route):
                state = {'stages': {'agentService': 'pass'},
                         'agentImportTask': {'workspaceId': 'workspace-1', 'turnId': 'import-turn'},
                         'agentService': {'turnId': 'service-turn'}}
                args = SimpleNamespace(workspace_id='workspace-1', turn_id='map-turn', route=route)
                with self.assertRaises(runner.Blocked):
                    runner.phase_agent_map(args, state, {}, Path('/nonexistent'))
                self.assertEqual(state['stages'], {'agentService': 'pass'})

    def test_installed_source_must_reject_anonymous_read_before_map(self):
        service = {'serviceName': 'native-source', 'layerId': 42}
        endpoint = 'http://127.0.0.1:8080/rest/services/native-source/FeatureServer/42?f=json'
        for status in (401, 403):
            with self.subTest(status=status), patch.object(runner, 'urlopen',
                side_effect=HTTPError(endpoint, status, 'denied', None, None)) as read:
                runner.require_private_source('http://127.0.0.1:8080', service)
                self.assertEqual([call.args[0] for call in read.call_args_list], [endpoint,
                    endpoint.removesuffix('?f=json') + '/query?where=1%3D1&returnCountOnly=true&f=json'])
        for status in (200, 404):
            with self.subTest(status=status), patch.object(runner, 'urlopen',
                return_value=nullcontext(SimpleNamespace(status=status))):
                with self.assertRaises(runner.Blocked):
                    runner.require_private_source('http://127.0.0.1:8080', service)

    def test_private_source_checks_real_segment_encoded_service_route(self):
        service = {'serviceName': 'folder/source name', 'layerId': 42}
        endpoint = 'http://127.0.0.1:8080/rest/services/folder/source%20name/FeatureServer/42?f=json'
        with patch.object(runner, 'urlopen',
                return_value=nullcontext(SimpleNamespace(status=200))) as read:
            with self.assertRaises(runner.Blocked):
                runner.require_private_source('http://127.0.0.1:8080', service)
            self.assertEqual(read.call_args.args[0], endpoint)
        with patch.object(runner, 'urlopen',
                side_effect=HTTPError(endpoint, 401, 'denied', None, None)) as read:
            runner.require_private_source('http://127.0.0.1:8080', service)
            self.assertEqual([call.args[0] for call in read.call_args_list], [endpoint,
                endpoint.removesuffix('?f=json') + '/query?where=1%3D1&returnCountOnly=true&f=json'])

    def test_private_metadata_does_not_prove_private_feature_query(self):
        service = {'serviceName': 'native-source', 'layerId': 42}
        endpoint = 'http://127.0.0.1:8080/rest/services/native-source/FeatureServer/42'
        with patch.object(runner, 'urlopen', side_effect=[
                HTTPError(endpoint + '?f=json', 401, 'denied', None, None),
                nullcontext(SimpleNamespace(status=200))]) as read:
            with self.assertRaises(runner.Blocked):
                runner.require_private_source('http://127.0.0.1:8080', service)
            self.assertEqual(read.call_args.args[0],
                endpoint + '/query?where=1%3D1&returnCountOnly=true&f=json')


class AgentMapShareEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.original = {'workspaceId': 'workspace-1', 'targetId': 'target-1',
                         'tenantId': 'tenant-1'}
        self.service = {'serviceId': 'svc-publish-map-1', 'serviceName': 'map-1',
                        'layerId': 42, 'studioSourceId': 'source-42', 'featureCount': 3}
        self.final = {'proposalId': 'map-proposal-1', 'reopenedDraftId': 'draft-final',
                      'itemId': 'item-1', 'versionId': 'version-2'}
        task = {'kind': 'studio-composition', 'id': 'draft-final', 'revision': 1,
                'packageKey': 'map-package'}
        self.request = {'turnId': 'sharing-turn', 'prompt': runner.map_share_prompt(self.service, self.final),
                        'task': task}
        locator = {'workspaceId': 'workspace-1', 'turnId': 'sharing-turn'}
        message = {'role': 'user', 'turnId': 'sharing-turn', 'content': self.request['prompt']}
        self.exchanges = [
            ('honua_workspace_conversation', {'workspaceId': 'workspace-1'},
             {'messages': [message]}),
            ('honua_workspace_get_turn', locator, {'turnId': 'sharing-turn',
             'messages': [message], 'context': {'workspaceId': 'workspace-1',
             'targetId': 'target-1', 'tenantId': 'tenant-1', 'task': task}}),
            ('honua_agent_connect', locator, {'state': 'attached', 'turnBinding': locator}),
            ('honua_describe_layer', {'serviceId': 'map-1', 'layerId': 42,
             'includeRowCount': True}, {'serviceId': 'svc-publish-map-1',
             'serviceName': 'map-1', 'layerId': 42, 'studioSourceId': 'source-42',
             'rowCount': 3}),
            ('honua_admin_services_access_policy_set', {'serviceName': 'map-1',
             'allowAnonymous': True, 'allowAnonymousWrite': False},
             {'status': 'Completed', 'requiresApproval': False,
              'operationId': 'admin.services.access-policy.set',
              'operationInstanceId': 'policy-operation-1', 'details': {'response':
               json.dumps({'success': True, 'data': {'serviceName': 'map-1',
                'accessPolicy': {'allowAnonymous': True, 'allowAnonymousWrite': False}}})}}),
        ]

    def observe(self, exchanges=None):
        return runner.observed_agent_map_share(wire(transcript(exchanges or self.exchanges)),
                                               self.original, self.service,
                                               self.request, self.final)

    def test_completed_exact_original_source_policy(self):
        result = self.observe()
        self.assertEqual(result['status'], 'Completed')
        self.assertFalse(result['requiresApproval'])
        self.assertEqual(result['serviceId'], 'svc-publish-map-1')

    def test_pending_requires_separate_durable_proposal(self):
        changed = copy.deepcopy(self.exchanges)
        changed[4] = (changed[4][0], changed[4][1],
                      {'status': 'RequiresApproval', 'requiresApproval': True,
                       'operationId': 'admin.services.access-policy.set',
                       'operationInstanceId': 'policy-operation-1',
                       'proposalId': 'policy-proposal-1'})
        result = self.observe(changed)
        self.assertTrue(result['requiresApproval'])
        self.assertEqual(result['proposalId'], 'policy-proposal-1')
        changed[4][2].pop('proposalId')
        with self.assertRaises(runner.Blocked): self.observe(changed)

    def test_rejects_wrong_service_write_or_human_turn_and_premature_policy(self):
        for index, change in ((4, lambda x: x[1].update(serviceName='other')),
                              (4, lambda x: x[1].update(allowAnonymousWrite=True)),
                              (1, lambda x: x[2]['context'].update(targetId='other')),
                              (3, lambda x: x[2].update(serviceId='other'))):
            changed = copy.deepcopy(self.exchanges)
            change(changed[index])
            with self.subTest(index=index), self.assertRaises(runner.Blocked): self.observe(changed)
        changed = copy.deepcopy(self.exchanges)
        changed.insert(0, changed.pop(4))
        with self.assertRaises(runner.Blocked): self.observe(changed)


class AgentMapFinishEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.original = {'workspaceId': 'workspace-1', 'targetId': 'target-1', 'tenantId': 'tenant-1',
                         'ownerActorId': 'apikey:api-key:22222222-2222-4222-8222-222222222222'}
        self.initial = {'workspaceId': 'workspace-1', 'turnId': 'map-turn',
                        'packageKey': 'map-package', 'draftId': 'draft-1',
                        'reopenedDraftId': 'draft-2', 'itemId': 'item-1', 'route': '/maps/map-1'}
        expression = ['match', ['get', 'class'], 'A', '#eff3ff', '#c6dbef']
        before = {'title': 'Hawaiʻi', 'layers': [{'id': 'layer-1', 'sourceId': 'source-42'}],
                  'mapSpec': {'version': 8, 'sources': {'source-42': {'type': 'vector'}},
                              'layers': [{'id': 'layer-1', 'type': 'circle',
                                          'paint': {'circle-color': expression, 'circle-radius': 1.0}}]}}
        after = copy.deepcopy(before)
        after['mapSpec']['layers'][0]['paint']['circle-opacity'] = 0.55
        self.visual = {'workspaceId': 'workspace-1', 'turnId': 'visual-turn',
                       'draftId': 'draft-2', 'itemId': 'item-1', 'generation': 3,
                       'packageKey': 'map-package', 'layerId': 'layer-1',
                       'colorProperty': 'circle-color', 'colorExpression': expression,
                       'opacityProperty': 'circle-opacity',
                       'body': copy.deepcopy(before),
                       'prompt': 'Human visual edit followed by actual new prompt.'}
        locator = {'workspaceId': 'workspace-1', 'turnId': 'visual-turn'}
        task = {'kind': 'studio-composition', 'id': 'draft-2', 'revision': 3,
                'packageKey': 'map-package'}
        final_task = {'kind': 'studio-composition', 'id': 'draft-3', 'revision': 1,
                      'packageKey': 'map-package'}
        message = {'role': 'user', 'turnId': 'visual-turn', 'content': self.visual['prompt']}
        proposal_input = {'itemId': 'item-1', 'versionId': 'version-2',
                          'contentHash': 'c' * 64, 'route': '/maps/map-1', 'visibility': 'public'}
        self.exchanges = [
            ('honua_workspace_conversation', {'workspaceId': 'workspace-1'}, {'messages': [message]}),
            ('honua_workspace_get_turn', locator, {'turnId': 'visual-turn', 'messages': [message],
              'context': {'workspaceId': 'workspace-1', 'targetId': 'target-1',
                          'tenantId': 'tenant-1', 'viewId': 'view-2', 'task': task}}),
            ('honua_agent_connect', locator, {'state': 'attached', 'turnBinding': locator}),
            ('honua_studio_get_draft', {'draftId': 'draft-2'}, {'draftId': 'draft-2',
              'itemId': 'item-1', 'generation': 3, 'envelope': {'body': before}}),
            ('honua_studio_set_map_style', {'draftId': 'draft-2', 'generation': 3,
              'mapSpec': after['mapSpec']}, {'generation': 4}),
            ('honua_studio_get_draft', {'draftId': 'draft-2'}, {'draftId': 'draft-2',
              'generation': 4, 'envelope': {'body': after}}),
            ('honua_studio_validate_draft', {'draftId': 'draft-2'}, {'status': 'valid'}),
            ('honua_studio_save_version', {'draftId': 'draft-2', 'generation': 4},
             {'version': {'itemId': 'item-1', 'versionId': 'version-2', 'contentHash': 'c' * 64}}),
            ('honua_studio_get_version', {'itemId': 'item-1', 'versionId': 'version-2'},
             {'versionId': 'version-2', 'contentHash': 'c' * 64}),
            ('honua_studio_reopen_version', {'itemId': 'item-1', 'versionId': 'version-2'},
             {'draftId': 'draft-3', 'itemId': 'item-1'}),
            ('honua_studio_get_draft', {'draftId': 'draft-3'}, {'draftId': 'draft-3',
             'itemId': 'item-1', 'generation': 1, 'envelope': {'body': after}}),
            ('honua_workspace_get', {'workspaceId': 'workspace-1', 'viewId': 'view-2'},
             {'revision': 6, 'selectedResources': [], 'task': task}),
            ('honua_workspace_set_context', {'workspaceId': 'workspace-1', 'viewId': 'view-2',
             'expectedRevision': 6, 'targetId': 'target-1', 'selectedResources': [],
             'task': final_task}, {'task': final_task}),
            ('honua_workspace_get', {'workspaceId': 'workspace-1', 'viewId': 'view-2'},
             {'revision': 7, 'task': final_task}),
            ('honua_studio_propose_publication', proposal_input,
             {'status': 'AwaitingApproval', 'humanConfirmationRequired': True,
              'proposalId': 'proposal-1', 'operationInstanceId': 'operation-1',
              'operation': {'proposalId': 'proposal-1', 'operationInstanceId': 'operation-1'}}),
        ]

    def observe(self, exchanges=None):
        return runner.observed_agent_map_finish(wire(transcript(exchanges or self.exchanges)),
                                                self.original, self.initial, self.visual)

    def test_keeps_visual_edit_then_proposes_only_exact_final_version(self):
        # A JS JSON.parse/JSON.stringify round trip writes 1.0 as 1; Unicode is not ASCII-escaped.
        self.visual['body']['mapSpec']['layers'][0]['paint']['circle-radius'] = 1
        result = self.observe()
        self.assertEqual(result['proposalId'], 'proposal-1')
        self.assertEqual(result['versionId'], 'version-2')
        self.assertEqual(result['finalOpacity'], 0.55)
        self.assertEqual(result['body']['title'], 'Hawaiʻi')
        self.assertEqual(result['expectedRequestedBy'], '22222222-2222-4222-8222-222222222222')

    def test_rejects_structurally_altered_human_visual_body(self):
        self.visual['body']['title'] = 'Retargeted map'
        with self.assertRaises(runner.Blocked): self.observe()

    def test_rejects_style_overwrite_stale_human_turn_and_direct_publication(self):
        changed = copy.deepcopy(self.exchanges)
        changed[4][1]['mapSpec']['layers'][0]['paint']['circle-color'] = '#000000'
        with self.assertRaises(runner.Blocked): self.observe(changed)
        changed = copy.deepcopy(self.exchanges)
        changed[1][2]['context']['task']['revision'] = 2
        with self.assertRaises(runner.Blocked): self.observe(changed)
        changed = copy.deepcopy(self.exchanges)
        changed[14][2]['status'] = 'Published'
        changed[14][2]['humanConfirmationRequired'] = False
        with self.assertRaises(runner.Blocked): self.observe(changed)
        changed = copy.deepcopy(self.exchanges)
        changed[14][1]['versionId'] = 'version-1'
        with self.assertRaises(runner.Blocked): self.observe(changed)


class AgentMapApprovalIdentityTests(unittest.TestCase):
    def test_requires_actual_proposal_requester_to_match_original_stored_agent(self):
        agent_id = '22222222-2222-4222-8222-222222222222'
        original = {'ownerActorId': 'apikey:api-key:' + agent_id}
        final = {'expectedRequestedBy': agent_id}
        witness = {'requestedBy': agent_id, 'resolvedBy': 'actual-distinct-human'}
        runner.require_agent_map_approver(witness, original, final, "originating-human")
        with self.assertRaises(runner.Blocked):
            runner.require_agent_map_approver({**witness, 'requestedBy': '33333333-3333-4333-8333-333333333333'}, original, final, 'originating-human')
        with self.assertRaises(runner.Blocked):
            runner.require_agent_map_approver({**witness, 'resolvedBy': agent_id}, original, final, 'originating-human')
        with self.assertRaises(runner.Blocked):
            runner.require_agent_map_approver(witness, original, {'expectedRequestedBy': 'foreign-agent'}, 'originating-human')
        for actor in ('operatorbearer:subject:' + agent_id, 'ApiKey:api-key:' + agent_id,
                      'apikey:api-key:00000000-0000-0000-0000-000000000000', 'apikey:api-key:bad-id'):
            with self.subTest(actor=actor), self.assertRaises(runner.Blocked):
                runner.require_agent_map_approver(witness, {'ownerActorId': actor}, final, 'originating-human')

    def test_originating_human_cannot_supply_map_or_required_policy_approval(self):
        agent_id = '22222222-2222-4222-8222-222222222222'
        original = {'ownerActorId': 'apikey:api-key:' + agent_id}
        final = {'expectedRequestedBy': agent_id}
        visual = {'principalId': 'originating-human'}
        witness = {'requestedBy': agent_id, 'resolvedBy': 'separate-human',
                   'policyRequestedBy': agent_id, 'policyResolvedBy': 'separate-human'}
        required = {'requiresApproval': True}
        runner.require_agent_map_audience_approvers(witness, original, final, visual, required)
        for field in ('resolvedBy', 'policyResolvedBy'):
            for resolver in ('originating-human', agent_id, '', None):
                with self.subTest(field=field, resolver=resolver), self.assertRaises(runner.Blocked):
                    runner.require_agent_map_audience_approvers(
                        {**witness, field: resolver}, original, final, visual, required)
        for origin in (None, '', agent_id):
            with self.subTest(origin=origin), self.assertRaises(runner.Blocked):
                runner.require_agent_map_audience_approvers(
                    witness, original, final, {'principalId': origin}, required)
        with self.assertRaises(runner.Blocked):
            runner.require_agent_map_audience_approvers(
                {**witness, 'policyRequestedBy': 'foreign-agent'}, original, final, visual, required)
        runner.require_agent_map_audience_approvers(
            {**witness, 'policyRequestedBy': None, 'policyResolvedBy': None},
            original, final, visual, {'requiresApproval': False})

    def test_browser_approval_identity_requires_a_second_actual_human(self):
        script = """
import assert from 'node:assert/strict';
import {isDistinctHumanApproval} from './publication_identity.mjs';
const approved = {requestedBy: 'agent', resolvedBy: 'approver'};
assert.equal(isDistinctHumanApproval(approved, 'agent', 'author'), true);
for (const resolvedBy of ['author', 'agent', '', null, undefined, false]) {
  assert.equal(isDistinctHumanApproval({...approved, resolvedBy}, 'agent', 'author'), false);
}
for (const origin of ['', null, undefined, 'agent']) {
  assert.equal(isDistinctHumanApproval(approved, 'agent', origin), false);
}
assert.equal(isDistinctHumanApproval({...approved, requestedBy:'other'}, 'agent', 'author'), false);
"""
        result = subprocess.run(['node', '--input-type=module', '-e', script],
                                cwd=Path(__file__).parent, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class MapWorkflowDispatchTests(unittest.TestCase):
    def dispatch(self, phase, workflow, requested=None):
        state = {'conversationMode': 'external-agent', 'stages': {}}
        if workflow is not None:
            state['mapWorkflow'] = workflow
        argv = ['run.py', phase, '--manifest', '/unused/manifest', '--attempt', '/unused/attempt',
                '--console-root', '/unused/console', '--console-sha', 'a' * 40]
        if requested:
            argv.extend(['--map-workflow', requested])
        with patch.dict(runner.sys.modules, {runner.__name__: runner}), \
                patch.object(runner.sys, 'argv', argv), \
                patch.object(runner, 'candidate', return_value=({}, {})), \
                patch.object(runner, 'check_harness', return_value={}), \
                patch.object(runner, 'read_json', return_value=state), \
                patch.object(runner, 'check_state'), \
                patch.object(runner, 'phase_agent_map') as create, \
                patch.object(runner.agent_map, 'phase_map') as existing, \
                patch.object(runner, 'phase_agent_map_share') as sharing, \
                patch.object(runner.agent_map, 'phase_publication') as publication:
            result = runner.main()
            return result, create.call_count, existing.call_count, sharing.call_count, publication.call_count

    def test_initial_map_dispatch_keeps_both_original_contracts(self):
        self.assertEqual(self.dispatch('agent-map', 'human-draft'), (0, 0, 1, 0, 0))
        self.assertEqual(self.dispatch('agent-map', 'external-visual'), (0, 1, 0, 0, 0))

    def test_cross_workflow_approval_and_sharing_refuse_before_dispatch(self):
        self.assertEqual(self.dispatch('agent-publication', 'external-visual'), (2, 0, 0, 0, 0))
        self.assertEqual(self.dispatch('agent-map-share', 'human-draft'), (2, 0, 0, 0, 0))
        self.assertEqual(self.dispatch('agent-publication', 'human-draft'), (0, 0, 0, 0, 1))
        self.assertEqual(self.dispatch('agent-map-share', 'external-visual'), (0, 0, 0, 1, 0))

    def test_missing_unknown_or_changed_selection_never_starts_a_model(self):
        for workflow in (None, '', 'unknown'):
            with self.subTest(workflow=workflow):
                self.assertEqual(self.dispatch('agent-map', workflow), (2, 0, 0, 0, 0))
        self.assertEqual(self.dispatch('agent-map', 'human-draft', 'external-visual'), (2, 0, 0, 0, 0))
        self.assertEqual(self.dispatch('agent-map', 'external-visual', 'human-draft'), (2, 0, 0, 0, 0))


helper = runner.agent_map


class AgentMapTests(unittest.TestCase):
    def test_browser_proposal_is_bound_to_the_authenticated_human(self):
        script = """
import assert from 'node:assert/strict';
import {isPendingHumanProposal} from './publication_identity.mjs';
const proposal = {proposalId:'proposal1', status:'AwaitingApproval',
                  requestedBy:'human1', requestedByAgent:false};
assert.equal(isPendingHumanProposal(proposal, 'proposal1', 'human1'), true);
assert.equal(isPendingHumanProposal(proposal, 'proposal1', 'human2'), false);
for (const requestedByAgent of [undefined, null, '', 0, true, 'false']) {
  assert.equal(isPendingHumanProposal({...proposal, requestedByAgent}, 'proposal1', 'human1'), false);
}
for (const patch of [{requestedBy:'human2'}, {proposalId:'old'},
                     {status:'Succeeded'}, {resolvedBy:'human2'}]) {
  assert.equal(isPendingHumanProposal({...proposal, ...patch}, 'proposal1', 'human1'), false);
}
assert.equal(isPendingHumanProposal({...proposal, requestedBy:''}, 'proposal1', ''), false);
"""
        result = subprocess.run(['node', '--input-type=module', '-e', script],
                                cwd=Path(__file__).parent, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def setUp(self):
        self.original = {'workspaceId': 'w1', 'turnId': 'import-turn', 'targetId': 'target1', 'tenantId': 'tenant1'}
        self.service = {'turnId': 'service-turn', 'studioSourceId': 'source1'}
        self.task = {'id': 'draft1', 'kind': 'studio-composition', 'revision': 1, 'packageKey': 'map1'}
        self.context = {**{k: self.original[k] for k in ('workspaceId', 'targetId', 'tenantId')},
                        'viewId': 'view1', 'revision': 9, 'task': self.task}
        self.messages = [{'role': 'user', 'turnId': 'map-turn', 'content': helper.MAP_PROMPT}]
        self.before = {'draftId': 'draft1', 'itemId': 'item1', 'packageKey': 'map1', 'workspaceId': 'w1',
                       'family': 'map', 'generation': 1, 'envelope': {'body': {'layers': []}}}
        added = copy.deepcopy(self.before)
        added['generation'] = 2
        self.layer = {'id': 'presentation1', 'sourceId': 'source1', 'type': 'line'}
        added['envelope']['body']['layers'] = [self.layer]
        self.style = {'presentation1': {'paint': {'line-color': '#2563eb'}}}
        styled = copy.deepcopy(added)
        styled['generation'] = 3
        styled['envelope']['body']['styleRefs'] = [{'styleId': 'honua-inline:presentation1', 'body': self.style}]
        self.version = {'itemId': 'item1', 'versionId': 'version1', 'contentHash': 'a' * 64,
                        'envelope': copy.deepcopy(styled['envelope'])}
        self.reopened = {**copy.deepcopy(styled), 'draftId': 'draft2', 'generation': 1, 'baseVersionId': 'version1',
                         'operation': self.operation('reopen1')}
        locator = {'workspaceId': 'w1', 'turnId': 'map-turn'}
        link = {'workspaceId': 'w1', 'viewId': 'view1', 'expectedRevision': 9, 'targetId': 'target1', 'selectedResources': [],
                'task': {**self.task, 'id': 'draft2', 'revision': 1}}
        current = {**self.context, 'revision': 10, 'task': link['task']}
        self.exchanges = [
            ('honua_workspace_conversation', {'workspaceId': 'w1'}, {'messages': self.messages}),
            ('honua_workspace_get_turn', locator, {'turnId': 'map-turn', 'messages': self.messages, 'context': self.context}),
            ('honua_agent_connect', locator, {'state': 'attached', 'turnBinding': locator}),
            ('honua_studio_get_draft', {'draftId': 'draft1'}, self.before),
            ('honua_studio_add_layer', {'draftId': 'draft1', 'generation': 1, 'layer': self.layer}, added),
            ('honua_studio_set_layer_style', {'draftId': 'draft1', 'generation': 2, 'layerId': 'presentation1', 'styleBody': self.style}, styled),
            ('honua_studio_save_version', {'draftId': 'draft1', 'generation': 3}, {'operation': self.operation('save1'), 'version': self.version}),
            ('honua_studio_get_version', {'itemId': 'item1', 'versionId': 'version1'}, {'versionId': 'version1', 'contentHash': 'a' * 64}),
            ('honua_studio_reopen_version', {'itemId': 'item1', 'versionId': 'version1'}, self.reopened),
            ('honua_studio_get_draft', {'draftId': 'draft2'}, self.reopened),
            ('honua_workspace_set_context', link, current),
            ('honua_workspace_get', {'workspaceId': 'w1', 'viewId': 'view1'}, current),
            ('honua_workspace_append_message', {'workspaceId':'w1','turnId':'map-turn','messageId':'summary1','content':helper.MAP_SUMMARY},
             {'id':'summary1','turnId':'map-turn','role':'assistant','content':helper.MAP_SUMMARY,'evidenceKind':'conversation'}),
        ]

    @staticmethod
    def operation(identifier):
        return {'operationInstanceId': identifier, 'status': 'Completed', 'requiresApproval': False}

    @staticmethod
    def encode(exchanges, structured=True):
        events = []
        for index, (tool, arguments, value) in enumerate(exchanges):
            call = 'call' + str(index)
            events.append({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': call,
                            'name': 'mcp__honua__' + tool, 'input': arguments}]}})
            event = {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': call,
                     'content': json.dumps(value)}]}}
            if structured:
                event['tool_use_result'] = {'structuredContent': value}
            events.append(event)
        return '\n'.join(json.dumps(e) for e in events)

    def observe(self, exchanges=None):
        return helper.observed_map(runner, self.encode(self.exchanges if exchanges is None else exchanges),
                                   self.service, self.original, 'map-turn')

    def test_joins_existing_human_map_generation_source_save_reopen_and_workspace_pointer(self):
        receipt = self.observe()
        self.assertEqual(receipt['reopenedDraftId'], 'draft2')
        self.assertEqual(receipt['operationInstanceIds'], ['save1', 'reopen1'])
        self.assertEqual(receipt['bodyCanonicalSha256'], helper.body_hash(self.version['envelope']['body']))
        self.assertFalse(receipt['renderedMapVerified'])
        self.assertEqual(receipt['publication'], 'awaiting-real-human-proposer')

    def test_model_prose_and_unrelated_or_repeated_mutations_cannot_pass(self):
        with self.assertRaises(runner.Blocked):
            helper.observed_map(runner, self.encode(self.exchanges, structured=False), self.service, self.original, 'map-turn')
        for tool in ('honua_studio_propose_publication', 'honua_admin_proposals_approve', 'honua_studio_add_layer'):
            broken = copy.deepcopy(self.exchanges) + [(tool, {}, {})]
            with self.subTest(tool=tool), self.assertRaises(runner.Blocked):
                self.observe(broken)

    def test_rejects_context_generation_source_style_and_saved_version_drift(self):
        for index, section, key, value in ((1, 'output', 'turnId', 'another-turn'),
                                         (4, 'input', 'generation', 2), (5, 'input', 'layerId', 'other'),
                                         (7, 'output', 'contentHash', 'b' * 64),
                                         (8, 'input', 'versionId', 'other-version'),
                                         (9, 'output', 'draftId', 'replacement')):
            broken = copy.deepcopy(self.exchanges)
            target = broken[index][1 if section == 'input' else 2]
            target[key] = value
            with self.subTest(index=index, key=key), self.assertRaises(runner.Blocked):
                self.observe(broken)
        broken = copy.deepcopy(self.exchanges)
        broken[4][1]['layer']['sourceId'] = 'other-source'
        with self.assertRaises(runner.Blocked):
            self.observe(broken)
        broken = copy.deepcopy(self.exchanges)
        broken[6][2]['operation']['status'] = 'AwaitingApproval'
        with self.assertRaises(runner.Blocked):
            self.observe(broken)

    def test_independent_http_reads_must_match_all_draft_version_content_identities(self):
        receipt = self.observe()
        original_draft = {**self.before, 'envelope': copy.deepcopy(self.version['envelope'])}
        documents = [self.version, original_draft, self.reopened]
        from unittest.mock import Mock
        read = Mock(side_effect=copy.deepcopy(documents))
        result = helper.independent_map_readback(runner, {}, receipt, read=read)
        self.assertEqual(result['status'], 'pass')
        self.assertFalse(result['renderedMapVerified'])
        for index in range(3):
            broken = copy.deepcopy(documents)
            broken[index]['envelope']['body']['layers'][0]['sourceId'] = 'other-source'
            with self.subTest(index=index), self.assertRaises(runner.Blocked):
                helper.independent_map_readback(runner, {}, receipt, read=Mock(side_effect=broken))

    def test_audience_resumption_is_same_original_turn_and_read_only(self):
        receipt = self.observe()
        locator = {'workspaceId': 'w1', 'turnId': 'map-turn'}
        exchanges = [self.exchanges[0], self.exchanges[1], self.exchanges[2],
                     self.exchanges[9], self.exchanges[7]]
        result = helper.observed_audience_resume(runner, self.encode(exchanges), self.original, receipt)
        self.assertEqual(result['anonymousAudienceObservedBy'], 'independent-controller-browser')
        with self.assertRaises(runner.Blocked):
            helper.observed_audience_resume(runner, self.encode(exchanges + [self.exchanges[8]]), self.original, receipt)


    def test_preserves_unrelated_before_draft_content_and_rejects_independent_identity_drift(self):
        broken = copy.deepcopy(self.exchanges)
        broken[3][2]['envelope']['body']['widgets'] = [{'id': 'human-chart', 'kind': 'bar'}]
        with self.assertRaisesRegex(runner.Blocked, 'unrelated'):
            self.observe(broken)
        receipt = self.observe()
        from unittest.mock import Mock
        original_draft = {**self.before, 'envelope': copy.deepcopy(self.version['envelope'])}
        for index, key in ((0, 'itemId'), (0, 'versionId'), (0, 'contentHash'), (1, 'draftId'), (2, 'workspaceId')):
            documents = copy.deepcopy([self.version, original_draft, self.reopened])
            documents[index][key] = 'wrong'
            with self.subTest(index=index, key=key), self.assertRaises(runner.Blocked):
                helper.independent_map_readback(runner, {}, receipt, read=Mock(side_effect=documents))

    def test_map_response_loss_blocks_mutation_replay(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import subprocess
        import tempfile
        args = SimpleNamespace(workspace_id='w1', turn_id='map-turn', claude='real-client')
        state = {'stages': {'agentService': 'pass'}, 'agentImportTask': self.original,
                 'agentService': self.service, 'externalClientSessionId': 'same-session'}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(helper, 'validate_session', return_value=Path(directory) / 'mcp.json'), \
                patch.object(runner, 'private_json'), \
                patch.object(helper.subprocess, 'run', side_effect=subprocess.TimeoutExpired('real-client', 900)) as external:
            with self.assertRaises(subprocess.TimeoutExpired):
                helper.phase_map(runner, args, state, {}, Path(directory))
            self.assertEqual(state['stages']['agentMap'], 'blocked')
            with self.assertRaises(runner.Blocked):
                helper.phase_map(runner, args, state, {}, Path(directory))
            self.assertEqual(external.call_count, 1)

    def pending_handoff(self):
        observed = self.observe()
        return {**observed, 'status':'awaiting-separate-human-approval','route':'/review-map',
                'proposalId':'proposal1','requestedBy':'human1','operationInstanceId':'publish1',
                'commandId':'command1','requestId':'request1','renderedMapVerified':True,
                'canonicalProposal':{'proposalId':'proposal1','status':'AwaitingApproval','requestedBy':'human1',
                    'diff':['itemId=item1','versionId=version1','contentHash='+'a'*64,'route=/review-map','visibility=public']}}

    def pending_operation(self):
        return {'operationInstanceId':'publish1','operationId':'studio.content.create-publication-request',
                'proposalId':'proposal1','status':'RequiresApproval','resourceIds':{}}

    def test_pending_capture_joins_canonical_human_diff_and_operation_without_claiming_request_actuation(self):
        handoff = self.pending_handoff()
        result = helper.observed_pending_publication(runner, {}, handoff, self.observe(), 'proposal1', '/review-map',
                                                     read=lambda _:self.pending_operation())
        self.assertFalse(result['requestIdentityVerified'])
        canonical = {**self.pending_operation(), 'resourceIds': {
            'itemId': 'item1', 'versionId': 'version1', 'requestId': 'request1'}}
        joined = helper.observed_pending_publication(runner, {}, handoff, self.observe(), 'proposal1', '/review-map',
                                                     read=lambda _: canonical)
        self.assertEqual(joined['requestId'], 'request1')
        self.assertTrue(joined['requestIdentityVerified'])
        self.assertEqual(result['pendingBinding'],'canonical-proposal-diff-and-operation')
        for field,value in [('status','Succeeded'),('requestedByAgent','agent1'),('resolvedBy','human2'),
                            ('diff',['itemId=other']),('proposalId','other')]:
            broken=copy.deepcopy(handoff)
            broken['canonicalProposal'][field]=value
            with self.subTest(field=field),self.assertRaises(runner.Blocked):
                helper.observed_pending_publication(runner, {}, broken, self.observe(),'proposal1','/review-map',
                                                     read=lambda _:self.pending_operation())
        for field,value in [('status','Completed'),('proposalId','other'),('operationId','other'),
                            ('resourceIds',{'itemId':'wrong','versionId':'version1','requestId':'request1'})]:
            operation={**self.pending_operation(),field:value}
            with self.subTest(field=field),self.assertRaises(runner.Blocked):
                helper.observed_pending_publication(runner, {}, handoff, self.observe(),'proposal1','/review-map',
                                                     read=lambda _:operation)

    def test_pending_capture_accepts_absent_request_id_but_rejects_wrong_observed_id(self):
        handoff = self.pending_handoff()
        handoff.pop('requestId')
        result = helper.observed_pending_publication(runner, {}, handoff, self.observe(), 'proposal1', '/review-map',
                                                     read=lambda _: self.pending_operation())
        self.assertNotIn('requestId', result)
        self.assertFalse(result['requestIdentityVerified'])
        operation = {**self.pending_operation(), 'resourceIds': {
            'itemId': 'item1', 'versionId': 'version1', 'requestId': 'other'}}
        with self.assertRaises(runner.Blocked):
            helper.observed_pending_publication(runner, {}, self.pending_handoff(), self.observe(),
                                               'proposal1', '/review-map', read=lambda _: operation)

    def test_audience_requires_final_request_and_same_operation_with_optional_pending_id(self):
        handoff = self.pending_handoff()
        witness = {**handoff, 'status': 'pass', 'resolvedBy': 'human2'}
        helper.validate_audience_witness(runner, handoff, witness)
        pending_without_id = {key: value for key, value in handoff.items() if key != 'requestId'}
        helper.validate_audience_witness(runner, pending_without_id, witness)
        for field, value in [('requestId', None), ('requestId', ''), ('operationInstanceId', 'other'),
                             ('proposalId', 'other'), ('itemId', 'other'), ('versionId', 'other')]:
            with self.subTest(field=field, value=value), self.assertRaises(runner.Blocked):
                helper.validate_audience_witness(runner, pending_without_id, {**witness, field: value})
        with self.assertRaises(runner.Blocked):
            helper.validate_audience_witness(runner, handoff, {**witness, 'requestId': 'other'})

    def test_read_only_human_capture_can_retry_lost_response_without_publication(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            browser_state=root/'human.json'
            browser_state.write_text('{}')  # Test path only, never live human credentials.
            observed=self.observe()
            runner.private_json(root/'external-map.json',observed,new=True)
            args=SimpleNamespace(human_browser_cdp='http://127.0.0.1:9222',origin='http://127.0.0.1:8081',route='/review-map',
                                 console_root=root,proposal_id='proposal1')
            state={'stages':{'agentMap':'pass','agentPublication':'awaiting-real-human-proposer'},
                   'target':{'origins':{'console':args.origin}},'agentMap':observed}
            with patch.object(helper,'validate_session'),patch.object(helper,'independent_map_readback'), \
                    patch.object(helper.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='',stderr='lost capture')) as browser:
                for _ in range(2):
                    with self.assertRaises(runner.Blocked):
                        helper.phase_publication(runner,args,state,{},root)
                self.assertEqual(browser.call_count,2)
                self.assertEqual(state['stages']['agentPublication'],'blocked-read-only-capture')
            def capture(command,**kwargs):
                runner.private_json(Path(command[6]),self.pending_handoff(),new=True)
                return SimpleNamespace(returncode=0,stdout='',stderr='')
            with patch.object(helper,'validate_session'),patch.object(helper,'independent_map_readback'), \
                    patch.object(helper,'server_reader',return_value=lambda _:self.pending_operation()), \
                    patch.object(helper.subprocess,'run',side_effect=capture):
                helper.phase_publication(runner,args,state,{},root)
            self.assertEqual(state['stages']['agentPublication'],'awaiting-separate-human-approval')
            self.assertFalse(state['externalPublication']['requestIdentityVerified'])

    def test_missing_exact_pending_proposal_blocks_before_browser_and_helper_has_no_mutation_controls(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            browser_state=root/'human.json'
            browser_state.write_text('{}')
            args=SimpleNamespace(human_browser_cdp='http://127.0.0.1:9222',origin='http://127.0.0.1:8081',route='/review-map',proposal_id=None)
            state={'stages':{'agentMap':'pass','agentPublication':'awaiting-real-human-proposer'},
                   'target':{'origins':{'console':args.origin}},'agentMap':self.observe()}
            with patch.object(helper,'validate_session'),patch.object(helper.subprocess,'run') as browser:
                with self.assertRaises(runner.Blocked):
                    helper.phase_publication(runner,args,state,{},root)
                browser.assert_not_called()
        source=Path(__file__).with_name('external_map_publication.mjs').read_text()
        for forbidden in ('.click(','.fill(','.post(','.put(','.delete('):
            self.assertNotIn(forbidden,source)
        for forbidden in ('chromium.launch(','.newContext(','.newPage(','.goto(','.route(','.close('):
            self.assertNotIn(forbidden,source)
        self.assertIn('chromium.connectOverCDP(humanBrowserCdp)',source)
        self.assertIn('process.exit(exitStatus)',source)

    def test_human_browser_cdp_is_explicit_loopback_origin_only(self):
        self.assertTrue(helper.valid_human_browser_cdp('http://127.0.0.1:9222'))
        for value in (None,'','https://127.0.0.1:9222','http://remote:9222','http://127.0.0.1:0',
                      'http://127.0.0.1:65536','http://user@127.0.0.1:9222','http://127.0.0.1:9222/path',
                      'http://127.0.0.1:9222?token=secret','http://127.0.0.1:9222#fragment'):
            with self.subTest(value=value):
                self.assertFalse(helper.valid_human_browser_cdp(value))

    def test_external_session_mismatch_cannot_qualify_and_audience_turn_is_frozen(self):
        with self.assertRaises(runner.Blocked):
            runner.observed_client_session(json.dumps({'type':'system','subtype':'init','session_id':'other'}), 'same')
        receipt = self.observe()
        changed = copy.deepcopy([self.exchanges[0], self.exchanges[1], self.exchanges[2], self.exchanges[9], self.exchanges[7]])
        changed[1][2]['context']['targetId'] = 'other-target'
        with self.assertRaises(runner.Blocked):
            helper.observed_audience_resume(runner, self.encode(changed), self.original, receipt)


    def test_requalification_failure_cannot_retain_previous_pass(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import tempfile
        for failure in ('witness', 'resume', 'readback', 'context'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                observed, handoff = self.observe(), self.pending_handoff()
                runner.private_json(root / 'external-publication-handoff.json', handoff)
                (root / '.env').write_text('HONUA_AGENT_API_KEY=synthetic-offline-key\n')
                operator = root / 'operator.json'; operator.write_text('{}')
                witness_ref = {'path': 'previous-witness.json', 'sha256': 'a' * 64}
                runner.private_json(root / witness_ref['path'], {'offline': True})
                state = {'stages': {key: 'pass' for key in ('prepare', 'install', 'agentTask', 'agentSubmission',
                         'agentImportObservation', 'agentService', 'agentMap', 'agentPublication', 'agentAudience')},
                         'agentImportTask': self.original, 'agentMap': observed, 'externalPublication': handoff,
                         'target': {'origins': {'console': 'http://127.0.0.1:8081', 'server': 'http://127.0.0.1:8080'}},
                         'installDirectory': str(root), 'externalClientSessionId': 'same-session',
                         'status': 'same-external-agent-with-human-governance-pass',
                         'oneExternalAgentConversationQualified': True, 'qualificationScope': 'previous observed scope',
                         'externalAudienceWitness': witness_ref,
                         'agentAudienceResume': {'transcriptSha256': 'b' * 64}, 'externalAudience': {'status': 'pass'}}
                args = SimpleNamespace(operator_state=operator, origin='http://127.0.0.1:8081',
                         server_origin='http://127.0.0.1:8080', console_root=root, claude='offline-only')
                def execute(command, **kwargs):
                    if 'env' in kwargs:
                        if failure == 'witness': return SimpleNamespace(returncode=1, stdout='', stderr='failed witness')
                        witness = {**handoff, 'status': 'pass', 'resolvedBy': 'human2'}
                        runner.private_json(Path(kwargs['env']['HONUA_WORKSPACE_E2E_PUBLICATION_WITNESS']), witness, new=True)
                        return SimpleNamespace(returncode=0, stdout='', stderr='')
                    return SimpleNamespace(returncode=1 if failure == 'resume' else 0, stdout='', stderr='')
                with patch.object(helper, 'validate_session', side_effect=runner.Blocked('context changed') if failure == 'context' else None), \
                        patch.object(runner, 'playwright_command', return_value=['offline-witness']), \
                        patch.object(helper.subprocess, 'run', side_effect=execute), \
                        patch.object(runner, 'observed_client_session'), \
                        patch.object(helper, 'observed_audience_resume', return_value={'status': 'pass'}), \
                        patch.object(helper, 'independent_map_readback', side_effect=runner.Blocked('readback changed') if failure == 'readback' else None):
                    with self.assertRaises(runner.Blocked):
                        helper.phase_audience(runner, args, state, {}, root)
                retained = runner.read_json(root / 'receipt.json')
                self.assertFalse(retained['oneExternalAgentConversationQualified'])
                self.assertEqual(retained['status'], 'blocked-fresh-external-observation')
                self.assertEqual(retained['stages']['agentAudience'], 'blocked-read-only-observation')
                self.assertNotIn('qualificationScope', retained)
                self.assertEqual(retained['priorExternalQualifications'][0]['audienceWitness'], witness_ref)
                self.assertTrue((root / witness_ref['path']).exists())

    def test_failed_map_reobservation_does_not_resurrect_prior_qualification(self):
        from unittest.mock import patch
        import tempfile
        for failure in ('context', 'readback'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = {'stages': {'agentMap': 'pass', 'agentPublication': 'pass', 'agentAudience': 'pass'},
                         'agentMap': self.observe(), 'oneExternalAgentConversationQualified': True,
                         'status': 'same-external-agent-with-human-governance-pass',
                         'qualificationScope': 'prior', 'externalAudienceWitness': {'path': 'old', 'sha256': 'a'*64}}
                with patch.object(helper, 'validate_session', side_effect=runner.Blocked('context changed') if failure == 'context' else None), \
                        patch.object(helper, 'independent_map_readback', side_effect=runner.Blocked('readback failed')):
                    with self.assertRaises(runner.Blocked):
                        helper.phase_observe_map(runner, None, state, {}, root)
                self.assertFalse(runner.read_json(root / 'receipt.json')['oneExternalAgentConversationQualified'])
                self.assertEqual(state['stages']['agentMap'], 'blocked-read-only-observation')
                self.assertNotEqual(state['stages']['agentPublication'], 'pass')

    def test_successful_map_only_reobservation_still_requires_fresh_audience(self):
        from unittest.mock import patch
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {'stages': {'agentMap': 'pass', 'agentPublication': 'pass', 'agentAudience': 'pass'},
                     'agentMap': self.observe(), 'oneExternalAgentConversationQualified': True,
                     'status': 'same-external-agent-with-human-governance-pass', 'qualificationScope': 'prior'}
            with patch.object(helper, 'validate_session'), patch.object(helper, 'independent_map_readback', return_value={'status': 'pass'}):
                helper.phase_observe_map(runner, None, state, {}, root)
            self.assertEqual(state['stages']['agentMap'], 'pass')
            self.assertFalse(state['oneExternalAgentConversationQualified'])
            self.assertEqual(state['stages']['agentAudience'], 'awaiting-fresh-observation')
            self.assertEqual(len(state['priorExternalQualifications']), 1)


if __name__ == '__main__':
    unittest.main()
