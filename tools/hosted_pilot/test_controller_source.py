"""Actual git-index/file checks for the trusted, separate controller image."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import controller_image
import controller_source


class ControllerSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path.cwd()
        self.addCleanup(os.chdir, self.cwd)
        os.chdir(self.tmp.name)
        subprocess.run(['git', 'init', '--quiet'], check=True)
        self.file = Path('reviewed.py')
        self.file.write_text('reviewed source\n')
        subprocess.run(['git', 'add', '.'], check=True)
        self.tree = controller_source.tree()
        self.source, self.archive = 'a' * 40, 'b' * 64
        self.receipt = {'schema': 'honua.hosted-controller-source/v1', 'source_sha': self.source, 'tree_id': self.tree, 'archive_sha256': self.archive, 'tracked_files_sha256': controller_source.source_digest()}

    def verify(self):
        controller_source.verify(self.receipt, self.source, self.tree, self.archive)

    def test_readonly_runtime_verification_preserves_index(self):
        index = Path('.git/index').read_bytes()
        Path('.git/index').chmod(0o444)
        self.file.chmod(0o444)
        self.verify()
        self.assertEqual(Path('.git/index').read_bytes(), index)

    def test_actual_source_bytes_and_executable_modes_are_verified(self):
        self.file.write_text('substituted source\n')
        with self.assertRaisesRegex(ValueError, 'bytes differ'):
            self.verify()
        self.file.write_text('reviewed source\n')
        self.file.chmod(0o755)
        with self.assertRaisesRegex(ValueError, 'executable mode changed'):
            self.verify()

    def test_index_and_receipt_substitution_are_refused(self):
        self.file.write_text('new source\n')
        subprocess.run(['git', 'add', '.'], check=True)
        with self.assertRaisesRegex(ValueError, 'receipt identity mismatch'):
            self.verify()

    def test_mutable_source_and_wrong_archive_refused(self):
        for source, archive in [('trunk', self.archive), (self.source, 'c' * 64)]:
            with self.subTest(source=source, archive=archive):
                with self.assertRaises(ValueError):
                    controller_source.verify(self.receipt, source, self.tree, archive)


class ControllerImageRefusals(unittest.TestCase):
    def test_registry_account_region_and_namespace_are_bound_before_login(self):
        source = 'a' * 40
        env = {'GITHUB_REPOSITORY': 'honua-io/honua-release', 'GITHUB_REF': 'refs/heads/trunk', 'GITHUB_SHA': source, 'PILOT_ACCOUNT_ID': '123456789012', 'PILOT_REGION': 'us-east-1'}
        for repository in ['999999999999.dkr.ecr.us-east-1.amazonaws.com/honua-pilot-case-trusted-controller', '123456789012.dkr.ecr.us-west-2.amazonaws.com/honua-pilot-case-trusted-controller', '123456789012.dkr.ecr.us-east-1.amazonaws.com/public-demo']:
            with self.subTest(repository=repository), patch.dict(os.environ, env | {'PILOT_CONTROLLER_REPOSITORY': repository}, clear=True), patch.object(controller_image, 'call', return_value=source) as call:
                with self.assertRaisesRegex(ValueError, 'outside reviewed'):
                    controller_image.main()
                call.assert_called_once_with(['git', 'rev-parse', 'HEAD'], capture=True)


if __name__ == '__main__':
    unittest.main()
