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
                                   ('town-A', 'response_lost_after_commit', 'fault is incompatible')]:
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


def test_compatible_fault_requires_adopted_vm_before_dispatch(tmp_path):
    import ai_arc_services
    row, cases = ai_arc_services.run_fixture_services(
        goal='Synthetic request', fixture_id='town-A', fault='http_503_once',
        candidate_digest='sha256:' + 'a' * 64, base_url='http://127.0.0.1:1', admin_key='unused',
        project='unused', arm={}, limits={}, out_dir=tmp_path)
    assert cases == [] and row['failure']['category'] == 'model'
    assert 'VM slot is missing' in row['failure']['reason']


def test_p15_link_requires_a_passing_agent_publication_and_fired_fault():
    import hashlib
    from ai_arc_services import shared_publication
    receipt = {'status': 'pass', 'roles': {'created:service:shelters': 'service', 'child:layer:shelters': 2},
               'faultEvents': [{'kind': 'http_503_once', 'id': 'arc-p10-query-503'}]}
    link = shared_publication(receipt, b'retained', 'A')
    assert link == {'variant': 'A', 'service': 'service', 'layerId': 2,
                    'caseReceiptSha256': 'sha256:' + hashlib.sha256(b'retained').hexdigest()}
    for changed in ({**receipt, 'status': 'fail'}, {**receipt, 'faultEvents': []},
                    {**receipt, 'roles': {'created:service:shelters': 'foreign/route', 'child:layer:shelters': 2}}):
        with pytest.raises(ValueError):
            shared_publication(changed, b'retained', 'A')


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_town_setup_verifies_only_selected_fixture(monkeypatch, variant):
    import agent_e2e_local_scenarios.w2g3_common as common
    seen = []
    monkeypatch.setattr(common, 'verify_corpus', lambda root, manifest: seen.extend(r['path'] for r in manifest['files']))
    common.town_setup(object(), variant)
    assert len(seen) == 3 and all(path.startswith('town-' + variant + '/') for path in seen)


def test_adopted_sql_uses_matching_remote_project_and_one_quoted_sql_argument(tmp_path):
    import shlex
    from types import SimpleNamespace
    import ai_arc_services
    calls = []
    class Vm:
        project = 'arc-test'
        def ssh(self, script, timeout=300):
            calls.append(shlex.split(script))
            return SimpleNamespace(returncode=0, stdout=(b'abcdef012345\n' if len(calls) == 1 else b'[]\n'))
    runtime = ai_arc_services.runtime_class()(
        base_url='http://127.0.0.1:1', admin_key='unused', project='arc-test',
        candidate_digest='sha256:' + 'a' * 64, agent='codex-cli', model='test',
        limits={'maxTurns': 1, 'maxToolCalls': 1, 'maxSeconds': 1}, private_root=tmp_path, vm=Vm())
    verified = []
    runtime._ensure_reader = lambda stack: verified.append(stack)
    stack = object()
    sql = "SELECT 'a; b'"
    assert runtime.psql(stack, sql, read_only=True) == (0, b'[]\n')
    assert verified == [stack]
    assert 'label=com.docker.compose.project=arc-test' in calls[0]
    assert 'label=com.docker.compose.service=postgres' in calls[0]
    assert calls[1][-2:] == ['-c', sql]
    assert 'default_transaction_read_only=on' in ' '.join(calls[1])
    runtime.vm.project = 'foreign'
    with pytest.raises(ValueError, match='does not match'):
        runtime.psql(stack, sql, read_only=True)
    assert len(calls) == 2


def test_adopted_connection_is_created_under_controller_custody(tmp_path):
    from types import SimpleNamespace
    import ai_arc_services
    secret = 'fixture-database-secret'
    vm = SimpleNamespace(project='arc-test', secrets={'POSTGRES_PASSWORD': secret})
    runtime = ai_arc_services.runtime_class()(
        base_url='http://127.0.0.1:1', admin_key='unused', project='arc-test',
        candidate_digest='sha256:' + 'a' * 64, agent='codex-cli', model='test',
        limits={'maxTurns': 1, 'maxToolCalls': 1, 'maxSeconds': 1}, private_root=tmp_path, vm=vm)
    calls = []
    class Rec:
        def json(self, step, method, path, body):
            calls.append((step, method, path, body))
            return {'observation': {'httpStatus': 201, 'body': {'data': {'connectionId': 'conn'}}}}
    stack = SimpleNamespace(compose_project='arc-test-p10', extra={'recorder': Rec()})
    assert runtime.ensure_arc_connection(stack) == 'conn'
    assert calls[0][3]['host'] == 'postgres' and calls[0][3]['password'] == secret
    assert secret in runtime._secrets and secret in runtime.extra_secrets
    vm.project = 'foreign'
    with pytest.raises(ValueError):
        runtime.ensure_arc_connection(stack)
    assert len(calls) == 1


