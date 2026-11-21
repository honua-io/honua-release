import agent_safety_gate as walk
import agent_safety_inventory as inventory
import agent_safety_routes as routes

INVENTORY = """# Every governed route that changes state.
ANY /metrics
ANY geospatial.v1.SpecService/{unimplementedMethod:grpcunimplemented}
DELETE /api/v{version:apiVersion}/admin/alerts/rules/{ruleId:long}
DELETE /api/v{version:apiVersion}/admin/connections/{id:guid}
PATCH /sta/v1.1/{**resourcePath}
POST /api/v{version:apiVersion}/admin/agent-authority/
POST /api/v{version:apiVersion}/console/content/{id}/share/link
"""


def parsed():
    return {(r['method'], r['template']): r for r in inventory.parse_inventory(INVENTORY)}


def test_inventory_lines_become_callable_routes():
    keys = set(parsed())
    assert keys == {('POST', '/metrics'), ('POST', '/geospatial.v1.SpecService/{unimplementedMethod}'),
                    ('DELETE', '/api/v1/admin/alerts/rules/{ruleId}'), ('DELETE', '/api/v1/admin/connections/{id}'),
                    ('PATCH', '/sta/v1.1/{resourcePath}'), ('POST', '/api/v1/admin/agent-authority'),
                    ('POST', '/api/v1/console/content/{id}/share/link')}


def test_route_constraints_choose_a_value_the_route_matches():
    by_key = parsed()
    for key in by_key:
        by_key[key].update(rule=None, fixture={}, unmappedReason=None)
    rule = routes.concrete(by_key[('DELETE', '/api/v1/admin/alerts/rules/{ruleId}')], {})
    guid = routes.concrete(by_key[('DELETE', '/api/v1/admin/connections/{id}')], {})
    rest = routes.concrete(by_key[('PATCH', '/sta/v1.1/{resourcePath}')], {})
    assert rule == '/api/v1/admin/alerts/rules/1'
    assert len(guid.rsplit('/', 1)[1]) == 36
    assert rest == '/sta/v1.1/agent-safety-gate'


def test_only_routes_the_other_lists_missed_are_added():
    known = {('POST', '/metrics'), ('POST', '/api/v1/admin/agent-authority')}
    added = inventory.inventory_routes(inventory.parse_inventory(INVENTORY + 'ANY /metrics\n'), known)
    assert len(added) == 5 and not {(r['method'], r['template']) for r in added} & known


def test_an_inventory_route_without_an_endpoint_needs_a_declared_reason():
    route = {**parsed()[('POST', '/metrics')], 'rule': None, 'fixture': {}, 'unmappedReason': None}
    for status in (404, 405, 415):
        assert routes.judge_route(route, status, b'') == 'unchecked'
        assert routes.judge_route({**route, 'unmappedReason': 'capability off'}, status, b'') == 'unmapped'
    assert routes.judge_route(route, 403, b'') == 'refused'
    assert routes.judge_route(route, 404, b'{"title":"Not found"}') == 'violation'
    assert routes.judge_route(route, 200, b'') == 'violation'


def test_an_uncalled_route_or_a_missing_inventory_leaves_the_row_unchecked(tmp_path):
    listed = inventory.parse_inventory(INVENTORY)
    walked = [{'method': r['method'], 'route': r['template'], 'verdict': 'refused', 'status': 403} for r in listed]
    assert inventory.uncovered(listed, walked) == []
    missing = inventory.load_inventory(tmp_path / 'agent-change-routes.txt')
    (tmp_path / 'empty.txt').write_text('# header only\n')
    assert missing is None and inventory.load_inventory(tmp_path / 'empty.txt') is None
    operations = [{'operationId': 'op', 'verdict': 'proposal'}]
    for gaps, finding in ((inventory.uncovered(listed, walked[1:]), 'POST /metrics -> not called'),
                          (inventory.uncovered(missing, walked), f'FILE {inventory.INVENTORY} -> missing at the candidate revision')):
        assertions, findings = walk.decide(operations, walked[1:] + gaps, {'proposals': [], 'refusals': []}, [])
        assert assertions['allMutatingRoutesChecked'] is False and assertions['proposalOnly'] is True
        assert findings['unchecked'] == [finding]
