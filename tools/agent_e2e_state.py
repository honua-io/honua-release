"""Durable controller-only state with a fenced DynamoDB lease and S3 snapshots.

The bucket must have versioning enabled. Workers must have no access to either
store. A timed-out writer cannot publish its snapshot after its lease expires.
Unreferenced snapshots are retained for investigation, never treated as state.
"""
from __future__ import annotations

import json
import copy
import re
import time
import uuid
import threading
import argparse
from pathlib import Path

from agent_e2e_pack import PackError, canonical_digest, digest_bytes, load_json


def open_store(config, candidate_key):
    """Construct controller clients without embedding credentials in configuration."""
    if not isinstance(config, dict) or set(config) != {'region', 'bucket', 'table'}:
        raise PackError('invalid durable state configuration')
    if not all(isinstance(v, str) and v.strip() for v in config.values()):
        raise PackError('empty durable state configuration')
    import boto3
    from botocore.config import Config
    client_config = Config(connect_timeout=10, read_timeout=20, retries={'total_max_attempts':1})
    return StateStore(s3=boto3.client('s3', region_name=config['region'], config=client_config),
        dynamodb=boto3.client('dynamodb', region_name=config['region'], config=client_config),
        bucket=config['bucket'], table=config['table'], candidate_key=candidate_key)


