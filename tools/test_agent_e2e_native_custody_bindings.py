"""Original HTTP/SDK replay controls; fixture observations are not live evidence."""
import copy
import json
from datetime import datetime, timezone
import pytest

from agent_e2e_pack import PackError, canonical_digest
import agent_e2e_native_custody_bindings as custody
import agent_e2e_native_bindings as bindings
from test_agent_e2e_studio_custody_observer import observed
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server


@pytest.fixture
def frozen(observed):
    session = observed.session
    config = session.config['studio']['custody']
    mcp = session.durable.state['scenarioOperations'][config['workerMcp']['key']]['value']
    declaration = {'profile': custody.PROFILE, 'fields': ['taskWorkerId'], 'relationships': {
        'bootstrapWorkerId': observed.producer_task, 'taskPrincipalId': observed.context['agentPrincipal']['actorId']},
        'observers': {'1': {'worker': copy.deepcopy(session.worker_config),
            'mcp': {key: copy.deepcopy(mcp[key]) for key in ('secretArn', 'versionId', 'nativeContext')},
            'http': {'endpoint': observed.server.url, 'attemptId': 'custody-custody-task', 'credentialKind': 'api-key'}}}}
    _, original = observed.observer.revoked(step='before')
    observed.observer.worker(step='custody-started', original=original)
    key = session.phase_key('custody-started-worker')
    reference = {'key': key, 'sha256': canonical_digest(session.durable.state['scenarioOperations'][key])}
    report = custody.retain(session.durable, session.context, name='custody', declaration=declaration,
        proof_reference=reference, started_at=session.started_at, completed_at=datetime.now(timezone.utc).isoformat())
    context = {**session.context, 'startedAt': session.started_at, 'completedAt': datetime.now(timezone.utc).isoformat()}
    return observed, declaration, report, context, reference


def test_only_task_id_resolves_from_original_launch_history_and_independent_task_role_principal(frozen):
    observed, declaration, report, context, _ = frozen
    result = bindings.resolve('custody.worker-replaced', {'taskWorkerId': {'$native': 'custody.taskWorkerId'}},
        {'custody': declaration}, {'custody': report}, context)
    assert result == {'taskWorkerId': observed.worker_task['taskArn']}
    assert report['values'] == result
    assert {row['operation'] for row in report['proof']['lifecycleReads']} == {
        'describe_execution', 'get_execution_history', 'get_item', 'describe_tasks'}


@pytest.mark.parametrize('change', ['attempt', 'task', 'role', 'stale', 'missing-original', 'missing-launch', 'missing-start',
    'claim', 'history-truncated', 'redriven', 'mcp-version', 'foreign-queue', 'principal', 'privileged-env', 'unrelated-original'])
def test_finite_binding_rejects_wrong_stale_missing_or_privileged_originals_even_after_rehashing(frozen, change):
    observed, declaration, report, context, _ = frozen
    proof = report['proof']
    if change == 'attempt': report['context']['attempt'] = 2
    elif change == 'task': proof['lifecycle']['taskArn'] = observed.producer_task
    elif change == 'role': proof['definition']['taskRoleArn'] = 'arn:aws:iam::585192672263:role/admin'
    elif change == 'stale': proof['lifecycleReads'][0]['observedAt'] = '2000-01-01T00:00:00Z'
    elif change == 'missing-original': proof['lifecycleReads'].pop()
    elif change == 'missing-launch': proof['launch'] = None
    elif change == 'missing-start': del proof['lifecycleReads'][0]['response']['startDate']
    elif change == 'claim':
        row = next(row for row in proof['lifecycleReads'] if row['operation'] == 'get_item' and row['response'].get('Item'))
        row['response']['Item']['attemptId'] = {'S': 'foreign-attempt'}
    elif change == 'history-truncated':
        row = next(row for row in proof['lifecycleReads'] if row['operation'] == 'get_execution_history')
        row['response']['events'][0]['stateExitedEventDetails']['outputDetails']['truncated'] = True
    elif change == 'redriven': proof['lifecycleReads'][0]['response']['redriveCount'] = 1
    elif change == 'mcp-version': proof['mcp']['versionId'] = '0'*64
    elif change == 'foreign-queue':
        proof['role']['policyDocument']['Statement'][0]['Resource'] += '-foreign'
        proof['role']['policy']['PolicyDocument'] = copy.deepcopy(proof['role']['policyDocument'])
    elif change == 'principal': proof['taskPrincipalRead']['response']['response']['body']['principal']['actorId'] = 'different-principal'
    elif change == 'privileged-env': proof['nativeTasks']['tasks'][0]['overrides']['containerOverrides'][0]['environment'] = [{'name': 'ROOT', 'value': 'credential'}]
    elif change == 'unrelated-original': report['originalRecord']['phase'] = 'worker-claimed-success'
    # A caller can hash JSON; that never repairs wrong native relationships.
    report['originalRecord']['value'] = copy.deepcopy(proof)
    report['proofReference']['sha256'] = canonical_digest(report['originalRecord'])
    with pytest.raises((PackError, KeyError, TypeError)):
        custody.replay(report, declaration, context)


