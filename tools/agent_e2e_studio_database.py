"""Exact owned Studio fixture rows; rollback and independent SQL absence.

Uses the same isolated IaC target and distinct controller-only SQL identities as
FixtureDatabase. Native initial fixture IDs select rows; no prefix, schema reset,
CASCADE, administrator login or model payload can select cleanup targets. This
profile includes the actual saved-map delete trigger/log/checkpoint relationships.
"""
from __future__ import annotations

import copy
import json

from agent_e2e_fixture_database import FixtureDatabase, require
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_studio_observer import guid


class StudioFixtureDatabase(FixtureDatabase):
    ITEM_TABLES = ('studio_content_items', 'studio_package_drafts', 'studio_content_versions',
        'studio_content_version_dependencies', 'studio_publication_requests', 'studio_rollback_requests')
    MAP_TABLES = ('saved_map_operation_log_heads', 'saved_map_operations', 'saved_map_checkpoint_versions',
                  'studio_map_comment_threads', 'studio_map_activity_events')
    TABLES = (*ITEM_TABLES, *MAP_TABLES, 'studio_map_comment_messages')
    PROTECTED = ('content_publication_versions', 'content_publication_routes', 'content_publication_events')
    PARENTS = {'studio_content_items': 'item_id', 'studio_package_drafts': 'draft_id',
        'studio_content_versions': 'version_id', 'saved_map_operation_log_heads': 'map_id',
        'studio_map_comment_threads': 'thread_id'}
    DELETE_BODY = 'BEGIN DELETE FROM honua.saved_map_operation_log_heads WHERE map_id = OLD.draft_id::text; RETURN OLD; END;'
    EDGES = {(table, 'item_id', 'studio_content_items', 'item_id') for table in ITEM_TABLES if table != 'studio_content_items'} | {
        ('studio_content_version_dependencies', 'version_id', 'studio_content_versions', 'version_id'),
        ('studio_publication_requests', 'version_id', 'studio_content_versions', 'version_id'),
        ('studio_rollback_requests', 'target_version_id', 'studio_content_versions', 'version_id'),
        ('saved_map_operations', 'map_id', 'saved_map_operation_log_heads', 'map_id'),
        ('saved_map_checkpoint_versions', 'map_id', 'saved_map_operation_log_heads', 'map_id'),
        ('saved_map_checkpoint_versions', 'version_id', 'studio_content_versions', 'version_id'),
        ('studio_map_comment_messages', 'thread_id', 'studio_map_comment_threads', 'thread_id')}

    def snapshot(self, connection):
        from psycopg import sql
        rows = {}
        for table in (*self.TABLES, *self.PROTECTED):
            observed = self._rows(connection, sql.SQL('SELECT to_jsonb(t) AS value FROM {} t').format(sql.Identifier('honua', table)))
            rows[table] = sorted((row['value'] for row in observed), key=canonical_digest)
        oids = connection.execute('''SELECT c.relname, c.oid::bigint AS oid, c.relrowsecurity AS row_security FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'honua' AND c.relname = ANY(%s)''',
            (list((*self.TABLES, *self.PROTECTED)),)).fetchall()
        require(len(oids) == len(self.TABLES)+len(self.PROTECTED), 'Studio fixture schema is absent or ambiguous')
        require(not any(row['row_security'] for row in oids), 'Studio cleanup refuses row-filtered native inventories')
        triggers = connection.execute('''SELECT c.relname AS table, t.tgname AS name, t.tgtype::int AS type,
            t.tgenabled::text AS enabled, t.tgnargs::int AS nargs, p.proname AS function,
            n.nspname AS function_schema, p.prosrc AS body, p.prosecdef AS security_definer,
            p.pronargs::int AS function_nargs, l.lanname AS language
            FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
            JOIN pg_namespace s ON s.oid = c.relnamespace JOIN pg_proc p ON p.oid = t.tgfoid
            JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang
            WHERE s.nspname = 'honua' AND c.relname = ANY(%s) AND NOT t.tgisinternal
                AND (t.tgtype::int & 8) = 8 ORDER BY c.relname, t.tgname''', (list(self.TABLES),)).fetchall()
        require(len(triggers) == 1, 'Studio cleanup refuses missing or unknown delete triggers')
        trigger = triggers[0]
        require(trigger['table'] == 'studio_package_drafts' and trigger['name'] == 'trg_delete_saved_map_operation_log_with_studio_draft' and
                trigger['type'] == 9 and trigger['enabled'] == 'O' and trigger['nargs'] == trigger['function_nargs'] == 0 and
                trigger['function'] == 'delete_saved_map_operation_log_with_studio_draft' and trigger['function_schema'] == 'honua' and
                trigger['security_definer'] is False and trigger['language'] == 'plpgsql' and
                ' '.join(trigger['body'].split()) == self.DELETE_BODY,
                'Studio delete trigger differs from reviewed exact draft/log semantics')
        rules = connection.execute('''SELECT 1 FROM pg_rewrite r JOIN pg_class c ON c.oid = r.ev_class
            JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'honua'
            AND c.relname = ANY(%s) AND r.ev_type = '4' LIMIT 1''', (list(self.TABLES),)).fetchone()
        require(rules is None, 'Studio cleanup refuses unknown delete rewrite rules')
        return {'product': super().snapshot(connection), 'studio': {table: rows[table] for table in self.TABLES},
            'protectedRegistry': {table: rows[table] for table in self.PROTECTED},
            'tableOids': {row['relname']: row['oid'] for row in oids}, 'deleteTriggers': triggers}

    def owned(self, snapshot, record):
        rows = snapshot['studio']
        item = record['itemId']
        drafts = {row['draft_id'] for row in rows['studio_package_drafts'] if row['item_id'] == item} | {record['draftId']}
        maps = drafts | {item}
        threads = {row['thread_id'] for row in rows['studio_map_comment_threads'] if row['map_id'] in maps}
        result = {table: [row for row in rows[table] if row['item_id'] == item] for table in self.ITEM_TABLES if table != 'studio_content_items'}
        result['studio_content_items'] = [row for row in rows['studio_content_items'] if row['item_id'] == item]
        result.update({table: [row for row in rows[table] if row['map_id'] in maps] for table in self.MAP_TABLES})
        result['studio_map_comment_messages'] = [row for row in rows['studio_map_comment_messages'] if row['thread_id'] in threads]
        return result

    def without_owned(self, snapshot, record):
        result = copy.deepcopy(snapshot)
        owned = self.owned(snapshot, record)
        for table, rows in owned.items():
            digests = {canonical_digest(row) for row in rows}
            result['studio'][table] = [row for row in result['studio'][table] if canonical_digest(row) not in digests]
        return result

    def ownership(self, baseline, fixture):
        require(baseline.get('candidateKey') == self.config['candidateKey'] and
                fixture.get('schema') == 'honua.agent-e2e.studio-fixture/v1' and
                fixture.get('baselineDigest') == canonical_digest(baseline), 'Studio ownership lacks exact native initial fixture')
        record = {'schemaVersion': 1, 'candidateKey': self.config['candidateKey'], 'baseline': copy.deepcopy(baseline),
            'itemId': guid(fixture['itemId']), 'draftId': guid(fixture['draftId']), 'versionId': guid(fixture['versionId']),
            'namespace': fixture['namespace'], 'ownerPrincipalId': fixture['ownerPrincipalId'], 'contentHash': fixture['contentHash']}
        with self.connection() as (connection, identity):
            require(baseline['database'] == {key: identity[key] for key in baseline['database']}, 'Studio baseline database identity changed')
            prepared = self.snapshot(connection)
        owned = self.owned(prepared, record)
        require(not any(self.owned(baseline['snapshot'], record).values()), 'Studio fixture IDs already belonged to baseline/shared content')
        require(len(owned['studio_content_items']) == len(owned['studio_package_drafts']) == len(owned['studio_content_versions']) == 1,
                'Studio setup does not contain exactly its observed initial item/draft/version')
        item, draft, version = (owned[table][0] for table in ('studio_content_items', 'studio_package_drafts', 'studio_content_versions'))
        require(item['current_version_id'] == record['versionId'] and item['published_version_id'] is None and
                draft['draft_id'] == record['draftId'] and version['version_id'] == record['versionId'] and
                version['source_draft_id'] == record['draftId'] and version['version_number'] == 1 and
                version['content_hash'] == record['contentHash'] and version['envelope'] == fixture['initialVersion']['envelope'] and
                all(row['owner_id'] == record['ownerPrincipalId'] and row['workspace_id'] == record['namespace'] and
                    row['package_key'] == record['namespace'] for row in (item, draft, version)),
                'native initial Studio SQL identity/content/pointers differ from HTTP fixture')
        require(self.without_owned(prepared, record) == baseline['snapshot'], 'Studio setup changed unrelated/native shared state')
        record['prepared'] = prepared
        return record

    def check_references(self, connection, snapshot, record):
        from psycopg.errors import InsufficientPrivilege
        from psycopg import sql
        owned = self.owned(snapshot, record)
        identifiers = {record['itemId'], record['draftId'], record['versionId']}
        for table, column in self.PARENTS.items():
            identifiers.update(row[column] for row in owned[table])
        remaining = self.without_owned(snapshot, record)
        for rows in (*remaining['studio'].values(), *remaining['protectedRegistry'].values()):
            for row in rows:
                encoded = json.dumps(row, sort_keys=True).lower()
                require(not any(identifier in encoded for identifier in identifiers),
                        'owned Studio identifier has an unowned JSON/text reference')
        # Lock every relevant parent before inspecting FK children: new child
        # inserts must not slip in between the check and a native FK cascade.
        values = {}
        for table, column in self.PARENTS.items():
            values[table] = [row[column] for row in owned[table]]
            if values[table]:
                connection.execute(sql.SQL('SELECT 1 FROM {} WHERE {}::text = ANY(%s) FOR UPDATE').format(
                    sql.Identifier('honua', table), sql.Identifier(column)), (values[table],)).fetchall()
        foreign_keys = connection.execute('''SELECT c.relname AS child, n.nspname AS schema, p.relname AS parent,
            c.relrowsecurity AS row_security,
            ARRAY(SELECT a.attname FROM unnest(f.conkey) WITH ORDINALITY k(attnum, ordinal)
                JOIN pg_attribute a ON a.attrelid = f.conrelid AND a.attnum = k.attnum ORDER BY k.ordinal) AS columns,
            ARRAY(SELECT a.attname FROM unnest(f.confkey) WITH ORDINALITY k(attnum, ordinal)
                JOIN pg_attribute a ON a.attrelid = f.confrelid AND a.attnum = k.attnum ORDER BY k.ordinal) AS parent_columns
            FROM pg_constraint f JOIN pg_class c ON c.oid = f.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_class p ON p.oid = f.confrelid WHERE f.contype = 'f' AND f.confrelid = ANY(%s::oid[])''',
            ([snapshot['tableOids'][table] for table in self.TABLES],)).fetchall()
        for foreign in foreign_keys:
            parent = foreign['parent']
            require(not foreign['row_security'], 'Studio cleanup refuses row-filtered foreign-key inventories')
            require(parent in self.PARENTS and len(foreign['columns']) == 1 and
                    foreign['parent_columns'] == [self.PARENTS[parent]], 'unsupported composite/ambiguous Studio foreign key')
            column, child = foreign['columns'][0], foreign['child']
            try:
                matching = self._rows(connection, sql.SQL('SELECT to_jsonb(t) AS value FROM {} t WHERE {}::text = ANY(%s)').format(
                    sql.Identifier(foreign['schema'], child), sql.Identifier(column)), (values[parent],))
            except InsufficientPrivilege as error:
                raise PackError('Studio cleanup cannot independently inspect foreign-key dependent rows') from error
            edge = (child, column, parent, self.PARENTS[parent])
            if foreign['schema'] == 'honua' and edge in self.EDGES:
                allowed = {canonical_digest(row) for row in owned[child]}
                require(all(canonical_digest(row['value']) in allowed for row in matching), 'known Studio cascade contains an unowned child row')
            else:
                require(not matching, 'owned Studio resource has an unowned foreign-key dependent row')

    def cleanup(self, record, *, authorize, retain):
        from psycopg import sql
        require(record.get('candidateKey') == self.config['candidateKey'], 'Studio cleanup belongs to another candidate')
        authorize(record)
        intent = {'schemaVersion': 1, 'candidateKey': record['candidateKey'], 'ownershipDigest': canonical_digest(record)}
        acknowledgement = retain('intent', intent)
        require(isinstance(acknowledgement, dict) and acknowledgement.get('sha256') == canonical_digest(intent), 'Studio cleanup intent not retained')
        authorize(record)
        with self.connection(cleanup=True) as (connection, cleanup_identity):
            connection.execute(sql.SQL('LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE').format(
                sql.SQL(', ').join(sql.Identifier('honua', table) for table in (*self.TABLES, *self.PROTECTED))))
            current = self.snapshot(connection)
            require(self.without_owned(current, record) == record['baseline']['snapshot'], 'Studio model/setup changed unrelated shared state')
            owned = self.owned(current, record)
            for table in ('studio_content_items', 'studio_package_drafts', 'studio_content_versions'):
                require(all(row['owner_id'] == record['ownerPrincipalId'] and row['workspace_id'] == record['namespace'] and
                            row['package_key'] == record['namespace'] for row in owned[table]), 'Studio owned metadata identity changed')
            original_versions = self.owned(record['prepared'], record)['studio_content_versions']
            actual_versions = {row['version_id']: row for row in owned['studio_content_versions']}
            require(all(row['version_id'] not in actual_versions or row == actual_versions[row['version_id']]
                        for row in original_versions), 'immutable initial Studio version changed')
            self.check_references(connection, current, record)
            authorize(record)
            # Delete exact observed owned children first. The independently
            # checked native draft trigger sees only its already-absent own log.
            order = ('studio_map_comment_messages', 'studio_map_activity_events', 'studio_map_comment_threads',
                'saved_map_checkpoint_versions', 'saved_map_operations', 'saved_map_operation_log_heads',
                'studio_publication_requests', 'studio_rollback_requests', 'studio_content_version_dependencies',
                'studio_package_drafts', 'studio_content_versions', 'studio_content_items')
            for table in order:
                rows = owned[table]
                if not rows:
                    continue
                # Parent identifiers are derived from exact current owned rows,
                # never a prefix or another task's input.
                column = ('item_id' if table in self.ITEM_TABLES else 'thread_id' if table == 'studio_map_comment_messages' else 'map_id')
                selected = sorted({row[column] for row in rows})
                connection.execute(sql.SQL('DELETE FROM {} WHERE {}::text = ANY(%s)').format(
                    sql.Identifier('honua', table), sql.Identifier(column)), (selected,))
            require(self.snapshot(connection) == record['baseline']['snapshot'], 'Studio transaction did not restore exact shared baseline')
            authorize(record)
        with self.connection() as (connection, observer_identity):
            observed = self.snapshot(connection)
            require(observer_identity['principal'] != cleanup_identity['principal'] and observed == record['baseline']['snapshot'],
                    'independent SQL observer found remaining or changed Studio resources')
        result = {'schemaVersion': 1, 'candidateKey': record['candidateKey'], 'ownershipDigest': canonical_digest(record),
            'cleanupPrincipal': cleanup_identity['principal'], 'observerPrincipal': observer_identity['principal'],
            'baselineDigest': canonical_digest(observed), 'resourceIds': [], 'qualification': False}
        acknowledgement = retain('absence', result)
        require(isinstance(acknowledgement, dict) and acknowledgement.get('sha256') == canonical_digest(result), 'Studio independent absence not retained')
        return result
