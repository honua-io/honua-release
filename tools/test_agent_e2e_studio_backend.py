"""Publication composition against native HTTP contract fixtures; no live claims."""
import copy
import json
from types import SimpleNamespace

import pytest

import agent_e2e_studio_backend as backend
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_state import preserve_history
from test_agent_e2e_studio_observer import studio, select
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server


@pytest.mark.parametrize('mode,case', [('qualification', 'P14:A:aws-ecs-redis-on'),
    ('development', 'P14:C:aws-ecs-redis-on'), ('development', 'P14:A:aws-eks'),
    ('development', 'P13:A:aws-ecs-redis-on'), ('development', 'P16:A:aws-ecs-redis-on'),
    ('development', 'P19:A:aws-ecs-redis-on'), ('qualification', 'P17:A:aws-ecs-redis-on'), ('development', 'P14:A:local-docker')])
def test_only_explicit_development_publication_cases_can_use_this_adapter(tmp_path, mode, case):
    with pytest.raises(PackError, match='only explicit A/B ECS development'):
        backend.Session(durable=None, campaign={'campaignMode': mode}, bundle=tmp_path,
            configuration={}, context={'caseId': case})


def test_p14_cannot_export_development_as_complete_legacy_campaign():
    with pytest.raises(PackError, match='complete 252-case'):
        backend.handle('export_campaign', {}, durable=None, campaign={}, bundle=None, configuration={}, timeout=1)


@pytest.mark.parametrize('field', ['studioBootstrapOperations', 'studioBootstrapFixtures'])
def test_prefreeze_originals_survive_candidate_state_restart_and_cannot_be_rebound(field):
    before = {field: {'native-key': {'versionId': 'actual-observed-version'}}}
    after = copy.deepcopy(before)
    after[field]['native-key']['versionId'] = 'rebound-version'
    with pytest.raises(PackError, match='cannot rewrite or erase'):
        preserve_history(before, after)
    preserve_history(before, {**before, 'other': {'new': 'append-only'}})


@pytest.fixture
def publication_session(studio, adapter, tmp_path):
    value = backend.Session.__new__(backend.Session)
    value.durable, value.bundle = adapter.durable, tmp_path
    value.context = {**adapter.http.context, 'campaignMode': 'development'}
    value.key = canonical_digest(backend.identity(value.context))
    value.campaign = {key: value.context[key] for key in ('campaignId', 'campaignMode', 'componentBindingDigest')}
    value.case = {'assertions': ['approval.separate', 'publication.public', 'tenant.denied',
        'custody.bootstrap-revoked', 'custody.worker-replaced', 'source-content.unchanged']}
    value.attempt_id = 'publication-worker-fixture'
    value.config = {'studio': {}}
    value.durable._fixture_identity = lambda context: value.key
    value.observer_for_recovery = lambda: studio.observer
    studio.observer.retain = value.retain
    value.retain('prepare-start', {'startedAt': studio.binding['notBefore']})
    before = studio.observer.version('initial-saved-version')
    value.retain('source-before', before)
    declaration = {'profile': 'honua.studio-publication-bindings/v1', 'fields': ['proposalId', 'approvalAuditId'],
        'relationships': {key: item for key, item in studio.binding.items() if key != 'notBefore'},
        'observers': {'1': {'endpoint': adapter.http.client.endpoint, 'attemptId': adapter.http.identity['attemptId'],
            'credentialKind': 'api-key'}}}
    expected = {'approval.separate': {'proposalId': {'$native': 'publication.proposalId'},
        'auditId': {'$native': 'publication.approvalAuditId'}, 'requesterPrincipalId': studio.binding['requesterPrincipalId'],
        'approverPrincipalId': 'actual-fixture-approver', 'approvedVersionId': studio.binding['versionId']},
        'publication.public': {'publishedUrl': studio.server.url+studio.observer.path, 'anonymousHttpStatus': 200,
            'resourceId': studio.binding['itemId'], 'publishedVersionId': studio.binding['versionId']},
        'source-content.unchanged': {'profile': 'honua.protected-content/v1', 'resources': [before['snapshot']]},
        'tenant.denied': {}, 'custody.bootstrap-revoked': {}, 'custody.worker-replaced': {}}
    (tmp_path/'expectations.json').write_text(json.dumps({'cases': {value.context['caseId']: {
        'assertions': expected, 'nativeBindings': {'publication': declaration}}}}))
    return value


