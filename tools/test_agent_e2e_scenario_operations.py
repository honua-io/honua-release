"""Current Server HTTP contract replay; fixture servers are not live evidence."""
import copy
import json

import pytest

from agent_e2e_pack import PackError
from agent_e2e_scenario_operations import completed_import, discover_tables, observe_approved_publication, publication_request, submit_publication
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server

CONNECTION = '12345678-1234-1234-1234-123456789abc'


def answer(value, status=200):
    return status, {}, json.dumps(value).encode()


def synchronous():
    return {'success': True, 'tableName': 'case_shelters', 'physicalTableName': 'server_chosen_42',
            'schema': 'honua_data', 'featureCount': 5}


def test_synchronous_import_uses_observed_physical_identity(adapter, server):
    server.respond = lambda *_: answer(synchronous())
    observed = adapter.http.json('accepted', 'POST', '/import', {})
    result = completed_import(adapter.http, observed, table='case_shelters', feature_count=5, step='import')
    assert result['table'] == 'server_chosen_42'
    assert result['schema'] == 'honua_data' and result['jobId'] is None
    assert len(server.seen) == 1


def background_server(server, *, statuses=(0, 1, 2), added=1, failures=0):
    state = {'polls': 0, 'discovery': 0}
    def respond(method, path, raw):
        if path.endswith('/tables'):
            state['discovery'] += 1
            rows = [{'schema': 'public', 'table': 'shared', 'srid': 4326}]
            if state['discovery'] > 1:
                rows.extend({'schema': 'honua_data', 'table': 'server_chosen_'+str(i), 'srid': 3857}
                            for i in range(added))
            return answer({'tables': rows})
        if method == 'POST':
            return answer({'jobId': 'job-42', 'statusUrl': '/api/v1/admin/import/jobs/job-42'}, 202)
        status = statuses[min(state['polls'], len(statuses)-1)]
        state['polls'] += 1
        return answer({'jobId': 'job-42', 'tableName': 'case_shelters', 'status': status,
                       'featuresProcessed': 5, 'failedFeatures': failures,
                       'completedAt': '2026-10-23T00:00:00Z' if status == 2 else None})
    server.respond = respond
    return state


def test_background_import_polls_observed_job_and_discovers_actual_table(adapter, server):
    state = background_server(server)
    before = discover_tables(adapter.http, CONNECTION, 'before')
    accepted = adapter.http.json('accepted', 'POST', '/import', {})
    result = completed_import(adapter.http, accepted, table='case_shelters', feature_count=5,
        step='import', before_tables=before, connection=CONNECTION, sleep=lambda _: None)
    assert result['table'] == 'server_chosen_0' and result['jobId'] == 'job-42'
    assert state == {'polls': 3, 'discovery': 2}
    assert len(result['references']) == 6
    assert sum(method == 'POST' for method, *_ in server.seen) == 1


@pytest.mark.parametrize('change', ['failed', 'cancelled', 'unknown', 'incomplete', 'multiple', 'missing', 'failed-features'])
def test_background_import_fails_without_replaying_or_guessing(adapter, server, change):
    statuses = {'failed': (3,), 'cancelled': (4,), 'unknown': ('Completed',), 'incomplete': (1,)}.get(change, (2,))
    state = background_server(server, statuses=statuses, added={'multiple': 2, 'missing': 0}.get(change, 1),
                              failures=1 if change == 'failed-features' else 0)
    before = discover_tables(adapter.http, CONNECTION, 'before')
    accepted = adapter.http.json('accepted', 'POST', '/import', {})
    with pytest.raises(PackError):
        completed_import(adapter.http, accepted, table='case_shelters', feature_count=5,
            step='import', before_tables=before, connection=CONNECTION, max_polls=2, sleep=lambda _: None)
    assert state['polls'] <= 2
    assert sum(method == 'POST' for method, *_ in server.seen) == 1


def test_background_status_origin_cannot_change(adapter, server):
    server.respond = lambda *_: answer({'jobId': 'job-42', 'statusUrl': 'https://elsewhere.test/jobs/job-42'}, 202)
    accepted = adapter.http.json('accepted', 'POST', '/import', {})
    before = {'observation': {'httpStatus': 200, 'body': {'tables': []}}, 'reference': {'key': 'before'}}
    with pytest.raises(PackError, match='status URL'):
        completed_import(adapter.http, accepted, table='case_shelters', feature_count=5,
            step='import', before_tables=before, connection=CONNECTION)
    assert len(server.seen) == 1


