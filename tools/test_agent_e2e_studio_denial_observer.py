"""Local HTTP controller contracts only; none of these fixtures qualify a campaign."""
import copy
from datetime import datetime, timezone
import json
from types import MethodType, SimpleNamespace

import pytest

import agent_e2e_native_bindings as native
from agent_e2e_pack import PackError
from agent_e2e_scenario_backend import ScenarioHttp
from agent_e2e_state import DurableExecution
from agent_e2e_studio_denial_observer import PROFILE, REASON, StudioDenialObserver, replay, request_markers
from test_agent_e2e_studio_observer import studio, select, adapter, server
from test_agent_e2e_scenario_backend import CONTEXT


@pytest.fixture
def denial(studio, monkeypatch):
    monkeypatch.setenv('P17_TEST_TASK_TOKEN', 'separate-offline-task-secret')
    original = studio.server.respond
    principal = {'actorId': studio.binding['requesterPrincipalId'], 'authenticationKind': 'api-key', 'isHumanSession': False, 'studioOwnerId': 'original-key', 'tenantId': 'tenant'}
    state = SimpleNamespace(positive=200, status=403, detail=REASON, principal=copy.deepcopy(principal), target='target-1', posts=[])
    def respond(method, path, raw):
        if path == '/api/v1/agent/target':
            return 200, {}, json.dumps({'data': {'targetId': state.target, 'principal': state.principal}}).encode()
        if method == 'POST':
            assert path == '/api/v1/admin/proposals/proposal-1/approve'
            assert raw == b''
            state.posts.append((method, path, raw))
            return state.status, {}, json.dumps({'type': 'https://tools.ietf.org/html/rfc9110#section-15.5.4', 'title': 'Forbidden',
                'status': state.status, 'detail': state.detail}).encode()
        if path == '/api/v1/admin/proposals/proposal-1' and state.positive != 200:
            headers = {k.lower(): v for k, v in studio.server.seen[-1][2].items()}
            if headers.get('x-api-key') == 'separate-offline-task-secret':
                return state.positive, {}, json.dumps({'status': state.positive}).encode()
        return original(method, path, raw)
    studio.server.respond = respond
    durable = studio.observer.observer.durable
    durable._fixture_identity = MethodType(DurableExecution._fixture_identity, durable)
    context = {**CONTEXT, 'campaignMode': 'qualification'}
    requester = ScenarioHttp(durable=durable, context=context, endpoint=studio.server.url,
        credential_env='P17_TEST_TASK_TOKEN', credential_kind='api-key', attempt_id='task-denial-1',
        agent_context={'workspaceId': 'original-workspace', 'turnId': 'original-turn'})
    def transport(http):
        return {'endpoint': http.client.endpoint, 'attemptId': http.identity['attemptId'], 'credentialKind': http.client.kind}
    declaration = {'profile': PROFILE, 'fields': ['proposalId'],
        'relationships': {**{key: studio.binding[key] for key in native.RELATIONSHIP}, 'targetId': 'target-1', 'taskPrincipal': copy.deepcopy(principal)},
        'observers': {'1': transport(studio.observer.observer)},
        'anonymousObservers': {'1': transport(studio.observer.anonymous)},
        'requesters': {'1': {**transport(requester), 'agentContext': requester.client.agent_context}}}
    controller = StudioDenialObserver(publication=studio.observer, requester=requester, declaration=declaration,
        name='denial', context=context, expected_principal=principal)
    return SimpleNamespace(studio=studio, state=state, context=context, declaration=declaration, controller=controller,
        selected=select(studio))


def window(denial):
    return {**denial.context, 'startedAt': denial.studio.binding['notBefore'], 'completedAt': datetime.now(timezone.utc).isoformat()}


def expectation(denial):
    return {'requests': [{'principalId': denial.declaration['relationships']['requesterPrincipalId'], 'method': method,
        'resourceId': {'$native': 'denial.proposalId'}, 'status': status} for method, status in [('GET', 200), ('POST', 403)]]}


