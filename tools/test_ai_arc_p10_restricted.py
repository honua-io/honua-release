"""Founder decision 2026-11-02 (rehearsal of record r4): P10 observes a role-restricted service through the admin
catalog and the table's SQL snapshot; no principal bypasses the access policy."""
import copy
import hashlib
import json

import pytest

from agent_e2e_local_recorder import Record, Unverifiable, action_bytes
import agent_e2e_local_projections as P

TABLE, SCHEMA, CONN, SVC, LAYER = ('imported_arc10_abc_shelters', 'honua_data',
                                   '7882ed79-1e53-43d6-b2c6-bc5b35be8e55', 'kestrel', 1)
ROWS = [  # town-A shelters (arc-P10.json variant A protected content)
    {'source_id': 's1', 'name': 'Kestrel Central', 'is_open': True, 'capacity': None},
    {'source_id': 's2', 'name': 'Kestrel Central', 'is_open': False, 'capacity': 120},
    {'source_id': 's3', 'name': 'Hālau, "North" refuge', 'is_open': True, 'capacity': 80},
    {'source_id': 's4', 'name': 'Centre d’accueil', 'is_open': True, 'capacity': 75},
    {'source_id': 's5', 'name': 'Mālama refuge', 'is_open': True, 'capacity': 50}]
GOOD_WHERE = 'is_open = TRUE AND capacity > 50'
OCC = 'occ-final'


def _http(step, path, body, phase='observe', status=200):
    raw = json.dumps(body).encode()
    return Record(step, phase, 'http', 'controller', None, 'GET', path, status, raw,
                  'sha256:' + hashlib.sha256(raw).hexdigest())


def _mcp(step, principal, result, arguments, replay_of=None, origin='controller'):
    raw = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': result}).encode()
    return Record(step, 'observe', 'mcp', principal, None, 'POST', '/mcp#tools/call', 200, raw,
                  'sha256:' + hashlib.sha256(raw).hexdigest(), replay_of, action_bytes(arguments), origin)


DENIED = {'isError': True, 'structuredContent': {'status': 'error', 'error': {'kind': 'AuthorizationDenied',
                                                                             'message': 'You do not have permission.'}}}


def _query_result(ids):
    feats = [{'type': 'Feature', 'properties': {'source_id': i}, 'geometry': None} for i in ids]
    return {'isError': False, 'structuredContent': {'serviceId': SVC, 'layerId': LAYER, 'exceededTransferLimit': False,
                                                    'resultOffset': 0, 'returnedCount': len(feats),
                                                    'geojson': {'type': 'FeatureCollection', 'features': feats}}}


