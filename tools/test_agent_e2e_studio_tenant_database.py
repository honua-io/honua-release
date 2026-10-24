"""Actual local SQL read-only/protected-row controls, not native tenant qualification."""
import copy

import pytest

from agent_e2e_pack import PackError
from test_agent_e2e_studio_database import studio_db, seed
from test_agent_e2e_fixture_database import fixture_db, postgres


@pytest.fixture
def private_tenant_db(studio_db):
    import psycopg
    from psycopg import sql
    database, dsn = studio_db
    _, fixture = seed(database, dsn)
    # This bounded SQL fixture exercises the observer contract. It does not
    # stand in for a product migration, signed IdP identity or live campaign.
    with psycopg.connect(dsn) as connection:
        connection.execute('SET ROLE fixture_cleanup')
        for table in ('studio_content_items', 'studio_package_drafts', 'studio_content_versions'):
            connection.execute(sql.SQL('ALTER TABLE {} ADD COLUMN tenant_id text').format(sql.Identifier('honua', table)))
            connection.execute(sql.SQL('UPDATE {} SET tenant_id = %s WHERE item_id = %s').format(sql.Identifier('honua', table)),
                ('actual-sql-contract-tenant-b', fixture['itemId']))
    return database, dsn, fixture, database.baseline()


def observe(value, authorize):
    database, _, fixture, baseline = value
    return database.observe_private_tenant_source(baseline, item_id=fixture['itemId'],
        version_id=fixture['versionId'], authorize=authorize)


def test_actual_read_only_sql_proves_private_metadata_without_adopting_or_changing_rows(private_tenant_db):
    database, _, fixture, baseline = private_tenant_db
    calls = []
    proof = observe(private_tenant_db, lambda: calls.append('live-fence'))
    assert proof['source']['itemId'] == fixture['itemId']
    assert proof['source']['tenantId'] == 'actual-sql-contract-tenant-b'
    assert proof['source']['envelope'] == fixture['initialVersion']['envelope']
    assert proof['observerPrincipal'] == database.config['observerRole']
    assert calls == ['live-fence', 'live-fence']
    assert database.baseline()['snapshot'] == baseline['snapshot']
    assert 'ownership' not in proof and 'cleanup' not in proof


@pytest.mark.parametrize('change', ['tenant', 'publication', 'content', 'draft'])
def test_actual_sql_rejects_changed_protected_tenant_pointer_content_or_related_draft(private_tenant_db, change):
    import psycopg
    from psycopg.types.json import Jsonb
    _, dsn, fixture, _ = private_tenant_db
    with psycopg.connect(dsn) as connection:
        connection.execute('SET ROLE fixture_cleanup')
        if change == 'tenant':
            connection.execute('UPDATE honua.studio_content_versions SET tenant_id=%s WHERE version_id=%s',
                ('different-tenant', fixture['versionId']))
        elif change == 'publication':
            connection.execute('UPDATE honua.studio_content_items SET published_version_id=%s WHERE item_id=%s',
                (fixture['versionId'], fixture['itemId']))
        else:
            table, column, key = ('studio_content_versions', 'version_id', fixture['versionId']) if change == 'content' else (
                'studio_package_drafts', 'draft_id', fixture['draftId'])
            from psycopg import sql
            connection.execute(sql.SQL('UPDATE {} SET envelope=%s WHERE {}=%s').format(
                sql.Identifier('honua', table), sql.Identifier(column)),
                (Jsonb({'family': 'map', 'body': {'changed': True}}), key))
    with pytest.raises(PackError): observe(private_tenant_db, lambda: None)


def test_original_sql_baseline_without_actual_resource_tenant_cannot_invent_it(private_tenant_db):
    database, _, fixture, baseline = private_tenant_db
    missing = copy.deepcopy(baseline)
    for table in ('studio_content_items', 'studio_content_versions'):
        for row in missing['snapshot']['studio'][table]: row.pop('tenant_id', None)
    with pytest.raises(PackError, match='private tenant'):
        database.observe_private_tenant_source(missing, item_id=fixture['itemId'],
            version_id=fixture['versionId'], authorize=lambda: None)


def test_lost_live_fence_after_sql_read_cannot_emit_a_tenant_proof(private_tenant_db):
    calls = []
    def authorize():
        calls.append('fence')
        if len(calls) == 2: raise PackError('lease lost')
    with pytest.raises(PackError, match='lease lost'): observe(private_tenant_db, authorize)
