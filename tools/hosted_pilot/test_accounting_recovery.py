"""Actual state-store publication followed by strictly GET-only receipt recovery."""
import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import budget_bootstrap
from accounting_snapshot import MAX_STATE_BYTES
from agent_e2e_pack import PackError, canonical_digest, digest_bytes
from agent_e2e_state import DurableExecution, StateStore
import test_budget_initialization as fixture


class AwsStore:
    """SDK-shaped storage; disabling writes also forbids lease acquisition."""
    def __init__(self, candidate, config):
        self.objects, self.row, self.calls, self.bodies = {}, {}, [], []
        self.readonly = False
        self.on_get_head = None
        self.on_get_object = None
        self.s3 = Mock(spec=['get_bucket_versioning', 'list_object_versions', 'put_object', 'get_object'])
        self.db = Mock(spec=['get_item', 'update_item'])
        self.s3.get_bucket_versioning.side_effect = self.versioning
        self.s3.list_object_versions.side_effect = self.list_versions
        self.s3.put_object.side_effect = self.put
        self.s3.get_object.side_effect = self.get
        self.db.get_item.side_effect = self.head
        self.db.update_item.side_effect = self.update
        self.store = StateStore(s3=self.s3, dynamodb=self.db, bucket=config['bucket'],
            table=config['table'], candidate_key=candidate, owner='original-initializer')

    def record(self, operation, args):
        self.calls.append((operation, copy.deepcopy(args)))
        if self.readonly and operation not in ('GetItem', 'GetObjectVersion'):
            raise AssertionError('recovery attempted '+operation)

    def versioning(self, **args):
        self.record('GetBucketVersioning', args)
        return {'Status': 'Enabled'}

    def list_versions(self, **args):
        self.record('ListObjectVersions', args)
        return {'Versions': [{'Key': key} for key in self.objects], 'IsTruncated': False}

    def put(self, **args):
        self.record('PutObject', args)
        assert args['Bucket'] == self.store.bucket and args['IfNoneMatch'] == '*'
        assert args['Key'] not in self.objects
        version = 'version-'+str(len(self.objects)+1)
        self.objects[args['Key']] = (version, args['Body'])
        return {'VersionId': version}

    def get(self, **args):
        self.record('GetObjectVersion', args)
        assert args['Bucket'] == self.store.bucket
        version, raw = self.objects[args['Key']]
        assert args['VersionId'] == version
        body = io.BytesIO(raw)
        self.bodies.append(body)
        response = {'VersionId': version, 'ContentLength': len(raw), 'Body': body}
        if self.on_get_object:
            self.on_get_object(args, response)
        return response

    def head(self, **args):
        self.record('GetItem', args)
        assert args == {'TableName': self.store.table, 'Key': self.store.key, 'ConsistentRead': True}
        if self.on_get_head:
            self.on_get_head()
        return {'Item': copy.deepcopy(self.row)}

    def update(self, **args):
        self.record('UpdateItem', args)
        assert args['TableName'] == self.store.table and args['Key'] == self.store.key
        expression, values = args['UpdateExpression'], args['ExpressionAttributeValues']
        if expression.startswith('SET leaseOwner'):
            assert 'leaseOwner' not in self.row
            self.row.update(leaseOwner=values[':owner'], leaseUntil=values[':until'],
                fence={'N': str(int(self.row.get('fence', {'N':'0'})['N'])+1)})
        else:
            assert self.row['leaseOwner'] == values[':owner'] and self.row['fence'] == values[':fence']
            if expression.startswith('REMOVE'):
                self.row.pop('leaseOwner'); self.row.pop('leaseUntil')
            else:
                for assignment in expression.removeprefix('SET ').split(', '):
                    field, value = assignment.split(' = ')
                    self.row[field] = values[value]
        return {'Attributes': copy.deepcopy(self.row)}

    def snapshot(self):
        return json.loads(self.objects[self.row['snapshotKey']['S']][1])

    def corrupt_snapshot(self, change):
        snapshot = self.snapshot()
        change(snapshot)
        key = self.row['snapshotKey']['S']
        version = self.objects[key][0]
        raw = json.dumps(snapshot, sort_keys=True, separators=(',', ':')).encode()
        self.objects[key] = version, raw
        self.row['snapshotDigest'] = {'S': digest_bytes(raw)}