def request():
    return publication_request(connection=CONNECTION, schema='honua_data', table='server_chosen_42',
                               service='case-shelters', geometry_type='Point')


def completed_handle():
    return {'operationId': 'service.publish', 'operationInstanceId': 'opinst-42', 'correlationId': 'corr-42',
            'status': 'Completed', 'resourceIds': {'layerId': '703', 'serviceName': 'case-shelters'}}


def test_publication_uses_typed_operation_validate_submit_and_real_handle(adapter, server):
    def respond(method, path, raw):
        assert method == 'POST'
        sent = json.loads(raw)
        assert sent['parameters']['srid'] == '3857' and sent['connectionId'] == CONNECTION
        assert sent['parameters']['table'] == 'server_chosen_42'
        if path.endswith('/validate'):
            return answer({'success': True, 'data': {'isValid': True}})
        assert path == '/api/v1/operations/service.publish/submit'
        return answer({'success': True, 'data': completed_handle()})
    server.respond = respond
    result = submit_publication(adapter.http, request(), step='publish')
    assert result['status'] == 'completed' and result['layerId'] == 703
    assert result['operationInstanceId'] == 'opinst-42'
    assert len(server.seen) == 2


def pending():
    return {'operationId': 'service.publish', 'operationInstanceId': 'opinst-42', 'correlationId': 'corr-42',
            'status': 'RequiresApproval', 'proposalId': 'proposal-42', 'auditId': 'audit-42'}


def test_required_approval_remains_pending_and_never_approves(adapter, server):
    def respond(method, path, raw):
        if path.endswith('/validate'):
            return answer({'success': True, 'data': {'isValid': True}})
        return answer({'success': True, 'data': pending()})
    server.respond = respond
    result = submit_publication(adapter.http, request(), step='publish')
    assert result['status'] == 'awaiting-approval'
    assert result['proposalId'] == 'proposal-42' and 'layerId' not in result
    assert all('/approve' not in path for _, path, *_ in server.seen)


@pytest.mark.parametrize('mode', ['gate-403', 'bad-validation', 'denied', 'failed', 'indeterminate', 'wrong-service'])
def test_publication_refusal_never_turns_into_success(adapter, server, mode):
    def respond(method, path, raw):
        if path.endswith('/validate'):
            return answer({'success': True, 'data': {'isValid': mode != 'bad-validation'}})
        if mode == 'gate-403':
            return answer({'type': 'urn:honua:approval-required', 'status': 403}, 403)
        handle = completed_handle()
        if mode == 'wrong-service':
            handle['resourceIds']['serviceName'] = 'unrelated'
        else:
            handle['status'] = mode.title()
        return answer({'success': True, 'data': handle})
    server.respond = respond
    with pytest.raises(PackError):
        submit_publication(adapter.http, request(), step='publish')
    assert all('/approve' not in path for _, path, *_ in server.seen)


@pytest.mark.parametrize('actor', ['approver-77', 'author-42'])
def test_approval_observer_requires_real_distinct_actor_and_replay_binding(adapter, server, actor):
    original = {'status': 'awaiting-approval', 'proposalId': 'proposal-42', 'service': 'case-shelters'}
    def respond(method, path, raw):
        assert method == 'GET'
        if '/proposals/' in path:
            return answer({'proposalId': 'proposal-42', 'kind': 'ServicePublish', 'requestedBy': 'author-42',
                           'status': 'Succeeded', 'resolvedBy': actor, 'executionOperationId': 'replay-42'})
        assert path == '/api/v1/operations/handles/replay-42'
        handle = completed_handle()
        handle.update(operationInstanceId='replay-42', proposalId='proposal-42')
        return answer({'success': True, 'data': handle})
    server.respond = respond
    if actor == 'author-42':
        with pytest.raises(PackError, match='distinct-actor'):
            observe_approved_publication(adapter.http, original, step='approved', requested_by='author-42')
        assert len(server.seen) == 1
    else:
        result = observe_approved_publication(adapter.http, original, step='approved', requested_by='author-42')
        assert result['layerId'] == 703 and result['approvedBy'] == actor
        assert all(method == 'GET' for method, *_ in server.seen)
