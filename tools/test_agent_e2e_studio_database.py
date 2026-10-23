"""Actual PostgreSQL Studio rollback/absence; opt in, never live qualification."""
import copy
from uuid import uuid4

import pytest

from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_studio_database import StudioFixtureDatabase
from test_agent_e2e_fixture_database import fixture_db, postgres


SCHEMA = '''
CREATE TABLE honua.studio_content_items (item_id uuid PRIMARY KEY, owner_id text, workspace_id text, package_key text,
    current_version_id uuid, published_version_id uuid);
CREATE TABLE honua.studio_package_drafts (draft_id uuid PRIMARY KEY, item_id uuid REFERENCES honua.studio_content_items ON DELETE CASCADE,
    owner_id text, workspace_id text, package_key text, envelope jsonb);
CREATE TABLE honua.studio_content_versions (version_id uuid PRIMARY KEY, item_id uuid REFERENCES honua.studio_content_items ON DELETE CASCADE,
    source_draft_id uuid, owner_id text, workspace_id text, package_key text, version_number int, content_hash text, envelope jsonb);
CREATE TABLE honua.studio_content_version_dependencies (item_id uuid REFERENCES honua.studio_content_items ON DELETE CASCADE,
    version_id uuid REFERENCES honua.studio_content_versions ON DELETE CASCADE, dependency_ref text);
CREATE TABLE honua.studio_publication_requests (request_id uuid PRIMARY KEY, item_id uuid REFERENCES honua.studio_content_items ON DELETE CASCADE,
    version_id uuid REFERENCES honua.studio_content_versions ON DELETE CASCADE, status text);
CREATE TABLE honua.studio_rollback_requests (request_id uuid PRIMARY KEY, item_id uuid REFERENCES honua.studio_content_items ON DELETE CASCADE,
    target_version_id uuid REFERENCES honua.studio_content_versions ON DELETE CASCADE);
CREATE TABLE honua.saved_map_operation_log_heads (map_id text PRIMARY KEY, head_cursor bigint);
CREATE TABLE honua.saved_map_operations (map_id text REFERENCES honua.saved_map_operation_log_heads ON DELETE CASCADE, server_cursor bigint);
CREATE TABLE honua.saved_map_checkpoint_versions (map_id text REFERENCES honua.saved_map_operation_log_heads ON DELETE CASCADE,
    version_id uuid REFERENCES honua.studio_content_versions ON DELETE CASCADE);
CREATE TABLE honua.studio_map_comment_threads (thread_id uuid PRIMARY KEY, map_id text);
CREATE TABLE honua.studio_map_comment_messages (message_id uuid PRIMARY KEY, thread_id uuid REFERENCES honua.studio_map_comment_threads ON DELETE CASCADE, body text);
CREATE TABLE honua.studio_map_activity_events (event_id uuid PRIMARY KEY, map_id text);
CREATE TABLE honua.content_publication_versions (version_id uuid PRIMARY KEY, source_content_id text, body jsonb);
CREATE TABLE honua.content_publication_routes (publication_id uuid PRIMARY KEY, route_slug text);
CREATE TABLE honua.content_publication_events (event_id uuid PRIMARY KEY, detail text);
CREATE FUNCTION honua.delete_saved_map_operation_log_with_studio_draft() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN DELETE FROM honua.saved_map_operation_log_heads WHERE map_id = OLD.draft_id::text; RETURN OLD; END;
$$;
CREATE TRIGGER trg_delete_saved_map_operation_log_with_studio_draft AFTER DELETE ON honua.studio_package_drafts
FOR EACH ROW EXECUTE FUNCTION honua.delete_saved_map_operation_log_with_studio_draft();
GRANT SELECT ON ALL TABLES IN SCHEMA honua TO fixture_observer;
'''


@pytest.fixture
def studio_db(fixture_db):
    import psycopg
    database, admin_dsn = fixture_db
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        connection.execute('SET ROLE fixture_cleanup')
        connection.execute(SCHEMA)
    return StudioFixtureDatabase(database.config), admin_dsn


