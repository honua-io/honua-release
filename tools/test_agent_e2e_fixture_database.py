"""Real PostgreSQL rollback/absence tests; opt in with HONUA_AGENT_POSTGRES_TESTS=1.

An already cached immutable PostGIS image runs in an owned, bounded local container.
These transactions test cleanup safety, not live model/product qualification.
"""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import time
from uuid import uuid4

import pytest

from agent_e2e_fixture_database import FixtureDatabase
from agent_e2e_pack import PackError, canonical_digest


@pytest.fixture(scope='module')
def postgres():
    if os.environ.get('HONUA_AGENT_POSTGRES_TESTS') != '1':
        pytest.skip('opt-in actual PostgreSQL transaction checks')
    psycopg = pytest.importorskip('psycopg')
    assert shutil.which('docker'), 'Docker is required for requested actual PostgreSQL checks'
    name = 'honua-agent-sql-'+uuid4().hex[:12]
    image = subprocess.check_output(['docker', 'image', 'inspect', 'postgis/postgis:18-3.6',
                                     '--format', '{{.Id}}'], text=True).strip()
    subprocess.run(['docker', 'run', '--detach', '--pull', 'never', '--name', name,
        '--user', '999:999', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
        '--memory', '512m', '--cpus', '0.5', '--pids-limit', '100',
        '--tmpfs', '/var/lib/postgresql:rw,size=256m,uid=999,gid=999',
        '--tmpfs', '/var/run/postgresql:rw,uid=999,gid=999', '--tmpfs', '/tmp:rw,size=64m',
        '--publish', '127.0.0.1::5432', '--env', 'PGDATA=/var/lib/postgresql/data',
        '--env', 'POSTGRES_DB=honua_agent_contract', '--env', 'POSTGRES_PASSWORD=local-contract-only',
        image, '-c', 'shared_buffers=16MB', '-c', 'max_connections=20'],
        check=True, stdout=subprocess.DEVNULL)
    try:
        port = subprocess.check_output(['docker', 'port', name, '5432/tcp'], text=True).strip().rsplit(':', 1)[1]
        dsn = f'host=127.0.0.1 port={port} dbname=honua_agent_contract user=postgres password=local-contract-only'
        for _ in range(100):
            try:
                with psycopg.connect(dsn, connect_timeout=1, autocommit=True) as connection:
                    connection.execute("CREATE ROLE fixture_cleanup LOGIN PASSWORD 'local-cleanup-only'")
                    connection.execute("CREATE ROLE fixture_observer LOGIN PASSWORD 'local-observer-only'")
                    break
            except psycopg.OperationalError:
                time.sleep(0.2)
        else:
            pytest.fail('owned PostgreSQL container did not become ready')
        yield dsn
    finally:
        subprocess.run(['docker', 'rm', '--force', '--volumes', name], check=True, stdout=subprocess.DEVNULL)


SCHEMA = '''
CREATE SCHEMA honua;
CREATE SCHEMA honua_data;
CREATE TABLE honua.services (service_name text PRIMARY KEY, description text NOT NULL);
CREATE TABLE honua.layers (layer_id int PRIMARY KEY, table_schema text NOT NULL, table_name text NOT NULL);
CREATE TABLE honua.service_layers (service_name text REFERENCES honua.services ON DELETE CASCADE,
    layer_id int REFERENCES honua.layers ON DELETE CASCADE, PRIMARY KEY (service_name, layer_id));
CREATE TABLE honua.layer_fields (layer_id int REFERENCES honua.layers ON DELETE CASCADE, field_name text);
CREATE TABLE honua.features (layer_id int, attributes jsonb);
CREATE TABLE honua.metadata_v2_snapshots (environment text, revision bigint, schema_version text,
    api_version text, document jsonb, etag text, generated_at timestamptz, created_at timestamptz DEFAULT now(),
    PRIMARY KEY (environment, revision));
CREATE TABLE honua.metadata_v2_current (environment text PRIMARY KEY, revision bigint, etag text,
    activated_at timestamptz DEFAULT now(), FOREIGN KEY (environment, revision)
    REFERENCES honua.metadata_v2_snapshots ON DELETE RESTRICT);
CREATE TABLE honua.metadata_v2_services_idx (environment text, revision bigint, service_id text,
    FOREIGN KEY (environment, revision) REFERENCES honua.metadata_v2_snapshots ON DELETE CASCADE);
CREATE TABLE honua_data.shared (id int);
INSERT INTO honua_data.shared VALUES (7);
INSERT INTO honua.services VALUES ('shared', 'preserve this');
INSERT INTO honua.layers VALUES (1, 'honua_data', 'shared');
INSERT INTO honua.service_layers VALUES ('shared', 1);
INSERT INTO honua.layer_fields VALUES (1, 'id');
INSERT INTO honua.features VALUES (1, '{"source_id":"shared"}');
INSERT INTO honua.metadata_v2_snapshots VALUES ('test', 1, 'v2', 'v1', '{"shared":true}', 'baseline', now(), now());
INSERT INTO honua.metadata_v2_current VALUES ('test', 1, 'baseline', now());
INSERT INTO honua.metadata_v2_services_idx VALUES ('test', 1, 'shared');
GRANT USAGE ON SCHEMA honua, honua_data TO fixture_observer;
GRANT SELECT ON ALL TABLES IN SCHEMA honua, honua_data TO fixture_observer;
ALTER DEFAULT PRIVILEGES IN SCHEMA honua, honua_data GRANT SELECT ON TABLES TO fixture_observer;
'''


