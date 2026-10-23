"""Interrupted native submission cleanup over actual HTTP and actual PostGIS.

These local original-response controls exercise cleanup, never cloud qualification.
"""
import copy
from types import MethodType, SimpleNamespace
from uuid import uuid4
from datetime import datetime, timezone

import pytest

from agent_e2e_import_backend import Session, identity
from agent_e2e_import_database import ImportedFixtureDatabase
from agent_e2e_import_observer import CREATE, SUBMIT, SqlImportedDataset
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_state import DurableExecution
from test_agent_e2e_fixture_database import fixture_db, postgres
from test_agent_e2e_import_database import imported_sql
from test_agent_e2e_import_observer import imported
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server


@pytest.fixture
def recovery(imported_sql, imported, adapter, server, monkeypatch):
    database, dsn, baseline, physical, _ = imported_sql
    session = Session.__new__(Session)
    session.durable = adapter.durable
    session.context = {**adapter.http.context, 'campaignMode': 'qualification'}
    session.key, session.campaign = canonical_digest(identity(session.context)), session.context
    session.database, session.config = ImportedFixtureDatabase(database.config), {'ownershipTransfer': {}}
    session.observer_for_recovery = lambda: imported.observer
    imported.observer.sql = SqlImportedDataset(session.database, baseline=baseline)
    imported.observer.retain = session.retain
    durable = session.durable
    for row in durable.state['ledger']['entries']:
        row['campaignMode'] = 'qualification'
    durable.state['workerLaunches'] = {'import': {'context': {field: session.context[field] for field in
        ('campaignId', 'caseId', 'attempt', 'campaignMode')}}}
    for name in ('_fixture_identity', 'retain_fixture_baseline', 'retain_fixture_ownership', 'require_fixture_ownership', 'retain_fixture_cleanup'):
        setattr(durable, name, MethodType(getattr(DurableExecution, name), durable))
    durable.retain_fixture_baseline(session.context, baseline)
    session.retain('worker-stopped', {'lastStatus': 'STOPPED', 'testOnly': True})
    original_respond = server.respond
    tasks = {}
    def respond(method, path, raw):
        if method == 'GET' and '/dataset-import-tasks/' in path:
            task_id = path.split('/dataset-import-tasks/', 1)[1].split('/', 1)[0]
            if task_id in tasks:
                imported.state.task, imported.state.receipt = copy.deepcopy(tasks[task_id])
        return original_respond(method, path, raw)
    server.respond = respond
    ordinal = 0
    def execute(tool, arguments, *, lose_reply=False):
        nonlocal ordinal
        ordinal += 1
        action = {'name': 'mcp_call_tool', 'input': {'name': tool, 'arguments': arguments}}
        dispatch = {'ordinal': ordinal, 'requestDigest': canonical_digest(action), 'actionId': canonical_digest({
            **identity(session.context), 'componentBindingDigest': durable.store.candidate, 'ordinal': ordinal})[7:]}
        session.retain('model-action-'+str(ordinal), {'action': action, 'dispatch': dispatch})
        _, _, worker = imported.execute(tool, arguments, observe=False)
        if not lose_reply:
            session.retain('model-result-'+str(ordinal), worker)
            selected = imported.observer.selected(action, dispatch, worker)
            session.retain('import-ordinal-'+str(ordinal), selected)
        return worker
    def submit(*, complete, lose_reply=False):
        create = copy.deepcopy(imported.create)
        create['request']['intentId'] += '-'+str(ordinal+1)
        execute(CREATE, create)
        task = copy.deepcopy(imported.state.task)
        execute(SUBMIT, {'workspaceId': task['workspaceId'], 'taskId': task['taskId'], 'revision': task['revision'],
            'request': {'taskContentHash': task['contentHash'], 'authorityRevision': 1,
                'authorityFingerprint': 'c'*64, 'policyContextFingerprint': 'd'*64}}, lose_reply=lose_reply)
        if complete:
            imported.complete()
            imported.state.receipt['job']['output'] = copy.deepcopy(physical)
        tasks[task['taskId']] = (task, copy.deepcopy(imported.state.receipt))
        return task
    handoffs = []
    def transfer(request, **kwargs):
        import psycopg
        from psycopg import sql
        handoffs.append(copy.deepcopy(request))
        DurableExecution.retain_import_transfer_request(durable, kwargs['context'], request)
        with psycopg.connect(dsn) as connection:
            for table in request['tables']:
                connection.execute(sql.SQL('ALTER TABLE {} OWNER TO fixture_cleanup').format(sql.Identifier(table['schema'], table['name'])))
        return {'testOnly': True, 'ownershipRequestDigest': canonical_digest(request)}
    monkeypatch.setattr('hosted_pilot.fixture_handoff.transfer_ownership', transfer)
    return session, submit, tasks, dsn, physical, handoffs


