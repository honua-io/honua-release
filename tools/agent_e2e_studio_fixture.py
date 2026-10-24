"""Real saved-map initial fixture via the native Studio draft/version lifecycle.

For a case whose starting state is already a saved map, this trusted bootstrap
adapter may create and save the supplied reviewed envelope. It never performs a
scenario's requested publication, approves a proposal, retries a mutation, or
claims cleanup. Callers must retain original baseline/owned-resource SQL custody
and paid bootstrap admission before using their guarded HTTP transports here.
"""
from __future__ import annotations

import copy
import json
import re
from urllib.parse import urlencode

from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_scenario_operations import data
from agent_e2e_studio_observer import guid


def require(condition, reason):
    if not condition:
        raise PackError(reason)


def exact_envelope(actual, source):
    require(isinstance(actual, dict) and all(actual.get(field) == value for field, value in source.items()),
            'native Studio fixture changed the reviewed saved-map envelope')


def validation(value):
    require(isinstance(value, dict) and value.get('status') == 'valid' and
            isinstance(value.get('diagnostics'), list) and not value['diagnostics'],
            'native Studio fixture validation is not unambiguously valid')


class StudioSetup:
    def __init__(self, *, setup, requester):
        require(setup.client.kind != 'anonymous' and requester.client.kind != 'anonymous' and
                setup.client.endpoint == requester.client.endpoint and setup.client.token != requester.client.token,
                'Studio setup and task readback require distinct native identities on one origin')
        self.setup, self.requester = setup, requester

    def prepare_saved_map(self, *, namespace, owner_id, envelope, item_id, baseline):
        item_id = guid(item_id)
        require(isinstance(baseline, dict) and baseline.get('candidateKey') == self.setup.identity['candidateKey'] and
                isinstance(baseline.get('database'), dict) and baseline['database'] and
                isinstance(baseline.get('observerPrincipal'), str) and baseline['observerPrincipal'] and
                isinstance(baseline.get('snapshot'), dict) and
                {'studio', 'protectedRegistry', 'tableOids', 'deleteTriggers', 'product'} <= set(baseline['snapshot']) and
                item_id not in json.dumps(baseline['snapshot'], sort_keys=True).lower(),
                'allocated Studio item requires its independently observed exact SQL absence baseline')
        require(isinstance(namespace, str) and re.fullmatch(r'ae_[a-z0-9_]{3,35}', namespace),
                'saved-map fixture requires an exact fresh owned namespace')
        require(isinstance(owner_id, str) and 0 < len(owner_id) <= 512 and
                all(33 <= ord(char) <= 126 for char in owner_id), 'saved-map fixture requires an observed native task principal')
        require(isinstance(envelope, dict) and envelope.get('family') == 'map' and envelope.get('schemaVersion') == '1.0' and
                envelope.get('format') == 'honua_map_package.v1' and isinstance(envelope.get('body'), dict) and envelope['body'] and
                isinstance(envelope.get('bindings'), list) and isinstance(envelope.get('dependencies'), list) and
                isinstance(envelope.get('provenance'), list) and not envelope.get('publicationIntent') and
                'validation' not in envelope, 'saved-map fixture needs a reviewed unpublished native map envelope')
        source = copy.deepcopy(envelope)
        path = '/api/v1/studio/content-items?'+urlencode({'family': 'map', 'workspaceId': namespace,
                                                       'owner': owner_id, 'limit': 100})
        before_answer = self.setup.json('studio-fixture-before', 'GET', path)
        before = data(before_answer)
        require(before.get('items') == [] and type(before.get('total')) is int and before['total'] == 0 and
                before.get('nextCursor') is None, 'Studio fixture workspace already contains content; no overwrite or inferred reuse')
        create_answer = self.setup.json('studio-fixture-create', 'POST', '/api/v1/studio/package-drafts',
            {'itemId': item_id, 'packageKey': namespace, 'workspaceId': namespace, 'ownerId': owner_id, 'envelope': source})
        draft = data(create_answer, (201,))
        draft_id = guid(draft.get('draftId'))
        require(draft.get('itemId') == item_id and draft.get('packageKey') == namespace and
                draft.get('workspaceId') == namespace and draft.get('ownerId') == owner_id and
                draft.get('family') == 'map' and type(draft.get('generation')) is int and draft['generation'] == 1 and
                draft.get('baseVersionId') is None, 'native Studio draft is not the exact new owned initial fixture')
        exact_envelope(draft.get('envelope'), source)
        validation(draft.get('validation'))
        validated_answer = self.setup.json('studio-fixture-validate', 'POST',
            '/api/v1/studio/package-drafts/'+draft_id+'/validate', {})
        validation(data(validated_answer))
        saved_answer = self.setup.json('studio-fixture-save', 'POST',
            '/api/v1/studio/package-drafts/'+draft_id+'/content-versions', {'changeNote': 'Initial isolated campaign fixture'})
        version = data(saved_answer, (201,))
        version_id = guid(version.get('versionId'))
        require(version.get('itemId') == item_id and version.get('sourceDraftId') == draft_id and
                version.get('ownerId') == owner_id and version.get('workspaceId') == namespace and version.get('packageKey') == namespace and
                type(version.get('versionNumber')) is int and version['versionNumber'] == 1 and
                isinstance(version.get('contentHash'), str) and re.fullmatch(r'[0-9a-f]{64}', version['contentHash']),
                'native saved Studio version differs from exact owned draft')
        exact_envelope(version.get('envelope'), source)
        validation(version.get('validation'))
        requester_answer = self.requester.json('studio-fixture-task-read', 'GET',
            '/api/v1/studio/content-items/'+item_id+'/versions/'+version_id)
        independently_read = data(requester_answer)
        require(independently_read == version, 'actual task principal cannot independently reopen exact saved fixture bytes')
        after_answer = self.requester.json('studio-fixture-after', 'GET', path)
        after = data(after_answer)
        require(type(after.get('total')) is int and after['total'] == 1 and after.get('nextCursor') is None and
                isinstance(after.get('items'), list) and len(after['items']) == 1,
                'Studio fixture creation left missing, duplicate or unobserved content')
        item = after['items'][0]
        require(item.get('itemId') == item_id and item.get('currentVersionId') == version_id and
                item.get('publishedVersionId') is None and item.get('packageKey') == namespace and
                item.get('workspaceId') == namespace and item.get('ownerId') == owner_id,
                'native fixture current pointer/owner differs or publication already occurred')
        return {'schema': 'honua.agent-e2e.studio-fixture/v1', 'namespace': namespace,
            'baselineDigest': canonical_digest(baseline),
            'ownerPrincipalId': owner_id, 'reviewedEnvelopeDigest': canonical_digest(source),
            'draftId': draft_id, 'itemId': item_id, 'versionId': version_id, 'contentHash': version['contentHash'],
            'initialVersion': copy.deepcopy(version), 'initialItem': copy.deepcopy(item),
            'nativeReferences': {'before': before_answer['reference'], 'create': create_answer['reference'],
                'validate': validated_answer['reference'], 'save': saved_answer['reference'],
                'taskRead': requester_answer['reference'], 'after': after_answer['reference']}}
