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
    assert scenario.supported_variants == (('A', 'B') if scenario_id == 'P07' else ())
    assert scenario.fault_plan(variant) is None
    assert set(scenario.expected(variant)) == ASSERTIONS[scenario_id]
    if scenario_id != 'P07':
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
        def upload(self, step, raw, **kwargs):
            calls.append(('upload', step, kwargs['table'], len(raw)))
            return {'fixture': True}

    monkeypatch.setattr(operations, 'discover_tables', lambda *args: {})
    monkeypatch.setattr(operations, 'completed_import', lambda http, accepted, **kw:
                        {'table': kw['table'], 'featureCount': kw['feature_count']})
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
    action_id = 'sha256:' + 'a' * 64
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