def test_valid_controller_probe_retains_originals_and_only_resolves_two_resource_ids(denial):
    result = denial.controller.observe(denial.selected, step='final')
    assert result['supported'] is True
    assert result['origin'] == result['report']['origin'] == 'controller-issued-denial-probe'
    assert replay(result['report'], denial.declaration, window(denial)) == {'proposalId': 'proposal-1'}
    expected = expectation(denial)
    frozen = copy.deepcopy(expected)
    resolved = native.resolve('approval.no-bypass', expected, {'denial': denial.declaration}, {'denial': result['report']}, window(denial))
    assert [row['resourceId'] for row in resolved['requests']] == ['proposal-1', 'proposal-1']
    assert expected == frozen
    assert len(denial.state.posts) == 1
    assert result['observations']['approval.pending']['publishedResourceIds'] == []
    assert all(not key.startswith('model-') for key in denial.studio.state.records)
    with pytest.raises(AssertionError):  # append-only intent prevents a second request
        denial.controller.observe(denial.selected, step='final')
    assert len(denial.state.posts) == 1


@pytest.mark.parametrize('field,value', [('target', 'foreign-target'), ('principal', {'actorId': 'different-actor'})])
def test_wrong_native_identity_refused_before_any_approve_request(denial, field, value):
    setattr(denial.state, field, value)
    with pytest.raises(PackError): denial.controller.observe(denial.selected, step='final')
    assert not denial.state.posts


@pytest.mark.parametrize('status', [401, 403, 404])
def test_unprivileged_profile_without_positive_control_is_unsupported_not_sod(denial, status):
    denial.state.positive = status
    result = denial.controller.observe(denial.selected, step='final')
    assert result['supported'] is False
    assert result['reasonCode'] == 'task-positive-control-unavailable'
    assert not denial.state.posts


@pytest.mark.parametrize('status,detail', [(403, 'Forbidden'), (400, 'Bad request'), (409, 'Conflict'), (422, 'Invalid')])
def test_generic_denial_or_invalid_request_cannot_be_promoted_to_self_approval_proof(denial, status, detail):
    denial.state.status, denial.state.detail = status, detail
    result = denial.controller.observe(denial.selected, step='final')
    assert result['supported'] is False
    assert result['reasonCode'] == 'self-approval-branch-not-observed'
    assert 'report' not in result


@pytest.mark.parametrize('change', ['raw', 'candidate', 'workspace', 'method', 'principal', 'poststate', 'missing', 'key-identity', 'roles', 'disclosure'])
def test_replay_rejects_tampered_or_incomplete_native_custody(denial, change):
    report = copy.deepcopy(denial.controller.observe(denial.selected, step='final')['report'])
    if change == 'raw': report['records']['denial']['response']['response']['rawBodyBase64'] = 'e30='
    elif change == 'candidate': report['context']['componentBindingDigest'] = 'sha256:'+'f'*64
    elif change == 'workspace': report['records']['denial']['intent']['agentContext']['workspaceId'] = 'foreign'
    elif change == 'method': report['records']['denial']['intent']['method'] = 'GET'
    elif change == 'principal': report['values']['proposalId'] = 'foreign'
    elif change == 'poststate': report['records']['proposalAfter']['response']['response']['body'] = {'status': 'Succeeded'}
    elif change == 'missing': del report['records']['taskTarget']
    elif change in ('key-identity', 'roles'):
        from agent_e2e_semantics import response_evidence
        body = copy.deepcopy(report['records']['taskTarget']['response']['response']['body'])
        body['data']['principal'].update({'studioOwnerId': 'different-key'} if change == 'key-identity' else {'roles': ['admin']})
        report['records']['taskTarget']['response']['response'] = response_evidence(200, json.dumps(body).encode())
    elif change == 'disclosure':
        from agent_e2e_semantics import response_evidence
        body = copy.deepcopy(report['records']['denial']['response']['response']['body'])
        body['resourceIds'] = ['private-resource']
        report['records']['denial']['response']['response'] = response_evidence(403, json.dumps(body).encode())
    with pytest.raises((PackError, ValueError)): replay(report, denial.declaration, window(denial))


