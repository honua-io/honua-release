"""Producer and retained-byte refusal controls; these are not installed evidence."""
import copy
import io
import json
import stat
import unittest
from unittest.mock import Mock
import warnings
import zipfile

from agent_e2e_pack import canonical_digest, digest_bytes
from bundle_agent_e2e_container import runtime_index
import worker_runtime_publication as publication


class WorkerRuntimePublication(unittest.TestCase):
    def setUp(self):
        self.source = 'a'*40
        self.image = '585192672263.dkr.ecr.us-east-1.amazonaws.com/honua-pilot-unit-worker@sha256:'+'b'*64
        self.public = 'sha256:'+'c'*64
        self.index = runtime_index()
        self.index['files']['public/public-manifest.json'] = self.public
        self.metadata = {'schema': 'honua.hosted-worker-image/v1', 'sourceSha': self.source,
            'runId': '42', 'runAttempt': '2', 'image': self.image, 'imageBytes': 4096, 'platform': 'linux/amd64',
            'caseId': 'unit-A', 'pack': {'path': 'reviewed'}, 'knowledge': {'sourceRevision': 'd'*40},
            'publicBundleDigest': self.public, 'runtimeBundleDigest': canonical_digest(self.index),
            'ownerBudget': {'version_id': 'original'}, 'retention': {'expiryEligibilityDays': 14},
            'publicationClaim': {'bucket': 'original-plans', 'key': 'hosted-pilot/reservations/worker-image/original.json',
                'versionId': 'original-claim', 'sha256': 'sha256:'+'d'*64},
            'qualification': False, 'modelExecution': 'not-run'}
        self.binding = {'sourceSha': self.source, 'runId': 42, 'runAttempt': 2, 'artifactId': 99,
            'artifactDigest': ''}
        self.repository = {'id': 123, 'full_name': publication.REPOSITORY}
        self.run = {'id': 42, 'run_attempt': 2, 'head_sha': self.source, 'head_branch': 'trunk',
            'event': 'workflow_dispatch', 'path': publication.WORKFLOW, 'status': 'completed',
            'conclusion': 'success', 'repository': self.repository, 'head_repository': self.repository}
        self.job = {'id': 43, 'run_id': 42, 'run_attempt': 2, 'head_sha': self.source, 'name': 'build',
            'status': 'completed', 'conclusion': 'success', 'steps': [
                {'name': name, 'number': position, 'status': 'completed', 'conclusion': 'success'}
                for position, name in enumerate((publication.BUILD_STEP, publication.RETAIN_STEP), start=6)]}
        self.jobs = {'total_count': 1, 'jobs': [self.job]}
        self.comparison = {'status': 'ahead', 'merge_base_commit': {'sha': self.source}}
        self.branch = {'name': 'trunk', 'protected': True, 'commit': {'sha': 'f'*40}}
        self.artifact = {'id': 99, 'name': 'hosted-pilot-worker-image-42-2', 'digest': '', 'expired': False,
            'size_in_bytes': 0, 'workflow_run': {'id': 42, 'head_sha': self.source, 'head_branch': 'trunk',
                'repository_id': 123, 'head_repository_id': 123}}
        self.calls = []
        self.guard = Mock()
        self.repack()

    def repack(self, members=None):
        self.index_raw = json.dumps(self.index, sort_keys=True).encode()+b'\n'
        if members is None:
            members = [(publication.METADATA_FILE, json.dumps(self.metadata).encode()),
                (publication.INDEX_FILE, self.index_raw)]
        output = io.BytesIO()
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', UserWarning)
            with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as package:
                for name, raw in members:
                    package.writestr(name, raw)
        self.archive = output.getvalue()
        self.binding['artifactDigest'] = digest_bytes(self.archive)
        self.artifact.update(digest=self.binding['artifactDigest'], size_in_bytes=len(self.archive))

    def github(self, path):
        self.calls.append(path)
        prefix = 'repos/'+publication.REPOSITORY+'/'
        self.assertTrue(path.startswith(prefix))
        endpoint = path.removeprefix(prefix)
        if endpoint == 'actions/runs/42/attempts/2': return copy.deepcopy(self.run)
        if endpoint == 'actions/runs/42/attempts/2/jobs?per_page=100': return copy.deepcopy(self.jobs)
        if endpoint == 'actions/artifacts/99': return copy.deepcopy(self.artifact)
        if endpoint == 'branches/trunk': return copy.deepcopy(self.branch)
        if endpoint == 'compare/'+self.source+'...'+'f'*40: return copy.deepcopy(self.comparison)
        self.fail('unexpected network endpoint: '+endpoint)

    def verify(self, **kwargs):
        return publication.verify_publication(self.binding, archive=self.archive,
            index_raw=self.index_raw, image=self.image, public_bundle_digest=self.public,
            github=self.github, guard=self.guard, **kwargs)

    def test_original_zip_and_index_authenticate_using_only_public_metadata(self):
        value = self.verify()
        self.assertEqual(value['metadata'], self.metadata)
        self.assertEqual(value['index'], self.index)
        self.assertEqual(value['producer']['jobId'], 43)
        self.assertEqual(value['producer']['artifactDigest'], digest_bytes(self.archive))
        self.assertEqual(len(self.calls), 5)
        self.assertTrue(all('/zip' not in path and '/contents/' not in path for path in self.calls))
        self.assertEqual(self.guard.call_count, 12)
        self.assertEqual(publication.publication_originals(self.archive)[publication.INDEX_FILE], self.index_raw)

    def test_protected_ancestor_is_supported_without_whole_commit_equality(self):
        for status in ('ahead', 'identical'):
            self.comparison['status'] = status
            self.assertEqual(self.verify()['producer']['sourceSha'], self.source)

    def test_unprotected_or_unrelated_source_refuses(self):
        for change in ({'status': 'behind'}, {'status': 'diverged'}, {'merge_base_commit': {'sha': 'e'*40}}):
            original = copy.deepcopy(self.comparison)
            self.comparison.update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'trunk ancestor'):
                self.verify()
            self.comparison = original

    def test_missing_protection_or_mutable_trunk_identity_refuses(self):
        for change in ({'protected': False}, {'protected': None}, {'name': 'other'},
            {'commit': {'sha': 'trunk'}}, {'commit': {}}):
            original = copy.deepcopy(self.branch)
            self.branch.update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'observed protected trunk'):
                self.verify()
            self.branch = original

    def test_workflow_source_run_attempt_event_and_fork_refuse(self):
        for change in ({'id': 41}, {'run_attempt': 1}, {'head_sha': 'e'*40}, {'head_branch': 'feature'},
            {'event': 'pull_request'}, {'path': '.github/workflows/other.yml'}, {'status': 'in_progress'},
            {'conclusion': 'failure'}, {'head_repository': {'id': 9, 'full_name': 'fork/honua-release'}},
            {'repository': {'id': 9, 'full_name': 'other/repo'}}):
            original = copy.deepcopy(self.run)
            self.run.update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'protected publisher'):
                self.verify()
            self.run = original

    def test_missing_duplicate_or_truncated_jobs_refuse(self):
        for jobs in ({'total_count': 0, 'jobs': []}, {'total_count': 101, 'jobs': [self.job]},
            {'total_count': 2, 'jobs': [self.job]}, {'total_count': 2, 'jobs': [self.job, self.job]}):
            self.jobs = jobs
            with self.subTest(jobs=jobs['total_count']), self.assertRaises(ValueError): self.verify()

    def test_build_and_retention_must_succeed_in_the_same_attempt(self):
        for change in ({'run_id': 41}, {'run_attempt': 1}, {'head_sha': 'e'*40},
            {'status': 'in_progress'}, {'conclusion': 'failure'}, {'name': 'other'}):
            original = copy.deepcopy(self.job)
            self.job.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError): self.verify()
            self.job.clear(); self.job.update(original)
        original = copy.deepcopy(self.job['steps'])
        for steps in ([], [original[0]], [original[0], original[0], original[1]],
            [original[0] | {'conclusion': 'skipped'}, original[1]],
            [original[0], original[1] | {'number': 1}],
            [original[0], original[1] | {'conclusion': 'failure'}]):
            self.job['steps'] = steps
            with self.subTest(steps=steps), self.assertRaises(ValueError): self.verify()
        self.job['steps'] = original

    def test_artifact_identity_digest_size_and_custody_refuse(self):
        for change in ({'id': 98}, {'name': 'hosted-pilot-worker-image-42-1'}, {'digest': 'sha256:'+'f'*64},
            {'expired': True}, {'size_in_bytes': len(self.archive)+1},
            *({'workflow_run': self.artifact['workflow_run'] | {key: value}} for key, value in
                (('id', 41), ('head_sha', 'e'*40), ('head_branch', 'feature'),
                 ('repository_id', 999), ('head_repository_id', 999)))):
            original = copy.deepcopy(self.artifact)
            self.artifact.update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'artifact producer'):
                self.verify()
            self.artifact = original

    def test_tampered_archive_refuses_before_network(self):
        self.archive += b'changed'
        with self.assertRaisesRegex(ValueError, 'original artifact digest'): self.verify()
        self.assertEqual(self.calls, [])

    def test_raw_sidecar_must_be_exact_not_merely_equivalent_json(self):
        self.index_raw = json.dumps(self.index).encode()
        with self.assertRaisesRegex(ValueError, 'ZIP member'): self.verify()

    def test_receipt_image_public_source_and_qualification_refuse(self):
        for key, value in (('image', self.image.replace('b'*64, 'e'*64)), ('sourceSha', 'e'*40),
            ('runId', '41'), ('runAttempt', '1'), ('imageBytes', 0), ('imageBytes', 512*1024*1024+1),
            ('platform', 'linux/arm64'), ('publicBundleDigest', 'sha256:'+'e'*64),
            ('qualification', True), ('modelExecution', 'success')):
            original = copy.deepcopy(self.metadata)
            self.metadata[key] = value; self.repack()
            with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, 'receipt differs'): self.verify()
            self.metadata = original

    def test_stale_runtime_cannot_be_admitted_by_a_rehashed_receipt(self):
        name = next(iter(runtime_index()['files']))
        self.index['files'][name] = 'sha256:'+'e'*64
        self.metadata['runtimeBundleDigest'] = canonical_digest(self.index)
        self.repack()
        with self.assertRaisesRegex(ValueError, 'reviewed controller source'): self.verify()

    def test_missing_or_mutable_single_build_claim_refuses(self):
        original = self.metadata['publicationClaim']
        for claim in ({}, original | {'versionId': 'null'}, original | {'key': '../claim.json'},
            original | {'sha256': 'unbound'}, original | {'extra': 'ignored'}):
            self.metadata['publicationClaim'] = claim; self.repack()
            with self.subTest(claim=claim), self.assertRaisesRegex(ValueError, 'single-build claim'): self.verify()

    def test_missing_public_manifest_or_different_canonical_digest_refuses(self):
        self.metadata['runtimeBundleDigest'] = 'sha256:'+'e'*64; self.repack()
        with self.assertRaisesRegex(ValueError, 'runtime/public bytes'): self.verify()
        del self.index['files']['public/public-manifest.json']
        self.metadata['runtimeBundleDigest'] = canonical_digest(self.index); self.repack()
        with self.assertRaisesRegex(ValueError, 'runtime/public bytes'): self.verify()

    def test_duplicate_json_keys_refuse(self):
        self.repack([(publication.METADATA_FILE, json.dumps(self.metadata).encode()),
            (publication.INDEX_FILE, b'{"schemaVersion":1,"schemaVersion":1,"files":{}}')])
        self.index_raw = None
        with self.assertRaisesRegex(ValueError, 'duplicate keys'): self.verify()

    def test_unsafe_extra_duplicate_and_oversized_zip_members_refuse(self):
        metadata = json.dumps(self.metadata).encode()
        link = zipfile.ZipInfo(publication.INDEX_FILE)
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for members in (
            [(publication.METADATA_FILE, metadata)],
            [(publication.METADATA_FILE, metadata), ('../runtime-index.json', self.index_raw)],
            [(publication.METADATA_FILE, metadata), (publication.INDEX_FILE, self.index_raw), ('extra', b'x')],
            [(publication.METADATA_FILE, metadata), (publication.INDEX_FILE, self.index_raw), (publication.INDEX_FILE, self.index_raw)],
            [(publication.METADATA_FILE, metadata), (link, self.index_raw)],
            [(publication.METADATA_FILE, metadata), (publication.INDEX_FILE, b'x'*(publication.LIMIT+1))]):
            self.repack(members)
            with self.subTest(names=[str(row[0]) for row in members]), self.assertRaisesRegex(ValueError, 'ZIP'):
                self.verify()

    def test_funding_or_authority_loss_ends_verification_at_each_boundary(self):
        for boundary in range(1, 13):
            self.guard = Mock(side_effect=[None]*(boundary-1)+[ValueError('original authority lost')])
            with self.subTest(boundary=boundary), self.assertRaisesRegex(ValueError, 'original authority lost'):
                self.verify()

    def test_binding_rejects_boolean_ids_unknown_fields_and_nonimmutable_source(self):
        for change in ({'runId': True}, {'runAttempt': 0}, {'artifactId': -1}, {'sourceSha': 'trunk'},
            {'artifactDigest': 'latest'}, {'extra': 'ignored'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                publication.validate_binding(self.binding | change)


if __name__ == '__main__':
    unittest.main()
