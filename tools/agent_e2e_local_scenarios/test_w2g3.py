"""Fail-closed controls; these do not claim a supported scenario or live run."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from agent_e2e_local_api import Seeded, Stack

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('corpus_oracle_w2g3', ROOT / 'certification/agent-e2e/corpus/oracle.py')
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)


@pytest.mark.parametrize('scenario_id', ['P08', 'P09', 'P11', 'P13', 'P19'])
@pytest.mark.parametrize('variant', ['A', 'B'])
def test_unsupported_scenario_fails_closed(scenario_id, variant, tmp_path):
    path = Path(__file__).parent / f'{scenario_id}.py'
    module_spec = importlib.util.spec_from_file_location(scenario_id, path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    scenario = module.Scenario()
    stack = Stack('http://127.0.0.1:1', 'controller-only', None, 'test', 'compose.yml', str(tmp_path), 'sha256:test')
    assert scenario.id == scenario_id
    assert scenario.supported_variants == ()
    assert 'unsupported' in module.__doc__
    assert scenario.fault_plan(variant) is None
    assert scenario.needs_approver() is False
    for invoke in (lambda: scenario.seed(stack, variant),
                   lambda: scenario.observe(stack, Seeded({}), variant),
                   lambda: scenario.expected(variant),
                   lambda: scenario.cleanup(stack, Seeded({}))):
        with pytest.raises(RuntimeError, match='no independent local observer'):
            invoke()


@pytest.mark.parametrize('scenario_id', ['P08', 'P09', 'P11', 'P13', 'P19'])
@pytest.mark.parametrize('variant', ['A', 'B'])
def test_fixture_preservation_oracle_negative_control(scenario_id, variant):
    # A fixture-level oracle control, not an observation from these unsupported cases.
    source = json.loads((ROOT / f'certification/agent-e2e/corpus/town-{variant}/shelters.3857.geojson').read_text())
    resources = [{'resourceId': 'fixture-shelters', 'content': source}]
    expected = {'profile': 'honua.protected-content/v1', 'resources': resources}
    observed = {'profile': expected['profile'], 'before': copy.deepcopy(resources), 'after': copy.deepcopy(resources)}
    oracle.verify('source-content.unchanged', observed, expected)
    observed['after'][0]['content']['features'].clear()
    with pytest.raises(oracle.VerificationError):
        oracle.verify('source-content.unchanged', observed, expected)