@pytest.mark.parametrize('interruption', ['verify-never-ran', 'submit-reply-lost', 'invalid-feature-output'])
def test_stopped_worker_reconciles_materialized_output_without_full_verification(recovery, interruption):
    import psycopg
    from psycopg import sql
    session, submit, _, dsn, physical, handoffs = recovery
    submit(complete=True, lose_reply=interruption == 'submit-reply-lost')
    if interruption == 'invalid-feature-output':
        with psycopg.connect(dsn) as connection:
            connection.execute(sql.SQL("UPDATE {} SET geometry=NULL, properties='{{}}'::jsonb").format(sql.Identifier('honua_data', physical['physicalTableName'])))
    assert session.value('completed-imports') is None and session.value('verification') is None
    session.prepare_cleanup_ownership()
    owned = session.durable.state['fixtureOwnership'][session.key]
    assert len(owned['tables']) == 1 and len(handoffs) == 1
    assert session.cleanup_fixture(owned)['resourceIds'] == []
    assert session.database.baseline()['snapshot'] == owned['baseline']['snapshot']
    assert session.value('cleanup-import-reconciliation')['unresolved'] == []


@pytest.mark.parametrize('unknown_relation', [False, True])
def test_partial_success_is_cleaned_while_pending_job_and_unknown_relation_keep_liabilities(recovery, unknown_relation):
    import psycopg
    session, submit, tasks, dsn, physical, _ = recovery
    completed = submit(complete=True)
    pending = submit(complete=False, lose_reply=True)
    if unknown_relation:
        with psycopg.connect(dsn) as connection:
            connection.execute('CREATE TABLE honua_data.unproven_pending_output (id int)')
            connection.execute('INSERT INTO honua_data.unproven_pending_output VALUES (17)')
    session.prepare_cleanup_ownership()
    owned = session.durable.state['fixtureOwnership'][session.key]
    reconciliation = session.value('cleanup-import-reconciliation')
    assert [row['taskId'] for row in reconciliation['materialized']] == [completed['taskId']]
    assert [row['taskId'] for row in reconciliation['unresolved']] == [pending['taskId']]
    with pytest.raises(PackError, match='remaining liabilities'): session.cleanup_fixture(owned)
    assert session.value('partial-import-sql-cleanup') is not None
    assert all(row['state'] == 'reserved' for row in session.durable.state['ledger']['entries'])
    with psycopg.connect(dsn) as connection:
        assert connection.execute('SELECT to_regclass(%s)', ('honua_data.'+physical['physicalTableName'],)).fetchone()[0] is None
        assert connection.execute('SELECT id FROM honua_data.shared').fetchall() == [(7,)]
        if unknown_relation:
            assert connection.execute('SELECT id FROM honua_data.unproven_pending_output').fetchall() == [(17,)]
    assert tasks[pending['taskId']][1]['job']['status'] == 'queued'


