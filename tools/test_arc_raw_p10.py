"""Raw-input P10 selects only actual agent publications and the final query."""
from types import SimpleNamespace

from agent_e2e_local_api import Seeded
from agent_e2e_local_recorder import LoggedAction
from agent_e2e_local_scenarios.arc_p10 import ArcP10


def action(n, tool, arguments, status='pass'):
    return LoggedAction(1, n, 'sha256:' + str(n), 'tool_call', tool, arguments, None, status, '/x')


class Recorder:
    def __init__(self, actions, *, publication_error=False):
        self.log, self.calls, self.publication_error = actions, [], publication_error
    def actions(self): return self.log
    def table_rows(self, step, schema, table, columns):
        self.calls.append(('sql', step, schema, table, columns))
    def json(self, step, method, path):
        self.calls.append(('http', step, method, path))
        body = {'data': [{'serviceName': 'agent-service'}]} if step.endswith('services-after') else {
            'serviceName': 'agent-service', 'layers': [{'id': 2, 'name': 'shelters'}]}
        return {'observation': {'body': body}}
    def action_result(self, step, selected):
        self.calls.append(('action-result', selected.sequence))
        return SimpleNamespace(json={'result': {'isError': self.publication_error, 'structuredContent': {'status': 'Completed',
            'operationId': 'service.publish', 'serviceName': 'agent-service', 'layerId': '2'}}})
    def replay(self, step, selected): self.calls.append(('replay', step, selected.sequence))
    def mcp_call(self, step, tool, arguments): self.calls.append(('mcp', step, tool, arguments))


def test_raw_seed_imports_without_publishing(monkeypatch):
    import agent_e2e_local_scenarios.w2g3_common as common
    calls = []
    class Town:
        def prepare(self, **kwargs):
            calls.append(kwargs)
            return {'layers': {'shelters': {'schema': 'honua_data', 'table': 'arc_shelters'}}}
    monkeypatch.setattr(common, 'town_setup', lambda *_: Town())
    rec = Recorder([])
    stack = SimpleNamespace(compose_project='arc-test-p10', extra={'recorder': rec, 'ensureConnection': lambda: 'conn'})
    seeded = ArcP10().seed(stack, 'A')
    assert calls[0]['starting_state'] == 'imported-town'
    assert seeded.state['table'] == 'arc_shelters'
    assert not any('publish' in str(call) for call in rec.calls)


def test_only_matching_agent_publication_and_last_query_are_replayed():
    publish = action(1, 'honua_publish_service', {'schema': 'honua_data', 'table': 'arc_shelters',
                                                    'connectionId': 'conn'})
    query = action(2, 'honua_query_features', {'serviceId': 'agent-service', 'layerId': 2})
    rec = Recorder([publish, query])
    state = Seeded({'variant': 'A', 'schema': 'honua_data', 'table': 'arc_shelters', 'connection': 'conn'})
    rows = ArcP10().observe(SimpleNamespace(extra={'recorder': rec}), state, 'A')
    assert [r.assertion_id for r in rows] == ['filter.exact', 'source-content.unchanged']
    assert ('replay', 'p10-replay-query', 2) in rec.calls
    assert state.state['service'] == 'agent-service'


def test_foreign_table_publication_never_selects_a_query():
    publish = action(1, 'honua_publish_service', {'schema': 'foreign', 'table': 'arc_shelters',
                                                    'connectionId': 'conn'})
    query = action(2, 'honua_query_features', {'serviceId': 'agent-service', 'layerId': 2})
    rec = Recorder([publish, query])
    state = Seeded({'variant': 'A', 'schema': 'honua_data', 'table': 'arc_shelters', 'connection': 'conn'})
    ArcP10().observe(SimpleNamespace(extra={'recorder': rec}), state, 'A')
    assert not any(call[0] == 'replay' for call in rec.calls)


def test_error_publication_result_never_selects_a_query():
    publish = action(1, 'honua_publish_service', {'schema': 'honua_data', 'table': 'arc_shelters',
                                                    'connectionId': 'conn'})
    query = action(2, 'honua_query_features', {'serviceId': 'agent-service', 'layerId': 2})
    rec = Recorder([publish, query], publication_error=True)
    state = Seeded({'variant': 'A', 'schema': 'honua_data', 'table': 'arc_shelters', 'connection': 'conn'})
    ArcP10().observe(SimpleNamespace(extra={'recorder': rec}), state, 'A')
    assert not any(call[0] == 'replay' for call in rec.calls)