class AccountingRecovery(unittest.TestCase):
    def setUp(self):
        self.original = fixture.InitialAccounting()
        self.original.setUp()
        self.aws = AwsStore(self.original.candidate, self.original.config['state'])
        self.original.store = self.aws.store
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def initialize(self, name, **kwargs):
        self.aws.readonly = kwargs.get('recover', False)
        start = len(self.aws.calls)
        try:
            return self.original.initialize(self.root/name, **kwargs)
        finally:
            if self.aws.readonly:
                self.assertTrue(all(op in ('GetItem', 'GetObjectVersion') for op, _ in self.aws.calls[start:]))

    def state(self):
        return self.aws.snapshot()['payload']

    def assert_one_generation(self):
        self.assertEqual(len(self.aws.objects), 2)
        self.assertEqual(self.aws.row['stateVersion'], {'N': '1'})

    def test_process_loss_after_commit_recovers_receipt_without_second_generation(self):
        def lose_receipt(*args, **kwargs):
            durable = DurableExecution(*args, **kwargs)
            durable.close()
            raise SystemExit('process exited after commit and before receipt')
        with self.assertRaises(SystemExit):
            self.initialize('lost.json', execution=lose_receipt)
        state, generation = self.state(), copy.deepcopy(self.original.store.generation)
        original_head = copy.deepcopy(self.aws.row)
        first_recovery_body = len(self.aws.bodies)
        receipt = self.initialize('recover.json', recover=True)
        self.assert_one_generation()
        self.assertEqual(self.state(), state)
        self.assertEqual(self.aws.row, original_head)
        self.assertEqual(receipt['generation'], generation)
        self.assertEqual(receipt['ledgerDigest'], canonical_digest(state['ledger']))
        self.assertEqual(receipt['bootstrapLedgerDigest'], canonical_digest(self.original.ledger))
        self.assertEqual(receipt['mode'], 'recovered')
        self.assertFalse(receipt['qualification'])
        self.assertEqual(receipt['modelExecution'], 'not-executed-by-recovery')
        again = self.initialize('recover-again.json', recover=True)
        self.assertEqual(receipt, again)
        self.assert_one_generation()
        self.assertTrue(all(body.closed for body in self.aws.bodies[first_recovery_body:]))
        self.assertFalse((self.root/'recover.json').exists())

    def test_recovery_preserves_later_liability_and_reports_current_digest(self):
        first = self.initialize('first.json')
        state = self.original.store.acquire()
        ledger = state['ledger']
        ledger['operations'].append({'id':'later-observed-hold','operationId':'later-operation',
            'purpose':'telemetry','kind':'infrastructure','amountUsd':'5','basisDigest':'sha256:'+'f'*64,
            'state':'reserved','actualUsd':None})
        self.original.store.commit(state)
        self.original.store.release()
        before = self.state()
        recovered = self.initialize('recover.json', recover=True)
        self.assertEqual(self.state(), before)
        self.assertEqual(recovered['generation'], first['generation'])
        self.assertEqual(recovered['ledgerDigest'], canonical_digest(ledger))
        self.assertNotEqual(recovered['ledgerDigest'], recovered['bootstrapLedgerDigest'])
        self.assertEqual(self.aws.row['stateVersion'], {'N': '2'})
        self.assertEqual(len(self.aws.objects), 3)

    def test_original_report_aging_cannot_mint_new_state_but_can_recover_committed_receipt(self):
        self.initialize('first.json')
        later = datetime.now(timezone.utc)+timedelta(hours=2)
        class Later(datetime):
            @classmethod
            def now(cls, tz=None): return later if tz else later.replace(tzinfo=None)
        before = self.state()
        with patch.object(budget_bootstrap, 'datetime', Later):
            with self.assertRaisesRegex(ValueError, 'stale, future or inconsistent'):
                self.initialize('not-a-new-initialization.json')
            recovered = self.initialize('recover.json', recover=True)
        self.assertEqual(recovered['mode'], 'recovered')
        self.assertEqual(self.state(), before)
        self.assert_one_generation()

    def test_recovery_of_absent_state_never_falls_back_to_initialization(self):
        with self.assertRaisesRegex(ValueError, 'absent or incomplete'):
            self.initialize('absent.json', recover=True)
        self.assertEqual(self.aws.row, {})
        self.assertEqual(self.aws.objects, {})

    def test_recovery_refuses_foreign_bootstrap_without_mutating_it(self):
        self.initialize('first.json')
        self.aws.corrupt_snapshot(lambda s: s['payload']['bootstrapImport'].update(artifactDigest='sha256:'+'f'*64))
        before = self.state()
        with self.assertRaisesRegex(ValueError, 'authenticated original bootstrap'):
            self.initialize('foreign.json', recover=True)
        self.assertEqual(self.state(), before)
        self.assert_one_generation()

    def test_recovery_cannot_erase_original_spend_or_liabilities(self):
        self.initialize('first.json')
        self.aws.corrupt_snapshot(lambda s: s['payload']['ledger']['operations'][0].update(amountUsd='0'))
        before = self.state()
        with self.assertRaises((ValueError, PackError)):
            self.initialize('reset.json', recover=True)
        self.assertEqual(self.state(), before)
        self.assert_one_generation()

    def test_recovery_requires_actual_generation_metadata_and_original_expiry(self):
        self.initialize('first.json')
        del self.aws.row['generationVersion']
        with self.assertRaisesRegex(ValueError, 'absent or incomplete'):
            self.initialize('unanchored.json', recover=True)
        self.original.config['expiresAt'] = '2000-01-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'missing or expired'):
            self.initialize('expired.json', recover=True)
        self.assert_one_generation()

    def test_version_digest_length_and_foreign_candidate_fail_closed(self):
        self.initialize('first.json')
        for field, value in [('VersionId', 'other-version'), ('ContentLength', MAX_STATE_BYTES+1),
                             ('ContentLength', 0), ('ContentLength', True)]:
            with self.subTest(field=field, value=value):
                self.aws.on_get_object = lambda args, response: response.update({field: value})
                with self.assertRaisesRegex(ValueError, 'version or bounded length'):
                    self.initialize('recover.json', recover=True)
                self.assertTrue(self.aws.bodies[-1].closed)
        self.aws.on_get_object = None
        before = copy.deepcopy(self.aws.row)
        for field, value in [('snapshotDigest', 'sha256:'+'f'*64), ('snapshotVersion', 'null'),
                             ('generationKey', 'agent-e2e/'+'b'*64+'/initialization.json')]:
            with self.subTest(field=field):
                self.aws.row = copy.deepcopy(before)
                self.aws.row[field] = {'S': value}
                with self.assertRaisesRegex(ValueError, 'digest|immutable version|outside the original candidate'):
                    self.initialize('recover.json', recover=True)
        self.aws.row = before
        original_objects = copy.deepcopy(self.aws.objects)
        for change in [lambda s: s.update(candidateKey='sha256:'+'b'*64), lambda s: s.update(version=True),
                       lambda s: s.update(generationDigest='sha256:'+'f'*64)]:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'original generation/version'):
                self.aws.objects = copy.deepcopy(original_objects)
                self.aws.row = copy.deepcopy(before)
                self.aws.corrupt_snapshot(change)
                self.initialize('recover.json', recover=True)

    def test_changed_head_refuses_receipt_but_lease_heartbeat_is_irrelevant(self):
        self.initialize('first.json')
        head = copy.deepcopy(self.aws.row)
        reads = 0
        def race():
            nonlocal reads
            reads += 1
            if reads == 2:
                self.aws.row['stateVersion'] = {'N': '2'}
        self.aws.on_get_head = race
        with self.assertRaisesRegex(ValueError, 'head advanced'):
            self.initialize('raced.json', recover=True)
        self.aws.row = head
        self.aws.on_get_head = lambda: self.aws.row.update(leaseUntil={'N': '99'}, leaseOwner={'S': 'live-controller'})
        self.assertEqual(self.initialize('heartbeat.json', recover=True)['mode'], 'recovered')

    def test_deleted_head_cannot_reinitialize_retained_history(self):
        self.initialize('first.json')
        originals = copy.deepcopy(self.aws.objects)
        self.aws.row = {}
        with self.assertRaisesRegex(ValueError, 'absent or incomplete'):
            self.initialize('head-lost.json', recover=True)
        self.assertEqual(self.aws.objects, originals)


if __name__ == '__main__':
    unittest.main()
