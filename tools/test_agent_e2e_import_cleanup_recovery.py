"""Interrupted native submission cleanup over actual HTTP and actual PostGIS.

These local original-response controls exercise cleanup, never cloud qualification.
"""
import copy
from types import MethodType

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
    def transfer(request, **_kwargs):
        import psycopg
        from psycopg import sql
        handoffs.append(copy.deepcopy(request))
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
