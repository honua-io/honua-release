"""Native-ID custody and gate replay contracts; fixtures never qualify a release."""
import copy
from datetime import datetime, timezone
from types import MethodType

import pytest

import agent_e2e_native_bindings as native
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_state import DurableExecution, preserve_history
from test_agent_e2e_studio_observer import studio, select, adapter, server, ITEM, VERSION
from test_agent_e2e_scenario_backend import CONTEXT


@pytest.fixture
def bound(studio):
    observer = studio.observer.observer
    durable = observer.durable
    durable._fixture_identity = MethodType(DurableExecution._fixture_identity, durable)
    context = {**CONTEXT, 'campaignMode': 'qualification'}
    declaration = {'profile': native.PROFILE, 'fields': ['proposalId'],
        'observers': {'1': {'endpoint': observer.client.endpoint, 'attemptId': observer.identity['attemptId'],
                           'credentialKind': observer.client.kind}},
        'relationships': {key: studio.binding[key] for key in native.RELATIONSHIP}}
    case = {'assertions': {'approval.pending': {'proposalId': {'$native': 'publication.proposalId'},
        'status': 'AwaitingApproval', 'publishedResourceIds': []}}, 'cleanup': {'resourceIds': []},
        'nativeBindings': {'publication': declaration}}
    selected = select(studio)
    return {'studio': studio, 'durable': durable, 'context': context, 'declaration': declaration,
            'case': case, 'selected': selected}


def window(bound):
    return {**bound['context'], 'startedAt': bound['studio'].binding['notBefore'],
            'completedAt': datetime.now(timezone.utc).isoformat()}


def collect(bound, *, approved=False):
    selected, source = bound['selected'], bound['studio']
    responses = {'submissionHandle': selected['nativeHandleReference'], 'proposal': selected['nativeProposalReference']}
    if approved:
        source.state.approved = True
        observation = source.observer.observe(selected, step='approved')
        refs = observation['references']
        responses.update(proposal=refs[0], approvedHandle=refs[1], publicationRequest=refs[2], savedVersion=refs[3])
    context = window(bound)
    return native.retain(bound['durable'], bound['context'], name='publication', declaration=bound['declaration'],
        responses=responses, stage='approved' if approved else 'proposal',
        started_at=context['startedAt'], completed_at=context['completedAt'])


def test_frozen_expectations_resolve_native_pending_id_without_changing_input_bytes(bound):
    frozen = copy.deepcopy(bound['case'])
    native.validate_case(frozen)
    report = collect(bound)
    resolved = native.resolve('approval.pending', frozen['assertions']['approval.pending'],
        frozen['nativeBindings'], {'publication': report}, window(bound))
    assert resolved == {'proposalId': 'proposal-1', 'status': 'AwaitingApproval', 'publishedResourceIds': []}
    assert frozen == bound['case']
    assert bound['durable'].state['nativeBindings']
    assert report['values'] == {'proposalId': 'proposal-1'}


def test_approved_audit_id_requires_native_seal_replay_request_and_saved_version(bound):
    bound['declaration']['fields'].append('approvalAuditId')
    report = collect(bound, approved=True)
    resolved = native.resolve('approval.separate', {'proposalId': {'$native': 'publication.proposalId'},
        'auditId': {'$native': 'publication.approvalAuditId'}},
        {'publication': bound['declaration']}, {'publication': report}, window(bound))
    assert resolved == {'proposalId': 'proposal-1', 'auditId': 'audit-approved-1'}
    assert set(report['records']) == native.STAGES['approved']


def test_pending_id_can_be_retained_before_approved_id_exists_but_cannot_satisfy_complete_assertion(bound):
    bound['declaration']['fields'].append('approvalAuditId')
    pending = collect(bound)
    with pytest.raises(PackError, match='remains unbound'):
        native.replay(pending, bound['declaration'], window(bound))
    original_state = copy.deepcopy(bound['durable'].state)
    approved = collect(bound, approved=True)
    preserve_history(original_state, bound['durable'].state)
    assert len(bound['durable'].state['nativeBindings']) == 2
    assert pending['values']['proposalId'] == approved['values']['proposalId']


@pytest.mark.parametrize('change', ['unknown-field', 'wrong-assertion', 'fallback', 'nested', 'cleanup', 'unused'])
def test_frozen_dynamic_field_allowlist_cannot_weaken_other_assertions(bound, change):
    case = copy.deepcopy(bound['case'])
    if change == 'unknown-field': case['nativeBindings']['publication']['fields'] = ['status']
    elif change == 'wrong-assertion': case['assertions'] = {'filter.exact': {'proposalId': {'$native': 'publication.proposalId'}}}
    elif change == 'fallback': case['assertions']['approval.pending']['proposalId']['fallback'] = 'made-up'
    elif change == 'nested': case['assertions']['approval.pending']['proposalId'] = [{'x': {'$native': 'publication.proposalId'}}]
    elif change == 'cleanup': case['cleanup']['resourceIds'] = [{'$native': 'publication.proposalId'}]
    elif change == 'unused': case['nativeBindings']['publication']['fields'].append('approvalAuditId')
    with pytest.raises(PackError): native.validate_case(case)


