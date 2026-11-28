import json
from dataclasses import replace

import pytest

from agent_e2e_local_api import Seeded, Stack
from agent_e2e_local_recorder import LoggedAction
from agent_e2e_local_runner import oracle, select_case, PACK
from agent_e2e_local_scenarios.P10 import P10


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_frozen_expectations_match_the_fixtures_and_the_pack(variant):
    expected = P10().expected(variant)
    assert set(expected) == set(select_case(PACK, 'P10', variant)['assertions'])
    assert expected['filter.exact'] == oracle.fixture_expectation(variant, 'filter.exact')
    fixture = oracle.fixture_expectation(variant, 'features.exact', layer='shelters')
    frozen = expected['source-content.unchanged']['resources'][0]['content']
    assert frozen['srid'] == fixture['srid']
    assert frozen['features'] == sorted(fixture['features'], key=lambda f: f['properties']['source_id'])


def test_expectations_reject_held_out_variant():
    with pytest.raises(ValueError):
        P10().expected('C')


def test_seed_is_idempotent_per_case(monkeypatch, tmp_path):
    import agent_e2e_local_scenarios.w2g3_common as common
    calls = []

    class Town:
        def __init__(self, http, manifest): pass
        def prepare(self, **kw):
            calls.append(kw)
            return {'layers': {'shelters': {'service': 'svc', 'layerId': 3}}}

    class Recorder:
        def __init__(self): self.steps = []
        def json(self, step, method, path, body=None, **kw): self.steps.append(step)
    monkeypatch.setattr(common, 'town_setup', lambda recorder, variant: Town(recorder, None))
    recorder = Recorder()
    stack = Stack('http://127.0.0.1:1', 'k', None, 'corpus-abc', 'f', str(tmp_path), 'sha256:' + 'a' * 64,
                  {'recorder': recorder, 'ensureConnection': lambda: 'conn-1'})
    first = P10().seed(stack, 'A')
    assert P10().seed(stack, 'A') == first
    assert len(calls) == 1 and recorder.steps == ['p10-source-before', 'p10-source-before-geo']
    with pytest.raises(ValueError):
        P10().seed(stack, 'B')


def test_query_selection_uses_the_last_query_and_requires_the_seeded_layer():
    seeded = Seeded({'service': 'svc', 'layerId': 3})

    def action(n, tool, args):
        return LoggedAction(1, n, f'sha256:{n}', 'tool_call', tool, args, None, 'pass', '/x')
    actions = [action(1, 'honua_query_features', {'serviceId': 'svc', 'layerId': 3, 'where': 'a'}),
               action(2, 'honua_query_features', {'serviceId': 'other', 'layerId': 3}),
               action(3, 'honua_query_features', {'serviceId': 'svc', 'layerId': '3', 'where': 'b'}),
               action(4, 'honua_get_layer', {'serviceId': 'svc', 'layerId': 3})]
    assert P10.selected_query(actions, seeded).sequence == 3
    assert P10.selected_query(actions[1:2], seeded) is None
    assert P10.selected_query([replace(actions[0], status='fail')], seeded) is None


def test_observations_are_projection_specs_not_values(tmp_path):
    class Recorder:
        records = {}
        def json(self, *a, **kw): pass
        def actions(self): return ()
    stack = Stack('http://127.0.0.1:1', 'k', None, 'p', 'f', str(tmp_path), 'sha256:' + 'a' * 64,
                  {'recorder': Recorder()})
    rows = P10().observe(stack, Seeded({'service': 'svc', 'layerId': 3}), 'A')
    assert [r.assertion_id for r in rows] == ['filter.exact', 'source-content.unchanged']
    assert all(r.observed is None and r.evidence['projection'] == 'extract/v1' for r in rows)
    assert 's3' not in json.dumps([r.evidence for r in rows])


