import json

import pytest

import agent_safety_gate as walk
import agent_safety_routes as routes
import release_promise as promise
import rc_gate_window as gate

SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
LOCK = 'sha256:' + 'c' * 64
RULES = """# header
POST /api/v{version:apiVersion}/admin/connections/test | Tests a connection and changes nothing; the attempt is audited.
DELETE /api/v{version:apiVersion}/admin/roles/{id:guid} | Its own agent rule decides (AgentApprovalFilter.AccessGrants).
POST /api/v{version:apiVersion}/console/content/ | Its own agent rule decides (AgentApprovalFilter.ContentVisibility).
"""


def route(method='POST', template='/api/v1/admin/folders', anonymous=False, rule=None, unmapped=None, fixture=None):
    return {'method': method, 'template': template, 'parameters': {}, 'contentType': 'application/json',
            'anonymous': anonymous, 'rule': rule, 'unmappedReason': unmapped, 'fixture': fixture or {}}


OWN = 'Its own agent rule decides (AgentApprovalFilter.AccessGrants).'
TESTS = 'Tests a connection and changes nothing; the attempt is audited.'


def test_rule_templates_normalize_to_the_openapi_form():
    rules = routes.parse_rules(RULES)
    assert set(rules) == {('POST', '/api/v1/admin/connections/test'), ('DELETE', '/api/v1/admin/roles/{id}'),
                          ('POST', '/api/v1/console/content')}


@pytest.mark.parametrize('case, status, raw, verdict', [
    (route(), 403, b'{}', 'refused'),
    (route(), 200, b'{}', 'violation'),
    (route(), 400, b'{"detail":"bad"}', 'violation'),
    (route(), 404, b'', 'unchecked'),
    (route(unmapped='alerts are off in the harness'), 404, b'', 'unmapped'),
    (route(unmapped='alerts are off in the harness'), 404, b'{"message":"not found"}', 'violation'),
    (route(), 404, b'{"message":"Folder not found"}', 'violation'),
    (route(template='/api/v1/admin/auth/session', anonymous=True), 400, b'{}', 'anonymous'),
    (route(template='/api/v1/admin/auth/session', anonymous=True), 200, b'{}', 'violation'),
    (route(template='/api/v1/admin/backup', anonymous=True), 400, b'{}', 'violation'),
    (route(rule=OWN), 403, b'{}', 'own-rule'),
    (route(rule=OWN), 409, b'{"policyRef":"operator.agent.access-grants"}', 'own-rule'),
    (route(rule=OWN), 400, b'{"detail":"Invalid request body"}', 'unchecked'),
    (route(rule=OWN), 404, b'', 'unchecked'),
    (route(rule=OWN), 204, b'', 'violation'),
    (route(rule=OWN), 202, b'{"proposalId":"p1"}', 'own-rule'),
    (route(rule=OWN), 500, b'', 'violation'),
    (route(rule=TESTS), 200, b'{}', 'changes-nothing'),
    (route(rule='Changes nothing.'), 400, b'{}', 'changes-nothing'),
    (route(rule='Returns a changed spec and saves nothing.'), 400, b'{}', 'changes-nothing'),
    (route(rule='Its own agent rule decides; a dry run changes nothing.'), 200, b'{}', 'violation'),
])
def test_a_direct_route_call_by_an_agent_is_judged(case, status, raw, verdict):
    assert routes.judge_route(case, status, raw) == verdict


def test_route_fixtures_supply_seeded_path_values_and_bodies():
    typed = route('PUT', '/api/v1/admin/services/{serviceName}/layers/{storageLayerId}',
                  fixture={'path': {'storageLayerId': '${layerId}'}, 'body': {'title': '${serviceName}'}})
    world = {'serviceName': 'svc', 'layerId': 7}
    assert routes.concrete(typed, world) == '/api/v1/admin/services/svc/layers/7'
    assert json.loads(routes.request_body(typed, world)) == {'title': 'svc'}


