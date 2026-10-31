"""Fail-closed controls for the unsupported resilience cases."""
from __future__ import annotations

import copy
import importlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools'))
from agent_e2e_local_api import Seeded, Stack

spec = importlib.util.spec_from_file_location('corpus_oracle', ROOT / 'certification/agent-e2e/corpus/oracle.py')
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)


@pytest.fixture(params=['P21', 'P22', 'P23'])
def scenario(request):
    return importlib.import_module('agent_e2e_local_scenarios.' + request.param).scenario


@pytest.fixture
def stack(tmp_path):
    return Stack('http://127.0.0.1:1', 'controller-only', None, 'case', 'unused',
                 str(tmp_path), 'sha256:' + '0' * 64)


def test_unsupported_case_cannot_seed_score_or_mutate(scenario, stack, tmp_path):
    assert scenario.supported_variants == ()
    assert scenario.id in type(scenario).__name__
    assert scenario.needs_approver() == (scenario.id in ('P21', 'P22'))
    for variant in ('A', 'B'):
        for _ in range(2):
            with pytest.raises(NotImplementedError):
                scenario.seed(stack, variant)
        for call in (lambda: scenario.observe(stack, Seeded({}), variant),
                     lambda: scenario.expected(variant)):
            with pytest.raises(NotImplementedError):
                call()
    assert scenario.cleanup(stack, Seeded({})) is None
    assert list(tmp_path.iterdir()) == []
    assert not (ROOT / 'certification/agent-e2e/expectations/local' /
                (scenario.id + '.json')).exists()


def test_fixture_source_preservation_negative_control(scenario):
    # This tests the oracle, not an observation from an unsupported scenario.
    features = json.loads((ROOT / 'certification/agent-e2e/corpus/town-A/shelters.3857.geojson').read_text())
    snapshot = {'resourceId': 'fixture-shelters', 'content': features}
    expected = {'profile': 'honua.protected-content/v1', 'resources': [snapshot]}
    observed = {'profile': expected['profile'], 'before': [copy.deepcopy(snapshot)],
                'after': [copy.deepcopy(snapshot)]}
    oracle.verify('source-content.unchanged', observed, expected)
    observed['after'][0]['content']['features'][0]['properties']['name'] = 'changed'
    with pytest.raises(oracle.VerificationError):
        oracle.verify('source-content.unchanged', observed, expected)


@pytest.mark.parametrize(('scenario_id', 'assertion', 'expected', 'wrong'), [
    ('P21', 'fault.observed',
     {'injectionId': 'injection-1', 'targetOperationId': 'map-1',
      'observedFailure': {'status': 503, 'count': 1}},
     {'injectionId': 'injection-1', 'targetOperationId': 'map-1',
      'observedFailure': {'status': 200, 'count': 1}}),
    ('P22', 'session.fresh',
     {'previousSessionId': 'session-1', 'currentSessionId': 'session-2',
      'visibleConversationMessageIds': []},
     {'previousSessionId': 'session-1', 'currentSessionId': 'session-1',
      'visibleConversationMessageIds': []}),
    ('P23', 'cleanup.complete', {'resourceIds': []},
     {'resourceIds': ['owned-resource-still-present']}),
])
def test_scenario_oracle_negative_control(scenario_id, assertion, expected, wrong):
    # Synthetic controls exercise predicates only; they are not case evidence.
    scenario = importlib.import_module('agent_e2e_local_scenarios.' + scenario_id).scenario
    assert scenario.supported_variants == ()
    oracle.verify(assertion, copy.deepcopy(expected), expected)
    with pytest.raises(oracle.VerificationError):
        oracle.verify(assertion, wrong, expected)


def test_runner_contract():
    p21 = importlib.import_module('agent_e2e_local_scenarios.P21').scenario
    p22 = importlib.import_module('agent_e2e_local_scenarios.P22').scenario
    for variant in ('A', 'B'):
        assert p21.fault_plan(variant) == {
            'kind': 'http-transient', 'status': 503, 'count': 1,
            'operationClass': 'map-create', 'trigger': 'after-commit-before-response',
            'injectionActor': 'controller', 'requireCommittedResourceObservation': True}
        assert p22.fault_plan(variant) is None
    assert p22.sessions_required() == 2
