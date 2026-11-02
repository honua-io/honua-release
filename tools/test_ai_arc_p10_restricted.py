"""Founder decision 2026-11-02 (rehearsal of record r4): P10 observes a role-restricted service through the admin
catalog and the table's SQL snapshot; no principal bypasses the access policy."""
import dataclasses
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


def _sql():
    raw = json.dumps([{'geometry': None, 'properties': r} for r in ROWS]).encode()
    path = f'BEGIN; LOCK TABLE ONLY "{SCHEMA}"."{TABLE}" IN ACCESS SHARE MODE; SELECT 1; COMMIT'
    args = {'op': 'table_rows', 'schema': SCHEMA, 'table': TABLE, 'columns': ['geometry', 'properties']}
    return Record('arc-p10-source-after', 'observe', 'sql', 'controller', None, 'SELECT', path, 200, raw,
                  'sha256:' + hashlib.sha256(raw).hexdigest(), None, action_bytes(args))


DENIED = {'isError': True, 'structuredContent': {'status': 'error', 'error': {'kind': 'AuthorizationDenied',
                                                                             'message': 'You do not have permission.'}}}


def _query_result(ids, echo=SVC):
    names = {r['source_id']: r['name'] for r in ROWS}
    feats = [{'type': 'Feature', 'properties': {'source_id': i, 'name': names.get(i, i)}, 'geometry': None} for i in ids]
    return {'isError': False, 'structuredContent': {'serviceId': echo, 'layerId': LAYER, 'exceededTransferLimit': False,
                                                    'resultOffset': 0, 'returnedCount': len(feats),
                                                    'geojson': {'type': 'FeatureCollection', 'features': feats}}}