def test_frozen_raw_projection_requires_agent_created_service_and_fixture_rows():
    import hashlib
    import json
    import agent_e2e_local_projections as projections
    from agent_e2e_local_api import Observation
    from agent_e2e_local_expectations import load_expected, load_projections, load_roles
    from agent_e2e_local_recorder import Record, action_bytes
    from agent_e2e_local_runner import score_assertions

    def record(step, phase, kind, method, path, value, *, args=None, replay=None):
        raw = json.dumps(value, separators=(',', ':')).encode()
        return Record(step, phase, kind, 'controller', 'controller-id', method, path, 200, raw,
                      'sha256:' + hashlib.sha256(raw).hexdigest(), replay,
                      action_bytes(args) if args is not None else None)

    expected = load_expected('P10', 'A', 'arc')
    rows = expected['source-content.unchanged']['resources'][0]['content']['rows']
    sql_args = {'op': 'table_rows', 'schema': 'honua_data', 'table': 'arc_shelters',
                'columns': ['geometry', 'properties']}
    sql = 'BEGIN; LOCK TABLE ONLY "honua_data"."arc_shelters" IN ACCESS SHARE MODE; SELECT 1; COMMIT'
    query_args = {'serviceId': 'agent-service', 'layerId': 2}
    ids = expected['filter.exact']['resourceIds']
    query_result = {'jsonrpc': '2.0', 'id': 1, 'result': {'structuredContent': {
        'serviceId': 'agent-service', 'layerId': 2, 'exceededTransferLimit': False, 'resultOffset': 0,
        'returnedCount': len(ids), 'geojson': {'type': 'FeatureCollection', 'features': [
            {'properties': {'source_id': identity}} for identity in ids]}}}}
    full_args = {'serviceId': 'agent-service', 'layerId': 2, 'where': '1=1', 'outFields': ['*'],
                 'limit': 1000, 'resultOffset': 0, 'returnGeometry': True, 'outSrid': 3857,
                 'geometryPrecision': -1}
    full_result = {'jsonrpc': '2.0', 'id': 2, 'result': {'structuredContent': {
        'serviceId': 'agent-service', 'layerId': 2, 'exceededTransferLimit': False, 'resultOffset': 0,
        'returnedCount': len(rows), 'geojson': {'type': 'FeatureCollection', 'features': rows}}}}
    records = {
        'import-shelters': record('import-shelters', 'seed', 'http', 'POST', '/api/v1/admin/import/upload',
                                  {'physicalTableName': 'arc_shelters', 'schema': 'honua_data'}),
        'arc-p10-connection-create': record('arc-p10-connection-create', 'seed', 'http', 'POST',
                                             '/api/v1/admin/connections/', {'data': {'connectionId': 'conn'}}),
        'arc-p10-services-before': record('arc-p10-services-before', 'seed', 'http', 'GET',
                                         '/api/v1/admin/services/', {'data': []}),
        'arc-p10-services-after': record('arc-p10-services-after', 'observe', 'http', 'GET',
                                        '/api/v1/admin/services/', {'data': [{'serviceName': 'agent-service'}]}),
        'arc-p10-service-meta': record('arc-p10-service-meta', 'observe', 'http', 'GET',
                                      '/rest/services/agent-service/FeatureServer?f=json',
                                      {'serviceName': 'agent-service', 'layers': [{'id': 2}]}),
        'arc-p10-source-before': record('arc-p10-source-before', 'seed', 'sql', 'SELECT', sql, rows, args=sql_args),
        'arc-p10-source-after': record('arc-p10-source-after', 'observe', 'sql', 'SELECT', sql, rows, args=sql_args),
        'p10-replay-query': record('p10-replay-query', 'observe', 'mcp', 'POST', '/mcp#tools/call', query_result,
                                   args={'tool': 'honua_query_features', 'arguments': query_args}, replay='occ'),
        'arc-p10-published-full': record('arc-p10-published-full', 'observe', 'mcp', 'POST', '/mcp#tools/call',
                                        full_result, args={'tool': 'honua_query_features', 'arguments': full_args}),
    }
    publication = {'tool': 'honua_publish_service', 'status': 'pass',
                   'arguments': {'table': 'arc_shelters', 'schema': 'honua_data', 'connectionId': 'conn'},
                   'json': {'result': {'structuredContent': {
                       'serviceName': 'agent-service'}}}}
    roles = projections.resolve_roles(load_roles('P10', 'arc'), records, agent_principal='agent-id',
                                      action_results=[publication])
    observations = [Observation(name, None, {'projection': 'extract/v1'}) for name in expected]
    model_actions = {'occ': {'kind': 'tool_call', 'tool': 'honua_query_features', 'arguments': query_args}}
    scored = score_assertions(list(expected), observations, expected, records=records, roles=roles,
                              scenario_id='P10', model_actions=model_actions,
                              frozen=load_projections('P10', 'A', 'arc'))
    assert [row['status'] for row in scored] == ['pass', 'pass'], scored
    full_result['result']['structuredContent']['geojson']['features'][0]['geometry']['coordinates'] = [
        float(x) for x in rows[0]['geometry']['coordinates']]
    for feature in full_result['result']['structuredContent']['geojson']['features']:
        if type(feature['properties'].get('capacity')) is int:
            feature['properties']['capacity'] = float(feature['properties']['capacity'])
            break
    records['arc-p10-published-full'] = record('arc-p10-published-full', 'observe', 'mcp', 'POST',
                                               '/mcp#tools/call', full_result,
                                               args={'tool': 'honua_query_features', 'arguments': full_args})
    equivalent = score_assertions(list(expected), observations, expected, records=records, roles=roles,
                                  scenario_id='P10', model_actions=model_actions,
                                  frozen=load_projections('P10', 'A', 'arc'))
    assert equivalent[0]['status'] == 'pass', equivalent
    full_result['result']['structuredContent']['geojson']['features'][0]['properties']['is_open'] = 1
    records['arc-p10-published-full'] = record('arc-p10-published-full', 'observe', 'mcp', 'POST',
                                               '/mcp#tools/call', full_result,
                                               args={'tool': 'honua_query_features', 'arguments': full_args})
    wrong_type = score_assertions(list(expected), observations, expected, records=records, roles=roles,
                                  scenario_id='P10', model_actions=model_actions,
                                  frozen=load_projections('P10', 'A', 'arc'))
    assert wrong_type[0]['status'] == 'fail'
    full_result['result']['structuredContent']['geojson']['features'] = []
    records['arc-p10-published-full'] = record('arc-p10-published-full', 'observe', 'mcp', 'POST',
                                               '/mcp#tools/call', full_result,
                                               args={'tool': 'honua_query_features', 'arguments': full_args})
    changed = score_assertions(list(expected), observations, expected, records=records, roles=roles,
                               scenario_id='P10', model_actions=model_actions,
                               frozen=load_projections('P10', 'A', 'arc'))
    assert changed[0]['status'] == 'fail'
    import pytest
    with pytest.raises(ValueError):
        projections.resolve_roles(load_roles('P10', 'arc'), records, agent_principal='agent-id', action_results=[])
    publication['arguments']['schema'] = 'foreign'
    with pytest.raises(ValueError):
        projections.resolve_roles(load_roles('P10', 'arc'), records, agent_principal='agent-id',
                                  action_results=[publication])


def test_raw_p10_fault_fires_once_after_publication():
    from agent_e2e_local_faults import FaultInjector
    injector = FaultInjector(ArcP10().fault_plan('A'))
    publish = {'tool': 'honua_publish_service', 'arguments': {}}
    query = {'tool': 'honua_query_features', 'arguments': {}}
    assert injector.intercept('tool_call', query) is None
    injector.observe('tool_call', publish, {'status': 'ok'})
    event = injector.intercept('tool_call', query)
    assert event and event['kind'] == 'http_503_once' and event['httpStatus'] == 503
    assert injector.intercept('tool_call', query) is None