def test_unretained_or_rebound_custody_proof_cannot_be_adopted(frozen):
    observed, declaration, report, context, reference = frozen
    with pytest.raises(PackError, match='already retained'):
        custody.retain(observed.session.durable, observed.session.context, name='custody', declaration=declaration,
            proof_reference=reference, started_at=report['startedAt'], completed_at=report['completedAt'])
    with pytest.raises(PackError, match='original retained'):
        custody.retain(observed.session.durable, observed.session.context, name='other', declaration=declaration,
            proof_reference={'key': 'not-retained', 'sha256': reference['sha256']}, started_at=report['startedAt'], completed_at=report['completedAt'])


def test_export_replays_the_retained_before_model_binding_without_new_native_reads(frozen):
    observed, declaration, report, context, _ = frozen
    before = len(observed.ecs.mock_calls)
    exported = []
    result = bindings.export(observed.session.durable, context, {'custody': declaration},
        lambda name, value: exported.append((name, value)) or name)
    assert result == {'custody': 'native-binding-custody-running'} and exported[0][1] == report
    assert len(observed.ecs.mock_calls) == before


@pytest.mark.parametrize('change', [None, 'role', 'secret-version', 'endpoint', 'missing-declaration', 'missing-context'])
def test_session_refuses_unfrozen_or_mismatched_custody_before_paid_launch(frozen, tmp_path, change):
    from agent_e2e_studio_backend import Session
    observed, declaration, _, _, _ = frozen
    session = observed.session
    session.bundle = tmp_path
    session.config['studio']['requesterCredentialKind'] = 'api-key'
    if change == 'role': declaration['observers']['1']['worker']['taskRoleArn'] = 'arn:aws:iam::585192672263:role/privileged'
    elif change == 'secret-version': declaration['observers']['1']['mcp']['versionId'] = '0'*64
    elif change == 'endpoint': declaration['observers']['1']['http']['endpoint'] = 'https://foreign.example'
    elif change == 'missing-context': del session.durable.state['studioBootstrapOperations'][session.config['studio']['custody']['nativeContext']['key']]
    declarations = {} if change == 'missing-declaration' else {'custody': declaration}
    (tmp_path/'expectations.json').write_text(json.dumps({'cases': {session.context['caseId']: {'nativeBindings': declarations}}}))
    before = len(observed.ecs.mock_calls)
    if change is None: assert Session.custody_declaration(session) == ('custody', declaration)
    else:
        with pytest.raises(PackError): Session.custody_declaration(session)
    assert len(observed.ecs.mock_calls) == before


@pytest.mark.parametrize('assertion,field', [('custody.worker-replaced', 'taskPrincipalId'), ('state.saved', 'versionId'),
    ('custody.bootstrap-revoked', 'bootstrapPrincipalId')])
def test_worker_identity_cannot_resolve_other_oracle_fields(frozen, assertion, field):
    _, declaration, report, context, _ = frozen
    with pytest.raises(PackError):
        bindings.resolve(assertion, {field: {'$native': 'custody.taskWorkerId'}}, {'custody': declaration}, {'custody': report}, context)


def test_only_reviewed_distinct_publication_and_custody_pair_is_accepted(frozen):
    observed, declaration, _, _, _ = frozen
    publication = {'profile': bindings.PROFILE, 'fields': ['proposalId', 'approvalAuditId'],
        'relationships': {'itemId': '11111111-1111-4111-8111-111111111111',
            'versionId': '22222222-2222-4222-8222-222222222222', 'contentHash': 'a'*64,
            'route': '/maps/custody-contract', 'visibility': 'public', 'requesterPrincipalId': 'actual-agent'},
        'observers': {'1': {'endpoint': observed.server.url, 'attemptId': 'publication-contract', 'credentialKind': 'api-key'}}}
    case = {'assertions': {'approval.separate': {'proposalId': {'$native': 'publication.proposalId'},
        'auditId': {'$native': 'publication.approvalAuditId'}},
        'custody.worker-replaced': {'taskWorkerId': {'$native': 'custody.taskWorkerId'}}},
        'cleanup': {}, 'nativeBindings': {'publication': publication, 'custody': declaration}}
    bindings.validate_case(case)
    invalid = copy.deepcopy(case)
    invalid['nativeBindings']['duplicate'] = copy.deepcopy(declaration)
    with pytest.raises(PackError): bindings.validate_case(invalid)
    invalid = copy.deepcopy(case)
    invalid['nativeBindings']['publication'] = copy.deepcopy(declaration)
    with pytest.raises(PackError, match='distinct'): bindings.validate_case(invalid)


def test_existing_finite_authoring_and_job_pair_is_supported_without_opening_unrelated_combinations():
    http = {'1': {'endpoint': 'http://127.0.0.1:8080', 'attemptId': 'original-contract', 'credentialKind': 'api-key'}}
    authoring = {'profile': 'honua.studio-authoring-bindings/v1', 'fields': ['savedVersionId'], 'observers': http,
        'relationships': {'itemId': '11111111-1111-4111-8111-111111111111', 'family': 'map', 'namespace': 'ae_contract',
            'ownerPrincipalId': 'actual-agent', 'startingVersionId': None, 'minimumVersionNumber': 1}}
    job = {'profile': 'honua.managed-job-bindings/v1', 'fields': ['jobId'], 'observers': http,
        'relationships': {'processId': 'analytics.buffer-aggregate-managed', 'requesterPrincipalId': 'actual-agent'}}
    case = {'nativeBindings': {'save': authoring, 'job': job}, 'cleanup': {}, 'assertions': {
        'state.saved': {'versionId': {'$native': 'save.savedVersionId'}},
        'job.completed': {'resourceId': {'$native': 'job.jobId'}}}}
    bindings.validate_case(case)
