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
