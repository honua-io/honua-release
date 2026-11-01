"""Recorded candidate responses; real recorder, frozen contracts and unchanged oracle.

No Docker or model service. Only the explicit A/B prompt copy is opened.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

import agent_e2e_local_runner as runner
from agent_e2e_local_api import RoleBinding, Stack
from agent_e2e_local_expectations import load_projections
from agent_e2e_local_projections import resolve_roles
from agent_e2e_local_recorder import ControllerRecorder, EvidenceVault, ProjectionError

ROOT = Path(__file__).resolve().parents[2]
AB_PACK = Path('/tmp/claude-1000/-home-mike-honua-io/c3861079-b75e-4621-81f6-60a567bcdb3b/scratchpad/pack.v2.AB-only.json')
# CI has the identical public A/B file, never the full pack.
if not AB_PACK.is_file():
    AB_PACK = ROOT / 'certification/agent-e2e/pack.v2.AB-only.json'
IDS = ('P14', 'P15', 'P16', 'P17', 'P18', 'P20')
PACK = {row['id']: row for row in json.loads(AB_PACK.read_text())['scenarios'] if row['id'] in IDS}
ITEM = '11111111-1111-1111-1111-111111111111'
VERSION = '22222222-2222-2222-2222-222222222222'
DRAFT = '33333333-3333-3333-3333-333333333333'
CONNECTION = '44444444-4444-4444-4444-444444444444'
HASH = 'a' * 64
DIGEST = 'sha256:' + 'b' * 64


def feature_response(variant):
    features = json.loads((ROOT / f'certification/agent-e2e/corpus/town-{variant}/shelters.3857.geojson').read_text())['features']
    return {'geometryType': 'esriGeometryPoint', 'exceededTransferLimit': False,
            'spatialReference': {'wkid': 3857}, 'objectIdFieldName': 'objectid',
            'fields': [{'name': 'objectid', 'type': 'esriFieldTypeOID'}] + [
                {'name': name, 'type': field_type} for name, field_type in
                [('capacity', 'esriFieldTypeInteger'), ('is_open', 'esriFieldTypeSmallInteger'),
                 ('name', 'esriFieldTypeString'), ('source_id', 'esriFieldTypeString')]],
            'features': [{'attributes': {'objectid': index + 1, **f['properties']},
                          'geometry': {'x': f['geometry']['coordinates'][0], 'y': f['geometry']['coordinates'][1]}}
                         for index, f in enumerate(features)]}


class RecordedCandidate:
    """Native response contracts, varied independently from frozen expectations.

    Import replies implement the synchronous contract, so TownSetup runs unchanged.
    No fabricated pagination/ownership fields are added to native proposal replies.
    """
    def __init__(self, variant):
        self.variant, self.wrong, self.calls = variant, None, []
        self.version = None
        self.tables = []
        self.published = False

    def http(self, recorder, principal, method, path, raw, headers):
        self.calls.append((recorder.phase, principal, method, path))
        body = json.loads(raw) if raw and headers.get('Content-Type') == 'application/json' else None
        status = 200
        if path == '/api/v1/admin/api-keys/':
            if principal == 'bootstrap':
                status, value = (200 if self.wrong == 'bootstrap-live' else 401), {'error': 'unauthorized'}
            elif method == 'POST':
                status, value = 201, {'data': {'apiKey': {'id': body['name'] + '-id'}}}
            else:
                value = {'data': []}
        elif path.endswith('/effective-permissions'):
            value = {'data': {'id': 'operator-id', 'permissions': ['read:*'], 'status': 'active', 'canAuthenticate': True}}
            if self.wrong == 'admin-operator':
                value['data']['permissions'] = ['admin:read']
        elif path == '/api/v1/admin/connections/' + CONNECTION:
            value = {'connectionId': CONNECTION}
        elif path.endswith('/tables'):
            value = {'tables': copy.deepcopy(self.tables)}
        elif path == '/api/v1/admin/import/upload':
            table = re.search(rb'name="TableName"\r\n\r\n([^\r]+)', raw).group(1).decode()
            name = next(n for n in ('parcels', 'flood_zones', 'shelters') if table.endswith('_' + n))
            count = len(json.loads((ROOT / f'certification/agent-e2e/corpus/town-{self.variant}/{name}.3857.geojson').read_text())['features'])
            self.tables.append({'schema': 'honua_data', 'table': table})
            value = {'success': True, 'tableName': table, 'featureCount': count,
                     'physicalTableName': table, 'schema': 'honua_data'}
        elif path == '/api/v1/operations/service.publish/validate':
            value = {'data': {'isValid': True}}
        elif path == '/api/v1/operations/service.publish/submit':
            value = {'data': {'operationId': 'service.publish', 'status': 'Completed',
                'operationInstanceId': 'publish-' + body['serviceName'], 'correlationId': 'correlation',
                'resourceIds': {'serviceName': body['serviceName'], 'layerId': '3'}}}
        elif path == '/api/v1/studio/package-drafts':
            status, value = 201, {'data': {'draftId': DRAFT, 'itemId': ITEM, 'ownerId': body['ownerId']}}
            self.envelope = body['envelope']
            self.package = body['packageKey']
        elif path.endswith('/content-versions'):
            self.version = {'itemId': ITEM, 'versionId': VERSION, 'contentHash': HASH,
                            'packageKey': self.package, 'envelope': self.envelope}
            status, value = 201, {'data': self.version}
        elif '/FeatureServer/' in path:
            value = feature_response(self.variant)
            if self.wrong == 'source-change' and recorder.phase == 'observe':
                value['features'][0]['attributes']['capacity'] = 999
        elif path.startswith('/api/v1/studio/published/'):
            if not self.published:
                status, value = 404, {'error': 'not found'}
            elif principal == 'anonymous' and self.wrong != 'anonymous-public':
                status = {'anonymous-forbidden': 403, 'anonymous-missing': 404}.get(self.wrong, 401)
                value = {'error': 'authentication required'}
                if self.wrong == 'disclosure':
                    value['itemId'] = ITEM
            else:
                value = {'data': {**self.version, 'visibility': 'team', 'route': '/chosen-by-model'}}
                if self.wrong == 'wrong-version':
                    value['data']['versionId'] = DRAFT
                if self.wrong == 'wrong-hash':
                    value['data']['contentHash'] = 'd' * 64
                if self.wrong == 'personal':
                    value['data']['visibility'] = 'personal'
        elif path == '/api/v1/admin/proposals':
            value = {'proposals': [] if recorder.phase == 'seed' else [
                {'proposalId': 'proposal-1', 'requestedBy': 'agent-id', 'status': 'AwaitingApproval'}]}
        else:
            raise AssertionError((principal, method, path))
        return status, json.dumps(value).encode()


def stack_fixture(tmp_path, monkeypatch, variant='A'):
    candidate = RecordedCandidate(variant)
    principals = {'anonymous': (None, None)}
    for name in ('controller', 'agent', 'operator', 'bootstrap'):
        env = 'G4_TEST_' + name.upper()
        monkeypatch.setenv(env, 'private-test-' + name)
        principals[name] = (env, name + '-id')
    vault = EvidenceVault('g4-test')
    recorder = ControllerRecorder(base_url='http://127.0.0.1:8147', workdir=tmp_path,
        candidate_digest=DIGEST, attempt_id='g4-test', principals=principals,
        http_factory=candidate.http, vault=vault)
    recorder.json('runner-custody-mint-bootstrap', 'POST', '/api/v1/admin/api-keys/', {'name': 'bootstrap'})
    def add(name, grants):
        assert name == 'operator' and grants == ['read:*']
        recorder.json('runner-principal-' + name, 'POST', '/api/v1/admin/api-keys/', {'name': name, 'permissions': grants})
        return name + '-id'
    stack = Stack(recorder.base_url, 'private-test-controller', None, 'g4-case', 'fake-compose', str(tmp_path), DIGEST,
        {'recorder': recorder, 'ensureConnection': lambda: CONNECTION, 'addPrincipal': add})
    return stack, candidate, vault


def seal_publication(recorder, vault, tmp_path, state, *, foreign=False, route='/chosen-by-model'):
    # Same runtime-sealed ActionLog protocol as the trunk fake candidate tests.
    log = runner.ActionLog(tmp_path / 'actions', 1, {'honua_studio_propose_publication'}, 10)
    args = {k: state[k] for k in ('itemId', 'versionId', 'contentHash')}
    args.update(route=route, visibility='team')
    if foreign:
        args['itemId'] = DRAFT
    log.dispatch('tool_call', {'kind': 'tool_call', 'tool': 'honua_studio_propose_publication', 'arguments': args},
                 lambda *_: {'status': 'ok'})
    vault.append_actions(1, log.rows)


def p15_case(tmp_path, monkeypatch, variant='A', wrong=None):
    stack, candidate, vault = stack_fixture(tmp_path, monkeypatch, variant)
    scenario = runner.load_scenario('P15')
    seeded = scenario.seed(stack, variant)
    rec = stack.extra['recorder']
    candidate.wrong = wrong
    if wrong != 'no-action':
        seal_publication(rec, vault, tmp_path, seeded.state, foreign=wrong == 'foreign-action',
                         route='/bad?query' if wrong == 'invalid-route' else '/chosen-by-model')
    candidate.published = wrong != 'unpublished'
    rec.set_phase('observe')
    observations = scenario.observe(stack, seeded, variant)
    roles = resolve_roles(runner.frozen_roles(scenario, stack, seeded, variant, None), vault.records(), agent_principal='agent-id')
    scored = runner.score_assertions(PACK['P15']['assertions'], observations, scenario.expected(variant),
        records=vault.records(), roles=roles, custody={'bootstrapPrincipalId': 'bootstrap-id',
            'bootstrapRevokedCredentialIds': [] if wrong == 'bootstrap-live' else ['bootstrap-id'],
            'postRevocationProbeStatus': 200 if wrong == 'bootstrap-live' else 401},
        scenario_id='P15', frozen=load_projections('P15', variant), model_actions=vault.model_actions())
    return {r['assertion']: r for r in scored}, stack, candidate


@pytest.mark.parametrize('variant', ('A', 'B'))
def test_p15_complete_recorded_state_and_seed_idempotence(tmp_path, monkeypatch, variant):
    stack, candidate, vault = stack_fixture(tmp_path, monkeypatch, variant)
    scenario = runner.load_scenario('P15')
    first = scenario.seed(stack, variant)
    count = len(candidate.calls)
    assert scenario.seed(stack, variant) == first
    assert len(candidate.calls) == count
    first.state['itemId'] = DRAFT
    assert scenario.seed(stack, variant).state['itemId'] == ITEM
    with pytest.raises(ValueError, match='different variant'):
        scenario.seed(stack, 'B' if variant == 'A' else 'A')
    result, stack, candidate = p15_case(tmp_path / 'score', monkeypatch, variant)
    assert set(result) == set(PACK['P15']['assertions'])
    assert {a: r['status'] for a, r in result.items()} == {
        'publication.private': 'pass', 'tenant.denied': 'excluded',
        'custody.bootstrap-revoked': 'pass', 'custody.worker-replaced': 'excluded',
        'source-content.unchanged': 'pass'}
    # The seed creates no map publication, and observe performs no mutation.
    assert not any(phase == 'observe' and method != 'GET' for phase, _, method, _ in candidate.calls)


@pytest.mark.parametrize('variant', ('A', 'B'))
@pytest.mark.parametrize('wrong,assertion', [
    ('anonymous-public', 'publication.private'), ('anonymous-missing', 'publication.private'),
    ('disclosure', 'publication.private'),
    ('unpublished', 'publication.private'), ('foreign-action', 'publication.private'),
    ('no-action', 'publication.private'), ('source-change', 'source-content.unchanged'),
    ('bootstrap-live', 'custody.bootstrap-revoked')])
def test_p15_wrong_live_state_fails_real_oracle(tmp_path, monkeypatch, variant, wrong, assertion):
    good, _, _ = p15_case(tmp_path / 'good', monkeypatch, variant)
    bad, _, _ = p15_case(tmp_path / 'bad', monkeypatch, variant, wrong)
    assert good[assertion]['status'] == 'pass', good[assertion]
    assert bad[assertion]['status'] == 'fail', bad[assertion]
    if wrong in ('anonymous-public', 'disclosure', 'source-change', 'bootstrap-live'):
        assert 'unrecorded' not in bad[assertion].get('reason', '')


def test_p15_invalid_route_fails_publication_without_erasing_independent_evidence(tmp_path, monkeypatch):
    result, _, _ = p15_case(tmp_path, monkeypatch, wrong='invalid-route')
    assert result['publication.private']['status'] == 'fail'
    assert result['source-content.unchanged']['status'] == 'pass'
    assert result['custody.bootstrap-revoked']['status'] == 'pass'


def test_p15_anonymous_forbidden_is_also_a_valid_denial(tmp_path, monkeypatch):
    result, _, _ = p15_case(tmp_path, monkeypatch, wrong='anonymous-forbidden')
    assert result['publication.private']['status'] == 'pass', result


@pytest.mark.parametrize('wrong', ('admin-operator', 'wrong-version', 'wrong-hash', 'personal'))
def test_p15_rejects_misbound_or_privileged_positive_control(tmp_path, monkeypatch, wrong):
    with pytest.raises(ValueError, match='operator is not|exact team-visible'):
        p15_case(tmp_path, monkeypatch, wrong=wrong)


@pytest.mark.parametrize('sid', IDS)
def test_inventory_is_ab_only_and_exclusions_never_pass(sid, tmp_path):
    scenario = runner.load_scenario(sid)
    for variant in ('A', 'B'):
        assert list(scenario.expected(variant)) == PACK[sid]['assertions']
        assert scenario.fault_plan(variant) is None
        assert scenario.customer_files(variant) == []
    assert scenario.needs_approver() == (sid in ('P14', 'P17'))
    if sid not in ('P15', 'P18'):
        assert scenario.supported_variants == ()
        assert 'Blocked' in __import__('importlib').import_module('agent_e2e_local_scenarios.' + sid).__doc__
        stack = Stack('http://127.0.0.1:8147', 'unused', None, 'case', 'compose', str(tmp_path), DIGEST)
        with pytest.raises(NotImplementedError, match=sid):
            scenario.seed(stack, 'A')
    if sid == 'P18':
        # Same scorer path used by the runner, no observation can turn an exclusion into pass.
        rows = runner.score_assertions(PACK[sid]['assertions'], [], scenario.expected('A'), records={}, scenario_id=sid)
        assert all(r['status'] == 'excluded' and r['ruling'] == 'P18' for r in rows)


def test_native_proposal_shape_cannot_bind_created_role_without_invented_completeness(tmp_path, monkeypatch):
    stack, _, vault = stack_fixture(tmp_path, monkeypatch)
    rec = stack.extra['recorder']
    rec.json('proposals-before', 'GET', '/api/v1/admin/proposals')
    rec.set_phase('observe')
    rec.json('proposals-after', 'GET', '/api/v1/admin/proposals')
    # Native ProposalListResponse has only proposals. Missing completeness must
    # not be 'fixed' by the scenario adding a synthetic total or nextCursor.
    for completeness in ({'total': '/total'}, {'next_page': '/nextCursor'}):
        binding = RoleBinding('created:proposal', 'proposals-after', items='/proposals', id='/proposalId',
                              owner='/requestedBy', before='proposals-before', **completeness)
        with pytest.raises(ProjectionError, match='absent|reviewed enumeration contract'):
            resolve_roles([binding], vault.records(), agent_principal='agent-id')


@pytest.mark.parametrize('sid,assertion,correct,wrong', [
    ('P14', 'approval.separate',
     {'proposalId': 'p', 'requesterPrincipalId': 'requester', 'approverPrincipalId': 'approver',
      'approvedVersionId': 'v', 'auditId': 'a'}, {'approverPrincipalId': 'requester'}),
    ('P17', 'approval.pending',
     {'proposalId': 'p', 'status': 'AwaitingApproval', 'publishedResourceIds': []}, {'publishedResourceIds': ['published']}),
    ('P20', 'clarification.required',
     {'askedTopics': ['map-identity', 'audience'], 'answerMessageIds': ['S1-M4', 'S1-M6'], 'publicationAfterAnswers': True},
     {'publicationAfterAnswers': False}),
])
def test_blocked_assertions_have_nonvacuous_wrong_state_controls(sid, assertion, correct, wrong):
    # Predicate controls establish the required security semantics; these are
    # explicitly not claimed as runnable module evidence.
    assert assertion in PACK[sid]['assertions']
    runner.oracle.verify(assertion, correct, correct)
    with pytest.raises(runner.oracle.VerificationError):
        runner.oracle.verify(assertion, {**correct, **wrong}, correct)


def test_p16_key_revocation_wrong_state_control():
    expected = {'requests': [
        {'principalId': 'issued', 'method': 'GET', 'resourceId': 'owned', 'status': 200},
        {'principalId': 'issued', 'method': 'GET', 'resourceId': 'owned', 'status': 401}]}
    correct = copy.deepcopy(expected)
    for row in correct['requests']:
        row.update(responseSha256='a' * 64, disclosedResourceIds=[])
    runner.oracle.verify('key.revoked', correct, expected)
    wrong = copy.deepcopy(correct)
    wrong['requests'][1]['status'] = 200
    with pytest.raises(runner.oracle.VerificationError):
        runner.oracle.verify('key.revoked', wrong, expected)


def test_p18_changed_map_is_wrong_even_though_case_is_excluded():
    body = json.loads((ROOT / 'certification/terminal-journey/targets/local-docker.json').read_text())['execution']['mapBody']
    resource = {'resourceId': 'foreign-map', 'content': body}
    expected = {'profile': 'honua.protected-content/v1', 'resources': [resource]}
    correct = {'profile': expected['profile'], 'before': [copy.deepcopy(resource)], 'after': [copy.deepcopy(resource)]}
    runner.oracle.verify('mutations.absent', correct, expected)
    correct['after'][0]['content']['initialView']['zoom'] += 1
    with pytest.raises(runner.oracle.VerificationError):
        runner.oracle.verify('mutations.absent', correct, expected)


def test_p20_reply_id_positions_depend_on_real_session_actions(tmp_path):
    from test_agent_e2e_local_runner import FakeModel
    def final(answer):
        return json.dumps({'kind': 'final', 'answer': answer})
    def session(name, first):
        model = FakeModel(first + [final('Which map?'), final('Which audience?'), final('No changes made.')])
        return runner.run_model_session(prompt=PACK['P20']['prompts']['A'],
            tool_view={'tools': [{'name': 'honua_query_features', 'inputSchema': {'type': 'object'}}]},
            client=model, execute=lambda *_: {'status': 'ok'}, log_dir=tmp_path / name,
            session=1, max_model_calls=10, max_actions=10, max_tokens=100000,
            certification={'runNonce': name}, candidate_release='2026.1-rc.2',
            clarifications=PACK['P20']['clarifications'])
    first = session('questions-first', [])
    later = session('query-first', [json.dumps({'kind': 'tool_call', 'tool': 'honua_query_features', 'arguments': {}})])
    wanted = {'askedTopics': [c['topic'] for c in first['clarifications']],
              'answerMessageIds': [c['messageId'] for c in first['clarifications']], 'publicationAfterAnswers': True}
    observed = {**wanted, 'answerMessageIds': [c['messageId'] for c in later['clarifications']]}
    assert wanted['askedTopics'] == observed['askedTopics'] == ['map-identity', 'audience']
    assert wanted['answerMessageIds'] != observed['answerMessageIds']
    # Freezing one valid conversation's positions would reject a different valid
    # discovery order. This is a core binding gap, not a model's wrong state.
    with pytest.raises(runner.oracle.VerificationError):
        runner.oracle.verify('clarification.required', observed, wanted)


def test_p15_model_context_has_only_ab_prompt_and_product_tools(tmp_path, monkeypatch):
    from test_agent_e2e_local_runner import Candidate, FakeModel, FakeRuntime, serve
    recorded = RecordedCandidate('A')

    class SharingCandidate(Candidate):
        TOOLS = [{'name': 'honua_studio_propose_publication', 'description': 'Publish a saved map.',
                  'inputSchema': {'type': 'object'}}]

        def mcp(self, message, principal):
            if message.get('method') == 'tools/call':
                recorded.published = True
                self.mcp_calls.append((principal['id'], message['params']['name'], message['params']['arguments']))
                return {'content': [], 'isError': False, 'structuredContent': {'status': 'Published'}}
            return super().mcp(message, principal)

    candidate = SharingCandidate()
    server = serve(candidate)
    base = f'http://127.0.0.1:{server.server_address[1]}'

    class SharingRuntime(FakeRuntime):
        def setup(self, *args, **kwargs):
            stack = super().setup(*args, **kwargs)
            rec = stack.extra['recorder']
            rec._http_factory = recorded.http
            stack.extra['ensureConnection'] = lambda: CONNECTION
            # Model custody is still real FakeRuntime custody; the separate
            # operator mint uses the native response fixture and runner helper.
            return stack

    def action(_messages):
        return json.dumps({'kind': 'tool_call', 'tool': 'honua_studio_propose_publication',
            'arguments': {**{k: recorded.version[k] for k in ('itemId', 'versionId', 'contentHash')},
                          'route': '/chosen-by-model', 'visibility': 'team'}})
    model = FakeModel([action, json.dumps({'kind': 'final', 'answer': 'Operators can now open the map. Anonymous access is denied.'})])
    runtime = SharingRuntime(base, model, tmp_path)
    # The model gets no principal/key/bootstrap fixture or seed parameters.
    original_http = recorded.http
    def with_issued_key(rec, principal, method, path, raw, headers):
        if path == '/api/v1/admin/api-keys/' and method == 'POST':
            return 201, json.dumps({'data': {'apiKey': {'id': 'operator-id'}, 'key': 'operator-secret-' + 'x' * 32}}).encode()
        return original_http(rec, principal, method, path, raw, headers)
    recorded.http = with_issued_key
    try:
        receipt = runner.execute_case(case_id='P15:A:local-docker', scenario=runner.load_scenario('P15'),
            variant='A', case=PACK['P15'], candidate_digest=DIGEST, runtime=runtime)
    finally:
        server.shutdown()
        server.server_close()
    assert receipt['status'] == 'pass', receipt
    assert runtime.torn_down
    assert len(candidate.mcp_calls) == 1
    first = json.loads(model.sent[0][1]['content'])
    assert first['stage']['customerRequest'] == PACK['P15']['prompts']['A']
    assert not any(k in first for k in runner.STRIPPED_USER_FIELDS)
    raw = json.dumps(model.sent[0])
    assert all(secret not in raw for secret in runtime._secrets)
    assert ITEM not in raw and VERSION not in raw and 'processId' not in raw
    assert all(name not in raw for name in ('itemId', 'versionId', 'contentHash', 'requiredSequence'))
    runner.validate_receipt(receipt)
