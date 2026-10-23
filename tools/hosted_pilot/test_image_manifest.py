"""Multiarch readiness accepts only the native task's reviewed platform member."""
import hashlib
import json
import unittest
from unittest.mock import patch

import image_manifest as images
import readiness


class ImageManifestBinding(unittest.TestCase):
    def setUp(self):
        self.amd64 = 'sha256:' + 'a' * 64
        self.arm64 = 'sha256:' + 'b' * 64
        self.raw = json.dumps({'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json', 'manifests': [
            {'digest': self.amd64, 'platform': {'os': 'linux', 'architecture': 'amd64'}},
            {'digest': self.arm64, 'platform': {'os': 'linux', 'architecture': 'arm64'}}]}, separators=(',', ':')).encode()
        self.index = 'sha256:' + hashlib.sha256(self.raw).hexdigest()
        self.image = 'ghcr.io/honua-io/honua-console@' + self.index

    def test_exact_native_manifest_digest_needs_no_public_network(self):
        with patch('image_manifest.read') as http:
            proof = images.verify(self.image, self.index)
        self.assertEqual(proof['binding'], 'native-exact-digest')
        http.assert_not_called()

    def test_native_amd64_member_is_bound_to_exact_registry_index_bytes(self):
        with patch('image_manifest.read', return_value=self.raw):
            proof = images.verify(self.image, self.amd64)
        self.assertEqual(proof['manifestDigest'], self.index)
        self.assertEqual(proof['observedDigest'], self.amd64)
        self.assertEqual(proof['binding'], 'verified-platform-manifest-member')

    def test_other_architecture_and_unrelated_digest_are_refused(self):
        for digest in (self.arm64, 'sha256:' + 'c' * 64):
            with self.subTest(digest=digest), patch('image_manifest.read', return_value=self.raw):
                with self.assertRaisesRegex(ValueError, 'task platform'):
                    images.verify(self.image, digest)
        with patch('image_manifest.read', return_value=self.raw):
            self.assertEqual(images.verify(self.image, self.arm64, architecture='ARM64')['platform']['architecture'], 'arm64')

    def test_registry_digest_substitution_is_refused_before_member_acceptance(self):
        with patch('image_manifest.read', return_value=self.raw + b' '):
            with self.assertRaisesRegex(ValueError, 'reviewed SHA256'):
                images.verify(self.image, self.amd64)

    def test_unknown_registry_cannot_trigger_arbitrary_http_or_bearer_forwarding(self):
        with patch('image_manifest.read') as http:
            with self.assertRaisesRegex(ValueError, 'unsupported registry'):
                images.verify(self.image.replace('ghcr.io', 'internal.example'), self.amd64)
        http.assert_not_called()

    def test_native_ecr_manifest_uses_exact_account_region_repository_and_digest(self):
        image = '123456789012.dkr.ecr.us-east-1.amazonaws.com/reviewed-image@' + self.index
        def aws(*args):
            self.assertIn('imageDigest=' + self.index, args)
            self.assertEqual(args[args.index('--region') + 1], 'us-east-1')
            self.assertEqual(args[args.index('--registry-id') + 1], '123456789012')
            return {'images': [{'imageId': {'imageDigest': self.index}, 'imageManifest': self.raw.decode()}]}
        self.assertEqual(images.verify(image, self.amd64, aws=aws)['observedDigest'], self.amd64)


class NativeServiceObservation(unittest.TestCase):
    setUp = ImageManifestBinding.setUp
    def aws(self, *args):
        if args[:2] == ('ecs', 'describe-services'):
            return {'services': [{'runningCount': 1, 'desiredCount': 1, 'pendingCount': 0, 'deployments': [{'rolloutState': 'COMPLETED'}]}]}
        if args[:2] == ('ecs', 'list-tasks'):
            return {'taskArns': ['native-task']}
        if args[:2] == ('ecs', 'describe-tasks'):
            return {'tasks': [{'taskArn': 'native-task', 'taskDefinitionArn': 'native-definition:5', 'containers': [{'name': 'console', 'image': self.image, 'imageDigest': self.amd64}]}]}
        if args[:2] == ('ecs', 'describe-task-definition'):
            return {'taskDefinition': {'taskDefinitionArn': 'native-definition:5', 'runtimePlatform': {'cpuArchitecture': 'X86_64', 'operatingSystemFamily': 'LINUX'}}}
        self.fail(str(args))

    def test_readiness_records_verified_native_container_and_task_platform(self):
        with patch('readiness.aws', side_effect=self.aws), patch('image_manifest.read', return_value=self.raw):
            result = readiness.observed_service('cluster', 'service', {'console': self.image})
        self.assertEqual(result[0]['taskArn'], 'native-task')
        self.assertEqual(result[0]['observedDigest'], self.amd64)
        self.assertEqual(result[0]['binding'], 'verified-platform-manifest-member')

    def test_unexpected_sidecar_is_refused_before_registry_access(self):
        def aws(*args):
            value = self.aws(*args)
            if args[:2] == ('ecs', 'describe-tasks'):
                value['tasks'][0]['containers'].append({'name': 'unreviewed'})
            return value
        with patch('readiness.aws', side_effect=aws), patch('image_manifest.read') as http:
            with self.assertRaisesRegex(ValueError, 'unexpected or missing containers'):
                readiness.observed_service('cluster', 'service', {'console': self.image})
        http.assert_not_called()
