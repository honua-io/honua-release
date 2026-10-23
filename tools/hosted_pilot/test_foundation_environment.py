import copy
import json
from unittest.mock import patch
import unittest

import foundation_environment as module


class FoundationEnvironment(unittest.TestCase):
    def setUp(self):
        self.policy = json.loads((module.ROOT / 'certification/hosted-pilot/foundation-environment.json').read_text())
        self.detail = {
            'name': self.policy['environment'], 'can_admins_bypass': False,
            'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True},
            'protection_rules': [{'type': 'required_reviewers', 'prevent_self_review': False,
                                  'reviewers': [{'type': 'User', 'reviewer': {'id': 12301237, 'login': 'mikemcdougall'}}]}],
        }
        self.branches = [{'name': 'trunk', 'type': 'branch'}]
        self.present = True
        self.calls = []

    def api(self, method, endpoint, body=None):
        self.calls.append((method, endpoint, body))
        if endpoint.startswith('users/'):
            return {'id': 12301237, 'type': 'User'}
        if '/environments?' in endpoint:
            return {'environments': [{'name': self.policy['environment']}] if self.present else []}
        if '/deployment-branch-policies' in endpoint:
            if method == 'POST':
                self.branches = [body]
            return {'branch_policies': self.branches}
        if method == 'PUT':
            self.present = True
        return copy.deepcopy(self.detail)

    def run_check(self, create=False):
        with patch.object(module, 'api', self.api):
            module.reconcile(self.policy, create=create)

    def test_missing_environment_refused_without_mutations(self):
        self.present = False
        with self.assertRaisesRegex(ValueError, 'absent'):
            self.run_check()
        self.assertTrue(all(row[0] == 'GET' for row in self.calls))

    def test_valid_protection_check_is_read_only(self):
        self.run_check()
        self.assertTrue(all(row[0] == 'GET' for row in self.calls))

    def test_create_protects_reviewers_before_enabling_trunk(self):
        self.present = False
        self.branches = []
        self.run_check(create=True)
        mutations = [row for row in self.calls if row[0] != 'GET']
        self.assertEqual([row[0] for row in mutations], ['PUT', 'POST'])
        self.assertEqual(mutations[0][2]['reviewers'], [{'type': 'User', 'id': 12301237}])

    def test_existing_weaker_protection_is_not_silently_reconfigured(self):
        self.detail['protection_rules'] = []
        with self.assertRaisesRegex(ValueError, 'protection differs'):
            self.run_check(create=True)
        self.assertTrue(all(row[0] == 'GET' for row in self.calls))

    def test_other_branch_or_tag_is_refused(self):
        for branch in [{'name': '*', 'type': 'branch'}, {'name': 'trunk', 'type': 'tag'}]:
            self.branches = [branch]
            with self.assertRaisesRegex(ValueError, 'only trunk'):
                self.run_check()

    def test_unrestricted_branches_are_refused(self):
        self.detail['deployment_branch_policy'] = None
        with self.assertRaisesRegex(ValueError, 'trunk branch'):
            self.run_check()

    def test_replaced_reviewer_identity_is_refused(self):
        self.detail['protection_rules'][0]['reviewers'][0]['reviewer']['id'] = 1
        with self.assertRaisesRegex(ValueError, 'identity policy'):
            self.run_check()

    def test_partial_create_can_finish_branch_binding_without_replacing_reviewers(self):
        self.branches = []
        self.run_check(create=True)
        self.assertEqual([row[0] for row in self.calls if row[0] != 'GET'], ['POST'])


if __name__ == '__main__':
    unittest.main()
