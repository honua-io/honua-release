"""A/B-only tests of unsupported cases, fixture expectations and fail-closed behavior."""
import copy
import importlib

import pytest

from agent_e2e_local_api import Stack
from agent_e2e_local_runner import load_scenario, oracle

IDS = ('P03', 'P04', 'P05', 'P06', 'P07')
ASSERTIONS = {
    'P03': {'connection.verified', 'catalog.exact', 'tenant.denied'},
    'P04': {'features.exact', 'geometry.crs', 'source-content.unchanged'},
    'P05': {'features.exact', 'csv.invalid-reported', 'geometry.crs', 'source-content.unchanged'},
    'P06': {'service.query', 'pagination.complete', 'service.write-denied', 'source-content.unchanged'},
    'P07': {'catalog.exact', 'tenant.denied'},
}


@pytest.mark.parametrize('scenario_id', IDS)
@pytest.mark.parametrize('variant', ['A', 'B'])
def test_runner_inventory_and_expectation_shape(scenario_id, variant):
    scenario = load_scenario(scenario_id)
    assert scenario.supported_variants == (('A', 'B') if scenario_id in ('P03', 'P04', 'P05', 'P07') else ())
    assert scenario.fault_plan(variant) is None
    assert set(scenario.expected(variant)) == ASSERTIONS[scenario_id]
    if scenario_id not in ('P03', 'P04', 'P05', 'P07'):
        assert 'unsupported' in importlib.import_module('agent_e2e_local_scenarios.' + scenario_id).__doc__.lower()


@pytest.mark.parametrize('scenario_id,layer', [('P03', 'parcels'), ('P04', 'flood_zones'),
                                               ('P05', 'shelters'), ('P06', 'survey_points'), ('P07', 'roads')])
@pytest.mark.parametrize('variant', ['A', 'B'])
def test_fixture_oracle_rejects_changed_feature(scenario_id, layer, variant):
    # Fixture oracle control only: no assertion that these scenarios observed a live result.
    expected = oracle.fixture_expectation(variant, 'features.exact', layer=layer)
    observed = copy.deepcopy(expected)
    oracle.verify('features.exact', observed, expected)
    observed['features'][0]['properties']['source_id'] = 'incorrect-source-id'
    with pytest.raises(oracle.VerificationError):
        oracle.verify('features.exact', observed, expected)


def test_p06_seed_includes_survey_and_is_idempotent(monkeypatch, tmp_path):
    import agent_e2e_scenario_operations as operations
    calls = []

    class Recorder:
        def json(self, step, method, path):
            calls.append(('read', step))
            return {'observation': {'httpStatus': 200, 'body': {'data': []}}}
        def upload(self, step, raw, **kwargs):
            calls.append(('upload', step, kwargs['table'], len(raw)))
            return {'fixture': True}
        def sql(self, step, query):
            import re
            if step.endswith('-table'):
                return [{'table_name': re.search(r"table_name = '([^']+)'", query).group(1)}]
            return [{'feature': True}]

    monkeypatch.setattr(operations, 'discover_tables', lambda *args: {})
    monkeypatch.setattr(operations, 'completed_import', lambda http, accepted, **kw:
                        {'table': kw['table'], 'schema': 'honua_data', 'featureCount': kw['feature_count']})
    stack = Stack('http://127.0.0.1:1', 'private', None, 'case-123', 'fake', str(tmp_path), 'sha256:fake',
                  {'recorder': Recorder(), 'ensureConnection': lambda: 'connection',
                   'addPrincipal': lambda name, grants: 'viewer'})
    scenario = load_scenario('P06')
    first = scenario.seed(stack, 'A')
    assert scenario.seed(stack, 'A') == first
    assert first.state['layers']['survey_points']['featureCount'] > 1
    assert len([call for call in calls if call[0] == 'upload']) == 4
    with pytest.raises(ValueError):
        scenario.seed(stack, 'B')


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_p07_catalog_rejects_missing_layer(variant):
    import hashlib
    import json
    from agent_e2e_local_api import Observation
    from agent_e2e_local_recorder import Record
    from agent_e2e_local_runner import frozen_projections, score_assertions

    scenario = load_scenario('P07')
    roles = {f'seeded:service:{name}': name for name in ('parcels', 'flood_zones', 'shelters', 'countries')}
    action = {'kind': 'tool_call', 'tool': 'honua_list_layers', 'arguments': {}}
    action_id = 'occ:1:1:sha256:' + 'a' * 64
    def score(names):
        response = {'jsonrpc': '2.0', 'id': 1, 'result': {'isError': False, 'structuredContent':
                    {'layers': [{'serviceName': name} for name in names]}}}
        raw = json.dumps(response).encode()
        record = Record('p07-catalog', 'observe', 'mcp', 'controller', 'admin', 'POST',
                        '/mcp#tools/call', 200, raw, 'sha256:' + hashlib.sha256(raw).hexdigest(),
                        replay_of=action_id, request_arguments_raw=json.dumps({'tool': action['tool'],
                        'arguments': action['arguments']}).encode())
        from dataclasses import replace
        original = replace(record, step='p07-agent-catalog', principal='agent', origin='session-result')
        observations = [Observation('catalog.exact', None, {'projection': 'extract/v1'})]
        return score_assertions(['catalog.exact', 'tenant.denied'], observations, scenario.expected(variant),
               records={'p07-catalog': record, 'p07-agent-catalog': original}, roles=roles, scenario_id='P07',
               model_actions={action_id: action}, frozen=frozen_projections(scenario, variant))
    assert [row['status'] for row in score(list(roles.values()))] == ['pass', 'excluded']
    assert [row['status'] for row in score(list(roles.values())[:-1])] == ['fail', 'excluded']


def test_p07_seed_is_idempotent_and_includes_countries(monkeypatch, tmp_path):
    import agent_e2e_scenario_operations as operations
    calls = []

    class Recorder:
        def upload(self, step, raw, **kwargs):
            calls.append(('upload', step, kwargs['srid']))
            return {'fixture': True}

    monkeypatch.setattr(operations, 'discover_tables', lambda *args: {})
    monkeypatch.setattr(operations, 'completed_import', lambda http, accepted, **kw:
                        {'table': kw['table'], 'schema': 'honua_data'})
    monkeypatch.setattr(operations, 'publication_request', lambda **kw: kw)
    def publish(http, request, *, step):
        calls.append(('publish', step, request['service']))
        return {'status': 'completed', 'service': request['service']}
    monkeypatch.setattr(operations, 'submit_publication', publish)
    stack = Stack('http://127.0.0.1:1', 'private', None, 'case-123', 'fake', str(tmp_path), 'sha256:fake',
                  {'recorder': Recorder(), 'ensureConnection': lambda: 'connection'})
    scenario = load_scenario('P07')
    first = scenario.seed(stack, 'A')
    assert scenario.seed(stack, 'A') == first
    assert set(first.state['services']) == {'parcels', 'flood_zones', 'shelters', 'countries'}
    assert len([x for x in calls if x[0] == 'upload']) == 4
    assert ('upload', 'p07-countries-import', 4326) in calls
    with pytest.raises(ValueError):
        scenario.seed(stack, 'B')