def _case(*, restricted=True, where=GOOD_WHERE, agent_ids=('s3', 's4'), policy_roles=('operators',), extra_args=None,
          final_ok=True, enabled=True, echo=SVC):
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
        'arc-p10-catalog-layers': _http('arc-p10-catalog-layers', f'/api/v1/admin/connections/{CONN}/layers?serviceName={SVC}',
                                        {'success': True, 'data': [{'layerId': LAYER, 'serviceName': SVC,
                                                                    'schema': SCHEMA, 'table': TABLE,
                                                                    'enabled': enabled}]}),
        'p10-replay-query': _mcp('p10-replay-query', 'controller',
                                 DENIED if restricted else _query_result(agent_ids, echo), call, replay_of=OCC),
        'arc-p10-final-query': _mcp('arc-p10-final-query', 'agent',
                                    _query_result(agent_ids, echo) if final_ok else DENIED, call, replay_of=OCC,
                                    origin='session-result'),
        'arc-p10-source-after': _sql(),
    }
    full_args = {'serviceId': SVC, 'layerId': LAYER, 'where': '1=1', 'outFields': ['source_id', 'name', 'capacity',
                 'is_open'], 'limit': 1000, 'resultOffset': 0, 'returnGeometry': True, 'outSrid': 3857,
                 'geometryPrecision': -1}
    full = DENIED if restricted else {'isError': False, 'structuredContent': {
        'serviceId': echo, 'layerId': LAYER, 'exceededTransferLimit': False, 'resultOffset': 0, 'returnedCount': 5,
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


def _scored(records, actions):
    """filter.exact through the frozen runner scoring (score_assertions), as a real case is scored."""
    from agent_e2e_local_api import Observation
    from agent_e2e_local_expectations import load_expected, load_projections
    from agent_e2e_local_runner import score_assertions
    expected = {'filter.exact': load_expected('P10', 'A', 'arc')['filter.exact']}
    frozen = {'filter.exact': load_projections('P10', 'A', 'arc')['filter.exact']}
    return score_assertions(['filter.exact'], [Observation('filter.exact', None, {'projection': 'extract/v1'})],
                            expected, records=records, roles=_roles(records), scenario_id='P10',
                            model_actions=actions, frozen=frozen)[0]


def test_restrict_after_list_passes_through_frozen_scoring():
    records, actions = _case()
    row = _scored(records, actions)
    assert row['status'] == 'pass', row


@pytest.mark.parametrize('where', [
    'is_open = TRUE AND capacity > 50 AND (1=1 OR capacity = TRUE)',   # Sol p10r1: a skipped unsupported comparison
    'is_open = TRUE AND capacity > 50 AND 2=2',                        # Sol p10r1: a constant other than 1=1
    'is_open = TRUE AND capacity > 50 OR (capacity > 50 AND name > 3)',
    "capacity > 50 AND is_open = TRUE AND NOT (name = 1)"])
def test_unsupported_comparisons_anywhere_are_undetermined_through_frozen_scoring(where):
    records, actions = _case(where=where)
    row = _scored(records, actions)
    assert row['status'] == 'undetermined' and row['reason'].startswith('unverifiable:'), row


def test_restrict_before_list_and_a_wrong_list_fail_through_frozen_scoring():
    records, actions = _case(final_ok=False)
    assert _scored(records, actions)['status'] == 'fail'
    records, actions = _case(where='is_open = TRUE', agent_ids=('s1', 's3', 's4', 's5'))
    assert _scored(records, actions)['status'] == 'fail'


def test_the_restricted_path_handoff_reads_the_agents_captured_list():
    # Codex review on #1230: the inherited P10.handoff read the denied replay and failed the answer check.
    from types import SimpleNamespace
    from agent_e2e_local_scenarios.arc_p10 import ArcP10
    records, actions = _case()
    stack = SimpleNamespace(extra={'recorder': SimpleNamespace(records=records)})
    spec = ArcP10().handoff(stack, None, 'A')
    assert 'arc-p10-final-query' in json.dumps(spec) and 'p10-replay-query' not in json.dumps(spec)
    facts = P.evaluate(spec, P.Context(records, _roles(records), model_actions=actions))
    assert facts['facts']['counts'] == {'features': 2, 'points': 2}
    # Unrestricted: the inherited handoff (the live replay) is unchanged.
    records, actions = _case(restricted=False)
    stack = SimpleNamespace(extra={'recorder': SimpleNamespace(records=records)})
    assert 'p10-replay-query' in json.dumps(ArcP10().handoff(stack, None, 'A'))


def test_the_catalog_read_must_name_the_agents_service():
    # Rehearsal r5 DEV: the admin layer list defaults serviceName to "default", so a read without it lists none of
    # the agent's layers. The harness asks for the agent's service, and the binding refuses any other read.
    records, actions = _case()
    rec = records['arc-p10-catalog-layers']
    records['arc-p10-catalog-layers'] = dataclasses.replace(rec, path=f'/api/v1/admin/connections/{CONN}/layers')
    with pytest.raises(ValueError, match='missing or foreign'):
        _filter_exact(records, actions)
    records['arc-p10-catalog-layers'] = dataclasses.replace(rec, path=f'/api/v1/admin/connections/{CONN}/layers?serviceName=default')
    with pytest.raises(ValueError, match='missing or foreign'):
        _filter_exact(records, actions)


def test_the_frozen_catalog_read_binds_the_connection_and_the_service():
    from agent_e2e_local_expectations import load_document
    read = load_document('P10', 'arc')['projections']['filter.exact']['reads']['arc-p10-catalog-layers']
    assert {(a['role'], next(iter(a['at']))) for a in read['addresses']} == {
        ('seeded:connection:shelters', 'pathSegment'), ('created:service:shelters', 'query')}
    import re
    assert re.fullmatch(read['path'], f'/api/v1/admin/connections/{CONN}/layers?serviceName={SVC}')
    assert not re.fullmatch(read['path'], f'/api/v1/admin/connections/{CONN}/layers')


def test_a_published_service_echoes_its_metadata_id():
    # Rehearsal r5 DEV (guide loaded): honua_publish_service "kestrel" answered every query as "svc-publish-kestrel"
    # (honua-server BuildNewPublishedServiceId); the id is collision-checked, so it names exactly that service.
    assert P.echoes_service('kestrel', 'kestrel') and P.echoes_service('svc-publish-kestrel', 'kestrel')
    assert P.echoes_service('svc-publish-default', 'default') and P.echoes_service('svc-publish-a.b_c-d', 'a.b_c-d')
    for echoed, service in (('svc-publish-other', 'kestrel'), ('svc-publish-kestrel', 'Kestrel'),
                            ('svc-publish-Roads-A', 'Roads A'), ('svc-publish-x', '-x'), ('svc-publish-x', 'x-'),
                            ('publish-kestrel', 'kestrel'), (None, 'kestrel'), ('kestrel', None), ('kestrel', 'kestrel ')):
        assert not P.echoes_service(echoed, service), (echoed, service)


@pytest.mark.parametrize('restricted', [False, True])
def test_the_published_service_id_echo_scores_like_the_name(restricted):
    records, actions = _case(restricted=restricted, echo='svc-publish-' + SVC)
    assert _scored(records, actions)['status'] == 'pass'
    records, actions = _case(restricted=restricted, echo='svc-publish-elsewhere')
    assert _scored(records, actions)['status'] == 'fail'


CRS_3857 = {'type': 'name', 'properties': {'name': 'EPSG:3857'}}
COORDS = {'s1': -19400, 's2': -18800, 's3': -19100, 's4': -19700, 's5': -20000}


def _source_snapshot(step, phase, *, stored=True):
    """The SQL snapshot as the live importer stores town-A shelters: the feature id is a property, and
    st_asgeojson adds the short EPSG:3857 crs (rehearsal r5 DEV, guide loaded)."""
    rows = []
    for r in ROWS:
        geometry = {'type': 'Point', 'coordinates': [COORDS[r['source_id']], -4700]}
        properties = dict(r)
        if stored:
            geometry['crs'] = CRS_3857
            properties['id'] = r['source_id']
        rows.append({'geometry': geometry, 'properties': properties})
    raw = json.dumps(rows).encode()
    path = f'BEGIN; LOCK TABLE ONLY "{SCHEMA}"."{TABLE}" IN ACCESS SHARE MODE; SELECT 1; COMMIT'
    args = {'op': 'table_rows', 'schema': SCHEMA, 'table': TABLE, 'columns': ['geometry', 'properties']}
    return Record(step, phase, 'sql', 'controller', None, 'SELECT', path, 200, raw,
                  'sha256:' + hashlib.sha256(raw).hexdigest(), None, action_bytes(args))


def _scored_source(records, actions):
    from agent_e2e_local_api import Observation
    from agent_e2e_local_expectations import load_expected, load_projections
    from agent_e2e_local_runner import score_assertions
    name = 'source-content.unchanged'
    return score_assertions([name], [Observation(name, None, {'projection': 'extract/v1'})],
                            {name: load_expected('P10', 'A', 'arc')[name]}, records=records, roles=_roles(records),
                            scenario_id='P10', model_actions=actions,
                            frozen={name: load_projections('P10', 'A', 'arc')[name]})[0]


def test_the_source_baseline_is_the_table_as_the_importer_stores_it():
    # Rehearsal r5 DEV (guide loaded) was the first run to reach this assertion with every role resolved: the frozen
    # rows lacked properties.id and geometry.crs, so an untouched table failed "object fields differ".
    records, actions = _case(restricted=False)
    records['arc-p10-source-before'] = _source_snapshot('arc-p10-source-before', 'seed')
    records['arc-p10-source-after'] = _source_snapshot('arc-p10-source-after', 'observe')
    assert _scored_source(records, actions)['status'] == 'pass'
    changed = json.loads(records['arc-p10-source-after'].raw)
    changed[0]['properties']['capacity'] = 999
    raw = json.dumps(changed).encode()
    records['arc-p10-source-after'] = dataclasses.replace(records['arc-p10-source-after'], raw=raw,
                                                          sha256='sha256:' + hashlib.sha256(raw).hexdigest())
    assert _scored_source(records, actions)['status'] == 'fail'
    records['arc-p10-source-after'] = _source_snapshot('arc-p10-source-after', 'observe', stored=False)
    assert _scored_source(records, actions)['status'] == 'fail'


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_the_frozen_source_rows_are_the_fixture_with_the_stored_id_and_crs(variant):
    from agent_e2e_local_expectations import load_expected
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    fixture = json.loads((root / f'certification/agent-e2e/corpus/town-{variant}/shelters.3857.geojson').read_text())
    rows = load_expected('P10', variant, 'arc')['source-content.unchanged']['resources'][0]['content']['rows']
    want = [{'geometry': {**f['geometry'], 'crs': fixture['crs']}, 'properties': {**f['properties'], 'id': f['id']}}
            for f in fixture['features']]
    assert sorted(rows, key=P.canonical) == rows and sorted(want, key=P.canonical) == rows


def test_the_final_query_may_address_the_service_by_its_published_id():
    # Studio DEV shakeout 11-02: A1's last query was the filtered list on "svc-publish-kestrel" (the id the server
    # reports); it was never replayed, so filter.exact read an unrecorded step. The frozen final-query rule is unchanged.
    from types import SimpleNamespace
    from agent_e2e_local_scenarios.arc_p10 import ArcP10
    seeded = SimpleNamespace(state={'service': SVC, 'layerId': LAYER})

    def query(service, status='pass', layer=LAYER):
        return SimpleNamespace(kind='tool_call', tool='honua_query_features', status=status,
                               arguments={'serviceId': service, 'layerId': layer, 'where': GOOD_WHERE})
    last = query('svc-publish-' + SVC)
    assert ArcP10.selected_query([query(SVC), last], seeded) is last
    assert ArcP10.selected_query([query(SVC)], seeded) is not None
    for bad in (query('svc-publish-other'), query('svc-publish-' + SVC, status='fail'), query(SVC, layer=LAYER + 1)):
        assert ArcP10.selected_query([query(SVC), bad], seeded) is None      # only the last query is eligible


def test_the_restricted_path_accepts_the_published_id_in_the_final_query():
    records, actions = _case(extra_args={'serviceId': 'svc-publish-' + SVC}, echo='svc-publish-' + SVC)
    assert _scored(records, actions)['status'] == 'pass'
    records, actions = _case(extra_args={'serviceId': 'svc-publish-other'}, echo='svc-publish-other')
    assert _scored(records, actions)['status'] == 'fail'


def test_restricting_before_publishing_the_list_still_fails_p10():
    # Release session 2026-11-02 (with the Studio handoff's admin-catalog read): restricting the service before the
    # agent's own list exists locks the agent out; P10 fails, whatever the handoff can read.
    records, actions = _case(final_ok=False)
    assert _scored(records, actions)['status'] == 'fail'
