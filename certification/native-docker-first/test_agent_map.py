"""Offline negative evidence joins for same-client map stages; never live qualification."""
import copy
import hashlib
import importlib.util
import json
import unittest
from pathlib import Path

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
            ('honua_admin_services_access_policy_set', {'serviceName': 'map-1',
             'allowAnonymous': True, 'allowAnonymousWrite': False}, {'status': 'Completed',
             'requiresApproval': False, 'operationInstanceId': 'policy-1',
             'details': {'response': json.dumps({'success': True})}}),
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
        changed[4][1]['serviceName'] = 'svc-publish-map-1'
        cases.append(changed)
        changed = copy.deepcopy(self.exchanges)
        changed[14][2]['envelope']['body']['layers'][0]['sourceId'] = 'other-source'
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


class AgentMapFinishEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.original = {'workspaceId': 'workspace-1', 'targetId': 'target-1', 'tenantId': 'tenant-1'}
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


if __name__ == '__main__':
    unittest.main()
