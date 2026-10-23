"""Initial custody failures must preserve recovery without publishing state."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bootstrap


class InitialCustodyRecovery(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.work = self.root / 'private'
        self.work.mkdir()
        self.addCleanup(patch.stopall)
        patch.object(bootstrap, 'TF', self.root).start()
        patch.dict(os.environ, {'GITHUB_RUN_ID': '123', 'PILOT_BOOTSTRAP_ACCOUNT_ID': '123456789012'}, clear=True).start()
        self.cfg = {'name_prefix': 'honua-pilot-offline'}
        self.identity = {'release_root_sha': 'a' * 40, 'config_sha256': 'b' * 64}

    def state(self):
        (self.root / 'terraform.tfstate').write_text(json.dumps({'resources': [
            {'mode': 'managed', 'type': 'aws_kms_key', 'name': 'custody', 'instances': [{'attributes': {'id': 'key-id', 'secret_string': 'DO-NOT-PUBLISH-STATE'}}]},
            {'mode': 'managed', 'type': 'aws_secretsmanager_secret_version', 'name': 'application', 'instances': [{'attributes': {'id': 'application-secret', 'secret_string': 'DO-NOT-PUBLISH-APPLICATION'}}]},
        ]}))

    def test_journal_allowlists_only_custody_import_ids(self):
        self.state()
        with patch('bootstrap.aws', return_value={'Buckets': []}):
            value = bootstrap.journal(self.cfg, self.identity, self.work)
        encoded = json.dumps(value)
        self.assertEqual(value['resources'], [{'address': 'aws_kms_key.custody', 'id': 'key-id'}])
        self.assertNotIn('DO-NOT-PUBLISH', encoded)
        self.assertNotIn('application-secret', encoded)
        self.assertEqual(value['cloud_discovery_tags']['BootstrapExecution'], '123')

    def test_insecure_custody_never_receives_state_and_journal_survives(self):
        self.state()
        with patch('bootstrap.secure_backend', side_effect=ValueError('not private')), patch('bootstrap.aws', return_value={'Buckets': []}) as api:
            remote = bootstrap.save_recovery({'bucket': 'unready'}, self.cfg, self.identity, self.work)
        self.assertIsNone(remote)
        self.assertFalse(any(call.args[:2] == ('s3api', 'put-object') for call in api.call_args_list))
        self.assertTrue((self.work / 'custody-recovery.json').exists())
        self.assertNotIn('DO-NOT-PUBLISH', (self.work / 'custody-recovery.json').read_text())

    def test_unacknowledged_key_is_discovered_only_by_exact_creation_tags(self):
        def api(*args):
            if args == ('kms', 'list-keys'):
                return {'Keys': [{'KeyId': 'owned'}, {'KeyId': 'foreign'}]}
            if args[:2] == ('kms', 'list-resource-tags'):
                return {'Tags': [
                    {'TagKey': 'ManagedBy', 'TagValue': 'honua-release-hosted-pilot-bootstrap'},
                    {'TagKey': 'ResetBoundary', 'TagValue': 'honua-pilot-offline' if args[-1] == 'owned' else 'public-demo'},
                    {'TagKey': 'BootstrapExecution', 'TagValue': '123'},
                ]}
            return {'Buckets': []}
        with patch('bootstrap.aws', side_effect=api):
            value = bootstrap.journal(self.cfg, self.identity, self.work)
        self.assertEqual(value['resources'], [{'address': 'aws_kms_key.custody', 'id': 'owned'}])
        self.assertTrue(value['cloud_discovery_complete'])

    def test_existing_custody_with_foreign_tags_is_refused_before_state_access(self):
        with patch('bootstrap.aws', return_value={'TagSet': [{'Key': 'ResetBoundary', 'Value': 'public-demo'}]}):
            with self.assertRaisesRegex(ValueError, 'ownership tags differ'):
                bootstrap.secure_backend({'bucket': 'collision', 'key': 'honua/hosted-pilot/honua-pilot-offline/foundation.tfstate', 'region': 'us-west-2'})

    def test_discovered_bucket_collision_does_not_grant_ownership(self):
        bucket = 'honua-release-pilot-offline-foundation-state-123456789012'
        def api(*args):
            return {'Keys': []} if args == ('kms', 'list-keys') else {'Buckets': [{'Name': bucket}]}
        with patch('bootstrap.aws', side_effect=api), patch('bootstrap.optional_aws', return_value={'TagSet': [{'Key': 'ResetBoundary', 'Value': 'public-demo'}]}):
            value = bootstrap.journal(self.cfg, self.identity, self.work)
        self.assertFalse(value['cloud_discovery_complete'])
        self.assertEqual(value['unverified_bucket_names'], [bucket])
        self.assertEqual(value['resources'], [])

    def test_helper_upgrade_requires_exact_reviewed_compatibility_mapping(self):
        identity = {'release_root_sha': 'new-root', 'foundation_source_sha': 'new-foundation', 'config_sha256': 'config', 'provider_lock_sha256': 'new-lock', 'config_path': 'reviewed-config'}
        old = identity | {'release_root_sha': 'old-root', 'foundation_source_sha': 'old-foundation', 'provider_lock_sha256': 'old-lock'}
        base = self.root / 'certification/hosted-pilot/bootstrap'
        base.mkdir(parents=True)
        policy = {'schema': 'honua.hosted-pilot-recovery-compatibility/v1', 'migrations': []}
        path = base / 'recovery-compatibility.json'
        path.write_text(json.dumps(policy))
        with patch.object(bootstrap, 'ROOT', self.root), patch('bootstrap.bootstrap_contract_digest', return_value='fixed-contract'):
            self.assertFalse(bootstrap.recovery_compatible(old, identity))
            policy['migrations'] = [{'from_release_root_sha': 'old-root', 'from_foundation_source_sha': 'old-foundation', 'config_sha256': 'config', 'from_provider_lock_sha256': 'old-lock', 'to_provider_lock_sha256': 'new-lock', 'to_bootstrap_contract_sha256': 'fixed-contract', 'reason': 'Reviewed custody recovery fix'}]
            path.write_text(json.dumps(policy))
            self.assertTrue(bootstrap.recovery_compatible(old, identity))
            self.assertFalse(bootstrap.recovery_compatible(old | {'config_sha256': 'different'}, identity))

    def test_ambiguous_creation_keys_and_authorization_errors_fail_closed(self):
        with patch('bootstrap.aws', side_effect=ValueError('denied')):
            value = bootstrap.journal(self.cfg, self.identity, self.work)
        self.assertFalse(value['cloud_discovery_complete'])
        self.assertEqual(value['resources'], [])
        self.assertFalse(bootstrap.allowed_address('aws_iam_role.executor["apply"]'))
        self.assertFalse(bootstrap.allowed_address('aws_s3_bucket.custody["other-customer"]'))


if __name__ == '__main__':
    unittest.main()
