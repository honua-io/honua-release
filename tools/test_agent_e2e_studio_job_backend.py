"""Original MCP actions, native job GETs and saved Studio bytes join end to end."""
import copy
import json
import math
from types import SimpleNamespace

import pytest

from agent_e2e_job_observer import PROCESS, EXECUTE
from agent_e2e_native_job_bindings import PROFILE
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_scenario_backend import ScenarioHttp
from test_agent_e2e_studio_authoring_backend import session
from test_agent_e2e_studio_composition_observer import authoring, CREATE, SAVE
from test_agent_e2e_job_observer import native_job
from test_agent_e2e_job_map import saved_map
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server
from test_agent_e2e_mcp import result


@pytest.fixture
def job_session(session, authoring, adapter, server, request):
    studio = server.respond
    native = request.getfixturevalue('native_job')
    jobs = server.respond
    def respond(method, path, body):
        if path == '/mcp' and body:
            message = json.loads(body)
            if message.get('method') == 'tools/call' and message.get('params', {}).get('name') == EXECUTE:
                status, headers, raw = result(message, {'content': [], **native.worker['agentResult']['content']})
                return status, {'Content-Type': 'application/json', **headers}, raw
        if path.startswith(('/api/v1/admin/jobs/', '/ogc/processes/jobs/', '/api/geoprocessing/jobs/')):
            return jobs(method, path, body)
        return studio(method, path, body)
    server.respond = respond
    session.attempt_id = adapter.http.identity['attemptId']
    adapter.http = ScenarioHttp(durable=adapter.durable, context=adapter.http.context,
        endpoint=server.url, credential_env='SCENARIO_TEST_TOKEN', attempt_id=session.attempt_id+'-observer')
    session.config = {'managedJob': {'processId': PROCESS, 'requesterPrincipalId': native.binding['requesterPrincipalId'], 'mapLayerId': 'buffer'},
        'http': {'endpoint': adapter.http.client.endpoint, 'credentialKind': 'api-key'}}
    session.case = {'scenarioId': 'P11', 'assertions': ['job.completed', 'buffer.metric', 'map.composition', 'source-content.unchanged']}
    session.http = lambda *_: adapter.http
    session.retain('prepare-start', {'startedAt': native.binding['notBefore']})
    session.retain('job-binding', native.binding)
    session.retain('public-inputs', {'fixture': 'local-controlled-native-responses'})
    session.open_worker = lambda: None
    session.durable._fixture_identity = lambda _: session.key
    path = session.bundle/'expectations.json'
    original = json.loads(path.read_text())['cases'][session.context['caseId']]['assertions']['source-content.unchanged']
    case = {'assertions': {'job.completed': {'resourceId': {'$native': 'buffer.jobId'}, 'terminalState': 'Succeeded',
        'requiredStates': ['Running', 'Succeeded']}, 'buffer.metric': {'sourceId': 'shelter-first', 'center3857': [0, 0],
        'radiusMetres': 500, 'radialToleranceMetres': 0.01, 'expectedArea': math.pi*500*500, 'areaRelativeTolerance': 0.01},
        'map.composition': {'mapId': authoring.binding['itemId'], 'orderedLayerIds': ['buffer'], 'visibility': {'buffer': True},
            'extent': [-0.005, -0.005, 0.005, 0.005]}, 'source-content.unchanged': original},
        'nativeBindings': {'buffer': {'profile': PROFILE, 'fields': ['jobId'], 'relationships': {
            'processId': PROCESS, 'requesterPrincipalId': native.binding['requesterPrincipalId']}, 'observers': {'1': {
            'endpoint': adapter.http.client.endpoint, 'attemptId': adapter.http.identity['attemptId'], 'credentialKind': 'api-key'}}}}}
    path.write_text(json.dumps({'cases': {session.context['caseId']: case}}))
    def action(tool, arguments):
        selected, dispatch, worker = authoring.execute(tool, arguments, observe=False)
        def execute(actual, actual_dispatch, timeout):
            assert actual == selected and actual_dispatch == dispatch
            return worker
        session.execute_action = execute
        return session.action({'action': selected, 'dispatch': dispatch}, timeout=30)
    return session, native, action


