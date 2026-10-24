"""A worker's original reserved cost cannot be lent to new runtime bindings."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from worker_allowance import basis, verify


def require(condition, reason):
    if not condition: raise ValueError(reason)


class WorkerAllowance(unittest.TestCase):
    def setUp(self):
        self.candidate = 'sha256:'+'a'*64
        self.config = {'candidate_digest': self.candidate, 'region': 'us-east-1', 'account_id': '123456789012', 'attempt_id': 'actual-attempt',
                       'issued_epoch': 100, 'lifetime_seconds': 6000, 'worker_image': 'actual-image@sha256:'+'b'*64,
                       'mcp_configuration': {'secret_arn': 'actual-attempt-secret', 'version_id': 'original-version'}}
        marker = {'candidateKey': self.candidate, 'generationId': 'actual-original-generation'}
        self.objects = {}
        def encode(key, value):
            raw = json.dumps(value, sort_keys=True).encode();self.objects[key] = raw
            return 'sha256:'+hashlib.sha256(raw).hexdigest()
        prefix = 'agent-e2e/'+'a'*64+'/'
        generation = encode(prefix+'initialization.json', marker)
        self.config['accounting'] = {'state': {'region': 'us-east-1', 'bucket': 'honua-agent-'+'a'*24+'-123456789012', 'table': 'honua-agent-'+'a'*24+'-state'},
            'generationDigest': generation, 'bootstrapArtifactDigest': 'sha256:'+'c'*64,
            'operation': {'operationId': 'worker-actual-attempt', 'liabilityBasisDigest': basis(self.config),
                          'reservationIds': {'infrastructure': 'actual-infra', 'cleanup': 'actual-cleanup'}},
            'maximumUsd': {'infrastructure': '5', 'cleanup': '2'}}
        rows = [{'id': 'actual-infra' if kind == 'infrastructure' else 'actual-cleanup', 'operationId': 'worker-actual-attempt', 'purpose': 'shared-environment',
            'kind': kind, 'amountUsd': amount, 'basisDigest': basis(self.config), 'state': 'reserved', 'actualUsd': None}
            for kind, amount in [('infrastructure', '5'), ('cleanup', '2')]]
        ledger = {'schemaVersion': 2, 'candidateKey': self.candidate, 'ceilingUsd': '500', 'cleanupReserveUsd': '50', 'entries': [], 'operations': rows}
        self.snapshot = {'candidateKey': self.candidate, 'generationDigest': generation, 'version': 7,
                         'payload': {'ledger': ledger, 'bootstrapImport': {'artifactDigest': 'sha256:'+'c'*64}}}
        snapshot = encode(prefix+'original.json', self.snapshot)
        self.row = {k: {'S': v} for k,v in {'generationDigest': generation, 'generationKey': prefix+'initialization.json',
            'generationVersion': 'actual-generation-version', 'snapshotKey': prefix+'original.json', 'snapshotVersion': 'actual-snapshot-version',
            'snapshotDigest': snapshot}.items()}
        self.row['stateVersion'] = {'N': '7'}
        self.calls = []
    def aws(self, *args):
        self.calls.append(args)
        if args[:2] == ('dynamodb', 'get-item'): return {'Item': self.row}
        if args[:2] == ('s3api', 'get-object'):
            Path(args[-1]).write_bytes(self.objects[args[args.index('--key')+1]])
            return {'VersionId': args[args.index('--version-id')+1]}
        raise AssertionError('mutation or unrelated AWS operation attempted')
    def verify(self):
        with tempfile.TemporaryDirectory() as directory:
            return verify(self.config, directory, aws=self.aws, require=require)
    def test_original_versions_bind_actual_fixed_attempt_without_any_write(self):
        receipt = self.verify()
        self.assertEqual(receipt['operationId'], 'worker-actual-attempt')
        self.assertEqual([call[:2] for call in self.calls], [('dynamodb', 'get-item'), ('s3api', 'get-object'), ('s3api', 'get-object')])
        self.assertTrue(all('--version-id' in call for call in self.calls[1:]))
    def test_mcp_version_image_epoch_and_attempt_cannot_spend_original_operation(self):
        original = copy.deepcopy(self.config)
        for field, value in [('worker_image', 'new-image'), ('issued_epoch', 101), ('lifetime_seconds', 5999),
                             ('attempt_id', 'other-attempt'), ('mcp_configuration', {'secret_arn': 'actual-attempt-secret', 'version_id': 'new-version'})]:
            self.config = {**copy.deepcopy(original), field: value}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'distinct original operation/basis'):
                self.verify()
    def test_alternate_namespace_is_refused_before_any_native_read(self):
        self.config['accounting']['state']['bucket'] += '-fresh'
        with self.assertRaisesRegex(ValueError, 'substitute another accounting namespace'): self.verify()
        self.assertEqual(self.calls, [])

    def test_missing_generation_never_creates_an_allowance(self):
        self.row = {}
        with self.assertRaisesRegex(ValueError, 'never initialize another allowance'): self.verify()
        self.assertEqual(len(self.calls), 1)
    def test_changed_original_snapshot_bytes_are_refused(self):
        self.objects[self.row['snapshotKey']['S']] += b' '
        with self.assertRaisesRegex(ValueError, 'snapshot binding differs|snapshot differs'): self.verify()
    def test_returned_s3_version_must_match_the_original_version(self):
        aws = self.aws
        def wrong_version(*args):
            value = aws(*args)
            if args[:2] == ('s3api', 'get-object'): value['VersionId'] = 'substituted-version'
            return value
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, 'immutable version differs'):
            verify(self.config, directory, aws=wrong_version, require=require)

    def test_original_cleanup_liability_must_remain_reserved(self):
        self.snapshot['payload']['ledger']['operations'][1].update(state='settled', actualUsd='1')
        raw = json.dumps(self.snapshot, sort_keys=True).encode()
        self.objects[self.row['snapshotKey']['S']] = raw
        self.row['snapshotDigest']['S'] = 'sha256:'+hashlib.sha256(raw).hexdigest()
        with self.assertRaisesRegex(ValueError, 'retained infrastructure/cleanup liability'): self.verify()
