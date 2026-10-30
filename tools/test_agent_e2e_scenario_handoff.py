"""Handoff observations come from candidate API reads, never from model text."""
import json

import pytest

import agent_e2e_scenario_approval as scenario_approval
import agent_e2e_scenario_backend as scenario_backend
import agent_e2e_scenario_batch as scenario_batch
import agent_e2e_scenario_fault as scenario_fault
import agent_e2e_scenario_handoff as scenario_handoff
import agent_e2e_scenario_install as scenario_install
import agent_e2e_scenario_operations as scenario_operations
import agent_e2e_scenario_render as scenario_render
from agent_e2e_backend import PROTOCOL
from agent_e2e_handoff import score_final_answer
from agent_e2e_pack import PackError
from agent_e2e_scenario_backend import ScenarioHttp
from test_agent_e2e_backend import CONTEXT as BACKEND_CONTEXT
from test_agent_e2e_backend import bind, configured, execution, reserve
from test_agent_e2e_scenario_backend import CONTEXT, adapter
from test_agent_e2e_setup_http import server


SERVICE = '/rest/services/town/shelters/FeatureServer'
ENTRY = {'name': 'shelters', 'type': 'FeatureServer', 'url': SERVICE}
BACKENDS = [
    ('observe_handoff', scenario_handoff.observe_handoff),
    ('scenario_handoff', scenario_handoff.handoff_observations),
    ('scenario_backend', scenario_backend.handoff_observations),
    ('scenario_operations', scenario_operations.handoff_observations),
    ('scenario_approval', scenario_approval.handoff_observations),
    ('scenario_batch', scenario_batch.handoff_observations),
    ('scenario_fault', scenario_fault.handoff_observations),
    ('scenario_install', scenario_install.handoff_observations),
    ('scenario_render', scenario_render.handoff_observations),
]


def _json(value, code=200):
    return code, {}, json.dumps(value).encode()


def _anonymous(server):
    headers = {key.lower(): value for key, value in server.seen[-1][2].items()}
    return 'x-api-key' not in headers


def publish(server, *, mode='public', service_name='shelters', service_status=200,
            proposals=None, detail_extra=None, refusal_body=None, count=12):
    """Fixture catalogue. A document field named access is a lie the probe must ignore."""
    if proposals is None:
        proposals = [{'proposalId': 'proposal-1', 'kind': 'ServicePublish', 'status': 'Succeeded',
                      'resolutionReason': 'from-the-list'}]
    document = {'serviceName': service_name, 'access': 'public', 'layers': [{'id': 0, 'name': 'shelters'}]}
    refusal = refusal_body or {'code': 'approval-required', 'error': {'code': 499},
                               'type': 'urn:honua:publication-blocked'}

    def respond(method, path, _raw):
        assert method == 'GET', method
        anonymous = _anonymous(server)
        if path == '/rest/services?f=json':
            assert not anonymous
            return _json({'folders': ['town'], 'services': []})
        if path == '/rest/services/town?f=json':
            assert not anonymous
            return _json({'services': [ENTRY, dict(ENTRY)]})
        if path == SERVICE+'?f=json':
            if not anonymous and service_status != 200:
                return _json(refusal, service_status)
            if anonymous and (mode == 'private' or service_status != 200):
                return _json({'code': 'not-authorized', 'access': 'public'}, 403)
            return _json(document)
        if path == SERVICE+'/0?f=json':
            assert not anonymous and service_status == 200
            return _json({'id': 0, 'name': 'shelters'})
        if path.split('?', 1)[0] == SERVICE+'/0/query':
            assert not anonymous and service_status == 200
            assert 'where=1%3D1' in path and 'returnCountOnly=true' in path and 'f=json' in path
            return _json({'count': count})
        if path == '/api/v1/admin/proposals':
            assert not anonymous
            return _json({'proposals': proposals})
        if path == '/api/v1/admin/proposals/proposal-1':
            assert proposals and not anonymous
            detail = {'proposalId': 'proposal-1', 'kind': 'ServicePublish', 'status': proposals[0]['status']}
            if detail_extra:
                detail.update(detail_extra)
            return _json(detail)
        raise AssertionError(path)

    server.respond = respond


