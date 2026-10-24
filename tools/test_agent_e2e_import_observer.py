"""Real MCP + independent HTTP contract replay; fixtures never qualify imports."""
import copy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_e2e_import_observer import DatasetImportObserver, CREATE, SUBMIT
from agent_e2e_mcp_runner import run
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_scenario_backend import ScenarioHttp
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server
from test_agent_e2e_mcp import result, standard


@pytest.fixture
def imported(adapter, server, monkeypatch):
    created = datetime.now(timezone.utc)-timedelta(seconds=2)
    context = {'workspaceId': 'native-import-workspace', 'turnId': 'native-import-turn'}
    monkeypatch.setenv('IMPORT_TASK_TOKEN', 'private-task-import-key')
    http = ScenarioHttp(durable=adapter.durable, context=adapter.http.context, endpoint=server.url,
        credential_env='IMPORT_TASK_TOKEN', credential_kind='api-key', attempt_id='import-observer', agent_context=context)
    source = {'schema': 'honua_data', 'table': 'ae_requested_parcels', 'sourceSha256': 'a'*64,
        'totalBytes': 4096, 'fileName': 'parcels.geojson', 'sourceSrid': 4326, 'targetSrid': 3857}
    binding = {'workspaceId': context['workspaceId'], 'ownerPrincipalId': 'api-key:'+str(uuid4()),
        'tenantId': 'actual-captured-tenant', 'targetId': 'actual-captured-target', 'destinations': [source],
        'notBefore': (created-timedelta(seconds=1)).isoformat()}
    state = SimpleNamespace(task=None, receipt=None, records={}, sql=[], ordinal=0)
    def sql(receipt):
        state.sql.append(copy.deepcopy(receipt))
        return {'features': {'srid': 3857, 'features': [{'type': 'Feature', 'id': 'p1',
            'properties': {'source_id': 'p1'}, 'geometry': {'type': 'Polygon', 'coordinates': [[[0,0],[0,10],[10,0],[0,0]]]}}]}}
    def retain(name, value):
        assert name not in state.records
        state.records[name] = copy.deepcopy(value)
    observer = DatasetImportObserver(http=http, binding=binding, sql=SimpleNamespace(observe=sql), retain=retain)
    def respond(method, path, raw):
        headers = {key.lower(): value for key, value in server.seen[-1][2].items()}
        assert headers['x-honua-workspace'] == context['workspaceId'] and headers['x-honua-turn'] == context['turnId']
        if path == '/mcp':
            request = json.loads(raw) if raw else None
            if request and request.get('method') == 'tools/call':
                tool, args = request['params']['name'], request['params']['arguments']
                if tool == CREATE:
                    state.task = {'taskId': 'import-task-'+uuid4().hex, 'revision': 1, 'contentHash': 'b'*64,
                        'ownerActorId': binding['ownerPrincipalId'], 'tenantId': binding['tenantId'], 'targetId': binding['targetId'],
                        'workspaceId': context['workspaceId'], 'destination': copy.deepcopy(args['request']['destination']),
                        'source': copy.deepcopy(args['request']['source']), 'options': copy.deepcopy(args['request']['options']),
                        'uploadAdmissionDeadline': (created+timedelta(hours=24)).isoformat()}
                    output = state.task
                else:
                    assert tool == SUBMIT
                    state.receipt = {'taskId': state.task['taskId'], 'taskRevision': 1, 'taskContentHash': state.task['contentHash'],
                        'state': 'accepted', 'operationId': 'import-op-'+uuid4().hex,
                        'authorityRevision': 1, 'authorityFingerprint': 'c'*64, 'policyContextFingerprint': 'd'*64,
                        'acceptBefore': (created+timedelta(hours=1)).isoformat()}
                    state.receipt['job'] = {'jobId': state.receipt['operationId'], 'leaseId': 'native-lease-'+uuid4().hex,
                        'status': 'queued', 'createdAt': created.isoformat(), 'updatedAt': created.isoformat(),
                        'completedAt': None, 'percentComplete': None, 'output': None, 'errorCode': None, 'outcomePending': False}
                    output = state.receipt
                return result(request, {'content': [], 'structuredContent': copy.deepcopy(output)})
            return standard(request)
        assert method == 'GET', 'observer cannot upload, submit or replay an import'
        if '/submission?' in path: output = state.receipt
        elif '/dataset-import-task-intents/' in path or '/dataset-import-tasks/' in path: output = state.task
        else: raise AssertionError(path)
        return 200, {'Content-Type': 'application/json'}, json.dumps(output).encode()
    def typed_response(*args):
        status, headers, raw = respond(*args)
        return status, {'Content-Type': 'application/json', **headers}, raw
    server.respond = typed_response
    actor = {'endpoint': server.url+'/mcp', 'credentialKind': 'api-key', 'credential': 'private-task-import-key',
        'workflowView': None, 'agentContext': context}
    create = {'workspaceId': context['workspaceId'], 'request': {'intentId': 'persisted-original-intent', 'viewId': 'native-view',
        'contextRevision': 1, 'destination': {'schema': source['schema'], 'table': source['table']},
        'source': {'kind': 'browser-file', 'fileName': source['fileName'], 'totalBytes': source['totalBytes'],
            'expectedSha256': source['sourceSha256'], 'browserSelectionId': 'native-human-selection'},
        'options': {'loadMode': 0, 'sourceSrid': source['sourceSrid'], 'targetSrid': source['targetSrid']}}}
    def execute(tool, args, *, observe=True):
        state.ordinal += 1
        action = {'name': 'mcp_call_tool', 'input': {'name': tool, 'arguments': args}}
        reply = run(action, actor, 30)
        assert reply['error'] is None
        worker = {'agentResult': {'content': reply['result']}}
        dispatch = {'actionId': canonical_digest({'ordinal': state.ordinal})[7:], 'requestDigest': canonical_digest(action)}
        return observer.selected(action, dispatch, worker) if observe else (action, dispatch, worker)
    def submit():
        return execute(SUBMIT, {'workspaceId': context['workspaceId'], 'taskId': state.task['taskId'], 'revision': 1,
            'request': {'taskContentHash': state.task['contentHash'], 'authorityRevision': 1,
                'authorityFingerprint': 'c'*64, 'policyContextFingerprint': 'd'*64}})
    def complete():
        state.receipt['job'].update(status='succeeded', completedAt=(created+timedelta(seconds=1)).isoformat(),
            output={'physicalTableName': 'actual_provider_physical_table', 'schema': 'honua_data', 'featureCount': 1, 'detectedSrid': 4326})
    return SimpleNamespace(observer=observer, state=state, execute=execute, create=create, submit=submit, complete=complete)


