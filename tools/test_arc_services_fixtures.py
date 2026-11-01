"""ARC fixture selection tests use synthetic in-memory ciphertext only."""
import json

import pytest
from cryptography.fernet import Fernet

import agent_e2e_local_expectations as expectations
import agent_e2e_local_runner as runner


@pytest.mark.parametrize('fixture,variant', [('town-A', 'A'), ('town-B', 'B')])
def test_synthetic_fixture_selects_every_frozen_scenario(fixture, variant):
    cipher = Fernet(Fernet.generate_key())
    goal = 'Synthetic customer request for an independently prepared data service.'
    payload = json.loads(cipher.decrypt(cipher.encrypt(json.dumps(
        {'services': goal, 'fixtures': fixture}).encode())))
    actual_variant, cases = runner.select_fixture_cases(goal=payload['services'], fixture_id=payload['fixtures'])
    assert actual_variant == variant and set(cases) == {'P10', 'P15'}
    for scenario, case in cases.items():
        assert case['prompts'] == {variant: goal}
        assert case['fixtureId'] == fixture
        assert set(case['assertions']) == set(expectations.load_fixture_expected(fixture, scenario))


@pytest.mark.parametrize('fixture', ['unknown', 'town-C', '../town-A', None])
def test_unknown_fixture_fails_closed(fixture):
    with pytest.raises(ValueError):
        runner.select_fixture_cases(goal='Synthetic customer request', fixture_id=fixture)


def test_catalog_tampering_and_missing_expectations_fail_closed(monkeypatch, tmp_path):
    with pytest.raises(ValueError):
        runner.select_fixture_cases(goal=' ', fixture_id='town-A')
    copy = tmp_path / 'catalog.json'
    copy.write_bytes(expectations.ARC_CATALOG.read_bytes() + b' ')
    monkeypatch.setattr(expectations, 'ARC_CATALOG', copy)
    with pytest.raises(ValueError, match='digest'):
        runner.select_fixture_cases(goal='request', fixture_id='town-A')
    monkeypatch.undo()
    monkeypatch.setattr(expectations, 'load_fixture_expected', lambda *_: {})
    with pytest.raises(ValueError, match='assertion inventory'):
        runner.select_fixture_cases(goal='request', fixture_id='town-A')


def test_adapter_returns_named_failure_for_unmapped_or_incompatible_cases(tmp_path):
    import ai_arc_services
    common = dict(scenarios=('P13',), variant='B', candidate_digest='sha256:' + 'a' * 64,
                  base_url='http://127.0.0.1:1', admin_key='unused', project='unused',
                  arm={}, limits={}, out_dir=tmp_path)
    for fixture, fault, reason in [('unknown', 'http_503_once', 'unmapped services fixture'),
                                   ('town-A', 'http_503_once', 'fault is incompatible')]:
        row, cases = ai_arc_services.run_fixture_services(**common, goal='request', fixture_id=fixture, fault=fault)
        assert cases == [] and row['failure']['category'] == 'model'
        assert reason in row['failure']['reason']
    row, cases = ai_arc_services.run_fixture_services(**common, goal=None, fixture_id=None, fault=None)
    assert cases == [] and row['failure']['category'] == 'model'


def test_redacted_assignment_is_not_a_credential_but_real_value_is():
    from agent_cli_sessions import screen_model_input
    screen_model_input('The earlier note says db_password=[redacted]')
    with pytest.raises(ValueError):
        screen_model_input('The earlier note says db_password=secret-value')


def test_later_case_seed_failure_is_undetermined_after_earlier_actions():
    from ai_arc_runner import stage_verdict
    arc = {'stages': [{'id': 'services', 'required': {'local-docker': ['P10:filter.exact']}}],
           'exclusions': []}
    limits = {'maxSeconds': 10, 'maxToolCalls': 10, 'maxContinueNudges': 1}
    actions = [{'status': 'pass'}]
    failure = {'category': 'infrastructure', 'beforeCaseActions': True, 'reason': 'P15 seed'}
    verdict, cause = stage_verdict(arc, 'local-docker', 'services', [], {'failure': failure}, actions,
                                   {}, [], limits, 1)
    assert verdict == 'undetermined' and 'P15 seed' in cause
    failure.pop('beforeCaseActions')
    verdict, _ = stage_verdict(arc, 'local-docker', 'services', [], {'failure': failure}, actions,
                                {}, [], limits, 1)
    assert verdict == 'fail'


def test_harness_custody_is_retained_in_case_but_not_scored_as_agent_work():
    from ai_arc_services import case_assertions
    receipt = {'status': 'pass', 'assertions': [
        {'assertion': 'custody.bootstrap-revoked', 'status': 'pass'},
        {'assertion': 'publication.private', 'status': 'pass'}],
        'finalAnswerCheck': {'status': 'pass'}}
    rows = case_assertions([{'scenario': 'P15', 'receipt': receipt}])
    assert 'P15:custody.bootstrap-revoked' not in [r['assertion'] for r in rows]
    assert receipt['assertions'][0]['status'] == 'pass'
    assert 'P15:publication.private' in [r['assertion'] for r in rows]


def test_agent_publication_guard_blocks_even_a_compatible_fault(monkeypatch, tmp_path):
    import ai_arc_services
    original = runner.load_scenario
    class Wrapped:
        def __init__(self, scenario): self.scenario = scenario
        def __getattr__(self, name): return getattr(self.scenario, name)
        def fault_plan(self, variant): return {'kind': 'http_503_once'}
    monkeypatch.setattr(runner, 'load_scenario', lambda sid: Wrapped(original(sid)))
    row, cases = ai_arc_services.run_fixture_services(
        goal='Synthetic request', fixture_id='town-A', fault='http_503_once',
        candidate_digest='sha256:' + 'a' * 64, base_url='http://127.0.0.1:1', admin_key='unused',
        project='unused', arm={}, limits={}, out_dir=tmp_path)
    assert cases == [] and row['failure']['category'] == 'model'
    assert 'publication from raw inputs' in row['failure']['reason']


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_town_setup_verifies_only_selected_fixture(monkeypatch, variant):
    import agent_e2e_local_scenarios.w2g3_common as common
    seen = []
    monkeypatch.setattr(common, 'verify_corpus', lambda root, manifest: seen.extend(r['path'] for r in manifest['files']))
    common.town_setup(object(), variant)
    assert len(seen) == 3 and all(path.startswith('town-' + variant + '/') for path in seen)
