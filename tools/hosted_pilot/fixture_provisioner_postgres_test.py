"""Actual PostgreSQL owner transfer, TLS refusal and transaction rollback."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from agent_e2e_pack import PackError, canonical_digest
from test_agent_e2e_fixture_database import postgres, fixture_db, seed_owned
import fixture_provisioner as provisioner


@pytest.fixture
def tls_fixture(fixture_db):
    """Enable actual TLS inside the already admitted owned local test container."""
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
    database, dsn = fixture_db
    port = conninfo_to_dict(dsn)['port']
    candidates = subprocess.check_output(['docker', 'ps', '--filter', 'name=honua-agent-sql-', '--format', '{{.Names}}'], text=True).splitlines()
    assert candidates
    inspected = json.loads(subprocess.check_output(['docker', 'inspect', *candidates], text=True))
    names = [row['Name'].lstrip('/') for row in inspected
             if any(binding['HostPort'] == port and binding['HostIp'] == '127.0.0.1'
                    for binding in row['NetworkSettings']['Ports'].get('5432/tcp', []))]
    assert len(names) == 1
    with tempfile.TemporaryDirectory() as folder:
        key, cert = Path(folder)/'key.pem', Path(folder)/'cert.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
            '-subj', '/CN=localhost', '-keyout', str(key), '-out', str(cert)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for source, destination in [(key, '/tmp/test-server.key'), (cert, '/tmp/test-server.crt')]:
            subprocess.run(['docker', 'exec', '-i', names[0], 'sh', '-c', 'umask 077; cat > '+destination],
                input=source.read_bytes(), check=True)
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("ALTER SYSTEM SET ssl_cert_file = '/tmp/test-server.crt'")
        connection.execute("ALTER SYSTEM SET ssl_key_file = '/tmp/test-server.key'")
        connection.execute("ALTER SYSTEM SET ssl = 'on'")
        connection.execute('SELECT pg_reload_conf()')
    yield database, dsn


def request_for(fixture_db):
    import psycopg
    from psycopg import sql
    database, dsn = fixture_db
    record = seed_owned(fixture_db)
    with psycopg.connect(dsn) as connection:
        connection.execute('GRANT USAGE ON SCHEMA honua_data TO fixture_app')
        for table in record['tables']:
            connection.execute(sql.SQL('ALTER TABLE {} OWNER TO fixture_app').format(sql.Identifier(table['schema'], table['name'])))
    tables = [table | {'owner': 'fixture_app'} for table in record['tables']]
    return {'schemaVersion': 1, 'candidateKey': database.config['candidateKey'], 'database': record['baseline']['database'],
        'baselineDigest': canonical_digest(record['baseline']), 'expectedCleanupRole': 'fixture_cleanup', 'tables': tables}


def current_owners(dsn, request):
    import psycopg
    with psycopg.connect(dsn) as connection:
        return [row[0] for row in connection.execute('SELECT pg_get_userbyid(relowner) FROM pg_class WHERE oid=ANY(%s::oid[]) ORDER BY oid',
            ([table['oid'] for table in request['tables']],)).fetchall()]


def test_real_tls_transfer_preserves_oids_and_transfers_only_observed_tables(tls_fixture):
    import psycopg
    from psycopg.rows import dict_row
    request = request_for(tls_fixture)
    with psycopg.connect(tls_fixture[1], sslmode='require', row_factory=dict_row, autocommit=True) as connection:
        result = provisioner.transfer(connection, request, authorize=lambda: None)
    assert result == [table | {'owner': 'fixture_cleanup'} for table in request['tables']]
    assert current_owners(tls_fixture[1], request) == ['fixture_cleanup']*3
    with psycopg.connect(tls_fixture[1]) as connection:
        connection.execute('SET ROLE fixture_app')
        for table in request['tables']:
            from psycopg import sql
            connection.execute(sql.SQL('SELECT * FROM {}').format(sql.Identifier(table['schema'], table['name']))).fetchall()


@pytest.mark.parametrize('failure', ['stale-fence-before-commit', 'replaced-oid', 'wrong-database', 'plaintext'])
def test_real_transfer_refuses_changed_identity_and_rolls_back_all_owner_effects(tls_fixture, failure):
    import psycopg
    from psycopg.rows import dict_row
    request = request_for(tls_fixture)
    if failure == 'replaced-oid':
        request['tables'][1]['oid'] += 10000000
    elif failure == 'wrong-database':
        request['database']['databaseOid'] += 1
    calls = []
    def authorize():
        calls.append(True)
        if failure == 'stale-fence-before-commit' and len(calls) == 3:
            raise PackError('stale native fence')
    with psycopg.connect(tls_fixture[1], sslmode='disable' if failure == 'plaintext' else 'require', row_factory=dict_row, autocommit=True) as connection:
        with pytest.raises(PackError):
            provisioner.transfer(connection, request, authorize=authorize)
    # Inspect actual names independently even when the submitted OID was wrong.
    with psycopg.connect(tls_fixture[1]) as connection:
        owners = [row[0] for row in connection.execute("SELECT pg_get_userbyid(relowner) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='honua_data' AND c.relname <> 'shared'").fetchall()]
    assert owners == ['fixture_app']*3