def test_genuine_native_publication_fields_do_not_hide_unimplemented_custody_or_isolation(publication_session, studio):
    value = publication_session
    chosen = select(studio)
    value.retain('publication-ordinal-1', chosen)
    studio.state.approved = True
    result = value.verify()
    assert result == {'status': 'fail', 'failedAssertions': ['tenant.denied', 'custody.bootstrap-revoked', 'custody.worker-replaced']}
    verified = value.value('verification')
    assert verified['unsupportedAssertions'] == result['failedAssertions']
    assert verified['observations']['approval.separate']['approverPrincipalId'] == 'actual-fixture-approver'
    assert verified['observations']['publication.public']['anonymousHttpStatus'] == 200
    assert value.durable.state['nativeBindings'][value.key+'/publication/approved']['values'] == {
        'proposalId': 'proposal-1', 'approvalAuditId': 'audit-approved-1'}
    assert all(method == 'GET' for method, *_ in studio.server.seen)


def test_model_claim_or_no_selected_proposal_cannot_qualify_publication(publication_session):
    value = publication_session
    value.retain('model-result-1', {'agentResult': {'content': {'status': 'published', 'approvedBy': 'human'}}})
    result = value.verify()
    assert result['status'] == 'fail'
    assert set(result['failedAssertions']) == set(value.case['assertions'])-{'source-content.unchanged'}
    assert not value.durable.state.get('nativeBindings')


def test_multiple_model_submissions_cannot_be_reduced_to_one_successful_publication(publication_session, studio):
    value = publication_session
    value.retain('publication-ordinal-1', select(studio))
    value.retain('publication-ordinal-2', {'actionId': 'different-model-action'})
    studio.state.approved = True
    assert 'publication.public' in value.verify()['failedAssertions']
    assert value.value('verification')['selectedActionId'] is None


def test_configured_tenant_verification_cannot_manufacture_a_missing_before_worker_proof(publication_session, studio):
    value = publication_session
    value.config['studio']['tenantIsolation'] = {'configuration': 'present-without-original-proof'}
    value.retain('publication-ordinal-1', select(studio))
    studio.state.approved = True
    result = value.verify()
    assert 'tenant.denied' in result['failedAssertions']
    assert 'tenant.denied' not in value.value('verification')['unsupportedAssertions']
    assert value.value('tenant-verification-refused') == {'reasonCode': 'native-tenant-proof-refused'}


def test_p17_cannot_send_controller_probe_without_original_native_custody(publication_session, studio):
    value = publication_session
    value.is_denial_case = lambda: True
    value.config['studio']['custody'] = {}
    value.case['assertions'] = ['approval.pending', 'approval.no-bypass',
        'custody.bootstrap-revoked', 'custody.worker-replaced']
    value.custody_declaration = lambda: ('custody', {})
    value.retain('publication-ordinal-1', select(studio))
    path = value.bundle/'expectations.json'
    expected = json.loads(path.read_text())
    expected['cases'][value.context['caseId']]['assertions'].update({
        'approval.pending': {'proposalId': {'$native': 'denial.proposalId'}, 'status': 'AwaitingApproval', 'publishedResourceIds': []},
        'approval.no-bypass': {'requests': []}})
    path.write_text(json.dumps(expected))
    result = value.verify()
    assert result['status'] == 'fail'
    assert set(result['failedAssertions']) == set(value.case['assertions'])
    assert value.value('custody-verification-refused') is not None
    assert value.value('denial-final') is None
    assert {'approval.pending', 'approval.no-bypass'} <= set(value.value('verification')['unsupportedAssertions'])
    assert all(method == 'GET' for method, _ in studio.state.calls)
