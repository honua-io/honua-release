"""Independent canonical dataset-task/job and actual PostGIS output observations.

Only model-selected native workspace import tasks may be observed. Preparation,
sealed uploads and queued jobs never count as imported data. Successful output
must name its actual persisted physical table; SQL checks that new exact OID and
reads every source property and stored geometry without reprojection or repair.
No method uploads, submits, retries, approves, publishes or cleans a dataset.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import re
from urllib.parse import urlencode

from agent_e2e_fixture_database import FixtureDatabase, ident
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_scenario_operations import data, token
from agent_e2e_studio_observer import instant

CREATE = 'honua_workspace_create_dataset_import_task'
SUBMIT = 'honua_workspace_submit_dataset_import'


def require(condition, reason):
    if not condition:
        raise PackError(reason)


class SqlImportedDataset:
    def __init__(self, database, *, baseline):
        require(isinstance(database, FixtureDatabase) and baseline.get('candidateKey') == database.config['candidateKey'],
                'import SQL observer lacks its original independent candidate baseline')
        self.database, self.baseline = database, copy.deepcopy(baseline)

    def ownership(self, receipt):
        """Read exact physical identity even when feature verification failed.

        Cleanup must not depend on valid geometry/properties or an expected row
        count. Original successful native job output, new OID and importer marker
        still limit the cleanup target; unknown references remain DROP RESTRICT.
        """
        from psycopg import sql
        require(isinstance(receipt, dict) and receipt.get('schema') == 'honua_data',
                'cleanup import lacks its actual physical output')
        schema, table = ident(receipt['schema']), ident(receipt.get('physicalTableName'))
        before = self.baseline['snapshot'].get('product', self.baseline['snapshot'])['relations']
        require(not any(row['schema'] == schema and row['name'] == table for row in before),
                'cleanup import output names preexisting shared content')
        with self.database.connection() as (connection, identity):
            require(self.baseline['database'] == {key: identity[key] for key in self.baseline['database']},
                    'cleanup import database changed from original baseline')
            connection.execute(sql.SQL('LOCK TABLE {} IN ACCESS SHARE MODE').format(sql.Identifier(schema, table)))
            relation = connection.execute('''SELECT c.oid::bigint AS oid, c.relkind::text AS kind, c.relrowsecurity AS row_security,
                pg_get_userbyid(c.relowner) AS owner, obj_description(c.oid, 'pg_class') AS marker
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s''',
                (schema, table)).fetchone()
            require(relation is not None and relation['kind'] == 'r' and relation['marker'] == 'honua:import-owned:v1' and
                    relation['oid'] not in {row['oid'] for row in before},
                    'cleanup import physical output is absent, replaced or lacks native ownership marker')
        return {'table': {'schema': schema, 'name': table, **relation}, 'observerPrincipal': identity['principal']}

    def observe(self, receipt):
        from psycopg import sql
        require(isinstance(receipt, dict) and set(receipt) == {'physicalTableName', 'schema', 'featureCount', 'detectedSrid'} and
                receipt['schema'] == 'honua_data' and type(receipt['featureCount']) is int and
                0 <= receipt['featureCount'] <= self.database.MAX_ROWS, 'import job lacks bounded actual physical output')
        schema, table = ident(receipt['schema']), ident(receipt['physicalTableName'])
        baseline = self.baseline['snapshot']
        relations = baseline.get('product', baseline)['relations']
        require(not any(row['schema'] == schema and row['name'] == table for row in relations),
                'successful import output points to preexisting shared content')
        with self.database.connection() as (connection, identity):
            require(self.baseline['database'] == {key: identity[key] for key in self.baseline['database']},
                    'import SQL observation database changed from original baseline')
            connection.execute(sql.SQL('LOCK TABLE {} IN ACCESS SHARE MODE').format(sql.Identifier(schema, table)))
            relation = connection.execute('''SELECT c.oid::bigint AS oid, c.relkind::text AS kind, c.relrowsecurity AS row_security,
                pg_get_userbyid(c.relowner) AS owner, obj_description(c.oid, 'pg_class') AS marker
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s AND c.relname=%s''',
                (schema, table)).fetchone()
            require(relation is not None and relation['kind'] == 'r' and not relation['row_security'] and
                    relation['marker'] == 'honua:import-owned:v1' and relation['oid'] not in {row['oid'] for row in relations},
                    'import physical output is absent, replaced, row-filtered or not an actual importer-owned relation')
            columns = connection.execute('''SELECT a.attname AS name, t.typname AS type FROM pg_attribute a
                JOIN pg_type t ON t.oid=a.atttypid WHERE a.attrelid=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum''',
                (relation['oid'],)).fetchall()
            require({row['name']: row['type'] for row in columns} ==
                    {'id': 'int4', 'geometry': 'geometry', 'properties': 'jsonb', 'created_at': 'timestamptz'},
                    'import SQL observer refuses an unknown physical feature layout')
            rows = self.database._rows(connection, sql.SQL('''SELECT properties, ST_AsGeoJSON(geometry,15,0)::jsonb AS geometry,
                ST_SRID(geometry) AS srid FROM {} ORDER BY id''').format(sql.Identifier(schema, table)))
        require(len(rows) == receipt['featureCount'], 'actual imported SQL rows differ from persisted job count')
        srids = {row['srid'] for row in rows}
        require(len(srids) == 1 and type(next(iter(srids))) is int and next(iter(srids)) > 0,
                'imported SQL geometry is empty, mixed, NULL or lacks an actual positive SRID')
        features, identifiers = [], set()
        for row in rows:
            properties, geometry = row['properties'], row['geometry']
            require(isinstance(properties, dict) and isinstance(properties.get('source_id'), str) and properties['source_id'] and
                    properties['source_id'] not in identifiers and isinstance(geometry, dict) and
                    geometry.get('type') in ('Point', 'MultiPoint', 'LineString', 'MultiLineString', 'Polygon', 'MultiPolygon') and
                    isinstance(geometry.get('coordinates'), list), 'import SQL output lost source IDs, properties or supported geometry')
            identifiers.add(properties['source_id'])
            features.append({'type': 'Feature', 'id': properties['source_id'],
                             'properties': copy.deepcopy(properties), 'geometry': copy.deepcopy(geometry)})
        features.sort(key=lambda feature: feature['id'])
        return {'table': {'schema': schema, 'name': table, **relation}, 'observerPrincipal': identity['principal'],
                'features': {'srid': next(iter(srids)), 'features': features}}


class DatasetImportObserver:
    def __init__(self, *, http, binding, sql, retain):
        require(set(binding) == {'workspaceId', 'ownerPrincipalId', 'tenantId', 'targetId', 'destinations', 'notBefore'} and
                isinstance(binding['destinations'], list) and 1 <= len(binding['destinations']) <= 16,
                'canonical import observer lacks exact native workspace/source/destination scope')
        token(binding['workspaceId'], 'import workspace')
        self.http, self.binding, self.sql, self.retain = http, copy.deepcopy(binding), sql, retain
        self.not_before = instant(binding['notBefore'])
        destinations = set()
        for destination in binding['destinations']:
            require(set(destination) == {'schema', 'table', 'sourceSha256', 'totalBytes', 'fileName', 'sourceSrid', 'targetSrid'} and
                    destination['schema'] == 'honua_data' and
                    re.fullmatch(r'[0-9a-f]{64}', destination['sourceSha256']) and type(destination['totalBytes']) is int and
                    destination['totalBytes'] > 0 and type(destination['targetSrid']) is int and destination['targetSrid'] > 0,
                    'canonical import scope lacks exact original source bytes and destination CRS')
            selected = (ident(destination['schema']), ident(destination['table']))
            require(selected not in destinations, 'duplicate original import destination')
            destinations.add(selected)

    def eligible(self, action):
        return action.get('name') == 'mcp_call_tool' and action.get('input', {}).get('name') in (CREATE, SUBMIT)

    def task_path(self, task_id, revision):
        require(type(revision) is int and revision > 0, 'invalid native immutable import task revision')
        return '/api/v1/agent/workspaces/'+self.binding['workspaceId']+'/dataset-import-tasks/'+token(task_id, 'import task')+'/versions/'+str(revision)

    def validate_task(self, task):
        require(task.get('workspaceId') == self.binding['workspaceId'] and
                task.get('ownerActorId') == self.binding['ownerPrincipalId'] and task.get('tenantId') == self.binding['tenantId'] and
                task.get('targetId') == self.binding['targetId'] and re.fullmatch(r'[0-9a-f]{64}', task.get('contentHash', '')),
                'native import task belongs to another actor/tenant/target/workspace')
        target, source, options = task.get('destination', {}), task.get('source', {}), task.get('options', {})
        matches = [row for row in self.binding['destinations'] if row['schema'] == target.get('schema') and row['table'] == target.get('table')]
        require(len(matches) == 1, 'native import task selected an unowned destination')
        selected = matches[0]
        require(source.get('expectedSha256') == selected['sourceSha256'] and source.get('totalBytes') == selected['totalBytes'] and
                source.get('fileName') == selected['fileName'] and source.get('kind') in ('browser-file', 'local-process-artifact') and
                options.get('targetSrid') == selected['targetSrid'] and options.get('sourceSrid') == selected['sourceSrid'] and
                options.get('loadMode') in (0, 1), 'native import task changed source bytes, destination or declared coordinate systems')
        return selected

    def selected(self, action, dispatch, worker_result):
        require(self.eligible(action), 'import observation requires an actual selected canonical workspace action')
        arguments, tool = action['input'].get('arguments', {}), action['input']['name']
        require(arguments.get('workspaceId') == self.binding['workspaceId'], 'model selected another import workspace')
        result = worker_result.get('agentResult', {}).get('content', {})
        require(result.get('isError', False) is False and isinstance(result.get('structuredContent'), dict),
                'model import action lacks native structured output')
        value, prefix = result['structuredContent'], 'import-'+dispatch['actionId'][:32]
        references = {}
        if tool == CREATE:
            intent = token(arguments.get('request', {}).get('intentId'), 'original import intent')
            answer = self.http.json(prefix+'-intent', 'GET', '/api/v1/agent/workspaces/'+self.binding['workspaceId']+'/dataset-import-task-intents/'+intent)
            task = data(answer)
            require(task == value, 'model import task differs from independently retained original intent')
            references['intent'] = answer['reference']
            task_id, revision, content_hash = task.get('taskId'), task.get('revision'), task.get('contentHash')
        else:
            task_id, revision = arguments.get('taskId'), arguments.get('revision')
            content_hash = arguments.get('request', {}).get('taskContentHash')
            task = None
        path = self.task_path(task_id, revision)
        answer = self.http.json(prefix+'-task', 'GET', path)
        independently_read = data(answer)
        require(independently_read.get('taskId') == task_id and independently_read.get('revision') == revision and
                independently_read.get('contentHash') == content_hash and (task is None or task == independently_read),
                'native import task identity changed after model action')
        self.validate_task(independently_read)
        references['task'] = answer['reference']
        if tool == SUBMIT:
            require(all(value.get(key) == expected for key, expected in
                (('taskId', task_id), ('taskRevision', revision), ('taskContentHash', content_hash))), 'model submission belongs to another immutable task')
        record = {'actionId': dispatch['actionId'], 'requestDigest': dispatch['requestDigest'], 'tool': tool,
            'task': independently_read, 'bindingDigest': canonical_digest(self.binding), 'nativeReferences': references,
            'workerResultDigest': canonical_digest(worker_result['agentResult'])}
        self.retain('import-selected-'+dispatch['actionId'], record)
        return record

    def reconcile_submission(self, action, dispatch, *, step):
        """Read a dispatched submission without requiring a returned worker reply."""
        require(self.eligible(action) and action['input']['name'] == SUBMIT,
                'cleanup reconciliation requires the original model submission intent')
        arguments = action['input'].get('arguments', {})
        require(arguments.get('workspaceId') == self.binding['workspaceId'] and
                dispatch.get('requestDigest') == canonical_digest({key: action[key] for key in ('name', 'input')}),
                'cleanup submission differs from its original model request/workspace')
        path = self.task_path(arguments.get('taskId'), arguments.get('revision'))
        answer = self.http.json(step+'-original-task', 'GET', path)
        task = data(answer)
        require(task.get('taskId') == arguments['taskId'] and task.get('revision') == arguments['revision'] and
                task.get('contentHash') == arguments.get('request', {}).get('taskContentHash'),
                'cleanup native task differs from original dispatched immutable task')
        self.validate_task(task)
        selection = {'actionId': dispatch['actionId'], 'requestDigest': dispatch['requestDigest'], 'tool': SUBMIT,
            'task': task, 'bindingDigest': canonical_digest(self.binding), 'nativeReferences': {'task': answer['reference']}}
        return self.observe(selection, step=step, cleanup=True)

    def observe(self, selected, *, step, cleanup=False):
        require(selected.get('bindingDigest') == canonical_digest(self.binding), 'import selection belongs to another exact native scope')
        task = selected['task']
        self.validate_task(task)
        path = self.task_path(task['taskId'], task['revision'])
        current_task = self.http.json(step+'-task', 'GET', path)
        require(data(current_task) == task, 'original immutable import task changed before status observation')
        answer = self.http.json(step+'-submission', 'GET', path+'/submission?'+urlencode({'contentHash': task['contentHash']}))
        receipt = data(answer)
        require(all(receipt.get(field) == task[key] for field, key in
                (('taskId', 'taskId'), ('taskRevision', 'revision'), ('taskContentHash', 'contentHash'))) and
                receipt.get('state') in ('not-prepared', 'prepared', 'accepted'), 'native submission belongs to another task or unknown state')
        observed = {'taskId': task['taskId'], 'submission': receipt, 'dataset': None,
            'nativeReferences': {'task': current_task['reference'], 'submission': answer['reference']}}
        if receipt['state'] != 'accepted':
            require(receipt.get('job') is None, 'prepared import cannot claim an actual accepted job')
        else:
            job = receipt.get('job')
            require(isinstance(job, dict) and job.get('jobId') == receipt.get('operationId') and
                    job.get('status') in ('queued', 'provisioning', 'running', 'succeeded', 'failed', 'cancelled') and
                    self.not_before <= instant(job.get('createdAt')) <= datetime.now(timezone.utc),
                    'native import acceptance lacks the original independently observed durable job')
            token(job.get('leaseId'), 'accepted import lease')
            if job['status'] == 'succeeded':
                require(job.get('outcomePending') is False and job.get('errorCode') is None and
                        instant(job['createdAt']) <= instant(job.get('completedAt')) <= datetime.now(timezone.utc) and
                        isinstance(job.get('output'), dict), 'successful native import still has unknown completion/output')
                observed['dataset'] = self.sql.ownership(job['output']) if cleanup else self.sql.observe(job['output'])
                if not cleanup:
                    features = observed['dataset']['features']
                    observed['features.exact'] = copy.deepcopy(features)
                    observed['geometry.crs'] = {'sourceSrid': job['output'].get('detectedSrid'), 'returnedSrid': features['srid'],
                        'coordinateDigest': canonical_digest([{'sourceId': row['id'], 'geometry': row['geometry']} for row in features['features']])[7:]}
            else:
                require(job.get('output') is None, 'incomplete or failed import cannot expose completed dataset output')
        self.retain(step+'-observation', observed)
        return observed
