"""Regression tests for live server response shapes in P11, P12, P13, and P15."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_e2e_local_api import Observation
from agent_e2e_local_expectations import load_expected, load_projections, load_roles
from agent_e2e_local_projections import Context, evaluate, nullable_field_get, resolve_roles
from agent_e2e_local_recorder import ProjectionError, Record
from agent_e2e_local_runner import score_assertions
from agent_e2e_local_scenarios import P12
from agent_e2e_query_contract import service_list_items

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / 'certification/agent-e2e/corpus'


def _http_record(meta, path, payload):
    step, phase = meta
    raw = json.dumps(payload).encode()
    return Record(step, phase, 'http', 'controller', 'controller', 'GET', path, 200,
                  raw, 'sha256:' + hashlib.sha256(raw).hexdigest())


def _mcp_record(meta, payload, args):
    step, phase = meta
    raw = json.dumps(payload).encode()
    return Record(step, phase, 'mcp', 'controller', 'controller', 'POST', '/mcp#tools/call', 200,
                  raw, 'sha256:' + hashlib.sha256(raw).hexdigest(),
                  request_arguments_raw=json.dumps(args).encode())


def _paged_services(names, *, total=None):
    rows = [{'serviceName': name} for name in names]
    return {'success': True, 'data': {'items': rows, 'page': 1, 'pageSize': 50,
                                      'total': len(rows) if total is None else total, 'layerTotal': 3}}


def _wrap_multipolygon(geometry):
    if geometry.get('type') != 'Polygon':
        return copy.deepcopy(geometry)
    return {'type': 'MultiPolygon', 'coordinates': [copy.deepcopy(geometry['coordinates'])]}


def _multipolygon_town_records(scenario_id, variant):
    roles, records, stem = {}, {}, scenario_id.lower()
    for idx, name in enumerate(('parcels', 'flood_zones', 'shelters')):
        service = f'{stem}-{name}'
        roles[f'seeded:service:{name}'], roles[f'seeded:layer:{name}'] = service, idx
        raw_features = json.loads((CORPUS / f'town-{variant}/{name}.3857.geojson').read_text())['features']
        flat = [{'id': i + 1, 'attributes': copy.deepcopy(f['properties']),
                 'geometry': _wrap_multipolygon(f['geometry'])} for i, f in enumerate(raw_features)]
        args = {'tool': 'honua_query_features', 'arguments': {'serviceId': service, 'serviceLayerIndex': idx}}
        body = {'jsonrpc': '2.0', 'id': 1, 'result': {'isError': False, 'structuredContent': {
            'serviceId': 'svc-publish-' + service, 'serviceLayerIndex': idx, 'returnedCount': len(flat),
            'exceededTransferLimit': False, 'resultOffset': 0, 'features': flat}}}
        for phase, suffix in (('seed', 'before'), ('observe', 'after')):
            step = f'{stem}-{name.replace("_", "-")}-{suffix}'
            records[step] = _mcp_record((step, phase), body, args)
    return roles, records


def test_service_list_items_and_p11_created_service_use_paged_envelope():
    body = _paged_services(['geoprocessing', 'svc-a'])
    assert [row['serviceName'] for row in service_list_items(body)] == ['geoprocessing', 'svc-a']
    for bad in ({'data': [{'serviceName': 'a'}]}, {'data': {'items': []}},
                _paged_services(['geoprocessing'], total=2)):
        with pytest.raises(ValueError, match='service inventory'):
            service_list_items(bad)

    path = '/api/v1/admin/services/'
    bindings = [b for b in load_roles('P11') if b.role == 'created:service:buffer']
    records = {
        'p11-services-before': _http_record(('p11-services-before', 'seed'), path, _paged_services(['geoprocessing'])),
        'p11-services-after': _http_record(('p11-services-after', 'observe'), path,
                                           _paged_services(['geoprocessing', 'buffer-svc'])),
    }
    actions = [{'tool': 'honua_publish_result', 'status': 'pass',
                'json': {'result': {'structuredContent': {'serviceId': 'buffer-svc'}}}}]
    assert resolve_roles(bindings, records, agent_principal='agent', action_results=actions) == {
        'created:service:buffer': 'buffer-svc'}

    records['p11-services-after'] = _http_record(
        ('p11-services-after', 'observe'), path, _paged_services(['geoprocessing', 'buffer-svc'], total=3))
    with pytest.raises(ProjectionError):
        resolve_roles(bindings, records, agent_principal='agent', action_results=actions)

    records['p11-services-after'] = _http_record(
        ('p11-services-after', 'observe'), path, _paged_services(['geoprocessing']))
    with pytest.raises(ProjectionError, match='matched 0'):
        resolve_roles(bindings, records, agent_principal='agent', action_results=[])


class _P12SeedRecorder:
    principals = {'agent': ('unused', 'agent')}

    def __init__(self, ids, steps):
        self.ids, self.steps, self.envelope = ids, steps, None

    def json(self, step, *_args):
        self.steps.append(step)
        payload = _args[2] if len(_args) > 2 else None
        item, draft, version = self.ids
        if step == 'p12-seed-draft':
            self.envelope = copy.deepcopy(payload['envelope'])
            return {'observation': {'httpStatus': 201, 'body': {'data': {
                'draftId': draft, 'envelope': self.envelope}}}}
        if step == 'p12-seed-save':
            return {'observation': {'httpStatus': 201, 'body': {'data': {'itemId': item, 'versionId': version}}}}
        if step == 'p12-seed-version':
            return {'observation': {'httpStatus': 200, 'body': {'data': {
                'itemId': item, 'versionId': version, 'envelope': self.envelope}}}}
        return {'observation': {'httpStatus': 200, 'body': {'data': {}}}}


def test_p12_seed_uses_ensure_connection_without_database_handoff(monkeypatch):
    ids = ('11111111-1111-4111-8111-111111111111',
           '22222222-2222-4222-8222-222222222222',
           '33333333-3333-4333-8333-333333333333')
    seen = {'ensure': 0, 'handoff': 0, 'publish_conn': None, 'steps': []}

    class Town:
        def __init__(self, *_):
            pass

        def prepare(self, **_):
            return {'layers': {'shelters': {'schema': 'honua_data', 'table': 'imported_shelters'}}}

    def fake_pub_request(**kwargs):
        seen['publish_conn'] = kwargs['connection']
        return kwargs

    def fake_ensure():
        seen['ensure'] += 1
        return 'conn-admin-1'

    monkeypatch.setattr(P12, 'TownSetup', Town)
    monkeypatch.setattr(P12, 'publication_request', fake_pub_request)
    monkeypatch.setattr(P12, 'submit_publication', lambda *_, **__: {'status': 'completed', 'layerId': 3})
    stack = SimpleNamespace(
        compose_project='honua-p12-test',
        extra={'recorder': _P12SeedRecorder(ids, seen['steps']), 'ensureConnection': fake_ensure,
               'databaseHandoff': lambda *_: seen.__setitem__('handoff', seen['handoff'] + 1)})
    seeded = P12.scenario.seed(stack, 'A')
    assert seeded.state['itemId'] == ids[0]
    assert seen['ensure'] == 1 and seen['handoff'] == 0
    assert seen['publish_conn'] == 'conn-admin-1'
    assert 'p12-connection-create' not in seen['steps']


def test_nullable_field_get_and_evaluate_fail_closed():
    doc = {'data': {'items': [{'itemId': 'i1'}], 'total': 1}}
    assert nullable_field_get(doc, '/data/items/0/publishedVersionId') is None
    assert nullable_field_get(doc, '/data/nextCursor') is None
    assert nullable_field_get({'data': {'nextCursor': 'c2'}}, '/data/nextCursor') == 'c2'
    for bad_ptr in ('', '/data/items/1/publishedVersionId', '/data/missing/x', '/data/items/*', '/data/items/0'):
        with pytest.raises(ProjectionError):
            nullable_field_get(doc, bad_ptr)
    rec = _http_record(('s', 'observe'), '/x', doc)
    assert evaluate({'body': 's', 'pointer': '/data/nextCursor', 'nullable': True}, Context({'s': rec})) is None
    with pytest.raises(ProjectionError, match='nullable'):
        evaluate({'body': 's', 'pointer': '/data/nextCursor', 'nullable': False}, Context({'s': rec}))


def _p13_shared_preserved_fixture():
    roles = {
        'seeded:workspace:shared': 'ae_s1', 'seeded:owner:shared': 'owner-shared',
        'seeded:map:shared': '22222222-2222-4222-8222-222222222222', 'seeded:version:shared': 'v-shared',
        'seeded:map:target': '11111111-1111-4111-8111-111111111111',
    }
    shared_path = '/api/v1/studio/content-items?family=map&workspaceId=ae_s1&owner=owner-shared&limit=100'
    shared_item = {'itemId': roles['seeded:map:shared'], 'currentVersionId': 'v-shared',
                   'ownerId': 'owner-shared', 'workspaceId': 'ae_s1'}
    records = {
        'p13-shared-before': _http_record(('p13-shared-before', 'seed'), shared_path,
                                          {'data': {'items': [shared_item], 'total': 1}}),
        'p13-shared-after': _http_record(('p13-shared-after', 'observe'), shared_path,
                                         {'data': {'items': [shared_item], 'total': 1}}),
    }
    for name in ('parcels', 'flood_zones', 'shelters'):
        svc = 'p13-' + name.replace('_', '-')
        roles[f'seeded:service:{name}'] = svc
        body = {'data': {
            'serviceName': svc, 'enabledProtocols': ['FeatureServer'],
            'accessPolicy': {'allowAnonymous': False, 'allowAnonymousWrite': False,
                             'allowedRoles': [], 'allowedWriteRoles': []},
            'timeInfo': None, 'mapServer': {'maxImageWidth': 4096, 'maxImageHeight': 4096,
                                            'defaultImageWidth': 400, 'defaultImageHeight': 400,
                                            'defaultDpi': 96, 'defaultFormat': 'png',
                                            'defaultTransparent': True, 'maxFeaturesPerLayer': 10000}}}
        for phase, suffix in (('seed', 'before'), ('observe', 'after')):
            step = f'p13-service-{name.replace("_", "-")}-{suffix}'
            records[step] = _http_record((step, phase), f'/api/v1/admin/services/{svc}/settings', body)
    maps_body = {'data': {'items': [{'itemId': roles['seeded:map:shared']}, {'itemId': roles['seeded:map:target']}],
                          'total': 2}}
    svcs_body = _paged_services(['geoprocessing', 'p13-flood-zones', 'p13-parcels', 'p13-shelters'])
    for phase, suffix in (('seed', 'before'), ('observe', 'after')):
        records[f'p13-maps-inventory-{suffix}'] = _http_record(
            (f'p13-maps-inventory-{suffix}', phase), '/api/v1/studio/content-items?family=map&limit=100', maps_body)
        records[f'p13-services-inventory-{suffix}'] = _http_record(
            (f'p13-services-inventory-{suffix}', phase), '/api/v1/admin/services/', svcs_body)
    return roles, records


def test_p13_shared_preserved_handles_omitted_nulls_and_paged_services():
    roles, records = _p13_shared_preserved_fixture()
    expected = {'shared.preserved': load_expected('P13', 'A')['shared.preserved']}
    obs = [Observation('shared.preserved', None, {'projection': 'extract/v1'})]
    frozen = load_projections('P13', 'A')

    def status():
        return score_assertions(['shared.preserved'], obs, expected, records=records, roles=roles,
                                scenario_id='P13', frozen=frozen)[0]['status']

    assert status() == 'pass'

    published = copy.deepcopy(records['p13-shared-after'].json)
    published['data']['items'][0]['publishedVersionId'] = 'v-shared'
    saved_shared = records['p13-shared-after']
    records['p13-shared-after'] = _http_record(('p13-shared-after', 'observe'), saved_shared.path, published)
    assert status() == 'fail'
    records['p13-shared-after'] = saved_shared

    paged = copy.deepcopy(records['p13-maps-inventory-after'].json)
    paged['data']['nextCursor'] = 'page-2'
    saved_maps = records['p13-maps-inventory-after']
    records['p13-maps-inventory-after'] = _http_record(('p13-maps-inventory-after', 'observe'), saved_maps.path, paged)
    assert status() == 'fail'
    records['p13-maps-inventory-after'] = saved_maps

    extra = _paged_services(['geoprocessing', 'p13-flood-zones', 'p13-parcels', 'p13-shelters', 'rogue'])
    records['p13-services-inventory-after'] = _http_record(
        ('p13-services-inventory-after', 'observe'), '/api/v1/admin/services/', extra)
    assert status() == 'fail'


@pytest.mark.parametrize('scenario_id', ['P11', 'P13'])
@pytest.mark.parametrize('variant', ['A', 'B'])
def test_single_part_multipolygon_normalizes_and_multipart_fails_closed(scenario_id, variant):
    roles, records = _multipolygon_town_records(scenario_id, variant)
    expected = {'source-content.unchanged': load_expected(scenario_id, variant)['source-content.unchanged']}
    obs = [Observation('source-content.unchanged', None, {'projection': 'extract/v1'})]
    frozen = load_projections(scenario_id, variant)

    def status():
        return score_assertions(['source-content.unchanged'], obs, expected, records=records, roles=roles,
                                scenario_id=scenario_id, frozen=frozen)[0]['status']

    assert status() == 'pass'

    step = f'{scenario_id.lower()}-parcels-after'
    good = records[step]
    for bad_coords in (
        good.json['result']['structuredContent']['features'][0]['geometry']['coordinates'] * 2,
        [],
    ):
        mutated = copy.deepcopy(good.json)
        mutated['result']['structuredContent']['features'][0]['geometry']['coordinates'] = bad_coords
        records[step] = _mcp_record((step, 'observe'), mutated, json.loads(good.request_arguments_raw))
        assert status() == 'fail'
    records[step] = good


def test_p13_version_restored_and_p15_publication_private_remain_fail_closed():
    item, v1 = '11111111-1111-4111-8111-111111111111', '33333333-3333-4333-8333-333333333333'
    v1_body = {'data': {'itemId': item, 'versionId': v1, 'contentHash': 'a' * 64,
                        'envelope': {'body': {'layers': [{}, {}, {'style': {'type': 'circle'}}]}}}}
    p13_recs = {
        'p13-target-after': _http_record(('p13-target-after', 'observe'), '/api/v1/studio/content-items?family=map',
                                         {'data': {'items': [{'itemId': item, 'currentVersionId': v1}], 'total': 1}}),
        'p13-target-current': _http_record(('p13-target-current', 'observe'),
                                           f'/api/v1/studio/content-items/{item}/versions/{v1}', v1_body),
        'p13-target-v1': _http_record(('p13-target-v1', 'observe'),
                                      f'/api/v1/studio/content-items/{item}/versions/{v1}', v1_body),
        'p13-target-versions': _http_record(('p13-target-versions', 'observe'),
                                            f'/api/v1/studio/content-items/{item}/versions',
                                            {'data': {'itemId': item, 'versions': [v1_body['data']]}}),
    }
    p13_roles = {'seeded:map:target': item, 'seeded:version:v1': v1, 'seeded:hash:v1': 'a' * 64}
    res_p13 = score_assertions(
        ['version.restored'], [Observation('version.restored', None, {'projection': 'extract/v1'})],
        {'version.restored': load_expected('P13', 'A')['version.restored']}, records=p13_recs, roles=p13_roles,
        scenario_id='P13', frozen=load_projections('P13', 'A'), run_facts={'agentPrincipal': 'agent'})[0]
    assert res_p13['status'] == 'fail' and 'pointer index absent' in res_p13['reason']

    p15_recs = {'p15-operator-grants': _http_record(
        ('p15-operator-grants', 'observe'), '/api/v1/admin/api-keys/op-1/effective-permissions',
        {'data': {'id': 'op-1', 'permissions': ['read:*'], 'status': 'active', 'canAuthenticate': True}})}
    p15_roles = {'seeded:principal:operator': 'op-1', 'seeded:map:item': item,
                 'seeded:map:version': v1, 'seeded:map:hash': 'a' * 64}
    res_p15 = score_assertions(
        ['publication.private'], [Observation('publication.private', None, {'projection': 'extract/v1'})],
        {'publication.private': load_expected('P15', 'A')['publication.private']}, records=p15_recs, roles=p15_roles,
        scenario_id='P15', frozen=load_projections('P15', 'A'))[0]
    assert res_p15['status'] == 'fail' and 'unrecorded step' in res_p15['reason']