@pytest.mark.parametrize('scenario_id', ['P04', 'P05'])
@pytest.mark.parametrize('variant', ['A', 'B'])
def test_customer_files_are_frozen_for_the_selected_variant(scenario_id, variant):
    from agent_e2e_local_runner import frozen_customer_files
    files = frozen_customer_files(load_scenario(scenario_id), variant)
    assert len(files) == 2
    assert all(path.startswith('town-' + variant + '/') for path in files)


def test_p07_rejects_incomplete_catalog_response(tmp_path):
    from types import SimpleNamespace
    class Recorder:
        def __init__(self, incomplete): self.incomplete = incomplete
        def actions(self): return [SimpleNamespace(kind='tool_call', tool='honua_list_layers')]
        def replay(self, step, action):
            return {'isError': False, 'structuredContent': {'layers': [{'serviceName': 's'}],
                    'offset': 0, 'hasMore': self.incomplete, 'layerCount': 1, 'totalCount': 1}}
        def action_result(self, step, action):
            return SimpleNamespace(status=200, json={'result': self.replay(step, action)})
    scenario = load_scenario('P07')
    def observe(incomplete):
        stack = Stack('http://127.0.0.1:1', 'private', None, 'fake', 'fake', str(tmp_path), 'sha256:fake',
                      {'recorder': Recorder(incomplete)})
        return {row.assertion_id for row in scenario.observe(stack, None, 'A')}
    assert 'catalog.exact' in observe(False)
    assert 'catalog.exact' not in observe(True)


@pytest.mark.parametrize('missing', [False, True])
def test_p07_full_runner_with_fake_candidate(tmp_path, missing):
    import json
    from agent_e2e_local_api import Seeded
    from agent_e2e_local_runner import execute_case
    from agent_e2e_local_scenarios.P07 import P07
    from test_agent_e2e_local_runner import Candidate, FakeModel, FakeRuntime, DIGEST, serve

    class CatalogCandidate(Candidate):
        TOOLS = [{'name': 'honua_list_layers', 'description': 'List layers',
                  'inputSchema': {'type': 'object', 'properties': {}}}]
        def mcp(self, message, principal):
            if message.get('method') == 'tools/call' and message['params']['name'] == 'honua_list_layers':
                names = ['parcels', 'flood_zones', 'shelters', 'countries']
                if missing:
                    names.pop()
                return {'content': [], 'isError': False, 'structuredContent': {
                    'layers': [{'serviceName': name, 'name': name} for name in names],
                    'offset': 0, 'hasMore': False, 'layerCount': len(names), 'totalCount': len(names)}}
            return super().mcp(message, principal)

    class FakeP07(P07):
        def seed(self, stack, variant):
            for name in ('parcels', 'flood_zones', 'shelters', 'countries'):
                stack.extra['recorder'].json('p07-' + name.replace('_', '-') + '-publish-submit',
                    'POST', '/api/v1/operations/service.publish/submit', {'serviceName': name})
            return Seeded({'variant': variant})

    candidate = CatalogCandidate()
    server = serve(candidate)
    candidate.base_url = f'http://127.0.0.1:{server.server_address[1]}'
    try:
        actions = [json.dumps({'kind': 'tool_call', 'tool': 'honua_list_layers', 'arguments': {}}),
                   json.dumps({'kind': 'final', 'answer': 'The catalog is available.'})]
        model = FakeModel(actions)
        runtime = FakeRuntime(candidate.base_url, model, tmp_path)
        case = {'prompts': {'A': 'Which layers can I use?'},
                'assertions': ['catalog.exact', 'tenant.denied'], 'clarifications': []}
        receipt = execute_case(case_id='P07:A:local-docker', scenario=FakeP07(), variant='A',
                               case=case, candidate_digest=DIGEST, runtime=runtime)
        assert [row['status'] for row in receipt['assertions']] == [
            'fail' if missing else 'pass', 'excluded'], json.dumps(receipt, indent=2)
    finally:
        server.shutdown()
        server.server_close()


def test_p07_refuses_agent_catalog_that_differs_from_controller(tmp_path):
    from types import SimpleNamespace
    def response(names):
        return {'isError': False, 'structuredContent': {'layers': [{'serviceName': n} for n in names],
                'offset': 0, 'hasMore': False, 'layerCount': len(names), 'totalCount': len(names)}}
    class Recorder:
        def actions(self): return [SimpleNamespace(kind='tool_call', tool='honua_list_layers')]
        def replay(self, step, action): return response(['one', 'two'])
        def action_result(self, step, action):
            return SimpleNamespace(status=200, json={'result': response(['one'])})
    stack = Stack('http://127.0.0.1:1', 'private', None, 'fake', 'fake', str(tmp_path), 'sha256:fake',
                  {'recorder': Recorder()})
    assert [row.assertion_id for row in load_scenario('P07').observe(stack, None, 'A')] == ['tenant.denied']


def test_p03_records_the_same_unpaged_inventory_before_and_after(tmp_path, monkeypatch):
    from agent_e2e_local_api import Seeded
    from agent_e2e_local_scenarios.P06 import P06
    layers = {name: {'table': name, 'schema': 'honua_data'}
              for name in ('flood_zones', 'parcels', 'shelters', 'survey_points')}
    monkeypatch.setattr(P06, 'seed', lambda self, stack, variant: Seeded({'layers': layers}))
    calls = []
    class Recorder:
        def json(self, *args):
            calls.append(args)
            return {'observation': {'httpStatus': 200, 'body': {'data': []}}}
        def sql(self, step, query):
            name = step.removeprefix('p03-').removesuffix('-table').replace('-', '_')
            return [{'table_name': name}]
    stack = Stack('http://127.0.0.1:1', 'private', None, 'fake', 'fake', str(tmp_path), 'sha256:fake',
                  {'recorder': Recorder(), 'databaseHandoff': lambda name, schema: calls.append(('handoff', name, schema))})
    case = load_scenario('P03')
    seeded = case.seed(stack, 'A')
    assert case.seed(stack, 'A') == seeded
    assert [row.assertion_id for row in case.observe(stack, seeded, 'A')] == ['tenant.denied']
    assert calls == [('handoff', 'town', 'honua_data'),
                     ('p03-connections-before', 'GET', '/api/v1/admin/connections'),
                     ('p03-connections-after', 'GET', '/api/v1/admin/connections')]


