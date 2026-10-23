"""Actual HTTP contract replay; fixture payloads are never live campaign evidence."""
import copy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from agent_e2e_pack import PackError
from agent_e2e_scenario_backend import ScenarioHttp
from agent_e2e_studio_observer import OPERATION, TOOL, StudioPublicationObserver, route_path
from test_agent_e2e_scenario_backend import CONTEXT, adapter
from test_agent_e2e_setup_http import server

ITEM = '11111111-1111-1111-1111-111111111111'
VERSION = '22222222-2222-2222-2222-222222222222'
PUBLICATION = '33333333-3333-3333-3333-333333333333'


@pytest.fixture
def studio(adapter, server):
    created = datetime.now(timezone.utc)-timedelta(seconds=10)
    binding = {'itemId': ITEM, 'versionId': VERSION, 'contentHash': 'a'*64,
        'route': '/maps/case-a', 'visibility': 'public', 'requesterPrincipalId': 'actual-fixture-requester',
        'notBefore': (created-timedelta(seconds=1)).isoformat()}
    dispatch = {'actionId': 'b'*64, 'requestDigest': 'sha256:'+'c'*64}
    state = SimpleNamespace(approved=False, change=None, calls=[], records={})
    def retain(name, value):
        assert name not in state.records
        state.records[name] = copy.deepcopy(value)
        return {'key': name}
    anonymous = ScenarioHttp(durable=adapter.durable, context=CONTEXT, endpoint=server.url,
        credential_env=None, credential_kind='anonymous', attempt_id='anonymous-fixture-1')
    observer = StudioPublicationObserver(observer=adapter.http, anonymous=anonymous, binding=binding, retain=retain)
    arguments = {key: binding[key] for key in ('itemId', 'versionId', 'contentHash', 'route', 'visibility')}
    action = {'name': 'mcp_call_tool', 'input': {'name': TOOL, 'arguments': arguments}}
    worker = {'agentResult': {'content': {'structuredContent': {'status': 'AwaitingApproval',
        'humanConfirmationRequired': True, 'operationInstanceId': 'invocation-1', 'proposalId': 'proposal-1',
        'auditId': 'audit-request-1', 'correlationId': 'correlation-1'}}}}
    version = {key: binding[key] for key in ('itemId', 'versionId', 'contentHash')}
    version['envelope'] = {'family': 'map', 'payload': {'fixture': 'offline-contract-only'}}
    def respond(method, path, raw):
        state.calls.append((method, path))
        assert method == 'GET'
        if path == '/api/v1/operations/handles/invocation-1':
            result = {'operationInstanceId': 'invocation-1', 'operationId': OPERATION,
                'status': 'RequiresApproval', 'proposalId': 'proposal-1', 'auditId': 'audit-request-1',
                'correlationId': 'correlation-1', 'createdAt': created.isoformat()}
            if state.change == 'foreign-operation': result['operationId'] = 'service.publish'
            if state.change == 'foreign-proposal': result['proposalId'] = 'unrelated-proposal'
            if state.change == 'old-operation': result['createdAt'] = (created-timedelta(days=1)).isoformat()
        elif path == '/api/v1/admin/proposals/proposal-1':
            result = {'proposalId': 'proposal-1', 'kind': 'StudioDraftMutation',
                'requestedBy': binding['requesterPrincipalId'], 'createdAt': created.isoformat(),
                'status': 'Succeeded' if state.approved else 'AwaitingApproval',
                'diff': [key+'='+binding[key] for key in ('itemId', 'versionId', 'contentHash', 'route', 'visibility')]}
            if state.approved:
                result.update(resolvedBy='actual-fixture-approver', executionOperationId='replay-1',
                              resolvedAt=(created+timedelta(seconds=1)).isoformat())
            if state.change == 'wrong-hash': result['diff'][2] = 'contentHash='+'e'*64
            if state.change == 'wrong-owner': result['requestedBy'] = 'different-requester'
            if state.change == 'self-approval': result['resolvedBy'] = binding['requesterPrincipalId']
            if state.change == 'old-resolution': result['resolvedAt'] = (created-timedelta(days=1)).isoformat()
            if state.change == 'rejected': result['status'] = 'Rejected'
            return 200, {}, json.dumps(result).encode()
        elif path == '/api/v1/operations/handles/replay-1':
            result = {'operationInstanceId': 'replay-1', 'operationId': OPERATION,
                'status': 'Completed', 'proposalId': 'proposal-1', 'auditId': 'audit-approved-1',
                'resourceIds': {'itemId': ITEM, 'versionId': VERSION, 'publicationId': PUBLICATION,
                    'route': binding['route'], 'activeUrl': route_path(binding['route'])}}
            if state.change == 'wrong-replay': result['proposalId'] = 'foreign-proposal'
            if state.change == 'external-url': result['resourceIds']['activeUrl'] = 'https://foreign.invalid/exfiltrate'
        elif path.endswith('/publish-requests/'+PUBLICATION):
            result = {'requestId': PUBLICATION, 'itemId': ITEM, 'versionId': VERSION, 'status': 'accepted',
                'intent': {'route': binding['route'], 'visibility': binding['visibility']},
                'requestedBy': binding['requesterPrincipalId']}
            if state.change == 'pending-request': result['status'] = 'pending'
        elif path == observer.version_path():
            result = copy.deepcopy(version)
        elif path == route_path(binding['route']):
            headers = {key.lower(): value for key, value in server.seen[-1][2].items()}
            is_anonymous = 'x-api-key' not in headers and 'authorization' not in headers
            if not state.approved and state.change != 'already-published':
                return 404, {}, b'{"status":404}'
            if binding['visibility'] != 'public' and is_anonymous:
                if state.change == 'private-disclosure': return 401, {}, json.dumps(version).encode()
                if state.change != 'private-leak': return 401, {}, b'{"status":401}'
            result = {**copy.deepcopy(version), 'publicationId': PUBLICATION,
                'route': binding['route'], 'visibility': binding['visibility']}
            if state.change == 'wrong-route-version': result['versionId'] = PUBLICATION
            if state.change == 'altered-envelope': result['envelope'] = {'changed': True}
        else:
            raise AssertionError(path)
        return 200, {}, json.dumps({'success': True, 'data': result}).encode()
    server.respond = respond
    return SimpleNamespace(state=state, binding=binding, observer=observer, action=action,
                           dispatch=dispatch, worker=worker, server=server)


