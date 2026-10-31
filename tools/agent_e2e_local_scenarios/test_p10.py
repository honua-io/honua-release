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