def test_p05_retains_only_the_last_matching_import_report(tmp_path):
    from types import SimpleNamespace
    root = __import__('pathlib').Path(__file__).resolve().parents[2]
    source = (root / 'certification/agent-e2e/corpus/town-A/invalid-rows.csv').read_text()
    calls = []
    class Recorder:
        def actions(self):
            return [SimpleNamespace(kind='tool_call', tool='honua_ingest_dataset', sequence=1, arguments={'data': source}),
                    SimpleNamespace(kind='tool_call', tool='another_tool', sequence=2, arguments={}),
                    SimpleNamespace(kind='tool_call', tool='honua_ingest_dataset', sequence=3, arguments={'data': source})]
        def action_result(self, step, action):
            calls.append((step, action.sequence))
            return SimpleNamespace(status=200, json={'result': {'structuredContent': {'success': False}}})
        def sql(self, step, query): pass
    stack = Stack('http://127.0.0.1:1', 'private', None, 'fake', 'fake', str(tmp_path), 'sha256:fake',
                  {'recorder': Recorder()})
    case = load_scenario('P05')
    from agent_e2e_local_api import Seeded
    assert [item.assertion_id for item in case.observe(stack, Seeded({'variant': 'A'}), 'A')] == ['features.exact', 'csv.invalid-reported', 'geometry.crs', 'source-content.unchanged']
    assert calls == [('p05-invalid-report', 3)]


def test_core_role_binding_rejects_two_services_from_same_tool():
    import hashlib
    import json
    from agent_e2e_local_api import RoleBinding
    from agent_e2e_local_projections import resolve_roles
    from agent_e2e_local_recorder import Record

    def record(step, phase, rows):
        raw = json.dumps({'data': rows, 'next': None}).encode()
        return Record(step, phase, 'http', 'controller', 'admin', 'GET', '/api/v1/admin/services', 200,
                      raw, 'sha256:' + hashlib.sha256(raw).hexdigest())
    records = {'before': record('before', 'seed', []),
               'after': record('after', 'observe', [{'name': 'parcels'}, {'name': 'shelters'}])}
    binding = RoleBinding('created:service:parcels', 'after', items='/data', id='/name',
                          before='before',
                          created_by={'tool': 'honua_publish_service', 'pointer': '/result/structuredContent/serviceName'})
    actions = [{'tool': 'honua_publish_service', 'status': 'pass',
                'json': {'result': {'structuredContent': {'serviceName': name}}}}
               for name in ('parcels', 'shelters')]
    with pytest.raises(ValueError, match='matched 2'):
        resolve_roles([binding], records, agent_principal='agent', action_results=actions)


def test_core_connection_binding_uses_unpaged_inventory_and_owner():
    import hashlib
    import json
    from agent_e2e_local_api import RoleBinding
    from agent_e2e_local_projections import resolve_roles
    from agent_e2e_local_recorder import Record

    def record(step, phase, rows):
        raw = json.dumps({'data': rows}).encode()
        return Record(step, phase, 'http', 'controller', 'admin', 'GET', '/api/v1/admin/connections',
                      200, raw, 'sha256:' + hashlib.sha256(raw).hexdigest())
    binding = RoleBinding('created:connection', 'after', items='/data', id='/connectionId',
                          owner='/createdBy', before='before')
    before = record('before', 'seed', [])
    records = {'before': before, 'after': record('after', 'observe', [
        {'connectionId': 'owned', 'createdBy': 'agent'}])}
    assert resolve_roles([binding], records, agent_principal='agent') == {'created:connection': 'owned'}
    records['after'] = record('after', 'observe', [{'connectionId': 'foreign', 'createdBy': 'other'}])
    with pytest.raises(ValueError, match='matched 0'):
        resolve_roles([binding], records, agent_principal='agent')


@pytest.mark.parametrize('scenario_id', ['P04', 'P05'])
@pytest.mark.parametrize('variant', ['A', 'B'])
def test_attachment_snapshot_rejects_changed_content(scenario_id, variant):
    import copy
    from agent_e2e_local_api import Observation
    from agent_e2e_local_runner import frozen_projections, score_assertions
    scenario = load_scenario(scenario_id)
    expected = scenario.expected(variant)
    rows = expected['source-content.unchanged']['resources']
    initial = {row['resourceId']: row['content']['sha256'] for row in rows}
    custody = {'customerFiles:before': initial, 'customerFiles:after': dict(initial)}
    observations = [Observation('source-content.unchanged', None, {'projection': 'extract/v1'})]
    def score(facts):
        return score_assertions(list(expected), observations, expected, records={}, custody=facts,
                                scenario_id=scenario_id, frozen=frozen_projections(scenario, variant))
    assert next(row for row in score(custody) if row['assertion'] == 'source-content.unchanged')['status'] == 'pass'
    wrong = copy.deepcopy(custody)
    wrong['customerFiles:after'][rows[0]['resourceId']] = 'sha256:' + '0' * 64
    assert next(row for row in score(wrong) if row['assertion'] == 'source-content.unchanged')['status'] == 'fail'


def test_p03_catalog_missing_dataset_fails_oracle():
    expected = {'resourceIds': ['flood_zones', 'parcels', 'shelters', 'survey_points']}
    oracle.verify('catalog.exact', {'resourceIds': list(expected['resourceIds'])}, expected)
    with pytest.raises(oracle.VerificationError):
        oracle.verify('catalog.exact', {'resourceIds': expected['resourceIds'][:-1]}, expected)


