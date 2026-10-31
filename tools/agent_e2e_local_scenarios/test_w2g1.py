"""Offline controls; no full pack or live Docker is used."""
import copy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path

import pytest

from agent_e2e_local_api import Seeded, Stack

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('w2g1_oracle', ROOT / 'certification/agent-e2e/corpus/oracle.py')
oracle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oracle)


@pytest.fixture
def stack(tmp_path):
    return Stack('http://127.0.0.1:1', 'controller-only', None, 'fake', 'fake.yml', str(tmp_path), 'sha256:' + '0' * 64, {})


@pytest.mark.parametrize('scenario_id', ['P01', 'P02', 'P12', 'P24'])
def test_scenario_is_discoverable_and_not_enabled(scenario_id):
    module = importlib.import_module('agent_e2e_local_scenarios.' + scenario_id)
    case = module.scenario
    assert case.id == scenario_id and case.supported_variants == ()
    assert 'unsupported' in module.__doc__.lower()
    assert case.fault_plan('A') is None and case.fault_plan('B') is None
    path = ROOT / 'certification/agent-e2e/expectations/local' / (scenario_id + '.json')
    doc = json.loads(path.read_text())
    assert doc['schemaVersion'] == 1 and doc['scenario'] == scenario_id
    # Assertion IDs are frozen explicitly so absence cannot silently pass scoring.
    expected = {
        'P01': {'candidate.identity', 'deployment.ready', 'authentication.required', 'custody.bootstrap-revoked', 'custody.worker-replaced'},
        'P02': {'configuration.persisted', 'restart.observed', 'secrets.absent'},
        'P12': {'state.saved', 'restart.observed', 'state.reopened', 'source-content.unchanged'},
        'P24': {'candidate.identity', 'deployment.ready', 'features.exact', 'filter.exact', 'buffer.metric', 'map.style', 'state.reopened', 'approval.separate', 'publication.public', 'render.actual', 'custody.bootstrap-revoked', 'custody.worker-replaced', 'source-content.unchanged'},
    }[scenario_id]
    assert set(doc['variants']) == {'A', 'B'}
    assert all(set(value) == expected for value in doc['variants'].values())
    assert any(value is None for value in doc['variants']['A'].values())


def test_p02_records_real_order_and_refuses_to_grade_partial_evidence(stack):
    case = importlib.import_module('agent_e2e_local_scenarios.P02').scenario
    calls = []
    class Recorder:
        def json(self, step, method, path):
            calls.append((step, method, path))
            return {'observation': {'httpStatus': 200, 'body': {'success': True, 'data': []}}}
    def restart(prefix):
        calls.append(('restart', prefix))
        return {'restarted': True}
    stack.extra.update(recorder=Recorder(), restartServer=restart)
    seeded = case.seed(stack, 'A')
    assert case.seed(stack, 'A') == seeded
    with pytest.raises(ValueError):
        case.seed(stack, 'B')
    assert case.sessions_required() == 2
    assert case.between_sessions(stack, seeded, 'A', 1) == {'restartServer': True}
    observations = case.observe(stack, seeded, 'A')
    assert [row.assertion_id for row in observations] == ['configuration.persisted', 'restart.observed']
    assert [entry[0] for entry in calls] == ['p02-connections-seed', 'p02-connections-before-restart', 'p02-connections-after-restart']


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_p24_rejects_missing_real_fixture_features(variant):
    expected = oracle.fixture_expectation(variant, 'filter.exact')
    assert expected['resourceIds']
    oracle.verify('filter.exact', copy.deepcopy(expected), expected)
    wrong = copy.deepcopy(expected)
    wrong['resourceIds'].pop()
    with pytest.raises(oracle.VerificationError):
        oracle.verify('filter.exact', wrong, expected)


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_p12_rejects_altered_real_source_content(variant):
    expected = json.loads((ROOT / 'certification/agent-e2e/expectations/local/P10.json').read_text())['variants'][variant]['source-content.unchanged']
    correct = {'profile': expected['profile'], 'before': copy.deepcopy(expected['resources']), 'after': copy.deepcopy(expected['resources'])}
    oracle.verify('source-content.unchanged', correct, expected)
    wrong = copy.deepcopy(correct)
    wrong['after'][0]['content']['features'][0]['properties']['name'] = 'changed'
    with pytest.raises(oracle.VerificationError):
        oracle.verify('source-content.unchanged', wrong, expected)


def test_p02_rejects_a_changed_recorded_configuration():
    # These are fixed offline HTTP snapshots, not a model run or frozen scenario oracle.
    before = b'{"data":{"settings":{"storage":"durable"},"savedDataSourceIds":["fixture-source"]}}'
    after = b'{"data":{"settings":{"storage":"ephemeral"},"savedDataSourceIds":["fixture-source"]}}'
    digest = lambda value: hashlib.sha256(value).hexdigest()
    expected = {'configurationBeforeSha256': digest(before), 'configurationAfterSha256': digest(before), 'savedDataSourceIds': ['fixture-source']}
    oracle.verify('configuration.persisted', copy.deepcopy(expected), expected)
    wrong = {**expected, 'configurationAfterSha256': digest(after)}
    with pytest.raises(oracle.VerificationError):
        oracle.verify('configuration.persisted', wrong, expected)


