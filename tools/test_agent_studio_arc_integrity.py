import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from agent_e2e_studio_arc import score_goal, ui_provenance


def record(body, time):
    raw = json.dumps(body).encode()
    return SimpleNamespace(principal='controller', status=200, raw=raw, json=body,
                           sha256='sha256:' + hashlib.sha256(raw).hexdigest(), recorded_at=time)


def evidence(tmp_path):
    now = datetime.now(timezone.utc)
    at = lambda n: (now + timedelta(seconds=n)).isoformat()
    customer = 'subject:issuer:customer@tenant:public'
    version = {'itemId': 'item', 'versionId': 'version', 'sourceDraftId': 'draft', 'createdBy': customer, 'contentHash': 'content-hash'}
    events = [{'auditId': n, 'action': 'operation.completed', 'resourceType': 'operation_instance',
               'actor': customer, 'outcome': 'Success', 'timestamp': at(n), 'resourceId': 'handle' + str(n),
               'details': 'operationId=studio.draft.' + operation + ';status=Completed'}
              for n, operation in [(1, 'create'), (2, 'save-version')]]
    records = {'p08-version': record({'data': version}, at(3)),
               'studio-audit-before-0': record({'items': []}, at(0)),
               'studio-audit-after-0': record({'items': events}, at(3))}
    rows = []
    for n, (kind, path, data) in enumerate([
        ('studio_ui_create', '/api/studio/v1/studio/package-drafts', {'draftId': 'draft'}),
        ('studio_ui_save', '/api/studio/v1/studio/package-drafts/draft/content-versions', version)], 1):
        action = {'tool': kind, 'arguments': {'path': path, 'startedAt': at(n)}}
        raw = json.dumps(action).encode()
        response = json.dumps({'data': data}).encode()
        action_path, response_path = tmp_path / str(n), tmp_path / (str(n) + '.response')
        action_path.write_bytes(raw)
        response_path.write_bytes(response)
        interaction = json.dumps({'tool': 'studio_click', 'arguments': {'selector': 'button'}}).encode()
        interaction_path = tmp_path / (str(n) + '.click')
        interaction_path.write_bytes(interaction)
        rows.append({'status': 'pass', 'logPath': str(interaction_path),
                     'digest': 'sha256:' + hashlib.sha256(interaction).hexdigest()})
        rows.append({'status': 'pass', 'logPath': str(action_path), 'resultRawPath': str(response_path),
                     'digest': 'sha256:' + hashlib.sha256(raw).hexdigest(),
                     'resultRawSha256': 'sha256:' + hashlib.sha256(response).hexdigest(), 'resultHttpStatus': 201})
    handles = {
        'handle1': {'operationInstanceId': 'handle1', 'operationId': 'studio.draft.create', 'status': 'Completed',
                    'resourceIds': {'draftId': 'draft', 'itemId': 'item'}},
        'handle2': {'operationInstanceId': 'handle2', 'operationId': 'studio.draft.save-version', 'status': 'Completed',
                    'resourceIds': {'itemId': 'item', 'versionId': 'version', 'contentHash': 'content-hash'}},
    }
    return records, rows, customer, handles.get


def test_ui_evidence_rejects_mcp_only_wrong_actor_and_wrong_map(tmp_path):
    records, rows, customer, handle_lookup = evidence(tmp_path)
    assert ui_provenance(records, rows, customer, handle_lookup)
    with pytest.raises(ValueError, match='uniquely bind'):
        ui_provenance(records, rows, customer, lambda _id: None)
    with pytest.raises(ValueError, match='browser create and save'):
        ui_provenance(records, [], customer, handle_lookup)
    with pytest.raises(ValueError, match='UI interactions'):
        ui_provenance(records, [rows[1], rows[3]], customer, handle_lookup)
    records['studio-audit-after-0'].json['items'][1]['actor'] = 'api-key:other'
    # Re-seal the changed controller observation to isolate the principal check.
    records['studio-audit-after-0'] = record(records['studio-audit-after-0'].json,
                                              records['studio-audit-after-0'].recorded_at)
    with pytest.raises(ValueError, match='another principal'):
        ui_provenance(records, rows, customer, handle_lookup)
    records, rows, customer, handle_lookup = evidence(tmp_path)
    records['p08-version'] = record({'data': {'itemId': 'different', 'versionId': 'version',
        'sourceDraftId': 'draft', 'createdBy': customer, 'contentHash': 'content-hash'}}, records['p08-version'].recorded_at)
    with pytest.raises(ValueError, match='browser create and save'):
        ui_provenance(records, rows, customer, handle_lookup)


