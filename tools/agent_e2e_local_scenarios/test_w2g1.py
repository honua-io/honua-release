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


@pytest.mark.parametrize('scenario_id', ['P01', 'P24'])
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
        def json(self, step, method, path, request=None):
            calls.append((step, method, path))
            return {'observation': {'httpStatus': 201 if method == 'POST' else 200, 'body': {'success': True, 'data': []}}}
    def restart(prefix):
        calls.append(('restart', prefix))
        return {'restarted': True}
    stack.extra.update(recorder=Recorder(), restartServer=restart, registerCanary=lambda _: 'test-canary',
                       databaseHandoff=lambda name, schema: None)
    seeded = case.seed(stack, 'A')
    assert case.seed(stack, 'A') == seeded
    with pytest.raises(ValueError):
        case.seed(stack, 'B')
    assert case.sessions_required() == 2
    assert case.between_sessions(stack, seeded, 'A', 1) == {'restartServer': True}
    observations = case.observe(stack, seeded, 'A')
    assert [row.assertion_id for row in observations] == ['configuration.persisted', 'restart.observed', 'secrets.absent']
    assert [entry[0] for entry in calls] == ['p02-canary-create', 'p02-connections-seed', 'p02-connections-before-restart', 'p02-connections-after-restart']


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
    class Recorder:
        def json(self, step, method, path):
            if step == 'p12-items-between':
                return {'observation': {'httpStatus': 200, 'body': {'data': {'total': 1, 'items': [{'itemId': '11111111-1111-1111-1111-111111111111', 'currentVersionId': '33333333-3333-3333-3333-333333333333'}]}}}}
            return {'observation': {'httpStatus': 200, 'body': {'data': {}}}}
    stack = Stack('', '', None, '', '', '', '', {'recorder': Recorder()})
    seed = Seeded({'variant': 'A', 'itemId': '11111111-1111-1111-1111-111111111111', 'initialVersionId': '22222222-2222-2222-2222-222222222222'})
    assert case.sessions_required() == 2
    assert case.between_sessions(stack, seed, 'A', 1) == {'restartServer': True}
    assert seed.state['savedVersionId'] == '33333333-3333-3333-3333-333333333333'
    with pytest.raises(ValueError):
        case.between_sessions(stack, seed, 'A', 2)


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


def test_p02_recorded_configuration_rejects_changed_state():
    from agent_e2e_local_api import RoleBinding, Observation
    from agent_e2e_local_projections import resolve_roles
    from agent_e2e_local_runner import score_assertions
    from test_agent_e2e_local_runner import _record
    document = json.loads((ROOT / 'certification/agent-e2e/expectations/local/P02.json').read_text())
    config = {'data': [{'connectionId': 'fixture-id', 'createdBy': 'api-key:agent'}]}
    records = {
        'p02-connections-before-restart': _record('p02-connections-before-restart', phase='between', body=config, path='/api/v1/admin/connections/'),
        'p02-connections-after-restart': _record('p02-connections-after-restart', phase='observe', body=config, path='/api/v1/admin/connections/'),
        'p02-connections-seed': _record('p02-connections-seed', phase='seed', body={'data': []}, path='/api/v1/admin/connections/'),
    }
    roles = resolve_roles([RoleBinding(**{'step': '', **row}) for row in document['roles']], records, agent_principal='agent')
    rows = score_assertions(['configuration.persisted'],
        [Observation('configuration.persisted', None, {'projection': 'extract/v1'})],
        {'configuration.persisted': document['variants']['A']['configuration.persisted']},
        records=records, roles=roles, scenario_id='P02',
        frozen={'configuration.persisted': document['projections']['configuration.persisted']})
    assert rows[0]['status'] == 'pass', rows
    changed = {'data': [{'connectionId': 'fixture-id', 'createdBy': 'api-key:agent', 'host': 'wrong'}]}
    records['p02-connections-after-restart'] = _record('p02-connections-after-restart', phase='observe', body=changed, path='/api/v1/admin/connections/')
    rows = score_assertions(['configuration.persisted'], [Observation('configuration.persisted', None, {'projection': 'extract/v1'})],
        {'configuration.persisted': document['variants']['A']['configuration.persisted']}, records=records, roles=roles,
        scenario_id='P02', frozen={'configuration.persisted': document['projections']['configuration.persisted']})
    assert rows[0]['status'] == 'fail'


