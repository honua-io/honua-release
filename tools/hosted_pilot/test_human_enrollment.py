"""Enrollment uses private nominated identities and the existing saved-plan path."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import deploy
import human_enrollment as enrollment

PREFIX = 'honua-pilot-offline'
POOL = 'us-east-1_Offline123'
KEY = 'arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111'
PEOPLE = {
    'human_author': {'email': 'author@example.invalid', 'role': 'authors', 'emailOwnershipConfirmed': True},
    'human_approver': {'email': 'approver@example.invalid', 'role': 'approvers', 'emailOwnershipConfirmed': True},
}


class AwsReadAndEncrypt:
    def __init__(self):
        self.calls = []
        self.account = '123456789012'
        self.pool = {'Id': POOL, 'Name': PREFIX + '-operators',
                     'Arn': f'arn:aws:cognito-idp:us-east-1:{self.account}:userpool/{POOL}',
                     'AdminCreateUserConfig': {'AllowAdminCreateUserOnly': True}}
        self.tags = {'ResetBoundary': PREFIX, 'ManagedBy': 'honua-release-hosted-pilot'}
        self.key = {'Arn': KEY, 'KeyState': 'Enabled', 'KeyUsage': 'ENCRYPT_DECRYPT', 'KeySpec': 'SYMMETRIC_DEFAULT'}

    def get_caller_identity(self):
        return {'Account': self.account}

    def describe_user_pool(self, **kwargs):
        assert kwargs == {'UserPoolId': POOL}
        return {'UserPool': self.pool}

    def list_tags_for_resource(self, **kwargs):
        assert kwargs == {'ResourceArn': self.pool['Arn']}
        return {'Tags': self.tags}

    def describe_key(self, **kwargs):
        assert kwargs == {'KeyId': KEY}
        return {'KeyMetadata': self.key}

    def encrypt(self, **kwargs):
        self.calls.append(kwargs)
        # Deliberately opaque fixture bytes, never claimed as real KMS encryption.
        return {'KeyId': KEY, 'CiphertextBlob': bytes([len(self.calls)]) * 80}


class HumanEnrollment(unittest.TestCase):
    def seal(self, people=None, aws=None):
        aws = aws or AwsReadAndEncrypt()
        return enrollment.seal(PEOPLE if people is None else people, prefix=PREFIX, pool_id=POOL,
                               key_arn=KEY, sts=aws, cognito=aws, kms=aws)

    def test_sealed_source_contains_no_identity_or_credential_and_each_binding_is_exact(self):
        aws = AwsReadAndEncrypt()
        sealed, private = self.seal(aws=aws)
        enrollment.validate_configuration(sealed, region='us-east-1', custody_key_arn=KEY)
        self.assertFalse(private['accountsCreated']); self.assertFalse(private['invitationsSent'])
        self.assertEqual(len(aws.calls), 2)
        passwords = set()
        for (alias, person), call in zip(PEOPLE.items(), aws.calls):
            self.assertEqual(call['EncryptionAlgorithm'], 'SYMMETRIC_DEFAULT')
            self.assertEqual(call['KeyId'], KEY)
            self.assertEqual(call['EncryptionContext'], {
                'Purpose': 'honua.pilot-human-enrollment/v1', 'Pilot': PREFIX, 'PoolId': POOL,
                'Alias': alias, 'Role': person['role']})
            plaintext = json.loads(call['Plaintext'])
            self.assertEqual(plaintext['email'], person['email'])
            password = private['people'][alias]['temporaryPassword']
            passwords.add(password)
            self.assertEqual(plaintext['temporaryPassword'], password)
            self.assertEqual(len(password), 32)
            for expression in ('[a-z]', '[A-Z]', '[0-9]', '[!@#%+_-]'):
                self.assertRegex(password, expression)
            self.assertNotIn(password, json.dumps(sealed)); self.assertNotIn(person['email'], json.dumps(sealed))
        self.assertEqual(len(passwords), 2)

    def test_same_person_cannot_hold_both_roles_even_with_different_case(self):
        people = copy.deepcopy(PEOPLE)
        people['human_approver']['email'] = people['human_author']['email'].upper()
        aws = AwsReadAndEncrypt()
        with self.assertRaisesRegex(ValueError, 'distinct human'):
            self.seal(people, aws)
        self.assertEqual(aws.calls, [])

    def test_missing_confirmation_extra_fields_unknown_roles_and_wrong_count_refuse_before_encryption(self):
        for transform in (
            lambda p: p['human_author'].update(emailOwnershipConfirmed=False),
            lambda p: p['human_author'].update(emailOwnershipConfirmed='true'),
            lambda p: p['human_author'].update(password='not-an-enrollment-input'),
            lambda p: p['human_author'].update(role='administrator'),
            lambda p: p['human_author'].update(role='approvers'),
            lambda p: p.pop('human_approver'),
            lambda p: p.update({'user@invalid': p.pop('human_author')}),
        ):
            with self.subTest(transform=transform):
                people = copy.deepcopy(PEOPLE); transform(people); aws = AwsReadAndEncrypt()
                with self.assertRaises(ValueError): self.seal(people, aws)
                self.assertEqual(aws.calls, [])

    def test_observed_account_pool_tags_and_key_must_match_before_encryption(self):
        for target, field, value in (
            ('self', 'account', '999999999999'), ('pool', 'Name', 'public-demo'),
            ('pool', 'Id', 'us-east-1_Another'), ('pool', 'AdminCreateUserConfig', {'AllowAdminCreateUserOnly': False}),
            ('tags', 'ResetBoundary', 'another-pilot'), ('tags', 'ManagedBy', 'other'),
            ('key', 'KeyState', 'Disabled'), ('key', 'KeySpec', 'RSA_2048'),
            ('key', 'Arn', KEY.replace('11111111', '22222222')),
        ):
            with self.subTest(target=target, field=field):
                aws = AwsReadAndEncrypt()
                if target == 'self': setattr(aws, field, value)
                else: getattr(aws, target)[field] = value
                with self.assertRaises(ValueError): self.seal(aws=aws)
                self.assertEqual(aws.calls, [])

    def test_source_rejects_plaintext_extra_fields_context_rebinding_and_invalid_payload(self):
        sealed, _ = self.seal()
        for transform in (
            lambda s: s.update(email='private@example.invalid'),
            lambda s: s['users']['human_author'].update(email='private@example.invalid'),
            lambda s: s['users']['human_author'].update(payload='not base64'),
            lambda s: s['users']['human_author'].update(payload=base64.b64encode(b'short').decode()),
            lambda s: s['users']['human_author'].update(role='administrator'),
            lambda s: s.update(key_arn=KEY.replace('11111111', '22222222')),
            lambda s: s.update(pool_id='us-west-2_Other'),
        ):
            with self.subTest(transform=transform):
                changed = copy.deepcopy(sealed); transform(changed)
                with self.assertRaises(ValueError):
                    enrollment.validate_configuration(changed, region='us-east-1', custody_key_arn=KEY)

    def test_private_files_are_owner_only_and_existing_handoffs_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'people.json'
            enrollment.write_new_private(path, PEOPLE)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(enrollment.private_input(path), PEOPLE)
            with self.assertRaises(FileExistsError): enrollment.write_new_private(path, {})
            self.assertEqual(json.loads(path.read_text()), PEOPLE)
            link = Path(directory) / 'link'; link.symlink_to(path)
            with self.assertRaises(ValueError): enrollment.private_input(link)
            path.chmod(0o644)
            with self.assertRaises(ValueError): enrollment.private_input(path)

    def test_checked_deployment_refuses_private_fields_before_terraform(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); tf = root / 'certification/hosted-pilot'
            path = tf / 'deployments/pilot.tfvars.json'; path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'name_prefix': PREFIX, 'region': 'us-east-1',
                'expires_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                'human_enrollment': {'email': 'private@example.invalid'}}))
            with patch.object(deploy, 'ROOT', root), patch.object(deploy, 'TF', tf), patch.object(deploy, 'run') as run, patch.dict(os.environ,
                    {'PILOT_REGION': 'us-east-1', 'PILOT_OPERATION': 'apply', 'PILOT_STATE_KMS_ARN': KEY}):
                with self.assertRaisesRegex(ValueError, 'unreviewed fields'):
                    deploy.config(str(path.relative_to(root)))
                self.assertEqual(run.call_args.args[0][:2], ['git', 'ls-files'])

    def test_campaign_enrollment_uses_same_sealed_input_boundary(self):
        sealed, _ = self.seal()
        application = {
            'candidate': dict.fromkeys(('manifest_sha256', 'server_image', 'console_image', 'server_revision',
                                       'console_revision', 'studio_revision', 'sdk_revision'), 'reviewed-binding'),
            'permissions_boundary_arn': 'reviewed-runtime-boundary', 'allowed_operator_cidrs': ['192.0.2.10/32'],
            'identity_redirects': dict.fromkeys(('console_origin', 'edge_callback_url', 'edge_logout_url',
                                                'server_bff_callback_url', 'server_bff_logout_url'), 'reviewed-url'),
            'human_enrollment': sealed,
        }
        for mutation in (
            None,
            lambda app: app.update(password='private-value'),
            lambda app: app['candidate'].update(token='private-value'),
            lambda app: app['identity_redirects'].update(client_secret='private-value'),
            lambda app: app['human_enrollment']['users']['human_author'].update(email='private@example.invalid'),
            lambda app: app['human_enrollment'].update(key_arn=KEY.replace('11111111', '22222222')),
        ):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); tf = root / 'certification/hosted-pilot/campaign'
                path = tf / 'deployments/pilot.tfvars.json'; path.parent.mkdir(parents=True)
                changed = copy.deepcopy(application)
                if mutation: mutation(changed)
                path.write_text(json.dumps({'name_prefix': PREFIX+'-agent', 'region': 'us-east-1',
                    'expires_at': (datetime.now(timezone.utc)+timedelta(days=1)).isoformat(), 'application': changed}))
                with patch.multiple(deploy, ROOT=root, TF=tf, TF_ROOT='certification/hosted-pilot/campaign', CAMPAIGN=True), \
                        patch.object(deploy, 'run') as run, patch.dict(os.environ,
                            {'PILOT_REGION': 'us-east-1', 'PILOT_OPERATION': 'apply', 'PILOT_STATE_KMS_ARN': KEY}):
                    if mutation:
                        with self.assertRaises(ValueError): deploy.config(str(path.relative_to(root)))
                    else:
                        self.assertEqual(deploy.config(str(path.relative_to(root))), path)
                    self.assertEqual(run.call_count, 1)
                    self.assertEqual(run.call_args.args[0][:2], ['git', 'ls-files'])


if __name__ == '__main__':
    unittest.main()
