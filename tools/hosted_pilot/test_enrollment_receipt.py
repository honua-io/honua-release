"""Original-plan binding, privacy, lost-reply and apply-order controls."""
import base64
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import deploy
import enrollment_receipt as receipt


class PrivateEnrollmentReceipt(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root_path = 'certification/hosted-pilot'
        self.tf = self.root / self.root_path
        self.cfg = self.tf / 'deployments/pilot.tfvars.json'
        self.cfg.parent.mkdir(parents=True)
        self.work = self.root / 'hosted-pilot-private'
        self.work.mkdir(mode=0o700)
        self.kms = 'arn:aws:kms:us-west-2:123456789012:key/' + '12345678-1234-1234-1234-123456789012'
        self.env = {
            'GITHUB_REPOSITORY': 'honua-io/honua-release', 'GITHUB_REF': 'refs/heads/trunk',
            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SHA': 'a' * 40,
            'GITHUB_RUN_ID': '1234', 'GITHUB_RUN_ATTEMPT': '2', 'PILOT_PLAN_ATTEMPT': '1',
            'PILOT_OPERATION': 'apply', 'PILOT_REGION': 'us-west-2', 'PILOT_ACCOUNT_ID': '123456789012',
            'PILOT_STATE_KMS_ARN': self.kms, 'PILOT_PLAN_KMS_ARN': self.kms, 'PILOT_PLAN_BUCKET': 'private-plans',
            'PILOT_REVIEW_ARTIFACT_ID': '12345', 'PILOT_BUDGET_VERSION_ID': 'original-budget',
            'PILOT_BUDGET_SHA256': 'b' * 64, 'RUNNER_TEMP': str(self.root),
            'GITHUB_OUTPUT': str(self.root / 'step-output'),
        }
        candidate = {'manifest_sha256': 'c' * 64, 'server_image': 'ghcr.io/honua-io/honua-server@sha256:' + 'd' * 64,
            'console_image': 'ghcr.io/honua-io/honua-console@sha256:' + 'e' * 64,
            **dict.fromkeys(('server_revision', 'console_revision', 'studio_revision', 'sdk_revision'), 'a' * 40)}
        enrollment = {'pool_id': 'us-west-2_Ab123', 'key_arn': self.kms, 'users': {
            alias: {'role': role, 'payload': base64.b64encode(b'ciphertext' * 12).decode()}
            for alias, role in (('person_one', 'authors'), ('person_two', 'approvers'))}}
        self.configuration = {'region': 'us-west-2', 'name_prefix': 'honua-pilot-demo',
            'server_domain': 'server.example.invalid', 'candidate': candidate, 'human_enrollment': enrollment}
        self.deployment = {'claimed_candidate': copy.deepcopy(candidate), 'server_origin': 'https://server.example.invalid',
            'ecs_cluster': 'honua-pilot-demo', 'server_service': 'server', 'console_service': 'console',
            'server_task_definition': 'arn:aws:ecs:us-west-2:123456789012:task-definition/server:7',
            'console_task_definition': 'arn:aws:ecs:us-west-2:123456789012:task-definition/console:8'}
        self.identity = {'pool_id': 'us-west-2_Ab123', 'issuer': 'https://cognito-idp.us-west-2.amazonaws.com/us-west-2_Ab123',
            'groups': {'authors': 'honua-pilot-demo-authors', 'approvers': 'honua-pilot-demo-approvers'},
            'unused_secret': 'MUST-NOT-ESCAPE'}
        self.humans = {alias: {'subject_id': f'12345678-1234-1234-1234-12345678901{i}',
            'role': person['role'], 'status': 'FORCE_CHANGE_PASSWORD'}
            for i, (alias, person) in enumerate(enrollment['users'].items())}
        self.outputs = {key: {'value': value} for key, value in (
            ('deployment', self.deployment), ('identity_preparation', self.identity), ('human_enrollment', self.humans))}
        self.outputs['extra_secret'] = {'value': 'MUST-NOT-ESCAPE'}
        (self.tf / '.terraform.lock.hcl').write_text('original provider lock')
        (self.work / 'saved.tfplan').write_bytes(b'private original saved plan')
        self.review = {'schema': 'honua.hosted-pilot-saved-plan/v1',
            'context': {key: self.env[key] for key in ('GITHUB_REPOSITORY', 'GITHUB_SHA', 'GITHUB_RUN_ID')} |
                       {'GITHUB_RUN_ATTEMPT': '1'},
            'operation': 'apply', 'backend_sha256': 'f' * 64,
            'lock_sha256': deploy.digest(self.tf / '.terraform.lock.hcl'),
            'plan_sha256': deploy.digest(self.work / 'saved.tfplan'),
            'budget': {'version_id': 'original-budget', 'sha256': 'b' * 64},
            'created_at': datetime.now(timezone.utc).isoformat(),
            's3_version_id': 'saved-plan-version',
            'resource_changes': [{'address': 'module.application.aws_cognito_user.humans["person_one"]', 'actions': ['create']}],
            'action_counts': {'create': 1}}
        self.objects = {}
        self.calls = []
        self.lost_write = False
        self.wrong_kms = False
        self.write_source()

    def write_source(self):
        self.cfg.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.write_text(json.dumps(self.configuration))
        self.review.update(config_path=str(self.cfg.relative_to(self.root)), config_sha256=deploy.digest(self.cfg), root_path=self.root_path)
        (self.work / 'review.json').write_text(json.dumps(self.review))
        self.env['PILOT_REVIEW_SHA256'] = deploy.digest(self.work / 'review.json')

    def aws(self, *args):
        self.calls.append(args)
        self.assertEqual(args[0], 's3api')
        key = args[args.index('--key') + 1]
        if args[1] == 'put-object':
            self.assertEqual(args[args.index('--if-none-match') + 1], '*')
            if key in self.objects:
                raise ValueError('conditional object already exists')
            content = Path(args[args.index('--body') + 1]).read_bytes()
            self.objects[key] = content
            if self.lost_write:
                raise ValueError('reply lost after original write')
            return {'VersionId': 'object-version'}
        self.assertEqual(args[1], 'get-object')
        if key.endswith('/saved.tfplan'):
            content = b'private original saved plan'
            object_version = 'saved-plan-version'
        else:
            content = self.objects[key]
            object_version = 'object-version'
        Path(args[-1]).write_bytes(content)
        return {'VersionId': object_version, 'ServerSideEncryption': 'aws:kms',
                'SSEKMSKeyId': 'wrong-key' if self.wrong_kms else self.kms}

    def emit(self):
        return receipt.emit(cfg=self.cfg, root=self.root, root_path=self.root_path, outputs=self.outputs,
                            review=self.review, work=self.work, env=self.env, aws=self.aws)

    def test_original_review_and_actual_subjects_retained_privately_only(self):
        reference = self.emit()
        self.assertEqual(reference['key'], 'hosted-pilot/plans/1234/1/human-identities-2.json')
        carrier = json.loads(self.objects[reference['key']])
        self.assertFalse(carrier['qualification'])
        self.assertEqual(carrier['producer']['runAttempt'], 2)
        self.assertEqual(carrier['plan']['attempt'], 1)
        self.assertEqual(carrier['plan']['reviewArtifactId'], 12345)
        self.assertEqual(carrier['users']['person_one']['subjectId'], self.humans['person_one']['subject_id'])
        self.assertEqual(carrier['identity']['approverRole'], 'honua-pilot-demo-approvers')
        self.assertEqual(self.objects[carrier['plan']['review']['key']], (self.work / 'review.json').read_bytes())
        self.assertEqual(reference['sha256'], receipt.sha256(self.objects[reference['key']]))
        public = (self.work / 'human-enrollment-reference.json').read_text()
        self.assertNotIn('subjectId', public)
        self.assertNotIn('person_one', public)
        self.assertNotIn('MUST-NOT-ESCAPE', ''.join(value.decode() for value in self.objects.values()))
        self.assertNotIn('payload', self.objects[reference['key']].decode())
        self.assertEqual((self.work / 'human-identities.json').stat().st_mode & 0o777, 0o600)

    def test_no_nominations_produce_no_carrier_or_cloud_calls(self):
        self.configuration['human_enrollment'] = None
        self.write_source()
        self.assertIsNone(self.emit())
        self.assertEqual(self.calls, [])

    def test_campaign_uses_its_composed_application_and_original_candidate(self):
        app = self.configuration
        self.configuration = {'region': 'us-west-2', 'name_prefix': 'honua-pilot-demo-agent',
            'candidate_digest': 'sha256:' + 'c' * 64, 'server_image': app['candidate']['server_image'],
            'target_domain': 'server.example.invalid', 'application': app}
        self.outputs = {'application_deployment': {'value': {key: value['value'] for key, value in self.outputs.items()}}}
        self.root_path = 'certification/hosted-pilot/campaign'
        self.cfg = self.root / self.root_path / 'deployments/pilot.tfvars.json'
        self.write_source()
        self.assertIsNotNone(self.emit())
        self.configuration['candidate_digest'] = 'sha256:' + 'f' * 64
        self.write_source()
        self.calls.clear()
        with self.assertRaisesRegex(ValueError, 'candidate differ'):
            self.emit()
        self.assertEqual(self.calls, [])

    def test_wrong_pool_issuer_roles_subject_or_image_refuses_before_retention(self):
        mutations = [
            (self.identity, 'pool_id', 'us-west-2_Other'), (self.identity, 'issuer', 'https://attacker.invalid'),
            (self.identity, 'groups', {'authors': 'admin', 'approvers': 'admin'}),
            (self.humans['person_one'], 'role', 'approvers'),
            (self.humans['person_one'], 'subject_id', 'invented-user'),
            (self.humans['person_one'], 'status', 'DISABLED'),
            (self.humans['person_two'], 'subject_id', self.humans['person_one']['subject_id']),
            (self.deployment, 'claimed_candidate', {}),
            (self.deployment, 'server_origin', 'https://foreign.invalid'),
            (self.deployment, 'server_task_definition', 'arn:aws:ecs:us-west-2:999999999999:task-definition/server:7'),
            (self.deployment, 'console_task_definition', 'console:latest'),
        ]
        for target, key, value in mutations:
            with self.subTest(field=key):
                original = target[key]
                target[key] = value
                with self.assertRaises(ValueError):
                    self.emit()
                self.assertEqual(self.calls, [])
                target[key] = original

    def test_changed_configuration_review_and_missing_task_revision_refuse(self):
        self.cfg.write_text(self.cfg.read_text() + ' ')
        with self.assertRaises(ValueError):
            self.emit()
        self.write_source()
        self.review['private_values'] = 'MUST-NOT-ESCAPE'
        self.write_source()
        with self.assertRaises(ValueError):
            self.emit()
        del self.review['private_values']
        self.write_source()
        del self.deployment['server_task_definition']
        with self.assertRaises(KeyError):
            self.emit()
        self.assertEqual(self.calls, [])

    def test_wrong_source_workflow_attempt_and_artifact_refuse_before_effect(self):
        for key, value in (('GITHUB_REF', 'refs/heads/feature'), ('GITHUB_EVENT_NAME', 'pull_request'),
                ('GITHUB_REPOSITORY', 'fork/repo'), ('GITHUB_SHA', 'b' * 40),
                ('PILOT_PLAN_ATTEMPT', '3'), ('GITHUB_RUN_ID', '555'),
                ('PILOT_REVIEW_ARTIFACT_ID', ''), ('PILOT_OPERATION', 'destroy')):
            with self.subTest(field=key):
                original = self.env[key]
                self.env[key] = value
                with self.assertRaises(ValueError):
                    self.emit()
                self.env[key] = original
                self.assertEqual(self.calls, [])

    def test_lost_write_reply_recovers_by_read_without_second_mutation(self):
        self.lost_write = True
        reference = self.emit()
        self.assertEqual(len([call for call in self.calls if call[1] == 'put-object']), 2)
        self.assertEqual(len([call for call in self.calls if call[1] == 'get-object']), 2)
        self.assertIn(reference['key'], self.objects)

    def test_repeated_emit_preserves_exact_original_versions(self):
        original = self.emit()
        retained = copy.deepcopy(self.objects)
        self.assertEqual(self.emit(), original)
        self.assertEqual(self.objects, retained)

    def test_conflicting_original_or_wrong_encryption_refuses_without_overwrite(self):
        self.objects['hosted-pilot/plans/1234/1/review.json'] = b'foreign original'
        with self.assertRaises(ValueError):
            self.emit()
        self.assertEqual(self.objects, {'hosted-pilot/plans/1234/1/review.json': b'foreign original'})
        self.objects.clear()
        self.wrong_kms = True
        with self.assertRaises(ValueError):
            self.emit()
        self.assertNotIn('hosted-pilot/plans/1234/1/human-identities-2.json', self.objects)

    def invoke_apply(self, fail=False):
        def execute(args, **kwargs):
            if args[1] == 'apply':
                self.assertFalse(self.objects)
                if fail:
                    raise ValueError('Terraform apply failed; original effects retained')
                return b''
            self.assertEqual(args[1:], ['output', '-json'])
            return json.dumps(self.outputs).encode()
        with patch.dict(os.environ, self.env, clear=True), patch.object(deploy, 'ROOT', self.root), \
                patch.object(deploy, 'TF', self.tf), patch.object(deploy, 'TF_ROOT', self.root_path), \
                patch.object(deploy, 'config', return_value=self.cfg), patch.object(deploy, 'initialize', return_value='f' * 64), \
                patch.object(deploy, 'custody'), patch.object(deploy, 'budget'), \
                patch.object(deploy, 'aws', side_effect=self.aws), patch.object(deploy, 'run', side_effect=execute), \
                patch('sys.argv', ['deploy.py', 'apply', '--config', str(self.cfg.relative_to(self.root))]):
            deploy.main()

    def test_actual_apply_entrypoint_emits_only_after_verified_success(self):
        self.invoke_apply()
        self.assertEqual((self.root / 'step-output').read_text(), 'human_enrollment=true\n')
        self.assertEqual(len(self.objects), 2)

    def test_failed_apply_retains_no_identity_carrier(self):
        with self.assertRaisesRegex(ValueError, 'apply failed'):
            self.invoke_apply(fail=True)
        self.assertFalse(self.objects)
        self.assertFalse((self.work / 'human-enrollment-reference.json').exists())


if __name__ == '__main__':
    unittest.main()
