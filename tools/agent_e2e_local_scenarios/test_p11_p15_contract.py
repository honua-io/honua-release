"""Contract regression tests for P11/P12/P13/P15/arc-P10/studio against 786dc6e309."""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_browser_studio import map_body, seed_unstyled_map
from agent_e2e_local_api import Observation
from agent_e2e_local_expectations import load_document, load_expected, load_projections, load_roles
from agent_e2e_local_projections import resolve_roles
from agent_e2e_local_recorder import Record
from agent_e2e_local_runner import score_assertions
from agent_e2e_local_scenarios import P13, w2g3_common
from agent_e2e_local_scenarios.arc_p15 import ArcP15

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / 'certification/agent-e2e/corpus'


def _mcp_record(meta, payload, args):
    step, phase = meta
    raw = json.dumps(payload).encode()
    return Record(step, phase, 'mcp', 'controller', 'controller', 'POST', '/mcp#tools/call', 200,
                  raw, 'sha256:' + hashlib.sha256(raw).hexdigest(),
                  request_arguments_raw=json.dumps(args).encode())


def _http_record(meta, path, payload):
    step, phase = meta
    raw = json.dumps(payload).encode()
    return Record(step, phase, 'http', 'controller', 'controller', 'GET', path, 200,
                  raw, 'sha256:' + hashlib.sha256(raw).hexdigest())


def _floatify(node):
    if isinstance(node, list):
        return [_floatify(item) for item in node]
    return float(node) if type(node) is int else node


def _flat_feature(index, feature):
    geom = {**feature['geometry'], 'coordinates': _floatify(feature['geometry']['coordinates'])}
    return {'id': index + 1, 'attributes': copy.deepcopy(feature['properties']), 'geometry': geom}


def _flat_town_records(scenario_id, variant):
    roles, records, stem = {}, {}, scenario_id.lower()
    for idx, name in enumerate(('parcels', 'flood_zones', 'shelters')):
        service = f'{stem}-{name}'
        roles[f'seeded:service:{name}'], roles[f'seeded:layer:{name}'] = service, idx
        raw_features = json.loads((CORPUS / f'town-{variant}/{name}.3857.geojson').read_text())['features']
        flat = [_flat_feature(i, f) for i, f in enumerate(raw_features)]
        args = {'tool': 'honua_query_features', 'arguments': {'serviceId': service, 'serviceLayerIndex': idx}}
        body = {'jsonrpc': '2.0', 'id': 1, 'result': {'isError': False, 'structuredContent': {
            'serviceId': 'svc-publish-' + service, 'serviceLayerIndex': idx, 'returnedCount': len(flat),
            'exceededTransferLimit': False, 'resultOffset': 0, 'features': flat}}}
        for phase, suffix in (('seed', 'before'), ('observe', 'after')):
            step = f'{stem}-{name.replace("_", "-")}-{suffix}'
            records[step] = _mcp_record((step, phase), body, args)
    return roles, records


def test_read_town_uses_service_layer_index_and_rejects_conflict():
    calls = []
    recorder = SimpleNamespace(mcp_call=lambda step, tool, args: calls.append((step, tool, args)))
    layers = {n: {'service': f'svc-{n}', 'layerId': i} for i, n in enumerate(('parcels', 'flood_zones', 'shelters'))}
    w2g3_common.read_town(recorder, 'p11', layers, 'before')
    assert len(calls) == 3
    for idx, (_, tool, args) in enumerate(calls):
        assert tool == 'honua_query_features'
        assert args['serviceLayerIndex'] == idx and 'layerId' not in args
    layers['shelters'] = {'service': 'svc-shelters', 'layerId': 2, 'serviceLayerIndex': 9}
    with pytest.raises(ValueError, match='layer index'):
        w2g3_common.read_town(recorder, 'p11', layers, 'after')


@pytest.mark.parametrize('scenario_id', ['P11', 'P13'])
@pytest.mark.parametrize('variant', ['A', 'B'])
def test_flat_town_query_passes_and_fails_closed(scenario_id, variant):
    roles, records = _flat_town_records(scenario_id, variant)
    expected = {'source-content.unchanged': load_expected(scenario_id, variant)['source-content.unchanged']}
    obs = [Observation('source-content.unchanged', None, {'projection': 'extract/v1'})]
    frozen = load_projections(scenario_id, variant)

    def run_score():
        return score_assertions(list(expected), obs, expected, records=records, roles=roles,
                                scenario_id=scenario_id, frozen=frozen)[0]['status']

    assert run_score() == 'pass'
    step = f'{scenario_id.lower()}-shelters-after'
    good = records[step]
    for mutate in (_foreign_service, _int_bool, _coord_shift, _both_shapes):
        records[step] = mutate(good)
        assert run_score() == 'fail'
    records[step] = good


