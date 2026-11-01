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
    def scores(goal):
        return {r['assertion']: r['status'] for r in score_goal(records, goal, 'A')}
    assert set(scores('style and labels').values()) == {'pass'}
    body['mapSpec']['layers'][1]['paint']['fill-color'] = '#ff0000'
    records['p08-version'] = record({'data': {'envelope': {'body': body}}}, '2026-11-01T00:00:00Z')
    assert scores('style and labels')['goal.authored-style'] == 'fail'
    assert scores('share with operators')['goal.sharing'] == 'fail'
    assert scores('filter only parcels')['goal.filters'] == 'fail'
    assert scores('make it red')['goal.unsupported-colour'] == 'fail'
    records['p08-render'] = record({'visibleLabelSourceIds': []}, '2026-11-01T00:00:00Z')
    assert scores('labels')['goal.labels'] == 'fail'


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
    goal = 'Show only open shelters with capacity over 50'
    def checks(): return {r['assertion']: r['status'] for r in score_goal(records, goal, 'A')}
    assert checks()['goal.filters'] == 'pass'
    assert checks()['goal.labels'] == 'pass'
    body['mapSpec']['layers'][-1].pop('filter')
    records['p08-version'] = record({'data': {'envelope': {'body': body}}}, '2026-11-01T00:00:00Z')
    assert checks()['goal.filters'] == 'fail'


def test_unrecognized_goal_fails_closed_even_when_map_is_correct():
    rows = score_goal({}, 'Add a buildings layer as well', 'A', recognized=False)
    assert any(row['assertion'] == 'goal.unsupported-instructions' and row['status'] == 'fail' for row in rows)


def test_recognized_development_goals_are_pinned_to_fixture_variant():
    from agent_e2e_studio_arc import STUDIO_GOAL_SHA256
    from ai_arc_runner import ARC_PATH
    arc = json.loads(ARC_PATH.read_text())
    goals = arc['targets']['local-docker']['goals']['studio']
    assert {variant: hashlib.sha256(goal.encode()).hexdigest() for variant, goal in goals.items()} == STUDIO_GOAL_SHA256
    assert STUDIO_GOAL_SHA256['A'] != STUDIO_GOAL_SHA256['B']


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


def test_open_map_is_not_a_filter_but_open_shelters_are():
    from agent_e2e_studio_arc import requests_filter
    assert not requests_filter('Let the operator team open it')
    assert requests_filter('Show open shelters with room for more than 50 people')


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


def test_sharing_requires_authenticated_colleague_and_denied_outsider():
    from agent_e2e_studio_arc import sharing_from_records
    version = {'itemId': 'item', 'versionId': 'version', 'contentHash': 'hash'}
    def response(body, principal, identity, status=200, path='/map'):
        row = record(body, '2026-11-01T00:00:00Z')
        row.principal, row.principal_id, row.status, row.path = principal, identity, status, path
        return row
    records = {'p08-version': record({'data': version}, '2026-11-01T00:00:00Z'),
        'studio-share-colleague-auth': response({}, 'agent-user', 'colleague'),
        'studio-share-outsider-auth': response({}, 'agent-user', 'outsider'),
        'studio-share-colleague': response({'data': version}, 'agent-user', 'colleague'),
        'studio-share-outsider': response({}, 'agent-user', 'outsider', 403),
        'studio-share-anonymous': response({}, 'anonymous', None, 401)}
    assert sharing_from_records(records)
    records['studio-share-outsider'].status = 200
    assert not sharing_from_records(records)
    records['studio-share-outsider'].status = 403
    records['studio-share-colleague'] = response({'data': {'itemId': 'item', 'versionId': 'other', 'contentHash': 'hash'}}, 'agent-user', 'colleague')
    assert not sharing_from_records(records)
    records['studio-share-outsider-auth'].status = 401
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


def test_sharing_probe_uses_distinct_users_and_restores_credentials(monkeypatch):
    import os
    from agent_e2e_studio_arc import probe_sharing
    version = {'itemId': 'item', 'versionId': 'version', 'contentHash': 'hash'}
    class Recorder:
        def __init__(self):
            self.records = {'p08-version': record({'data': version}, '2026-11-01T00:00:00Z')}
            self.principals = {'agent-user': ('previous-env', 'customer')}
            self.calls = []
        def json(self, step, method, path, principal):
            self.calls.append((step, principal, self.principals.get(principal),
                               os.environ.get('HONUA_ARC_STUDIO_READER_BEARER')))
    monkeypatch.setenv('HONUA_ARC_STUDIO_READER_BEARER', 'previous')
    stack = SimpleNamespace(extra={'studioReaderSubjects': {'colleague': 'user-one', 'outsider': 'user-two'}})
    recorder = Recorder()
    probe_sharing(recorder, stack, lambda subject: 'token-' + subject)
    assert recorder.calls[0][2][1] == 'user-one' and recorder.calls[2][2][1] == 'user-two'
    assert recorder.calls[-1][1] == 'anonymous'
    assert recorder.principals['agent-user'] == ('previous-env', 'customer')
    assert os.environ['HONUA_ARC_STUDIO_READER_BEARER'] == 'previous'