def test_p03_frozen_projection_rejects_missing_live_dataset():
    import hashlib
    import json
    from agent_e2e_local_api import Observation
    from agent_e2e_local_recorder import Record
    from agent_e2e_local_runner import frozen_projections, score_assertions
    scenario = load_scenario('P03')
    connection = '11111111-1111-1111-1111-111111111111'
    names = ['flood_zones', 'parcels', 'shelters', 'survey_points']
    roles = {'created:connection': connection, **{'seeded:table:' + name: name for name in names}}
    def record(step, method, path, body):
        raw = json.dumps(body).encode()
        return Record(step, 'observe', 'http', 'controller', 'admin', method, path, 200, raw,
                      'sha256:' + hashlib.sha256(raw).hexdigest())
    path = '/api/v1/admin/connections/' + connection
    probe = record('p03-connection-probe', 'POST', path + '/test', {'data': {'isHealthy': True}})
    def score(found):
        catalog = record('p03-catalog', 'GET', path + '/tables', {'tables': [{'table': name} for name in found]})
        observed = [Observation(name, None, {'projection': 'extract/v1'})
                    for name in ('connection.verified', 'catalog.exact', 'tenant.denied')]
        return score_assertions(list(scenario.expected('A')), observed, scenario.expected('A'),
            records={'p03-connection-probe': probe, 'p03-catalog': catalog}, roles=roles,
            scenario_id='P03', frozen=frozen_projections(scenario, 'A'))
    assert [row['status'] for row in score(names)] == ['pass', 'pass', 'excluded']
    assert [row['status'] for row in score(names[:-1])] == ['fail', 'fail', 'excluded']


def test_p05_reads_only_a_safe_table_named_by_the_sealed_report(tmp_path):
    from types import SimpleNamespace
    calls = []
    root = __import__('pathlib').Path(__file__).resolve().parents[2]
    source = (root / 'certification/agent-e2e/corpus/town-A/shelters.csv').read_text()
    class Recorder:
        def __init__(self, table): self.table = table
        def actions(self): return [SimpleNamespace(kind='tool_call', tool='honua_ingest_dataset', arguments={'data': source})]
        def action_result(self, step, action):
            return SimpleNamespace(status=200, json={'result': {'isError': False, 'structuredContent': {
                'success': True, 'schema': 'honua_data', 'table': self.table, 'geometryColumn': 'geom'}}})
        def sql(self, step, query):
            if step == 'p05-import-rows': calls.append((step, query))
    def observe(table):
        stack = Stack('http://127.0.0.1:1', 'private', None, 'fake', 'fake', str(tmp_path), 'sha256:fake',
                      {'recorder': Recorder(table)})
        case = load_scenario('P05')
        from agent_e2e_local_api import Seeded
        case.observe(stack, Seeded({'variant': 'A'}), 'A')
    observe('imported_shelters')
    assert len(calls) == 1 and 'FROM honua_data.imported_shelters' in calls[0][1]
    observe('unsafe;drop')
    assert len(calls) == 1


def test_p04_only_reads_a_table_from_the_matching_sealed_input(tmp_path):
    from types import SimpleNamespace
    root = __import__('pathlib').Path(__file__).resolve().parents[2]
    source = (root / 'certification/agent-e2e/corpus/town-A/parcels.4326.geojson').read_text()
    calls = []
    class Recorder:
        def actions(self):
            return [SimpleNamespace(kind='tool_call', tool='honua_ingest_dataset', arguments={'data': source})]
        def action_result(self, step, action):
            return SimpleNamespace(status=200, json={'result': {'isError': False, 'structuredContent': {
                'success': True, 'schema': 'honua_data', 'table': 'imported_parcels', 'geometryColumn': 'geom'}}})
        def sql(self, step, query):
            if step.endswith('-rows'): calls.append((step, query))
    stack = Stack('http://127.0.0.1:1', 'private', None, 'fake', 'fake', str(tmp_path), 'sha256:fake',
                  {'recorder': Recorder()})
    from agent_e2e_local_api import Seeded
    scenario = load_scenario('P04')
    assert [item.assertion_id for item in scenario.observe(stack, Seeded({'variant': 'A'}), 'A')] == ['features.exact', 'geometry.crs', 'source-content.unchanged']
    assert len(calls) == 1 and calls[0][0] == 'p04-parcels-rows'
    assert 'FROM honua_data.imported_parcels' in calls[0][1]


def test_p03_checks_server_api_key_owner_before_probes(tmp_path):
    from types import SimpleNamespace
    from agent_e2e_local_api import Seeded
    key = '11111111-1111-1111-1111-111111111111'
    calls = []
    class Recorder:
        def json(self, step, method, path):
            calls.append(step)
            if step == 'p03-connections-after':
                body = {'data': [{'connectionId': key, 'createdBy': 'api-key:agent'}]}
            elif step == 'p03-connection-probe':
                body = {'data': {'isHealthy': True}}
            else:
                body = {'tables': []}
            return {'observation': {'httpStatus': 200, 'body': body}}
    stack = Stack('http://127.0.0.1:1', 'private', None, 'fake', 'fake', str(tmp_path), 'sha256:fake',
                  {'recorder': Recorder()})
    rows = load_scenario('P03').observe(stack, Seeded({'baseline': []}), 'A', SimpleNamespace(agent_principal='agent'))
    assert {row.assertion_id for row in rows} == {'tenant.denied', 'connection.verified', 'catalog.exact'}
    assert calls == ['p03-connections-after', 'p03-connection-probe', 'p03-catalog']


def test_p06_records_agent_publications_for_later_role_binding(tmp_path):
    from types import SimpleNamespace
    from agent_e2e_local_api import Seeded
    calls = []
    class Recorder:
        def sql(self, step, query): calls.append(step)
        def json(self, *args): calls.append(args[0])
        def actions(self):
            return [SimpleNamespace(kind='tool_call', tool='honua_publish_service', arguments={'table': 'parcels'}),
                    SimpleNamespace(kind='tool_call', tool='honua_query_features', arguments={}),
                    SimpleNamespace(kind='tool_call', tool='honua_publish_service', arguments={'table': 'shelters'})]
        def action_result(self, step, action):
            calls.append(step)
            return SimpleNamespace(status=200, json={'result': {'structuredContent': {'status': 'Completed',
                                      'serviceName': action.arguments['table'], 'layerId': '0'}}})
        def mcp_call(self, step, tool, arguments): calls.append(step)
    stack = Stack('http://127.0.0.1:1', 'private', None, 'fake', 'fake', str(tmp_path), 'sha256:fake',
                  {'recorder': Recorder()})
    layers = {name: {'schema': 'honua_data', 'table': name} for name in ('parcels', 'shelters', 'survey_points')}
    assert [o.assertion_id for o in load_scenario('P06').observe(stack, Seeded({'layers': layers}), 'A')] == ['service.query', 'pagination.complete', 'service.write-denied', 'source-content.unchanged']
    assert calls == ['p06-parcels-source-after', 'p06-shelters-source-after', 'p06-survey-points-source-after',
                     'p06-services-after', 'p06-parcels-publish', 'p06-parcels-describe',
                     'p06-shelters-publish', 'p06-shelters-describe']