def compose(job_session, authoring, *, save=True, wrong_source=False, missed_transition=False, wrong_metric=False):
    session, native, action = job_session
    created = authoring.execute(CREATE, authoring.create_args)
    session.retain('authoring-ordinal-'+str(created['ordinal']), created)
    if missed_transition:
        native.succeed()
    action(EXECUTE, native.action['input']['arguments'])
    native.succeed()
    if wrong_metric:
        native.state.document['features'][0]['geometry']['coordinates'][0][1][0] += 100
    layer = saved_map(native.state.document)['envelope']['body']['layers'][0]
    if wrong_source:
        layer['metadata']['honua']['source']['data']['features'][0]['properties']['source_id'] = 'different-shelter'
    action('honua_studio_add_layer', {'draftId': authoring.state.draft['draftId'], 'generation': authoring.state.draft['generation'], 'layer': layer})
    action('honua_studio_set_view', {'draftId': authoring.state.draft['draftId'], 'generation': authoring.state.draft['generation'],
        'view': {'bbox': [-0.005, -0.005, 0.005, 0.005], 'crs': 'EPSG:4326'}})
    if save:
        action(SAVE, {'draftId': authoring.state.draft['draftId'], 'generation': authoring.state.draft['generation']})


def test_original_job_and_model_saved_map_satisfy_unchanged_independent_predicates(job_session, authoring, server):
    compose(job_session, authoring)
    session, _, _ = job_session
    assert session.verify() == {'status': 'pass', 'failedAssertions': []}
    verified = session.value('verification')
    assert verified['unsupportedAssertions'] == []
    assert verified['observations']['job.completed'] == {'resourceId': 'gp-actual-job', 'states': ['Running', 'Succeeded']}
    assert session.value('job-saved-map')['savedVersionId'] == authoring.state.current
    assert session.value('job-saved-map')['nativeSrid'] == 3857
    assert session.value('job-saved-map')['savedSrid'] == 4326
    assert all(method == 'GET' for method, path, *_ in server.seen if path != '/mcp')


@pytest.mark.parametrize('mutation,failed', [('unsaved', ['map.composition']), ('wrong-source', ['map.composition']),
    ('changed-draft', ['map.composition']), ('wrong-metric', ['buffer.metric']),
    ('missed-transition', ['job.completed', 'buffer.metric', 'map.composition'])])
def test_actual_completed_job_cannot_substitute_for_missing_saved_map_or_failed_geometry(job_session, authoring, mutation, failed):
    compose(job_session, authoring, save=mutation != 'unsaved', wrong_source=mutation == 'wrong-source',
        missed_transition=mutation == 'missed-transition', wrong_metric=mutation == 'wrong-metric')
    if mutation == 'changed-draft':
        authoring.state.draft['envelope']['body']['layers'][0]['visible'] = False
    session, _, _ = job_session
    assert session.verify() == {'status': 'fail', 'failedAssertions': failed}
    assert session.value('source-before') is not None


def test_native_job_fingerprint_failure_stays_failed_even_when_model_saves_correct_geometry(job_session, authoring):
    session, native, _ = job_session
    native.state.detail['requestFingerprint'] = 'f'*64
    compose(job_session, authoring)
    assert session.verify()['failedAssertions'] == ['job.completed', 'buffer.metric', 'map.composition']
    assert session.value('job-final') is None


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'wrong-requester', 'wrong-process',
    'wrong-attempt', 'missing-metric', 'wrong-marker', 'unmapped', 'hidden', 'missing-configuration',
    'wrong-observer-endpoint', 'wrong-observer-attempt', 'wrong-observer-credential'])
def test_invalid_frozen_job_scope_refuses_before_any_prepare_or_worker_effect(job_session, mutation):
    session, _, _ = job_session
    path = session.bundle/'expectations.json'
    frozen = json.loads(path.read_text())
    case = frozen['cases'][session.context['caseId']]
    declaration = case['nativeBindings']['buffer']
    if mutation == 'missing':
        case['nativeBindings'] = {}
    elif mutation == 'duplicate':
        case['nativeBindings']['other'] = copy.deepcopy(declaration)
    elif mutation == 'wrong-requester':
        declaration['relationships']['requesterPrincipalId'] = 'another-requester'
    elif mutation == 'wrong-process':
        declaration['relationships']['processId'] = 'unreviewed-process'
    elif mutation == 'wrong-attempt':
        session.context['attempt'] = 2
    elif mutation == 'missing-metric':
        del case['assertions']['buffer.metric']
    elif mutation == 'wrong-marker':
        case['assertions']['job.completed']['resourceId'] = {'$native': 'unrelated.jobId'}
    elif mutation == 'unmapped':
        case['assertions']['map.composition']['orderedLayerIds'] = ['another-layer']
    elif mutation == 'hidden':
        case['assertions']['map.composition']['visibility']['buffer'] = False
    elif mutation == 'missing-configuration':
        session.config = {}
    elif mutation == 'wrong-observer-endpoint':
        declaration['observers']['1']['endpoint'] = 'https://another-candidate.example'
    elif mutation == 'wrong-observer-attempt':
        declaration['observers']['1']['attemptId'] = 'another-attempt-observer'
    elif mutation == 'wrong-observer-credential':
        declaration['observers']['1']['credentialKind'] = 'bearer'
    path.write_text(json.dumps(frozen))
    before = copy.deepcopy(session.durable.state)
    effects = []
    session.retain = lambda *args, **kwargs: effects.append('retain')
    session.observe_deployment = lambda: effects.append('deployment')
    session.start_worker = lambda **kwargs: effects.append('worker')
    with pytest.raises(PackError, match='managed|job'):
        session.prepare()
    assert effects == []
    assert session.durable.state == before


