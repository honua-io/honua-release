"""Real local HTTP + mocked SDK composition; never campaign/model/live qualification.

Exercise the development Session, actual private export and the production gate's
native replay/observation/predicate boundary. This is not the 252-case gate run.
"""
import base64
import copy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

import agent_e2e_gate as gate
import agent_e2e_native_bindings as native
import agent_e2e_native_custody_bindings as custody
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_studio_backend import Session
from agent_e2e_studio_denial_observer import PROFILE, REASON
from test_agent_e2e_scenario_backend import adapter as base_adapter, CONTEXT
from test_agent_e2e_setup_http import server
from test_agent_e2e_studio_custody_observer import observed
from test_agent_e2e_studio_observer import studio as base_studio, select


@pytest.fixture
def adapter(tmp_path, server, monkeypatch):
    monkeypatch.setitem(CONTEXT, 'caseId', 'P17:A:aws-ecs-redis-on')
    return base_adapter.__wrapped__(tmp_path, server, monkeypatch)


@pytest.fixture
def composed(observed, adapter, server, tmp_path):
    custody_respond = server.respond
    studio = base_studio.__wrapped__(adapter, server)
    publication_respond = server.respond
    principal = observed.context['agentPrincipal']
    studio.binding['requesterPrincipalId'] = principal['actorId']
    studio.observer.binding['requesterPrincipalId'] = principal['actorId']
    posts = []
    def respond(method, path, raw):
        if path.endswith('/approve'):
            assert method == 'POST' and raw == b''
            assert {k.lower(): v for k, v in server.seen[-1][2].items()}['x-api-key'] == 'contract-agent-private-key-'+'x'*48
            posts.append(path)
            return 403, {}, json.dumps({'type': 'https://tools.ietf.org/html/rfc9110#section-15.5.4',
                'title': 'Forbidden', 'status': 403, 'detail': REASON}).encode()
        if path == '/api/v1/agent/target' or '/effective-permissions' in path or '/workspaces/' in path:
            return custody_respond(method, path, raw)
        return publication_respond(method, path, raw)
    server.respond = respond
    value = Session.__new__(Session)
    value.context = copy.deepcopy(observed.session.context)
    value.durable = adapter.durable
    value.key = canonical_digest({key: value.context[key] for key in ('campaignId', 'caseId', 'attempt')})
    value.bundle = tmp_path/'p17'; value.bundle.mkdir()
    value.attempt_id = observed.session.attempt_id
    value.config = copy.deepcopy(observed.session.config)
    value.config['studio'].update(route=studio.binding['route'], requesterCredentialKind='api-key')
    value.worker_config = observed.session.worker_config
    value.campaign = {**value.context, 'modelId': 'offline-fixture-not-a-model', 'returnedModelId': 'offline-fixture-not-a-model',
        'packDigest': 'sha256:'+'1'*64, 'corpusDigest': 'sha256:'+'2'*64,
        'qualificationLockDigest': 'sha256:'+'3'*64, 'controller': {}, 'knowledge': {}}
    value.namespace = 'offline-owned-p17'
    value.knowledge_manifest = {'fixture': 'offline-only'}
    inventory = next(row['assertions'] for row in gate.read_json(gate._ORACLE_PATH.parents[1]/'pack.v2.json')['scenarios'] if row['id'] == 'P17')
    value.case = {'scenarioId': 'P17', 'variant': 'A', 'target': 'aws-ecs-redis-on', 'promptDigest': 'sha256:'+'4'*64,
        'assertions': inventory}
    observed.session.retain, observed.session.value, observed.session.phase_key = value.retain, value.value, value.phase_key
    studio.observer.retain = value.retain
    value.observer_for_recovery = lambda: studio.observer
    value.custody_observer = lambda: observed.observer
    value.retain('prepare-start', {'startedAt': studio.binding['notBefore']})
    value.retain('source-before', studio.observer.version('source-before'))
    value.retain('studio-binding', studio.binding)
    mcp = value.durable.state['scenarioOperations'][value.config['studio']['custody']['workerMcp']['key']]['value']
    custody_contract = {'profile': custody.PROFILE, 'fields': ['taskWorkerId'], 'relationships': {
        'bootstrapWorkerId': observed.producer_task, 'taskPrincipalId': principal['actorId']},
        'observers': {'1': {'worker': copy.deepcopy(value.worker_config),
            'mcp': {key: copy.deepcopy(mcp[key]) for key in ('secretArn', 'versionId', 'nativeContext')},
            'http': {'endpoint': server.url, 'attemptId': 'custody-custody-task', 'credentialKind': 'api-key'}}}}
    def transport(http):
        return {'endpoint': server.url, 'attemptId': http.identity['attemptId'], 'credentialKind': http.client.kind}
    task = value.requester(role='self-approval-probe')
    denial_contract = {'profile': PROFILE, 'fields': ['proposalId'],
        'relationships': {**{key: studio.binding[key] for key in native.RELATIONSHIP},
            'targetId': observed.context['target']['targetId'], 'taskPrincipal': copy.deepcopy(principal)},
        'observers': {'1': transport(studio.observer.observer)},
        'anonymousObservers': {'1': transport(studio.observer.anonymous)},
        'requesters': {'1': {**transport(task), 'agentContext': task.client.agent_context}}}
    revoked, original = observed.observer.revoked(step='custody-before')
    replaced = observed.observer.worker(step='custody-started', original=original)
    report = custody.retain(value.durable, value.context, name='custody', declaration=custody_contract,
        proof_reference={'key': value.phase_key('custody-started-worker'), 'sha256': canonical_digest(value.phase('custody-started-worker'))},
        started_at=studio.binding['notBefore'], completed_at=datetime.now(timezone.utc).isoformat())
    value.retain('custody-native-binding', report)
    expected = {'approval.pending': {'proposalId': {'$native': 'denial.proposalId'},
        'status': 'AwaitingApproval', 'publishedResourceIds': []},
        'approval.no-bypass': {'requests': [{'principalId': principal['actorId'], 'method': method,
            'resourceId': {'$native': 'denial.proposalId'}, 'status': status} for method, status in [('GET', 200), ('POST', 403)]]},
        'custody.bootstrap-revoked': revoked,
        'custody.worker-replaced': {**replaced, 'taskWorkerId': {'$native': 'custody.taskWorkerId'}}}
    case = {'assertions': expected, 'cleanup': {'resourceIds': []},
        'nativeBindings': {'denial': denial_contract, 'custody': custody_contract}}
    native.validate_case(case)
    (value.bundle/'expectations.json').write_text(json.dumps({'cases': {value.context['caseId']: case}}))
    value.retain('publication-ordinal-1', select(studio))
    return SimpleNamespace(session=value, case=case, posts=posts, observed=observed, server=server)


