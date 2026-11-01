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


def test_fixture_goal_reaches_real_arc_adapter_and_scores_live_evidence(tmp_path):
    import ai_arc_services
    import test_agent_e2e_local_runner as corpus

    goal = 'Synthetic request: find matching shelters in the prepared service.'
    cipher = Fernet(Fernet.generate_key())
    payload = json.loads(cipher.decrypt(cipher.encrypt(json.dumps(
        {'services': goal, 'fixtures': 'town-A'}).encode())))
    state = corpus.Candidate()
    server = corpus.serve(state)
    seen = []

    class Cli:
        def run(self, *, prompt, execute, **kwargs):
            seen.append(prompt)
            execute('tool_call', {'kind': 'tool_call', 'tool': 'honua_query_features',
                                  'arguments': {'serviceId': 'p10-shelters', 'layerId': 0,
                                                'where': corpus.CORRECT_WHERE}})
            return {'finalAnswer': 'Two shelters match: Centre and the North refuge.', 'tokens': 1, 'usage': [],
                    'modelVisibleDigests': [], 'provenance': [], 'messageIds': [], 'systemContractSha256': None,
                    'toolViewSha256': None}

    try:
        _, cases = ai_arc_services.run_services(
            scenarios=('P10',), variant='A', candidate_digest=corpus.DIGEST,
            base_url=f'http://127.0.0.1:{server.server_address[1]}', admin_key=corpus.ADMIN,
            project='arc-synthetic', arm={'agent': 'claude-cli', 'model': 'synthetic'},
            limits={'maxTurns': 20, 'maxToolCalls': 20, 'maxSeconds': 60}, out_dir=tmp_path / 'out',
            cli=Cli(), load_scenario=lambda _: corpus.FakeP10(), goal=payload['services'],
            fixture_id=payload['fixtures'], scenario_id='P10')
        assert cases[0]['receipt']['status'] == 'pass', cases[0]['receipt']
        assert cases[0]['receipt']['fixtureId'] == 'town-A'
        assert len(seen) == 1 and goal in seen[0]
    finally:
        server.shutdown()


def test_adapter_rejects_partial_or_mismatched_fixture_arguments_before_setup(tmp_path):
    import ai_arc_services
    common = dict(scenarios=('P10',), variant='A', candidate_digest='sha256:' + 'a' * 64,
                  base_url='http://127.0.0.1:1', admin_key='unused', project='unused',
                  arm={}, limits={}, out_dir=tmp_path)
    with pytest.raises(ValueError):
        ai_arc_services.run_services(**common, goal='request')
    with pytest.raises(ValueError):
        ai_arc_services.run_services(**common, goal='request', fixture_id='town-B', scenario_id='P10')
    common['scenarios'] = ('P12',)
    with pytest.raises(ValueError, match='adopted ARC runtime'):
        ai_arc_services.run_services(**common, goal='request', fixture_id='town-A', scenario_id='P12')