def _foreign_service(rec):
    body = copy.deepcopy(rec.json)
    body['result']['structuredContent']['serviceId'] = 'svc-publish-foreign'
    return _mcp_record((rec.step, rec.phase), body, json.loads(rec.request_arguments_raw))


def _int_bool(rec):
    body = copy.deepcopy(rec.json)
    body['result']['structuredContent']['features'][0]['attributes']['is_open'] = 1
    return _mcp_record((rec.step, rec.phase), body, json.loads(rec.request_arguments_raw))


def _coord_shift(rec):
    body = copy.deepcopy(rec.json)
    body['result']['structuredContent']['features'][0]['geometry']['coordinates'][0] += 0.001
    return _mcp_record((rec.step, rec.phase), body, json.loads(rec.request_arguments_raw))


def _both_shapes(rec):
    body = copy.deepcopy(rec.json)
    body['result']['structuredContent']['geojson'] = {'type': 'FeatureCollection', 'features': []}
    return _mcp_record((rec.step, rec.phase), body, json.loads(rec.request_arguments_raw))


def _buffer_ring(facts):
    cx, cy, radius = facts['center3857'][0], facts['center3857'][1], facts['radiusMetres']
    ring = [[cx + radius * math.cos(2 * math.pi * i / 128), cy + radius * math.sin(2 * math.pi * i / 128)]
            for i in range(128)]
    ring.append(ring[0][:])
    return ring, [cx - radius - 100, cy - radius - 100, cx + radius + 100, cy + radius + 100]


def _p11_publish_record():
    pub_raw = json.dumps({'jsonrpc': '2.0', 'id': 2, 'result': {'structuredContent': {
        'sourceJobId': 'job-1', 'status': 'Completed'}}}).encode()
    return Record('p11-result-publish', 'observe', 'mcp', 'agent', 'agent', 'POST', '/mcp#tools/call', 200,
                  pub_raw, 'sha256:' + hashlib.sha256(pub_raw).hexdigest(), origin='session-result',
                  request_arguments_raw=json.dumps({'tool': 'honua_publish_result',
                                                    'arguments': {'sourceId': 'job-1'}}).encode())


def _p11_flat_records(variant):
    facts = load_expected('P11', variant)['buffer.metric']
    ring, rect = _buffer_ring(facts)
    item, version = '11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222'
    roles = {'created:job:buffer': 'job-1', 'created:service:buffer': 'buf-svc',
             'child:layer:published': 7, 'created:map:result': item, 'child:layer:result': 'buf-layer'}
    query = {'jsonrpc': '2.0', 'id': 1, 'result': {'isError': False, 'structuredContent': {
        'serviceId': 'svc-publish-buf-svc', 'serviceLayerIndex': 7, 'returnedCount': 1, 'resultOffset': 0,
        'exceededTransferLimit': False, 'features': [{'id': 1, 'attributes': {'source_id': facts['sourceId']},
                                                      'geometry': {'type': 'Polygon', 'coordinates': [ring]}}]}}}
    saved = {'data': {'versionId': version, 'envelope': {'body': {
        'view': {'bbox': rect, 'crs': 'EPSG:3857'},
        'layers': [{'id': 'buf-layer', 'sourceId': 'buf-svc', 'visible': True}]}}}}
    records = {
        'p11-result-feature': _http_record(('p11-result-feature', 'observe'),
                                           '/rest/services/buf-svc/FeatureServer/7/query?f=json',
                                           {'spatialReference': {'wkid': 3857}}),
        'p11-result-geojson': _mcp_record(('p11-result-geojson', 'observe'), query,
                                          {'tool': 'honua_query_features',
                                           'arguments': {'serviceId': 'buf-svc', 'serviceLayerIndex': 7}}),
        'p11-map-version': _http_record(('p11-map-version', 'observe'),
                                        f'/api/v1/studio/content-items/{item}/versions/{version}', saved),
        'p11-map-current': _http_record(('p11-map-current', 'observe'), '/api/v1/studio/content-items?family=map',
                                        {'data': {'total': 1, 'items': [{'itemId': item, 'currentVersionId': version}]}}),
        'p11-result-publish': _p11_publish_record(),
    }
    return roles, records


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_p11_buffer_and_map_composition_use_flat_features(variant):
    roles, records = _p11_flat_records(variant)
    wanted, frozen = load_expected('P11', variant), load_projections('P11', variant)

    def run_score(assertion):
        return score_assertions([assertion], [Observation(assertion, None, {'projection': 'extract/v1'})],
                                {assertion: wanted[assertion]}, records=records, roles=roles,
                                scenario_id='P11', frozen=frozen)[0]['status']

    assert run_score('buffer.metric') == 'pass'
    assert run_score('map.composition') == 'pass'
    bad = copy.deepcopy(records['p11-result-geojson'].json)
    bad['result']['structuredContent']['features'][0]['geometry']['type'] = 'Point'
    records['p11-result-geojson'] = _mcp_record(('p11-result-geojson', 'observe'), bad,
                                                {'tool': 'honua_query_features',
                                                 'arguments': {'serviceId': 'buf-svc', 'serviceLayerIndex': 7}})
    assert run_score('map.composition') == 'fail'
    assert run_score('buffer.metric') == 'fail'


