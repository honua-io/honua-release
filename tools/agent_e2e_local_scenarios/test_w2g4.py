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

    def http(recorder, principal, method, path, body, headers):
        if path.startswith('/keys/'):
            value = {'data': {'apiKey': {'id': path.split('/')[-1] + '-id'}}}
            return 201, json.dumps(value).encode()
        if path == '/fixture-map':
            return 201, json.dumps({'data': {'itemId': ITEM, 'versionId': VERSION}}).encode()
        if path == '/api/v1/agent/target':
            identity = {'actorId': principal + '-id', 'tenantId': 'tenant-b' if principal == 'owner' else 'tenant-a',
                        'isHumanSession': principal == 'owner',
                        'authenticationKind': 'operator-bearer' if principal == 'owner' else 'api-key'}
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
        if path == f'/api/v1/studio/content-items/{ITEM}/versions/{VERSION}':
            code = {'operator': 200, 'owner': 200, 'agent': 404, 'anonymous': 401}[principal]
            if state['wrong'] == 'anonymous-public' and principal == 'anonymous':
                code = 200
            if state['wrong'] == 'tenant-open' and principal == 'agent':
                code = 200
            value = {'data': {'itemId': ITEM, 'versionId': VERSION}} if code == 200 else {'error': 'denied'}
            if state['wrong'] == 'wrong-map' and principal in ('operator', 'owner'):
                value['data']['versionId'] = '33333333-3333-3333-3333-333333333333'
            return code, json.dumps(value).encode()
        raise AssertionError((principal, method, path))

    recorder = ControllerRecorder(base_url='http://127.0.0.1:8147', workdir=tmp_path,
        candidate_digest='sha256:' + '1' * 64, attempt_id='case', principals=principals, http_factory=http)
    recorder.json(sid.lower() + '-map-saved', 'POST', '/fixture-map')
    if sid in ('P15', 'P20'):
        recorder.json('runner-principal-operator', 'POST', '/keys/operator')
    if sid in ('P15', 'P18'):
        recorder.json('runner-principal-owner', 'POST', '/keys/owner')
        recorder.json('runner-custody-mint-agent', 'POST', '/keys/agent')
    if sid == 'P15':
        from agent_e2e_p10_observer import source_path
        recorder.json('p15-source-before', 'GET', source_path({'service': 'town', 'layerId': 3}))
    recorder.set_phase('observe')
    stack = Stack('http://127.0.0.1:8147', 'controller', None, 'case', 'compose', str(tmp_path),
                  'sha256:' + '1' * 64, {'recorder': recorder})
    seeded = Seeded({'itemId': ITEM, 'versionId': VERSION, 'shelters': {'service': 'town', 'layerId': 3}})
    scenario = runner.load_scenario(sid)
    observed = scenario.observe(stack, seeded, variant)
    roles = resolve_roles(scenario.roles(stack, seeded, variant), recorder.records, agent_principal='agent-id')
    scored = runner.score_assertions(PACK[sid], observed, scenario.expected(variant), records=recorder.records, roles=roles)
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


@pytest.mark.parametrize('sid', ('P15', 'P18', 'P20'))
def test_positive_control_must_read_the_expected_map_version(tmp_path, monkeypatch, sid):
    with pytest.raises(ValueError, match='exact saved map version'):
        recorded_case(tmp_path, monkeypatch, sid, 'A', 'wrong-map')


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
