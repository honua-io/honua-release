"""Fail-closed scenario registration and frozen-fixture oracle controls."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

from agent_e2e_local_api import Seeded, Stack

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / 'certification/agent-e2e/corpus'
SPEC = importlib.util.spec_from_file_location('w2g2_oracle', CORPUS / 'oracle.py')
oracle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(oracle)
STACK = Stack('http://127.0.0.1:1', 'controller-only', None, 'fake', 'fake', '/fake', 'sha256:fake')


def module(scenario):
    spec = importlib.util.spec_from_file_location(scenario, Path(__file__).parent / (scenario + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.mark.parametrize('scenario', ['P03', 'P04', 'P05', 'P06', 'P07'])
@pytest.mark.parametrize('variant', ['A', 'B'])
def test_unsupported_fails_closed_and_fixture_is_pinned(scenario, variant):
    case = module(scenario)
    assert case.id == scenario and case.supported_variants == ()
    assert case.__doc__ and 'unsupported' in case.__doc__
    facts = json.loads((ROOT / 'certification/agent-e2e/expectations/local' / (scenario + '.json')).read_text())
    assert facts['status'] == 'unsupported' and facts['assertions']
    assert facts['fixtureSha256'][variant] == hashlib.sha256((CORPUS / ('town-' + variant) / 'input-guide.json').read_bytes()).hexdigest()
    for function, args in [(case.seed, (STACK, variant)), (case.observe, (STACK, Seeded({}), variant)),
                           (case.expected, (variant,)), (case.fault_plan, (variant,)),
                           (case.cleanup, (STACK, Seeded({})))]:
        with pytest.raises(NotImplementedError):
            function(*args)
    assert case.needs_approver() is False


@pytest.mark.parametrize('scenario,layer', [('P03', 'parcels'), ('P04', 'flood_zones'),
                                           ('P05', 'shelters'), ('P06', 'survey_points'), ('P07', 'roads')])
@pytest.mark.parametrize('variant', ['A', 'B'])
def test_fixture_oracle_rejects_changed_feature(scenario, layer, variant):
    # This is an oracle negative control, not a claim of a live scenario observation.
    expected = oracle.fixture_expectation(variant, 'features.exact', layer=layer)
    observed = copy.deepcopy(expected)
    oracle.verify('features.exact', observed, expected)
    observed['features'][0]['properties']['source_id'] = 'incorrect-source-id'
    with pytest.raises(oracle.VerificationError):
        oracle.verify('features.exact', observed, expected)