def test_exact_frozen_job_preflight_preserves_semantic_expectations(job_session):
    session, _, _ = job_session
    path = session.bundle/'expectations.json'
    before = path.read_bytes()
    actual = session.preflight_job_binding()
    assert actual == json.loads(before)['cases'][session.context['caseId']]
    assert path.read_bytes() == before


def cleanup_worker(session):
    """Exercise inherited cleanup through its ordinary stop/SQL/settlement path."""
    effects = []
    session.launch = {'taskArn': 'owned-worker'}
    session.lifecycle = SimpleNamespace(stop=lambda *_: effects.append('stop') or {'taskArn': 'owned-worker'},
        _task=lambda *_: pytest.fail('retained native GP obligations must prohibit settlement'))
    session.await_worker = lambda **_: {'taskArn': 'owned-worker', 'status': 'STOPPED'}
    session.prepare_cleanup_ownership = lambda: None
    session.cleanup_ownership = lambda: {'ownership': 'exact-sql-fixture'}
    session.database = SimpleNamespace(cleanup=lambda *_args, **_kwargs:
        effects.append('sql') or {'resourceIds': [], 'independentlyObserved': True})
    return effects


@pytest.mark.parametrize('status', [200, 403, 404, 503])
def test_sql_and_worker_cleanup_do_not_retire_a_native_job_or_settle_its_hold(job_session, authoring, server, status):
    session, native, _ = job_session
    compose(job_session, authoring)
    original = server.respond
    def respond(method, path, body):
        if path.endswith('/artifacts?limit=200'):
            return 200, {'Content-Type': 'application/json'}, json.dumps({'jobId': 'gp-actual-job',
                'items': [{'artifactId': 'retained-inline-output', 'availability': 'redacted'}], 'nextCursor': None}).encode()
        return original(method, path, body)
    server.respond = respond
    native.state.status = status
    effects = cleanup_worker(session)
    original_ledger = copy.deepcopy(session.durable.state['ledger'])
    observed = session.cleanup()
    assert effects == ['stop', 'sql']
    assert observed['status'] == 'blocked'
    assert observed['failures'] == [{'component': 'sql-cleanup', 'errorType': 'PackError'}]
    assert session.value('managed-job-sql-cleanup')['resourceIds'] == []
    remaining = session.value('managed-job-cleanup')
    assert remaining['resourceIds'] == ['honua://jobs/gp-actual-job']
    assert remaining['jobs'][0]['retirementObserved'] is False
    assert remaining['unresolvedExecutions'] == []
    if status == 200:
        assert remaining['jobs'][0]['artifactInventoryComplete'] is True
        assert remaining['jobs'][0]['artifacts'][0]['artifactId'] == 'retained-inline-output'
    else:
        assert remaining['jobs'][0]['observationErrorType'] == 'PackError'
    assert session.durable.state['ledger'] == original_ledger
    assert all(method == 'GET' for method, path, *_ in server.seen if path != '/mcp')


@pytest.mark.parametrize('condition', ['lost-reply', 'rejected-provenance', 'terminal-unknown-effect'])
def test_unknown_execution_is_an_obligation_even_without_a_verified_job_id(job_session, authoring, condition):
    session, native, _ = job_session
    if condition in ('lost-reply', 'terminal-unknown-effect'):
        action = native.action if condition == 'lost-reply' else {'name': 'terminal', 'input': {'command': 'submit-job'}}
        session.retain('model-action-1', {'action': action, 'dispatch': native.dispatch})
    else:
        native.state.detail['requestFingerprint'] = 'f'*64
        compose(job_session, authoring)
    cleanup_worker(session)
    assert session.cleanup()['status'] == 'blocked'
    remaining = session.value('managed-job-cleanup')
    assert remaining['resourceIds'] == []
    assert remaining['status'] == 'blocked'
    assert len(remaining['unresolvedExecutions']) == 1


def test_no_dispatched_gp_action_has_no_gp_cleanup_obligation(job_session):
    session, _, _ = job_session
    cleanup_worker(session)
    # Only the scoped fixture hook is called: no compute receipt is invented.
    assert session.cleanup_fixture({'ownership': 'exact-sql-fixture'})['resourceIds'] == []
    assert session.value('managed-job-cleanup') == {'status': 'pass',
        'reason': 'no-managed-execution-dispatched', 'resourceIds': [], 'jobs': [], 'unresolvedExecutions': []}