def test_p01_rejects_a_failed_authenticated_read():
    # The offline HTTP fixture is independent of the live candidate.
    fixture = json.loads('{"deploymentId":"fixture-deployment","healthStatus":200,"authenticatedReadStatus":200}')
    oracle.verify('deployment.ready', copy.deepcopy(fixture), fixture)
    wrong = {**fixture, 'authenticatedReadStatus': 401}
    with pytest.raises(oracle.VerificationError):
        oracle.verify('deployment.ready', wrong, fixture)


def test_p12_requests_restart_between_fresh_sessions():
    case = importlib.import_module('agent_e2e_local_scenarios.P12').scenario
    assert case.sessions_required() == 2
    assert case.between_sessions(None, Seeded({}), 'A', 1) == {'restartServer': True}
    with pytest.raises(ValueError):
        case.between_sessions(None, Seeded({}), 'A', 2)


def test_restart_fact_is_not_a_lifecycle_observation():
    # Shape recorded by LocalRuntime.restart_server, not a made-up stopped state.
    from agent_e2e_local_api import Observation
    from agent_e2e_local_runner import score_assertions
    fact = {'command': 'docker compose restart server', 'exitCode': 0,
            'containersBefore': ['fixture-id 2026-01-01T00:00:00Z running'],
            'containersAfter': ['fixture-id 2026-01-01T00:01:00Z running'],
            'readinessSteps': ['between-1-ready-1'], 'ready': True, 'restarted': True}
    rows = score_assertions(['restart.observed'],
        [Observation('restart.observed', None, {'projection': 'extract/v1'})],
        {'restart.observed': {'resourceId': 'fixture-id', 'terminalState': 'running', 'requiredStates': ['stopped']}},
        records={}, custody={'restart:between-1': fact}, scenario_id='P12',
        frozen={'restart.observed': {'spec': {'custody': 'restart:between-1'}, 'reads': {}}})
    assert rows[0]['status'] == 'fail'


@pytest.mark.parametrize('scenario_id', ['P01', 'P24'])
def test_worker_exclusion_is_explicit_and_never_a_pass(scenario_id):
    from agent_e2e_local_runner import score_assertions
    document = json.loads((ROOT / 'certification/agent-e2e/expectations/local' / (scenario_id + '.json')).read_text())
    marker = document['variants']['A']['custody.worker-replaced']
    rows = score_assertions(['custody.worker-replaced'], [], {'custody.worker-replaced': marker},
                            records={}, scenario_id=scenario_id, frozen={})
    assert rows[0]['status'] == 'excluded'
    assert rows[0]['ruling'] == 'custody.worker-replaced@local-docker'


@pytest.mark.parametrize('scenario_id,prefix', [('P02', 'between-1'), ('P12', 'between-1')])
def test_frozen_restart_projection_passes_and_rejects_wrong_state(scenario_id, prefix):
    from agent_e2e_local_api import Observation
    from agent_e2e_local_runner import score_assertions
    document = json.loads((ROOT / 'certification/agent-e2e/expectations/local' / (scenario_id + '.json')).read_text())
    expected = {'restart.observed': document['variants']['A']['restart.observed']}
    frozen = {'restart.observed': document['projections']['restart.observed']}
    observation = Observation('restart.observed', None, {'projection': 'extract/v1'})
    def score(states):
        return score_assertions(['restart.observed'], [observation], expected, records={},
            custody={'lifecycle:' + prefix: {'resourceId': 'server', 'states': states}},
            scenario_id=scenario_id, frozen=frozen)[0]['status']
    assert score(['running', 'restarted', 'running', 'ready']) == 'pass'
    assert score(['running', 'running', 'ready']) == 'fail'


def test_protected_inventory_shape_cannot_satisfy_configuration_binding():
    # Core's protectedContent output is deliberately not re-labelled as digest fields.
    projected = {'profile': 'honua.protected-content/v1',
                 'before': [{'resourceId': 'connections', 'content': {'items': []}}],
                 'after': [{'resourceId': 'connections', 'content': {'items': []}}]}
    expected = {'configurationBeforeSha256': 'a' * 64,
                'configurationAfterSha256': 'a' * 64, 'savedDataSourceIds': ['fixture']}
    with pytest.raises(oracle.VerificationError):
        oracle.verify('configuration.persisted', projected, expected)


def test_p02_recorded_digest_prefix_mismatch_fails_closed():
    from agent_e2e_local_api import RoleBinding, Observation
    from agent_e2e_local_projections import resolve_roles
    from agent_e2e_local_runner import score_assertions
    from test_agent_e2e_local_runner import _record
    document = json.loads((ROOT / 'certification/agent-e2e/expectations/local/P02.json').read_text())
    config = {'data': [{'connectionId': 'fixture-id', 'createdBy': 'agent'}]}
    records = {
        'p02-connections-before-restart': _record('p02-connections-before-restart', phase='between', body=config, path='/api/v1/admin/connections/'),
        'p02-connections-after-restart': _record('p02-connections-after-restart', phase='observe', body=config, path='/api/v1/admin/connections/'),
        'p02-connections-seed': _record('p02-connections-seed', phase='seed', body={'data': []}, path='/api/v1/admin/connections/'),
    }
    roles = resolve_roles([RoleBinding(**row) for row in document['roles']], records, agent_principal='agent')
    rows = score_assertions(['configuration.persisted'],
        [Observation('configuration.persisted', None, {'projection': 'extract/v1'})],
        {'configuration.persisted': document['variants']['A']['configuration.persisted']},
        records=records, roles=roles, scenario_id='P02',
        frozen={'configuration.persisted': document['projections']['configuration.persisted']})
    assert rows[0]['status'] == 'fail'
    assert rows[0]['reason'] == 'value differs from frozen expectation'
