import json

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
    import agent_e2e_scenario_backend
    calls = []

    class Town:
        def __init__(self, http, manifest): pass
        def prepare(self, **kw):
            calls.append(kw)
            return {'layers': {'shelters': {'service': 'svc', 'layerId': 3}}}

    class Recorder:
        def __init__(self): self.steps = []
        def json(self, step, method, path, body=None, **kw): self.steps.append(step)
    monkeypatch.setattr(agent_e2e_scenario_backend, 'TownSetup', Town)
    recorder = Recorder()
    stack = Stack('http://127.0.0.1:1', 'k', None, 'corpus-abc', 'f', str(tmp_path), 'sha256:' + 'a' * 64,
                  {'recorder': recorder, 'ensureConnection': lambda: 'conn-1'})
    first = P10().seed(stack, 'A')
    assert P10().seed(stack, 'A') == first
    assert len(calls) == 1 and recorder.steps == ['p10-source-before']
    with pytest.raises(ValueError):
        P10().seed(stack, 'B')


def test_query_selection_uses_only_the_seeded_layer_and_the_last_query():
    seeded = Seeded({'service': 'svc', 'layerId': 3})

    def action(n, tool, args):
        return LoggedAction(1, n, f'sha256:{n}', 'tool_call', tool, args, None, 'pass', '/x')
    actions = [action(1, 'honua_query_features', {'serviceId': 'svc', 'layerId': 3, 'where': 'a'}),
               action(2, 'honua_query_features', {'serviceId': 'other', 'layerId': 3}),
               action(3, 'honua_query_features', {'serviceId': 'svc', 'layerId': '3', 'where': 'b'}),
               action(4, 'honua_get_layer', {'serviceId': 'svc', 'layerId': 3})]
    assert P10.selected_query(actions, seeded).sequence == 3
    assert P10.selected_query(actions[1:2], seeded) is None


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