def test_style_label_and_unsupported_goal_negatives():
    from agent_browser_studio import map_spec, label_ids
    body = {'layers': [{'id': n, 'visible': True} for n in ('parcels', 'flood_zones', 'shelters')],
            'mapSpec': map_spec(True)}
    records = {'p08-version': record({'data': {'envelope': {'body': body}}}, '2026-11-01T00:00:00Z'),
               'p08-render': record({'visibleLabelSourceIds': label_ids('P09', 'A')}, '2026-11-01T00:00:00Z')}
    def scores(requirements):
        return {r['assertion']: r['status'] for r in score_goal(records, requirements, 'A')}
    assert set(scores({'style', 'labels'}).values()) == {'pass'}
    body['mapSpec']['layers'][1]['paint']['fill-color'] = '#ff0000'
    records['p08-version'] = record({'data': {'envelope': {'body': body}}}, '2026-11-01T00:00:00Z')
    assert scores({'style', 'labels'})['goal.authored-style'] == 'fail'
    assert scores({'sharing'})['goal.sharing'] == 'fail'
    assert scores({'filter'})['goal.filters'] == 'fail'
    records['p08-render'] = record({'visibleLabelSourceIds': []}, '2026-11-01T00:00:00Z')
    assert scores({'labels'})['goal.labels'] == 'fail'


def test_browser_gate_records_native_create_and_save(tmp_path):
    from agent_browser_studio import OriginLock, PlaywrightPage
    from agent_e2e_local_runner import ActionLog
    page = PlaywrightPage.__new__(PlaywrightPage)
    page.origin = OriginLock('http://localhost:1234')
    page._stage_gate = page._identity = page._assistant_log = None
    page._refusal_log = ActionLog(tmp_path, 1, set(), 10)
    page.aborted = []
    class Response:
        status = 201
        headers = {'content-type': 'application/json'}
        def body(self): return b'{"data":{"draftId":"draft"}}'
    class Route:
        def __init__(self, path):
            self.request = SimpleNamespace(url='http://localhost:1234' + path, method='POST', resource_type='fetch')
        def fetch(self, **kwargs): return Response()
        def fulfill(self, **kwargs): pass
        def abort(self, reason): raise AssertionError(reason)
    page._gate(Route('/api/studio/v1/studio/package-drafts'))
    page._gate(Route('/api/studio/v1/studio/package-drafts/draft/content-versions'))
    assert [row['tool'] for row in page._refusal_log.rows] == ['studio_ui_create', 'studio_ui_save']
    assert all(row['status'] == 'pass' and row['resultHttpStatus'] == 201 for row in page._refusal_log.rows)
    assert json.loads((tmp_path / 'action-0001.json').read_text())['arguments']['startedAt']


def test_supported_filter_requires_both_saved_rules_and_rendered_labels():
    from agent_browser_studio import map_spec, label_ids
    body = {'layers': [{'id': n, 'visible': True} for n in ('parcels', 'flood_zones', 'shelters')],
            'mapSpec': map_spec(True, filtered=True)}
    records = {'p08-version': record({'data': {'envelope': {'body': body}}}, '2026-11-01T00:00:00Z'),
               'p08-render': record({'visibleLabelSourceIds': label_ids('P24', 'A')}, '2026-11-01T00:00:00Z')}
    def checks(): return {r['assertion']: r['status'] for r in score_goal(records, {'filter'}, 'A')}
    assert checks()['goal.filters'] == 'pass'
    assert checks()['goal.labels'] == 'pass'
    body['mapSpec']['layers'][-1].pop('filter')
    records['p08-version'] = record({'data': {'envelope': {'body': body}}}, '2026-11-01T00:00:00Z')
    assert checks()['goal.filters'] == 'fail'


def test_fixture_requirements_are_digest_pinned_and_unknown_fails(tmp_path):
    from agent_e2e_studio_arc import fixture_requirements, REQUIREMENTS_PATH
    from ai_arc_runner import RUNNER_FILES
    assert fixture_requirements('town-A', 'P08') == frozenset({'layers', 'style', 'labels', 'sharing'})
    assert fixture_requirements('town-B', 'P08') == fixture_requirements('town-A', 'P08')
    with pytest.raises(ValueError, match='requirements'):
        fixture_requirements('unknown', 'P08')
    bad = tmp_path / 'requirements.json'
    bad.write_bytes(REQUIREMENTS_PATH.read_bytes() + b' ')
    with pytest.raises(ValueError, match='digest'):
        fixture_requirements('town-A', 'P08', path=bad)
    assert 'certification/ai-arc/studio-fixture-requirements.json' in RUNNER_FILES