def seed(database, dsn, *, ids=None):
    import psycopg
    from psycopg.types.json import Jsonb
    item, draft, version = ids or tuple(str(uuid4()) for _ in range(3))
    namespace, owner = 'ae_studio_fixture', 'native-fixture-requester'
    envelope = {'family': 'map', 'schemaVersion': '1.0', 'body': {'fixture': 'actual-sql-contract-only'}}
    fixture = {'schema': 'honua.agent-e2e.studio-fixture/v1', 'itemId': item, 'draftId': draft, 'versionId': version,
        'namespace': namespace, 'ownerPrincipalId': owner, 'contentHash': 'a'*64, 'initialVersion': {'envelope': envelope}}
    baseline = database.baseline()
    fixture['baselineDigest'] = canonical_digest(baseline)
    with psycopg.connect(dsn) as connection:
        connection.execute('INSERT INTO honua.studio_content_items VALUES (%s,%s,%s,%s,%s,NULL)',
            (item, owner, namespace, namespace, version))
        connection.execute('INSERT INTO honua.studio_package_drafts VALUES (%s,%s,%s,%s,%s,%s)',
            (draft, item, owner, namespace, namespace, Jsonb(envelope)))
        connection.execute('INSERT INTO honua.studio_content_versions VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s)',
            (version, item, draft, owner, namespace, namespace, 'a'*64, Jsonb(envelope)))
    record = database.ownership(baseline, fixture)
    return record, fixture


def hooks(record, *, fail_at=None):
    state = {'authorizations': 0, 'records': {}}
    def authorize(value):
        assert value == record
        state['authorizations'] += 1
        if state['authorizations'] == fail_at:
            raise PackError('lost fenced authority at precommit')
    def retain(kind, value):
        assert kind not in state['records']
        state['records'][kind] = copy.deepcopy(value)
        return {'sha256': canonical_digest(value)}
    return state, authorize, retain


def test_actual_studio_transaction_restores_baseline_and_independent_role_observes_absence(studio_db):
    database, dsn = studio_db
    record, _ = seed(database, dsn)
    state, authorize, retain = hooks(record)
    result = database.cleanup(record, authorize=authorize, retain=retain)
    assert result['resourceIds'] == [] and result['cleanupPrincipal'] != result['observerPrincipal']
    assert state['records']['absence'] == result
    assert database.baseline()['snapshot'] == record['baseline']['snapshot']


def test_actual_saved_map_logs_checkpoints_and_comments_are_owned_children(studio_db):
    import psycopg
    database, dsn = studio_db
    record, fixture = seed(database, dsn)
    with psycopg.connect(dsn) as connection:
        connection.execute('INSERT INTO honua.saved_map_operation_log_heads VALUES (%s,1)', (fixture['draftId'],))
        connection.execute('INSERT INTO honua.saved_map_operations VALUES (%s,1)', (fixture['draftId'],))
        connection.execute('INSERT INTO honua.saved_map_checkpoint_versions VALUES (%s,%s)', (fixture['draftId'], fixture['versionId']))
        thread = str(uuid4())
        connection.execute('INSERT INTO honua.studio_map_comment_threads VALUES (%s,%s)', (thread, fixture['draftId']))
        connection.execute('INSERT INTO honua.studio_map_comment_messages VALUES (%s,%s,%s)', (str(uuid4()), thread, 'owned comment'))
        connection.execute('INSERT INTO honua.studio_map_activity_events VALUES (%s,%s)', (str(uuid4()), fixture['draftId']))
        connection.execute('INSERT INTO honua.studio_publication_requests VALUES (%s,%s,%s,%s)',
            (str(uuid4()), fixture['itemId'], fixture['versionId'], 'accepted'))
    state, authorize, retain = hooks(record)
    assert database.cleanup(record, authorize=authorize, retain=retain)['resourceIds'] == []
    assert database.baseline()['snapshot'] == record['baseline']['snapshot']


def test_actual_precommit_authority_loss_rolls_back_all_studio_deletes(studio_db):
    database, dsn = studio_db
    record, _ = seed(database, dsn)
    before = database.baseline()
    state, authorize, retain = hooks(record, fail_at=4)
    with pytest.raises(PackError, match='precommit'):
        database.cleanup(record, authorize=authorize, retain=retain)
    assert set(state['records']) == {'intent'}
    assert database.baseline() == before


