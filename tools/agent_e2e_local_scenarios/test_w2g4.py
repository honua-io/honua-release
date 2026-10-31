"""Governance projections use controller response bytes and real wrong-state controls."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import agent_e2e_local_runner as runner
from agent_e2e_local_api import Seeded, Stack
from agent_e2e_local_projections import resolve_roles, substitute_roles
from agent_e2e_local_recorder import ControllerRecorder

ROOT = Path(__file__).resolve().parents[2]
IDS = ('P14', 'P15', 'P16', 'P17', 'P18', 'P20')
VARIANTS = ('A', 'B')
PACK = {row['id']: row['assertions'] for row in json.loads(
    (ROOT / 'certification/agent-e2e/pack.v2.AB-only.json').read_text())['scenarios'] if row['id'] in IDS}
ITEM = '11111111-1111-1111-1111-111111111111'
VERSION = '22222222-2222-2222-2222-222222222222'
FOREIGN_ITEM = '33333333-3333-3333-3333-333333333333'
FOREIGN_VERSION = '44444444-4444-4444-4444-444444444444'


def feature_response(variant):
    features = json.loads((ROOT / f'certification/agent-e2e/corpus/town-{variant}/shelters.3857.geojson').read_text())['features']
    return {'geometryType': 'esriGeometryPoint', 'exceededTransferLimit': False,
            'spatialReference': {'wkid': 3857}, 'objectIdFieldName': 'objectid',
            'fields': [{'name': 'objectid', 'type': 'esriFieldTypeOID'}] + [
                {'name': name, 'type': 'esriFieldTypeString'} for name in ('capacity', 'is_open', 'name', 'source_id')],
            'features': [{'attributes': {'objectid': index + 1, **feature['properties']},
                          'geometry': {'x': feature['geometry']['coordinates'][0], 'y': feature['geometry']['coordinates'][1]}}
                         for index, feature in enumerate(features)]}


@pytest.fixture(params=IDS)
def scenario(request):
    return runner.load_scenario(request.param)


def test_inventory_and_refusal_are_safe(scenario, tmp_path):
    assert scenario.supported_variants == ()
    stack = Stack('http://127.0.0.1:8147', 'controller', None, 'case', 'compose', str(tmp_path), 'sha256:' + '1' * 64)
    for variant in VARIANTS:
        assert list(scenario.expected(variant)) == PACK[scenario.id]
        for _ in range(2):
            with pytest.raises(NotImplementedError, match=scenario.id):
                scenario.seed(stack, variant)
        assert scenario.fault_plan(variant) is None
    assert scenario.needs_approver() == (scenario.id in ('P14', 'P17'))
    assert scenario.cleanup(stack, Seeded({})) is None
    with pytest.raises(ValueError, match='cleanup ownership'):
        scenario.cleanup(stack, Seeded({'resourceId': 'not-owned'}))
    assert list(tmp_path.iterdir()) == []


def recorded_case(tmp_path, monkeypatch, sid, variant, wrong=None):
    """Record representative API responses through the real core recorder."""
    state = {'wrong': wrong}
    for principal in ('controller', 'operator', 'owner', 'agent'):
        monkeypatch.setenv('G4_' + principal.upper(), 'test-secret-' + principal)
    principals = {p: ('G4_' + p.upper(), p + '-id') for p in ('controller', 'operator', 'owner', 'agent')}
    principals['anonymous'] = (None, None)
    source = feature_response(variant)
    map_body = json.loads((ROOT / 'certification/terminal-journey/targets/local-docker.json').read_text())['execution']['mapBody']

    def http(recorder, principal, method, path, body, headers):
        if path.startswith('/keys/'):
            value = {'data': {'apiKey': {'id': path.split('/')[-1] + '-id'}}}
            return 201, json.dumps(value).encode()
        if path == '/fixture-map':
            version = '55555555-5555-5555-5555-555555555555' if state['wrong'] == 'unbound-seed' else VERSION
            return 201, json.dumps({'data': {'itemId': ITEM, 'versionId': version}}).encode()
        if path == '/fixture-foreign-map':
            return 201, json.dumps({'data': {'itemId': FOREIGN_ITEM, 'versionId': FOREIGN_VERSION}}).encode()
        if path == '/api/v1/agent/target':
            identity = {'actorId': principal + '-id', 'tenantId': 'tenant-b' if principal == 'owner' else 'tenant-a',
                        'isHumanSession': principal == 'owner',
                        'authenticationKind': 'operator-bearer' if principal == 'owner' else 'api-key'}
            if principal == 'owner':
                identity['studioOwnerId'] = 'owner-id'
            if state['wrong'] == 'same-tenant':
                identity['tenantId'] = 'tenant-a'
            if state['wrong'] == 'nonhuman-owner' and principal == 'owner':
                identity.update(isHumanSession=False, authenticationKind='api-key')
            return 200, json.dumps({'targetId': 'case-target', 'principal': identity}).encode()
        if '/FeatureServer/' in path:
            value = copy.deepcopy(source)
            if state['wrong'] == 'source-change' and recorder.phase == 'observe':
                value['features'][0]['attributes']['capacity'] = 999
            return 200, json.dumps(value).encode()
        if path in (f'/api/v1/studio/content-items/{ITEM}/versions/{VERSION}',
                    f'/api/v1/studio/content-items/{FOREIGN_ITEM}/versions/{FOREIGN_VERSION}'):
            item, version = (FOREIGN_ITEM, FOREIGN_VERSION) if FOREIGN_ITEM in path else (ITEM, VERSION)
            code = {'controller': 200, 'operator': 200, 'owner': 200, 'agent': 404, 'anonymous': 401}[principal]
            if state['wrong'] == 'anonymous-public' and principal == 'anonymous':
                code = 200
            if state['wrong'] == 'tenant-open' and principal == 'agent':
                code = 200
            envelope = {'family': 'map', 'body': copy.deepcopy(map_body)}
            if state['wrong'] == 'mutate-map' and recorder.phase == 'observe' and principal == 'controller':
                envelope['body']['initialView']['zoom'] += 1
            value = {'data': {'itemId': item, 'versionId': version, 'ownerId': 'owner-id', 'envelope': envelope}} if code == 200 else {'error': 'denied'}
            if code == 404 and principal == 'agent':
                value['code'] = ('studio_authorization/cross_tenant_denied' if state['wrong'] != 'generic-404'
                                 else 'studio_authorization/not_found')
            if state['wrong'] == 'wrong-map' and principal in ('operator', 'owner'):
                value['data']['versionId'] = '55555555-5555-5555-5555-555555555555'
            if state['wrong'] == 'wrong-owner' and principal == 'owner':
                value['data']['ownerId'] = 'someone-else'
            return code, json.dumps(value).encode()
        raise AssertionError((principal, method, path))

    recorder = ControllerRecorder(base_url='http://127.0.0.1:8147', workdir=tmp_path,
        candidate_digest='sha256:' + '1' * 64, attempt_id='case', principals=principals, http_factory=http)
    recorder.json(sid.lower() + '-map-saved', 'POST', '/fixture-map')
    if sid == 'P15':
        recorder.json('p15-foreign-saved', 'POST', '/fixture-foreign-map')
    if sid in ('P15', 'P20'):
        recorder.json('runner-principal-operator', 'POST', '/keys/operator')
    if sid in ('P15', 'P18'):
        recorder.json('runner-principal-owner', 'POST', '/keys/owner')
        recorder.json('runner-custody-mint-agent', 'POST', '/keys/agent')
    if sid == 'P15':
        from agent_e2e_p10_observer import source_path
        service = 'other' if wrong == 'other-source' else 'town'
        recorder.json('p15-source-before', 'GET', source_path({'service': service, 'layerId': 3}))
    if sid == 'P18':
        baseline_item = FOREIGN_ITEM if wrong == 'other-baseline' else ITEM
        baseline_version = FOREIGN_VERSION if wrong == 'other-baseline' else VERSION
        recorder.json('p18-map-before', 'GET', f'/api/v1/studio/content-items/{baseline_item}/versions/{baseline_version}')
    recorder.set_phase('observe')
    stack = Stack('http://127.0.0.1:8147', 'controller', None, 'case', 'compose', str(tmp_path),
                  'sha256:' + '1' * 64, {'recorder': recorder})
    seeded = Seeded({'itemId': ITEM, 'versionId': VERSION, 'foreignItemId': FOREIGN_ITEM,
                     'foreignVersionId': FOREIGN_VERSION, 'shelters': {'service': 'town', 'layerId': 3}})
    if wrong == 'same-map':
        seeded.state['foreignItemId'] = ITEM
        seeded.state['foreignVersionId'] = VERSION
    scenario = runner.load_scenario(sid)
    observed = scenario.observe(stack, seeded, variant)
    roles = resolve_roles(scenario.roles(stack, seeded, variant), recorder.records, agent_principal='agent-id')
    expectation = scenario.expected(variant)
    if sid == 'P18' and wrong in ('map-control', 'mutate-map'):
        # A fixture-only control, not the still-unavailable production baseline.
        expectation['mutations.absent'] = {'profile': 'honua.protected-content/v1', 'resources': [
            {'resourceId': 'foreign-map', 'content': {'family': 'map', 'body': map_body}}]}
    scored = runner.score_assertions(PACK[sid], observed, expectation, records=recorder.records, roles=roles)
    return {row['assertion']: row for row in scored}


@pytest.mark.parametrize('variant', VARIANTS)
@pytest.mark.parametrize('sid,assertion,wrong', [
    ('P15', 'publication.private', 'anonymous-public'),
    ('P15', 'tenant.denied', 'tenant-open'),
    ('P15', 'source-content.unchanged', 'source-change'),
    ('P18', 'tenant.denied', 'tenant-open'),
    ('P20', 'publication.private', 'anonymous-public'),
])
def test_recorded_correct_state_passes_and_wrong_state_fails(tmp_path, monkeypatch, sid, assertion, wrong, variant):
    good = recorded_case(tmp_path / 'good', monkeypatch, sid, variant)
    bad = recorded_case(tmp_path / 'bad', monkeypatch, sid, variant, wrong)
    assert good[assertion]['status'] == 'pass', good[assertion]
    assert good[assertion]['evidence']
    assert bad[assertion]['status'] == 'fail', bad[assertion]
    # Missing custody/clarification/mutation evidence never becomes a case pass.
    assert any(row['status'] == 'fail' for row in good.values())


@pytest.mark.parametrize('sid', ('P15', 'P18'))
@pytest.mark.parametrize('wrong', ('same-tenant', 'nonhuman-owner'))
def test_tenant_denial_refuses_unverified_native_custody(tmp_path, monkeypatch, sid, wrong):
    with pytest.raises(ValueError, match='tenant and human-owner custody'):
        recorded_case(tmp_path, monkeypatch, sid, 'A', wrong)


@pytest.mark.parametrize('sid', ('P15', 'P18'))
def test_generic_not_found_does_not_prove_tenant_isolation(tmp_path, monkeypatch, sid):
    with pytest.raises(ValueError, match='cross-tenant boundary'):
        recorded_case(tmp_path, monkeypatch, sid, 'A', 'generic-404')


@pytest.mark.parametrize('sid', ('P15', 'P18', 'P20'))
def test_positive_control_must_read_the_expected_map_version(tmp_path, monkeypatch, sid):
    with pytest.raises(ValueError, match='exact saved map version'):
        recorded_case(tmp_path, monkeypatch, sid, 'A', 'wrong-map')


def test_private_sharing_does_not_use_the_shared_map_as_the_foreign_control(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='distinct foreign map'):
        recorded_case(tmp_path, monkeypatch, 'P15', 'A', 'same-map')


@pytest.mark.parametrize('sid', ('P15', 'P18'))
def test_tenant_positive_control_requires_the_native_map_owner(tmp_path, monkeypatch, sid):
    with pytest.raises(ValueError, match='exact saved map version'):
        recorded_case(tmp_path, monkeypatch, sid, 'A', 'wrong-owner')


def test_mutated_foreign_map_fails_preservation_oracle():
    # Independent, source-controlled map fixture; it does not make P18 runnable.
    body = json.loads((ROOT / 'certification/terminal-journey/targets/local-docker.json').read_text())['execution']['mapBody']
    resource = {'resourceId': 'foreign-map', 'content': body}
    expected = {'profile': 'honua.protected-content/v1', 'resources': [resource]}
    correct = {'profile': expected['profile'], 'before': [copy.deepcopy(resource)], 'after': [copy.deepcopy(resource)]}
    runner.oracle.verify('mutations.absent', correct, expected)
    changed = copy.deepcopy(correct)
    changed['after'][0]['content']['initialView']['zoom'] += 1
    with pytest.raises(runner.oracle.VerificationError):
        runner.oracle.verify('mutations.absent', changed, expected)


def test_p18_recorded_map_projection_detects_a_real_envelope_change(tmp_path, monkeypatch):
    good = recorded_case(tmp_path / 'good-map', monkeypatch, 'P18', 'A', 'map-control')['mutations.absent']
    bad = recorded_case(tmp_path / 'bad-map', monkeypatch, 'P18', 'A', 'mutate-map')['mutations.absent']
    assert good['status'] == 'pass', good
    assert {row['step'] for row in good['evidence']} == {'p18-map-before', 'p18-map-after'}
    assert bad['status'] == 'fail', bad


def test_p18_refuses_a_foreign_baseline_for_another_resource(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='same controller-read version'):
        recorded_case(tmp_path, monkeypatch, 'P18', 'A', 'other-baseline')


def test_p15_refuses_a_baseline_for_another_layer(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='same controller-read layer'):
        recorded_case(tmp_path, monkeypatch, 'P15', 'A', 'other-source')


@pytest.mark.parametrize('sid', ('P15', 'P18', 'P20'))
def test_map_state_must_match_seed_recorded_identity(tmp_path, monkeypatch, sid):
    with pytest.raises(ValueError, match='seed-recorded version'):
        recorded_case(tmp_path, monkeypatch, sid, 'A', 'unbound-seed')


@pytest.mark.parametrize('sid', ('P15', 'P18'))
def test_tenant_identity_reads_are_part_of_the_assertion_evidence(tmp_path, monkeypatch, sid):
    result = recorded_case(tmp_path, monkeypatch, sid, 'A')['tenant.denied']
    assert result['status'] == 'pass'
    steps = {item['step'] for item in result['evidence']}
    assert {sid.lower() + '-owner-identity', sid.lower() + '-agent-identity'} <= steps


@pytest.mark.parametrize('sid,assertion,expected,correct,wrong', [
    ('P14', 'approval.separate',
     {'proposalId': 'p', 'requesterPrincipalId': 'requester', 'approverPrincipalId': 'approver',
      'approvedVersionId': 'v', 'auditId': 'a'},
     {'proposalId': 'p', 'requesterPrincipalId': 'requester', 'approverPrincipalId': 'approver',
      'approvedVersionId': 'v', 'auditId': 'a'},
     {'proposalId': 'p', 'requesterPrincipalId': 'requester', 'approverPrincipalId': 'requester',
      'approvedVersionId': 'v', 'auditId': 'a'}),
    ('P16', 'key.revoked',
     {'requests': [{'principalId': 'key', 'method': 'GET', 'resourceId': 'item', 'status': 200},
                   {'principalId': 'key', 'method': 'GET', 'resourceId': 'item', 'status': 401}]},
     {'requests': [{'principalId': 'key', 'method': 'GET', 'resourceId': 'item', 'status': 200, 'responseSha256': 'a'*64},
                   {'principalId': 'key', 'method': 'GET', 'resourceId': 'item', 'status': 401, 'responseSha256': 'b'*64, 'disclosedResourceIds': []}]},
     {'requests': [{'principalId': 'key', 'method': 'GET', 'resourceId': 'item', 'status': 200, 'responseSha256': 'a'*64},
                   {'principalId': 'key', 'method': 'GET', 'resourceId': 'item', 'status': 200, 'responseSha256': 'b'*64, 'disclosedResourceIds': []}]}),
    ('P17', 'approval.pending',
     {'proposalId': 'p', 'status': 'AwaitingApproval', 'publishedResourceIds': []},
     {'proposalId': 'p', 'status': 'AwaitingApproval', 'publishedResourceIds': []},
     {'proposalId': 'p', 'status': 'AwaitingApproval', 'publishedResourceIds': ['published']}),
])
def test_unsupported_module_oracle_has_a_real_wrong_state(sid, assertion, expected, correct, wrong):
    assert assertion in PACK[sid]
    roles = {'created:proposal': 'p', 'seeded:principal:agent': 'requester',
             'seeded:principal:approver': 'approver', 'seeded:map:version': 'v',
             'created:audit': 'a', 'created:key': 'key', 'seeded:service:item': 'item'}
    assert substitute_roles(runner.load_scenario(sid).expected('A')[assertion], roles) == expected
    runner.oracle.verify(assertion, correct, expected)
    with pytest.raises(runner.oracle.VerificationError):
        runner.oracle.verify(assertion, wrong, expected)
