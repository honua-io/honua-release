"""Actual worker MCP and independent HTTP replay; no live model qualification."""
import copy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_e2e_mcp_runner import run
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_studio_composition_observer import StudioCompositionObserver, CREATE, SAVE, REOPEN
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server
from test_agent_e2e_mcp import standard, result


@pytest.fixture
def authoring(adapter, server):
    item, draft_id = str(uuid4()), str(uuid4())
    created = datetime.now(timezone.utc)-timedelta(seconds=1)
    binding = {'itemId': item, 'namespace': 'ae_model_authoring', 'ownerPrincipalId': 'api-key:'+str(uuid4()),
        'family': 'map', 'notBefore': (created-timedelta(seconds=1)).isoformat()}
    state = SimpleNamespace(draft=None, versions={}, operations={}, current=None, records={}, change=None, ordinal=0)
    def retain(name, value):
        assert name not in state.records
        state.records[name] = copy.deepcopy(value)
    observer = StudioCompositionObserver(http=adapter.http, binding=binding, retain=retain)
    def respond(method, path, raw):
        if path == '/mcp':
            request = json.loads(raw) if raw else None
            if request and request.get('method') == 'tools/call':
                tool, args = request['params']['name'], request['params']['arguments']
                def execution(operation, resources):
                    native = {'operationInstanceId': uuid4().hex, 'operationId': operation, 'status': 'Completed',
                        'createdAt': created.isoformat(), 'resourceIds': resources}
                    state.operations[native['operationInstanceId']] = copy.deepcopy(native)
                    return native
                if tool == CREATE:
                    state.draft = {'draftId': draft_id, 'itemId': item, 'ownerId': binding['ownerPrincipalId'],
                        'workspaceId': binding['namespace'], 'packageKey': binding['namespace'],
                        'family': binding['family'], 'generation': 1, 'baseVersionId': None,
                        'createdBy': binding['ownerPrincipalId'], 'updatedBy': binding['ownerPrincipalId'],
                        'createdAt': created.isoformat(), 'updatedAt': created.isoformat(),
                        'envelope': {'family': binding['family'], 'schemaVersion': '1.0', 'body': copy.deepcopy(args['body'])},
                        'validation': {'status': 'valid', 'diagnostics': []}}
                    output = {**state.draft, 'operation': execution('studio.draft.create', {'draftId': draft_id, 'itemId': item})}
                elif tool == SAVE:
                    version = {'versionId': str(uuid4()), 'itemId': item, 'sourceDraftId': draft_id,
                        'ownerId': binding['ownerPrincipalId'], 'workspaceId': binding['namespace'], 'packageKey': binding['namespace'],
                        'versionNumber': len(state.versions)+1, 'contentHash': canonical_digest(state.draft['envelope'])[7:],
                        'envelope': copy.deepcopy(state.draft['envelope']), 'createdBy': binding['ownerPrincipalId'],
                        'createdAt': created.isoformat()}
                    state.versions[version['versionId']] = copy.deepcopy(version)
                    state.current = version['versionId']
                    state.draft['generation'] += 1
                    output = {'version': version, 'operation': execution('studio.draft.save-version',
                        {key: version[key] for key in ('itemId', 'versionId', 'contentHash')})}
                elif tool == REOPEN:
                    version = state.versions[args['versionId']]
                    state.draft = {**state.draft, 'draftId': str(uuid4()), 'generation': 1,
                        'baseVersionId': version['versionId'], 'envelope': copy.deepcopy(version['envelope'])}
                    output = {**state.draft, 'operation': execution('studio.content.reopen-version',
                        {'itemId': item, 'draftId': state.draft['draftId']})}
                else:
                    assert args['draftId'] == state.draft['draftId'] and args['generation'] == state.draft['generation']
                    if tool == 'honua_studio_add_layer': state.draft['envelope']['body']['layers'].append(copy.deepcopy(args['layer']))
                    elif tool == 'honua_studio_set_view': state.draft['envelope']['body']['view'] = copy.deepcopy(args['view'])
                    elif tool == 'honua_studio_add_widget': state.draft['envelope']['body'].setdefault('widgets', []).append(copy.deepcopy(args['widget']))
                    else: raise AssertionError(tool)
                    state.draft['generation'] += 1
                    output = state.draft
                return result(request, {'content': [], 'isError': False, 'structuredContent': copy.deepcopy(output)})
            return standard(request)
        assert method == 'GET', 'independent observer must not perform requested model mutations'
        if '/operations/handles/' in path:
            value = copy.deepcopy(state.operations[path.rsplit('/', 1)[1]])
            if state.change == 'foreign-handle': value['resourceIds']['itemId'] = str(uuid4())
        elif '/package-drafts/' in path:
            assert path.endswith(state.draft['draftId'])
            value = copy.deepcopy(state.draft)
            if state.change == 'changed-readback': value['envelope']['body']['title'] = 'not-the-model-result'
        elif '/versions/' in path:
            value = copy.deepcopy(state.versions[path.rsplit('/', 1)[1]])
        elif '/content-items?' in path:
            value = {'total': 1, 'nextCursor': None, 'items': [{'itemId': item, 'ownerId': binding['ownerPrincipalId'],
                'workspaceId': binding['namespace'], 'packageKey': binding['namespace'], 'currentVersionId': state.current,
                'publishedVersionId': None}]}
            if state.change == 'wrong-pointer': value['items'][0]['currentVersionId'] = item
        else: raise AssertionError(path)
        return 200, {}, json.dumps({'success': True, 'data': value}).encode()
    def typed_response(*args):
        status, headers, raw = respond(*args)
        return status, {'Content-Type': 'application/json', **headers}, raw
    server.respond = typed_response
    actor = {'endpoint': server.url+'/mcp', 'credentialKind': 'api-key', 'credential': 'private-model-task-key',
        'workflowView': None, 'agentContext': {'workspaceId': 'native-workspace', 'turnId': 'native-turn'}}
    def execute(tool, arguments, *, observe=True):
        state.ordinal += 1
        action = {'name': 'mcp_call_tool', 'input': {'name': tool, 'arguments': arguments}}
        payload = run(action, actor, 30)
        assert payload['error'] is None
        worker = {'agentResult': {'content': payload['result'], 'isError': False, 'errorCategory': None}}
        dispatch = {'ordinal': state.ordinal, 'actionId': canonical_digest({**{key: adapter.http.context[key]
            for key in ('campaignId', 'caseId', 'attempt', 'componentBindingDigest')}, 'ordinal': state.ordinal})[7:],
            'requestDigest': canonical_digest(action)}
        attempt = canonical_digest({key: adapter.http.context[key] for key in ('campaignId', 'caseId', 'attempt')})
        adapter.durable.state.setdefault('journals', {}).setdefault(attempt, []).append({'event': 'action', 'action': state.ordinal,
            'kind': 'mcp_call_tool', 'requestDigest': dispatch['requestDigest'], 'resultDigest': canonical_digest(worker['agentResult']), 'isError': False})
        return observer.selected(action, dispatch, worker) if observe else (action, dispatch, worker)
    create_args = {'itemId': item, 'workspaceId': binding['namespace'], 'packageKey': binding['namespace'],
                   'family': binding['family'], 'schemaVersion': '1.0', 'body': {'title': 'Model selected draft', 'layers': []}}
    return SimpleNamespace(observer=observer, state=state, execute=execute, create_args=create_args, binding=binding)


