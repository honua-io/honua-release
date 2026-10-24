"""Synthetic publication originals and actual bounded subprocess transport controls."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

import test_worker_runtime_publication as publication_fixtures
import worker_publication_installation as installation


class InstallationPublication(unittest.TestCase):
    def setUp(self):
        self.original = publication_fixtures.WorkerRuntimePublication()
        self.original.setUp()
        self.configuration = {'installed_slot': {'runtime_id': 'runtime'},
            'worker_publication': self.original.binding, 'worker_image': self.original.image,
            'public_bundle_digest': self.original.public}
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.work = Path(self.directory.name)
        self.guard = Mock()
        self.download = Mock(return_value=self.original.archive)

    def prepare(self):
        return installation.prepare(self.configuration, self.work, guard=self.guard,
            github=self.original.github, download=self.download)

    def test_real_authenticator_retains_exact_raw_zip_and_index_then_replays(self):
        result = self.prepare()
        self.assertEqual(result['index'], self.original.index)
        self.assertEqual((self.work/installation.ARCHIVE_NAME).read_bytes(), self.original.archive)
        self.assertEqual((self.work/installation.INDEX_FILE).read_bytes(), self.original.index_raw)
        self.assertEqual((self.work/installation.ARCHIVE_NAME).stat().st_mode & 0o777, 0o600)
        replay, archive, index = installation.saved(self.configuration, self.work,
            github=self.original.github, guard=self.guard)
        self.assertEqual((archive, index), (self.original.archive, self.original.index_raw))
        self.assertEqual(replay, result)

    def test_default_static_path_does_not_download_or_require_new_fields(self):
        self.assertIsNone(installation.prepare({}, self.work, guard=self.guard, download=self.download))
        self.download.assert_not_called()

    def test_missing_publication_refuses_before_download(self):
        del self.configuration['worker_publication']
        with self.assertRaises(ValueError): self.prepare()
        self.download.assert_not_called()

    def test_bad_original_zip_cannot_become_retained_installation(self):
        self.download.return_value = b'caller-replacement'
        with self.assertRaisesRegex(ValueError, 'original artifact digest'): self.prepare()
        self.assertEqual(list(self.work.iterdir()), [])

    def test_incomplete_publisher_refuses_before_original_files_are_written(self):
        self.original.run['status'] = 'in_progress'
        with self.assertRaisesRegex(ValueError, 'successful protected publisher'): self.prepare()
        self.assertEqual(list(self.work.iterdir()), [])

    def test_same_byte_preparation_recovery_is_read_only_and_changed_file_refuses(self):
        self.prepare()
        before = (self.work/installation.ARCHIVE_NAME).stat().st_mtime_ns
        self.prepare()
        self.assertEqual((self.work/installation.ARCHIVE_NAME).stat().st_mtime_ns, before)
        (self.work/installation.INDEX_FILE).write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'already differs'): self.prepare()

    def test_private_symlink_is_not_overwritten_or_read_as_original(self):
        target = self.work/'unrelated'; target.write_bytes(b'unchanged')
        (self.work/installation.ARCHIVE_NAME).symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'already differs'): self.prepare()
        self.assertEqual(target.read_bytes(), b'unchanged')

    def test_index_tamper_is_refused_even_with_unchanged_original_zip(self):
        self.prepare()
        (self.work/installation.INDEX_FILE).write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'ZIP member'):
            installation.saved(self.configuration, self.work, github=self.original.github, guard=self.guard)

    def test_retained_runtime_proof_is_derived_from_authenticated_objects(self):
        prefix = 'agent-e2e/candidate/installed-workers/runtime/slot/11/2/'
        refs = {field: {'bucket': 'original', 'key': prefix+name,
            'versionId': field+'-version', 'sha256': 'sha256:'+'a'*64}
            for field, name in (('publication', installation.ARCHIVE_NAME), ('indexReference', installation.INDEX_FILE))}
        binding = {'binding': self.configuration['worker_publication'], **refs}
        objects = {refs['publication']['key']: self.original.archive,
            refs['indexReference']['key']: self.original.index_raw}
        def read(reference, *, guard):
            guard()
            return objects[reference['key']]
        def verify(value):
            return installation.verify_retained(value, configuration=self.configuration,
                prefix=prefix, bucket='original', read_bytes=read,
                github=self.original.github, guard=self.guard)
        proof = verify(binding)
        self.assertEqual(set(proof), {'image', 'publication', 'indexReference', 'index', 'runtimeBundleDigest'})
        self.assertEqual(proof['index'], self.original.index)
        for field in ('publication', 'indexReference'):
            changed = copy.deepcopy(binding); changed[field]['key'] = 'another-run/'+field
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'output custody'):
                verify(changed)
        changed = copy.deepcopy(binding); changed['binding']['runAttempt'] += 1
        with self.assertRaisesRegex(ValueError, 'checked configuration'): verify(changed)


class ActualTransportBounds(unittest.TestCase):
    def invoke(self, code, **kwargs):
        return installation.bounded_output([sys.executable, '-c', code], guard=Mock(), **kwargs)

    def test_actual_binary_stdout_is_preserved_exactly(self):
        self.assertEqual(self.invoke('import sys;sys.stdout.buffer.write(bytes(range(256)))'), bytes(range(256)))

    def test_actual_oversized_writer_is_terminated(self):
        with self.assertRaisesRegex(ValueError, 'byte bound'):
            self.invoke('import os;\nwhile True: os.write(1,b"x"*4096)', limit=1024)

    def test_actual_silent_hang_is_terminated_at_original_call_deadline(self):
        with self.assertRaisesRegex(ValueError, 'deadline'):
            self.invoke('import time;time.sleep(30)', timeout=0.1)

    def test_actual_closed_stdout_does_not_hide_a_still_running_download(self):
        with self.assertRaisesRegex(ValueError, 'deadline'):
            self.invoke('import os,time;os.close(1);time.sleep(30)', timeout=0.1)

    def test_actual_failed_transport_cannot_return_partial_bytes_or_stderr(self):
        with self.assertRaisesRegex(ValueError, '^worker publication original download failed$'):
            self.invoke('import sys;sys.stderr.write("private-signed-url");sys.stdout.write("partial");sys.exit(1)')


if __name__ == '__main__':
    unittest.main()
