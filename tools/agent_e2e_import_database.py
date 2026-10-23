"""Exact model-import output ownership and DROP RESTRICT cleanup.

Native task/submission GETs supply actual physical outputs. A separate checked
provisioner transfers only those independently observed OIDs to the cleanup
role. This adapter never changes ownership, resets schemas, uses CASCADE or
touches preexisting tables; any unknown dependency rolls back every prior drop.
"""
from __future__ import annotations

import copy

from agent_e2e_fixture_database import FixtureDatabase, ident, require
from agent_e2e_pack import canonical_digest
from agent_e2e_scenario_operations import token

PROFILE = 'honua.model-import-owned-tables/v1'


class ImportedFixtureDatabase(FixtureDatabase):
    def previously_cleaned(self, record, observation):
        require(record.get('profile') == PROFILE and record.get('candidateKey') == self.config['candidateKey'] and
                observation.get('ownershipDigest') == canonical_digest(record) and
                observation.get('candidateKey') == self.config['candidateKey'], 'prior import absence receipt changed ownership/candidate')
        with self.connection() as (connection, identity):
            require(record['baseline']['database'] == {key: identity[key] for key in record['baseline']['database']},
                    'prior import absence database changed')
            current = self.snapshot(connection)
        require(not {row['oid'] for row in record['tables']} & {row['oid'] for row in current['relations']},
                'previously cleaned original relation OID is still present')
        request = record.get('ownershipRequest')
        if request is None:
            return {}
        tables = {row['oid']: row for row in record['tables']}
        return {native['jobId']: {'native': copy.deepcopy(native), 'table': copy.deepcopy(tables[native['tableOid']]),
            'ownershipDigest': canonical_digest(record), 'observationDigest': canonical_digest(observation)}
            for native in request['nativeImports']}

    def observe_import_tables(self, baseline, observations, *, partial=False):
        require(baseline.get('candidateKey') == self.config['candidateKey'] and
                isinstance(observations, list) and 1 <= len(observations) <= 16,
                'import ownership requires exact independent baseline and completed native outputs')
        before = {row['oid'] for row in baseline['snapshot']['relations']}
        with self.connection() as (connection, identity):
            require(baseline['database'] == {key: identity[key] for key in baseline['database']},
                    'import ownership database changed from original baseline')
            snapshot = self.snapshot(connection)
        tables, imports = [], []
        for observed in observations:
            receipt, dataset = observed.get('submission'), observed.get('dataset')
            require(isinstance(receipt, dict) and receipt.get('state') == 'accepted' and isinstance(dataset, dict),
                    'prepared/unobserved imports cannot select ownership targets')
            job = receipt.get('job', {})
            require(job.get('status') == 'succeeded' and job.get('outcomePending') is False and
                    job.get('jobId') == receipt.get('operationId') and isinstance(job.get('output'), dict),
                    'ownership requires actual completed accepted job output')
            table = dataset.get('table', {})
            physical = job['output']
            require(table.get('schema') == physical.get('schema') == 'honua_data' and
                    table.get('name') == physical.get('physicalTableName') and type(table.get('oid')) is int and
                    table['oid'] not in before and table.get('marker') == 'honua:import-owned:v1',
                    'import ownership physical relation differs from original native job/SQL observations')
            ident(table['name']); ident(table['owner'])
            current = [row for row in snapshot['relations'] if row['schema'] == 'honua_data' and row['name'] == table['name']]
            expected = {key: table[key] for key in ('oid', 'schema', 'name', 'owner')}
            require(current == [expected], 'import relation OID/owner changed before private handoff')
            references = observed.get('nativeReferences', {})
            require(set(references) == {'task', 'submission'}, 'import ownership lacks native original task and accepted-job GET references')
            for reference in references.values():
                require(isinstance(reference, dict) and set(reference) == {'key', 'sha256'} and reference['key'],
                        'invalid original import readback custody reference')
            tables.append(expected)
            imports.append({'taskId': token(receipt.get('taskId'), 'owned import task'),
                'taskRevision': receipt['taskRevision'], 'taskContentHash': receipt['taskContentHash'],
                'jobId': token(job['jobId'], 'owned import job'), 'leaseId': token(job.get('leaseId'), 'owned import lease'),
                'tableOid': table['oid'], 'taskReference': references['task'], 'submissionReference': references['submission']})
        require(len({row['oid'] for row in tables}) == len(tables) and len({row['jobId'] for row in imports}) == len(imports) and
                (partial or {row['oid'] for row in snapshot['relations']} - before == {row['oid'] for row in tables}),
                'import handoff contains duplicate or unrelated new physical output')
        return {'schemaVersion': 1, 'profile': PROFILE, 'candidateKey': self.config['candidateKey'],
            'database': copy.deepcopy(baseline['database']), 'baselineDigest': canonical_digest(baseline),
            'expectedCleanupRole': self.config['cleanupRole'], 'tables': tables, 'nativeImports': imports}

    def preserve_unowned(self, snapshot, baseline, record):
        """Allow untouched new relations while preserving every original object."""
        preservation = self.without_owned(snapshot, record)
        require({key: value for key, value in preservation.items() if key != 'relations'} ==
                {key: value for key, value in baseline['snapshot'].items() if key != 'relations'} and
                all(row in preservation['relations'] for row in baseline['snapshot']['relations']),
                'partial import cleanup changed an original shared catalog/source relation')
        return preservation

    def ownership_empty(self, baseline):
        """Observe absence of proven outputs without inventing an owned table."""
        require(baseline.get('candidateKey') == self.config['candidateKey'], 'empty import cleanup baseline changed candidate')
        with self.connection() as (connection, identity):
            require(baseline['database'] == {key: identity[key] for key in baseline['database']}, 'empty import database identity changed')
            prepared = self.snapshot(connection)
        record = {'schemaVersion': 1, 'profile': PROFILE, 'candidateKey': self.config['candidateKey'],
            'baseline': copy.deepcopy(baseline), 'prepared': prepared, 'tables': [], 'ownershipRequest': None}
        record['preservation'] = self.preserve_unowned(prepared, baseline, record)
        return record

    def ownership_imports(self, baseline, request, *, partial=False):
        require(set(request) == {'schemaVersion', 'profile', 'candidateKey', 'database', 'baselineDigest',
                'expectedCleanupRole', 'tables', 'nativeImports'} and request.get('schemaVersion') == 1 and
                isinstance(request['tables'], list) and 1 <= len(request['tables']) <= 16 and
                len(request['nativeImports']) == len(request['tables']) and
                request.get('profile') == PROFILE and request.get('candidateKey') == self.config['candidateKey'] and
                request.get('baselineDigest') == canonical_digest(baseline) and request.get('database') == baseline['database'] and
                request.get('expectedCleanupRole') == self.config['cleanupRole'], 'import ownership differs from original retained handoff')
        with self.connection() as (connection, identity):
            require(baseline['database'] == {key: identity[key] for key in baseline['database']}, 'import ownership database identity changed')
            prepared = self.snapshot(connection)
        tables = []
        old_oids = {row['oid'] for row in baseline['snapshot']['relations']}
        for previous in request['tables']:
            require(previous['oid'] not in old_oids, 'import cleanup refuses preexisting relation OIDs')
            rows = [row for row in prepared['relations'] if all(row[key] == previous[key] for key in ('oid', 'schema', 'name'))]
            require(len(rows) == 1 and rows[0]['owner'] == self.config['cleanupRole'],
                    'native imported relation has not been transferred to exact cleanup role')
            tables.append(rows[0])
        require(len({row['oid'] for row in tables}) == len(tables), 'duplicate imported output ownership')
        record = {'schemaVersion': 1, 'profile': PROFILE, 'candidateKey': self.config['candidateKey'],
            'baseline': copy.deepcopy(baseline), 'prepared': prepared, 'tables': tables, 'ownershipRequest': copy.deepcopy(request)}
        if partial:
            record['preservation'] = self.preserve_unowned(prepared, baseline, record)
        else:
            require(self.without_owned(prepared, record) == baseline['snapshot'], 'model import changed unrelated shared catalog or source state')
        return record

    def without_owned(self, snapshot, record):
        result = copy.deepcopy(snapshot)
        owned = {row['oid'] for row in record['tables']}
        result['relations'] = [row for row in result['relations'] if row['oid'] not in owned]
        return result

    def cleanup(self, record, *, authorize, retain, recover=False):
        from psycopg import sql
        require(record.get('profile') == PROFILE and record.get('candidateKey') == self.config['candidateKey'],
                'import cleanup lacks exact retained native physical ownership')
        preservation = record.get('preservation', record['baseline']['snapshot'])
        require(self.preserve_unowned(record['prepared'], record['baseline'], record) == preservation,
                'retained partial cleanup preservation differs from exact prepared objects')
        authorize(record)
        intent = {'schemaVersion': 1, 'candidateKey': self.config['candidateKey'], 'ownershipDigest': canonical_digest(record)}
        acknowledgement = retain('intent', intent)
        require(isinstance(acknowledgement, dict) and acknowledgement.get('sha256') == canonical_digest(intent), 'import cleanup intent not retained')
        with self.connection(cleanup=True) as (connection, cleanup_identity):
            before = self.snapshot(connection)
            committed = recover and before == preservation
            if not committed:
                # A prior COMMIT may have succeeded without its final receipt.
                # Only the exact retained full OID/owner set can be dropped;
                # partial disappearance or a replacement remains a refusal.
                require(all(table in before['relations'] for table in record['tables']),
                        'original import cleanup relation set changed or is partially absent')
                for table in sorted(record['tables'], key=lambda row: row['oid']):
                    connection.execute(sql.SQL('LOCK TABLE {} IN ACCESS EXCLUSIVE MODE').format(sql.Identifier(table['schema'], table['name'])))
            require(connection.execute("SELECT 1 FROM pg_event_trigger WHERE evtenabled <> 'D' LIMIT 1").fetchone() is None,
                    'import cleanup refuses unreviewed DDL event triggers')
            current = self.snapshot(connection)
            require(current == preservation if committed else self.without_owned(current, record) == preservation and
                    all(table in current['relations'] for table in record['tables']), 'owned import OIDs/owners or unrelated state changed')
            authorize(record)
            if not committed:
                for table in record['tables']:
                    connection.execute(sql.SQL('DROP TABLE {} RESTRICT').format(sql.Identifier(table['schema'], table['name'])))
            require(self.snapshot(connection) == preservation, 'import transaction failed to preserve exact unrelated objects')
            authorize(record)
        with self.connection() as (connection, observer_identity):
            observed = self.snapshot(connection)
            require(observer_identity['principal'] != cleanup_identity['principal'] and observed == preservation,
                    'independent import SQL observer found remaining or changed resources')
        result = {'schemaVersion': 1, 'candidateKey': self.config['candidateKey'], 'ownershipDigest': canonical_digest(record),
            'cleanupPrincipal': cleanup_identity['principal'], 'observerPrincipal': observer_identity['principal'],
            'baselineDigest': canonical_digest(observed), 'resourceIds': [row['schema']+'.'+row['name'] for row in observed['relations']
                if row['oid'] not in {old['oid'] for old in record['baseline']['snapshot']['relations']}], 'qualification': False}
        acknowledgement = retain('observation', result)
        require(isinstance(acknowledgement, dict) and acknowledgement.get('sha256') == canonical_digest(result), 'import absence not retained')
        return result
