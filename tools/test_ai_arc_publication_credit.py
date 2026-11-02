"""Rehearsal of record r3 (2026-11-02): P10 credits a publication made with honua_admin_layer_publish too (H1);
the auth preflight reaches the receipt (H3); an agent-restricted, unobservable service counts against the arm."""
import hashlib
import json

import pytest

from agent_e2e_local_api import RoleBinding
from agent_e2e_local_projections import admin_layer_publication, resolve_roles
from agent_e2e_local_recorder import Record

TABLE, SCHEMA, CONN = 'imported_arc10_abc_shelters', 'honua_data', '7882ed79-1e53-43d6-b2c6-bc5b35be8e55'


def _record(step, phase, body, path='/api/v1/admin/services/'):
    raw = json.dumps(body).encode()
    return Record(step, phase, 'http', 'controller', None, 'GET', path, 200, raw, 'sha256:' + hashlib.sha256(raw).hexdigest())


def _admin_publish(service='kestrel', table=TABLE, dry_run=None, status='pass', completed=True, validation=False,
                   operation='admin.layer.publish', is_error=False):
    data = ({'isValid': True, 'status': 'valid', 'serviceName': service, 'table': table} if validation else
            {'layerId': 1, 'serviceName': service, 'table': table, 'schema': SCHEMA, 'enabled': True})
    args = {'connectionId': CONN, 'schema': SCHEMA, 'table': table, 'layerName': 'Kestrel Shelters',
            'serviceName': service}
    if dry_run is not None:
        args['dryRun'] = dry_run
    payload = {'status': 'Completed' if completed else 'Failed', 'operationId': operation,
               'details': {'response': json.dumps({'success': True, 'data': data})}}
    return {'tool': 'honua_admin_layer_publish', 'kind': 'tool_call', 'status': status, 'arguments': args,
            'json': {'jsonrpc': '2.0', 'id': '1', 'result': {'isError': is_error, 'structuredContent': payload}}}


def _publish_service(service='kestrel-svc', table=TABLE):
    return {'tool': 'honua_publish_service', 'kind': 'tool_call', 'status': 'pass',
            'arguments': {'connectionId': CONN, 'schema': SCHEMA, 'table': table, 'serviceName': service},
            'json': {'jsonrpc': '2.0', 'id': '1', 'result': {'isError': False, 'structuredContent': {
                'status': 'Completed', 'operationId': 'service.publish', 'serviceName': service, 'layerId': '1'}}}}


def _resolve(results, live=('geoprocessing', 'kestrel', 'kestrel-svc', 'other')):
    from agent_e2e_local_expectations import load_roles
    bindings = [b for b in load_roles('P10', 'arc') if b.role in (
        'seeded:table:shelters', 'seeded:schema:shelters', 'seeded:connection:shelters', 'created:service:shelters')]
    imp = {'success': True, 'physicalTableName': TABLE, 'schema': SCHEMA}
    records = {
        'import-shelters': _record('import-shelters', 'seed', imp, '/api/v1/admin/import/upload'),
        'before-tables-shelters': _record('before-tables-shelters', 'seed', {'tables': []}, '/api/v1/admin/connections/x/tables'),
        'arc-p10-connection-create': _record('arc-p10-connection-create', 'seed', {'data': {'connectionId': CONN}},
                                             '/api/v1/admin/connections/'),
        'arc-p10-services-before': _record('arc-p10-services-before', 'seed',
                                           {'success': True, 'data': [{'serviceName': 'geoprocessing'}]}),
        'arc-p10-services-after': _record('arc-p10-services-after', 'observe',
                                          {'success': True, 'data': [{'serviceName': s} for s in live]})}
    return resolve_roles(bindings, records, agent_principal='agent', action_results=results)


def test_the_arc_p10_binding_reviews_exactly_the_admin_layer_publish_alternative():
    from agent_e2e_local_expectations import load_roles
    created = next(b for b in load_roles('P10', 'arc') if b.role == 'created:service:shelters')
    assert created.created_by['tool'] == 'honua_publish_service'
    assert created.created_by['alsoTool']['tool'] == 'honua_admin_layer_publish'
    assert [w['argument'] for w in created.created_by['whereAll']] == ['/table', '/schema', '/connectionId']