def test_only_successful_native_job_output_reaches_independent_sql(imported, server):
    created = imported.execute(CREATE, imported.create)
    selected = imported.submit()
    queued = imported.observer.observe(selected, step='queued')
    assert queued['dataset'] is None and not imported.state.sql
    imported.complete()
    observed = imported.observer.observe(selected, step='complete')
    assert imported.state.sql[0]['physicalTableName'] == 'actual_provider_physical_table'
    assert imported.state.sql[0]['physicalTableName'] != created['task']['destination']['table']
    assert observed['features.exact']['features'][0]['properties']['source_id'] == 'p1'
    assert observed['geometry.crs']['sourceSrid'] == 4326 and observed['geometry.crs']['returnedSrid'] == 3857
    assert all(method == 'GET' for method, path, *_ in server.seen if path != '/mcp')


@pytest.mark.parametrize('change', ['owner', 'source', 'target'])
def test_original_task_must_match_real_scope_source_and_native_owner(imported, change):
    action, dispatch, worker = imported.execute(CREATE, imported.create, observe=False)
    if change == 'owner': imported.state.task['ownerActorId'] = 'different-principal'
    elif change == 'source': imported.state.task['source']['expectedSha256'] = 'e'*64
    else: imported.state.task['targetId'] = 'different-target'
    with pytest.raises(PackError): imported.observer.selected(action, dispatch, worker)
    assert not imported.state.sql and not imported.state.records


def test_prepared_authority_without_accepted_job_never_counts_as_imported_data(imported):
    imported.execute(CREATE, imported.create)
    selected = imported.submit()
    imported.state.receipt.update(state='prepared', job=None)
    observed = imported.observer.observe(selected, step='prepared-only')
    assert observed['dataset'] is None and 'features.exact' not in observed and not imported.state.sql


@pytest.mark.parametrize('change', ['pending-outcome', 'wrong-job', 'failed-with-output'])
def test_unconfirmed_or_foreign_job_cannot_become_completed_dataset(imported, change):
    imported.execute(CREATE, imported.create)
    selected = imported.submit()
    imported.complete()
    if change == 'pending-outcome': imported.state.receipt['job']['outcomePending'] = True
    elif change == 'wrong-job': imported.state.receipt['job']['jobId'] = 'foreign-job'
    else: imported.state.receipt['job']['status'] = 'failed'
    with pytest.raises(PackError): imported.observer.observe(selected, step='invalid-completion')
    assert not imported.state.sql
