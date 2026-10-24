"""Foundation bindings survive unrelated trunk movement, never substitution."""
import os
from pathlib import Path
from subprocess import CompletedProcess
import tempfile
import unittest
from unittest.mock import patch

import bind_github


class ImmutableBindingSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.contract = Path('certification/hosted-pilot/github-bindings.json')
        for root in [self.root, self.root / '_pilot-bootstrap-source']:
            (root / self.contract).parent.mkdir(parents=True)
            (root / self.contract).write_text('{"reviewed":"contract"}')
        self.sha = 'a' * 40
        self.binding = {'release_root_sha': self.sha}
        self.addCleanup(patch.stopall)
        patch.object(bind_github, 'ROOT', self.root).start()
        patch.dict(os.environ, {'GITHUB_SHA': 'b' * 40, 'PILOT_BOOTSTRAP_ROOT_SHA': self.sha}).start()
        self.api = patch.object(bind_github, 'api', return_value={'protected': True}).start()
        self.git = patch.object(bind_github.subprocess, 'run', side_effect=[
            CompletedProcess([], 0, (self.sha + '\n').encode()),
            CompletedProcess([], 0, b''),
        ]).start()

    def test_unrelated_current_source_does_not_invalidate_reviewed_producer(self):
        bind_github.verify_release_source(self.binding)
        self.assertEqual(self.git.call_args_list[1].args[0][-2:], [self.sha, 'origin/trunk'])
        self.api.assert_called_once_with('GET', 'repos/honua-io/honua-release/branches/trunk')

    def test_changed_binding_contract_refused(self):
        (self.root / self.contract).write_text('{"reviewed":"new-contract"}')
        with self.assertRaisesRegex(ValueError, 'contract changed'):
            bind_github.verify_release_source(self.binding)

    def test_substituted_root_or_mutable_input_refused(self):
        for value in ['c' * 40, 'trunk']:
            with self.subTest(value=value), patch.dict(os.environ, {'PILOT_BOOTSTRAP_ROOT_SHA': value}):
                with self.assertRaises(ValueError):
                    bind_github.verify_release_source(self.binding)
        self.git.assert_not_called()

    def test_unreviewed_source_or_unprotected_trunk_refused(self):
        for checkout, ancestry, protected in [(1, 0, True), (0, 1, True), (0, 0, False)]:
            with self.subTest(checkout=checkout, ancestry=ancestry, protected=protected):
                self.git.side_effect = [CompletedProcess([], checkout, (self.sha + '\n').encode()), CompletedProcess([], ancestry, b'')]
                self.api.return_value = {'protected': protected}
                with self.assertRaises(ValueError):
                    bind_github.verify_release_source(self.binding)


if __name__ == '__main__':
    unittest.main()