def validate_retained_import_transfer(state, context, request, candidate):
    """Replay exact native task/job originals for the controller and private handoff."""
    from agent_e2e_import_database import PROFILE
    from agent_e2e_budget import validate as validate_budget
    validate_budget(state['ledger'])
    if state['ledger']['candidateKey'] != candidate or context.get('componentBindingDigest', candidate) != candidate:
        raise PackError('model import revalidation belongs to another candidate')
    scoped = [row for row in state['ledger']['entries'] if row['state'] == 'reserved' and
              all(row.get(field) == context[field] for field in ('campaignId', 'caseId', 'attempt')) and
              row.get('campaignMode', 'qualification') == context['campaignMode']]
    if not {'infrastructure', 'cleanup'} <= {row['kind'] for row in scoped}:
        raise PackError('model import revalidation lacks original live attempt liabilities')
    key = canonical_digest({field: context[field] for field in ('campaignId', 'caseId', 'attempt')})
    baseline = state.get('fixtureBaselines', {}).get(key)
    fields = {'schemaVersion', 'profile', 'candidateKey', 'database', 'baselineDigest',
              'expectedCleanupRole', 'tables', 'nativeImports'}
    if (set(request) != fields or request['schemaVersion'] != 1 or request['profile'] != PROFILE or
            request['candidateKey'] != candidate or baseline is None or
            request['baselineDigest'] != canonical_digest(baseline) or request['database'] != baseline['database'] or
            not isinstance(request['expectedCleanupRole'], str) or
            not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', request['expectedCleanupRole']) or
            not isinstance(request['tables'], list) or not 1 <= len(request['tables']) <= 16 or
            not isinstance(request['nativeImports'], list) or len(request['nativeImports']) != len(request['tables'])):
        raise PackError('model import transfer differs from original candidate/baseline/native output scope')
    before = {row['oid'] for row in baseline['snapshot']['relations']}
    tables = {}
    for table in request['tables']:
        if (set(table) != {'oid', 'schema', 'name', 'owner'} or type(table['oid']) is not int or table['oid'] <= 0 or
                table['oid'] in before or table['oid'] in tables or table['schema'] != 'honua_data' or
                any(not isinstance(table[field], str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', table[field])
                    for field in ('name', 'owner'))):
            raise PackError('model import transfer has preexisting/duplicate/ambiguous physical OIDs')
        tables[table['oid']] = table
    attempts = [attempt for attempt, launch in state.get('workerLaunches', {}).items()
                if all(launch.get('context', {}).get(field) == context[field]
                       for field in ('campaignId', 'caseId', 'attempt', 'campaignMode'))]
    originals = state.get('scenarioOperations', {})
    def read(reference):
        if not isinstance(reference, dict) or set(reference) != {'key', 'sha256'}:
            raise PackError('model import transfer requires original retained native GET references')
        response = originals.get(reference['key'])
        if not isinstance(response, dict) or canonical_digest(response) != reference['sha256']:
            raise PackError('model import native GET response is absent or changed')
        intent_ref = response.get('intentReference', {})
        intent = originals.get(intent_ref.get('key'))
        if (not isinstance(intent, dict) or canonical_digest(intent) != intent_ref.get('sha256') or
                intent.get('method') != 'GET' or intent.get('candidateKey') != candidate or
                intent.get('credentialKind') not in ('api-key', 'bearer') or
                response.get('candidateKey') != candidate or
                response.get('intentDigest') != canonical_digest(intent) or
                not any(intent.get('attemptId', '').startswith(attempt+'-') for attempt in attempts) or
                response.get('attemptId') != intent.get('attemptId') or
                response.get('response', {}).get('httpStatus') != 200):
            raise PackError('model import original is not an authenticated same-attempt native GET')
        raw = response['response'].get('body')
        if not isinstance(raw, dict) or raw.get('success', True) is False:
            raise PackError('model import original native body is unsuccessful')
        return raw.get('data', raw), intent
    used_oids, used_jobs = set(), set()
    for native in request['nativeImports']:
        if set(native) != {'taskId', 'taskRevision', 'taskContentHash', 'jobId', 'leaseId', 'tableOid',
                           'taskReference', 'submissionReference'} or native['tableOid'] not in tables:
            raise PackError('model import handoff lacks an exact task/job/relation mapping')
        task, task_intent = read(native['taskReference'])
        receipt, receipt_intent = read(native['submissionReference'])
        if (not isinstance(task, dict) or not isinstance(receipt, dict) or not isinstance(receipt.get('job'), dict) or
                not isinstance(receipt['job'].get('output'), dict) or type(native['taskRevision']) is not int or native['taskRevision'] < 1 or
                not isinstance(native['taskContentHash'], str) or not re.fullmatch(r'[0-9a-f]{64}', native['taskContentHash'])):
            raise PackError('model import original has no successful actual task/job/output binding')
        from urllib.parse import urlencode
        path = '/api/v1/agent/workspaces/'+str(task.get('workspaceId'))+'/dataset-import-tasks/'+str(native['taskId'])+'/versions/'+str(native['taskRevision'])
        job, physical = receipt.get('job', {}), receipt.get('job', {}).get('output', {})
        table = tables[native['tableOid']]
        if (task.get('taskId') != native['taskId'] or task.get('revision') != native['taskRevision'] or
                task.get('contentHash') != native['taskContentHash'] or receipt.get('taskId') != native['taskId'] or
                receipt.get('taskRevision') != native['taskRevision'] or receipt.get('taskContentHash') != native['taskContentHash'] or
                receipt.get('state') != 'accepted' or receipt.get('operationId') != native['jobId'] or
                job.get('jobId') != native['jobId'] or job.get('leaseId') != native['leaseId'] or
                job.get('status') != 'succeeded' or job.get('outcomePending') is not False or
                physical.get('schema') != table['schema'] or physical.get('physicalTableName') != table['name'] or
                task_intent.get('path') != path or receipt_intent.get('path') != path+'/submission?'+urlencode({'contentHash': native['taskContentHash']}) or
                task_intent.get('endpoint') != receipt_intent.get('endpoint') or
                task_intent.get('agentContext', {}).get('workspaceId') != task.get('workspaceId') or
                receipt_intent.get('agentContext') != task_intent.get('agentContext') or
                native['tableOid'] in used_oids or native['jobId'] in used_jobs):
            raise PackError('model import original task/job GETs do not corroborate exact actual physical output')
        used_oids.add(native['tableOid']); used_jobs.add(native['jobId'])
    return key


class StateStore:
    def __init__(self, *, s3, dynamodb, bucket, table, candidate_key, owner=None, clock=time.time):
        self.s3, self.db, self.bucket, self.table = s3, dynamodb, bucket, table
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', candidate_key):
            raise PackError('state requires a candidate digest')
        self.candidate = candidate_key
        self.owner = owner or str(uuid.uuid4())
        self.clock = clock
        self.version = None
        self.payload = None
        self.generation = None

    @property
    def key(self):
        return {'candidateKey': {'S': self.candidate}}

    @property
    def generation_key(self):
        return f"agent-e2e/{self.candidate.split(':')[1]}/initialization.json"

    def _initialize_generation(self):
        # S3's conditional write serializes initial publication independently of
        # the deletable DynamoDB head. No snapshot may precede this permanent
        # record, so an in-flight older writer cannot race a fresh allowance.
        marker = {'schemaVersion':1, 'candidateKey':self.candidate, 'generationId':str(uuid.uuid4())}
        data = json.dumps(marker, sort_keys=True, separators=(',', ':')).encode()
        response = self.s3.put_object(Bucket=self.bucket, Key=self.generation_key,
                                     Body=data, ContentType='application/json', IfNoneMatch='*')
        if not response.get('VersionId') or response['VersionId'] == 'null':
            raise PackError('initialization record was not versioned')
        generation = {'key':self.generation_key, 'version':response['VersionId'], 'digest':digest_bytes(data)}
        condition, values = self._condition()
        condition += ' AND attribute_not_exists(snapshotKey) AND attribute_not_exists(generationDigest)'
        values.update({':generationKey':{'S':generation['key']}, ':generationVersion':{'S':generation['version']},
                       ':generationDigest':{'S':generation['digest']}})
        self.db.update_item(TableName=self.table, Key=self.key, ConditionExpression=condition,
            UpdateExpression='SET generationKey = :generationKey, generationVersion = :generationVersion, generationDigest = :generationDigest',
            ExpressionAttributeValues=values)
        self.generation = generation

    def _load_generation(self, row):
        if not all(field in row for field in ('generationKey', 'generationVersion', 'generationDigest')):
            raise PackError('unanchored legacy state requires quiesced reconciliation; refusing automatic adoption')
        generation = {'key':row['generationKey']['S'], 'version':row['generationVersion']['S'],
                      'digest':row['generationDigest']['S']}
        if generation['key'] != self.generation_key:
            raise PackError('initialization record belongs to another candidate')
        response = self.s3.get_object(Bucket=self.bucket, Key=generation['key'], VersionId=generation['version'])
        data = response['Body'].read()
        marker = json.loads(data)
        if digest_bytes(data) != generation['digest'] or marker.get('candidateKey') != self.candidate:
            raise PackError('initialization record binding differs')
        self.generation = generation

    def acquire(self):
        if self.s3.get_bucket_versioning(Bucket=self.bucket).get('Status') != 'Enabled':
            raise PackError('controller state bucket must be versioned')
        now = int(self.clock())
        result = self.db.update_item(TableName=self.table, Key=self.key,
            UpdateExpression='SET leaseOwner = :owner, leaseUntil = :until ADD fence :one',
            ConditionExpression='attribute_not_exists(leaseOwner) OR leaseUntil < :now',
            ExpressionAttributeValues={':owner': {'S': self.owner}, ':until': {'N': str(now+120)},
                ':now': {'N': str(now)}, ':one': {'N': '1'}}, ReturnValues='ALL_NEW')
        self.fence = int(result['Attributes']['fence']['N'])
        self.version = int(result['Attributes'].get('stateVersion', {'N':'0'})['N'])
        return self.load()

    def _condition(self):
        return ('leaseOwner = :owner AND fence = :fence AND leaseUntil >= :now',
                {':owner': {'S': self.owner}, ':fence': {'N':str(self.fence)}, ':now': {'N':str(int(self.clock()))}})

    def renew(self):
        condition, values = self._condition()
        values[':until'] = {'N':str(int(self.clock())+120)}
        self.db.update_item(TableName=self.table, Key=self.key, ConditionExpression=condition,
            UpdateExpression='SET leaseUntil = :until', ExpressionAttributeValues=values)

    def load(self):
        self.generation, self.payload, self.version = None, None, None
        row = self.db.get_item(TableName=self.table, Key=self.key, ConsistentRead=True).get('Item', {})
        if row.get('leaseOwner', {}).get('S') != self.owner or int(row.get('fence', {'N':'-1'})['N']) != self.fence:
            raise PackError('controller state lease was lost')
        self.version = int(row.get('stateVersion', {'N':'0'})['N'])
        if 'snapshotKey' not in row:
            if self.version != 0:
                raise PackError('state history is missing; refusing reset')
            # Loss of the DynamoDB head must not turn retained S3 history into
            # a new candidate allowance. Include non-current versions and delete
            # markers: listing only current objects would miss a hidden history.
            history = self.s3.list_object_versions(Bucket=self.bucket,
                Prefix=f"agent-e2e/{self.candidate.split(':')[1]}/", MaxKeys=1)
            if history.get('Versions') or history.get('DeleteMarkers') or history.get('IsTruncated'):
                raise PackError('snapshot history exists without its state head; reconcile instead of resetting')
            return None
        self._load_generation(row)
        response = self.s3.get_object(Bucket=self.bucket, Key=row['snapshotKey']['S'], VersionId=row['snapshotVersion']['S'])
        data = response['Body'].read()
        if digest_bytes(data) != row['snapshotDigest']['S']:
            raise PackError('controller snapshot digest mismatch')
        state = json.loads(data)
        if (state['candidateKey'] != self.candidate or state['version'] != self.version
                or state.get('generationDigest') != self.generation['digest']):
            raise PackError('snapshot belongs to another candidate/version')
        self.payload = copy.deepcopy(state['payload'])
        return state['payload']

    def commit(self, payload):
        if self.version is None:
            raise PackError('acquire state before committing')
        canonical_digest(payload)
        if self.generation is None:
            if self.version != 0:
                raise PackError('acquire anchored state before committing')
            self._initialize_generation()
        preserve_history(self.payload, payload)
        state = {'candidateKey': self.candidate, 'version': self.version+1,
                 'generationDigest':self.generation['digest'], 'payload': payload}
        # Validate finite, JSON-serializable state before any write.
        canonical_digest(state)
        data = json.dumps(state, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        key = f"agent-e2e/{self.candidate.split(':')[1]}/{self.version+1}-{uuid.uuid4()}.json"
        response = self.s3.put_object(Bucket=self.bucket, Key=key, Body=data,
                                      ContentType='application/json', IfNoneMatch='*')
        if not response.get('VersionId') or response['VersionId'] == 'null':
            raise PackError('snapshot was not versioned')
        condition, values = self._condition()
        condition += ' AND generationDigest = :generationDigest'
        values[':generationDigest'] = {'S':self.generation['digest']}
        condition += ' AND (stateVersion = :previous OR (attribute_not_exists(stateVersion) AND :previous = :zero))'
        values.update({':previous':{'N':str(self.version)}, ':zero':{'N':'0'}, ':version':{'N':str(self.version+1)},
            ':key':{'S':key}, ':objectVersion':{'S':response['VersionId']}, ':digest':{'S':digest_bytes(data)}})
        self.db.update_item(TableName=self.table, Key=self.key, ConditionExpression=condition,
            UpdateExpression='SET stateVersion = :version, snapshotKey = :key, snapshotVersion = :objectVersion, snapshotDigest = :digest',
            ExpressionAttributeValues=values)
        self.version += 1
        self.payload = copy.deepcopy(payload)
        return digest_bytes(data)

    def release(self):
        condition, values = self._condition()
        self.db.update_item(TableName=self.table, Key=self.key, ConditionExpression=condition,
            UpdateExpression='REMOVE leaseOwner, leaseUntil', ExpressionAttributeValues=values)


def preserve_history(previous, current):
    """A new campaign cannot delete spending or reclassify retained attempts."""
    if previous is None:
        return
    for field in ('campaigns', 'attemptArtifacts', 'scenarioOperations', 'fixtureBaselines', 'fixtureOwnership',
                  'fixtureCleanup', 'fixtureTransferRequests', 'nativeCostAllocations', 'nativeOverheadAllocations', 'nativeBindings',
                  'studioBootstrapOperations', 'studioBootstrapFixtures', 'importTransferRequests'):
        for key, value in previous.get(field, {}).items():
            if canonical_digest(current.get(field, {}).get(key)) != canonical_digest(value):
                raise PackError('cannot rewrite or erase retained campaign evidence')
    for key, record in previous.get('workerLaunches', {}).items():
        if canonical_digest(current.get('workerLaunches', {}).get(key)) != canonical_digest(record):
            raise PackError('cannot rewrite or erase retained worker launch intent')
    for key, record in previous.get('workerActions', {}).items():
        if canonical_digest(current.get('workerActions', {}).get(key)) != canonical_digest(record):
            raise PackError('cannot rewrite or erase retained worker action evidence')
    if 'bootstrapImport' in current and 'bootstrapImport' not in previous:
        raise PackError('bootstrap import must be the first state, never a retrofit')
    if 'bootstrapImport' in previous and canonical_digest(previous['bootstrapImport']) != canonical_digest(current.get('bootstrapImport')):
        raise PackError('cannot rewrite or erase bootstrap import history')
    for key, old_rows in previous.get('journals', {}).items():
        new_rows = current.get('journals', {}).get(key, [])
        if len(new_rows) < len(old_rows) or canonical_digest(new_rows[:len(old_rows)]) != canonical_digest(old_rows):
            raise PackError('cannot rewrite or erase durable journal events')
    for key in ('ledger', 'registry'):
        if key not in previous:
            continue
        if key not in current:
            raise PackError('cannot erase durable candidate history')
        fields = ('entries', 'operations', 'paidAdmissions') if key == 'ledger' else ('attempts',)
        if canonical_digest({k:v for k,v in previous[key].items() if k not in fields}) != canonical_digest(
                {k:v for k,v in current[key].items() if k not in fields}):
            # The only accepted policy change is the fixed owner amendment,
            # whose embedded prior snapshot must be this exact retained ledger.
            from agent_e2e_budget import validate as validate_budget
            amendment = current[key].get('policyAmendment', {}) if key == 'ledger' else {}
            if (key != 'ledger' or previous[key].get('schemaVersion') not in (1, 2)
                    or current[key].get('schemaVersion') != 3
                    or canonical_digest(amendment.get('priorLedger')) != canonical_digest(previous[key])):
                raise PackError('cannot change durable candidate identity or budget policy')
            validate_budget(current[key])
        for field in fields:
            old_rows, new_rows = previous[key].get(field, []), current[key].get(field, [])
            if len(new_rows) < len(old_rows):
                raise PackError('cannot truncate durable candidate history')
            for old, new in zip(old_rows, new_rows):
                if field == 'paidAdmissions':
                    if old['state'] == 'released':
                        if old != new: raise PackError('released admission history cannot change')
                    else:
                        immutable = set(old) - {'state', 'reservationIds', 'cleanupStatus'}
                        if (set(old) != set(new) or any(old[k] != new[k] for k in immutable)
                                or new['state'] not in ('active', 'released')
                                or new['state'] == 'active' and (new['reservationIds'] is not None or new['cleanupStatus'] is not None)):
                            raise PackError('paid admission cannot be reset, replaced or extended')
                    continue
                if key == 'registry' or old.get('state') == 'settled':
                    if canonical_digest(old) != canonical_digest(new):
                        raise PackError('retained outcome or charge cannot change')
                else:
                    immutable = set(old) - {'state', 'actualUsd'}
                    if set(new) != set(old) or any(old[k] != new[k] for k in immutable):
                        raise PackError('reservation identity or ceiling changed')
                    if new['state'] not in ('reserved', 'settled'):
                        raise PackError('invalid reservation transition')

    if current.get('ledger', {}).get('schemaVersion') == 3:
        from agent_e2e_budget import validate_admissions
        validate_admissions(current['ledger'])


class DurableExecution:
    """Synchronously persist each liability and journal event before continuing."""
    def __init__(self, store, ledger_path, *, initialize=False, bootstrap=None):
        from agent_e2e_budget import update
        from agent_e2e_telemetry import atomic_write
        self.store, self.path = store, Path(ledger_path)
        if bootstrap is not None and not initialize:
            raise PackError('bootstrap import requires explicit initialization')
        if bootstrap is not None:
            from agent_e2e_bootstrap import validate_import
            imported_ledger = validate_import(bootstrap, store.candidate)
        state = store.acquire()
        try:
            if state is not None and bootstrap is not None:
                raise PackError('candidate history already exists; bootstrap cannot replace it')
            if state is None:
                if not initialize:
                    raise PackError('new candidate state requires explicit initialization')
                if self.path.exists():
                    raise PackError('cannot initialize durable state from an existing local ledger')
                if bootstrap is None:
                    # Offline callers can exercise a fresh store. Production
                    # CLI initialization requires authenticated bootstrap input.
                    update(self.path, 'init', candidate_key=store.candidate)
                    state = {'ledger':load_json(self.path), 'journals':{}}
                else:
                    state = {'ledger':imported_ledger, 'journals':{}, 'bootstrapImport':copy.deepcopy(bootstrap)}
                store.commit(state)
            if self.path.exists():
                # A failed remote settlement may leave a newer local charge.
                # Never overwrite it with an older snapshot and lose the cost.
                preserve_history({'ledger':load_json(self.path)}, {'ledger':state['ledger']})
            atomic_write(self.path, json.dumps(state['ledger'], allow_nan=False).encode())
        except Exception:
            try:
                store.release()
            except Exception:
                pass  # Leave an expired/lost lease to its fenced recovery path.
            raise
        self.state = state
        self.stop = threading.Event()
        self.heartbeat_error = None
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()

    def restore_journals(self, root):
        """Recover retained attempts without inventing end events or retry rights.

        An existing local file must agree exactly with the durable events. A
        divergent/torn local journal is retained for explicit reconciliation.
        Validate the entire recovery set before publishing any recovered file.
        """
        from agent_e2e_telemetry import atomic_write, read_events, validate_events
        root = Path(root)
        if root.is_symlink():
            raise PackError('refusing symlink journal root')
        pending = []
        for key, rows in self.state['journals'].items():
            validate_events(rows)
            expected_key = canonical_digest({k:rows[0][k] for k in ('campaignId','caseId','attempt')})
            if key != expected_key:
                raise PackError('durable journal identity mismatch')
            path = root/(key.split(':', 1)[1]+'.jsonl')
            if path.is_symlink():
                raise PackError('refusing symlink journal')
            if path.exists():
                if canonical_digest(read_events(path)) != canonical_digest(rows):
                    raise PackError('local journal differs from durable history; retain for reconciliation')
            else:
                pending.append((path, rows))
        if self.heartbeat_error:
            raise PackError('durable controller lease renewal failed')
        for path, rows in pending:
            atomic_write(path, ''.join(json.dumps(row, sort_keys=True, allow_nan=False)+'\n' for row in rows).encode())
        return len(pending)

    def _heartbeat(self):
        while not self.stop.wait(30):
            try:
                self.store.renew()
            except Exception as exc:
                self.heartbeat_error = type(exc).__name__
                return

    def _persist(self):
        if self.heartbeat_error:
            raise PackError('durable controller lease renewal failed')
        self.store.commit(self.state)

    def update(self, path, operation, **kwargs):
        from agent_e2e_budget import update
        if Path(path).resolve() != self.path.resolve():
            raise PackError('unexpected candidate ledger path')
        try:
            result = update(path, operation, **kwargs)
        finally:
            # Settlements can write an overrun then raise. Retain that charge.
            current = json.loads(self.path.read_text(encoding='utf-8'))
            if current != self.state['ledger']:
                self.state['ledger'] = current
                self._persist()
        return result

    def event(self, row):
        key = canonical_digest({k:row[k] for k in ('campaignId','caseId','attempt')})
        rows = self.state['journals'].setdefault(key, [])
        if len(rows) != row['sequence']:
            raise PackError('attempt already exists in durable history')
        if row['previousDigest'] != (canonical_digest(rows[-1]) if rows else None):
            raise PackError('durable journal chain is broken')
        if rows and rows[-1]['event'] == 'end':
            raise PackError('durable attempt is already complete')
        rows.append(row)
        self._persist()

    def retain_worker_launch(self, record):
        """Commit prepaid launch identity before the one allowed runtime start."""
        from agent_e2e_worker_lifecycle import validate_reservations
        self._require_live_lease()
        validate_reservations(record, self.state['ledger'])
        launches = self.state.setdefault('workerLaunches', {})
        if record['attemptId'] in launches:
            raise PackError('worker launch already retained; observe without restarting')
        reserved_ids = set(record['reservationIds'].values())
        if any(reserved_ids & set(old['reservationIds'].values()) for old in launches.values()):
            raise PackError('worker reservation already funds a retained launch')
        launches[record['attemptId']] = copy.deepcopy(record)
        # A failed acknowledgement leaves the intent in memory. A fresh process
        # must recover the durable head; neither path can assume no start occurred.
        self._persist()

    def require_worker_launch(self, record, *, launching=False):
        self._require_live_lease()
        retained = self.state.get('workerLaunches', {}).get(record['attemptId'])
        if canonical_digest(retained) != canonical_digest(record):
            raise PackError('worker launch differs from retained intent')
        if launching:
            from agent_e2e_worker_lifecycle import validate_reservations
            validate_reservations(record, self.state['ledger'])

    def _require_live_lease(self):
        if self.stop.is_set() or self.heartbeat_error:
            raise PackError('durable controller lease is unavailable')
        self.store.renew()

    def _fixture_identity(self, context):
        from agent_e2e_budget import validate
        self._require_live_lease()
        validate(self.state['ledger'])
        if (context.get('componentBindingDigest') != self.store.candidate or
                type(context.get('attempt')) is not int or context['attempt'] not in (1, 2) or
                context.get('campaignMode') not in ('development', 'qualification')):
            raise PackError('fixture belongs to another candidate or invalid attempt')
        identity = {key: context[key] for key in ('campaignId', 'caseId', 'attempt')}
        rows = [row for row in self.state['ledger']['entries'] if row['state'] == 'reserved' and
                all(row.get(key) == value for key, value in identity.items()) and
                row.get('campaignMode', 'qualification') == context['campaignMode']]
        if not {'infrastructure', 'cleanup'} <= {row['kind'] for row in rows}:
            raise PackError('fixture effects lack retained infrastructure and cleanup liability')
        return canonical_digest(identity)

    def retain_fixture_baseline(self, context, baseline):
        """Persist independent identity/baseline before any fixture creation."""
        key = self._fixture_identity(context)
        if baseline.get('candidateKey') != self.store.candidate:
            raise PackError('fixture baseline belongs to another candidate')
        baselines = self.state.setdefault('fixtureBaselines', {})
        if key in baselines:
            raise PackError('fixture baseline already retained; reconcile without repeating setup')
        baselines[key] = copy.deepcopy(baseline)
        self._persist()
        return {'key': key, 'sha256': canonical_digest(baseline)}

    def retain_fixture_ownership(self, context, record):
        """Bind actual created OIDs/revisions to the independently retained baseline."""
        key = self._fixture_identity(context)
        if (record.get('candidateKey') != self.store.candidate or
                key not in self.state.get('fixtureBaselines', {}) or
                canonical_digest(record.get('baseline')) != canonical_digest(self.state['fixtureBaselines'][key])):
            raise PackError('fixture ownership differs from retained independent baseline')
        owned = self.state.setdefault('fixtureOwnership', {})
        if key in owned:
            raise PackError('fixture ownership already retained; do not rebind resources')
        owned[key] = copy.deepcopy(record)
        self._persist()

    def retain_fixture_transfer_request(self, context, request):
        """Bind one exact observed owner handoff before the private provisioner."""
        key = self._fixture_identity(context)
        baseline = self.state.get('fixtureBaselines', {}).get(key)
        tables = request.get('tables')
        if (set(request) != {'schemaVersion', 'candidateKey', 'database', 'baselineDigest', 'expectedCleanupRole', 'tables'} or
                request.get('schemaVersion') != 1 or request.get('candidateKey') != self.store.candidate or
                baseline is None or request.get('baselineDigest') != canonical_digest(baseline) or
                request.get('database') != baseline.get('database') or
                not isinstance(request.get('expectedCleanupRole'), str) or
                not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', request['expectedCleanupRole']) or
                not isinstance(tables, list) or len(tables) != 3):
            raise PackError('fixture transfer differs from exact independently retained baseline')
        before = {row['oid'] for row in baseline['snapshot']['relations']}
        for table in tables:
            if (not isinstance(table, dict) or set(table) != {'oid', 'schema', 'name', 'owner'} or
                    type(table['oid']) is not int or table['oid'] <= 0 or table['oid'] in before or
                    table['schema'] != 'honua_data' or
                    any(not isinstance(table[field], str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', table[field])
                        for field in ('name', 'owner'))):
                raise PackError('fixture transfer table identity is ambiguous or preexisting')
        if len({table['oid'] for table in tables}) != 3 or len({table['name'] for table in tables}) != 3:
            raise PackError('fixture transfer contains duplicate relations')
        requests = self.state.setdefault('fixtureTransferRequests', {})
        if key in requests:
            raise PackError('fixture transfer already retained; observe without redispatch')
        requests[key] = copy.deepcopy(request)
        self._persist()
        return {'key': key, 'sha256': canonical_digest(request)}

    def retain_import_transfer_request(self, context, request):
        """Admit bounded exact native import outputs, preserving legacy P10 three."""
        key = self._fixture_identity(context)
        if validate_retained_import_transfer(self.state, context, request, self.store.candidate) != key:
            raise PackError('model import request identity changed during revalidation')
        requests = self.state.setdefault('importTransferRequests', {})
        if key in requests:
            raise PackError('model import transfer already retained; observe without redispatch')
        requests[key] = copy.deepcopy(request)
        self._persist()
        return {'key': key, 'sha256': canonical_digest(request)}

    def require_fixture_ownership(self, context, record):
        key = self._fixture_identity(context)
        retained = self.state.get('fixtureOwnership', {}).get(key)
        if retained is None or canonical_digest(retained) != canonical_digest(record):
            raise PackError('cleanup differs from exact retained fixture ownership')

    def retain_fixture_cleanup(self, context, kind, value):
        key = self._fixture_identity(context)
        record = self.state.get('fixtureOwnership', {}).get(key)
        if (kind not in ('intent', 'observation') or record is None or
                value.get('ownershipDigest') != canonical_digest(record) or
                value.get('candidateKey') != self.store.candidate):
            raise PackError('cleanup evidence differs from retained fixture ownership')
        self.require_fixture_ownership(context, record)
        references = self.state.setdefault('fixtureCleanup', {})
        if kind == 'observation' and key+'/intent' not in references:
            raise PackError('cleanup observation lacks retained before-effect intent')
        key += '/'+kind
        if key in references:
            raise PackError('fixture cleanup stage already retained; reconcile without repeating effects')
        references[key] = copy.deepcopy(value)
        self._persist()
        return {'key': key, 'sha256': canonical_digest(value)}

    def retain_worker_action(self, record, launch):
        """Publish reply references before returning any public action result."""
        self.require_worker_launch(launch)
        if record.get('launchDigest') != canonical_digest(launch):
            raise PackError('worker action evidence has another launch identity')
        action_id = record.get('actionId')
        if not isinstance(action_id, str) or not re.fullmatch(r'[0-9a-f]{64}', action_id):
            raise PackError('worker action lacks controller dispatch identity')
        actions = self.state.setdefault('workerActions', {})
        if action_id in actions:
            raise PackError('worker action evidence already retained; reconcile without redispatch')
        actions[action_id] = copy.deepcopy(record)
        self._persist()

    def close(self):
        self.stop.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive() or self.heartbeat_error:
            raise PackError('lease renewal unavailable; retain lease for recovery')
        self.store.release()


def main(argv=None):
    """Recover existing history for reporting; never initialize or run a model."""
    from agent_e2e_telemetry import atomic_write
    parser = argparse.ArgumentParser(description=main.__doc__)
    for name in ('state-config', 'ledger', 'journal-root', 'output'):
        parser.add_argument('--'+name, required=True, type=Path)
    parser.add_argument('--candidate-key', required=True)
    args = parser.parse_args(argv)
    execution = None
    try:
        store = open_store(load_json(args.state_config), args.candidate_key)
        execution = DurableExecution(store, args.ledger)
        count = execution.restore_journals(args.journal_root)
        result = {'status':'recovered', 'candidateKey':args.candidate_key,
                  'journalsRestored':count, 'journalsRetained':len(execution.state['journals']), 'qualification':False}
    except Exception as exc:
        result = {'status':'blocked', 'errorType':type(exc).__name__, 'qualification':False}
    finally:
        if execution is not None:
            try:
                execution.close()
            except Exception as exc:
                result = {'status':'blocked', 'errorType':type(exc).__name__,
                          'reason':'durable-state-release-failed', 'qualification':False}
    atomic_write(args.output, (json.dumps(result, indent=2)+'\n').encode())
    return 0 if result['status'] == 'recovered' else 1


if __name__ == '__main__':
    raise SystemExit(main())