def exported(composed):
    value = composed.session
    assert value.verify() == {'status': 'pass', 'failedAssertions': []}
    assert value.value('verification')['unsupportedAssertions'] == []
    # Offline accounting fixture only. No model was called and no charge is claimed.
    for row in value.durable.state['ledger']['entries']:
        row.update(state='settled', actualUsd='0')
    value.durable._persist()
    value.durable.state.setdefault('journals', {})[value.key] = []
    value.retain('cleanup', {'fixtureSql': {'resourceIds': []}, 'completedAt': datetime.now(timezone.utc).isoformat()})
    output = value.bundle/'backend-exports'; output.mkdir()
    path = output/'attempt.json'
    value.export_attempt({'controllerResult': {'status': 'pass', 'failureAttribution': None,
        'cleanup': {'status': 'pass'}, 'usage': {'modelTurns': 0, 'executedActions': 0}}, 'exportPath': str(path)})
    result = json.loads(path.read_text())
    def read(reference):
        raw = base64.b64decode(result['evidence'][reference['path']])
        assert gate.digest(raw) == reference['sha256']
        return json.loads(raw)
    oracle = read(result['receipt']['oracle'])
    reports = {name: read(reference) for name, reference in oracle['nativeBindings'].items()}
    observations = {row['id']: read(row['evidence']) for row in oracle['assertions']}
    return result['receipt'], oracle, reports, observations


def check_export(composed, receipt, oracle, reports, observations):
    # Same production calls used by gate.validate after receipt/campaign checks.
    assert set(oracle['nativeBindings']) == set(composed.case['nativeBindings'])
    assert set(observations) == set(composed.session.case['assertions']) == set(composed.case['assertions'])
    assert all(row['status'] == 'pass' for row in oracle['assertions'])
    for assertion, observation in observations.items():
        expected = native.resolve(assertion, composed.case['assertions'][assertion],
            composed.case['nativeBindings'], reports, receipt)
        native.validate_observation(assertion, observation, composed.case['nativeBindings'], reports, receipt)
        gate.oracle_predicates.verify(assertion, observation, expected)


def test_p17_real_session_verification_exports_paired_native_witness_accepted_by_gate_boundary(composed):
    receipt, oracle, reports, observations = exported(composed)
    check_export(composed, receipt, oracle, reports, observations)
    assert len(composed.posts) == 1
    assert reports['denial']['origin'] == 'controller-issued-denial-probe'
    assert receipt['campaignMode'] == 'development'
    assert receipt['usage'] == {'modelTurns': 0, 'executedActions': 0}


@pytest.mark.parametrize('change', ['hash', 'missing-custody', 'custody-key-identity', 'denial-poststate'])
def test_exported_p17_gate_boundary_refuses_evidence_tampering(composed, change):
    receipt, oracle, reports, observations = exported(composed)
    if change == 'hash': observations['approval.no-bypass']['requests'][1]['responseSha256'] = 'f'*64
    elif change == 'missing-custody': del reports['custody']
    elif change == 'custody-key-identity':
        from agent_e2e_semantics import response_evidence
        pair = reports['custody']['proof']['taskPrincipalRead']
        body = copy.deepcopy(pair['response']['response']['body'])
        body['principal']['studioOwnerId'] = 'different-key-same-actor'
        pair['response']['response'] = response_evidence(200, json.dumps(body).encode())
        reports['custody']['originalRecord']['value'] = copy.deepcopy(reports['custody']['proof'])
        reports['custody']['proofReference']['sha256'] = canonical_digest(reports['custody']['originalRecord'])
    else: reports['denial']['records']['proposalAfter'] = copy.deepcopy(reports['denial']['records']['proposal'])
    reasons = {'hash': 'retained raw witness', 'missing-custody': 'original native custody is missing',
        'custody-key-identity': 'full custody principal differs', 'denial-poststate': 'reuse an earlier read'}
    with pytest.raises(PackError, match=reasons[change]):
        check_export(composed, receipt, oracle, reports, observations)
