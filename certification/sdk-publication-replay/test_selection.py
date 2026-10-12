import copy
import io
import json
import subprocess
import tempfile
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from selection import CLIENTS, SDK_FAMILY, selected_versions, validate_candidate
import prepare


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.reviewed = yaml.safe_load((HERE.parents[1] / 'platform-manifest.yaml').read_text())

    def test_default_keeps_all_reviewed_independent_versions(self):
        self.assertEqual(selected_versions(self.reviewed), {
            key: self.reviewed['clientArtifacts'][key]['version'] for key in CLIENTS})
        validate_candidate(copy.deepcopy(self.reviewed), self.reviewed)

    def test_sdk_override_leaves_independent_mcp_and_scaffold_versions(self):
        versions = selected_versions(self.reviewed, '0.1.999')
        for key in SDK_FAMILY:
            self.assertEqual(versions[key], '0.1.999')
        for key in ('honua-mcp-server', 'create-honua-app'):
            self.assertEqual(versions[key], self.reviewed['clientArtifacts'][key]['version'])

    def test_nonstable_or_nonexact_versions_fail(self):
        for version in ('latest', '^0.1.16', '0.1.16-beta.1', '0.1.16+build', '00.1.16', '../tag', ''):
            with self.subTest(version=version), self.assertRaises(ValueError):
                selected_versions(self.reviewed, version)

    def test_mixed_reviewed_sdk_family_fails(self):
        for field, value in (('version', '0.1.999'), ('sourceSha', 'f' * 40)):
            changed = copy.deepcopy(self.reviewed)
            changed['clientArtifacts']['honua-react'][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                selected_versions(changed)

    def test_runtime_rejects_every_named_client_identity_drift(self):
        for key in CLIENTS:
            for field in ('version', 'sourceSha', 'integrity', 'filename', 'publicationState', 'package'):
                changed = copy.deepcopy(self.reviewed)
                changed['clientArtifacts'][key][field] = 'different'
                with self.subTest(key=key, field=field), self.assertRaises(ValueError):
                    validate_candidate(changed, self.reviewed)

    def test_temporary_override_does_not_authorize_runtime_or_other_mutations(self):
        candidate = copy.deepcopy(self.reviewed)
        for key in SDK_FAMILY:
            candidate['clientArtifacts'][key].update(version='0.1.999', sourceSha='f' * 40)
        candidate['components']['honua-sdk-js'].update(version='0.1.999', sha='f' * 40)
        candidate['evidenceSources']['sdk']['producerSha'] = 'f' * 40
        validate_candidate(candidate, self.reviewed, '0.1.999')
        with self.assertRaises(ValueError):
            validate_candidate(candidate, self.reviewed)
        for key in ('honua-mcp-server', 'create-honua-app'):
            changed = copy.deepcopy(candidate)
            changed['clientArtifacts'][key]['integrity'] = 'other'
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_candidate(changed, self.reviewed, '0.1.999')
        candidate['components']['honua-server']['sha'] = 'e' * 40
        with self.assertRaises(ValueError):
            validate_candidate(candidate, self.reviewed, '0.1.999')

    def test_component_and_evidence_source_must_match(self):
        for owner in ('component', 'evidence'):
            changed = copy.deepcopy(self.reviewed)
            if owner == 'component':
                changed['components']['honua-sdk-js']['sha'] = 'e' * 40
            else:
                changed['evidenceSources']['sdk']['producerSha'] = 'e' * 40
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                selected_versions(changed)

    def test_artifact_verifier_failure_emits_no_candidate(self):
        sources = [self.reviewed['clientArtifacts'][key]['sourceSha']
                   for key in ('honua-sdk-js', 'honua-mcp-server', 'create-honua-app')]
        metadata = []
        for key in CLIENTS:
            pin = self.reviewed['clientArtifacts'][key]
            metadata.append(io.BytesIO(json.dumps({
                'version': pin['version'], 'gitHead': pin['sourceSha'],
                'dist': {'integrity': pin['integrity'],
                         'tarball': 'https://registry.npmjs.org/' + pin['filename']},
            }).encode()))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'candidate'
            with patch.object(prepare, 'published_source', side_effect=sources), \
                    patch.object(prepare.urllib.request, 'urlopen', side_effect=metadata), \
                    patch.object(prepare.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'verifier')) as verify, \
                    self.assertRaises(subprocess.CalledProcessError):
                prepare.main(['--output', str(output)])
            self.assertFalse(output.exists())
            self.assertTrue(verify.call_args.kwargs['check'])
            self.assertIn('tools/verify_client_artifacts.py', verify.call_args.args[0])
            self.assertEqual(verify.call_args.args[0][-2:], ['--ecosystem', 'npm'])

    def test_release_must_be_published_stable(self):
        for override in ({'draft': True}, {'prerelease': True}, {'published_at': None}):
            release = dict(draft=False, prerelease=False, published_at='2026-10-12')
            release.update(override)
            with self.subTest(override=override), patch.object(prepare, 'github', return_value=release), self.assertRaises(SystemExit):
                prepare.published_source('js-sdk-v0.1.999')

    def test_annotated_tag_is_dereferenced_to_exact_source(self):
        with patch.object(prepare, 'github', side_effect=[
            {'draft': False, 'prerelease': False, 'published_at': '2026-10-12'},
            {'object': {'type': 'tag', 'sha': 'e' * 40}},
            {'object': {'type': 'commit', 'sha': 'f' * 40}},
        ]):
            self.assertEqual(prepare.published_source('js-sdk-v0.1.999'), 'f' * 40)

    def test_invalid_tag_target_fails(self):
        with patch.object(prepare, 'github', side_effect=[
            {'draft': False, 'prerelease': False, 'published_at': '2026-10-12'},
            {'object': {'type': 'commit', 'sha': 'short'}},
        ]), self.assertRaises(SystemExit):
            prepare.published_source('js-sdk-v0.1.999')


if __name__ == '__main__':
    unittest.main()
