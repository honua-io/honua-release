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
from pathlib import Path

from agent_e2e_pack import PackError, canonical_digest, digest_bytes


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

    @property
    def key(self):
        return {'candidateKey': {'S': self.candidate}}

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
        row = self.db.get_item(TableName=self.table, Key=self.key, ConsistentRead=True).get('Item', {})
        if row.get('leaseOwner', {}).get('S') != self.owner or int(row.get('fence', {'N':'-1'})['N']) != self.fence:
            raise PackError('controller state lease was lost')
        self.version = int(row.get('stateVersion', {'N':'0'})['N'])
        if 'snapshotKey' not in row:
            if self.version != 0:
                raise PackError('state history is missing; refusing reset')
            return None
        response = self.s3.get_object(Bucket=self.bucket, Key=row['snapshotKey']['S'], VersionId=row['snapshotVersion']['S'])
        data = response['Body'].read()
        if digest_bytes(data) != row['snapshotDigest']['S']:
            raise PackError('controller snapshot digest mismatch')
        state = json.loads(data)
        if state['candidateKey'] != self.candidate or state['version'] != self.version:
            raise PackError('snapshot belongs to another candidate/version')
        self.payload = copy.deepcopy(state['payload'])
        return state['payload']

    def commit(self, payload):
        if self.version is None:
            raise PackError('acquire state before committing')
        preserve_history(self.payload, payload)
        state = {'candidateKey': self.candidate, 'version': self.version+1, 'payload': payload}
        # Validate finite, JSON-serializable state before any write.
        canonical_digest(state)
        data = json.dumps(state, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        key = f"agent-e2e/{self.candidate.split(':')[1]}/{self.version+1}-{uuid.uuid4()}.json"
        response = self.s3.put_object(Bucket=self.bucket, Key=key, Body=data,
                                      ContentType='application/json', IfNoneMatch='*')
        if not response.get('VersionId') or response['VersionId'] == 'null':
            raise PackError('snapshot was not versioned')
        condition, values = self._condition()
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
    for key, old_rows in previous.get('journals', {}).items():
        new_rows = current.get('journals', {}).get(key, [])
        if len(new_rows) < len(old_rows) or canonical_digest(new_rows[:len(old_rows)]) != canonical_digest(old_rows):
            raise PackError('cannot rewrite or erase durable journal events')
    for key in ('ledger', 'registry'):
        if key not in previous:
            continue
        if key not in current:
            raise PackError('cannot erase durable candidate history')
        field = 'entries' if key == 'ledger' else 'attempts'
        if canonical_digest({k:v for k,v in previous[key].items() if k != field}) != canonical_digest(
                {k:v for k,v in current[key].items() if k != field}):
            raise PackError('cannot change durable candidate identity or budget policy')
        old_rows, new_rows = previous[key][field], current[key][field]
        if len(new_rows) < len(old_rows):
            raise PackError('cannot truncate durable candidate history')
        for old, new in zip(old_rows, new_rows):
            if key == 'registry' or old.get('state') == 'settled':
                if canonical_digest(old) != canonical_digest(new):
                    raise PackError('retained outcome or charge cannot change')
            else:
                immutable = set(old) - {'state', 'actualUsd'}
                if set(new) != set(old) or any(old[k] != new[k] for k in immutable):
                    raise PackError('reservation identity or ceiling changed')
                if new['state'] not in ('reserved', 'settled'):
                    raise PackError('invalid reservation transition')


class DurableExecution:
    """Synchronously persist each liability and journal event before continuing."""
    def __init__(self, store, ledger_path, *, initialize=False):
        from agent_e2e_budget import update
        self.store, self.path = store, Path(ledger_path)
        state = store.acquire()
        if state is None:
            if not initialize:
                store.release()
                raise PackError('new candidate state requires explicit initialization')
            if self.path.exists():
                store.release()
                raise PackError('cannot initialize durable state from an existing local ledger')
            update(self.path, 'init', candidate_key=store.candidate)
            state = {'ledger':json.loads(self.path.read_text()), 'journals':{}}
            store.commit(state)
        self.state = state
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state['ledger']), encoding='utf-8')
        self.stop = threading.Event()
        self.heartbeat_error = None
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()

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

    def close(self):
        self.stop.set()
        self.thread.join(timeout=10)
        if self.thread.is_alive() or self.heartbeat_error:
            raise PackError('lease renewal unavailable; retain lease for recovery')
        self.store.release()
