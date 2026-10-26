"""Worker publication retains original runtime bytes after funded real controls."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_e2e_pack import canonical_digest, digest_bytes
from bundle_agent_e2e_container import runtime_index
import worker_image as publisher


class WorkerPublication(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.commands, self.order = [], []
        self.size = 4*1024*1024
        self.retention = {'policySha256': 'sha256:'+'b'*64, 'expiryEligibilityDays': 14}
        self.original = {'version_id': 'original-owner-version', 'sha256': 'c'*64}
        self.claim = {'bucket': 'original-plans', 'key': 'hosted-pilot/reservations/worker-image/original.json',
            'versionId': 'original-claim-version', 'sha256': 'sha256:'+'d'*64}
        self.env = {'GITHUB_REPOSITORY': 'honua-io/honua-release', 'GITHUB_REF': 'refs/heads/trunk',
            'GITHUB_SHA': 'a'*40, 'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '2',
            'PILOT_ACCOUNT_ID': '585192672263', 'PILOT_REGION': 'us-east-1',
            'PILOT_WORKER_REPOSITORY': '585192672263.dkr.ecr.us-east-1.amazonaws.com/honua-pilot-unit-worker',
            'RUNNER_TEMP': self.temp.name}
        self.config = {'schemaVersion': 1, 'caseId': 'unit-A', 'pack': {'path': 'pack.json'},
            'knowledge': {'sourceRepository': publisher.REPOSITORY, 'sourceRevision': 'd'*40}}
        (self.root/'pack.json').write_text('{}')
        self.index = runtime_index()
        self.public_raw = b'{"unit":"public manifest"}\n'
        self.index['files']['public/public-manifest.json'] = digest_bytes(self.public_raw)
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.env, clear=True).start()
        patch.object(publisher, 'ROOT', self.root).start()
        patch.object(publisher, 'configuration', return_value=self.config).start()
        patch.object(publisher, 'export', return_value=self.config['knowledge']).start()
        patch.object(publisher, 'bundle', side_effect=self.bundle).start()
        patch.object(publisher, 'call', side_effect=self.call).start()
        self.budget = patch.object(publisher, 'budget', side_effect=self.admit).start()
        self.claim_build = patch.object(publisher, 'claim_publication', side_effect=self.claim_once).start()
        self.policy = patch.object(publisher, 'verify_retention', side_effect=self.verify).start()
        self.login = patch.object(publisher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout=b'private-login-token')).start()

    def admit(self):
        self.order.append('original-budget')
        return self.original

    def claim_once(self, original):
        self.assertEqual(original, self.original)
        self.order.append('physical-build-claim')
        return self.claim

    def verify(self, *args, **kwargs):
        self.order.append('actual-retention')
        return self.retention

    def bundle(self, context, **kwargs):
        (context/'public').mkdir(parents=True)
        (context/'public/public-manifest.json').write_bytes(self.public_raw)
        return self.index

    def call(self, args, **kwargs):
        self.commands.append(args)
        self.order.append(' '.join(args[:2]))
        if args == ['git', 'rev-parse', 'HEAD']: return self.env['GITHUB_SHA']
        if args[:2] == ['git', '-C']: return self.config['knowledge']['sourceRevision']
        if args[:3] == ['aws', 'sts', 'get-caller-identity']: return json.dumps({'Account': self.env['PILOT_ACCOUNT_ID']})
        if args[:3] == ['docker', 'image', 'inspect']: return str(self.size)
        if args[:3] == ['aws', 'ecr', 'describe-images']: return 'sha256:'+'f'*64
        return ''

    def test_two_originals_keep_exact_runtime_budget_and_image_after_container_controls(self):
        publisher.main()
        metadata_raw = (self.root/'hosted-pilot-worker-image.json').read_bytes()
        metadata = json.loads(metadata_raw)
        index_raw = (self.root/'runtime-index.json').read_bytes()
        self.assertEqual(json.loads(index_raw), self.index)
        self.assertEqual(metadata['runtimeBundleDigest'], canonical_digest(self.index))
        self.assertEqual(metadata['publicBundleDigest'], digest_bytes(self.public_raw))
        self.assertEqual(metadata['ownerBudget'], self.original)
        self.assertEqual(metadata['publicationClaim'], self.claim)
        self.assertEqual(metadata['retention'], self.retention)
        self.assertEqual(metadata['image'], self.env['PILOT_WORKER_REPOSITORY']+'@sha256:'+'f'*64)
        self.assertFalse(metadata['qualification'])
        self.assertEqual(metadata['modelExecution'], 'not-run')
        self.assertEqual(self.budget.call_count, 1)
        self.assertEqual(self.policy.call_count, 2)
        self.assertLess(self.order.index('original-budget'), self.order.index('docker build'))
        self.assertLess(self.order.index('original-budget'), self.order.index('physical-build-claim'))
        self.assertLess(self.order.index('physical-build-claim'), self.order.index('docker build'))
        smoke = next(row for row in self.commands if row[:2] == ['docker', 'run'])
        self.assertIn('--read-only', smoke)
        self.assertEqual(smoke[smoke.index('--network')+1], 'none')
        self.assertLess(self.commands.index(smoke), next(i for i, row in enumerate(self.commands) if row[:2] == ['docker', 'push']))
        self.assertNotIn(b'private-login-token', metadata_raw+index_raw)
        self.assertFalse(any(row[:2] in (['aws', 's3api'], ['aws', 's3']) for row in self.commands))

    def test_missing_original_funding_prevents_build_and_publication(self):
        self.budget.side_effect = ValueError('original reservation missing')
        with self.assertRaisesRegex(ValueError, 'original reservation missing'): publisher.main()
        self.assertFalse(any(row[0] == 'docker' for row in self.commands))
        self.login.assert_not_called()
        self.assertFalse((self.root/'runtime-index.json').exists())

    def test_retention_failure_cannot_consume_budget(self):
        self.policy.side_effect = ValueError('expiry missing')
        with self.assertRaisesRegex(ValueError, 'expiry missing'): publisher.main()
        self.budget.assert_not_called()
        self.login.assert_not_called()

    def test_repeated_or_ambiguous_physical_claim_prevents_any_build(self):
        self.claim_build.side_effect = ValueError('worker publication is already claimed')
        with self.assertRaisesRegex(ValueError, 'already claimed'): publisher.main()
        self.assertFalse(any(row[0] == 'docker' for row in self.commands))
        self.login.assert_not_called()

    def test_empty_and_oversized_images_cannot_publish(self):
        for size in (0, 512*1024*1024+1):
            self.size = size
            with self.subTest(size=size), self.assertRaisesRegex(ValueError, 'artifact-storage liability'): publisher.main()
        self.assertFalse(any(row[:2] in (['docker', 'push'], ['docker', 'login']) for row in self.commands))
        self.login.assert_not_called()
        self.assertFalse((self.root/'hosted-pilot-worker-image.json').exists())

    def test_retention_drift_cannot_push_or_retain_publication(self):
        self.policy.side_effect = [self.retention, self.retention | {'expiryEligibilityDays': 9999}]
        with self.assertRaisesRegex(ValueError, 'expiry changed'): publisher.main()
        self.assertFalse(any(row[:2] == ['docker', 'push'] for row in self.commands))
        self.assertFalse((self.root/'runtime-index.json').exists())

    def test_runtime_or_public_manifest_drift_refuses_before_build(self):
        self.index['files']['public/public-manifest.json'] = 'sha256:'+'e'*64
        with self.assertRaisesRegex(ValueError, 'exact public manifest'): publisher.main()
        self.assertFalse(any(row[0] == 'docker' for row in self.commands))
        self.login.assert_not_called()

    def test_workflow_retains_both_originals_in_the_same_named_artifact(self):
        import yaml
        workflow = yaml.safe_load((Path(publisher.__file__).resolve().parents[2]/'.github/workflows/hosted-pilot-worker-image.yml').read_text())
        artifacts = [step for step in workflow['jobs']['build']['steps'] if step.get('uses', '').startswith('actions/upload-artifact@')]
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]['with']['path'].splitlines(), [
            '${{ runner.temp }}/hosted-pilot-worker-image.json', '${{ runner.temp }}/runtime-index.json'])
        self.assertEqual(artifacts[0]['with']['name'], 'hosted-pilot-worker-image-${{ github.run_id }}-${{ github.run_attempt }}')
        self.assertEqual(artifacts[0]['with']['if-no-files-found'], 'error')


class SinglePhysicalBuildClaim(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = json.dumps({'reservation_id': 'original-reservation'}).encode()
        (self.root/'pilot-budget.json').write_bytes(self.raw)
        self.original = {'version_id': 'original-version', 'sha256': digest_bytes(self.raw)[7:]}
        self.env = {'RUNNER_TEMP': self.temp.name, 'GITHUB_SHA': 'a'*40, 'GITHUB_RUN_ID': '42',
            'GITHUB_RUN_ATTEMPT': '1', 'PILOT_WORKER_REPOSITORY': 'original-worker-repository',
            'PILOT_PLAN_BUCKET': 'original-plans', 'PILOT_PLAN_KMS_ARN': 'arn:aws:kms:us-east-1:585192672263:key/original'}
        self.objects = {}
        self.commands = []
        self.ambiguous = False
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.env, clear=True).start()
        self.run = patch.object(publisher.subprocess, 'run', side_effect=self.put).start()

    def put(self, args, **kwargs):
        self.commands.append(args)
        self.assertEqual(args[:3], ['aws', 's3api', 'put-object'])
        self.assertEqual(args[args.index('--if-none-match')+1], '*')
        self.assertEqual(args[args.index('--server-side-encryption')+1], 'aws:kms')
        key = args[args.index('--key')+1]
        if key in self.objects:
            return subprocess.CompletedProcess(args, 1, stdout=b'', stderr=b'PreconditionFailed')
        self.objects[key] = Path(args[args.index('--body')+1]).read_bytes()
        if self.ambiguous:
            return subprocess.CompletedProcess(args, 1, stdout=b'', stderr=b'connection lost after write')
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({'VersionId': 'immutable-original',
            'ServerSideEncryption': 'aws:kms', 'SSEKMSKeyId': self.env['PILOT_PLAN_KMS_ARN']}).encode())

    def test_one_original_attempt_gets_one_immutable_claim(self):
        reference = publisher.claim_publication(self.original)
        raw = self.objects[reference['key']]
        self.assertEqual(reference['key'], 'hosted-pilot/reservations/worker-image/original-reservation.json')
        self.assertEqual(reference['sha256'], digest_bytes(raw))
        self.assertEqual(reference['versionId'], 'immutable-original')
        self.assertEqual(json.loads(raw)['runAttempt'], '1')
        self.assertEqual(json.loads(raw)['ownerBudget'], self.original)

    def test_same_attempt_and_later_rerun_cannot_reclaim(self):
        publisher.claim_publication(self.original)
        for attempt in ('1', '2'):
            os.environ['GITHUB_RUN_ATTEMPT'] = attempt
            with self.subTest(attempt=attempt), self.assertRaisesRegex(ValueError, 'already claimed'):
                publisher.claim_publication(self.original)
        self.assertEqual(len(self.objects), 1)
        self.assertEqual(json.loads(next(iter(self.objects.values())))['runAttempt'], '1')

    def test_ambiguous_write_cannot_be_treated_as_fresh_credit(self):
        self.ambiguous = True
        with self.assertRaisesRegex(ValueError, 'ambiguous'): publisher.claim_publication(self.original)
        self.ambiguous = False
        with self.assertRaisesRegex(ValueError, 'already claimed'): publisher.claim_publication(self.original)
        self.assertEqual(len(self.objects), 1)

    def test_changed_original_ledger_refuses_before_claim(self):
        (self.root/'pilot-budget.json').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'reservation bytes changed'): publisher.claim_publication(self.original)
        self.run.assert_not_called()

    def test_missing_immutable_version_or_wrong_encryption_refuses(self):
        for result in ({}, {'VersionId': 'null'}, {'VersionId': 'v', 'ServerSideEncryption': 'AES256'},
            {'VersionId': 'v', 'ServerSideEncryption': 'aws:kms', 'SSEKMSKeyId': 'foreign-key'}):
            self.run.side_effect = None
            self.run.return_value = subprocess.CompletedProcess([], 0, stdout=json.dumps(result).encode())
            with self.subTest(result=result), self.assertRaisesRegex(ValueError, 'immutable encrypted version'):
                publisher.claim_publication(self.original)


if __name__ == '__main__':
    unittest.main()
