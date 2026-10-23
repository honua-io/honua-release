"""Archive custody rejects traversal/link substitution and proves actual Git trees."""
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import controller_archive as archive


class ControllerArchives(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def tar(self, name, *, kind=tarfile.REGTYPE, link=''):
        target = io.BytesIO()
        with tarfile.open(fileobj=target, mode='w') as stream:
            entry = tarfile.TarInfo(name)
            entry.type, entry.linkname = kind, link
            stream.addfile(entry)
        return target.getvalue()

    def test_bundle_is_deterministic_and_preserves_bytes(self):
        source = self.root / 'bundle'
        source.mkdir()
        (source / 'campaign.json').write_bytes(b'{"fixed":true}\n')
        first = archive.bundle_bytes(source)
        (source / 'campaign.json').touch()
        self.assertEqual(first, archive.bundle_bytes(source))
        restored = archive.extract(first, self.root / 'restored', limit=archive.BUNDLE_LIMIT)
        self.assertEqual((restored / 'campaign.json').read_bytes(), b'{"fixed":true}\n')

    def test_links_devices_absolute_and_parent_paths_are_refused(self):
        for n, data in enumerate([
            self.tar('../outside'), self.tar('/absolute'), self.tar('safe/../../outside'),
            self.tar('link', kind=tarfile.SYMTYPE, link='../outside'),
            self.tar('hard', kind=tarfile.LNKTYPE, link='other'),
            self.tar('device', kind=tarfile.CHRTYPE), self.tar('windows\\path'),
        ]):
            with self.subTest(index=n), self.assertRaises(ValueError):
                archive.extract(data, self.root / str(n), limit=archive.SOURCE_LIMIT)
        self.assertFalse((self.root / 'outside').exists())

    def test_existing_destination_and_oversized_archive_refuse_before_extraction(self):
        with self.assertRaisesRegex(ValueError, 'fresh'):
            archive.extract(self.tar('file'), self.root, limit=archive.SOURCE_LIMIT)
        with self.assertRaisesRegex(ValueError, 'bounded'):
            archive.extract(self.tar('file'), self.root / 'new', limit=10)
        self.assertFalse((self.root / 'new').exists())

    def test_duplicate_file_is_refused_without_partial_extraction(self):
        target = io.BytesIO()
        with tarfile.open(fileobj=target, mode='w') as stream:
            for _ in range(2):
                stream.addfile(tarfile.TarInfo('same'))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            archive.extract(target.getvalue(), self.root / 'new', limit=archive.SOURCE_LIMIT)
        self.assertFalse((self.root / 'new').exists())

    def test_source_index_must_match_real_tree_before_becoming_readonly(self):
        source = self.root / 'source'
        source.mkdir()
        (source / 'tracked.py').write_text('print("reviewed")\n')
        subprocess.run(['git', 'init', '--quiet'], cwd=source, check=True)
        subprocess.run(['git', 'add', '.'], cwd=source, check=True)
        tree = subprocess.run(['git', 'write-tree'], cwd=source, check=True, capture_output=True).stdout.decode().strip()
        # Test the source verifier against actual Git state rather than mocking it.
        self.assertEqual(archive.seal_source(source, tree), tree)
        self.assertEqual((source / 'tracked.py').stat().st_mode & 0o222, 0)
        stale = self.root / 'stale'
        stale.mkdir()
        (stale / 'tracked.py').write_text('print("stale")\n')
        with self.assertRaisesRegex(ValueError, 'reviewed Git tree'):
            archive.seal_source(stale, tree)

    def test_contract_binds_entry_dependencies_and_verifier(self):
        path = self.root / 'runtime.py'
        path.write_text('fixed')
        with patch.object(archive, 'CONTRACT_PATHS', ('runtime.py',)):
            first = archive.contract(self.root)
            path.write_text('changed')
            self.assertNotEqual(first, archive.contract(self.root))