def test_p05_sql_projection_rejects_changed_feature_and_wrong_table():
    import copy
    import hashlib
    import json
    from agent_e2e_local_api import Observation
    from agent_e2e_local_recorder import Record
    from agent_e2e_local_runner import frozen_projections, score_assertions
    case = load_scenario('P05')
    expected = case.expected('A')
    fixture = oracle.fixture_expectation('A', 'features.exact', layer='shelters')
    rows = [{**row['properties'], 'srid': 3857, 'geometry': row['geometry']} for row in fixture['features']]
    roles = {'created:table:shelters': 'imported_shelters'}
    def score(data, table):
        query = ('SELECT source_id, name, capacity, is_open, ST_SRID(ST_Transform(geometry, 3857)) AS srid, '
                 'ST_AsGeoJSON(ST_Transform(geometry, 3857))::json AS geometry, '
                 'ST_AsGeoJSON(ST_SnapToGrid(ST_Transform(geometry, 3857), 0.001))::json AS rounded_geometry FROM honua_data.' + table + ' ORDER BY source_id')
        raw = json.dumps(data).encode()
        record = Record('p05-import-rows', 'observe', 'sql', 'controller', 'owner', 'SELECT', query, 200, raw,
                        'sha256:' + hashlib.sha256(raw).hexdigest())
        observations = [Observation('features.exact', None, {'projection': 'extract/v1'})]
        results = score_assertions(list(expected), observations, expected, records={'p05-import-rows': record},
                                   roles=roles, scenario_id='P05', frozen=frozen_projections(case, 'A'))
        return next(row['status'] for row in results if row['assertion'] == 'features.exact')
    assert score(rows, 'imported_shelters') == 'pass'
    changed = copy.deepcopy(rows)
    changed[0]['capacity'] = 999
    assert score(changed, 'imported_shelters') == 'fail'
    assert score(rows, 'another_table') == 'fail'


def test_p05_fixture_and_inventory_seed_are_idempotent(tmp_path):
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / 'certification/agent-e2e/corpus/town-A'
    sources = {'valid': (root / 'shelters.csv').read_text(), 'invalid': (root / 'invalid-rows.csv').read_text()}
    calls = []
    class Recorder:
        def sql(self, step, query):
            calls.append((step, query))
            if step.startswith('p05-fixture-'):
                return [{'content': sources[step.removeprefix('p05-fixture-')], 'srid': 4326}]
            return []
    stack = Stack('http://127.0.0.1:1', 'private', None, 'fake', 'fake', str(tmp_path), 'sha256:fake',
                  {'recorder': Recorder()})
    case = load_scenario('P05')
    first = case.seed(stack, 'A')
    assert case.seed(stack, 'A') == first
    assert [step for step, _ in calls] == ['p05-table-baseline', 'p05-fixture-valid', 'p05-fixture-invalid']
    with pytest.raises(ValueError):
        case.seed(stack, 'B')


def test_p04_sql_projection_rejects_changed_attributes_and_foreign_table():
    import copy
    import hashlib
    import json
    from agent_e2e_local_api import Observation
    from agent_e2e_local_recorder import Record
    from agent_e2e_local_runner import frozen_projections, score_assertions
    case = load_scenario('P04')
    expected = case.expected('A')
    fixtures = {name: oracle.fixture_expectation('A', 'features.exact', layer=layer)['features']
                for name, layer in [('parcels', 'parcels'), ('flood-zones', 'flood_zones')]}
    roles = {'created:table:parcels': 'imported_parcels', 'created:table:flood-zones': 'imported_flood'}
    def score(mutated=False, wrong_table=False):
        records = {}
        for label, features in fixtures.items():
            rows = [{**row['properties'], 'srid': 3857, 'geometry': row['geometry']} for row in copy.deepcopy(features)]
            if label == 'parcels' and mutated:
                rows[0]['owner'] = 'wrong owner'
            table = ('foreign' if wrong_table else 'imported_parcels') if label == 'parcels' else 'imported_flood'
            columns = 'source_id, name, owner, value' if label == 'parcels' else 'source_id, name'
            query = ('SELECT ' + columns + ', ST_SRID(ST_Transform(geometry, 3857)) AS srid, '
                     'ST_AsGeoJSON(ST_Transform(geometry, 3857))::json AS geometry, '
                     'ST_AsGeoJSON(ST_SnapToGrid(ST_Transform(geometry, 3857), 0.001))::json AS rounded_geometry FROM honua_data.' + table + ' ORDER BY source_id')
            raw = json.dumps(rows).encode()
            step = 'p04-' + label + '-rows'
            records[step] = Record(step, 'observe', 'sql', 'controller', 'owner', 'SELECT', query, 200, raw,
                                   'sha256:' + hashlib.sha256(raw).hexdigest())
        result = score_assertions(list(expected), [Observation('features.exact', None, {'projection': 'extract/v1'})],
                                  expected, records=records, roles=roles, scenario_id='P04',
                                  frozen=frozen_projections(case, 'A'))
        return next(row['status'] for row in result if row['assertion'] == 'features.exact')
    assert score() == 'pass'
    assert score(mutated=True) == 'fail'
    assert score(wrong_table=True) == 'fail'