def test_worker_authors_map_adds_layer_sets_extent_and_saves_native_version(authoring, server):
    value = authoring
    created = value.execute(CREATE, value.create_args)
    assert created['resource']['envelope']['body']['layers'] == []
    draft = created['resource']
    added = value.execute('honua_studio_add_layer', {'draftId': draft['draftId'], 'generation': draft['generation'],
        'layer': {'id': 'actual-model-parcels', 'sourceId': 'native-source', 'visible': True}})
    changed = value.execute('honua_studio_set_view', {'draftId': draft['draftId'], 'generation': added['resource']['generation'],
        'view': {'bbox': [0, 0, 100, 100], 'crs': 'EPSG:3857'}})
    saved = value.execute(SAVE, {'draftId': draft['draftId'], 'generation': changed['resource']['generation']})
    projection = value.observer.map_composition(saved['resource'])
    assert projection == {'mapId': value.binding['itemId'], 'orderedLayerIds': ['actual-model-parcels'],
        'visibility': {'actual-model-parcels': True}, 'extent': [0, 0, 100, 100]}
    persisted = value.observer.saved(saved['resource'])
    assert persisted['versionId'] == value.state.current and persisted['documentSha256'] == canonical_digest(changed['resource']['envelope']['body'])[7:]
    assert saved['nativeReferences'].keys() == {'handle', 'version', 'current'}
    assert sum(method == 'POST' and path == '/mcp' for method, path, *_ in server.seen) > 4
    assert all(method == 'GET' for method, path, *_ in server.seen if path != '/mcp')


@pytest.mark.parametrize('change', ['changed-readback', 'foreign-handle'])
def test_uncorroborated_model_create_never_yields_native_owned_draft(authoring, change):
    action, dispatch, worker = authoring.execute(CREATE, authoring.create_args, observe=False)
    authoring.state.change = change
    with pytest.raises(PackError): authoring.observer.selected(action, dispatch, worker)
    assert not authoring.state.records


def test_model_owner_assignment_cannot_be_observed_as_own_compose(authoring):
    action, dispatch, worker = authoring.execute(CREATE, authoring.create_args, observe=False)
    action['input']['arguments']['ownerId'] = 'foreign-owner'
    with pytest.raises(PackError, match='assigned an owner'):
        authoring.observer.selected(action, dispatch, worker)


def test_save_claim_needs_exact_native_current_pointer(authoring):
    authoring.execute(CREATE, authoring.create_args)
    action, dispatch, worker = authoring.execute(SAVE, {'draftId': authoring.state.draft['draftId'], 'generation': 1}, observe=False)
    authoring.state.change = 'wrong-pointer'
    with pytest.raises(PackError, match='current pointer'):
        authoring.observer.selected(action, dispatch, worker)


def test_actual_reopen_corroborates_saved_bytes_and_new_native_draft(authoring):
    authoring.execute(CREATE, authoring.create_args)
    saved = authoring.execute(SAVE, {'draftId': authoring.state.draft['draftId'], 'generation': 1})
    reopened = authoring.execute(REOPEN, {'itemId': authoring.binding['itemId'], 'versionId': saved['resource']['versionId']})
    assert reopened['resource']['draftId'] != saved['resource']['sourceDraftId']
    assert reopened['resource']['envelope'] == saved['resource']['envelope']
    assert reopened['resource']['baseVersionId'] == saved['resource']['versionId']
