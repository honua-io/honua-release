"""Controller composition contracts. Fixtures never claim live campaign success."""
import base64
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_e2e_p10_backend as backend
from agent_e2e_pack import PackError, canonical_digest, load_json
from agent_e2e_state import DurableExecution
from test_agent_e2e_backend import Store, CANDIDATE, reserve


CASE = 'P10:A:aws-ecs-redis-on'
CONTEXT = {'campaignId': 'offline-campaign', 'caseId': CASE, 'attempt': 1, 'campaignMode': 'development',
           'componentBindingDigest': CANDIDATE}


@pytest.fixture
def session(tmp_path):
    durable = DurableExecution(Store(), tmp_path/'ledger.json', initialize=True)
    for kind in ('infrastructure', 'cleanup'):
        durable.update(durable.path, 'reserve', candidate_key=CANDIDATE, reservation_id=kind,
            campaign_id=CONTEXT['campaignId'], case_id=CASE, attempt=1, campaign_mode='development',
            kind=kind, amount='1', basis_digest='sha256:'+'b'*64)
    value = backend.Session.__new__(backend.Session)
    value.durable, value.bundle = durable, tmp_path
    value.context, value.key = copy.deepcopy(CONTEXT), canonical_digest(backend.identity(CONTEXT))
    value.campaign = {key: CONTEXT[key] for key in ('campaignId', 'campaignMode', 'componentBindingDigest')}
    value.namespace, value.attempt_id = 'ae_fixture', 'attempt-fixture'
    snapshot = {'resourceId': 'shelters', 'content': {'observed': 'native-source-fixture'}}
    value.retain('source-before', {'snapshot': snapshot, 'rawReference': {'key': 'before', 'sha256': 'sha256:'+'c'*64}})
    value.observer = SimpleNamespace(source=lambda step: {'snapshot': copy.deepcopy(snapshot),
        'rawReference': {'key': step, 'sha256': 'sha256:'+'d'*64}})
    value.observer_for_recovery = lambda: value.observer
    expected = {'filter.exact': {'resourceIds': ['s3']}, 'source-content.unchanged': {
        'profile': 'honua.protected-content/v1', 'resources': [snapshot]}}
    (tmp_path/'expectations.json').write_text(json.dumps({'cases': {CASE: {'assertions': expected}}}))
    try:
        yield value
    finally:
        durable.close()


def test_verification_uses_latest_actual_selected_query_and_preserves_wrong_result(session):
    for ordinal, ids in [(1, ['s3']), (2, ['s2', 's3'])]:
        session.retain('query-ordinal-'+str(ordinal), {'result': {'resourceIds': ids},
            'projectionError': None, 'actionId': str(ordinal)*64})
    observed = session.verify()
    assert observed == {'status': 'fail', 'failedAssertions': ['filter.exact']}
    retained = session.value('verification')
    assert retained['selectedActionId'] == '2'*64 and retained['observations']['filter.exact']['resourceIds'] == ['s2', 's3']


def test_no_query_or_changed_source_cannot_pass_from_model_claim(session):
    session.observer.source = lambda step: {'snapshot': {'resourceId': 'shelters', 'content': {'changed': True}}}
    assert session.verify() == {'status': 'fail', 'failedAssertions': ['filter.exact', 'source-content.unchanged']}


def test_worker_stopped_is_not_fixture_cleanup(session):
    session.open_worker = lambda: None
    session.launch = {'offline': True}
    session.lifecycle = SimpleNamespace(stop=lambda *args: {'taskStatus': 'STOPPED'})
    session.await_worker = lambda **kwargs: {'taskStatus': 'STOPPED', 'stopRecorded': True}
    observed = session.cleanup()
    assert observed['status'] == 'blocked'
    assert observed['worker']['stopRecorded'] is True and observed['fixtureSql'] is None
    assert observed['failures'] == [{'component': 'sql-cleanup', 'errorType': 'PackError'}]
    assert all(row['state'] == 'reserved' for row in session.durable.store.state['ledger']['entries'])


def test_fixture_absence_is_not_worker_termination(session):
    session.open_worker = lambda: (_ for _ in ()).throw(PackError('independent worker unavailable'))
    session.durable.state['fixtureOwnership'] = {session.key: {'candidateKey': CANDIDATE}}
    session.http = lambda *args, **kwargs: None
    session.database = SimpleNamespace(cleanup=lambda *args, **kwargs: {'resourceIds': [], 'independentlyObserved': True})
    observed = session.cleanup()
    assert observed['status'] == 'blocked' and observed['worker'] is None
    assert observed['fixtureSql']['independentlyObserved'] is True
    assert all(row['state'] == 'reserved' for row in session.durable.store.state['ledger']['entries'])


def test_missing_native_costs_never_become_zero_or_passing_receipt(session):
    result = {**CONTEXT, 'status': 'blocked', 'failureAttribution': 'undetermined'}
    with pytest.raises(PackError, match='costs remain unknown'):
        session.export_attempt({'controllerResult': result})
    assert session.value('controller-result') == result
    assert not list(session.bundle.glob('backend-exports/*'))