def test_p04_fixture_and_inventory_seed_are_idempotent(tmp_path):
    from pathlib import Path
    import json
    root = Path(__file__).resolve().parents[2] / 'certification/agent-e2e'
    document = json.loads((root / 'expectations/local/P04.json').read_text())
    files = document['customerFiles']['A']
    fixtures = {name: (root / 'corpus' / path).read_text() for name, path in zip(('parcels', 'flood-zones'), files)}
    calls = []
    class Recorder:
        def sql(self, step, query):
            calls.append(step)
            if step.startswith('p04-fixture-'):
                text = fixtures[step.removeprefix('p04-fixture-')]
                srid = int(json.loads(text)['crs']['properties']['name'].split(':')[-1])
                return [{'content': text, 'srid': srid}]
            return []
    stack = Stack('http://127.0.0.1:1', 'private', None, 'fake', 'fake', str(tmp_path), 'sha256:fake',
                  {'recorder': Recorder()})
    case = load_scenario('P04')
    assert case.seed(stack, 'A') == case.seed(stack, 'A')
    assert calls == ['p04-table-baseline', 'p04-fixture-parcels', 'p04-fixture-flood-zones']
    with pytest.raises(ValueError):
        case.seed(stack, 'B')


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_p04_crs_digest_rejects_changed_coordinates(variant):
    import copy
    import hashlib
    import json
    from agent_e2e_local_api import Observation
    from agent_e2e_local_recorder import Record
    from agent_e2e_local_runner import frozen_projections, score_assertions
    case = load_scenario('P04')
    expected = case.expected(variant)
    roles = {'created:table:parcels': 'imported_parcels', 'created:table:flood-zones': 'imported_flood'}
    source_srids = expected['geometry.crs']['sourceSrid']
    def record(step, phase, query, value):
        raw = json.dumps(value).encode()
        return Record(step, phase, 'sql', 'controller', 'owner', 'SELECT', query, 200, raw,
                      'sha256:' + hashlib.sha256(raw).hexdigest())
    records = {}
    for i, (label, layer, table) in enumerate((('parcels', 'parcels', 'imported_parcels'),
                                               ('flood-zones', 'flood_zones', 'imported_flood'))):
        fixture = oracle.fixture_expectation(variant, 'features.exact', layer=layer)['features']
        rows = [{**f['properties'], 'srid': 3857, 'geometry': f['geometry'], 'rounded_geometry': f['geometry']}
                for f in sorted(fixture, key=lambda f: f['properties']['source_id'])]
        cols = 'source_id, name, owner, value' if label == 'parcels' else 'source_id, name'
        query = ('SELECT ' + cols + ', ST_SRID(ST_Transform(geometry, 3857)) AS srid, '
                 'ST_AsGeoJSON(ST_Transform(geometry, 3857))::json AS geometry, '
                 'ST_AsGeoJSON(ST_SnapToGrid(ST_Transform(geometry, 3857), 0.001))::json AS rounded_geometry FROM '
                 'honua_data.' + table + ' ORDER BY source_id')
        records['p04-' + label + '-rows'] = record('p04-' + label + '-rows', 'observe', query, rows)
        seed_step = 'p04-fixture-' + label
        records[seed_step] = record(seed_step, 'seed', "SELECT 'fixture' AS content, " + str(source_srids[i]) + ' AS srid',
                                    [{'content': 'fixture', 'srid': source_srids[i]}])
    obs = [Observation('geometry.crs', None, {'projection': 'extract/v1'})]
    def score(values):
        result = score_assertions(list(expected), obs, expected, records=values, roles=roles,
                                  scenario_id='P04', frozen=frozen_projections(case, variant))
        return next(row['status'] for row in result if row['assertion'] == 'geometry.crs')
    assert score(records) == 'pass'
    bad = dict(records)
    step = 'p04-parcels-rows'
    rows = copy.deepcopy(records[step].json)
    rows[0]['rounded_geometry']['coordinates'][0][0][0] += 1
    bad[step] = record(step, 'observe', records[step].path, rows)
    assert score(bad) == 'fail'


def test_p04_roles_bind_each_table_to_its_matching_input():
    import hashlib
    import json
    from agent_e2e_local_expectations import load_roles
    from agent_e2e_local_projections import resolve_roles
    from agent_e2e_local_recorder import Record
    from agent_e2e_local_scenarios.P04 import INVENTORY
    def record(step, phase, path, value):
        raw = json.dumps(value).encode()
        return Record(step, phase, 'sql', 'controller', 'owner', 'SELECT', path, 200, raw,
                      'sha256:' + hashlib.sha256(raw).hexdigest())
    records = {'p04-fixture-parcels': record('p04-fixture-parcels', 'seed', "SELECT 'a'", [{'content': 'a'}]),
               'p04-fixture-flood-zones': record('p04-fixture-flood-zones', 'seed', "SELECT 'b'", [{'content': 'b'}]),
               'p04-table-baseline': record('p04-table-baseline', 'seed', INVENTORY, []),
               'p04-table-after': record('p04-table-after', 'observe', INVENTORY,
                   [{'table_schema': 'honua_data', 'table_name': 'flood'}, {'table_schema': 'honua_data', 'table_name': 'parcel'}])}
    actions = [{'tool': 'honua_ingest_dataset', 'status': 'pass', 'arguments': {'data': data},
                'json': {'result': {'structuredContent': {'table': table}}}}
               for data, table in [('a', 'parcel'), ('b', 'flood')]]
    roles = resolve_roles(load_roles('P04'), records, agent_principal='agent', action_results=actions)
    assert roles['created:table:parcels'] == 'parcel' and roles['created:table:flood-zones'] == 'flood'
    actions[1]['arguments']['data'] = 'unrelated'
    with pytest.raises(ValueError, match='matched 0'):
        resolve_roles(load_roles('P04'), records, agent_principal='agent', action_results=actions)


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_p05_report_and_crs_reject_wrong_rows_and_coordinates(variant):
    import copy
    import hashlib
    import json
    from pathlib import Path
    from agent_e2e_local_api import Observation
    from agent_e2e_local_recorder import Record
    from agent_e2e_local_runner import frozen_projections, score_assertions
    case = load_scenario('P05')
    expected = case.expected(variant)
    root = Path(__file__).resolve().parents[2] / 'certification/agent-e2e/corpus'
    path = f'town-{variant}/invalid-rows.csv'
    invalid = (root / path).read_bytes()
    roles = {'created:table:shelters': 'imported_shelters', 'seeded:input:invalid': invalid.decode()}
    action_id = 'occ:1:2:sha256:' + 'a' * 64
    action = {'kind': 'tool_call', 'tool': 'honua_ingest_dataset', 'arguments': {'data': invalid.decode()}}
    args = json.dumps({'tool': action['tool'], 'arguments': action['arguments']}).encode()
    def record(step, phase, kind, principal, method, path, value, **kw):
        raw = json.dumps(value).encode()
        return Record(step, phase, kind, principal, 'id', method, path, 200, raw,
                      'sha256:' + hashlib.sha256(raw).hexdigest(), **kw)
    rows = [{**f['properties'], 'srid': 3857, 'geometry': f['geometry'], 'rounded_geometry': f['geometry']}
            for f in sorted(oracle.fixture_expectation(variant, 'features.exact', layer='shelters')['features'],
                            key=lambda f: f['properties']['source_id'])]
    query = ('SELECT source_id, name, capacity, is_open, ST_SRID(ST_Transform(geometry, 3857)) AS srid, '
             'ST_AsGeoJSON(ST_Transform(geometry, 3857))::json AS geometry, '
             'ST_AsGeoJSON(ST_SnapToGrid(ST_Transform(geometry, 3857), 0.001))::json AS rounded_geometry FROM '
             'honua_data.imported_shelters ORDER BY source_id')
    errors = [{'row': 1, 'code': 'import.geometry_invalid'}, {'row': 2, 'code': 'import.geometry_missing'},
              {'row': 3, 'code': 'import.geometry_invalid'}]
    records = {'p05-fixture-valid': record('p05-fixture-valid', 'seed', 'sql', 'controller', 'SELECT',
               "SELECT 'fixture' AS content, 4326 AS srid", [{'content': 'fixture', 'srid': 4326}]),
               'p05-import-rows': record('p05-import-rows', 'observe', 'sql', 'controller', 'SELECT', query, rows),
               'p05-invalid-report': record('p05-invalid-report', 'observe', 'mcp', 'agent', 'POST', '/mcp#tools/call',
                   {'jsonrpc': '2.0', 'id': 1, 'result': {'structuredContent': {'rowErrors': errors}}},
                   replay_of=action_id, request_arguments_raw=args, origin='session-result')}
    observations = [Observation(name, None, {'projection': 'extract/v1'}) for name in ('csv.invalid-reported', 'geometry.crs')]
    def scores(values):
        out = score_assertions(list(expected), observations, expected, records=values, roles=roles, scenario_id='P05',
            frozen=frozen_projections(case, variant), model_actions={action_id: action}, customer_files={path: invalid})
        return {r['assertion']: r['status'] for r in out}
    assert scores(records)['csv.invalid-reported'] == scores(records)['geometry.crs'] == 'pass'
    bad = dict(records)
    wrong = copy.deepcopy(errors)
    wrong[1]['row'] = 1
    bad['p05-invalid-report'] = record('p05-invalid-report', 'observe', 'mcp', 'agent', 'POST', '/mcp#tools/call',
        {'jsonrpc': '2.0', 'id': 1, 'result': {'structuredContent': {'rowErrors': wrong}}},
        replay_of=action_id, request_arguments_raw=args, origin='session-result')
    assert scores(bad)['csv.invalid-reported'] == 'fail'
    changed = copy.deepcopy(rows)
    changed[0]['rounded_geometry']['coordinates'][0] += 1
    bad = dict(records)
    bad['p05-import-rows'] = record('p05-import-rows', 'observe', 'sql', 'controller', 'SELECT', query, changed)
    assert scores(bad)['geometry.crs'] == 'fail'


