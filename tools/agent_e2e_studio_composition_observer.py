"""Read back actual model-authored Studio drafts and immutable saves.

No method creates, patches, saves or replays a mutation. Model replies select
canonical native IDs only; native GETs must corroborate every projected byte.
The same observer handles maps and dashboards, including intermediate wrong
results, generation conflicts, edits, saved versions and explicit reopens.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import math
import re
from urllib.parse import urlencode

from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_scenario_operations import data, token
from agent_e2e_studio_observer import guid, instant

CREATE = 'honua_studio_create_draft'
SAVE = 'honua_studio_save_version'
REOPEN = 'honua_studio_reopen_version'
MUTATIONS = frozenset(('honua_studio_update_draft', 'honua_studio_add_layer', 'honua_studio_remove_layer',
    'honua_studio_set_layer_style', 'honua_studio_set_layer_visibility', 'honua_studio_set_view',
    'honua_studio_add_widget', 'honua_studio_remove_widget', 'honua_studio_bind_interaction',
    'honua_studio_remove_interaction', 'honua_studio_add_control', 'honua_studio_remove_control'))


def require(condition, reason):
    if not condition:
        raise PackError(reason)


def output(worker_result):
    result = worker_result.get('agentResult', {}).get('content', {})
    require(isinstance(result, dict) and result.get('isError', False) is False and
            isinstance(result.get('structuredContent'), dict), 'model Studio action has no successful native structured output')
    return result['structuredContent']


class StudioCompositionObserver:
    def __init__(self, *, http, binding, retain):
        require(isinstance(binding, dict) and set(binding) == {'itemId', 'namespace', 'ownerPrincipalId', 'family', 'notBefore'},
                'incomplete exact Studio authoring scope')
        guid(binding['itemId'])
        require(binding['family'] in ('map', 'dashboard') and
                isinstance(binding['namespace'], str) and re.fullmatch(r'ae_[a-z0-9_]{3,35}', binding['namespace']) and
                isinstance(binding['ownerPrincipalId'], str) and binding['ownerPrincipalId'], 'invalid Studio authoring scope')
        self.http, self.binding, self.retain = http, copy.deepcopy(binding), retain
        self.not_before = instant(binding['notBefore'])

    def eligible(self, action):
        return action.get('name') == 'mcp_call_tool' and action.get('input', {}).get('name') in (CREATE, SAVE, REOPEN, *MUTATIONS)

    def owned(self, resource):
        require(resource.get('itemId') == self.binding['itemId'] and
                resource.get('workspaceId') == resource.get('packageKey') == self.binding['namespace'] and
                resource.get('ownerId') == self.binding['ownerPrincipalId'] and
                isinstance(resource.get('envelope'), dict) and resource['envelope'].get('family') == self.binding['family'] and
                resource['envelope'].get('schemaVersion') == '1.0',
                'native Studio resource is outside exact item/family/workspace/owner scope')
        return resource

    def draft(self, draft_id, *, step):
        answer = self.http.json(step, 'GET', '/api/v1/studio/package-drafts/'+guid(draft_id))
        value = self.owned(data(answer))
        require(value.get('draftId') == draft_id and type(value.get('generation')) is int and value['generation'] >= 1,
                'native Studio draft identity/generation is absent or changed')
        return value, answer['reference']

    def version(self, version_id, *, step):
        answer = self.http.json(step, 'GET', '/api/v1/studio/content-items/'+self.binding['itemId']+'/versions/'+guid(version_id))
        value = self.owned(data(answer))
        require(value.get('versionId') == version_id and type(value.get('versionNumber')) is int and value['versionNumber'] >= 1 and
                isinstance(value.get('contentHash'), str) and re.fullmatch(r'[0-9a-f]{64}', value['contentHash']),
                'native saved version identity/content hash is absent or changed')
        return value, answer['reference']

    def current(self, *, step):
        answer = self.http.json(step, 'GET', '/api/v1/studio/content-items?'+urlencode({
            'family': self.binding['family'], 'workspaceId': self.binding['namespace'],
            'owner': self.binding['ownerPrincipalId'], 'limit': 100}))
        result = data(answer)
        require(result.get('total') == 1 and isinstance(result.get('items'), list) and len(result['items']) == 1 and
                result.get('nextCursor') is None, 'Studio authoring scope contains missing/duplicate/unobserved items')
        item = result['items'][0]
        require(item.get('itemId') == self.binding['itemId'] and item.get('ownerId') == self.binding['ownerPrincipalId'] and
                item.get('workspaceId') == item.get('packageKey') == self.binding['namespace'],
                'Studio authoring current item differs from exact native scope')
        return item, answer['reference']

    def completed_operation(self, value, expected, *, step):
        operation = value.get('operation')
        require(isinstance(operation, dict), 'Studio lifecycle reply lacks its native execution handle')
        operation_id = token(operation.get('operationInstanceId'), 'Studio lifecycle execution')
        answer = self.http.json(step, 'GET', '/api/v1/operations/handles/'+operation_id)
        observed = data(answer)
        require(observed.get('operationInstanceId') == operation_id and observed.get('operationId') == expected and
                observed.get('status') == 'Completed' and operation.get('status') == 'Completed' and
                self.not_before <= instant(observed.get('createdAt')) <= datetime.now(timezone.utc),
                'Studio native execution did not complete the actual selected lifecycle action')
        return observed, answer['reference']

    def selected(self, action, dispatch, worker_result):
        require(self.eligible(action), 'Studio authoring observation requires an actual model-selected supported tool')
        tool, arguments = action['input']['name'], action['input'].get('arguments', {})
        value = output(worker_result)
        prefix = 'authoring-'+dispatch['actionId'][:32]
        references, observed, operation = {}, None, None
        if tool == CREATE:
            require(arguments.get('itemId') == self.binding['itemId'] and not arguments.get('ownerId') and
                    arguments.get('packageKey') == arguments.get('workspaceId') == self.binding['namespace'] and
                    arguments.get('family') == self.binding['family'],
                    'model create selected another item/workspace/family or assigned an owner')
            operation, references['handle'] = self.completed_operation(value, 'studio.draft.create', step=prefix+'-handle')
            observed, references['draft'] = self.draft(value.get('draftId'), step=prefix+'-draft')
            require(observed.get('baseVersionId') is None and observed['generation'] == 1 and
                    observed.get('createdBy') == self.binding['ownerPrincipalId'] and
                    self.not_before <= instant(observed.get('createdAt')) and
                    operation.get('resourceIds', {}).get('itemId') == self.binding['itemId'] and
                    operation['resourceIds'].get('draftId') == observed['draftId'],
                    'native create does not corroborate the first model-authored owned draft')
            require(all(value.get(key) == observed.get(key) for key in observed), 'worker create output differs from native draft readback')
        elif tool == SAVE:
            operation, references['handle'] = self.completed_operation(value, 'studio.draft.save-version', step=prefix+'-handle')
            candidate = value.get('version')
            require(isinstance(candidate, dict), 'model save did not return an actual immutable version')
            observed, references['version'] = self.version(candidate.get('versionId'), step=prefix+'-version')
            require(observed == candidate and observed.get('sourceDraftId') == guid(arguments.get('draftId')) and
                    operation.get('resourceIds', {}).get('itemId') == self.binding['itemId'] and
                    operation['resourceIds'].get('versionId') == observed['versionId'] and
                    operation['resourceIds'].get('contentHash') == observed['contentHash'],
                    'model save output differs from independently persisted immutable version')
            current, references['current'] = self.current(step=prefix+'-current')
            require(current.get('currentVersionId') == observed['versionId'] and current.get('publishedVersionId') is None,
                    'model save current pointer differs or authoring published unexpectedly')
        elif tool == REOPEN:
            require(arguments.get('itemId') == self.binding['itemId'], 'model reopen selected another Studio item')
            operation, references['handle'] = self.completed_operation(value, 'studio.content.reopen-version', step=prefix+'-handle')
            observed, references['draft'] = self.draft(value.get('draftId'), step=prefix+'-draft')
            version, references['version'] = self.version(arguments.get('versionId'), step=prefix+'-version')
            require(observed.get('baseVersionId') == version['versionId'] and observed['envelope'] == version['envelope'] and
                    operation.get('resourceIds', {}).get('draftId') == observed['draftId'],
                    'model reopen differs from the exact independently saved version')
            require(all(value.get(key) == observed.get(key) for key in observed), 'worker reopen output differs from native draft readback')
        else:
            observed, references['draft'] = self.draft(arguments.get('draftId'), step=prefix+'-draft')
            expected_generation = arguments.get('generation')
            require(type(expected_generation) is int and observed['generation'] == expected_generation+1 and
                    observed.get('updatedBy') == self.binding['ownerPrincipalId'],
                    'model edit lacks its native actor/generation transition')
            require(all(value.get(key) == item for key, item in observed.items()), 'worker edit output differs from native persisted draft')
        record = {'tool': tool, 'actionId': dispatch['actionId'], 'requestDigest': dispatch['requestDigest'],
            'workerResultDigest': canonical_digest(worker_result['agentResult']), 'bindingDigest': canonical_digest(self.binding),
            'resource': copy.deepcopy(observed), 'nativeReferences': references}
        self.retain('authoring-selected-'+dispatch['actionId'], record)
        return record

    def map_composition(self, resource):
        self.owned(resource)
        body = resource['envelope'].get('body')
        require(isinstance(body, dict) and isinstance(body.get('layers'), list), 'native map has no observable composition layers')
        layers, view = body['layers'], body.get('view')
        require(layers and len(layers) <= 100 and all(isinstance(row, dict) and isinstance(row.get('id'), str) and
            row['id'] and type(row.get('visible')) is bool for row in layers), 'native map layers lack exact IDs/visibility')
        identifiers = [row['id'] for row in layers]
        require(len(set(identifiers)) == len(identifiers) and isinstance(view, dict) and
                isinstance(view.get('bbox'), list) and len(view['bbox']) == 4 and
                all(type(value) in (int, float) and math.isfinite(value) for value in view['bbox']),
                'native map has duplicate layer identities or no exact finite extent')
        return {'mapId': self.binding['itemId'], 'orderedLayerIds': identifiers,
                'visibility': {row['id']: row['visible'] for row in layers}, 'extent': copy.deepcopy(view['bbox'])}

    def saved(self, resource):
        self.owned(resource)
        return {'resourceId': self.binding['itemId'], 'versionId': guid(resource.get('versionId')),
                'documentSha256': canonical_digest(resource['envelope']['body'])[7:]}