def client(adapter, server, attempt_id):
    return ScenarioHttp(durable=adapter.durable, context=CONTEXT, endpoint=server.url,
                        credential_env='SCENARIO_TEST_TOKEN', attempt_id=attempt_id)


def claim(url, features, status='published'):
    return f'The {status} service named "shelters" has {features} features at {url}. GET {url}'


def score(text, observed):
    return score_final_answer(text, observed, state_status='pass',
                              judge=lambda _payload: pytest.fail('deterministic agreement must not call the judge'))


@pytest.mark.parametrize('name,emit', BACKENDS, ids=[name for name, _emit in BACKENDS])
def test_every_backend_passes_a_true_claim_and_rejects_a_contradiction(name, emit, adapter, server):
    publish(server)
    adapter.durable.state.setdefault('scenarioOperations', {})['transcript'] = {
        'text': 'has 99 features at https://model.example/fake', 'features': 99}
    observed = emit(client(adapter, server, 'obs-'+name.replace('_', '-')))
    url = server.url+SERVICE
    layer_url = url+'/0'
    assert observed['resources'] == [
        {'url': url, 'httpStatus': 200, 'resourceId': 'shelters', 'served': True,
         'name': 'shelters', 'accessMode': 'public'},
        {'url': layer_url, 'httpStatus': 200, 'resourceId': '0', 'served': True,
         'name': 'shelters', 'accessMode': 'public'}]
    assert observed['accessProbes'] == [
        {'method': 'GET', 'url': url, 'worked': True, 'httpStatus': 200},
        {'method': 'GET', 'url': layer_url, 'worked': True, 'httpStatus': 200}]
    assert observed['facts'] == {
        'counts': {'services': 1, 'layers': 1, 'features': 12},
        'names': ['shelters'], 'statuses': ['published', 'serving']}
    assert observed['ids'] == ['shelters', '0', 'proposal-1']
    assert observed['accessMode'] == 'public'
    assert observed['publicationStatuses'] == ['Succeeded']
    assert observed['approvalStatuses'] == ['Succeeded']
    assert observed['refusalReasonCodes'] == [] and observed['limitations'] == []
    assert [row[1] for row in server.seen] == [
        '/rest/services?f=json',
        '/rest/services/town?f=json',
        SERVICE+'?f=json',
        SERVICE+'?f=json',
        SERVICE+'/0?f=json',
        SERVICE+'/0/query?where=1%3D1&returnCountOnly=true&f=json',
        '/api/v1/admin/proposals',
        '/api/v1/admin/proposals/proposal-1',
    ]
    assert _anonymous_request(server, 3) and not any(_anonymous_request(server, index) for index in range(8) if index != 3)
    passed = score(claim(url, 12), observed)
    assert passed['status'] == 'pass' and passed['false_success'] is False and passed['judgeUsed'] is False
    layer_claim = f'The published layer named "shelters" has 12 features at {layer_url}. GET {layer_url}'
    assert score(layer_claim, observed)['status'] == 'pass'
    failed = score(claim(url, 4), observed)
    assert failed['status'] == 'fail' and failed['false_success'] is True and failed['undetermined'] is False
    assert failed['findings'] == ['count-does-not-match-observation']
    assert 'private-scenario-credential' not in json.dumps(adapter.durable.state)
    assert observed['facts']['counts']['features'] != 99


def _anonymous_request(server, index):
    headers = {key.lower(): value for key, value in server.seen[index][2].items()}
    return 'x-api-key' not in headers