def test_an_admin_layer_publish_of_the_right_table_is_credited():
    assert _resolve([_admin_publish()])['created:service:shelters'] == 'kestrel'


def test_publish_service_is_still_credited():
    assert _resolve([_publish_service()])['created:service:shelters'] == 'kestrel-svc'


@pytest.mark.parametrize('result', [
    _admin_publish(table='other_table'),                    # the wrong table: the whereAll binding refuses it
    _admin_publish(dry_run=True),                           # a dry run never creates the service
    _admin_publish(validation=True),                        # the validation reply of a dry run
    _admin_publish(status='fail'),                          # not an accepted call
    _admin_publish(completed=False),                        # the operation did not complete
    _admin_publish(operation='service.publish'),            # another operation's reply
    _admin_publish(is_error=True)])
def test_an_admin_layer_publish_that_is_not_a_real_publication_of_the_table_is_not_credited(result):
    with pytest.raises(ValueError, match='matched 0'):
        _resolve([result])


def test_admin_layer_publication_reads_only_completed_real_publications():
    assert admin_layer_publication(_admin_publish()) == 'kestrel'
    assert admin_layer_publication(_admin_publish(dry_run=False)) == 'kestrel'
    assert admin_layer_publication(_admin_publish(dry_run='true')) is None
    assert admin_layer_publication(_admin_publish(service='bad name')) is None


def test_an_unreviewed_alternative_tool_is_refused():
    import copy
    from agent_e2e_local_expectations import load_roles
    bindings = load_roles('P10', 'arc')
    created = next(b for b in bindings if b.role == 'created:service:shelters')
    spec = copy.deepcopy(created.created_by)
    spec['alsoTool'] = dict(spec['alsoTool'], tool='honua_ingest_dataset')
    forged = RoleBinding(created.role, created.step, items=created.items, id=created.id, before=created.before,
                         created_by=spec)
    with pytest.raises(ValueError, match='unreviewed created_by alternative'):
        resolve_roles([forged], {created.step: _record(created.step, 'observe', {'data': []}),
                                 created.before: _record(created.before, 'seed', {'data': []})},
                      agent_principal='agent', action_results=[])


def test_the_auth_preflight_reaches_the_receipt_provenance():
    import ai_arc_runner as R
    import inspect
    src = inspect.getsource(R)
    assert "'mcpMethods', 'transcriptSha256', 'budgets',\n" in src and "'authPreflight')}" in src


def test_an_agent_restricted_unobservable_service_counts_against_the_arm():
    # H2b (deferred, disclosed): r3 A2 role-restricted the service it published, so the harness could not read it.
    # The services stage is a model failure, not a suspected harness fault, so it can never be voided.
    import ai_arc_campaign as C
    receipt = {'status': 'blocked', 'stages': [
        {'stage': 'setup', 'outcome': 'pass'}, {'stage': 'ops', 'outcome': 'pass'},
        {'stage': 'services', 'outcome': 'fail',
         'cause': 'session model: prior P10 did not prove a passing agent publication and required fault',
         'assertions': [{'assertion': 'P10:filter.exact', 'status': 'fail',
                         'reason': 'observation failed (ProjectionError): pointer target absent: serviceName'}]},
        {'stage': 'studio', 'outcome': 'blocked'}]}
    assert C.suspected_harness_fault(receipt) is False


def test_a_long_lived_token_session_records_its_preflight_in_provenance(tmp_path, monkeypatch):
    from test_agent_cli_sessions import TOOLS, fake_cli
    from agent_cli_sessions import CliModelSession
    monkeypatch.setenv('CLAUDE_CODE_OAUTH_TOKEN', 'long-lived-test-token')
    cli = CliModelSession('claude-cli', 'claude-opus-5-5', binary=fake_cli(tmp_path, 'claude-cli'), auth_root=tmp_path)
    out = cli.run(prompt='Customer goal only', tool_view=TOOLS,
                  execute=lambda *_: {'status': 'ok', 'result': {'content': []}}, log_dir=tmp_path / 'log')
    assert out['provenance'][0]['authPreflight'] == {'longLivedToken': True, 'expiryUnknown': False}
    assert 'long-lived-test-token' not in json.dumps(out['provenance'])