@pytest.mark.parametrize('change', ['candidate', 'case', 'attempt', 'mode', 'observer', 'origin', 'credential',
                                  'raw-bytes', 'intent', 'values', 'model-body', 'timestamp', 'relationship'])
def test_native_replay_rejects_forged_cross_attempt_or_model_only_origins(bound, change):
    report = collect(bound)
    context, declaration = window(bound), copy.deepcopy(bound['declaration'])
    if change == 'candidate': report['context']['componentBindingDigest'] = 'sha256:'+'d'*64
    elif change == 'case': report['context']['caseId'] = 'P17:B:aws-ecs-redis-on'
    elif change == 'attempt': report['context']['attempt'] = 2
    elif change == 'mode': report['context']['campaignMode'] = 'development'
    elif change == 'observer': report['records']['proposal']['intent']['attemptId'] = 'worker-untrusted'
    elif change == 'origin': report['records']['proposal']['intent']['endpoint'] = 'https://foreign.invalid'
    elif change == 'credential': report['records']['proposal']['intent']['credentialKind'] = 'anonymous'
    elif change == 'raw-bytes': report['records']['proposal']['response']['response']['rawBodyBase64'] = 'e30='
    elif change == 'intent': report['records']['proposal']['response']['intentDigest'] = 'sha256:'+'0'*64
    elif change == 'values': report['values']['proposalId'] = 'worker-authored-id'
    elif change == 'model-body': report['records']['proposal'] = {'structuredContent': {'proposalId': 'worker-authored-id'}}
    elif change == 'timestamp': context['startedAt'] = context['completedAt']
    elif change == 'relationship': declaration['relationships']['versionId'] = ITEM
    with pytest.raises((PackError, KeyError)): native.replay(report, declaration, context)


def test_original_binding_is_create_only_and_lost_binding_ack_does_not_authorize_rebinding(bound):
    report = collect(bound)
    before = copy.deepcopy(bound['durable'].state)
    with pytest.raises(PackError, match='already retained'): collect(bound)
    assert bound['durable'].state == before
    key = next(iter(before['nativeBindings']))
    bound['durable'].state['nativeBindings'][key]['values']['proposalId'] = 'replacement'
    with pytest.raises(PackError, match='rewrite or erase'):
        bound['durable']._persist()
    assert report['values']['proposalId'] == 'proposal-1'


def test_a_model_payload_or_unretained_record_cannot_enter_durable_native_bindings(bound):
    source = {'key': 'not-retained', 'sha256': canonical_digest({'proposalId': 'model-id'})}
    with pytest.raises(PackError, match='not retained controller HTTP'):
        native.retain(bound['durable'], bound['context'], name='publication', declaration=bound['declaration'],
            responses={'submissionHandle': source, 'proposal': source}, stage='proposal',
            started_at=window(bound)['startedAt'], completed_at=window(bound)['completedAt'])
    assert not bound['durable'].state.get('nativeBindings')


def test_binding_collection_requires_original_outstanding_liabilities(bound):
    for row in bound['durable'].state['ledger']['entries']:
        row.update(state='settled', actualUsd='0')
    with pytest.raises(PackError, match='liability'): collect(bound)
    assert not bound['durable'].state.get('nativeBindings')


def test_concrete_studio_observer_wires_native_originals_into_same_durable_attempt(bound):
    source = bound['studio']
    observation = source.observer.observe(bound['selected'], step='pending-native')
    context = window(bound)
    report = source.observer.bind_native(observation, name='publication', declaration=bound['declaration'],
        context=bound['context'], started_at=context['startedAt'], completed_at=context['completedAt'])
    assert native.replay(report, bound['declaration'], context) == {'proposalId': 'proposal-1'}
    assert all(pair['intent']['method'] == 'GET' for pair in report['records'].values())


def test_private_export_replays_retained_binding_without_http_or_new_reservation(bound):
    report = collect(bound)
    calls = list(bound['studio'].state.calls)
    ledger = copy.deepcopy(bound['durable'].state['ledger'])
    emitted = {}
    def evidence(name, value):
        emitted[name] = copy.deepcopy(value)
        return {'path': name+'.json', 'sha256': canonical_digest(value)}
    refs = native.export(bound['durable'], window(bound), {'publication': bound['declaration']}, evidence)
    assert refs['publication']['path'] == 'native-binding-publication-proposal.json'
    assert emitted['native-binding-publication-proposal'] == report
    assert bound['studio'].state.calls == calls and bound['durable'].state['ledger'] == ledger