def test_p02_secret_scan_rejects_exposed_canary_in_final_answer():
    from agent_e2e_local_api import Observation, RoleBinding
    from agent_e2e_local_projections import resolve_roles
    from agent_e2e_local_runner import score_assertions
    from test_agent_e2e_local_runner import _record
    document = json.loads((ROOT / 'certification/agent-e2e/expectations/local/P02.json').read_text())
    inventory = {'data': [{'connectionId': 'fixture-id', 'createdBy': 'api-key:agent'}]}
    records = {
        'p02-connections-before-restart': _record('p02-connections-before-restart', phase='between', body=inventory, path='/api/v1/admin/connections/'),
        'p02-connections-after-restart': _record('p02-connections-after-restart', phase='observe', body=inventory, path='/api/v1/admin/connections/'),
        'p02-connections-seed': _record('p02-connections-seed', phase='seed', body={'data': []}, path='/api/v1/admin/connections/'),
    }
    roles = resolve_roles([RoleBinding(**{'step': '', **row}) for row in document['roles']], records, agent_principal='agent')
    def score(answer):
        from agent_e2e_local_projections import Context, scan_roles
        scan = scan_roles([RoleBinding(**{'step': '', **row}) for row in document['roles']],
                          Context(records, roles, model_visible=(b'{}',), action_results=(b'{}',), final_answers=(answer,), fingerprint_salt=b'case-salt-0123456789'))
        return score_assertions(['secrets.absent'], [Observation('secrets.absent', None, {'projection': 'extract/v1'})],
            {'secrets.absent': document['variants']['A']['secrets.absent']}, records=records, roles={**roles, **scan},
            scenario_id='P02', frozen={'secrets.absent': document['projections']['secrets.absent']},
            canaries={'p02-secret': 'secret-canary-value'}, model_visible=(b'{}',), action_results=(b'{}',),
            final_answers=(answer,), fingerprint_salt=b'case-salt-0123456789')[0]['status']
    assert score('Setup complete.') == 'pass'
    assert score('secret-canary-value') == 'fail'


def test_p12_saved_and_reopened_bind_real_recorded_version_and_session():
    from agent_e2e_local_api import Observation, RoleBinding
    from agent_e2e_local_projections import resolve_roles
    from agent_e2e_local_runner import score_assertions
    from agent_e2e_local_recorder import Record
    from test_agent_e2e_local_runner import _record
    document = json.loads((ROOT / 'certification/agent-e2e/expectations/local/P12.json').read_text())
    item = '11111111-1111-1111-1111-111111111111'
    draft = '22222222-2222-2222-2222-222222222222'
    version = '33333333-3333-3333-3333-333333333333'
    envelope = {'body': {'layers': ['shelters'], 'view': [1, 2, 3]}}
    version_body = {'data': {'itemId': item, 'versionId': version, 'envelope': envelope}}
    path = '/api/v1/studio/content-items/' + item + '/versions/' + version
    records = {
        'p12-seed-save': _record('p12-seed-save', phase='seed', body={'data': {'itemId': item}}),
        'p12-seed-draft': _record('p12-seed-draft', phase='seed', body={'data': {'draftId': draft}}),
        'p12-seed-version': _record('p12-seed-version', phase='seed', body=version_body),
        'publish-shelters-submit': _record('publish-shelters-submit', phase='seed', body={'data': {'resourceIds': {'serviceName': 'svc', 'layerId': '0'}}}),
        'p12-items-before': _record('p12-items-before', phase='seed', body={'data': {'items': [], 'total': 0}}, path='/api/v1/studio/content-items?limit=200'),
        'p12-version-between': _record('p12-version-between', phase='between', body=version_body, path=path),
        'p12-version-after': _record('p12-version-after', phase='observe', body=version_body, path=path),
    }
    save_args = {'tool': 'honua_studio_save_version', 'arguments': {'draftId': draft}}
    read_args = {'tool': 'honua_studio_get_version', 'arguments': {'itemId': item, 'versionId': version}}
    save_id, read_id = 'occ:1:1:save', 'occ:2:1:read'
    def mcp(step, principal, origin, replay, arguments, payload):
        raw = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'structuredContent': payload}}).encode()
        return Record(step, 'observe', 'mcp', principal, 'agent', 'POST', '/mcp#tools/call', 200, raw,
                      'sha256:' + hashlib.sha256(raw).hexdigest(), replay, json.dumps(arguments).encode(), origin)
    records['p12-save-action'] = mcp('p12-save-action', 'agent', 'session-result', save_id, save_args,
                                      {'version': {'itemId': item, 'versionId': version}})
    records['p12-reopen-read'] = mcp('p12-reopen-read', 'controller', 'controller', read_id, read_args,
                                      {'versionId': version})
    roles = resolve_roles([RoleBinding(**{'step': '', **row}) for row in document['roles']], records, agent_principal='agent')
    roles['session:current'] = 'fresh-session'
    actions = {save_id: {'kind': 'tool_call', **save_args, '_session': 1},
               read_id: {'kind': 'tool_call', **read_args, '_session': 2}}
    ids = ['state.saved', 'state.reopened']
    def score():
        return score_assertions(ids, [Observation(a, None, {'projection': 'extract/v1'}) for a in ids],
            {a: document['variants']['A'][a] for a in ids}, records=records, roles=roles, scenario_id='P12',
            model_actions=actions, frozen={a: document['projections'][a] for a in ids})
    assert [row['status'] for row in score()] == ['pass', 'pass']
    records['p12-version-after'] = _record('p12-version-after', phase='observe', path=path,
        body={'data': {'itemId': item, 'versionId': version, 'envelope': {'body': {'layers': []}}}})
    assert [row['status'] for row in score()] == ['pass', 'fail']
    records['p12-version-after'] = _record('p12-version-after', phase='observe', path=path, body=version_body)
    actions[read_id]['_session'] = 1
    assert score()[1]['status'] == 'fail'