def test_publication_requires_active_exact_public_version():
    from agent_e2e_studio_arc import public_publication
    version = {'itemId': 'map', 'versionId': 'version', 'contentHash': 'hash'}
    route = {'publicationId': 'publication', 'activeVersionId': 'pub-version', 'lifecycle': 'Active',
             'policy': {'visibility': 'Public', 'share': {'allowAnonymous': True}}}
    pub_version = {'versionId': 'pub-version', 'sourceContentId': 'map',
                   'contentVersionId': 'version', 'contentHash': 'hash'}
    page = {'items': [{'route': route, 'versions': [pub_version]}], 'total': 1}
    def records():
        return {'p08-version': record({'data': version}, '2026-11-01T00:00:00Z'),
                'studio-publications-0': record(page, '2026-11-01T00:00:00Z')}
    assert public_publication(records())
    pub_version['contentVersionId'] = 'other'
    assert not public_publication(records())
    pub_version['contentVersionId'] = 'version'
    route['policy']['share']['allowAnonymous'] = False
    assert not public_publication(records())
    route['policy']['share']['allowAnonymous'] = True
    page['nextCursor'] = 'another-page'
    assert not public_publication(records())


def test_p08_never_seeds_a_saved_map(monkeypatch):
    import agent_e2e_local_scenarios.P08 as module
    seen = []
    monkeypatch.setattr(module, 'seed_case', lambda *args, **kwargs: seen.append((args, kwargs)))
    module.P08().seed(object(), 'A')
    assert seen[0][1]['create_map'] is False


def test_cli_surface_excludes_studio_writes_and_future_unknown_tools():
    from agent_e2e_studio_arc import studio_cli_tool_view
    names = ['honua_studio_add_layer', 'honua_studio_save_version', 'honua_studio_future_write',
             'honua_studio_get_draft', 'honua_studio_get_version', 'honua_query']
    result = studio_cli_tool_view({'tools': [{'name': n} for n in names]})
    assert [row['name'] for row in result['tools']] == names[3:]


def test_other_principal_mcp_edit_between_ui_create_and_save_fails(tmp_path):
    records, rows, customer, lookup = evidence(tmp_path)
    audit = records['studio-audit-after-0'].json
    audit['items'].append({'auditId': 3, 'action': 'operation.completed', 'resourceType': 'operation_instance',
        'actor': 'apikey:other', 'outcome': 'Success', 'resourceId': 'handle3',
        'timestamp': audit['items'][0]['timestamp'], 'details': 'operationId=studio.draft.update;status=Completed'})
    records['studio-audit-after-0'] = record(audit, records['studio-audit-after-0'].recorded_at)
    def mixed_lookup(key):
        if key == 'handle3':
            return {'operationInstanceId': key, 'resourceIds': {'draftId': 'draft', 'itemId': 'item'}}
        return lookup(key)
    with pytest.raises(ValueError, match='another principal'):
        ui_provenance(records, rows, customer, mixed_lookup)
    with pytest.raises(ValueError, match='unresolved'):
        ui_provenance(records, rows, customer, lambda key: None if key == 'handle3' else lookup(key))
    def unrelated(key):
        if key == 'handle3':
            return {'operationInstanceId': key, 'resourceIds': {'draftId': 'unrelated', 'itemId': 'unrelated'}}
        return lookup(key)
    assert ui_provenance(records, rows, customer, unrelated)


