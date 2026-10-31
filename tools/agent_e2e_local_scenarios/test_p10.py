import hashlib
import json

import pytest

from agent_e2e_local_runner import action_bytes
from agent_e2e_local_scenarios.P10 import replay_query


def test_replay_uses_digest_bound_query_and_rejects_tampering(tmp_path):
    action = {'kind': 'tool_call', 'tool': 'honua_query_features',
              'arguments': {'serviceId': 'svc', 'layerId': 7, 'where': 'capacity > 50'}}
    raw = action_bytes(action)
    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
    path = tmp_path / 'action.json'
    path.write_bytes(raw)
    class Client:
        def __init__(self): self.observations = []; self.calls = []
        def call_tool(self, name, args):
            self.calls.append((name, args))
            result = {'structuredContent': {'serviceId': 'svc', 'layerId': 7, 'exceededTransferLimit': False,
                    'resultOffset': 0, 'returnedCount': 1, 'geojson': {'type': 'FeatureCollection',
                    'features': [{'properties': {'source_id': 's1'}}]}}}
            import base64
            raw = json.dumps({'jsonrpc': '2.0', 'id': 'x', 'result': result}).encode()
            self.observations.append({'rpcMethod': 'tools/call', 'httpStatus': 200,
                                      'bodyBase64': base64.b64encode(raw).decode(),
                                      'bodyDigest': 'sha256:' + hashlib.sha256(raw).hexdigest()})
            return result
    client = Client()
    observed, evidence = replay_query(path, digest, {'service': 'svc', 'layerId': 7}, client)
    assert observed == {'resourceIds': ['s1']}
    assert evidence['replayedRequestDigest'] == digest
    assert client.calls == [('honua_query_features', action['arguments'])]
    class Contradictory(Client):
        def call_tool(self, name, args):
            result = super().call_tool(name, args)
            result['structuredContent']['returnedCount'] = 0
            return result
    with pytest.raises(ValueError):
        replay_query(path, digest, {'service': 'svc', 'layerId': 7}, Contradictory())
    path.write_text(json.dumps({**action, 'arguments': {}}))
    with pytest.raises(ValueError):
        replay_query(path, digest, {'service': 'svc', 'layerId': 7}, client)
    assert len(client.calls) == 1


def test_replayed_ids_checked_against_fixture_not_model_claim():
    from agent_e2e_local_runner import oracle
    expected = oracle.fixture_expectation('A', 'filter.exact')
    oracle.verify('filter.exact', {'resourceIds': list(expected['resourceIds'])}, expected)
    with pytest.raises(ValueError):
        oracle.verify('filter.exact', {'resourceIds': ['incorrect']}, expected)


def test_p10_seed_is_idempotent_per_case(monkeypatch, tmp_path):
    from agent_e2e_local_api import Stack
    from agent_e2e_local_scenarios.P10 import P10
    import agent_e2e_scenario_backend
    import agent_e2e_p10_observer
    calls = []
    class Town:
        def __init__(self, http, manifest): pass
        def prepare(self, **kwargs):
            calls.append(kwargs)
            return {'layers': {'shelters': {'service': 's', 'layerId': 1}}}
    class Http:
        def json(self, *args): return {'observation': {'httpStatus': 200}}
    monkeypatch.setattr(agent_e2e_scenario_backend, 'TownSetup', Town)
    monkeypatch.setattr(agent_e2e_p10_observer, 'source_content', lambda row: {'resourceId': 'shelters', 'content': {}})
    stack = Stack('http://127.0.0.1', 'controller', None, 'corpus-test', 'compose', str(tmp_path),
                  'sha256:' + 'a' * 64, {'controllerHttp': Http(), 'connectionId': 'connection'})
    scenario = P10()
    assert scenario.seed(stack, 'A') == scenario.seed(stack, 'A')
    assert len(calls) == 1
    with pytest.raises(ValueError): scenario.seed(stack, 'B')


def test_p10_query_projection_uses_raw_response_and_fixture_expectation():
    import base64
    from agent_e2e_local_api import Observation
    from agent_e2e_local_runner import oracle, score_assertions
    wanted = oracle.fixture_expectation('A', 'filter.exact')
    action = {'kind': 'tool_call', 'tool': 'honua_query_features',
              'arguments': {'serviceId': 'svc', 'layerId': 7}}
    action_raw = action_bytes(action)
    result = {'structuredContent': {'serviceId': 'svc', 'layerId': 7, 'exceededTransferLimit': False,
        'resultOffset': 0, 'returnedCount': len(wanted['resourceIds']), 'geojson': {'type': 'FeatureCollection',
        'features': [{'properties': {'source_id': item}} for item in wanted['resourceIds']]}}}
    body = json.dumps({'jsonrpc': '2.0', 'id': 'test', 'result': result}).encode()
    evidence = {'projection': 'p10-query/v1', 'binding': {'service': 'svc', 'layerId': 7},
        'action_b64': base64.b64encode(action_raw).decode(),
        'replayedRequestDigest': 'sha256:' + hashlib.sha256(action_raw).hexdigest(),
        'requests': [{'method': 'POST', 'url': '/mcp', 'status': 200,
                      'body_b64': base64.b64encode(body).decode(), 'sha256': hashlib.sha256(body).hexdigest()}]}
    def score(observed):
        return score_assertions(['filter.exact'], [Observation('filter.exact', observed, evidence)],
                                {'filter.exact': wanted}, trusted_requests={('POST', '/mcp', 200, 'sha256:' + hashlib.sha256(body).hexdigest())})[0]['status']
    assert score(wanted) == 'pass'
    assert score({'resourceIds': ['incorrect']}) == 'fail'