def test_p05_handoff_requires_each_invalid_row_to_be_disclosed(tmp_path):
    import hashlib
    import json
    from pathlib import Path
    from types import SimpleNamespace
    from agent_e2e_handoff import score_final_answer
    from agent_e2e_local_projections import Context, evaluate
    from agent_e2e_local_recorder import Record
    root = Path(__file__).resolve().parents[2] / 'certification/agent-e2e/corpus'
    data = (root / 'town-A/invalid-rows.csv').read_bytes()
    def record(step, kind, path, body):
        raw = json.dumps(body).encode()
        return Record(step, 'observe', kind, 'controller', 'id', 'POST', path, 200, raw,
                      'sha256:' + hashlib.sha256(raw).hexdigest())
    records = {'p05-import-rows': record('p05-import-rows', 'sql', 'SELECT x', [{'name': 'shelter'}]),
               'p05-invalid-report': record('p05-invalid-report', 'mcp', '/mcp#tools/call',
                   {'jsonrpc': '2.0', 'id': 1, 'result': {'structuredContent': {'rowErrors': [{'row': 1}, {'row': 2}, {'row': 3}]}}})}
    stack = Stack('http://127.0.0.1:1', 'private', None, 'fake', 'fake', str(tmp_path), 'sha256:fake',
                  {'recorder': SimpleNamespace(records=records)})
    spec = load_scenario('P05').handoff(stack, None, 'A')
    facts = evaluate(spec, Context(records, customer_files={'town-A/invalid-rows.csv': data}))
    assert score_final_answer('Rows bad1, bad2 and bad3 could not be placed.', facts, state_status='pass')['status'] == 'pass'
    assert score_final_answer('Rows bad1 and bad2 could not be placed.', facts, state_status='pass')['status'] == 'fail'


def test_p06_query_and_permission_projections_reject_wrong_state():
    import copy
    import hashlib
    import json
    from agent_e2e_local_api import Observation
    from agent_e2e_local_recorder import Record
    from agent_e2e_local_runner import frozen_projections, score_assertions
    case = load_scenario('P06')
    expected = case.expected('A')
    roles = {'seeded:principal:viewer': 'viewer'}
    for name in ('parcels', 'shelters', 'survey_points'):
        roles.update({'created:service:' + name: 'service_' + name,
                      'child:serviceid:' + name: 'id_' + name, 'child:layer:' + name: 0})
    def record(step, kind, principal, principal_id, method, path, status, body, arguments=None):
        raw = json.dumps(body).encode()
        return Record(step, 'observe', kind, principal, principal_id, method, path, status, raw,
                      'sha256:' + hashlib.sha256(raw).hexdigest(),
                      request_arguments_raw=None if arguments is None else json.dumps(arguments).encode())
    records = {}
    for name in ('parcels', 'shelters', 'survey_points'):
        label = name.replace('_', '-')
        values = sorted(f['properties']['source_id'] for f in oracle.fixture_expectation('A', 'features.exact', layer=name)['features'])
        offsets = (0, 1000, 2000) if name == 'survey_points' else (0,)
        for offset in offsets:
            step = 'p06-' + label + '-query-' + str(offset)
            page = values[offset:offset+1000]
            body = {'jsonrpc': '2.0', 'id': 1, 'result': {'structuredContent': {
                    'resultOffset': offset, 'returnedCount': len(page), 'exceededTransferLimit': offset+1000 < len(values),
                    'geojson': {'features': [{'properties': {'source_id': x}} for x in page]}}}}
            args = {'tool': 'honua_query_features', 'arguments': {'serviceId': 'id_' + name, 'layerId': 0}}
            records[step] = record(step, 'mcp', 'controller', 'installer-bootstrap-admin', 'POST', '/mcp#tools/call', 200, body, args)
        path = '/rest/services/service_' + name + '/FeatureServer/0'
        read, write = 'p06-' + label + '-read', 'p06-' + label + '-write'
        records[read] = record(read, 'http', 'controller', 'installer-bootstrap-admin', 'GET', path + '?f=json', 200, {'ok': True})
        records[write] = record(write, 'http', 'p06-viewer', 'viewer', 'POST', path + '/applyEdits?f=json', 403, {'error': 'forbidden'})
    obs = [Observation(name, None, {'projection': 'extract/v1'}) for name in ('service.query', 'service.write-denied')]
    def score(values):
        rows = score_assertions(list(expected), obs, expected, records=values, roles=roles,
                                scenario_id='P06', frozen=frozen_projections(case, 'A'))
        return {row['assertion']: row['status'] for row in rows}
    assert score(records)['service.query'] == score(records)['service.write-denied'] == 'pass'
    wrong = dict(records)
    step = 'p06-survey-points-query-2000'
    body = copy.deepcopy(records[step].json)
    body['result']['structuredContent']['geojson']['features'].pop()
    old = records[step]
    wrong[step] = record(step, old.kind, old.principal, old.principal_id, old.method, old.path, 200, body, old.request_arguments)
    assert score(wrong)['service.query'] == 'fail'
    wrong = dict(records)
    step = 'p06-shelters-write'
    old = records[step]
    wrong[step] = record(step, old.kind, old.principal, old.principal_id, old.method, old.path, 200, {'ok': True})
    assert score(wrong)['service.write-denied'] == 'fail'