def test_query_selection_accepts_service_layer_index_and_published_service_id():
    seeded = Seeded({'service': 'p10_abc-shelters', 'layerId': 3})

    def action(n, args, status='pass'):
        return LoggedAction(1, n, f'sha256:{n}', 'tool_call', 'honua_query_features', args, None, status, '/x')

    modern = action(1, {'serviceId': 'svc-publish-p10_abc-shelters', 'serviceLayerIndex': 3, 'where': 'is_open = true'})
    assert P10.selected_query([modern], seeded).sequence == 1
    conflict = action(2, {'serviceId': 'svc-publish-p10_abc-shelters', 'serviceLayerIndex': 3, 'layerId': 4})
    assert P10.selected_query([conflict], seeded) is None
    wrong_layer = action(3, {'serviceId': 'svc-publish-p10_abc-shelters', 'serviceLayerIndex': 2})
    assert P10.selected_query([wrong_layer], seeded) is None


def test_p10_handoff_supports_flat_features_and_legacy_geojson(tmp_path):
    import hashlib
    from agent_e2e_local_recorder import Record

    def make_stack(body):
        raw = json.dumps(body, separators=(',', ':')).encode()
        rec = Record('p10-replay-query', 'observe', 'mcp', 'controller', 'admin', 'POST', '/mcp#tools/call', 200,
                     raw, 'sha256:' + hashlib.sha256(raw).hexdigest())
        recorder = type('Rec', (), {'records': {'p10-replay-query': rec}})()
        return Stack('http://127.0.0.1:1', 'k', None, 'p', 'f', str(tmp_path), 'sha256:' + 'a' * 64,
                     {'recorder': recorder})

    flat_spec = P10().handoff(make_stack({'jsonrpc': '2.0', 'id': 1, 'result': {'structuredContent': {
        'features': [{'id': 2, 'attributes': {'name': 'Willow Central'}}]}}}), None, 'B')
    assert flat_spec['object']['facts']['object']['names']['map']['pointer'] == '/result/structuredContent/features'
    assert flat_spec['object']['facts']['object']['names']['to']['item'] == '/attributes/name'

    geo_spec = P10().handoff(make_stack({'jsonrpc': '2.0', 'id': 1, 'result': {'structuredContent': {
        'geojson': {'type': 'FeatureCollection', 'features': [{'properties': {'name': 'Kestrel'}}]}}}}), None, 'A')
    assert geo_spec['object']['facts']['object']['names']['map']['pointer'] == '/result/structuredContent/geojson/features'
    assert geo_spec['object']['facts']['object']['names']['to']['item'] == '/properties/name'


