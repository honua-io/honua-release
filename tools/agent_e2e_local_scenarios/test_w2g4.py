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
from agent_e2e_local_api import Observation, RoleBinding, Seeded, Stack
from agent_e2e_local_expectations import load_projections
from agent_e2e_local_projections import resolve_roles
from agent_e2e_local_recorder import ControllerRecorder, EvidenceVault, ProjectionError

ROOT = Path(__file__).resolve().parents[2]
AB_PACK = ROOT / 'certification/agent-e2e/pack.v2.AB-only.json'
IDS = ('P14', 'P15', 'P16', 'P17', 'P18', 'P20')
PACK = {row['id']: row for row in json.loads(AB_PACK.read_text())['scenarios'] if row['id'] in IDS}
ITEM = '11111111-1111-1111-1111-111111111111'
VERSION = '22222222-2222-2222-2222-222222222222'
DRAFT = '33333333-3333-3333-3333-333333333333'
OTHER_DRAFT = '55555555-5555-5555-5555-555555555555'
OTHER_ITEM = '66666666-6666-6666-6666-666666666666'
OTHER_VERSION = '77777777-7777-7777-7777-777777777777'
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

    Import replies cover both synchronous and queued completion contracts.
    No fabricated pagination/ownership fields are added to native proposal replies.
    """
    def __init__(self, variant):
        self.variant, self.wrong, self.calls = variant, None, []
        self.version = None
        self.tables = []
        self.published = False
        self.drafts = {}
        self.async_import = False
        self.jobs = {}

    def sql(self, request):
        from agent_e2e_local_runner import inventory_sql
        assert request == {'op': 'inventory', 'schema': 'honua_data'}
        return 200, json.dumps([{'table_schema': t['schema'], 'table_name': t['table']} for t in self.tables]).encode(), inventory_sql('honua_data')

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
            if self.async_import:
                job = 'job-' + str(len(self.jobs) + 1)
                self.jobs[job] = (table, count)
                status, value = 202, {'success': True, 'jobId': job, 'statusUrl': '/api/v1/admin/import/jobs/' + job}
            else:
                self.tables.append({'schema': 'honua_data', 'table': table})
                value = {'success': True, 'tableName': table, 'featureCount': count,
                         'physicalTableName': table, 'schema': 'honua_data'}
        elif path.startswith('/api/v1/admin/import/jobs/'):
            job = path.rsplit('/', 1)[1]
            table, count = self.jobs[job]
            if self.wrong != 'async-missing' and not any(t['table'] == table for t in self.tables):
                self.tables.append({'schema': 'honua_data', 'table': table})
                if self.wrong == 'async-extra':
                    self.tables.append({'schema': 'honua_data', 'table': 'unexpected'})
            value = {'jobId': job, 'tableName': table, 'status': 3 if self.wrong == 'async-failed' else 2,
                     'featuresProcessed': count, 'failedFeatures': 0, 'completedAt': '2026-10-31T00:00:00Z'}
        elif path == '/api/v1/operations/service.publish/validate':
            value = {'data': {'isValid': True}}
        elif path == '/api/v1/operations/service.publish/submit':
            value = {'data': {'operationId': 'service.publish', 'status': 'Completed',
                'operationInstanceId': 'publish-' + body['serviceName'], 'correlationId': 'correlation',
                'resourceIds': {'serviceName': body['serviceName'], 'layerId': '3'}}}
        elif path == '/api/v1/studio/package-drafts':
            draft_id = DRAFT if not self.drafts else OTHER_DRAFT
            item_id = ITEM if not self.drafts else OTHER_ITEM
            self.drafts[draft_id] = {'itemId': item_id, 'ownerId': body['ownerId'],
                                     'envelope': body['envelope'], 'packageKey': body['packageKey']}
            status, value = 201, {'data': {'draftId': draft_id, 'itemId': item_id, 'ownerId': body['ownerId']}}
        elif path.endswith('/content-versions'):
            draft_id = path.split('/')[-2]
            row = self.drafts[draft_id]
            version = {**row, 'versionId': VERSION if draft_id == DRAFT else OTHER_VERSION, 'contentHash': HASH}
            if draft_id == DRAFT and self.wrong == 'seed-owner':
                version['ownerId'] = 'foreign-owner'
            if draft_id == DRAFT and self.wrong == 'seed-item':
                version['itemId'] = OTHER_ITEM
            if draft_id == DRAFT and self.wrong == 'seed-hash':
                version['contentHash'] = 'invalid'
            if draft_id == DRAFT:
                self.version = version
            status, value = 201, {'data': version}
        elif '/FeatureServer/' in path:
            value = feature_response(self.variant)
            if self.wrong == 'source-change' and recorder.phase == 'observe':
                value['features'][0]['attributes']['capacity'] = 999
        elif path.startswith('/api/v1/studio/published/'):
            if not self.published:
                status, value = 404, {'error': 'not found'}
            elif principal == 'anonymous' and self.wrong != 'anonymous-public':
                status, value = 401, {'error': 'authentication required'}
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
    for name in ('controller', 'agent', 'operator', 'otherteam', 'approver', 'bootstrap'):
        env = 'G4_TEST_' + name.upper()
        monkeypatch.setenv(env, 'private-test-' + name)
        principals[name] = (env, name + '-id')
    vault = EvidenceVault('g4-test')
    recorder = ControllerRecorder(base_url='http://127.0.0.1:8147', workdir=tmp_path,
        candidate_digest=DIGEST, attempt_id='g4-test', principals=principals,
        http_factory=candidate.http, sql_executor=candidate.sql, vault=vault)
    recorder.json('runner-custody-mint-bootstrap', 'POST', '/api/v1/admin/api-keys/', {'name': 'bootstrap'})
    def add(name, grants):
        assert name in ('operator', 'otherteam') and grants == ['read:*']
        recorder.json('runner-principal-' + name, 'POST', '/api/v1/admin/api-keys/', {'name': name, 'permissions': grants})
        return name + '-id'
    stack = Stack(recorder.base_url, 'private-test-controller', None, 'g4-case', 'fake-compose', str(tmp_path), DIGEST,
        {'recorder': recorder, 'ensureConnection': lambda: CONNECTION, 'addPrincipal': add})
    return stack, candidate, vault


def seal_publication(recorder, vault, tmp_path, state, *, foreign=False, visibility='team'):
    # Same runtime-sealed ActionLog protocol as the trunk fake candidate tests.
    log = runner.ActionLog(tmp_path / 'actions', 1, {'honua_studio_propose_publication'}, 10)
    args = {k: state[k] for k in ('itemId', 'versionId', 'contentHash')}
    args.update(route='/chosen-by-model', visibility=visibility)
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
        seal_publication(rec, vault, tmp_path, seeded.state, foreign=wrong == 'foreign-action')
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
    ('anonymous-public', 'publication.private'), ('disclosure', 'publication.private'),
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
            rec._sql = recorded.sql
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


@pytest.mark.parametrize('sid', ('P14', 'P15', 'P16', 'P17', 'P20'))
@pytest.mark.parametrize('variant', ('A', 'B'))
def test_governance_seed_is_idempotent_and_scoped_to_the_case(tmp_path, monkeypatch, sid, variant):
    stack, candidate, _ = stack_fixture(tmp_path, monkeypatch, variant)
    scenario = runner.load_scenario(sid)
    seeded = scenario.seed(stack, variant)
    count = len(candidate.calls)
    assert scenario.seed(stack, variant) == seeded
    assert len(candidate.calls) == count
    assert seeded.state['variant'] == variant
    if sid in ('P14', 'P15', 'P17', 'P20'):
        assert seeded.state['itemId'] == ITEM
    if sid == 'P20':
        assert seeded.state['otherItemId'] == OTHER_ITEM
        assert candidate.drafts[DRAFT]['packageKey'] == candidate.drafts[OTHER_DRAFT]['packageKey']
        assert candidate.drafts[DRAFT]['ownerId'] != candidate.drafts[OTHER_DRAFT]['ownerId']
    # Seed imports never use an unreviewed table inventory or publish a Studio map.
    assert not any(path.endswith('/tables') for _, _, _, path in candidate.calls)
    assert not any('/published/' in path for _, _, _, path in candidate.calls)
    assert not any(method == 'POST' and path.startswith('/api/v1/admin/proposals/')
                   for _, _, method, path in candidate.calls)


@pytest.mark.parametrize('visibility', ('public', 'team'))
def test_p14_does_not_approve_or_enumerate_without_a_reviewed_locator(tmp_path, monkeypatch, visibility):
    stack, candidate, vault = stack_fixture(tmp_path, monkeypatch)
    scenario = runner.load_scenario('P14')
    seeded = scenario.seed(stack, 'A')
    rec = stack.extra['recorder']
    seal_publication(rec, vault, tmp_path, seeded.state, visibility=visibility)
    approved = []
    stack.extra['approve'] = approved.append
    rec.set_phase('session')
    rec.after_session = 1
    with rec.between():
        scenario.between_sessions(stack, seeded, 'A', 1)
    assert approved == []
    assert not any(path == '/api/v1/admin/proposals' for _, _, _, path in candidate.calls)


@pytest.mark.parametrize('sid', ('P14', 'P16', 'P17'))
@pytest.mark.parametrize('wrong', (None, 'bootstrap-live', 'source-change'))
def test_blocked_scenario_available_fixture_assertions_are_independent(tmp_path, monkeypatch, sid, wrong):
    stack, candidate, vault = stack_fixture(tmp_path, monkeypatch)
    scenario = runner.load_scenario(sid)
    seeded = scenario.seed(stack, 'A')
    candidate.wrong = wrong
    rec = stack.extra['recorder']
    rec.set_phase('observe')
    observations = scenario.observe(stack, seeded, 'A')
    roles = resolve_roles(scenario.roles(stack, seeded, 'A'), vault.records(), agent_principal='agent-id')
    custody = {'bootstrapPrincipalId': 'bootstrap-id', 'bootstrapRevokedCredentialIds': ['bootstrap-id']}
    rows = runner.score_assertions(PACK[sid]['assertions'], observations, scenario.expected('A'),
        records=vault.records(), roles=roles, custody=custody, scenario_id=sid,
        frozen=load_projections(sid, 'A'))
    by_id = {row['assertion']: row for row in rows}
    assert by_id['custody.bootstrap-revoked']['status'] == ('fail' if wrong == 'bootstrap-live' else 'pass')
    if sid == 'P14':
        assert by_id['source-content.unchanged']['status'] == ('fail' if wrong == 'source-change' else 'pass')
    # Partial evidence never turns the blocked key/approval assertions into a pass.
    assert any(row['status'] == 'fail' for row in rows)
    if sid == 'P16':
        assert not any(path == '/api/v1/admin/api-keys/' and principal == 'controller' and method == 'GET'
                       for _, principal, method, path in candidate.calls)


@pytest.mark.parametrize('variant', ('A', 'B'))
@pytest.mark.parametrize('wrong', (None, 'anonymous-public', 'foreign-action'))
def test_p20_private_projection_is_independent_but_clarification_still_fails(tmp_path, monkeypatch, variant, wrong):
    stack, candidate, vault = stack_fixture(tmp_path, monkeypatch, variant)
    scenario = runner.load_scenario('P20')
    seeded = scenario.seed(stack, variant)
    assert candidate.drafts[DRAFT]['packageKey'] == candidate.drafts[OTHER_DRAFT]['packageKey'] == 'shelters-map'
    rec = stack.extra['recorder']
    seal_publication(rec, vault, tmp_path, seeded.state, foreign=wrong == 'foreign-action')
    candidate.published = True
    candidate.wrong = wrong
    rec.set_phase('observe')
    observations = scenario.observe(stack, seeded, variant)
    roles = resolve_roles(scenario.roles(stack, seeded, variant), vault.records(), agent_principal='agent-id')
    rows = runner.score_assertions(PACK['P20']['assertions'], observations, scenario.expected(variant),
        records=vault.records(), roles=roles, scenario_id='P20', frozen=load_projections('P20', variant),
        model_actions=vault.model_actions())
    by_id = {row['assertion']: row for row in rows}
    assert by_id['publication.private']['status'] == ('pass' if wrong is None else 'fail')
    assert by_id['clarification.required']['status'] == 'fail'


@pytest.mark.parametrize('sid', ('P14', 'P17'))
@pytest.mark.parametrize('foreign', (False, True))
def test_proposal_result_is_only_a_locator_and_is_checked_with_fresh_read(tmp_path, monkeypatch, sid, foreign):
    stack, candidate, vault = stack_fixture(tmp_path, monkeypatch)
    scenario = runner.load_scenario(sid)
    seeded = scenario.seed(stack, 'A')
    rec = stack.extra['recorder']
    log = runner.ActionLog(tmp_path / 'proposal-actions', 1, {'honua_studio_propose_publication'}, 10)
    arguments = {key: seeded.state[key] for key in ('itemId', 'versionId', 'contentHash')}
    arguments.update(route='/chosen-by-model', visibility='public')
    result = json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'structuredContent': {
        'proposalId': 'p-one', 'status': 'AwaitingApproval'}}}).encode()
    log.dispatch('tool_call', {'kind': 'tool_call', 'tool': 'honua_studio_propose_publication', 'arguments': arguments},
                 lambda *_: {'status': 'ok', '_rawResponse': (200, result)})
    vault.append_actions(1, log.rows)
    original = candidate.http
    def http(recorder, principal, method, path, raw, headers):
        if path == '/api/v1/admin/proposals/p-one':
            item_id = OTHER_ITEM if foreign else seeded.state['itemId']
            diff = [f'{key}={value}' for key, value in (
                ('itemId', item_id), ('versionId', seeded.state['versionId']),
                ('contentHash', seeded.state['contentHash']), ('route', '/chosen-by-model'), ('visibility', 'public'))]
            return 200, json.dumps({'proposalId': 'p-one', 'kind': 'StudioDraftMutation',
                'status': 'AwaitingApproval', 'requestedBy': 'agent-id', 'diff': diff}).encode()
        return original(recorder, principal, method, path, raw, headers)
    rec._http_factory = http
    rec.set_phase('observe')
    if foreign:
        with pytest.raises(ValueError, match='native proposal differs'):
            scenario.observe(stack, seeded, 'A')
    else:
        scenario.observe(stack, seeded, 'A')
    prefix = sid.lower()
    assert rec.records[prefix + '-action-result'].origin == 'session-result'
    assert rec.records[prefix + '-action-result'].replay_of.startswith('occ:1:1:')
    assert rec.records[prefix + '-proposal'].principal == 'controller'
    assert rec.records[prefix + '-proposal'].json['proposalId'] == 'p-one'


def test_source_projection_rejects_service_and_layer_role_swaps(tmp_path, monkeypatch):
    _, stack, _ = p15_case(tmp_path, monkeypatch)
    recorder = stack.extra['recorder']
    scenario = runner.load_scenario('P15')
    roles = resolve_roles(scenario.roles(stack, Seeded({}), 'A'), recorder.records, agent_principal='agent-id')
    expected = {'source-content.unchanged': scenario.expected('A')['source-content.unchanged']}
    projection = {'source-content.unchanged': load_projections('P15', 'A')['source-content.unchanged']}
    observation = [Observation('source-content.unchanged', None, {'projection': 'extract/v1'})]
    def score(bindings):
        return runner.score_assertions(['source-content.unchanged'], observation, expected,
            records=recorder.records, roles=bindings, scenario_id='P15', frozen=projection)[0]
    assert score(roles)['status'] == 'pass'
    swapped = dict(roles)
    service, layer = 'seeded:service:shelters', 'seeded:layer:shelters'
    swapped[service], swapped[layer] = swapped[layer], swapped[service]
    bad = score(swapped)
    assert bad['status'] == 'fail' and 'does not address' in bad['reason']


@pytest.mark.parametrize('sid', ('P14', 'P15', 'P16', 'P17', 'P20'))
def test_seed_uses_reviewed_inventory_for_async_import(tmp_path, monkeypatch, sid):
    stack, candidate, _ = stack_fixture(tmp_path, monkeypatch)
    candidate.async_import = True
    scenario = runner.load_scenario(sid)
    seeded = scenario.seed(stack, 'A')
    assert scenario.seed(stack, 'A') == seeded
    assert len(candidate.jobs) == 3
    assert not any(path.endswith('/tables') for _, _, _, path in candidate.calls)


@pytest.mark.parametrize('wrong', ('async-missing', 'async-extra', 'async-failed'))
def test_async_import_rejects_wrong_state_without_retry(tmp_path, monkeypatch, wrong):
    stack, candidate, _ = stack_fixture(tmp_path, monkeypatch)
    candidate.async_import, candidate.wrong = True, wrong
    scenario = runner.load_scenario('P15')
    with pytest.raises(ValueError, match='fixture import'):
        scenario.seed(stack, 'A')
    before = len(candidate.calls)
    with pytest.raises(ValueError, match='incomplete seed'):
        scenario.seed(stack, 'A')
    assert len(candidate.calls) == before


@pytest.mark.parametrize('sid', ('P14', 'P15', 'P17', 'P20'))
@pytest.mark.parametrize('wrong', ('seed-owner', 'seed-item', 'seed-hash'))
def test_seed_rejects_a_misbound_saved_map_without_retry(tmp_path, monkeypatch, sid, wrong):
    stack, candidate, _ = stack_fixture(tmp_path, monkeypatch)
    candidate.wrong = wrong
    scenario = runner.load_scenario(sid)
    with pytest.raises(ValueError, match='saved map does not match'):
        scenario.seed(stack, 'A')
    before = len(candidate.calls)
    with pytest.raises(ValueError, match='incomplete seed'):
        scenario.seed(stack, 'A')
    assert len(candidate.calls) == before
