"""Owned P10 fixture cleanup on one isolated IaC-provisioned PostgreSQL target.

The cleanup login and independent read-only observer stay on the controller.
No database, role, schema or cloud resource is created/configured here. Product
import/publication responses supply names/IDs; SQL captures the actual OIDs and
metadata revisions. Cleanup rejects changed ownership or concurrent mutations.
This profile supports read-only P10 A/B development; it is not a general reset.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import os
import re

from agent_e2e_pack import PackError, canonical_digest


def require(value, reason):
    if not value:
        raise PackError(reason)


def ident(value):
    require(isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,62}', value),
            'invalid observed fixture SQL identifier')
    return value


class FixtureDatabase:
    CATALOG = ('layers', 'services', 'service_layers', 'layer_fields', 'features')
    SIDECARS = tuple('metadata_v2_'+name+'_idx' for name in
                     ('resources', 'services', 'publications', 'storage_bindings', 'connections'))
    MAX_ROWS = 10000

    def __init__(self, config):
        fields = {'target', 'database', 'databaseOid', 'serverAddress', 'serverPort', 'metadataEnvironment',
                  'cleanupRole', 'observerRole', 'cleanupDsnEnv', 'observerDsnEnv', 'candidateKey'}
        require(isinstance(config, dict) and set(config) == fields, 'incomplete isolated fixture database binding')
        require(config['target'] in ('local-docker', 'aws-ecs-redis-on'), 'unsupported fixture database target')
        require(isinstance(config['database'], str) and re.fullmatch(r'honua_agent_[a-z0-9_]{1,40}', config['database']),
                'fixture SQL refuses public demo, default or unbound databases')
        require(type(config['databaseOid']) is int and config['databaseOid'] > 0 and
                isinstance(config['serverAddress'], str) and config['serverAddress'] and
                type(config['serverPort']) is int and 1 <= config['serverPort'] <= 65535,
                'missing independently observed database/server identity')
        require(isinstance(config['metadataEnvironment'], str) and
                re.fullmatch(r'[A-Za-z0-9_-]{1,100}', config['metadataEnvironment']), 'invalid metadata environment')
        require(ident(config['cleanupRole']) != ident(config['observerRole']), 'cleanup and observer identities must differ')
        require(isinstance(config['candidateKey'], str) and re.fullmatch(r'sha256:[0-9a-f]{64}', config['candidateKey']),
                'fixture database lacks exact candidate binding')
        for key in ('cleanupDsnEnv', 'observerDsnEnv'):
            require(isinstance(config[key], str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,100}', config[key]),
                    'fixture credentials must use controller-only environment references')
        require(config['cleanupDsnEnv'] != config['observerDsnEnv'], 'fixture credential references must differ')
        self.config = copy.deepcopy(config)

    @contextmanager
    def connection(self, *, cleanup=False):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        from psycopg.rows import dict_row
        kind = 'cleanup' if cleanup else 'observer'
        dsn = os.environ.get(self.config[kind+'DsnEnv'])
        require(isinstance(dsn, str) and dsn, 'missing controller-only fixture database credential')
        settings = conninfo_to_dict(dsn)
        local = self.config['target'] == 'local-docker' and settings.get('host') in ('127.0.0.1', '::1')
        require(local or settings.get('sslmode') == 'verify-full', 'remote fixture database requires TLS verify-full')
        with psycopg.connect(dsn, connect_timeout=5, row_factory=dict_row,
                application_name='honua-agent-fixture-'+kind) as connection:
            with connection.transaction():
                connection.execute('SET TRANSACTION ISOLATION LEVEL '+
                    ('READ COMMITTED' if cleanup else 'REPEATABLE READ READ ONLY'))
                connection.execute("SET LOCAL statement_timeout = '5s'")
                connection.execute("SET LOCAL lock_timeout = '1s'")
                identity = connection.execute('''SELECT current_database() AS database,
                    (SELECT oid::bigint FROM pg_database WHERE datname = current_database()) AS "databaseOid",
                    inet_server_addr()::text AS "serverAddress", inet_server_port() AS "serverPort",
                    current_user AS principal,
                    (SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()) AS ssl''').fetchone()
                require(all(identity[key] == self.config[key] for key in
                        ('database', 'databaseOid', 'serverAddress', 'serverPort')),
                        'fixture database differs from independently bound isolated target')
                require(identity['principal'] == self.config[kind+'Role'], 'fixture database login differs from reviewed role')
                require(local or identity['ssl'] is True, 'fixture connection did not negotiate TLS')
                flags = connection.execute('''SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls
                    FROM pg_roles WHERE rolname = current_user''').fetchone()
                require(flags is not None and not any(flags.values()), 'fixture roles must not have administrative database authority')
                if not cleanup:
                    require(connection.execute('SHOW transaction_read_only').fetchone()['transaction_read_only'] == 'on',
                            'fixture observer transaction must be read-only')
                yield connection, identity

    def _rows(self, connection, query, parameters=()):
        rows = connection.execute(query, parameters).fetchmany(self.MAX_ROWS+1)
        require(len(rows) <= self.MAX_ROWS, 'fixture observation exceeds bounded P10 database inventory')
        return rows

    def snapshot(self, connection):
        from psycopg import sql
        catalog = {}
        for table in self.CATALOG:
            rows = self._rows(connection, sql.SQL('SELECT to_jsonb(t) AS value FROM {} t').format(sql.Identifier('honua', table)))
            catalog[table] = sorted((row['value'] for row in rows), key=canonical_digest)
        environment = self.config['metadataEnvironment']
        snapshots = self._rows(connection, '''SELECT to_jsonb(t) AS value FROM honua.metadata_v2_snapshots t
            WHERE environment = %s ORDER BY revision''', (environment,))
        current = self._rows(connection, '''SELECT to_jsonb(t) AS value FROM honua.metadata_v2_current t
            WHERE environment = %s''', (environment,))
        require(len(current) <= 1, 'ambiguous metadata current pointer')
        relations = self._rows(connection, '''SELECT c.oid::bigint AS oid, n.nspname AS schema, c.relname AS name
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'honua_data' AND c.relkind IN ('r', 'p') ORDER BY c.oid''')
        sidecars = {}
        for table in self.SIDECARS:
            rows = self._rows(connection, sql.SQL('SELECT to_jsonb(t) AS value FROM {} t WHERE environment = %s').format(
                sql.Identifier('honua', table)), (environment,))
            sidecars[table] = sorted((row['value'] for row in rows), key=canonical_digest)
        return {'catalog': catalog, 'snapshots': [row['value'] for row in snapshots],
                'current': current[0]['value'] if current else None, 'relations': relations, 'sidecars': sidecars}

    def baseline(self):
        with self.connection() as (connection, identity):
            return {'schemaVersion': 1, 'candidateKey': self.config['candidateKey'],
                    'database': {key: identity[key] for key in ('database', 'databaseOid', 'serverAddress', 'serverPort')},
                    'observerPrincipal': identity['principal'], 'snapshot': self.snapshot(connection)}

    def ownership(self, baseline, bindings):
        """Call after product setup; retain this record before running the model."""
        require(baseline.get('candidateKey') == self.config['candidateKey'], 'fixture baseline belongs to another candidate')
        require(bindings.get('startingState') == 'published-town' and isinstance(bindings.get('layers'), dict)
                and set(bindings['layers']) == {'parcels', 'flood_zones', 'shelters'}, 'P10 requires exact observed town setup')
        with self.connection() as (connection, identity):
            require(baseline['database'] == {key: identity[key] for key in baseline['database']},
                    'fixture baseline database identity changed')
            prepared = self.snapshot(connection)
        owned_tables, layer_ids, services = [], [], []
        baseline_oids = {row['oid'] for row in baseline['snapshot']['relations']}
        for binding in bindings['layers'].values():
            schema, name = ident(binding['schema']), ident(binding['table'])
            require(schema == 'honua_data', 'P10 cleanup refuses non-fixture source schemas')
            matches = [row for row in prepared['relations'] if row['schema'] == schema and row['name'] == name]
            require(len(matches) == 1 and matches[0]['oid'] not in baseline_oids,
                    'source relation was not newly observed after this setup')
            layer_id, service = binding.get('layerId'), binding.get('service')
            require(type(layer_id) is int and layer_id >= 0 and isinstance(service, str) and
                    re.fullmatch(r'[a-z][a-z0-9_-]{0,62}', service), 'invalid actual layer/service ownership')
            layers = [row for row in prepared['catalog']['layers'] if row['layer_id'] == layer_id]
            require(len(layers) == 1 and layers[0]['table_schema'] == schema and layers[0]['table_name'] == name,
                    'actual layer catalog differs from observed imported table')
            owned_tables.append(matches[0])
            layer_ids.append(layer_id)
            services.append(service)
        require(len(set(layer_ids)) == len(layer_ids) and len(set(services)) == len(services), 'duplicate fixture ownership binding')
        actual_memberships = {(row['service_name'], row['layer_id']) for row in prepared['catalog']['service_layers']
                              if row['layer_id'] in layer_ids}
        require(actual_memberships == set(zip(services, layer_ids)), 'fixture layer has an unowned or unexpected service membership')
        old_revisions = {row['revision'] for row in baseline['snapshot']['snapshots']}
        new_revisions = [row['revision'] for row in prepared['snapshots'] if row['revision'] not in old_revisions]
        require(new_revisions and prepared['current'] and prepared['current']['revision'] in new_revisions,
                'fixture publication lacks observed new metadata revisions/current pointer')
        record = {'schemaVersion': 1, 'candidateKey': self.config['candidateKey'], 'baseline': baseline,
                  'prepared': prepared, 'tables': owned_tables, 'layerIds': layer_ids, 'services': services,
                  'revisions': new_revisions}
        require(self._without_owned(prepared, record) == baseline['snapshot'],
                'setup changed unrelated catalog, source relation or retained metadata')
        return record

    def _without_owned(self, snapshot, record):
        result = copy.deepcopy(snapshot)
        ids, services = set(record['layerIds']), set(record['services'])
        for table, rows in result['catalog'].items():
            result['catalog'][table] = [row for row in rows if
                (row['service_name'] not in services if table == 'services' else row['layer_id'] not in ids)]
        result['snapshots'] = [row for row in result['snapshots'] if row['revision'] not in record['revisions']]
        for table, rows in result['sidecars'].items():
            result['sidecars'][table] = [row for row in rows if row['revision'] not in record['revisions']]
        result['current'] = copy.deepcopy(record['baseline']['snapshot']['current'])
        result['relations'] = [row for row in result['relations'] if row['oid'] not in {table['oid'] for table in record['tables']}]
        return result

    def cleanup(self, record, *, authorize, retain, invalidate):
        """Single bounded transaction; a failed DROP rolls back metadata deletion.

        authorize must bind the exact durably retained ownership record and live
        cleanup liability. retain must create-only publish intent before effects.
        invalidate must use the product API for these exact case-owned services.
        """
        from psycopg import sql
        require(record.get('candidateKey') == self.config['candidateKey'], 'cleanup belongs to another candidate')
        authorize(record)
        intent = {'schemaVersion': 1, 'candidateKey': record['candidateKey'], 'ownershipDigest': canonical_digest(record)}
        acknowledgement = retain('intent', intent)
        require(isinstance(acknowledgement, dict) and acknowledgement.get('sha256') == canonical_digest(intent),
                'cleanup intent was not durably acknowledged')
        authorize(record)
        with self.connection(cleanup=True) as (connection, identity):
            connection.execute('''LOCK TABLE honua.layers, honua.services, honua.service_layers,
                honua.layer_fields, honua.features, honua.metadata_v2_current, honua.metadata_v2_snapshots
                IN SHARE ROW EXCLUSIVE MODE''')
            for table in sorted(record['tables'], key=lambda table: table['oid']):
                connection.execute(sql.SQL('LOCK TABLE {} IN ACCESS EXCLUSIVE MODE').format(
                    sql.Identifier(table['schema'], table['name'])))
            require(self.snapshot(connection) == record['prepared'],
                    'fixture state or ownership changed; retain liability and reconcile')
            authorize(record)
            ids = record['layerIds']
            # Block new FK references while checking dependencies. Table-level
            # SHARE ROW EXCLUSIVE alone does not conflict with FK key-share locks.
            connection.execute('SELECT layer_id FROM honua.layers WHERE layer_id = ANY(%s) FOR UPDATE', (ids,)).fetchall()
            connection.execute('SELECT service_name FROM honua.services WHERE service_name = ANY(%s) FOR UPDATE',
                               (record['services'],)).fetchall()
            connection.execute('''SELECT revision FROM honua.metadata_v2_snapshots
                WHERE environment = %s AND revision = ANY(%s) FOR UPDATE''',
                (self.config['metadataEnvironment'], record['revisions'])).fetchall()
            # Unknown cascading children could erase unrelated state even when
            # the parent IDs are owned. Observe both service and layer FKs, and
            # reject composite forms this bounded P10 adapter cannot interpret.
            foreign_keys = connection.execute('''SELECT n.nspname AS schema, c.relname AS table,
                p.relname AS parent,
                ARRAY(SELECT a.attname FROM unnest(f.conkey) WITH ORDINALITY k(attnum, ordinal)
                    JOIN pg_attribute a ON a.attrelid = f.conrelid AND a.attnum = k.attnum ORDER BY k.ordinal) AS columns,
                ARRAY(SELECT a.attname FROM unnest(f.confkey) WITH ORDINALITY k(attnum, ordinal)
                    JOIN pg_attribute a ON a.attrelid = f.confrelid AND a.attnum = k.attnum ORDER BY k.ordinal) AS parent_columns
                FROM pg_constraint f JOIN pg_class c ON c.oid = f.conrelid
                JOIN pg_class p ON p.oid = f.confrelid JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE f.contype = 'f' AND f.confrelid IN ('honua.layers'::regclass, 'honua.services'::regclass,
                    'honua.metadata_v2_snapshots'::regclass)''').fetchall()
            for foreign_key in foreign_keys:
                if foreign_key['parent'] == 'metadata_v2_snapshots':
                    require(foreign_key['schema'] == 'honua' and foreign_key['table'] in
                            (*self.SIDECARS, 'metadata_v2_current') and
                            foreign_key['columns'] == foreign_key['parent_columns'] == ['environment', 'revision'],
                            'unsupported metadata snapshot foreign key; refusing unowned cascading deletion')
                    continue
                parent_column = 'layer_id' if foreign_key['parent'] == 'layers' else 'service_name'
                require(len(foreign_key['columns']) == 1 and foreign_key['parent_columns'] == [parent_column],
                        'unsupported composite or ambiguous fixture foreign key; refusing cleanup')
                column = foreign_key['columns'][0]
                if (foreign_key['schema'], foreign_key['table'], column) in {
                        ('honua', 'layer_fields', 'layer_id'), ('honua', 'service_layers', 'layer_id'),
                        ('honua', 'features', 'layer_id'), ('honua', 'service_layers', 'service_name')}:
                    continue
                values = ids if foreign_key['parent'] == 'layers' else record['services']
                referenced = connection.execute(sql.SQL('SELECT 1 FROM {} WHERE {} = ANY(%s) LIMIT 1').format(
                    sql.Identifier(foreign_key['schema'], foreign_key['table']),
                    sql.Identifier(column)), (values,)).fetchone()
                require(referenced is None, 'owned resource has an unowned dependent row; refusing cascading deletion')
            # An owned layer must not have been attached to an unrelated service.
            memberships = connection.execute('SELECT service_name FROM honua.service_layers WHERE layer_id = ANY(%s)', (ids,)).fetchall()
            require(memberships and all(row['service_name'] in record['services'] for row in memberships),
                    'owned layer is referenced by an unowned service')
            for table in ('features', 'layer_fields', 'service_layers', 'layers'):
                connection.execute(sql.SQL('DELETE FROM {} WHERE layer_id = ANY(%s)').format(sql.Identifier('honua', table)), (ids,))
            connection.execute('DELETE FROM honua.services WHERE service_name = ANY(%s)', (record['services'],))
            baseline = record['baseline']['snapshot']
            environment = self.config['metadataEnvironment']
            if baseline['current'] is None:
                connection.execute('DELETE FROM honua.metadata_v2_current WHERE environment = %s', (environment,))
            else:
                old = baseline['current']
                connection.execute('''UPDATE honua.metadata_v2_current SET revision = %s, etag = %s,
                    activated_at = %s::timestamptz WHERE environment = %s''',
                    (old['revision'], old['etag'], old['activated_at'], environment))
            connection.execute('DELETE FROM honua.metadata_v2_snapshots WHERE environment = %s AND revision = ANY(%s)',
                               (environment, record['revisions']))
            for table in record['tables']:
                current = connection.execute('SELECT oid::bigint AS oid FROM pg_class WHERE oid = %s', (table['oid'],)).fetchone()
                require(current is not None, 'owned relation disappeared during cleanup')
                connection.execute(sql.SQL('DROP TABLE {}').format(sql.Identifier(table['schema'], table['name'])))
            require(self.snapshot(connection) == baseline, 'cleanup transaction does not preserve exact baseline')
            authorize(record)
        # A commit acknowledgement alone is not independent absence evidence.
        invalidate(record['services'])
        with self.connection() as (connection, observed):
            require(observed['principal'] != identity['principal'], 'cleanup observer is not an independent identity')
            result = self.snapshot(connection)
            require(result == record['baseline']['snapshot'], 'independent SQL observer found remaining or changed resources')
        evidence = {'schemaVersion': 1, 'candidateKey': record['candidateKey'],
            'ownershipDigest': canonical_digest(record), 'observerPrincipal': observed['principal'],
            'cleanupPrincipal': identity['principal'], 'baselineDigest': canonical_digest(result),
            'resourceIds': [], 'qualification': False}
        acknowledgement = retain('observation', evidence)
        require(acknowledgement.get('sha256') == canonical_digest(evidence), 'independent cleanup observation was not retained')
        return evidence