@pytest.mark.parametrize('visibility', ['team', 'organization'])
def test_sharing_requires_team_publication_colleague_and_anonymous_denial(visibility):
    from agent_e2e_studio_arc import sharing_from_records
    version = {'itemId': 'item', 'versionId': 'version', 'contentHash': 'hash'}
    path = '/api/v1/studio/published/team/map'
    def response(body, principal, identity, status=200, route=path):
        row = record(body, '2026-11-01T00:00:00Z')
        row.principal, row.principal_id, row.status, row.path = principal, identity, status, route
        row.method, row.phase, row.origin = 'GET', 'observe', 'controller'
        return row
    artifact = {**version, 'publicationId': 'request', 'visibility': visibility}
    publication = {'itemId': 'item', 'versionId': 'version', 'requestId': 'request',
                   'status': 'Accepted', 'intent': {'route': '/team/map', 'visibility': visibility}}
    records = {'p08-version': record({'data': version}, '2026-11-01T00:00:00Z'),
        'p08-maps': record({'data': {'items': [{'itemId': 'item', 'publishedVersionId': 'version'}]}}, '2026-11-01T00:00:00Z'),
        'studio-share-publication': response({'data': publication}, 'controller', 'admin', route='/api/v1/studio/content-items/item/versions/version/publish-requests/request'),
        'studio-share-controller': response({'data': artifact}, 'controller', 'admin'),
        'studio-share-colleague-auth': response({}, 'agent-user', 'colleague', route='/api/v1/studio/content-items?family=map&limit=1'),
        'studio-share-colleague': response({'data': artifact}, 'agent-user', 'colleague'),
        'studio-share-anonymous': response({}, 'anonymous', None, 401)}
    assert sharing_from_records(records)
    records['studio-share-anonymous'].status = 200
    assert not sharing_from_records(records)
    records['studio-share-anonymous'].status = 401
    records['p08-maps'] = record({'data': {'items': [{'itemId': 'item'}]}}, '2026-11-01T00:00:00Z')
    assert not sharing_from_records(records)
    records['p08-maps'] = record({'data': {'items': [{'itemId': 'item', 'publishedVersionId': 'version'}]}}, '2026-11-01T00:00:00Z')
    records['studio-share-publication'] = response({'data': {**publication, 'intent': {'route': '/team/map', 'visibility': 'public'}}}, 'controller', 'admin', route='/api/v1/studio/content-items/item/versions/version/publish-requests/request')
    assert not sharing_from_records(records)


def test_stage3_handoff_requires_controller_audit_and_handles():
    from agent_browser_studio import stage3_layers
    names = ('parcels', 'flood_zones', 'shelters')
    handoff = {'variant': 'A', 'fixtureId': 'town-A', 'principal': 'api-key:customer', 'agentKeyId': 'customer',
        'startedAt': '2026-11-01T00:00:00Z', 'endedAt': '2026-11-01T00:10:00Z',
        'layers': {name: {'service': 'svc' + str(i), 'layerId': i, 'operationInstanceId': 'op_' + str(i),
                          'auditId': i + 1} for i, name in enumerate(names)}}
    events = [{'auditId': i + 1, 'resourceId': 'op_' + str(i), 'resourceType': 'operation_instance',
        'action': 'operation.completed', 'outcome': 'Success', 'actor': 'api-key:customer',
        'timestamp': '2026-11-01T00:05:00Z', 'details': 'operationId=service.publish;status=Completed'}
        for i in range(3)]
    class Recorder:
        def __init__(self): self.calls = []
        def json(self, step, method, path):
            self.calls.append((step, method, path))
            if step == 'stage3-audit-0': return {'observation': {'httpStatus': 200, 'body': {'items': events}}}
            index = int(path.rsplit('_', 1)[-1])
            return {'observation': {'httpStatus': 200, 'body': {'data': {
                'operationInstanceId': 'op_' + str(index), 'operationId': 'service.publish', 'status': 'Completed',
                'resourceIds': {'serviceName': 'svc' + str(index), 'layerId': index}}}}}
    rec = Recorder()
    layers = stage3_layers(rec, handoff, 'A')
    assert layers['parcels'] == {'service': 'svc0', 'layerId': 0}
    assert all(method == 'GET' for _, method, _ in rec.calls)
    subset = {**handoff, 'layers': {'shelters': handoff['layers']['shelters']}}
    assert stage3_layers(Recorder(), subset, 'A') == {'shelters': {'service': 'svc2', 'layerId': 2}}
    assert stage3_layers(Recorder(), {**handoff, 'layers': {}}, 'A') == {}
    events[1]['actor'] = 'other'
    with pytest.raises(ValueError, match='audit'):
        stage3_layers(Recorder(), handoff, 'A')
    with pytest.raises(ValueError, match='handoff'):
        stage3_layers(Recorder(), None, 'A')


def test_recording_proxy_cannot_forward_removed_studio_mutation(tmp_path):
    from agent_cli_sessions import RecordingProxy
    from agent_e2e_studio_arc import studio_cli_tool_view
    calls = []
    catalog = studio_cli_tool_view({'tools': [{'name': 'honua_studio_add_layer'}, {'name': 'honua_query'}]})
    proxy = RecordingProxy(catalog, lambda *args: calls.append(args), tmp_path, 1, 10, server_name='honua')
    try:
        assert [row['name'] for row in proxy.rpc('tools/list', {})['tools']] == ['honua_query']
        with pytest.raises(ValueError, match='unadvertised'):
            proxy.rpc('tools/call', {'name': 'honua_studio_add_layer', 'arguments': {}})
        assert not calls
    finally:
        proxy.server.server_close()