def _case(*, restricted=True, where=GOOD_WHERE, agent_ids=('s3', 's4'), policy_roles=('operators',), extra_args=None,
          final_ok=True, enabled=True):
    args = {'serviceId': SVC, 'layerId': LAYER, 'where': where, 'returnGeometry': False}
    args.update(extra_args or {})
    call = {'tool': 'honua_query_features', 'arguments': args}
    model_actions = {OCC: {'kind': 'tool_call', **call, '_session': 1, '_sequence': 7}}
    records = {
        'import-shelters': _http('import-shelters', '/api/v1/admin/import/upload',
                                 {'success': True, 'physicalTableName': TABLE, 'schema': SCHEMA}, phase='seed'),
        'before-tables-shelters': _http('before-tables-shelters', '/x', {'tables': []}, phase='seed'),
        'arc-p10-connection-create': _http('arc-p10-connection-create', '/api/v1/admin/connections/',
                                           {'data': {'connectionId': CONN}}, phase='seed'),
        'arc-p10-services-before': _http('arc-p10-services-before', '/api/v1/admin/services/',
                                         {'success': True, 'data': [{'serviceName': 'geoprocessing'}]}, phase='seed'),
        'arc-p10-services-after': _http('arc-p10-services-after', '/api/v1/admin/services/',
                                        {'success': True, 'data': [{'serviceName': 'geoprocessing'},
                                                                   {'serviceName': SVC}]}),
        'arc-p10-service-meta': _http('arc-p10-service-meta', f'/rest/services/{SVC}/FeatureServer?f=json',
                                      {'error': {'code': 403, 'message': 'Forbidden'}} if restricted else
                                      {'serviceName': SVC, 'layers': [{'id': LAYER, 'name': 'Kestrel Shelters'}]}),
        'arc-p10-service-settings': _http('arc-p10-service-settings', f'/api/v1/admin/services/{SVC}/settings',
                                          {'success': True, 'data': {'serviceName': SVC, 'accessPolicy': {
                                              'allowAnonymous': not policy_roles, 'allowedRoles': list(policy_roles)}}}),
        'arc-p10-catalog-layers': _http('arc-p10-catalog-layers', f'/api/v1/admin/connections/{CONN}/layers',
                                        {'success': True, 'data': [{'layerId': LAYER, 'serviceName': SVC,
                                                                    'schema': SCHEMA, 'table': TABLE,
                                                                    'enabled': enabled}]}),
        'p10-replay-query': _mcp('p10-replay-query', 'controller',
                                 DENIED if restricted else _query_result(agent_ids), call, replay_of=OCC),
        'arc-p10-final-query': _mcp('arc-p10-final-query', 'agent',
                                    _query_result(agent_ids) if final_ok else DENIED, call, replay_of=OCC,
                                    origin='session-result'),
        'arc-p10-source-after': Record('arc-p10-source-after', 'observe', 'sql', 'controller', None, 'SELECT',
                                       'BEGIN; ...', 200,
                                       json.dumps([{'geometry': None, 'properties': r} for r in ROWS]).encode(),
                                       'sha256:x'),
    }
    full_args = {'serviceId': SVC, 'layerId': LAYER, 'where': '1=1', 'outFields': ['source_id', 'name', 'capacity',
                 'is_open'], 'limit': 1000, 'resultOffset': 0, 'returnGeometry': True, 'outSrid': 3857,
                 'geometryPrecision': -1}
    full = DENIED if restricted else {'isError': False, 'structuredContent': {
        'serviceId': SVC, 'layerId': LAYER, 'exceededTransferLimit': False, 'resultOffset': 0, 'returnedCount': 5,
        'geojson': {'type': 'FeatureCollection', 'features': [{'geometry': None, 'properties': r} for r in ROWS]}}}
    records['arc-p10-published-full'] = _mcp('arc-p10-published-full', 'controller', full,
                                             {'tool': 'honua_query_features', 'arguments': full_args})
    return records, model_actions


def _roles(records):
    from agent_e2e_local_expectations import load_roles
    bindings = [b for b in load_roles('P10', 'arc') if b.role in (
        'seeded:table:shelters', 'seeded:schema:shelters', 'seeded:connection:shelters', 'created:service:shelters',
        'child:layer:shelters')]
    pub = {'tool': 'honua_publish_service', 'kind': 'tool_call', 'status': 'pass',
           'arguments': {'connectionId': CONN, 'schema': SCHEMA, 'table': TABLE, 'serviceName': SVC},
           'json': {'jsonrpc': '2.0', 'id': '1', 'result': {'isError': False, 'structuredContent': {
               'status': 'Completed', 'operationId': 'service.publish', 'serviceName': SVC, 'layerId': '1'}}}}
    return P.resolve_roles(bindings, records, agent_principal='agent', action_results=[pub])


def _filter_exact(records, model_actions):
    from agent_e2e_local_expectations import load_document
    import importlib.util, pathlib
    spec = load_document('P10', 'arc')['projections']['filter.exact']['spec']
    roles = _roles(records)
    ctx = P.Context(records, roles, model_actions=model_actions)
    observed = P.evaluate(spec, ctx)
    path = pathlib.Path(__file__).resolve().parents[1] / 'certification/agent-e2e/corpus/oracle.py'
    module = importlib.util.spec_from_file_location('arc_oracle', path)
    oracle = importlib.util.module_from_spec(module)
    module.loader.exec_module(oracle)
    oracle.verify('filter.exact', observed, oracle.fixture_expectation('A', 'filter.exact'))
    return observed, roles