def test_p10_observe_scores_all_fixture_assertions_and_rejects_mutation(tmp_path):
    import base64
    from agent_e2e_local_api import RunRecord, Seeded, Stack
    from agent_e2e_local_runner import oracle, score_assertions
    from agent_e2e_local_scenarios.P10 import P10
    from agent_e2e_p10_observer import source_content
    fixture = oracle.fixture_expectation('A', 'features.exact', layer='shelters')
    features = fixture['features']
    fields = list(features[0]['properties'])
    def source(changed=False):
        rows = []
        for index, feature in enumerate(features):
            props = dict(feature['properties'])
            if changed and index == 0: props['name'] = 'changed'
            x, y = feature['geometry']['coordinates']
            rows.append({'attributes': {'objectid': index + 1, **props}, 'geometry': {'x': x, 'y': y}})
        doc = {'geometryType': 'esriGeometryPoint', 'exceededTransferLimit': False,
               'spatialReference': {'wkid': 3857}, 'objectIdFieldName': 'objectid',
               'fields': [{'name': 'objectid', 'type': 'esriFieldTypeOID'}] +
                         [{'name': name, 'type': 'string'} for name in fields], 'features': rows}
        raw = json.dumps(doc).encode()
        return {'observation': {'httpStatus': 200, 'body': doc, 'rawBodyBase64': base64.b64encode(raw).decode(),
                                'responseDigest': 'sha256:' + hashlib.sha256(raw).hexdigest()}}
    before = source()
    class Http:
        changed = False
        def json(self, *args): return source(self.changed)
    http = Http()
    wanted = oracle.fixture_expectation('A', 'filter.exact')
    action = {'kind': 'tool_call', 'tool': 'honua_query_features',
              'arguments': {'serviceId': 'svc', 'layerId': 7}}
    action_raw = action_bytes(action)
    path = tmp_path / 'action.json'
    path.write_bytes(action_raw)
    digest = 'sha256:' + hashlib.sha256(action_raw).hexdigest()
    class Mcp:
        def __init__(self): self.observations = []
        def initialize(self): pass
        def close(self): pass
        def call_tool(self, name, args):
            result = {'structuredContent': {'serviceId': 'svc', 'layerId': 7, 'exceededTransferLimit': False,
                'resultOffset': 0, 'returnedCount': len(wanted['resourceIds']), 'geojson': {'type': 'FeatureCollection',
                'features': [{'properties': {'source_id': rid}} for rid in wanted['resourceIds']]}}}
            raw = json.dumps({'jsonrpc': '2.0', 'id': 'call', 'result': result}).encode()
            self.observations.append({'rpcMethod': 'tools/call', 'httpStatus': 200,
                'bodyBase64': base64.b64encode(raw).decode(), 'bodyDigest': 'sha256:' + hashlib.sha256(raw).hexdigest()})
            return result
    extra = {'controllerHttp': http, 'controllerMcpFactory': Mcp,
             'actionLogs': [{'logPath': str(path), 'digest': digest}]}
    stack = Stack('http://127.0.0.1', 'controller', None, 'project', 'compose', str(tmp_path),
                  'sha256:' + 'a' * 64, extra)
    seeded = Seeded({'town': {'layers': {'shelters': {'service': 'svc', 'layerId': 7}}},
                     'sourceBefore': source_content(before), 'sourceBeforeResponse': before['observation']})
    run = RunRecord('actor', 1, (), (), ('done',))
    scenario = P10()
    ids = ['filter.exact', 'source-content.unchanged']
    observations = scenario.observe(stack, seeded, 'A', run)
    trusted = {(r['method'], r['url'], r['status'], r['sha256'] if r['sha256'].startswith('sha256:') else 'sha256:' + r['sha256'])
               for obs in observations for r in obs.evidence['requests']}
    results = score_assertions(ids, observations, scenario.expected('A'), trusted_requests=trusted)
    assert [item['status'] for item in results] == ['pass', 'pass']
    http.changed = True
    observations = scenario.observe(stack, seeded, 'A', run)
    trusted = {(r['method'], r['url'], r['status'], r['sha256'] if r['sha256'].startswith('sha256:') else 'sha256:' + r['sha256'])
               for obs in observations for r in obs.evidence['requests']}
    results = score_assertions(ids, observations, scenario.expected('A'), trusted_requests=trusted)
    assert [item['status'] for item in results] == ['pass', 'fail']
