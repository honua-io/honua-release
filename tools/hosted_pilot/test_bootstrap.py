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
        patch.dict(os.environ, {'GITHUB_RUN_ID': '123', 'PILOT_BOOTSTRAP_ACCOUNT_ID': '123456789012', 'RUNNER_TEMP': str(self.root)}, clear=True).start()
        self.cfg = {'name_prefix': 'honua-pilot-offline'}
        self.identity = {'release_root_sha': 'a' * 40, 'foundation_source_sha': 'c' * 40, 'config_path': 'certification/hosted-pilot/bootstrap/deployments/test.tfvars.json', 'provider_lock_sha256': 'd' * 64, 'config_sha256': 'b' * 64}

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
                    *({'TagKey': key, 'TagValue': value} for key, value in bootstrap.creation_tags(self.identity, '123').items()),
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

    def producer(self):
        os.environ['PILOT_RECOVERY_RUN_ID'] = '123'
        (self.root / 'recovery-producer.json').write_text(json.dumps({'id': 123, 'head_sha': self.identity['foundation_source_sha']}))

    def test_secure_custody_retains_content_addressed_immutable_journal(self):
        secured = {'bucket': 'private', 'kms_key_id': 'key'}
        with patch('bootstrap.secure_backend', return_value=secured), patch('bootstrap.exists', return_value=False), patch('bootstrap.aws', return_value={'Buckets': [], 'Keys': [], 'VersionId': 'immutable'}) as api:
            bootstrap.save_recovery({}, self.cfg, self.identity, self.work)
        puts = [call.args for call in api.call_args_list if call.args[:2] == ('s3api', 'put-object')]
        self.assertEqual(len(puts), 1)
        self.assertIn('/123/journals/' + bootstrap.digest(self.work / 'custody-recovery.json') + '.json', puts[0][5])
        self.assertEqual(puts[0][-2:], ('--if-none-match', '*'))
        self.assertIn('--ssekms-key-id', puts[0])
        self.assertNotIn('terraform.tfstate', puts[0])

    def test_original_creation_tags_recover_key_without_artifact_or_secure_bucket(self):
        self.producer()
        os.environ['GITHUB_RUN_ID'] = '456'
        tags = bootstrap.creation_tags(self.identity, '123') | {'ResetBoundary': self.cfg['name_prefix']}
        def api(*args):
            if args == ('kms', 'list-keys'):
                return {'Keys': [{'KeyId': 'owned'}]}
            if args[:2] == ('kms', 'list-resource-tags'):
                return {'Tags': [{'TagKey': key, 'TagValue': value} for key, value in tags.items()]}
            return {'Buckets': []}
        with patch('bootstrap.exists', return_value=False), patch('bootstrap.aws', side_effect=api):
            bootstrap.recover_imports(self.identity, self.work, {'bucket': 'private'}, self.cfg)
        self.assertIn('id = "owned"', (self.root / 'recovery-imports.tf').read_text())
        self.assertEqual(os.environ['PILOT_CREATION_RUN_ID'], '123')
        value = json.loads((self.work / 'recovery-input/custody-recovery.json').read_text())
        self.assertEqual(value['bootstrap_run_id'], '123')
        self.assertTrue(value['cloud_discovery_complete'])

    def test_bucket_only_partial_creation_is_recovered_with_exact_identity(self):
        self.producer()
        bucket = 'honua-release-pilot-offline-foundation-state-123456789012'
        tags = bootstrap.creation_tags(self.identity, '123') | {'ResetBoundary': self.cfg['name_prefix']}
        def api(*args):
            if args == ('kms', 'list-keys'):
                return {'Keys': []}
            return {'Buckets': [{'Name': bucket}]} if args == ('s3api', 'list-buckets') else {}
        def optional(*args):
            return {'TagSet': [{'Key': key, 'Value': value} for key, value in tags.items()]} if args[1] == 'get-bucket-tagging' else None
        def exists(*args):
            return args[-1] == bucket
        with patch('bootstrap.exists', side_effect=exists), patch('bootstrap.secure_backend', side_effect=ValueError('not secure yet')), patch('bootstrap.aws', side_effect=api), patch('bootstrap.optional_aws', side_effect=optional):
            value = bootstrap.retrieve_recovery({'bucket': bucket}, self.cfg, self.identity, self.work)
        self.assertEqual(value['resources'], [{'address': 'aws_s3_bucket.custody["state"]', 'id': bucket}])

    def test_missing_creation_tags_on_existing_bucket_retain_collision_liability(self):
        self.producer()
        with patch('bootstrap.exists', return_value=True), patch('bootstrap.secure_backend', side_effect=ValueError('untagged')), patch('bootstrap.aws', return_value={'Keys': []}), patch('bootstrap.optional_aws', return_value=None):
            with self.assertRaisesRegex(ValueError, 'ownership/producer differs'):
                bootstrap.retrieve_recovery({'bucket': 'collision'}, self.cfg, self.identity, self.work)

    def test_native_producer_mismatch_refuses_even_content_addressed_journal(self):
        self.producer()
        value = {'schema': 'honua.hosted-pilot-custody-recovery/v1', **self.identity, 'bootstrap_run_id': '999', 'cloud_discovery_complete': True, 'resources': [{'address': 'aws_kms_key.custody', 'id': 'key'}]}
        with self.assertRaisesRegex(ValueError, 'observed producing run'):
            bootstrap.validate_recovery(value, self.identity)

    def test_creation_tags_without_identity_never_authorize_recovery(self):
        self.producer()
        tags = {'ManagedBy': 'honua-release-hosted-pilot-bootstrap', 'ResetBoundary': self.cfg['name_prefix'], 'BootstrapExecution': '123'}
        def api(*args):
            if args == ('kms', 'list-keys'):
                return {'Keys': [{'KeyId': 'legacy'}]}
            return {'Tags': [{'TagKey': key, 'TagValue': value} for key, value in tags.items()]}
        with patch('bootstrap.exists', return_value=False), patch('bootstrap.aws', side_effect=api):
            with self.assertRaisesRegex(ValueError, 'lack durable source/config identity'):
                bootstrap.retrieve_recovery({'bucket': 'private'}, self.cfg, self.identity, self.work)
        self.assertFalse((self.root / 'recovery-imports.tf').exists())

    def test_versioned_journal_recovery_survives_missing_github_artifact(self):
        self.producer()
        self.state()
        with patch('bootstrap.aws', return_value={'Buckets': []}):
            value = bootstrap.journal(self.cfg, self.identity, self.work)
        (self.root / 'terraform.tfstate').unlink()
        content = json.dumps(value).encode()
        import hashlib
        key = bootstrap.recovery_prefix(self.cfg, '123') + 'journals/' + hashlib.sha256(content).hexdigest() + '.json'
        def api(*args):
            if args[:2] == ('s3api', 'list-object-versions'):
                return {'Versions': [{'Key': key, 'VersionId': 'original-version', 'LastModified': '2026-10-22'}]}
            if args[:2] == ('s3api', 'get-object'):
                Path(args[-1]).write_bytes(content)
                self.assertIn('original-version', args)
                return {}
            self.fail(str(args))
        with patch('bootstrap.exists', return_value=True), patch('bootstrap.secure_backend', return_value={'bucket': 'private'}), patch('bootstrap.aws', side_effect=api):
            bootstrap.recover_imports(self.identity, self.work, {'bucket': 'private'}, self.cfg)
        self.assertIn('key-id', (self.root / 'recovery-imports.tf').read_text())

    def test_tampered_durable_journal_refuses_without_discovery_fallback(self):
        self.producer()
        key = bootstrap.recovery_prefix(self.cfg, '123') + 'journals/' + 'a' * 64 + '.json'
        def api(*args):
            if args[:2] == ('s3api', 'list-object-versions'):
                return {'Versions': [{'Key': key, 'VersionId': 'v1', 'LastModified': 'now'}]}
            Path(args[-1]).write_text('tampered')
            return {}
        with patch('bootstrap.exists', return_value=True), patch('bootstrap.secure_backend', return_value={'bucket': 'private'}), patch('bootstrap.aws', side_effect=api):
            with self.assertRaisesRegex(ValueError, 'digest differs'):
                bootstrap.retrieve_recovery({'bucket': 'private'}, self.cfg, self.identity, self.work)

    def test_key_only_native_rerun_requires_explicit_original_recovery_binding(self):
        tags = bootstrap.creation_tags(self.identity, '123') | {'ResetBoundary': self.cfg['name_prefix']}
        def api(*args):
            return {'Keys': [{'KeyId': 'prior-key'}]} if args == ('kms', 'list-keys') else {'Tags': [{'TagKey': key, 'TagValue': value} for key, value in tags.items()]}
        # GITHUB_RUN_ID is still the original ID on a native rerun. That alone
        # is insufficient: only governed recovery authorizes resource adoption.
        with patch('bootstrap.aws', side_effect=api), patch('bootstrap.exists', return_value=False):
            with self.assertRaisesRegex(ValueError, 'explicit original recovery_run_id'):
                bootstrap.refuse_unbound_initial_custody(self.cfg, self.work)
        self.assertFalse((self.root / 'recovery-imports.tf').exists())
        recovery = self.work / 'recovery-input'
        recovery.mkdir()
        (recovery / 'custody-recovery.json').write_text(json.dumps(self.identity | {'bootstrap_run_id': '123'}))
        with patch('bootstrap.aws', side_effect=api), patch('bootstrap.exists', return_value=False):
            bootstrap.refuse_unbound_initial_custody(self.cfg, self.work)

    def test_new_dispatch_cannot_ignore_earlier_key_liability(self):
        os.environ['GITHUB_RUN_ID'] = '456'
        tags = bootstrap.creation_tags(self.identity, '123') | {'ResetBoundary': self.cfg['name_prefix']}
        def api(*args):
            return {'Keys': [{'KeyId': 'prior-key'}]} if args == ('kms', 'list-keys') else {'Tags': [{'TagKey': key, 'TagValue': value} for key, value in tags.items()]}
        with patch('bootstrap.aws', side_effect=api), patch('bootstrap.exists', return_value=False):
            with self.assertRaisesRegex(ValueError, 'explicit original recovery_run_id'):
                bootstrap.refuse_unbound_initial_custody(self.cfg, self.work)

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