@pytest.fixture
def fixture_db(postgres, monkeypatch):
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from psycopg.rows import dict_row
    name = 'honua_agent_'+uuid4().hex[:12]
    base = conninfo_to_dict(postgres)
    with psycopg.connect(postgres, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {} OWNER fixture_cleanup').format(sql.Identifier(name)))
    base['dbname'] = name
    admin_dsn = make_conninfo(**base)
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute('SET ROLE fixture_cleanup')
        admin.execute(SCHEMA)
        for table in FixtureDatabase.SIDECARS:
            if table != 'metadata_v2_services_idx':
                admin.execute(sql.SQL('''CREATE TABLE {} (environment text, revision bigint, id text,
                    FOREIGN KEY (environment, revision) REFERENCES honua.metadata_v2_snapshots ON DELETE CASCADE)''')
                    .format(sql.Identifier('honua', table)))
                admin.execute(sql.SQL('GRANT SELECT ON {} TO fixture_observer').format(sql.Identifier('honua', table)))
    for kind, password in [('cleanup', 'local-cleanup-only'), ('observer', 'local-observer-only')]:
        settings = dict(base, user='fixture_'+kind, password=password)
        monkeypatch.setenv('FIXTURE_'+kind.upper()+'_DSN', make_conninfo(**settings))
    with psycopg.connect(os.environ['FIXTURE_OBSERVER_DSN'], row_factory=dict_row) as observer:
        identity = observer.execute('''SELECT current_database() AS database,
            (SELECT oid::bigint FROM pg_database WHERE datname=current_database()) AS "databaseOid",
            inet_server_addr()::text AS "serverAddress", inet_server_port() AS "serverPort"''').fetchone()
    config = {**identity, 'target': 'local-docker', 'metadataEnvironment': 'test',
              'cleanupRole': 'fixture_cleanup', 'observerRole': 'fixture_observer',
              'cleanupDsnEnv': 'FIXTURE_CLEANUP_DSN', 'observerDsnEnv': 'FIXTURE_OBSERVER_DSN',
              'candidateKey': 'sha256:'+'a'*64}
    database = FixtureDatabase(config)
    yield database, admin_dsn
    with psycopg.connect(postgres, autocommit=True) as admin:
        admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


def seed_owned(fixture_db):
    import psycopg
    from psycopg import sql
    database, admin_dsn = fixture_db
    baseline = database.baseline()
    bindings = {'startingState': 'published-town', 'layers': {}}
    with psycopg.connect(admin_dsn) as connection:
        connection.execute('SET ROLE fixture_cleanup')
        for index, name in enumerate(('parcels', 'flood_zones', 'shelters')):
            table, service, layer, revision = 'observed_'+name, 'case-'+name.replace('_', '-'), index+10, index+2
            connection.execute(sql.SQL('CREATE TABLE {} (id int)').format(sql.Identifier('honua_data', table)))
            connection.execute('INSERT INTO honua.services VALUES (%s, %s)', (service, 'owned'))
            connection.execute('INSERT INTO honua.layers VALUES (%s, %s, %s)', (layer, 'honua_data', table))
            connection.execute('INSERT INTO honua.service_layers VALUES (%s, %s)', (service, layer))
            connection.execute('INSERT INTO honua.layer_fields VALUES (%s, %s)', (layer, 'id'))
            connection.execute('INSERT INTO honua.features VALUES (%s, %s::jsonb)', (layer, '{"owned":true}'))
            connection.execute('''INSERT INTO honua.metadata_v2_snapshots VALUES
                ('test', %s, 'v2', 'v1', '{"case":true}', %s, now(), now())''', (revision, service))
            connection.execute('INSERT INTO honua.metadata_v2_services_idx VALUES (%s,%s,%s)', ('test', revision, service))
            connection.execute('UPDATE honua.metadata_v2_current SET revision=%s, etag=%s WHERE environment=%s',
                               (revision, service, 'test'))
            bindings['layers'][name] = {'schema': 'honua_data', 'table': table, 'service': service, 'layerId': layer}
    return database.ownership(baseline, bindings)


def callbacks(record):
    retained, invalidated = {}, []
    def authorize(actual):
        assert actual == record
    def retain(kind, value):
        assert kind not in retained
        retained[kind] = copy.deepcopy(value)
        return {'sha256': canonical_digest(value)}
    return authorize, retain, invalidated.extend, retained, invalidated


def test_real_transaction_removes_only_owned_data_and_independent_observer_proves_absence(fixture_db):
    database, _ = fixture_db
    record = seed_owned(fixture_db)
    authorize, retain, invalidate, retained, invalidated = callbacks(record)
    evidence = database.cleanup(record, authorize=authorize, retain=retain, invalidate=invalidate)
    assert evidence['resourceIds'] == []
    assert evidence['observerPrincipal'] != evidence['cleanupPrincipal']
    assert set(retained) == {'intent', 'observation'}
    assert invalidated == record['services']
    assert database.baseline()['snapshot'] == record['baseline']['snapshot']


def test_drop_dependency_rolls_back_all_prior_metadata_and_data_deletions(fixture_db):
    import psycopg
    database, dsn = fixture_db
    record = seed_owned(fixture_db)
    with psycopg.connect(dsn) as connection:
        connection.execute('CREATE VIEW public.protected_view AS SELECT * FROM honua_data.observed_shelters')
    authorize, retain, invalidate, retained, _ = callbacks(record)
    with pytest.raises(psycopg.errors.DependentObjectsStillExist):
        database.cleanup(record, authorize=authorize, retain=retain, invalidate=invalidate)
    assert database.baseline()['snapshot'] == record['prepared']
    assert set(retained) == {'intent'}


@pytest.mark.parametrize('change', ['shared-row', 'replace-owned-relation', 'new-revision'])
def test_changed_ownership_or_concurrent_mutation_is_preserved_and_blocks_cleanup(fixture_db, change):
    import psycopg
    database, dsn = fixture_db
    record = seed_owned(fixture_db)
    with psycopg.connect(dsn) as connection:
        connection.execute('SET ROLE fixture_cleanup')
        if change == 'shared-row':
            connection.execute("UPDATE honua.services SET description='changed' WHERE service_name='shared'")
        elif change == 'replace-owned-relation':
            connection.execute('DROP TABLE honua_data.observed_shelters')
            connection.execute('CREATE TABLE honua_data.observed_shelters (id int)')
        else:
            connection.execute('''INSERT INTO honua.metadata_v2_snapshots VALUES
                ('test', 99, 'v2', 'v1', '{}', 'unrelated', now(), now())''')
    before_cleanup = database.baseline()['snapshot']
    authorize, retain, invalidate, retained, _ = callbacks(record)
    with pytest.raises(PackError, match='state or ownership changed'):
        database.cleanup(record, authorize=authorize, retain=retain, invalidate=invalidate)
    assert database.baseline()['snapshot'] == before_cleanup
    assert set(retained) == {'intent'}


@pytest.mark.parametrize('parent', ['layer', 'service', 'snapshot', 'composite'])
def test_unknown_cascading_reference_cannot_erase_an_unowned_child(fixture_db, parent):
    import psycopg
    database, dsn = fixture_db
    record = seed_owned(fixture_db)
    with psycopg.connect(dsn) as connection:
        if parent == 'layer':
            connection.execute('CREATE TABLE public.external_reference (layer_id int REFERENCES honua.layers ON DELETE CASCADE)')
            values = (10,)
        elif parent == 'service':
            connection.execute('CREATE TABLE public.external_reference (service_name text REFERENCES honua.services ON DELETE CASCADE)')
            values = ('case-parcels',)
        elif parent == 'snapshot':
            connection.execute('''CREATE TABLE public.external_reference (environment text, revision bigint,
                FOREIGN KEY (environment, revision) REFERENCES honua.metadata_v2_snapshots ON DELETE CASCADE)''')
            values = ('test', 2)
        else:
            connection.execute('ALTER TABLE honua.layers ADD CONSTRAINT composite_identity UNIQUE (layer_id, table_name)')
            connection.execute('''CREATE TABLE public.external_reference (layer_id int, table_name text,
                FOREIGN KEY (layer_id, table_name) REFERENCES honua.layers(layer_id, table_name) ON DELETE CASCADE)''')
            values = (10, 'observed_parcels')
        connection.execute('GRANT SELECT ON public.external_reference TO fixture_cleanup')
        connection.execute('INSERT INTO public.external_reference VALUES ('+', '.join(['%s']*len(values))+')', values)
    authorize, retain, invalidate, retained, _ = callbacks(record)
    with pytest.raises(PackError, match='unowned dependent row|unsupported .*foreign key'):
        database.cleanup(record, authorize=authorize, retain=retain, invalidate=invalidate)
    assert database.baseline()['snapshot'] == record['prepared']
    with psycopg.connect(dsn) as connection:
        assert connection.execute('SELECT count(*) FROM public.external_reference').fetchone()[0] == 1


def test_actual_observer_session_cannot_mutate(fixture_db):
    import psycopg
    database, _ = fixture_db
    with pytest.raises((psycopg.errors.ReadOnlySqlTransaction, psycopg.errors.InsufficientPrivilege)):
        with database.connection() as (connection, _):
            connection.execute('DELETE FROM honua.services')


def test_wrong_database_identity_refused_before_cleanup(fixture_db):
    database, _ = fixture_db
    changed = dict(database.config, databaseOid=database.config['databaseOid']+1)
    with pytest.raises(PackError, match='isolated target'):
        FixtureDatabase(changed).baseline()