def test_fixture_adapter_runs_catalog_cases_and_binds_retained_receipt(monkeypatch, tmp_path):
    import hashlib
    import ai_arc_services
    seen = []
    class FakeRuntime:
        def __init__(self, **kwargs):
            self._secrets, self.extra_secrets, self.workdirs, self.answers = [], [], [], {}
            self.withheld = self.catalog_redactions = 0
            self.shared_service = None
    monkeypatch.setattr(ai_arc_services, 'runtime_class', lambda: FakeRuntime)
    def execute_case(*, scenario, variant, case, **kwargs):
        seen.append((scenario.id, scenario.expectation_profile, case['prompts'][variant]))
        if scenario.id == 'P10':
            return {'status': 'pass', 'roles': {'created:service:shelters': 'service',
                                                'child:layer:shelters': 2},
                    'faultEvents': [{'kind': 'http_503_once', 'id': 'arc-p10-query-503'}],
                    'sessions': [], 'assertions': [], 'nudges': 0}
        return {'status': 'pass', 'sessions': [], 'assertions': [], 'nudges': 0,
                'roles': {'seeded:service:shelters': 'service', 'seeded:layer:shelters': 2}}
    monkeypatch.setattr(runner, 'execute_case', execute_case)
    cipher = Fernet(Fernet.generate_key())
    payload = json.loads(cipher.decrypt(cipher.encrypt(json.dumps({
        'services': 'Synthetic runtime goal', 'fixtures': 'town-A', 'fault': 'http_503_once'}).encode())))
    row, cases = ai_arc_services.run_fixture_services(
        goal=payload['services'], fixture_id=payload['fixtures'], fault=payload['fault'],
        scenarios=('P13',), variant='B', candidate_digest='sha256:' + 'a' * 64,
        base_url='http://127.0.0.1:1', admin_key='unused', project='arc-test',
        arm={'agent': 'codex-cli', 'model': 'fake'}, limits={'maxTurns': 10, 'maxToolCalls': 10, 'maxSeconds': 60},
        out_dir=tmp_path / 'retained', private_root=tmp_path / 'private', cli=object(), vm=object())
    assert row['failure'] is None and [c['scenario'] for c in cases] == ['P10', 'P15']
    assert seen == [('P10', 'arc', 'Synthetic runtime goal'), ('P15', 'arc', 'Synthetic runtime goal')]
    p10 = (tmp_path / 'retained/P10/case-receipt.json').read_bytes()
    assert cases[1]['receipt']['priorCaseReceiptSha256'] == 'sha256:' + hashlib.sha256(p10).hexdigest()


def test_harness_custody_failure_does_not_become_an_agent_false_success(monkeypatch):
    import agent_e2e_handoff
    seen = []
    monkeypatch.setattr(agent_e2e_handoff, 'score_final_answer',
                        lambda answer, observations, state_status: seen.append(state_status) or {'status': 'pass'})
    rows = [{'assertion': 'publication.private', 'status': 'pass'},
            {'assertion': 'custody.bootstrap-revoked', 'status': 'fail'}]
    runner.score_final_answer('The map is shared.', {}, runner.agent_work_assertions('P15', rows))
    assert seen == ['pass']
    assert runner.agent_work_assertions('P10', rows) == rows


def test_adopted_database_identity_matches_quickstart_fixture():
    import yaml
    from pathlib import Path
    from ai_arc_local import quickstart_compose
    page = Path(__file__).parent / 'test_ai_arc_fixtures/quickstart.md'
    compose = yaml.safe_load(quickstart_compose(page.read_text()))
    assert 'postgres' in compose['services']
    env = compose['services']['honua']['environment']
    assert 'Host=postgres;Database=honua;Username=honua;' in env['ConnectionStrings__DefaultConnection']
