"""The ARC fixture boundary uses synthetic in-memory ciphertext only."""
import json

import pytest
from cryptography.fernet import Fernet

import agent_e2e_local_runner as runner
from agent_e2e_local_expectations import load_fixture_expected


@pytest.mark.parametrize('fixture,variant', [('town-A', 'A'), ('town-B', 'B')])
@pytest.mark.parametrize('scenario', ['P10', 'P12', 'P13'])
def test_fixture_case_uses_runtime_goal_and_frozen_expectations(fixture, variant, scenario):
    key = Fernet.generate_key()
    cipher = Fernet(key)
    goal = 'Synthetic customer request for a previously prepared data service.'
    sealed = cipher.encrypt(json.dumps({'services': goal, 'fixtures': fixture}).encode())
    case = json.loads(cipher.decrypt(sealed))
    actual_variant, selected = runner.select_fixture_case(
        goal=case['services'], fixture_id=case['fixtures'], scenario_id=scenario)
    assert actual_variant == variant
    assert selected['prompts'] == {variant: goal}
    assert selected['fixtureId'] == fixture
    assert set(selected['assertions']) == set(load_fixture_expected(fixture, scenario))


@pytest.mark.parametrize('fixture,scenario', [('unknown', 'P10'), ('town-C', 'P10'),
                                               ('town-A', 'P07'), ('town-A', 'P99'),
                                               ('../town-A', 'P10')])
def test_unknown_fixture_or_scenario_fails_closed(fixture, scenario):
    with pytest.raises(ValueError):
        runner.select_fixture_case(goal='Synthetic customer request', fixture_id=fixture, scenario_id=scenario)


def test_empty_goal_and_missing_expectations_fail_closed(monkeypatch):
    with pytest.raises(ValueError):
        runner.select_fixture_case(goal=' ', fixture_id='town-A', scenario_id='P10')
    monkeypatch.setattr('agent_e2e_local_expectations.load_fixture_expected', lambda *_: {})
    with pytest.raises(ValueError, match='assertion inventory'):
        runner.select_fixture_case(goal='Synthetic request', fixture_id='town-A', scenario_id='P10')