def test_actual_unknown_fk_dependency_cannot_cascade_into_unowned_table(studio_db):
    import psycopg
    database, dsn = studio_db
    record, fixture = seed(database, dsn)
    with psycopg.connect(dsn) as connection:
        connection.execute('CREATE TABLE honua.unowned_studio_reference (item_id uuid REFERENCES honua.studio_content_items ON DELETE CASCADE)')
        connection.execute('INSERT INTO honua.unowned_studio_reference VALUES (%s)', (fixture['itemId'],))
        connection.execute('GRANT SELECT ON honua.unowned_studio_reference TO fixture_cleanup')
    state, authorize, retain = hooks(record)
    with pytest.raises(PackError, match='unowned foreign-key'):
        database.cleanup(record, authorize=authorize, retain=retain)
    with psycopg.connect(dsn) as connection:
        assert connection.execute('SELECT count(*) FROM honua.unowned_studio_reference').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM honua.studio_content_items').fetchone()[0] == 1
    assert set(state['records']) == {'intent'}


@pytest.mark.parametrize('visibility', ['no-select', 'row-security'])
def test_actual_unobservable_foreign_key_children_block_cleanup(studio_db, visibility):
    import psycopg
    database, dsn = studio_db
    record, fixture = seed(database, dsn)
    with psycopg.connect(dsn) as connection:
        connection.execute('CREATE TABLE public.unobservable_studio_reference (item_id uuid REFERENCES honua.studio_content_items ON DELETE CASCADE)')
        connection.execute('INSERT INTO public.unobservable_studio_reference VALUES (%s)', (fixture['itemId'],))
        if visibility == 'row-security':
            connection.execute('GRANT SELECT ON public.unobservable_studio_reference TO fixture_cleanup')
            connection.execute('ALTER TABLE public.unobservable_studio_reference ENABLE ROW LEVEL SECURITY')
    state, authorize, retain = hooks(record)
    with pytest.raises(PackError, match='cannot independently inspect|row-filtered'):
        database.cleanup(record, authorize=authorize, retain=retain)
    with psycopg.connect(dsn) as connection:
        assert connection.execute('SELECT count(*) FROM public.unobservable_studio_reference').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM honua.studio_content_items').fetchone()[0] == 1
    assert set(state['records']) == {'intent'}


def test_actual_composite_foreign_key_refuses_ambiguous_cleanup_even_without_children(studio_db):
    import psycopg
    database, dsn = studio_db
    record, _ = seed(database, dsn)
    with psycopg.connect(dsn) as connection:
        connection.execute('ALTER TABLE honua.studio_content_items ADD UNIQUE(item_id,owner_id)')
        connection.execute('CREATE TABLE honua.ambiguous_studio_reference (item_id uuid, owner_id text, FOREIGN KEY(item_id,owner_id) REFERENCES honua.studio_content_items(item_id,owner_id) ON DELETE CASCADE)')
    state, authorize, retain = hooks(record)
    with pytest.raises(PackError, match='composite'):
        database.cleanup(record, authorize=authorize, retain=retain)
    assert set(state['records']) == {'intent'}


def test_actual_preexisting_non_fk_publication_reference_preserved_and_blocks_cleanup(studio_db):
    import psycopg
    database, dsn = studio_db
    ids = tuple(str(uuid4()) for _ in range(3))
    with psycopg.connect(dsn) as connection:
        connection.execute('INSERT INTO honua.content_publication_versions VALUES (%s,%s,%s)',
            (str(uuid4()), ids[0], '{}'))
    record, _ = seed(database, dsn, ids=ids)
    state, authorize, retain = hooks(record)
    with pytest.raises(PackError, match='unowned JSON/text'):
        database.cleanup(record, authorize=authorize, retain=retain)
    with psycopg.connect(dsn) as connection:
        assert connection.execute('SELECT count(*) FROM honua.content_publication_versions').fetchone()[0] == 1
    assert set(state['records']) == {'intent'}


def test_actual_unknown_delete_trigger_fails_before_owned_effects(studio_db):
    import psycopg
    database, dsn = studio_db
    record, _ = seed(database, dsn)
    with psycopg.connect(dsn) as connection:
        connection.execute('CREATE FUNCTION honua.unreviewed_delete() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END; $$')
        connection.execute('CREATE TRIGGER unreviewed_delete BEFORE DELETE ON honua.studio_content_items FOR EACH ROW EXECUTE FUNCTION honua.unreviewed_delete()')
    state, authorize, retain = hooks(record)
    with pytest.raises(PackError, match='unknown delete triggers'):
        database.cleanup(record, authorize=authorize, retain=retain)
    with psycopg.connect(dsn) as connection:
        assert connection.execute('SELECT count(*) FROM honua.studio_content_items').fetchone()[0] == 1