def select(studio):
    return studio.observer.selected(studio.action, studio.dispatch, studio.worker)


def test_pending_stays_pending_with_actual_authenticated_and_anonymous_route_absence(studio):
    selected = select(studio)
    result = studio.observer.observe(selected, step='pending')
    assert result['approval.pending'] == {'proposalId': 'proposal-1', 'status': 'AwaitingApproval', 'publishedResourceIds': []}
    assert 'publication.public' not in result
    assert len(result['references']) == 3
    assert all(method == 'GET' and not path.endswith('/approve') for method, path in studio.state.calls)
    assert 'studio-selected-'+'b'*64 in studio.state.records


def test_actual_distinct_approval_replay_and_sealed_public_artifact_are_all_required(studio):
    selected = select(studio)
    before = studio.observer.version('before')
    studio.state.approved = True
    result = studio.observer.observe(selected, step='approved')
    assert result['approval.separate']['requesterPrincipalId'] != result['approval.separate']['approverPrincipalId']
    assert result['approval.separate']['auditId'] == 'audit-approved-1'
    assert result['publication.public']['publishedVersionId'] == VERSION
    assert result['publication.public']['publishedUrl'] == studio.server.url+'/api/v1/studio/published/maps/case-a'
    assert studio.observer.version('after')['snapshot'] == before['snapshot']
    assert all(method == 'GET' for method, _ in studio.state.calls)


@pytest.mark.parametrize('change', ['foreign-operation', 'foreign-proposal', 'old-operation', 'wrong-hash', 'wrong-owner'])
def test_native_observation_refuses_worker_substitution_before_any_publication_claim(studio, change):
    studio.state.change = change
    with pytest.raises(PackError): select(studio)
    assert not studio.state.records


@pytest.mark.parametrize('change', ['self-approval', 'old-resolution', 'rejected', 'wrong-replay', 'external-url',
                                  'pending-request', 'wrong-route-version', 'altered-envelope'])
def test_approved_status_alone_cannot_pass_stale_foreign_failed_or_inconsistent_native_reads(studio, change):
    selected = select(studio)
    studio.state.approved, studio.state.change = True, change
    with pytest.raises(PackError): studio.observer.observe(selected, step='approved')
    assert 'approved-observation' not in studio.state.records
    assert all('foreign.invalid' not in path for _, path in studio.state.calls)


def test_pending_proposal_cannot_hide_an_already_published_route(studio):
    selected = select(studio)
    studio.state.change = 'already-published'
    with pytest.raises(PackError, match='already published'): studio.observer.observe(selected, step='pending')
    assert 'pending-observation' not in studio.state.records


@pytest.mark.parametrize('change', [None, 'private-leak', 'private-disclosure'])
def test_private_anonymous_denial_uses_actual_positive_control_but_never_claims_full_tenant_matrix(studio, change):
    studio.binding['visibility'] = studio.observer.binding['visibility'] = studio.action['input']['arguments']['visibility'] = 'personal'
    selected = select(studio)
    studio.state.approved, studio.state.change = True, change
    if change:
        with pytest.raises(PackError): studio.observer.observe(selected, step='private')
    else:
        result = studio.observer.observe(selected, step='private')
        assert result['privateAnonymousDenial']['status'] == 401
        assert 'publication.private' not in result and 'publication.public' not in result


@pytest.mark.parametrize('field', ['itemId', 'versionId', 'contentHash', 'route', 'visibility'])
def test_observer_does_not_correct_model_selected_foreign_intent(studio, field):
    studio.action['input']['arguments'][field] = 'wrong'
    with pytest.raises(PackError, match='different Studio'): select(studio)
    assert not studio.state.calls
