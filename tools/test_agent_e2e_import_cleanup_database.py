"""Actual imported-table handoff ownership and transactional DROP RESTRICT."""
import copy
from uuid import uuid4

import pytest

from agent_e2e_import_database import ImportedFixtureDatabase
from agent_e2e_import_observer import SqlImportedDataset
from agent_e2e_pack import PackError, canonical_digest
from test_agent_e2e_fixture_database import fixture_db, postgres
from test_agent_e2e_import_database import imported_sql


def native_output(database, baseline, receipt):
    task, job, lease = ('observed-'+uuid4().hex for _ in range(3))
    return {'taskId': task, 'submission': {'state': 'accepted', 'taskId': task, 'taskRevision': 1,
        'taskContentHash': 'a'*64, 'operationId': job,
        'job': {'status': 'succeeded', 'jobId': job, 'leaseId': lease, 'outcomePending': False, 'output': receipt}},
        'dataset': SqlImportedDataset(database, baseline=baseline).observe(receipt),
        'nativeReferences': {name: {'key': 'actual-contract-'+name, 'sha256': 'sha256:'+'b'*64} for name in ('task', 'submission')}}


def transfer_and_adopt(imported_sql):
    import psycopg
    from psycopg import sql
    database, dsn, baseline, receipt, _ = imported_sql
    imported = ImportedFixtureDatabase(database.config)
    observed = native_output(database, baseline, receipt)
    request = imported.observe_import_tables(baseline, [observed])
    # Only a separately fenced native provisioner performs this exact owner
    # change in production. The test exercises PostgreSQL's real owner boundary.
    with psycopg.connect(dsn) as connection:
        connection.execute(sql.SQL('ALTER TABLE {} OWNER TO fixture_cleanup').format(sql.Identifier('honua_data', receipt['physicalTableName'])))
    return imported, dsn, imported.ownership_imports(baseline, request), request


def hooks(record, *, fail_at=None):
    state = {'authorizations': 0, 'records': {}}
    def authorize(actual):
        assert actual == record
        state['authorizations'] += 1
        if state['authorizations'] == fail_at: raise PackError('lost fenced authority at precommit')
    def retain(kind, value):
        assert kind not in state['records']
        state['records'][kind] = copy.deepcopy(value)
        return {'sha256': canonical_digest(value)}
    return state, authorize, retain


def test_actual_one_table_import_handoff_and_independent_absence_does_not_invent_three_tables(imported_sql):
    database, _, record, request = transfer_and_adopt(imported_sql)
    assert len(request['tables']) == len(request['nativeImports']) == 1
    state, authorize, retain = hooks(record)
    actual = database.cleanup(record, authorize=authorize, retain=retain)
    assert actual['resourceIds'] == [] and actual['cleanupPrincipal'] != actual['observerPrincipal']
    assert state['records']['observation'] == actual
    assert database.baseline()['snapshot'] == record['baseline']['snapshot']


def test_actual_import_precommit_authority_loss_rolls_back_drop(imported_sql):
    database, _, record, _ = transfer_and_adopt(imported_sql)
    before = database.baseline()
    state, authorize, retain = hooks(record, fail_at=3)
    with pytest.raises(PackError, match='precommit'): database.cleanup(record, authorize=authorize, retain=retain)
    assert database.baseline() == before and set(state['records']) == {'intent'}


def test_actual_unknown_view_dependency_is_preserved_and_blocks_import_drop(imported_sql):
    import psycopg
    from psycopg import sql
    database, dsn, record, _ = transfer_and_adopt(imported_sql)
    table = record['tables'][0]
    with psycopg.connect(dsn) as connection:
        connection.execute(sql.SQL('CREATE VIEW public.unowned_import_reference AS SELECT properties FROM {}').format(sql.Identifier(table['schema'], table['name'])))
    state, authorize, retain = hooks(record)
    with pytest.raises(psycopg.errors.DependentObjectsStillExist): database.cleanup(record, authorize=authorize, retain=retain)
    with psycopg.connect(dsn) as connection:
        assert connection.execute('SELECT count(*) FROM public.unowned_import_reference').fetchone()[0] == 1
    assert database.baseline()['snapshot'] == record['prepared'] and set(state['records']) == {'intent'}


def test_actual_import_without_private_owner_transfer_cannot_be_adopted(imported_sql):
    database, _, baseline, receipt, _ = imported_sql
    imported = ImportedFixtureDatabase(database.config)
    request = imported.observe_import_tables(baseline, [native_output(database, baseline, receipt)])
    with pytest.raises(PackError, match='transferred to exact cleanup role'):
        imported.ownership_imports(baseline, request)