@pytest.mark.parametrize('status, data, verdict', [
    (200, {'status': 'RequiresApproval', 'proposalId': 'p1', 'policyDecision': 'RequireApproval'}, 'proposal'),
    (200, {'status': 'RequiresApproval', 'policyDecision': 'RequireApproval'}, 'gated-without-proposal'),
    (200, {'status': 'Failed', 'reason': 'Required parameter missing'}, 'unreached'),
    (200, {'status': 'Succeeded'}, 'executed'),
    (202, {'status': 'Queued'}, 'executed'),
    (200, {'status': 'Failed', 'policyDecision': 'RequireApproval', 'reason': 'no durable replay mapper'},
     'gated-without-proposal'),
    (403, {}, 'refused'),
])
def test_an_operation_submission_is_judged(status, data, verdict):
    assert walk.judge_submission(status, data) == verdict


def test_operation_parameters_combine_generated_values_with_fixtures():
    operation = {'operationId': 'folder.update', 'inputSchema': [
        {'name': 'If-Match', 'required': True, 'schema': {'type': 'Text'}},
        {'name': 'id', 'required': True, 'schema': {'type': 'Text'}},
        {'name': 'expectedRevisionId', 'required': True, 'schema': {'type': 'WholeNumber'}},
        {'name': 'scope', 'required': True, 'schema': {'type': 'Text', 'enumValues': ['layer', 'service']}},
        {'name': 'title', 'required': False, 'schema': {'type': 'Text'}}]}
    world = {'metadataEtag': '"e1"', 'folderId': 'f-1'}
    parameters = walk.operation_parameters(operation, {'folder.update': {'id': '${folderId}'}}, world)
    assert parameters == {'If-Match': '"e1"', 'id': 'f-1', 'expectedRevisionId': '1', 'scope': 'layer'}


def test_a_fixture_naming_unseeded_state_is_refused():
    with pytest.raises(routes.GateUnavailable, match='serviceName'):
        routes.placeholder_values({}, '${serviceName}')


def test_path_values_follow_the_parameter_schema():
    typed = route(template='/api/v1/admin/layers/{layer}/x/{id}')
    typed['parameters'] = {'layer': {'schema': {'type': 'integer'}}, 'id': {'schema': {'type': 'string', 'format': 'uuid'}}}
    path = routes.concrete(typed, {})
    assert path.startswith('/api/v1/admin/layers/1/x/') and len(path.rsplit('/', 1)[1]) == 36


def test_openapi_routes_keep_only_mutating_methods_and_mark_anonymous_ones():
    spec = {'paths': {'/folders': {'get': {}, 'post': {'requestBody': {'content': {'multipart/form-data': {}}}}},
                      '/auth/session': {'post': {'security': []}}}}
    walked = routes.openapi_routes(spec)
    assert [(r['method'], r['template'], r['contentType'], r['anonymous']) for r in walked] == [
        ('POST', '/api/v1/admin/auth/session', 'application/json', True),
        ('POST', '/api/v1/admin/folders', 'multipart/form-data', False)]


def results(operations=('proposal',), routes=('refused',)):
    return ([{'operationId': f'op{i}', 'verdict': v} for i, v in enumerate(operations)],
            [{'method': 'POST', 'route': f'/r{i}', 'status': 403, 'verdict': v} for i, v in enumerate(routes)])


CLEAN = {'proposals': [], 'refusals': []}


def test_every_assertion_holds_only_on_a_clean_walk():
    assertions, findings = walk.decide(*results(), CLEAN, [])
    assert all(assertions.values()) and not any(findings.values())


@pytest.mark.parametrize('operations, routes, identity, changed, failed', [
    (('proposal', 'unreached'), ('refused',), CLEAN, [], 'allMutatingRoutesChecked'),
    (('proposal', 'executed'), ('refused',), CLEAN, [], 'proposalOnly'),
    (('proposal', 'gated-without-proposal'), ('refused',), CLEAN, [], 'proposalOnly'),
    (('proposal',), ('violation',), CLEAN, [], 'proposalOnly'),
    (('proposal',), ('refused',), CLEAN, ['/roles'], 'proposalOnly'),
    (('proposal',), ('refused',), {'proposals': ['op0'], 'refusals': []}, [], 'agentIdentityAudited'),
    ((), ('refused',), CLEAN, [], 'allMutatingRoutesChecked'),
])
def test_a_gap_fails_its_assertion(operations, routes, identity, changed, failed):
    assertions, _ = walk.decide(*results(operations, routes), identity, changed)
    assert assertions[failed] is False


