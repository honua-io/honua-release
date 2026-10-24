"""Actual SQL role grants and atomic rollback; local TLS fixture, not qualification."""
import sys
from pathlib import Path
from uuid import uuid4
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from fixture_provisioner_postgres_test import tls_fixture, fixture_db, postgres
from test_agent_e2e_studio_database import SCHEMA
from agent_e2e_pack import PackError
import native_bootstrap as bootstrap


def prepare(tls_fixture):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    database, dsn = tls_fixture
    with psycopg.connect(dsn) as connection:
        connection.execute(SCHEMA)
    settings = conninfo_to_dict(dsn)
    config = {'database': database.config['database'], 'appRole': 'postgres', 'databaseHost': settings['host'],
        'cleanupRole': 'native_cleanup_'+uuid4().hex[:12], 'observerRole': 'native_observer_'+uuid4().hex[:12]}
    credentials = {kind: make_conninfo(**dict(settings, user=config[kind+'Role'], password='p'*48, sslmode='verify-full')) for kind in ('cleanup', 'observer')}
    return config, credentials


def test_actual_native_roles_are_nonadmin_observer_readonly_and_cleanup_can_lock_and_delete(tls_fixture):
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    config, credentials = prepare(tls_fixture)
    with psycopg.connect(tls_fixture[1], sslmode='require', autocommit=True, row_factory=dict_row) as connection:
        result = bootstrap.sql_identities(connection, config, credentials, guard=lambda: None)
    assert result['qualification'] is False and result['nativeTables']
    for kind in ('cleanup', 'observer'):
        settings = conninfo_to_dict(credentials[kind]); settings['sslmode'] = 'require'
        with psycopg.connect(make_conninfo(**settings), row_factory=dict_row) as connection:
            flags = connection.execute('SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls FROM pg_roles WHERE rolname=current_user').fetchone()
            assert not any(flags.values())
            connection.execute('SELECT * FROM honua.studio_content_items').fetchall()
            if kind == 'observer':
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    connection.execute('DELETE FROM honua.studio_content_items')
            else:
                connection.execute('LOCK TABLE honua.content_publication_versions IN SHARE ROW EXCLUSIVE MODE')
                connection.execute('DELETE FROM honua.studio_content_items')


def test_lost_native_fence_before_commit_rolls_back_both_new_roles_and_grants(tls_fixture):
    import psycopg
    from psycopg.rows import dict_row
    config, credentials = prepare(tls_fixture)
    calls = []
    def guard():
        calls.append(True)
        if len(calls) == 2:
            raise PackError('native controller fence lost')
    with psycopg.connect(tls_fixture[1], sslmode='require', autocommit=True, row_factory=dict_row) as connection:
        with pytest.raises(PackError, match='fence lost'):
            bootstrap.sql_identities(connection, config, credentials, guard=guard)
        assert connection.execute('SELECT rolname FROM pg_roles WHERE rolname=ANY(%s)', ([config['cleanupRole'], config['observerRole']],)).fetchall() == []


def test_existing_native_sql_role_is_never_reset_or_regranted(tls_fixture):
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
    config, credentials = prepare(tls_fixture)
    with psycopg.connect(tls_fixture[1], sslmode='require', autocommit=True, row_factory=dict_row) as connection:
        connection.execute(sql.SQL('CREATE ROLE {} NOLOGIN').format(sql.Identifier(config['cleanupRole'])))
        with pytest.raises(PackError, match='existing SQL roles'):
            bootstrap.sql_identities(connection, config, credentials, guard=lambda: None)
        assert connection.execute('SELECT rolcanlogin FROM pg_roles WHERE rolname=%s', (config['cleanupRole'],)).fetchone() == {'rolcanlogin': False}
        assert connection.execute('SELECT 1 FROM pg_roles WHERE rolname=%s', (config['observerRole'],)).fetchone() is None