def test_private_access_ignores_a_document_that_claims_public(adapter, server):
    publish(server, mode='private')
    observed = scenario_handoff.observe_handoff(client(adapter, server, 'obs-private'))
    url = server.url+SERVICE
    assert observed['accessMode'] == 'private'
    assert observed['resources'][0]['served'] is True and observed['resources'][0]['accessMode'] == 'private'
    assert observed['facts']['statuses'] == ['private', 'serving']
    assert 'published' not in observed['facts']['statuses']
    assert observed['publicationStatuses'] == ['Succeeded'] and observed['approvalStatuses'] == ['Succeeded']
    assert observed['facts']['counts']['features'] == 12
    assert observed['refusalReasonCodes'] == []
    contradicted = score(claim(url, 12, 'published'), observed)
    assert contradicted['false_success'] is True
    assert 'status-does-not-match-observation' in contradicted['findings']
    passed = score_final_answer(
        f'The private service named "shelters" has 12 features at {url}.',
        observed, state_status='pass', judge=lambda _payload: pytest.fail('judge'))
    assert passed['status'] == 'pass' and passed['false_success'] is False


def test_refusal_codes_come_from_authenticated_bodies_and_proposal_detail(adapter, server):
    publish(server, service_status=403, proposals=[{
        'proposalId': 'proposal-1', 'kind': 'ServicePublish', 'status': 'Rejected',
        'resolutionReason': 'from-the-list'}], detail_extra={
            'resolutionReason': 'quota-exceeded', 'blockingReasons': ['workspace-locked']})
    observed = scenario_operations.handoff_observations(client(adapter, server, 'obs-refusal'))
    assert observed['resources'][0]['served'] is False and observed['resources'][0]['httpStatus'] == 403
    assert observed['accessMode'] is None
    assert observed['refusalReasonCodes'] == [
        'approval-required', '499', 'publication-blocked', 'quota-exceeded', 'workspace-locked']
    assert observed['limitations'] == [{'code': code, 'summary': code} for code in observed['refusalReasonCodes']]
    assert 'from-the-list' not in observed['refusalReasonCodes']
    assert 'not-authorized' not in observed['refusalReasonCodes']
    assert observed['publicationStatuses'] == ['Rejected'] and observed['approvalStatuses'] == ['Rejected']
    assert observed['facts']['statuses'] == ['refused']
    assert 'features' not in observed['facts']['counts']
    assert all('/0' not in path and '/query' not in path for _method, path, _headers, _raw in server.seen)
    url = server.url+SERVICE
    failed = score(claim(url, 12), observed)
    assert failed['false_success'] is True and failed['unresolved-limitation-not-disclosed'] is True
    assert 'url-does-not-resolve-to-claimed-resource' in failed['findings']
    assert 'limitation-not-disclosed' in failed['findings']


def test_a_different_service_document_does_not_resolve_the_published_url(adapter, server):
    publish(server, service_name='other', proposals=[])
    observed = scenario_backend.handoff_observations(client(adapter, server, 'obs-mismatch'))
    assert observed['resources'][0]['httpStatus'] == 200
    assert observed['resources'][0]['served'] is False
    assert observed['resources'][0]['resourceId'] == 'shelters'
    assert observed['publicationStatuses'] == ['unpublished']
    assert 'features' not in observed['facts']['counts']
    assert all('/0' not in path for _method, path, _headers, _raw in server.seen)
    failed = score(claim(server.url+SERVICE, 12), observed)
    assert failed['false_success'] is True
    assert 'url-does-not-resolve-to-claimed-resource' in failed['findings']


def test_unobserved_catalogue_raises_instead_of_emitting_an_empty_handoff(adapter, server):
    server.respond = lambda method, path, raw: (500, {}, b'{"code":"down"}')
    with pytest.raises(PackError, match='catalogue was not observed'):
        scenario_handoff.observe_handoff(client(adapter, server, 'obs-missing'))