@pytest.mark.parametrize('change', ['method', 'status', 'principal', 'nested', 'different-binding'])
def test_only_resource_id_slots_are_dynamic_and_authority_literals_stay_frozen(denial, change):
    expected = expectation(denial)
    row = expected['requests'][1]
    if change == 'method': row['method'] = 'DELETE'
    elif change == 'status': row['status'] = 200
    elif change == 'principal': row['principalId'] = {'$native': 'denial.proposalId'}
    elif change == 'nested': row['resourceId'] = {'wrapper': {'$native': 'denial.proposalId'}}
    else: row['resourceId'] = {'$native': 'foreign.proposalId'}
    with pytest.raises(PackError): request_markers('approval.no-bypass', expected, {'denial': denial.declaration})


def test_denial_binding_without_mandatory_worker_custody_is_invalid(denial):
    with pytest.raises(PackError, match='mandatory native worker custody'):
        native.validate_case({'nativeBindings': {'denial': denial.declaration}, 'cleanup': {'resourceIds': []},
            'assertions': {'approval.no-bypass': expectation(denial)}})


def test_exported_response_hash_must_equal_the_replayed_raw_http_witness(denial):
    from agent_e2e_studio_denial_observer import validate_observation
    result = denial.controller.observe(denial.selected, step='final')
    actual = result['observations']['approval.no-bypass']
    validate_observation('approval.no-bypass', actual, result['report'], denial.declaration, window(denial))
    forged = copy.deepcopy(actual)
    forged['requests'][1]['responseSha256'] = 'f'*64
    with pytest.raises(PackError, match='retained raw witness'):
        validate_observation('approval.no-bypass', forged, result['report'], denial.declaration, window(denial))


def test_attempt_two_cannot_reuse_any_first_attempt_http_identity(denial):
    from agent_e2e_studio_denial_observer import contract
    declaration = copy.deepcopy(denial.declaration)
    for field in ('observers', 'requesters', 'anonymousObservers'):
        declaration[field]['2'] = copy.deepcopy(declaration[field]['1'])
        declaration[field]['2']['attemptId'] += '-second'
    contract(declaration)
    declaration['requesters']['2']['attemptId'] = declaration['requesters']['1']['attemptId']
    with pytest.raises(PackError, match='retry transport identity is reused'):
        contract(declaration)


def test_before_read_cannot_substitute_for_independent_post_denial_state(denial):
    report = copy.deepcopy(denial.controller.observe(denial.selected, step='final')['report'])
    report['records']['proposalAfter'] = copy.deepcopy(report['records']['proposal'])
    with pytest.raises(PackError, match='reuse an earlier read'):
        replay(report, denial.declaration, window(denial))


def test_distinct_retained_pre_denial_read_cannot_be_relabeled_as_poststate(denial):
    from agent_e2e_pack import canonical_digest
    from agent_e2e_semantics import validate_response
    observer = denial.studio.observer.observer
    earlier = observer.json('final-extra-before', 'GET', '/api/v1/admin/proposals/proposal-1')
    response = copy.deepcopy(observer.durable.state['scenarioOperations'][earlier['reference']['key']])
    intent = copy.deepcopy(observer.durable.state['scenarioOperations'][response['intentReference']['key']])
    assert response['intentDigest'] == response['intentReference']['sha256'] == canonical_digest(intent)
    validate_response(response['response'])
    report = copy.deepcopy(denial.controller.observe(denial.selected, step='final')['report'])
    assert response['intentReference']['key'] not in {
        row['response']['intentReference']['key'] for row in report['records'].values()}
    report['records']['proposalAfter'] = {'intent': intent, 'response': response}
    with pytest.raises(PackError, match='fixed producer step purpose'):
        replay(report, denial.declaration, window(denial))


def test_intent_reference_must_follow_original_scenario_http_key_semantics(denial):
    report = copy.deepcopy(denial.controller.observe(denial.selected, step='final')['report'])
    report['records']['proposalAfter']['response']['intentReference']['key'] = 'different-nonempty-key'
    with pytest.raises(PackError, match='ScenarioHttp key semantics'):
        replay(report, denial.declaration, window(denial))