def contract_row():
    return next(row for row in promise.load_contract() if row['id'] == 'security-agent-default-deny')


def test_a_passing_receipt_satisfies_the_promise_contract():
    candidate = gate.Candidate(SHA, DIGEST, LOCK)
    assertions, findings = walk.decide(*results(), CLEAN, [])
    document = walk.receipt(candidate, assertions, findings)
    assert document['status'] == 'PASS'
    assert promise._common_receipt_valid(document, contract_row())
    assert gate.matches_candidate(document, candidate)


def test_a_failing_receipt_does_not_satisfy_the_contract():
    assertions, findings = walk.decide(*results(('unreached',)), CLEAN, [])
    document = walk.receipt(gate.Candidate(SHA, DIGEST, LOCK), assertions, findings)
    assert document['status'] == 'FAIL' and document['findings'] == ['unchecked: op0']
    assert not promise._common_receipt_valid(document, contract_row())


def test_redacted_credential_path_refusals_match_audit_trail():
    walked = [{
        'method': 'POST',
        'route': '/api/v1/console/share/embed/{token}/redeem',
        'path': '/api/v1/console/share/embed/agent-safety-gate/redeem',
        'status': 403,
        'verdict': 'refused',
    }]
    audited = {('POST', '/api/v1/console/share/embed/[REDACTED]/redeem')}
    assert walk.refusals_unaudited(walked, audited) == []


def test_audit_entries_read_every_page_and_tolerate_plain_details():
    pages = iter([{'items': [{'actorAgent': 'me', 'details': '{"method":"POST"}', 'resourceId': '/a?x=1'},
                             {'actorAgent': 'other', 'details': '{}', 'resourceId': '/b'}], 'nextCursor': 'c 1'},
                  {'items': [{'actorAgent': 'me', 'details': 'not json', 'action': 'admin.delete', 'resourceId': '/c'}]}])
    calls = []

    class Admin:
        def json(self, method, path):
            calls.append(path)
            return next(pages)
    assert walk.audit_entries(Admin(), 'me') == {('POST', '/a'), ('DELETE', '/c')}
    assert calls[1].endswith('&cursor=c%201')


def test_the_committed_fixtures_name_catalog_operations_only():
    fixtures = json.loads(walk.FIXTURES.read_text())
    assert set(fixtures) <= {'schema', 'operations', 'routes', 'unmapped', 'probes'}
    assert all(isinstance(params, dict) and all(isinstance(v, str) for v in params.values())
               for params in fixtures['operations'].values())


def test_a_probe_reuses_the_walked_route_with_its_own_request():
    walked = [route('PUT', '/api/v1/console/content/{id}', rule=OWN)]
    probes = routes.probe_routes(walked, {'probes': [{'route': 'PUT /api/v1/console/content/{id}',
                                                      'path': {'id': '${item}'}, 'body': {'title': 't'}}]})
    assert probes[0]['rule'] == OWN and routes.concrete(probes[0], {'item': 'c1'}) == '/api/v1/console/content/c1'
    with pytest.raises(routes.GateUnavailable, match='not a walked route'):
        routes.probe_routes(walked, {'probes': [{'route': 'DELETE /nowhere'}]})


def test_own_rule_refusals_must_be_audited_too():
    walked = [{'method': 'PUT', 'route': '/r', 'path': '/r/1', 'status': 403, 'verdict': 'own-rule'},
              {'method': 'POST', 'route': '/s', 'path': '/s', 'status': 400, 'verdict': 'changes-nothing'}]
    assert walk.refusals_unaudited(walked, set()) == ['PUT /r']
    assert walk.refusals_unaudited(walked, {('PUT', '/r/1')}) == []