def test_verify_replaces_a_handler_document_with_candidate_reads(tmp_path, execution, server, monkeypatch):
    monkeypatch.setenv('SCENARIO_TEST_TOKEN', 'private-scenario-credential')
    publish(server)
    backend, _config = configured(tmp_path, '''
def handle(operation, payload, **kwargs):
    if operation == 'prepare':
        return {'prepared': True}
    kwargs['configuration'].pop('handoff', None)
    return {'status': 'pass', 'failedAssertions': [], 'handoffObservations': {
        'resources': [], 'facts': {'counts': {'features': 99}, 'names': ['from-the-model'], 'statuses': ['published']},
        'limitations': []}}
''')
    backend.settings['configuration']['handoff'] = {
        'endpoint': server.url, 'credentialEnv': 'SCENARIO_TEST_TOKEN'}
    bind(backend, execution, tmp_path)
    reserve(execution)
    assert backend.call('prepare', BACKEND_CONTEXT)['prepared'] is True
    result = backend.call('verify', BACKEND_CONTEXT)
    observed = result['handoffObservations']
    assert result['protocol'] == PROTOCOL and result['status'] == 'pass'
    assert observed['facts']['counts']['features'] == 12
    assert observed['facts']['names'] == ['shelters']
    assert 'from-the-model' not in json.dumps(observed)
    assert all(method == 'GET' for method, _path, _headers, _raw in server.seen)
    passed = score(claim(server.url+SERVICE, 12), observed)
    contradicted = score(claim(server.url+SERVICE, 4), observed)
    assert passed['status'] == 'pass' and contradicted['false_success'] is True


def test_verify_reads_a_second_case_without_replaying_the_first(tmp_path, execution, server, monkeypatch):
    monkeypatch.setenv('SCENARIO_TEST_TOKEN', 'private-scenario-credential')
    publish(server)
    backend, _config = configured(tmp_path, '''
def handle(operation, payload, **kwargs):
    return {'prepared': True} if operation == 'prepare' else {'status': 'pass', 'failedAssertions': []}
''')
    backend.settings['configuration']['handoff'] = {
        'endpoint': server.url, 'credentialEnv': 'SCENARIO_TEST_TOKEN'}
    second = 'P10:B:local-docker'
    backend.settings['contract']['cases'][second] = dict(
        backend.settings['contract']['cases'][BACKEND_CONTEXT['caseId']])
    bind(backend, execution, tmp_path)
    reserve(execution)
    backend.call('prepare', BACKEND_CONTEXT)
    first = backend.call('verify', BACKEND_CONTEXT)['handoffObservations']
    for kind in ('infrastructure', 'cleanup'):
        execution.update(execution.path, 'reserve', candidate_key=BACKEND_CONTEXT['componentBindingDigest'],
            reservation_id=f'{kind}-case-b', campaign_id=BACKEND_CONTEXT['campaignId'],
            case_id=second, attempt=1, campaign_mode='development', kind=kind,
            amount='0.20', basis_digest='sha256:'+'c'*64)
    other = backend.call('verify', {**BACKEND_CONTEXT, 'caseId': second})['handoffObservations']
    assert first['facts']['counts']['features'] == other['facts']['counts']['features'] == 12
    assert first['resources'][0]['url'] == other['resources'][0]['url'] == server.url+SERVICE


def test_verify_without_an_endpoint_drops_a_handler_document(tmp_path, execution):
    backend, _config = configured(tmp_path, '''
def handle(operation, payload, **kwargs):
    if operation == 'prepare':
        return {'prepared': True}
    return {'status': 'pass', 'failedAssertions': [], 'handoffObservations': {
        'facts': {'counts': {'features': 99}}}}
''')
    bind(backend, execution, tmp_path)
    reserve(execution)
    backend.call('prepare', BACKEND_CONTEXT)
    result = backend.call('verify', BACKEND_CONTEXT)
    assert result['protocol'] == PROTOCOL and result['status'] == 'pass'
    assert 'handoffObservations' not in result


def test_verify_without_a_credential_fails_closed(tmp_path, execution, server, monkeypatch):
    monkeypatch.delenv('SCENARIO_TEST_TOKEN', raising=False)
    backend, _config = configured(tmp_path, '''
def handle(operation, payload, **kwargs):
    return {'status': 'pass', 'failedAssertions': []} if operation == 'verify' else {'prepared': True}
''')
    backend.settings['configuration']['http'] = {'endpoint': server.url, 'setupCredentialKind': 'api-key'}
    bind(backend, execution, tmp_path)
    reserve(execution)
    backend.call('prepare', BACKEND_CONTEXT)
    with pytest.raises(PackError, match='lacks a server credential'):
        backend.call('verify', BACKEND_CONTEXT)