def test_p06_preservation_rejects_mutation_and_wrong_table():
    import copy
    import hashlib
    import json
    from agent_e2e_local_api import Observation
    from agent_e2e_local_recorder import Record
    from agent_e2e_local_runner import frozen_projections, score_assertions
    from agent_e2e_local_scenarios.P06 import source_query
    case = load_scenario('P06')
    expected = case.expected('A')
    roles = {'seeded:table:' + n: 'fixture_' + n for n in ('parcels', 'shelters', 'survey_points')}
    records = {}
    def record(step, phase, query, rows):
        raw = json.dumps(rows).encode()
        return Record(step, phase, 'sql', 'controller', 'owner', 'SELECT', query, 200, raw,
                      'sha256:' + hashlib.sha256(raw).hexdigest())
    for n in ('parcels', 'shelters', 'survey_points'):
        features = oracle.fixture_expectation('A', 'features.exact', layer=n)['features']
        rows = [{**f['properties'], 'feature_type': 'Feature', 'srid': 3857, 'geometry': f['geometry']}
                for f in sorted(features, key=lambda f:f['properties']['source_id'])]
        query = source_query({'schema': 'honua_data', 'table': 'fixture_' + n}, n)
        for suffix, phase in [('before', 'seed'), ('after', 'observe')]:
            step = 'p06-' + n.replace('_', '-') + '-source-' + suffix
            records[step] = record(step, phase, query, rows)
    def score(values):
        result = score_assertions(list(expected), [Observation('source-content.unchanged', None, {'projection': 'extract/v1'})],
                                  expected, records=values, roles=roles, scenario_id='P06',
                                  frozen=frozen_projections(case, 'A'))
        return next(r['status'] for r in result if r['assertion'] == 'source-content.unchanged')
    assert score(records) == 'pass'
    step = 'p06-parcels-source-after'
    old = records[step]
    changed = copy.deepcopy(old.json)
    changed[0]['owner'] = 'changed'
    bad = dict(records)
    bad[step] = record(step, 'observe', old.path, changed)
    assert score(bad) == 'fail'
    bad[step] = record(step, 'observe', old.path.replace('fixture_parcels', 'foreign'), old.json)
    assert score(bad) == 'fail'

@pytest.mark.parametrize('variant', ['A', 'B'])
@pytest.mark.parametrize('page_size', [100, 1000])
def test_p06_pagination_uses_every_sealed_replay_and_rejects_missing_page(variant, page_size):
    import hashlib
    import json
    from agent_e2e_local_projections import Context, evaluate
    from agent_e2e_local_recorder import Record
    from agent_e2e_local_runner import frozen_projections

    scenario = load_scenario('P06')
    expected = scenario.expected(variant)['pagination.complete']
    ids = expected['sourceIds']
    roles = {'child:serviceid:survey_points': 'survey-service', 'child:layer:survey_points': 0}
    records, actions = {}, {}
    for index, start in enumerate(range(0, len(ids), page_size)):
        occurrence = f'occ:1:{index}:sha256:' + format(index + 1, '064x')
        args = {'serviceId': 'survey-service', 'layerId': 0, 'resultOffset': start, 'limit': page_size}
        actions[occurrence] = {'kind': 'tool_call', 'tool': 'honua_query_features', 'arguments': args,
                               '_session': 1, '_sequence': index}
        body = {'jsonrpc': '2.0', 'id': 1, 'result': {'structuredContent': {'geojson': {'features': [
            {'properties': {'source_id': value}} for value in ids[start:start + page_size]]}}}}
        raw = json.dumps(body).encode()
        step = f'p06-survey-replay-{index}'
        records[step] = Record(step, 'observe', 'mcp', 'controller', 'admin', 'POST', '/mcp#tools/call', 200,
                               raw, 'sha256:' + hashlib.sha256(raw).hexdigest(), replay_of=occurrence,
                               request_arguments_raw=json.dumps({'tool': 'honua_query_features', 'arguments': args}).encode())
    spec = frozen_projections(scenario, variant)['pagination.complete']['spec']
    observed = evaluate(spec, Context(records, roles, model_actions=actions))
    oracle.verify('pagination.complete', observed, expected)
    bad = copy.deepcopy(observed)
    bad['pages'].pop()
    with pytest.raises(oracle.VerificationError):
        oracle.verify('pagination.complete', bad, expected)
    del records[f'p06-survey-replay-{len(records)-1}']
    with pytest.raises(ValueError):
        evaluate(spec, Context(records, roles, model_actions=actions))


def test_import_fixture_identity_sql_is_accepted_by_core_allowlist():
    from agent_e2e_local_runner import sql_allowlisted
    for scenario_id in ('P04', 'P05'):
        scenario = load_scenario(scenario_id)
        for variant in ('A', 'B'):
            class Recorder:
                def sql(self, step, query):
                    sql_allowlisted(query)
                    if step.endswith('baseline'):
                        return []
                    import re
                    value = re.match(r"SELECT '((?:[^']|'')*)' AS content, (\d+) AS srid FROM ", query, re.S)
                    assert value
                    return [{'content': value.group(1).replace("''", "'"), 'srid': int(value.group(2))}]
            stack = Stack('http://127.0.0.1:1', 'private', None, 'fake', 'fake', '/tmp', 'sha256:fake',
                          {'recorder': Recorder()})
            scenario.seed(stack, variant)
