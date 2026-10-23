"""Adversarial checks for saved-plan identity and nonsecret evidence."""
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import deploy
import readiness


class SavedPlanContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.tf = self.root / 'certification/hosted-pilot'
        self.tf.mkdir(parents=True)
        self.cfg = self.tf / 'deployments/pilot.tfvars.json'
        self.cfg.parent.mkdir()
        self.cfg.write_text('{}')
        (self.tf / '.terraform.lock.hcl').write_text('reviewed provider lock')
        self.work = self.root / 'private'
        self.work.mkdir()
        (self.work / 'saved.tfplan').write_bytes(b'saved binary plan with sensitive state')
        self.env = {
            'GITHUB_SHA': 'a' * 40, 'GITHUB_RUN_ID': '12', 'GITHUB_RUN_ATTEMPT': '2',
            'GITHUB_REPOSITORY': 'honua-io/honua-release', 'GITHUB_REF': 'refs/heads/trunk',
            'PILOT_PLAN_ATTEMPT': '1', 'PILOT_OPERATION': 'apply',
            'PILOT_BUDGET_VERSION_ID': 'owner-ledger-version', 'PILOT_BUDGET_SHA256': 'b' * 64,
        }
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.env, clear=True).start()
        patch.object(deploy, 'ROOT', self.root).start()
        patch.object(deploy, 'TF', self.tf).start()
        self.review = {
            'schema': 'honua.hosted-pilot-saved-plan/v1',
            'context': deploy.context() | {'GITHUB_RUN_ATTEMPT': '1'},
            'operation': 'apply', 'config_path': str(self.cfg.relative_to(self.root)),
            'config_sha256': deploy.digest(self.cfg), 'backend_sha256': 'backend',
            'root_path': 'certification/hosted-pilot',
            'lock_sha256': deploy.digest(self.tf / '.terraform.lock.hcl'),
            'plan_sha256': deploy.digest(self.work / 'saved.tfplan'),
            'budget': {'version_id': 'owner-ledger-version', 'sha256': 'b' * 64},
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        self.write_review()

    def write_review(self):
        deploy.private_json(self.work / 'review.json', self.review)
        os.environ['PILOT_REVIEW_SHA256'] = deploy.digest(self.work / 'review.json')

    def verify(self):
        deploy.verify(self.review, self.work, self.cfg, 'backend')

    def test_apply_job_rerun_accepts_original_producing_attempt(self):
        self.verify()

    def test_refuses_substituted_binary_config_provider_and_review(self):
        for file in [self.work / 'saved.tfplan', self.cfg, self.tf / '.terraform.lock.hcl', self.work / 'review.json']:
            with self.subTest(file=file.name):
                original = file.read_bytes()
                file.write_bytes(original + b'tampered')
                with self.assertRaises(ValueError):
                    self.verify()
                file.write_bytes(original)

    def test_refuses_wrong_run_source_operation_and_attempt(self):
        for field, value in [('GITHUB_RUN_ID', '99'), ('GITHUB_SHA', 'f' * 40), ('GITHUB_REPOSITORY', 'attacker/fork'), ('PILOT_OPERATION', 'destroy'), ('PILOT_PLAN_ATTEMPT', '4')]:
            with self.subTest(field=field), patch.dict(os.environ, {field: value}):
                with self.assertRaises(ValueError):
                    self.verify()

    def test_refuses_expired_or_future_review_and_changed_backend(self):
        for offset in [-25, 1]:
            self.review['created_at'] = (datetime.now(timezone.utc) + timedelta(hours=offset)).isoformat()
            self.write_review()
            with self.assertRaises(ValueError):
                self.verify()
        self.review['created_at'] = datetime.now(timezone.utc).isoformat()
        self.review['backend_sha256'] = 'substituted-backend'
        self.write_review()
        with self.assertRaises(ValueError):
            self.verify()

    def test_review_does_not_include_terraform_values_or_outputs(self):
        plan = {'resource_changes': [{'address': 'aws_secretsmanager_secret_version.edge', 'change': {'actions': ['create'], 'before': {'secret_string': 'secret-before'}, 'after': {'secret_string': 'secret-after'}}}], 'planned_values': {'outputs': {'secret': 'secret-output'}}}
        encoded = json.dumps(deploy.summarize(plan))
        self.assertNotIn('secret-before', encoded)
        self.assertNotIn('secret-after', encoded)
        self.assertNotIn('secret-output', encoded)
        self.assertEqual(json.loads(encoded), [{'address': 'aws_secretsmanager_secret_version.edge', 'actions': ['create']}])

    def test_execution_failures_do_not_echo_sensitive_logs(self):
        from subprocess import CompletedProcess
        with patch('deploy.subprocess.run', return_value=CompletedProcess([], 1, b'SECRET-STDOUT', b'SECRET-STDERR')):
            with self.assertRaises(ValueError) as caught:
                deploy.run(['terraform', 'apply'])
        self.assertNotIn('SECRET', str(caught.exception))


class RefusalProbeContract(unittest.TestCase):
    def test_forged_request_still_requires_exact_oidc_pkce_login(self):
        login = 'https://pilot.auth.us-west-2.amazoncognito.com'
        location = login + '/oauth2/authorize?response_type=code&code_challenge_method=S256&state=x&nonce=y'
        with patch('readiness.get', return_value=(302, {'Location': location})) as request:
            readiness.refuse_console('https://console.example.invalid', login, forged=True)
        self.assertIn('Authorization', request.call_args.args[1])
        self.assertIn('X-Honua-Edge-Auth', request.call_args.args[1])

    def test_forged_identity_accepted_or_off_origin_redirect_is_failure(self):
        for result in [(200, {}), (302, {'Location': 'https://attacker.invalid/login'}), (302, {'Location': 'https://pilot.auth.us-west-2.amazoncognito.com/oauth2/authorize?response_type=code'})]:
            with self.subTest(result=result), patch('readiness.get', return_value=result):
                with self.assertRaises(ValueError):
                    readiness.refuse_console('https://console.example.invalid', 'https://pilot.auth.us-west-2.amazoncognito.com', forged=True)


if __name__ == '__main__':
    unittest.main()