def test_two_cleanup_passes_preserve_original_absence_and_finish_only_later_terminal_output(recovery):
    import psycopg
    from psycopg import sql
    from agent_e2e_native_costs import reserve_overhead, OVERHEAD
    from test_agent_e2e_native_costs import native
    session, submit, tasks, dsn, physical, handoffs = recovery
    task, definition, prices = native()  # Explicit local native-cost contract control, never cloud evidence.
    stop = {'taskArn': task['taskArn'], 'taskStatus': 'STOPPED', 'stopRecorded': True, 'testOnly': True}
    session.launch = {'attemptId': 'import', 'testOnly': True}
    session.open_worker = lambda: None
    session.await_worker = lambda **_kwargs: copy.deepcopy(stop)
    session.lifecycle = SimpleNamespace(stop=lambda *_args: copy.deepcopy(stop), _task=lambda _arn: copy.deepcopy(task))
    session.price_receipt = prices
    session.retain('worker-definition', definition)
    session.durable.state['ledger']['operations'].append({'id': 'shared-overhead', 'operationId': 'cleanup-recovery-control',
        'purpose': 'shared-environment', 'kind': 'infrastructure', 'amountUsd': '1', 'basisDigest': 'sha256:'+'d'*64,
        'state': 'reserved', 'actualUsd': None})
    coverage = {'schemaVersion': 1, 'allocation': 'compute-infrastructure-including-cleanup', 'bindings': [{
        'reservationId': 'shared-overhead', 'basisDigest': 'sha256:'+'d'*64, 'categories': sorted(OVERHEAD),
        'perAttemptCeilingUsd': '0.25', 'maximumAttempts': 4}]}
    session.config['accounting'] = {'overheadCoverage': coverage}
    reserve_overhead(session.durable, session.context, coverage)
    first = submit(complete=True)
    pending = submit(complete=False, lose_reply=True)
    ledger_before = copy.deepcopy(session.durable.state['ledger'])
    overhead_before = copy.deepcopy(session.durable.state['nativeOverheadAllocations'])
    first_result = session.cleanup()
    assert first_result['status'] == 'blocked'
    assert session.value('partial-import-sql-cleanup') is not None, (first_result, session.value('cleanup-import-reconciliation'))
    assert session.durable.state['ledger'] == ledger_before
    assert session.durable.state['nativeOverheadAllocations'] == overhead_before
    assert not session.durable.state.get('nativeCostAllocations')
    original = copy.deepcopy(session.durable.state)
    with psycopg.connect(dsn) as connection:
        assert connection.execute('SELECT to_regclass(%s)', ('honua_data.'+physical['physicalTableName'],)).fetchone()[0] is None
    second_name = 'actual_later_import_'+uuid4().hex[:12]
    with psycopg.connect(dsn) as connection:
        connection.execute(sql.SQL('CREATE TABLE {} (id int)').format(sql.Identifier('honua_data', second_name)))
        connection.execute(sql.SQL('COMMENT ON TABLE {} IS {}').format(sql.Identifier('honua_data', second_name), sql.Literal('honua:import-owned:v1')))
        connection.execute(sql.SQL('GRANT SELECT ON {} TO fixture_observer').format(sql.Identifier('honua_data', second_name)))
    tasks[pending['taskId']][1]['job'].update(status='succeeded', completedAt=datetime.now(timezone.utc).isoformat(),
        output={**physical, 'physicalTableName': second_name})
    second_result = session.cleanup()
    assert second_result['status'] == 'pass' and second_result['fixtureSql']['resourceIds'] == []
    assert float(second_result['observedInfrastructureUsd']) > 0
    assert len(handoffs) == 2 and handoffs[0]['tables'][0]['oid'] != handoffs[1]['tables'][0]['oid']
    assert len(session.durable.state['importTransferRequests']) == 2
    assert session.durable.state['ledger'] == ledger_before
    assert session.durable.state['nativeOverheadAllocations'] == overhead_before
    assert len(session.durable.state['nativeCostAllocations']) == 1
    from agent_e2e_state import preserve_history
    preserve_history(original, session.durable.state)
    assert session.value('cleanup-import-reconciliation')['previouslyCleaned'][0]['taskId'] == first['taskId']
    assert [row['taskId'] for row in session.value('cleanup-import-reconciliation')['materialized']] == [pending['taskId']]
    assert session.database.baseline()['snapshot'] == session.durable.state['fixtureBaselines'][session.key]['snapshot']
    before_repeat = copy.deepcopy(session.durable.state)
    assert session.cleanup() == second_result
    assert session.durable.state == before_repeat


@pytest.mark.parametrize('window', ['after-begin','after-stop','after-ownership','after-intent',
    'commit-before-observation','commit-before-observation-with-new-relation','after-observation','after-cost','before-outcome'])
