"""Original funding and finite storage must precede controller publication."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import controller_image as publisher


class ControllerPublication(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.commands, self.order = [], []
        self.size = 2*1024*1024*1024
        self.retention = {'policySha256': 'sha256:'+'b'*64, 'expiryEligibilityDays': 14}
        self.original = {'version_id': 'actual-original-owner-version', 'sha256': 'c'*64}
        self.env = {'GITHUB_REPOSITORY': 'honua-io/honua-release', 'GITHUB_REF': 'refs/heads/trunk',
            'GITHUB_SHA': 'a'*40, 'GITHUB_RUN_ID': '42', 'GITHUB_RUN_ATTEMPT': '1',
            'PILOT_ACCOUNT_ID': '585192672263', 'PILOT_REGION': 'us-east-1',
            'PILOT_CONTROLLER_REPOSITORY': '585192672263.dkr.ecr.us-east-1.amazonaws.com/honua-pilot-unit-trusted-controller',
            'RUNNER_TEMP': self.temp.name}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.env, clear=True).start()
        patch.object(publisher, 'call', side_effect=self.call).start()
        patch.object(publisher, 'contract', return_value='sha256:'+'d'*64).start()
        self.budget = patch.object(publisher, 'budget', side_effect=self.admit).start()
        self.policy = patch.object(publisher, 'verify_retention', side_effect=self.verify).start()
        self.login = patch.object(publisher.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout=b'private-login-token')).start()

    def admit(self):
        self.order.append('original-budget')
        return self.original

    def verify(self, *args, **kwargs):
        self.assertEqual(kwargs['kind'], 'controller')
        self.order.append('actual-retention')
        return self.retention

    def call(self, args, **kwargs):
        self.commands.append(args)
        self.order.append(' '.join(args[:2]))
        if args[:3] == ['git', 'rev-parse', 'HEAD']:
            return self.env['GITHUB_SHA']
        if args[:2] == ['git', 'rev-parse']:
            return 'e'*40
        if args[:2] == ['git', 'archive']:
            Path(args[args.index('--output')+1]).write_bytes(b'actual-test-archive')
        if args[:3] == ['aws', 'sts', 'get-caller-identity']:
            return json.dumps({'Account': self.env['PILOT_ACCOUNT_ID']})
        if args[:3] == ['docker', 'image', 'inspect']:
            return str(self.size)
        if args[:3] == ['aws', 'ecr', 'describe-images']:
            return 'sha256:'+'f'*64
        return ''

    def test_original_admission_precedes_build_and_receipt_keeps_budget_size_expiry(self):
        publisher.main()
        self.assertLess(self.order.index('actual-retention'), self.order.index('original-budget'))
        self.assertLess(self.order.index('original-budget'), self.order.index('docker build'))
        self.assertEqual(self.policy.call_count, 2)
        self.assertEqual(self.budget.call_count, 1)
        value = json.loads((self.root/'hosted-pilot-controller-image.json').read_text())
        self.assertEqual(value['owner_budget'], self.original)
        self.assertEqual(value['image_bytes'], self.size)
        self.assertEqual(value['retention'], self.retention)
        smoke = next(row for row in self.commands if row[-1].endswith('/browser/smoke.mjs'))
        self.assertIn('--read-only', smoke)
        self.assertEqual(smoke[smoke.index('--network')+1], 'none')
        self.assertLess(self.commands.index(smoke), next(i for i,row in enumerate(self.commands) if row[:2] == ['docker', 'push']))
        self.assertNotIn('private-login-token', json.dumps(value))

    def test_missing_original_funding_cannot_build_or_obtain_registry_password(self):
        self.budget.side_effect = ValueError('original reservation missing')
        with self.assertRaisesRegex(ValueError, 'original reservation missing'):
            publisher.main()
        self.assertFalse(any(row[0] == 'docker' for row in self.commands))
        self.login.assert_not_called()

    def test_missing_actual_retention_cannot_consume_budget_or_build(self):
        self.policy.side_effect = ValueError('actual expiry missing')
        with self.assertRaisesRegex(ValueError, 'actual expiry missing'):
            publisher.main()
        self.budget.assert_not_called()
        self.assertFalse(any(row[0] == 'docker' for row in self.commands))

    def test_empty_and_oversized_images_cannot_publish(self):
        for size in (0, publisher.MAX_IMAGE_BYTES+1):
            self.size = size
            with self.subTest(size=size), self.assertRaisesRegex(ValueError, 'artifact-storage liability'):
                publisher.main()
        self.assertFalse(any(row[:2] in (['docker', 'push'], ['docker', 'login']) for row in self.commands))
        self.login.assert_not_called()

    def test_retention_drift_cannot_publish_or_obtain_registry_password(self):
        self.policy.side_effect = [self.retention, self.retention | {'expiryEligibilityDays': 9999}]
        with self.assertRaisesRegex(ValueError, 'expiry changed'):
            publisher.main()
        self.assertFalse(any(row[:2] == ['docker', 'push'] for row in self.commands))
        self.login.assert_not_called()