def _fault():
    from agent_e2e_local_faults import FaultInjector
    from agent_e2e_local_scenarios.arc_p10 import ArcP10
    return FaultInjector(ArcP10('http_503_once').fault_plan('A'))


DONE = {'status': 'pass', 'result': {'isError': False, 'structuredContent': {'status': 'Completed'}}}


def _call(tool, **arguments):
    return {'kind': 'tool_call', 'tool': tool, 'arguments': arguments}


@pytest.mark.parametrize('tool', ['honua_publish_service', 'honua_admin_layer_publish'])
def test_either_publication_tool_arms_the_p10_query_503(tool):
    fault = _fault()
    query = _call('honua_query_features', serviceId='kestrel', layerId=1)
    assert fault.intercept('tool_call', query) is None                 # not armed before a publication
    if tool == 'honua_admin_layer_publish':
        real = _admin_publish(dry_run=False)
        fault.observe('tool_call', _call(tool, **real['arguments']), {'status': 'ok', 'result': real['json']['result']})
    else:
        fault.observe('tool_call', _call(tool, table=TABLE, dryRun=False), DONE)
    event = fault.intercept('tool_call', query)
    assert event is not None and event['id'] == 'arc-p10-query-503' and event['tool'] == 'honua_query_features'
    assert fault.intercept('tool_call', query) is None                 # once


def test_a_dry_run_failed_or_incomplete_publication_never_arms_the_503():
    fault = _fault()
    query = _call('honua_query_features', serviceId='kestrel', layerId=1)
    fault.observe('tool_call', _call('honua_admin_layer_publish', table=TABLE, dryRun=True), DONE)
    fault.observe('tool_call', _call('honua_admin_layer_publish', table=TABLE, dryRun='true'), DONE)
    fault.observe('tool_call', _call('honua_admin_layer_publish', table=TABLE), dict(DONE, status='fail'))
    fault.observe('tool_call', _call('honua_admin_layer_publish', table=TABLE),
                  {'status': 'pass', 'result': {'isError': False, 'structuredContent': {'status': 'Failed'}}})
    fault.observe('tool_call', _call('honua_admin_layer_publish', table=TABLE),
                  {'status': 'pass', 'result': {'isError': True, 'structuredContent': {'status': 'Completed'}}})
    fault.observe('tool_call', _call('honua_list_layers'), DONE)
    # Sol h13b: a completed validation-only admin reply (isValid, no layerId) never consumes the fault.
    validation = _admin_publish(validation=True)
    fault.observe('tool_call', _call('honua_admin_layer_publish', **validation['arguments']),
                  {'status': 'ok', 'result': validation['json']['result']})
    # A completed admin reply without a real publication body never arms it either.
    fault.observe('tool_call', _call('honua_admin_layer_publish', table=TABLE), DONE)
    assert fault.intercept('tool_call', query) is None


def test_only_the_reviewed_publication_trigger_is_accepted():
    from agent_e2e_local_faults import PUBLISH_TRIGGER, validate_fault_plan
    base = {'kind': 'http_503_once', 'id': 'x', 'match': {'tool': 'honua_query_features'}}
    assert validate_fault_plan(dict(base, after=PUBLISH_TRIGGER))['after'] == PUBLISH_TRIGGER
    for bad in ({'tools': ['honua_publish_service', 'honua_ingest_dataset'], 'skipDryRun': True},
                {'tools': ['honua_publish_service', 'honua_admin_layer_publish']},
                {'tools': ['honua_publish_service', 'honua_admin_layer_publish'], 'skipDryRun': False}):
        with pytest.raises(ValueError):
            validate_fault_plan(dict(base, after=bad))
    with pytest.raises(ValueError):                                   # never for the lost-response fault
        validate_fault_plan({'kind': 'response_lost_after_commit', 'id': 'x', 'after': PUBLISH_TRIGGER,
                             'match': {'tool': 'honua_publish_service'},
                             'commit': {'pointer': '/result/structuredContent/serviceName',
                                        'confirm': {'path': '/api/v1/admin/services/{id}/settings'}}})