def test_process_loss_recovers_original_sql_custody_without_erasing_history_or_double_charge(recovery, window):
    from agent_e2e_native_costs import reserve_overhead, OVERHEAD
    from test_agent_e2e_native_costs import native
    session, submit, _, dsn, _, handoffs = recovery
    interruption_window = window.replace('-with-new-relation','')
    task, definition, prices = native()
    stop = {'taskArn':task['taskArn'],'taskStatus':'STOPPED','stopRecorded':True,'testOnly':True}
    session.launch = {'attemptId':'import','testOnly':True}
    session.open_worker = lambda: None
    session.await_worker = lambda **_: copy.deepcopy(stop)
    session.lifecycle = SimpleNamespace(stop=lambda *_:copy.deepcopy(stop),_task=lambda _:copy.deepcopy(task))
    session.price_receipt = prices
    session.retain('worker-definition',definition)
    session.durable.state['ledger']['operations'].append({'id':'shared-overhead','operationId':'crash-control',
        'purpose':'shared-environment','kind':'infrastructure','amountUsd':'1','basisDigest':'sha256:'+'d'*64,
        'state':'reserved','actualUsd':None})
    coverage = {'schemaVersion':1,'allocation':'compute-infrastructure-including-cleanup','bindings':[{
        'reservationId':'shared-overhead','basisDigest':'sha256:'+'d'*64,'categories':sorted(OVERHEAD),
        'perAttemptCeilingUsd':'0.25','maximumAttempts':4}]}
    session.config['accounting'] = {'overheadCoverage':coverage}
    reserve_overhead(session.durable,session.context,coverage)
    submit(complete=True)
    durable, key = session.durable, session.key+'/import-cleanup-1'
    original_ledger = copy.deepcopy(durable.state['ledger'])
    persist, interrupted = durable._persist, []
    def crash():
        state = durable.state
        phases = {row.get('phase') for row in state.get('scenarioOperations',{}).values()}
        ready = {
            'after-begin': key in state.get('importCleanupPasses',{}),
            'after-stop': 'import-cleanup-1/worker-stopped' in phases,
            'after-ownership':key in state.get('fixtureOwnership',{}),
            'after-intent':key+'/intent' in state.get('fixtureCleanup',{}),
            'commit-before-observation':key+'/observation' in state.get('fixtureCleanup',{}),
            'after-observation':key+'/observation' in state.get('fixtureCleanup',{}),
            'after-cost':bool(state.get('nativeCostAllocations')),
            'before-outcome':key in state.get('importCleanupOutcomes',{}),
        }[interruption_window]
        if ready and not interrupted:
            interrupted.append(window)
            if interruption_window not in ('commit-before-observation','before-outcome'):
                persist()
            raise SystemExit('simulated process termination at '+window)
        persist()
    durable._persist = crash
    with pytest.raises(SystemExit): session.cleanup()
    assert interrupted == [window]
    durable._persist = persist
    durable.state = copy.deepcopy(durable.snapshots[-1])
    before_recovery = copy.deepcopy(durable.state)
    assert key not in durable.state.get('importCleanupOutcomes',{})
    if window.endswith('-with-new-relation'):
        import psycopg
        with psycopg.connect(dsn) as connection:
            connection.execute('CREATE TABLE honua_data.arrived_later (id int)')
            connection.execute('INSERT INTO honua_data.arrived_later VALUES (23)')
        assert session.cleanup()['status'] == 'blocked'
        assert key+'/observation' in durable.state['fixtureCleanup']
        assert not durable.state.get('nativeCostAllocations')
        with psycopg.connect(dsn) as connection:
            assert connection.execute('SELECT id FROM honua_data.arrived_later').fetchall() == [(23,)]
            connection.execute('DROP TABLE honua_data.arrived_later')
    result = session.cleanup()
    assert result['status'] == 'pass', result
    assert result['fixtureSql']['resourceIds'] == []
    assert result['observedInfrastructureUsd'] != '0'
    assert len(handoffs) == 1
    assert len(durable.state['nativeCostAllocations']) == 1
    assert durable.state['ledger'] == original_ledger
    assert session.database.baseline()['snapshot'] == durable.state['fixtureBaselines'][session.key]['snapshot']
    from agent_e2e_state import preserve_history
    preserve_history(before_recovery,durable.state)
    after_recovery = copy.deepcopy(durable.state)
    assert session.cleanup() == result and durable.state == after_recovery
