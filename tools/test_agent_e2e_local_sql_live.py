"""Live PostGIS contract for controller SQL; opt in with HONUA_AGENT_POSTGRES_TESTS=1.

Runs the runner's own code path (ControllerRecorder.table_rows/table_inventory -> LocalRuntime.sql_read ->
generated SQL -> LocalRuntime.psql as corpus_reader) against a real PostGIS container located exactly as in a
case stack (compose project + service labels). Scenarios cannot send SQL: every structured refusal in
certification/agent-e2e/fixtures/sql-confinement.v1.json must be refused before any row read, allowed reads
must return exactly their rows, and the raw database battery (defense in depth) must be refused with no
state change. DOCKER_HOST may point at a remote engine (e.g. ssh://honua-eval-3).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

import agent_e2e_local_runner as runner
from agent_e2e_local_api import Stack
from agent_e2e_local_recorder import ControllerRecorder, EvidenceVault

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'certification/agent-e2e/fixtures/sql-confinement.v1.json').read_text())
RESULTS = []


@pytest.fixture(scope='module')
def database(tmp_path_factory):
    if os.environ.get('HONUA_AGENT_POSTGRES_TESTS') != '1':
        pytest.skip('opt-in live PostgreSQL confinement checks')
    assert shutil.which('docker'), 'Docker is required for requested live PostgreSQL checks'
    reference = os.environ.get('HONUA_AGENT_POSTGRES_TEST_IMAGE',
        'postgis/postgis@sha256:b4466ea2ba7e3af54fcbba996d45325720f4b3ecb4f43f99b2f41367dc196f23')
    assert re.fullmatch(r'postgis/postgis@sha256:[0-9a-f]{64}', reference), 'use the reviewed immutable PostGIS image'
    project = 'corpus-sqlprobe-' + uuid.uuid4().hex[:10]
    name = project + '-db-1'
    subprocess.run(['docker', 'run', '--detach', '--pull', 'missing', '--name', name,
                    '--label', f'com.docker.compose.project={project}', '--label', 'com.docker.compose.service=db',
                    '--memory', '512m', '--cpus', '1', '--pids-limit', '200',
                    '--env', 'POSTGRES_USER=honua', '--env', 'POSTGRES_PASSWORD=local-contract-only',
                    '--env', 'POSTGRES_DB=honua', reference], check=True, stdout=subprocess.DEVNULL)
    try:
        runtime = runner.LocalRuntime(None, model='m', release_id='r')
        workdir = tmp_path_factory.mktemp('sqlprobe')
        stack = Stack('http://127.0.0.1:1', 'k', None, project, 'none', str(workdir), 'sha256:' + 'a' * 64, {})
        for _ in range(150):
            if subprocess.run(['docker', 'exec', name, 'pg_isready', '-U', 'honua', '-d', 'honua', '-h', '127.0.0.1'],
                              capture_output=True).returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail('PostGIS container did not become ready')
        time.sleep(2)
        # One owner session avoids repeated remote Docker/SSH handshakes; the
        # fixture statements are all transactional and ON_ERROR_STOP is set.
        code, _ = runtime.psql(stack, ';\n'.join(FIXTURE['seed']), read_only=False)
        assert code == 0, 'could not create the confinement fixture'
        runtime._ensure_reader(stack)
        recorder = ControllerRecorder(base_url='http://127.0.0.1:1', workdir=workdir, candidate_digest='sha256:' + 'a' * 64,
                                      attempt_id=project, principals={'controller': (None, None)}, vault=EvidenceVault(project),
                                      sql_executor=lambda request: runtime.sql_read(stack, request))
        recorder.set_phase('observe')
        yield runtime, stack, recorder
    finally:
        subprocess.run(['docker', 'rm', '--force', '--volumes', name], check=False, stdout=subprocess.DEVNULL)
        out = os.environ.get('HONUA_SQL_LIVE_RESULTS')
        if out:
            Path(out).write_text(json.dumps(RESULTS, indent=2))


STATE_SQL = ("SELECT json_build_object("
             "'tables', (SELECT json_agg(c.relname ORDER BY c.relname) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
             "WHERE n.nspname = 'honua_data'), "
             "'shelters', (SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM honua_data.shelters t), "
             "'guarded', (SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM honua_data.guarded t), "
             "'typed', (SELECT md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) FROM honua_data.typed t), "
             "'seq', (SELECT last_value FROM honua_data.counter), "
             "'functions', (SELECT json_agg(p.oid::regprocedure::text ORDER BY p.oid::regprocedure::text) FROM pg_proc p "
             "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname IN ('honua_data', 'public') "
             "AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')), "
             "'roles', (SELECT json_agg(rolname ORDER BY rolname) FROM pg_roles WHERE rolname NOT LIKE 'pg_%'), "
             "'largeObjects', (SELECT count(*) FROM pg_largeobject_metadata))")


def state(runtime, stack):
    code, out = runtime.psql(stack, STATE_SQL, read_only=False)
    assert code == 0
    return json.loads(out)


def test_reader_role_is_restricted(database):
    runtime, stack, recorder = database
    recorder.table_inventory('inventory-probe', 'honua_data')   # creates corpus_reader through the runner's path
    code, out = runtime.psql(stack, "SELECT json_build_object('super', rolsuper, 'createrole', rolcreaterole, 'createdb', "
                                    "rolcreatedb, 'inherit', rolinherit, 'bypassrls', rolbypassrls, 'config', rolconfig) "
                                    "FROM pg_roles WHERE rolname = 'corpus_reader'", read_only=False)
    role = json.loads(out)
    assert code == 0 and role['super'] is False and role['createrole'] is False and role['createdb'] is False
    assert role['inherit'] is True and role['bypassrls'] is False
    assert {'default_transaction_read_only=on', 'statement_timeout=15s'} <= set(role['config'])
    assert any(item.startswith('search_path=pg_catalog') for item in role['config'])
    RESULTS.append({'check': 'corpus_reader role', 'result': role})


@pytest.mark.parametrize('case', FIXTURE['allowed'], ids=lambda c: c['name'])
def test_allowed_structured_reads_return_exact_rows(database, case):
    runtime, stack, recorder = database
    before = state(runtime, stack)
    step = 'allowed-' + re.sub(r'[^a-z0-9]+', '-', case['name'])[:60]
    request = case['request']
    rows = (recorder.table_inventory(step, request['schema']) if request['op'] == 'inventory'
            else recorder.table_rows(step, request['schema'], request['table'], request['columns']))
    key = 'table_name' if request['op'] == 'inventory' else 'source_id'
    assert sorted(rows, key=lambda r: r[key]) == sorted(case['rows'], key=lambda r: r[key])
    assert state(runtime, stack) == before
    RESULTS.append({'allowed': case['name'], 'rows': len(rows), 'exact': True, 'stateUnchanged': True})


@pytest.mark.parametrize('case', FIXTURE['structuredRefusals'], ids=lambda c: c['name'])
def test_structured_api_refuses_user_code_paths(database, case):
    runtime, stack, recorder = database
    before = state(runtime, stack)
    step = 'refused-' + re.sub(r'[^a-z0-9]+', '-', case['name'])[:60]
    request = case['request']
    with pytest.raises(ValueError):
        if request.get('op') == 'table_rows':
            recorder.table_rows(step, request.get('schema'), request.get('table'), request.get('columns'))
        else:
            recorder._structured(step, request)
    assert state(runtime, stack) == before
    RESULTS.append({'structuredRefusal': case['name'], 'refused': True, 'stateUnchanged': True})


def test_geometry_read_refused_once_a_foreign_overload_exists(database):
    runtime, stack, recorder = database
    code, _ = runtime.psql(stack, FIXTURE['geometryOverload']['seed'], read_only=False)
    assert code == 0
    before = state(runtime, stack)
    request = FIXTURE['geometryOverload']['request']
    with pytest.raises(ValueError):
        recorder.table_rows('refused-geometry-overload', request['schema'], request['table'], request['columns'])
    assert state(runtime, stack) == before
    RESULTS.append({'structuredRefusal': 'foreign st_asgeojson overload', 'refused': True, 'stateUnchanged': True})


@pytest.mark.parametrize('case', FIXTURE['databaseBattery'], ids=lambda c: c['name'])
def test_raw_database_battery_as_reader(database, case):
    runtime, stack, _recorder = database
    before = state(runtime, stack)
    code, _ = runtime.psql(stack, case['sql'], read_only=True)
    outcome = {'raw': case['name'], 'database': 'refused' if code != 0 else 'answered'}
    after = state(runtime, stack)
    outcome['stateUnchanged'] = before == after
    RESULTS.append(outcome)
    assert before == after, f'{case["name"]} changed state'
    if case['name'] not in FIXTURE['databaseMayAnswer']:
        assert outcome['database'] == 'refused', case['name']


@pytest.mark.parametrize('partitioned', [False, True], ids=['inherited-foreign-child', 'foreign-default-partition'])
def test_foreign_descendants_refused_without_executing_remote_side_effect(database, partitioned):
    runtime, stack, recorder = database
    # The remote endpoint is the same disposable server, through postgres_fdw.
    # Grants intentionally reproduce the permissive conditions in the review.
    setup = """
    CREATE EXTENSION IF NOT EXISTS postgres_fdw;
    CREATE EXTENSION IF NOT EXISTS dblink;
    CREATE TABLE IF NOT EXISTS honua_data.review_marker (x integer NOT NULL);
    TRUNCATE honua_data.review_marker;
    INSERT INTO honua_data.review_marker VALUES (0);
    CREATE OR REPLACE FUNCTION public.review_side_effect() RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER AS $$
    BEGIN
      PERFORM public.dblink_exec('host=127.0.0.1 port=5432 dbname=honua user=honua password=local-contract-only',
                                 'UPDATE honua_data.review_marker SET x = x + 1');
      RETURN 1;
    END $$;
    CREATE OR REPLACE VIEW public.review_remote AS SELECT public.review_side_effect() AS x;
    CREATE SERVER IF NOT EXISTS review_loopback FOREIGN DATA WRAPPER postgres_fdw
      OPTIONS (host '127.0.0.1', port '5432', dbname 'honua');
    CREATE USER MAPPING IF NOT EXISTS FOR corpus_reader SERVER review_loopback
      OPTIONS (user 'honua', password 'local-contract-only', password_required 'false');
    GRANT USAGE ON FOREIGN SERVER review_loopback TO corpus_reader;
    """
    runtime._ensure_reader(stack)
    code, _ = runtime.psql(stack, setup, read_only=False)
    assert code == 0
    if partitioned:
        ddl = ("CREATE TABLE honua_data.review_partitioned (x integer) PARTITION BY RANGE (x); "
               "CREATE FOREIGN TABLE honua_data.review_partition_foreign (x integer) "
               "SERVER review_loopback OPTIONS (schema_name 'public', table_name 'review_remote'); "
               "ALTER TABLE honua_data.review_partitioned ATTACH PARTITION honua_data.review_partition_foreign DEFAULT")
        parent = 'review_partitioned'
    else:
        ddl = ("CREATE TABLE honua_data.review_parent (x integer); "
               "CREATE FOREIGN TABLE honua_data.review_inherited_foreign (x integer) "
               "INHERITS (honua_data.review_parent) SERVER review_loopback "
               "OPTIONS (schema_name 'public', table_name 'review_remote')")
        parent = 'review_parent'
    code, _ = runtime.psql(stack, ddl, read_only=False)
    assert code == 0
    def marker():
        code, out = runtime.psql(stack, 'SELECT pg_catalog.json_agg(x) FROM honua_data.review_marker', read_only=False)
        assert code == 0
        return json.loads(out)
    # Prove the fixture can cause a persistent write even through the reader's
    # read-only session; this is a test-only direct read of the foreign child.
    child = 'review_partition_foreign' if partitioned else 'review_inherited_foreign'
    code, _ = runtime.psql(stack, f'SELECT x FROM honua_data.{child}', read_only=True)
    assert code == 0
    assert marker() == [1]
    code, _ = runtime.psql(stack, 'UPDATE honua_data.review_marker SET x = 0', read_only=False)
    assert code == 0
    before = marker()
    assert before == [0]
    with pytest.raises(ValueError):
        recorder.table_rows('foreign-descendant-' + ('partition' if partitioned else 'inheritance'),
                            'honua_data', parent, ['x'])
    assert marker() == before
    code, _ = runtime.psql(stack, 'DROP USER MAPPING FOR corpus_reader SERVER review_loopback; '
                          'REVOKE USAGE ON FOREIGN SERVER review_loopback FROM corpus_reader', read_only=False)
    assert code == 0


def test_preexisting_reader_with_unreviewed_membership_is_refused_without_repair(database):
    runtime, stack, recorder = database
    runner.LocalRuntime(None, model='m', release_id='r')._ensure_reader(stack)
    code, _ = runtime.psql(stack, 'CREATE ROLE unexpected_reader_grant; GRANT unexpected_reader_grant TO corpus_reader', read_only=False)
    assert code == 0
    probe = runner.LocalRuntime(None, model='m', release_id='r')
    try:
        with pytest.raises(RuntimeError, match='read-only controller database role'):
            probe._ensure_reader(stack)
        code, raw = runtime.psql(stack, "SELECT pg_catalog.pg_has_role('corpus_reader', 'unexpected_reader_grant', 'MEMBER')", read_only=False)
        assert code == 0 and raw.strip() == b't'
    finally:
        code, _ = runtime.psql(stack, 'REVOKE unexpected_reader_grant FROM corpus_reader; DROP ROLE unexpected_reader_grant', read_only=False)
        assert code == 0


@pytest.mark.parametrize('kind', ['foreign', 'materialized'])
def test_direct_unsafe_relations_refused_without_state_change(database, kind):
    runtime, stack, recorder = database
    if kind == 'foreign':
        sql = ("CREATE EXTENSION IF NOT EXISTS postgres_fdw; "
               "CREATE SERVER IF NOT EXISTS direct_probe FOREIGN DATA WRAPPER postgres_fdw OPTIONS (dbname 'honua'); "
               "CREATE FOREIGN TABLE honua_data.direct_foreign (x integer) SERVER direct_probe OPTIONS (table_name 'missing')")
        table = 'direct_foreign'
    else:
        sql = 'CREATE MATERIALIZED VIEW honua_data.direct_materialized AS SELECT 7 AS x'
        table = 'direct_materialized'
    code, _ = runtime.psql(stack, sql, read_only=False)
    assert code == 0
    before = state(runtime, stack)
    with pytest.raises(ValueError):
        recorder.table_rows('direct-unsafe-' + kind, 'honua_data', table, ['x'])
    assert state(runtime, stack) == before


@pytest.mark.parametrize('setup,cleanup', [
    ('ALTER ROLE corpus_reader BYPASSRLS', 'ALTER ROLE corpus_reader NOBYPASSRLS'),
    ('GRANT SELECT ON honua_data.shelters TO corpus_reader', 'REVOKE SELECT ON honua_data.shelters FROM corpus_reader'),
    ('CREATE TABLE honua_data.reader_owned (x int); ALTER TABLE honua_data.reader_owned OWNER TO corpus_reader',
     'DROP TABLE honua_data.reader_owned'),
    ("CREATE EXTENSION IF NOT EXISTS postgres_fdw; CREATE SERVER mapping_probe FOREIGN DATA WRAPPER postgres_fdw; "
     "CREATE USER MAPPING FOR corpus_reader SERVER mapping_probe", 'DROP SERVER mapping_probe CASCADE'),
])
def test_existing_reader_rejects_extra_authority_without_repair(database, setup, cleanup):
    runtime, stack, _ = database
    runner.LocalRuntime(None, model='m', release_id='r')._ensure_reader(stack)
    code, _ = runtime.psql(stack, setup, read_only=False)
    assert code == 0
    try:
        with pytest.raises(RuntimeError):
            runner.LocalRuntime(None, model='m', release_id='r')._ensure_reader(stack)
        # The dirty role remains dirty; a failed check must not repair it.
        with pytest.raises(RuntimeError):
            runner.LocalRuntime(None, model='m', release_id='r')._ensure_reader(stack)
    finally:
        code, _ = runtime.psql(stack, cleanup, read_only=False)
        assert code == 0
    runner.LocalRuntime(None, model='m', release_id='r')._ensure_reader(stack)