def test_p10_projection_resolves_storage_layer_id_and_flat_oid_features():
    import hashlib
    import agent_e2e_local_projections as projections
    from agent_e2e_local_expectations import load_roles
    from agent_e2e_local_recorder import ProjectionError, Record, action_bytes

    def rec(step, phase, kind, method, path, body, *, args=None, replay=None):
        raw = json.dumps(body, separators=(',', ':')).encode()
        return Record(step, phase, kind, 'controller', 'admin', method, path, 200, raw,
                      'sha256:' + hashlib.sha256(raw).hexdigest(), replay,
                      action_bytes(args) if args is not None else None)

    publish = rec('publish-shelters-submit', 'seed', 'http', 'POST', '/api/v1/operations/service.publish/submit',
                  {'data': {'operationId': 'service.publish', 'status': 'Completed',
                            'resourceIds': {'serviceName': 'p10_abc-shelters', 'storageLayerId': '3',
                                            'serviceLayerIndex': '3'}}})
    roles = projections.resolve_roles(load_roles('P10'), {'publish-shelters-submit': publish}, agent_principal='a')
    assert roles == {'seeded:service:shelters': 'p10_abc-shelters', 'seeded:layer:shelters': '3'}

    conflicting = rec('publish-shelters-submit', 'seed', 'http', 'POST', '/api/v1/operations/service.publish/submit',
                      {'data': {'resourceIds': {'serviceName': 'p10_abc-shelters', 'storageLayerId': '3',
                                                'layerId': '4'}}})
    with pytest.raises(ProjectionError, match='conflicting'):
        projections.resolve_roles(load_roles('P10'), {'publish-shelters-submit': conflicting}, agent_principal='a')

    source_after = rec('p10-source-after', 'observe', 'http', 'GET',
                       '/rest/services/p10_abc-shelters/FeatureServer/3/query?f=json&where=1%3D1',
                       {'objectIdFieldName': 'id', 'exceededTransferLimit': False, 'features': [
                           {'attributes': {'id': 1, 'source_id': 's101', 'name': 'A'}},
                           {'attributes': {'id': 2, 'source_id': 's102', 'name': 'B'}},
                           {'attributes': {'id': 4, 'source_id': 's104', 'name': 'C'}}]})
    query_args = {'serviceId': 'svc-publish-p10_abc-shelters', 'serviceLayerIndex': 3,
                  'where': 'is_open = true AND capacity > 50', 'outFields': ['id', 'name', 'capacity', 'is_open']}
    flat_reply = {'jsonrpc': '2.0', 'id': 1, 'result': {'isError': False, 'structuredContent': {
        'serviceId': 'svc-publish-p10_abc-shelters', 'serviceLayerIndex': 3, 'returnedCount': 2,
        'resultOffset': 0, 'exceededTransferLimit': False, 'features': [
            {'id': 2, 'attributes': {'id': 2, 'name': 'B'}},
            {'id': 4, 'attributes': {'id': 4, 'name': 'C'}}]}}}
    replay = rec('p10-replay-query', 'observe', 'mcp', 'POST', '/mcp#tools/call', flat_reply,
                 args={'tool': 'honua_query_features', 'arguments': query_args}, replay='occ:1')
    spec = {'featureQueryIds': 'p10-replay-query', 'service': {'role': 'seeded:service:shelters'},
            'layer': {'role': 'seeded:layer:shelters'}, 'oidSource': 'p10-source-after'}
    ctx = projections.Context(
        {'publish-shelters-submit': publish, 'p10-source-after': source_after, 'p10-replay-query': replay},
        roles=roles, model_actions={'occ:1': {'kind': 'tool_call', 'tool': 'honua_query_features',
                                              'arguments': query_args}})
    assert projections.evaluate(spec, ctx) == ['s102', 's104']

    # Fail closed on unknown OID or mismatched direct source_id vs OID.
    bad_oid = json.loads(json.dumps(flat_reply))
    bad_oid['result']['structuredContent']['features'][1]['id'] = 99
    bad_oid['result']['structuredContent']['features'][1]['attributes']['id'] = 99
    ctx_bad = projections.Context(
        {'p10-source-after': source_after,
         'p10-replay-query': rec('p10-replay-query', 'observe', 'mcp', 'POST', '/mcp#tools/call', bad_oid,
                                 args={'tool': 'honua_query_features', 'arguments': query_args}, replay='occ:1')},
        roles=roles, model_actions=ctx.model_actions)
    with pytest.raises(ProjectionError, match='absent from source layer'):
        projections.evaluate(spec, ctx_bad)

    mismatch = json.loads(json.dumps(flat_reply))
    mismatch['result']['structuredContent']['features'][0]['attributes']['source_id'] = 's101'
    mismatch['result']['structuredContent']['features'][1]['attributes']['source_id'] = 's104'
    ctx_mis = projections.Context(
        {'p10-source-after': source_after,
         'p10-replay-query': rec('p10-replay-query', 'observe', 'mcp', 'POST', '/mcp#tools/call', mismatch,
                                 args={'tool': 'honua_query_features', 'arguments': query_args}, replay='occ:1')},
        roles=roles, model_actions=ctx.model_actions)
    with pytest.raises(ProjectionError, match='disagrees with source_id'):
        projections.evaluate(spec, ctx_mis)
