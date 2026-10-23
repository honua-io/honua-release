"""Private provisioner replays genuine test-server originals, then exact SQL ownership.

These development checks are not live product/model qualification.
"""
import copy
import io
import json
from pathlib import Path
import sys
import time
from unittest.mock import MagicMock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from test_agent_e2e_import_state import retained, imported, adapter, server
from fixture_provisioner_postgres_test import tls_fixture, fixture_db, postgres
from agent_e2e_state import DurableExecution
from agent_e2e_pack import PackError, canonical_digest
import fixture_handoff as handoff
import fixture_provisioner as provisioner


@pytest.mark.parametrize('changed', [False, True])
@pytest.mark.parametrize('retention', ['current', 'legacy', 'duplicate-alias', 'wrong-retained-key'])
def test_private_provisioner_independently_replays_actual_retained_task_job_http_originals(retained, changed, retention):
    durable, context, key, request = retained
    reference = DurableExecution.retain_import_transfer_request(durable, context, request)
    state = copy.deepcopy(durable.state)
    base = canonical_digest({name: context[name] for name in ('campaignId', 'caseId', 'attempt')})
    compound = base+'/'+canonical_digest(request)
    assert reference == {'key': compound, 'sha256': canonical_digest(request)}
    if retention == 'legacy':
        state['importTransferRequests'][base] = state['importTransferRequests'].pop(compound)
        reference = {'key': base, 'sha256': canonical_digest(request)}
    elif retention == 'duplicate-alias':
        state['importTransferRequests'][base] = copy.deepcopy(request)
    elif retention == 'wrong-retained-key':
        reference = {'key': base, 'sha256': canonical_digest(request)}
    if changed:
        del state['scenarioOperations'][request['nativeImports'][0]['submissionReference']['key']]
    generation = 'sha256:'+'b'*64
    snapshot = {'candidateKey': durable.store.candidate, 'generationDigest': generation, 'version': 7, 'payload': state}
    raw = handoff.encoded(snapshot)
    row = {'generationDigest': {'S': generation}, 'leaseOwner': {'S': 'original-controller'}, 'fence': {'N': '8'},
        'leaseUntil': {'N': str(int(time.time())+120)}, 'snapshotKey': {'S': 'agent-e2e/'+durable.store.candidate[7:]+'/snapshot.json'},
        'snapshotVersion': {'S': 'native-snapshot-version'}, 'snapshotDigest': {'S': 'sha256:'+__import__('hashlib').sha256(raw).hexdigest()}, 'stateVersion': {'N': '7'}}
    s3, db = MagicMock(), MagicMock()
    db.get_item.return_value = {'Item': row}
    s3.get_object.return_value = {'Body': io.BytesIO(raw), 'VersionId': 'native-snapshot-version'}
    config = {'candidateKey': durable.store.candidate, 'generationDigest': generation, 'state': {'table': 'original-state', 'bucket': 'original-custody'}}
    envelope = {'request': request, 'context': context, 'retained': reference, 'fence': {'candidateKey': durable.store.candidate,
        'owner': 'original-controller', 'fence': 8, 'stateTable': 'original-state', 'stateBucket': 'original-custody',
        'stateKey': {'candidateKey': {'S': durable.store.candidate}}}}
    if changed or retention in ('duplicate-alias', 'wrong-retained-key'):
        with pytest.raises(PackError, match='absent or changed|cannot migrate an original request'):
            provisioner.observe_authority(config, envelope, s3, db)
    else:
        assert provisioner.observe_authority(config, envelope, s3, db) == row
    db.put_item.assert_not_called()
    db.update_item.assert_not_called()


def imported_request(tls_fixture, count):
    import psycopg
    from psycopg import sql
    database, dsn = tls_fixture
    baseline = database.baseline()
    with psycopg.connect(dsn) as connection:
        connection.execute('GRANT USAGE ON SCHEMA honua_data TO fixture_app')
        for index in range(count):
            name = 'model_import_'+str(index)
            connection.execute(sql.SQL('CREATE TABLE {} (id int)').format(sql.Identifier('honua_data', name)))
            connection.execute(sql.SQL("COMMENT ON TABLE {} IS 'honua:import-owned:v1'").format(sql.Identifier('honua_data', name)))
            connection.execute(sql.SQL('ALTER TABLE {} OWNER TO fixture_app').format(sql.Identifier('honua_data', name)))
        rows = connection.execute("SELECT c.oid::bigint, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='honua_data' AND c.relname LIKE 'model_import_%' ORDER BY c.oid").fetchall()
    tables = [{'oid': oid, 'schema': 'honua_data', 'name': name, 'owner': 'fixture_app'} for oid, name in rows]
    return {'schemaVersion': 1, 'profile': handoff.IMPORT_PROFILE, 'candidateKey': database.config['candidateKey'],
        'database': baseline['database'], 'baselineDigest': canonical_digest(baseline), 'expectedCleanupRole': 'fixture_cleanup',
        'tables': tables, 'nativeImports': [{'taskId': 'observed-task-'+str(index), 'taskRevision': 1, 'taskContentHash': 'a'*64,
            'jobId': 'observed-job-'+str(index), 'leaseId': 'observed-lease-'+str(index), 'tableOid': table['oid'],
            'taskReference': {'key': 'original-task-'+str(index), 'sha256': 'sha256:'+'b'*64},
            'submissionReference': {'key': 'original-submission-'+str(index), 'sha256': 'sha256:'+'c'*64}}
            for index, table in enumerate(tables)]}


@pytest.mark.parametrize('count', [1, 2, 16])
def test_actual_model_import_owner_transfer_supports_exact_bounded_outputs_without_fake_p10_tables(tls_fixture, count):
    import psycopg
    from psycopg.rows import dict_row
    request = imported_request(tls_fixture, count)
    handoff.validate_request(request)
    legacy = {key: value for key, value in request.items() if key not in ('profile', 'nativeImports')}
    with pytest.raises(PackError, match='exactly three'):
        handoff.validate_request(legacy)
    with psycopg.connect(tls_fixture[1], sslmode='require', autocommit=True, row_factory=dict_row) as connection:
        observed = provisioner.transfer(connection, request, authorize=lambda: None)
        assert observed == [table | {'owner': 'fixture_cleanup'} for table in request['tables']]
        owners = connection.execute('SELECT pg_get_userbyid(relowner) AS owner FROM pg_class WHERE oid=ANY(%s::oid[])', ([table['oid'] for table in request['tables']],)).fetchall()
        assert owners == [{'owner': 'fixture_cleanup'}]*count


def test_actual_missing_import_owned_marker_refuses_all_owner_changes(tls_fixture):
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    request = imported_request(tls_fixture, 2)
    with psycopg.connect(tls_fixture[1], sslmode='require', autocommit=True, row_factory=dict_row) as connection:
        connection.execute(sql.SQL('COMMENT ON TABLE {} IS NULL').format(sql.Identifier('honua_data', request['tables'][0]['name'])))
        with pytest.raises(PackError, match='import-owned table marker'):
            provisioner.transfer(connection, request, authorize=lambda: None)
        owners = connection.execute('SELECT pg_get_userbyid(relowner) AS owner FROM pg_class WHERE oid=ANY(%s::oid[])', ([table['oid'] for table in request['tables']],)).fetchall()
        assert owners == [{'owner': 'fixture_app'}]*2
