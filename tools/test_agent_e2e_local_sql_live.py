"""Live PostGIS contract for controller SQL confinement; opt in with HONUA_AGENT_POSTGRES_TESTS=1.

Runs the runner's own code path (LocalRuntime.psql / sql_read / sql_allowlisted) against a real
PostGIS container located exactly as in a case stack (compose project + service labels). Every attack
in certification/agent-e2e/fixtures/sql-confinement.v1.json must be refused by the parser and, with the
parser bypassed, by the database as corpus_reader (wrapped as sql_read sends it, and bare); no attack may
change state. DOCKER_HOST may point at a remote engine (e.g. ssh://honua-eval-3).
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

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'certification/agent-e2e/fixtures/sql-confinement.v1.json').read_text())
RESULTS = []


@pytest.fixture(scope='module')
def database():
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
        stack = Stack('http://127.0.0.1:1', 'k', None, project, 'none', '/tmp', 'sha256:' + 'a' * 64, {})
        for _ in range(150):
            if subprocess.run(['docker', 'exec', name, 'pg_isready', '-U', 'honua', '-d', 'honua', '-h', '127.0.0.1'],
                              capture_output=True).returncode == 0:
                break
            time.sleep(1)
        else:
            pytest.fail('PostGIS container did not become ready')
        time.sleep(2)
        for statement in FIXTURE['seed']:
            code, _ = runtime.psql(stack, statement, read_only=False)
            assert code == 0, statement
        yield runtime, stack
    finally:
        subprocess.run(['docker', 'rm', '--force', '--volumes', name], check=False, stdout=subprocess.DEVNULL)
        out = os.environ.get('HONUA_SQL_LIVE_RESULTS')
        if out:
            Path(out).write_text(json.dumps(RESULTS, indent=2))


def state(runtime, stack):
    code, out = runtime.psql(stack, "SELECT json_build_object('rows', (SELECT count(*) FROM honua_data.shelters), "
                                    "'sum', (SELECT coalesce(sum(capacity), 0) FROM honua_data.shelters), "
                                    "'seq', (SELECT last_value FROM honua_data.counter), "
                                    "'tables', (SELECT json_agg(table_name ORDER BY table_name) FROM information_schema.tables "
                                    "WHERE table_schema = 'honua_data'), "
                                    "'roles', (SELECT json_agg(rolname ORDER BY rolname) FROM pg_roles WHERE rolname NOT LIKE 'pg_%'))",
                             read_only=False)
    assert code == 0
    return json.loads(out)


def test_reader_role_is_restricted(database):
    runtime, stack = database
    runtime.sql_read(stack, 'SELECT count(*) AS n FROM pg_catalog.pg_roles')   # creates corpus_reader via the runner
    code, out = runtime.psql(stack, "SELECT json_build_object('super', rolsuper, 'createrole', rolcreaterole, "
                                    "'createdb', rolcreatedb, 'config', rolconfig) FROM pg_roles WHERE rolname = 'corpus_reader'",
                             read_only=False)
    role = json.loads(out)
    assert code == 0 and role['super'] is False and role['createrole'] is False and role['createdb'] is False
    assert 'default_transaction_read_only=on' in role['config'] and 'statement_timeout=15s' in role['config']
    assert any(item.startswith('search_path=') for item in role['config'])
    code, out = runtime.psql(stack, "SELECT rolinherit FROM pg_catalog.pg_roles WHERE rolname = 'corpus_reader'", read_only=False)
    assert out.strip() == b't'
    RESULTS.append({'check': 'corpus_reader role', 'result': role})


@pytest.mark.parametrize('case', FIXTURE['allowed'], ids=lambda c: c['name'])
def test_allowed_reads_return_rows(database, case):
    runtime, stack = database
    status, raw = runtime.sql_read(stack, case['sql'])
    rows = json.loads(raw)
    RESULTS.append({'allowed': case['name'], 'status': status, 'rows': len(rows)})
    assert status == 200 and len(rows) == case['rows']


@pytest.mark.parametrize('case', FIXTURE['attacks'], ids=lambda c: c['name'])
def test_attacks_are_refused_by_parser_and_database(database, case):
    runtime, stack = database
    before = state(runtime, stack)
    with pytest.raises(ValueError):
        runner.sql_allowlisted(case['sql'])
    with pytest.raises(ValueError):
        runtime.sql_read(stack, case['sql'])
    outcome = {'attack': case['name'], 'parser': 'refused'}
    # Bypass the parser: the database alone, as corpus_reader, wrapped the way sql_read sends it.
    wrapped = f"SELECT coalesce(json_agg(t), '[]'::json) FROM ({case['sql']}) AS t"
    started = time.monotonic()
    code, out = runtime.psql(stack, wrapped, read_only=True)
    outcome['databaseWrapped'] = 'refused' if code != 0 else 'answered'
    outcome['wrappedSeconds'] = round(time.monotonic() - started, 1)
    if case.get('bare', True):
        code_bare, _ = runtime.psql(stack, case['sql'], read_only=True)
        outcome['databaseBare'] = 'refused' if code_bare != 0 else 'answered'
    after = state(runtime, stack)
    outcome['stateUnchanged'] = before == after
    RESULTS.append(outcome)
    assert before == after, f'{case["name"]} changed state: {before} -> {after}'
    if case['name'] not in FIXTURE['databaseMayAnswer']:
        assert outcome['databaseWrapped'] == 'refused', case['name']
        if case.get('bare', True):
            assert outcome['databaseBare'] == 'refused', case['name']