def test_studio_map_envelopes_use_honua_map_v1_and_map_id():
    body = map_body('A', [-20010, -5010, -18800, -4700], False)
    assert body['mapId'] == 'synthetic-town-a' and body['format'] == 'honua_map.v1'
    assert 'mapPackageId' not in body

    captured = []

    class StudioRecorder:
        def mcp_call(self, *_):
            return {'structuredContent': {'offset': 0, 'hasMore': False, 'layerCount': 3, 'layers': [
                {'serviceName': f'svc-{n}', 'layerId': i, 'serviceId': f'svc-id-{n}'}
                for i, n in enumerate(('parcels', 'flood_zones', 'shelters'))]}}

        def store_derived(self, *_):
            pass

        def json(self, step, *_args):
            captured.append((step, _args[-1] if len(_args) >= 3 else None))
            return {'observation': {'httpStatus': 201, 'body': {'data': {'draftId': 'draft-1'}}}}

    layers = {n: {'service': f'svc-{n}', 'layerId': i} for i, n in enumerate(('parcels', 'flood_zones', 'shelters'))}
    seed_unstyled_map(StudioRecorder(), 'A', layers, 'owner-1')
    draft = dict(captured)['p09-seed-draft']
    assert draft['envelope']['schemaVersion'] == '1.0'
    assert draft['envelope']['format'] == 'honua_map.v1'
    assert draft['envelope']['body']['mapId'] == 'synthetic-town-a'
    assert draft['envelope']['body']['format'] == 'honua_map.v1'


def test_p13_seed_map_uses_honua_map_v1_and_map_id():
    envelopes = []
    item = '11111111-1111-4111-8111-111111111111'
    ver = '33333333-3333-4333-8333-333333333333'

    class P13Rec:
        def json(self, step, *_args, **_kw):
            payload = _args[2] if len(_args) > 2 else None
            if step.endswith('-create'):
                envelopes.append(payload['envelope'])
                return {'observation': {'httpStatus': 201, 'body': {'data': {
                    'itemId': payload['itemId'], 'ownerId': payload['ownerId'], 'draftId': 'd1'}}}}
            if step.endswith('-validate'):
                return {'observation': {'httpStatus': 200, 'body': {'data': {'status': 'valid', 'diagnostics': []}}}}
            if step.endswith('-save'):
                return {'observation': {'httpStatus': 201, 'body': {'data': {
                    'itemId': item, 'ownerId': 'owner', 'versionNumber': 1, 'versionId': ver, 'contentHash': 'a' * 64}}}}
            if step.endswith('-before'):
                return {'observation': {'httpStatus': 200, 'body': {'data': {
                    'items': [{'itemId': item, 'currentVersionId': ver}], 'total': 1, 'nextCursor': None}}}}
            return {'observation': {'httpStatus': 200, 'body': {'data': {'itemId': item, 'versionId': ver}}}}

    town = {'layers': {n: {'service': f's-{n}'} for n in ('parcels', 'flood_zones', 'shelters')}}
    P13.P13._map(P13Rec(), prefix='p13-target', namespace='ae_t1', owner='owner', principal='agent', town=town)
    assert envelopes[0]['format'] == 'honua_map.v1' and envelopes[0]['schemaVersion'] == '1.0'
    assert envelopes[0]['body']['mapId'] == 'ae_t1' and envelopes[0]['body']['format'] == 'honua_map.v1'