def test_failed_attempt_exports_original_oracle_evidence_without_claiming_a_passing_oracle(session):
    for kind in ('infrastructure', 'cleanup'):
        session.durable.update(session.durable.path, 'settle', candidate_key=CANDIDATE, reservation_id=kind, amount='0.1')
    session.campaign.update(packDigest='sha256:'+'b'*64, corpusDigest='sha256:'+'c'*64,
        qualificationLockDigest='sha256:'+'d'*64, modelId='fixture-model', returnedModelId='fixture-returned-model',
        controller={'fixture': True}, knowledge={'fixture': True})
    session.case = {'scenarioId': 'P10', 'variant': 'A', 'target': 'aws-ecs-redis-on', 'promptDigest': 'sha256:'+'e'*64}
    session.knowledge_manifest = {'fixture': 'offline-only'}
    session.durable.state['journals'] = {session.key: []}
    session.retain('prepare-start', {'startedAt': '2026-10-22T01:00:00+00:00'})
    session.retain('cleanup', {'completedAt': '2026-10-22T01:01:00+00:00', 'fixtureSql': {'resourceIds': []}})
    session.retain('verification', {'failedAssertions': ['filter.exact'], 'observations': {'filter.exact': {'resourceIds': ['wrong']}}})
    directory = session.bundle/'backend-exports'
    directory.mkdir()
    result = {'status': 'fail', 'failureAttribution': 'model', 'usage': {'modelTurns': 0, 'executedActions': 0},
              'cleanup': {'status': 'pass'}}
    destination = directory/'failed.json'
    session.export_attempt({'controllerResult': result, 'exportPath': str(destination)})
    exported = load_json(destination)
    assert exported['receipt']['oracle'] is None and exported['receipt']['failureEvidence'] is not None
    artifacts = exported['evidence']
    report_path = next(key for key in artifacts if key.endswith('/failed-oracle.json'))
    report = json.loads(base64.b64decode(artifacts[report_path]))
    assert report['assertions'][0]['status'] == 'fail'


@pytest.mark.parametrize('mode,case', [('qualification', CASE), ('development', 'P10:C:aws-ecs-redis-on'),
                                    ('development', 'P10:A:aws-eks'), ('development', 'P11:A:aws-ecs-redis-on')])
def test_handler_refuses_qualification_holdout_and_unimplemented_cases(tmp_path, mode, case):
    with pytest.raises(PackError, match='only explicit A/B ECS development'):
        backend.Session(durable=None, campaign={'campaignMode': mode}, bundle=tmp_path, configuration={},
                        context={'caseId': case})


def test_handler_cannot_export_partial_profile_as_complete_campaign():
    with pytest.raises(PackError, match='complete 252-case'):
        backend.handle('export_campaign', {}, durable=None, campaign={}, bundle=None, configuration={}, timeout=10)


def test_long_model_turns_do_not_reuse_an_expired_observer_http_deadline(session, monkeypatch):
    session.retain('town', {'layers': {'shelters': {'service': 'owned-shelters', 'layerId': 503}}})
    session.config = {'observerMcp': {}}
    opened = []
    session.http = lambda role: opened.append(object()) or opened[-1]
    monkeypatch.setattr(backend, 'P10Observer', lambda **kwargs: SimpleNamespace(**kwargs))
    first = backend.Session.observer_for_recovery(session)
    second = backend.Session.observer_for_recovery(session)
    assert first.http is opened[0] and second.http is opened[1] and first.http is not second.http


@pytest.mark.parametrize('change', [None, 'root', 'writable', 'command', 'added-capability', 'controller-secret', 'transport'])
def test_worker_native_definition_enforces_runtime_and_credential_custody(change):
    config = {'imageUri': 'registry/worker@sha256:'+'a'*64, 'transport': {'attemptId': 'fixture'}}
    container = {'name': 'worker', 'image': config['imageUri'], 'user': '10001:10001', 'readonlyRootFilesystem': True,
        'privileged': False, 'command': ['python', '-I', '/app/agent_e2e_container_entry.py'],
        'linuxParameters': {'initProcessEnabled': True, 'capabilities': {'drop': ['ALL']}},
        'environment': [{'name': 'HONUA_AGENT_TRANSPORT', 'value': json.dumps(config['transport'])},
                        {'name': 'AWS_DEFAULT_REGION', 'value': 'us-east-1'}],
        'secrets': [{'name': 'HONUA_AGENT_MCP_CONFIG', 'valueFrom':
            'arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-agent-fixture-mcp-AbCdEf:::'+'a'*32}]}
    if change == 'root': container['user'] = '0'
    elif change == 'writable': container['readonlyRootFilesystem'] = False
    elif change == 'command': container['command'] = ['sh']
    elif change == 'added-capability': container['linuxParameters']['capabilities']['add'] = ['SYS_ADMIN']
    elif change == 'controller-secret': container['secrets'][0]['name'] = 'FIXTURE_CLEANUP_DSN'
    elif change == 'transport': container['environment'][0]['value'] = '{}'
    if change is None:
        assert backend.verify_worker_definition({'containerDefinitions': [container]}, config) == container
    else:
        with pytest.raises(PackError):
            backend.verify_worker_definition({'containerDefinitions': [container]}, config)