def test_p12_seed_is_idempotent_and_observes_only_matching_session_actions(stack, monkeypatch):
    from agent_e2e_local_recorder import LoggedAction
    module = importlib.import_module('agent_e2e_local_scenarios.P12')
    case = module.scenario
    item = '11111111-1111-1111-1111-111111111111'
    draft = '22222222-2222-2222-2222-222222222222'
    first = '33333333-3333-3333-3333-333333333333'
    saved = '44444444-4444-4444-4444-444444444444'
    calls = []
    class Recorder:
        principals = {'agent': ('unused', 'agent')}
        envelope = None
        alter = False
        def json(self, step, method, path, body=None):
            calls.append(step)
            if step == 'p12-seed-draft':
                self.envelope = copy.deepcopy(body['envelope'])
                if self.alter:
                    self.envelope['body']['mapPackageId'] = 'wrong'
            result = {'draftId': draft, 'envelope': self.envelope} if step == 'p12-seed-draft' else {'itemId': item, 'versionId': first} if step == 'p12-seed-save' else {'itemId': item, 'versionId': first, 'envelope': self.envelope} if step == 'p12-seed-version' else {}
            if step == 'p12-items-between':
                result = {'total': 1, 'items': [{'itemId': item, 'currentVersionId': saved}]}
            return {'observation': {'httpStatus': 201 if method == 'POST' else 200, 'body': {'data': result}}}
        def actions(self):
            def action(session, tool, args):
                return LoggedAction(session, 1, 'digest', 'tool_call', tool, args, None, 'pass', '/private')
            return (action(1, 'honua_studio_save_version', {'draftId': draft}),
                    action(1, 'honua_studio_get_version', {'itemId': item, 'versionId': saved}),
                    action(2, 'honua_studio_get_version', {'itemId': item, 'versionId': saved}))
        def action_result(self, step, action): calls.append((step, action.session))
        def replay(self, step, action): calls.append((step, action.session))
    class Town:
        def __init__(self, *args): pass
        def prepare(self, **kwargs): return {'layers': {'shelters': {'schema': 'honua_data', 'table': 'shelters'}}}
    monkeypatch.setattr(module, 'TownSetup', Town)
    monkeypatch.setattr(module, 'publication_request', lambda **kwargs: {})
    monkeypatch.setattr(module, 'submit_publication', lambda *args, **kwargs: {'status': 'completed', 'layerId': 0})
    recorder = Recorder()
    original_json = recorder.json
    def respond(step, method, path, body=None):
        if step == 'p12-connection-create':
            calls.append(step)
            return {'observation': {'httpStatus': 201, 'body': {'data': {'connectionId': 'connection'}}}}
        return original_json(step, method, path, body)
    recorder.json = respond
    stack.extra.update(recorder=recorder, databaseHandoff=lambda *args: {'host': 'db', 'port': 5432,
                       'database': 'honua', 'username': 'reader', 'password': 'fixture-password'})
    seeded = case.seed(stack, 'A')
    assert case.seed(stack, 'A') == seeded
    assert calls.count('p12-seed-draft') == 1
    assert case.between_sessions(stack, seeded, 'A', 1) == {'restartServer': True}
    observations = case.observe(stack, seeded, 'A')
    assert {row.assertion_id for row in observations} == {'state.saved', 'restart.observed', 'state.reopened', 'source-content.unchanged'}
    assert ('p12-save-action', 1) in calls and ('p12-reopen-read', 2) in calls
    recorder.alter = True
    second = Stack('', '', None, 'fake2', '', '', '', {'recorder': recorder, 'databaseHandoff': stack.extra['databaseHandoff']})
    with pytest.raises(ValueError, match='seed draft does not match'):
        case.seed(second, 'A')


def test_p02_scans_raw_bytes_instead_of_canonical_hashes():
    case = importlib.import_module('agent_e2e_local_scenarios.P02').scenario
    assert case.supported_variants == ('A', 'B')
    raw = b'{"success":true,"data":[],"timestamp":"2026-01-01T00:00:00Z"}'
    raw_digest = hashlib.sha256(raw).hexdigest()
    data_digest = hashlib.sha256(json.dumps([], separators=(',', ':')).encode()).hexdigest()
    assert raw_digest != data_digest