def test_restrict_after_list_passes_through_the_catalog_and_the_sql_snapshot():
    records, actions = _case()
    observed, roles = _filter_exact(records, actions)
    assert observed == {'resourceIds': ['s3', 's4']}
    assert roles['child:layer:shelters'] == LAYER                # bound through the admin catalog


def test_restrict_before_list_fails_because_there_is_no_list():
    records, actions = _case(final_ok=False)
    with pytest.raises(ValueError, match='feature query failed'):
        _filter_exact(records, actions)


def test_a_wrong_list_fails():
    records, actions = _case(where='is_open = TRUE', agent_ids=('s1', 's3', 's4', 's5'))
    with pytest.raises(Exception) as failure:
        _filter_exact(records, actions)
    assert not isinstance(failure.value, Unverifiable)          # a real, translated mismatch: a model failure


@pytest.mark.parametrize('where', ["name LIKE 'Kestrel%'", 'capacity IN (75, 80)', 'UPPER(name) = \'X\'',
                                   'capacity BETWEEN 51 AND 1000', 'is_open = 1', 'unknown_field > 3'])
def test_an_untranslatable_where_clause_is_unverifiable(where):
    records, actions = _case(where=where)
    with pytest.raises(Unverifiable):
        _filter_exact(records, actions)


def test_untranslated_arguments_or_a_disagreeing_translation_are_unverifiable():
    records, actions = _case(extra_args={'bbox': [0, 0, 1, 1]})
    with pytest.raises(Unverifiable):
        _filter_exact(records, actions)
    records, actions = _case(agent_ids=('s3',))                   # the agent saw something the snapshot disagrees with
    with pytest.raises(Unverifiable):
        _filter_exact(records, actions)


def test_the_restricted_path_needs_a_role_restricted_policy_and_an_enabled_catalog_layer():
    records, actions = _case(policy_roles=())                     # denied, but not by a role policy: no alternative
    with pytest.raises(ValueError):
        _filter_exact(records, actions)
    records, actions = _case(enabled=False)
    with pytest.raises(ValueError, match='disabled'):
        _filter_exact(records, actions)


def test_the_unrestricted_path_is_unchanged():
    records, actions = _case(restricted=False)
    observed, roles = _filter_exact(records, actions)
    assert observed == {'resourceIds': ['s3', 's4']} and roles['child:layer:shelters'] == LAYER
    records, actions = _case(restricted=False, agent_ids=('s3',))  # the live replay decides, as before
    with pytest.raises(Exception):
        _filter_exact(records, actions)


@pytest.mark.parametrize('where,expected', [
    ('is_open = TRUE AND capacity > 50', ['s3', 's4']),
    ('capacity > 50 AND is_open = true', ['s3', 's4']),
    ('(is_open = TRUE) AND (capacity >= 51)', ['s3', 's4']),
    ('is_open = TRUE AND capacity > 50 AND capacity IS NOT NULL', ['s3', 's4']),
    ('NOT (capacity <= 50) AND is_open = TRUE', ['s3', 's4']),          # NULL capacity stays unknown, not selected
    ('capacity > 50 OR capacity IS NULL', ['s1', 's2', 's3', 's4']),
    ("name = 'Kestrel Central'", ['s1', 's2']),
    ('1=1', ['s1', 's2', 's3', 's4', 's5'])])
def test_where_translation_is_sql_three_valued(where, expected):
    predicate = P.where_predicate(where, {'source_id', 'name', 'is_open', 'capacity'})
    assert sorted(r['source_id'] for r in ROWS if predicate(r)) == expected


def test_only_the_reviewed_alternative_and_restricted_projection_are_accepted():
    from agent_e2e_local_expectations import load_document, load_roles
    records, actions = _case()
    bindings = load_roles('P10', 'arc')
    child = next(b for b in bindings if b.role == 'child:layer:shelters')
    assert child.alternative == P.ARC_P10_LAYER_ALTERNATIVE
    spec = copy.deepcopy(load_document('P10', 'arc')['projections']['filter.exact']['spec'])
    spec['object']['resourceIds']['restricted']['catalog'] = 'other-step'
    with pytest.raises(ValueError, match='unreviewed restricted-service projection'):
        P.evaluate(spec, P.Context(records, _roles(records), model_actions=actions))