def test_unpublished_probe_checks_colleague_and_restores_credentials(monkeypatch):
    import os
    from agent_e2e_studio_arc import probe_sharing
    version = {'itemId': 'item', 'versionId': 'version', 'contentHash': 'hash'}
    class Recorder:
        def __init__(self):
            self.records = {'p08-version': record({'data': version}, '2026-11-01T00:00:00Z'),
                'p08-maps': record({'data': {'items': [{'itemId': 'item'}]}}, '2026-11-01T00:00:00Z')}
            self.principals = {'agent-user': ('previous-env', 'customer')}
            self.calls = []
        def json(self, step, method, path, principal):
            self.calls.append((step, principal, self.principals.get(principal),
                               os.environ.get('HONUA_ARC_STUDIO_READER_BEARER')))
    monkeypatch.setenv('HONUA_ARC_STUDIO_READER_BEARER', 'previous')
    stack = SimpleNamespace(extra={'studioReaderSubjects': {'colleague': 'user-one'}})
    recorder = Recorder()
    probe_sharing(recorder, stack, lambda subject, role: 'token-' + subject + '-' + role)
    assert recorder.calls[0][2][1] == 'user-one' and recorder.calls[1][2][1] == 'user-one'
    assert recorder.calls[-1][1] == 'anonymous'
    assert recorder.principals['agent-user'] == ('previous-env', 'customer')
    assert os.environ['HONUA_ARC_STUDIO_READER_BEARER'] == 'previous'


def test_team_sharing_probe_discovers_controller_observed_publication(monkeypatch):
    from agent_e2e_studio_arc import probe_sharing, sharing_from_records
    version = {'itemId': 'item', 'versionId': 'version', 'contentHash': 'hash'}
    artifact = {**version, 'publicationId': 'req', 'visibility': 'team'}
    event = {'auditId': 1, 'action': 'operation.completed', 'outcome': 'Success',
             'resourceType': 'operation_instance', 'resourceId': 'op_1',
             'details': 'operationId=studio.content.create-publication-request;status=Completed'}
    class Recorder:
        def __init__(self):
            self.records = {'p08-version': record({'data': version}, '2026-11-01T00:00:00Z'),
                'p08-maps': record({'data': {'items': [{'itemId': 'item', 'publishedVersionId': 'version'}]}}, '2026-11-01T00:00:00Z'),
                'studio-audit-before-0': record({'items': []}, '2026-11-01T00:00:00Z'),
                'studio-audit-after-0': record({'items': [event]}, '2026-11-01T00:00:00Z')}
            self.principals = {'agent-user': ('original', 'owner')}
        def json(self, step, method, path, principal='controller'):
            if step.startswith('studio-share-handle-'):
                body = {'data': {'operationInstanceId': 'op_1', 'operationId': 'studio.content.create-publication-request',
                    'status': 'Completed', 'resourceIds': {'itemId': 'item', 'versionId': 'version', 'requestId': 'req'}}}
            elif step == 'studio-share-publication':
                body = {'data': {'requestId': 'req', 'itemId': 'item', 'versionId': 'version', 'status': 'Accepted',
                                 'intent': {'route': '/team/map', 'visibility': 'team'}}}
            elif step in ('studio-share-controller', 'studio-share-colleague'):
                body = {'data': artifact}
            else:
                body = {}
            row = record(body, '2026-11-01T00:00:00Z')
            row.principal, row.principal_id = principal, self.principals.get(principal, (None, None))[1]
            row.method, row.path, row.phase, row.origin = method, path, 'observe', 'controller'
            row.status = 401 if step == 'studio-share-anonymous' else 200
            self.records[step] = row
    rec = Recorder()
    stack = SimpleNamespace(extra={'studioReaderSubjects': {'colleague': 'colleague'}})
    probe_sharing(rec, stack, lambda subject, role: 'token')
    assert sharing_from_records(rec.records)
    assert rec.records['studio-share-controller'].path == '/api/v1/studio/published/team/map'
    assert rec.principals['agent-user'] == ('original', 'owner')
    rec.records['studio-share-anonymous'].status = 200
    assert not sharing_from_records(rec.records)