def test_arc_p15_seed_uses_honua_map_v1_and_records_geo_read():
    steps = []

    class ArcRec:
        principals = {'agent': ('u', 'agent-id')}

        def json(self, step, *_args):
            path, payload = _args[1], (_args[2] if len(_args) > 2 else None)
            steps.append((step, path, payload))
            replies = {
                'arc-p15-service-meta': (200, {'serviceName': 'svc', 'layers': [{'id': 2}]}),
                'p15-map-draft': (201, {'data': {'draftId': '11111111-1111-4111-8111-111111111111'}}),
                'p15-map-saved': (201, {'data': {'itemId': '22222222-2222-4222-8222-222222222222',
                                                 'versionId': '33333333-3333-4333-8333-333333333333',
                                                 'contentHash': 'a' * 64}}),
            }
            status, body = replies.get(step, (200, {'features': []}))
            return {'observation': {'httpStatus': status, 'body': body}}

    shared = {'variant': 'A', 'service': 'svc', 'layerId': 2, 'caseReceiptSha256': 'sha256:' + 'a' * 64}
    stack = SimpleNamespace(compose_project='arc-p15', extra={'recorder': ArcRec(), 'arcSharedService': shared,
                                                              'addPrincipal': lambda *_: None})
    ArcP15().seed(stack, 'A')
    by_step = {step: (path, payload) for step, path, payload in steps}
    draft = by_step['p15-map-draft'][1]
    assert draft['packageKey'] == 'kestrel-map' and draft['envelope']['format'] == 'honua_map.v1'
    assert draft['envelope']['body']['mapId'] == 'kestrel-map' and 'mapPackageId' not in draft['envelope']['body']
    assert 'f=geojson' in by_step['p15-source-before-geo'][0]
    assert 'outFields=source_id%2Cname%2Ccapacity%2Cis_open' in by_step['p15-source-before-geo'][0]


def _shelters_fs_body(geo):
    return {
        'geometryType': 'esriGeometryPoint', 'exceededTransferLimit': False,
        'spatialReference': {'wkid': 3857}, 'objectIdFieldName': 'objectid',
        'fields': [{'name': 'objectid', 'type': 'esriFieldTypeOID'},
                   {'name': 'capacity', 'type': 'esriFieldTypeInteger'},
                   {'name': 'is_open', 'type': 'esriFieldTypeSmallInteger'},
                   {'name': 'name', 'type': 'esriFieldTypeString'},
                   {'name': 'source_id', 'type': 'esriFieldTypeString'}],
        'features': [{'attributes': {'objectid': i + 1, **f['properties'], 'is_open': int(f['properties']['is_open'])},
                      'geometry': {'x': f['geometry']['coordinates'][0], 'y': f['geometry']['coordinates'][1]}}
                     for i, f in enumerate(geo['features'])]}


def test_p12_boolean_geo_crosscheck_and_storage_layer_roles():
    geo = json.loads((CORPUS / 'town-A/shelters.3857.geojson').read_text())
    fs_body = _shelters_fs_body(geo)
    base = '/rest/services/svc/FeatureServer/3/query?'
    json_path = base + 'f=json&where=1%3D1&outFields=%2A&returnGeometry=true&outSR=3857&orderByFields=source_id+ASC&resultRecordCount=1000'
    geo_path = base + 'f=geojson&where=1%3D1&outFields=%2A&returnGeometry=true&outSR=4326&orderByFields=source_id+ASC&resultRecordCount=1000'
    pub = _http_record(('publish-shelters-submit', 'seed'), '/api/v1/operations/service.publish/submit',
                       {'data': {'resourceIds': {'serviceName': 'svc', 'storageLayerId': '3'}}})
    roles = resolve_roles([r for r in load_roles('P12') if r.step == 'publish-shelters-submit'],
                          {'publish-shelters-submit': pub}, agent_principal='agent')
    assert roles == {'seeded:service:shelters': 'svc', 'seeded:layer:shelters': '3'}
    records = {
        'p12-source-before': _http_record(('p12-source-before', 'seed'), json_path, fs_body),
        'p12-source-before-geo': _http_record(('p12-source-before-geo', 'seed'), geo_path, geo),
        'p12-source-after': _http_record(('p12-source-after', 'observe'), json_path, fs_body),
        'p12-source-after-geo': _http_record(('p12-source-after-geo', 'observe'), geo_path, geo),
    }
    expected = {'source-content.unchanged': load_expected('P12', 'A')['source-content.unchanged']}
    obs = [Observation('source-content.unchanged', None, {'projection': 'extract/v1'})]
    frozen = load_projections('P12', 'A')
    assert score_assertions(list(expected), obs, expected, records=records, roles=roles,
                            scenario_id='P12', frozen=frozen)[0]['status'] == 'pass'
    bad_geo = copy.deepcopy(geo)
    bad_geo['features'][0]['properties']['is_open'] = not bad_geo['features'][0]['properties']['is_open']
    records['p12-source-after-geo'] = _http_record(('p12-source-after-geo', 'observe'), geo_path, bad_geo)
    assert score_assertions(list(expected), obs, expected, records=records, roles=roles,
                            scenario_id='P12', frozen=frozen)[0]['status'] == 'fail'


def test_frozen_expectations_have_no_legacy_layer_id_args_or_pointers():
    for sid, profile in (('P11', None), ('P12', None), ('P13', None), ('P15', None), ('P10', 'arc')):
        raw = json.dumps(load_document(sid, profile))
        assert '"/layerId"' not in raw
        assert '"/data/resourceIds/layerId"' not in raw
        assert '"/result/structuredContent/geojson' not in raw
