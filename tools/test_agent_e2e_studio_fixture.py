"""Native initial-state HTTP replay only; no model/live qualification receipts."""
import copy
import json
from types import SimpleNamespace

import pytest

from agent_e2e_pack import PackError
from agent_e2e_scenario_backend import ScenarioHttp
from agent_e2e_studio_fixture import StudioSetup
from test_agent_e2e_scenario_backend import adapter, CONTEXT
from test_agent_e2e_setup_http import server

ITEM = '11111111-1111-1111-1111-111111111111'
DRAFT = '22222222-2222-2222-2222-222222222222'
VERSION = '33333333-3333-3333-3333-333333333333'


@pytest.fixture
def setup(adapter, server, monkeypatch):
    monkeypatch.setenv('STUDIO_REQUESTER_TOKEN', 'private-studio-requester')
    requester = ScenarioHttp(durable=adapter.durable, context=CONTEXT, endpoint=server.url,
        credential_env='STUDIO_REQUESTER_TOKEN', attempt_id='fixture-task-read', credential_kind='bearer')
    fixture = StudioSetup(setup=adapter.http, requester=requester)
    envelope = {'family': 'map', 'schemaVersion': '1.0', 'format': 'honua_map_package.v1',
        'bindings': [], 'dependencies': [], 'provenance': [],
        'body': {'title': 'Offline HTTP contract fixture', 'layers': [{'id': 'actual-source-fixture'}]}}
    state = SimpleNamespace(change=None, listing=0)
    baseline = {'candidateKey': adapter.http.identity['candidateKey'],
        'database': {'database': 'honua_agent_http_contract'}, 'observerPrincipal': 'sql-fixture-observer',
        'snapshot': {'studio': {}, 'protectedRegistry': {}, 'tableOids': {}, 'deleteTriggers': [], 'product': {}}}
    native_validation = {'status': 'valid', 'diagnostics': []}
    draft = {'draftId': DRAFT, 'itemId': ITEM, 'packageKey': 'ae_fixture', 'workspaceId': 'ae_fixture',
        'ownerId': 'fixture-task-principal', 'family': 'map', 'generation': 1, 'baseVersionId': None,
        'envelope': copy.deepcopy(envelope), 'validation': native_validation}
    version = {'versionId': VERSION, 'itemId': ITEM, 'sourceDraftId': DRAFT, 'ownerId': 'fixture-task-principal',
        'packageKey': 'ae_fixture', 'workspaceId': 'ae_fixture', 'versionNumber': 1, 'contentHash': 'a'*64,
        'envelope': copy.deepcopy(envelope), 'validation': native_validation}
    def respond(method, path, raw):
        if '/content-items?' in path:
            state.listing += 1
            expected_token = 'Bearer private-studio-requester' if state.listing > 1 else 'test-private-key'
            if state.listing > 1:
                assert server.seen[-1][2]['Authorization'] == expected_token
            item = {key: version[key] for key in ('itemId', 'packageKey', 'workspaceId', 'ownerId')}
            item.update(currentVersionId=VERSION, publishedVersionId=None)
            if state.change == 'already-published': item['publishedVersionId'] = VERSION
            if state.change == 'wrong-pointer': item['currentVersionId'] = ITEM
            rows = [] if state.listing == 1 else [item]
            if state.change == 'preexisting' and state.listing == 1: rows = [item]
            if state.change == 'duplicate' and state.listing > 1: rows.append(copy.deepcopy(item))
            result = {'items': rows, 'total': len(rows), 'nextCursor': None}
            status = 200
        elif path == '/api/v1/studio/package-drafts':
            assert method == 'POST'
            sent = json.loads(raw)
            assert sent['ownerId'] == 'fixture-task-principal' and sent['itemId'] == ITEM
            assert sent['envelope'] == envelope
            result, status = copy.deepcopy(draft), 201
            if state.change == 'wrong-owner': result['ownerId'] = 'setup-admin'
            if state.change == 'wrong-item': result['itemId'] = DRAFT
            if state.change == 'changed-envelope': result['envelope']['body']['title'] = 'changed'
            if state.change == 'requires-approval':
                result, status = {'operationId': 'studio.draft.create', 'status': 'RequiresApproval'}, 202
        elif path.endswith('/validate'):
            result, status = copy.deepcopy(native_validation), 200
            if state.change == 'invalid': result['status'] = 'invalid'
        elif path.endswith('/content-versions'):
            assert method == 'POST'
            result, status = copy.deepcopy(version), 201
            if state.change == 'wrong-draft': result['sourceDraftId'] = ITEM
            if state.change == 'not-first-version': result['versionNumber'] = 2
        else:
            assert path == '/api/v1/studio/content-items/'+ITEM+'/versions/'+VERSION
            assert server.seen[-1][2]['Authorization'] == 'Bearer private-studio-requester'
            result, status = copy.deepcopy(version), 200
            if state.change == 'task-denied': result, status = {'status': 403}, 403
            if state.change == 'changed-task-read': result['envelope']['body']['title'] = 'different'
        return status, {}, json.dumps({'success': status < 400, 'data': result}).encode()
    server.respond = respond
    return SimpleNamespace(fixture=fixture, envelope=envelope, state=state, baseline=baseline)


def prepare(setup):
    return setup.fixture.prepare_saved_map(namespace='ae_fixture', owner_id='fixture-task-principal', envelope=setup.envelope,
                                           item_id=ITEM, baseline=setup.baseline)


def test_prepares_only_real_saved_initial_state_and_reopens_with_actual_task_identity(setup, server):
    result = prepare(setup)
    assert (result['draftId'], result['itemId'], result['versionId']) == (DRAFT, ITEM, VERSION)
    assert result['initialItem']['publishedVersionId'] is None
    assert set(result['nativeReferences']) == {'before', 'create', 'validate', 'save', 'taskRead', 'after'}
    assert sum(method == 'POST' for method, *_ in server.seen) == 3
    assert all('publish' not in path and '/approve' not in path for _, path, *_ in server.seen)


@pytest.mark.parametrize('change', ['preexisting', 'wrong-owner', 'wrong-item', 'changed-envelope', 'requires-approval', 'invalid',
                                  'wrong-draft', 'not-first-version', 'task-denied', 'changed-task-read',
                                  'duplicate', 'wrong-pointer', 'already-published'])
def test_native_fixture_refusal_never_yields_a_saved_map_binding_or_publication(setup, server, change):
    setup.state.change = change
    with pytest.raises(PackError): prepare(setup)
    assert all('/approve' not in path and 'publish-requests' not in path for _, path, *_ in server.seen)
    assert sum(path == '/api/v1/studio/package-drafts' for _, path, *_ in server.seen) <= 1


@pytest.mark.parametrize('change', ['wrong-candidate', 'already-present', 'missing-sql-observer', 'missing-studio'])
def test_controller_allocation_requires_exact_absence_before_any_http_effect(setup, server, change):
    if change == 'wrong-candidate': setup.baseline['candidateKey'] = 'sha256:'+'b'*64
    elif change == 'already-present': setup.baseline['snapshot']['studio']['items'] = [{'item_id': ITEM}]
    elif change == 'missing-sql-observer': del setup.baseline['observerPrincipal']
    elif change == 'missing-studio': del setup.baseline['snapshot']['studio']
    with pytest.raises(PackError, match='SQL absence baseline'): prepare(setup)
    assert server.seen == []


def test_lost_or_completed_create_intent_cannot_be_replayed_as_another_setup(setup, server):
    prepare(setup)
    count = len(server.seen)
    with pytest.raises(PackError, match='already'): prepare(setup)
    assert len(server.seen) == count
