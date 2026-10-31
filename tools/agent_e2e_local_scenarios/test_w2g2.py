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
    assert scenario.supported_variants == ()
    assert scenario.fault_plan(variant) is None
    assert set(scenario.expected(variant)) == ASSERTIONS[scenario_id]
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
    import agent_e2e_scenario_backend as backend
    import agent_e2e_scenario_operations as operations
    calls = []

    class Town:
        def __init__(self, recorder, manifest): pass
        def prepare(self, **kwargs):
            calls.append(('town', kwargs['starting_state']))
            return {'layers': {}}

    class Recorder:
        def upload(self, step, raw, **kwargs):
            calls.append(('upload', step, kwargs['table'], len(raw)))
            return {'fixture': True}

    monkeypatch.setattr(backend, 'TownSetup', Town)
    monkeypatch.setattr(operations, 'discover_tables', lambda *args: {})
    monkeypatch.setattr(operations, 'completed_import', lambda http, accepted, **kw:
                        {'table': kw['table'], 'featureCount': kw['feature_count']})
    stack = Stack('http://127.0.0.1:1', 'private', None, 'case-123', 'fake', str(tmp_path), 'sha256:fake',
                  {'recorder': Recorder(), 'ensureConnection': lambda: 'connection',
                   'addPrincipal': lambda name, grants: 'viewer'})
    scenario = load_scenario('P06')
    first = scenario.seed(stack, 'A')
    assert scenario.seed(stack, 'A') == first
    assert first.state['survey']['featureCount'] > 1
    assert calls[0] == ('town', 'imported-town')
    assert len([call for call in calls if call[0] == 'upload']) == 1
    with pytest.raises(ValueError):
        scenario.seed(stack, 'B')
