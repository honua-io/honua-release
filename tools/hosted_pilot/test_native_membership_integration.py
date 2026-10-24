"""Membership preparation cannot replay native issuance or expose human records."""
import copy
import json
import unittest
from unittest.mock import MagicMock, patch

from agent_e2e_pack import PackError, canonical_digest
import native_bootstrap_dispatch as dispatch
import test_native_bootstrap as native_fixture


def membership_configuration():
    return {'schemaVersion': 1,
        'enrollment': {'bucket': 'honua-release-pilot-unit-foundation-plans-123456789012',
            'key': 'hosted-pilot/plans/123/1/human-identities-1.json', 'versionId': 'original-enrollment',
            'sha256': 'sha256:'+'a'*64, 'kmsArn': 'arn:aws:kms:us-east-1:123456789012:key/original-custody'},
        'credentials': {kind: {'secretArn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-unit-agent/'+kind+'-ABC123',
            'versionId': char*32} for kind, char in (('scim', 'c'), ('root', 'd'))}}


class NativeMembershipIntegration(unittest.TestCase):
    def setUp(self):
        self.native = native_fixture.CredentialCustody()
        self.native.setUp()
        self.native.config['humanMemberships'] = membership_configuration()

    def test_memberships_finish_before_parent_issuance_and_only_digest_enters_parent_receipt(self):
        private = {'schema': 'honua.private-human-memberships/v1', 'users': {'private-alias': {'subjectId': 'private-subject'}},
            'qualification': False}
        def provision(**kwargs):
            kwargs['guard']()
            self.assertEqual(kwargs['configuration'], self.native.config['humanMemberships'])
            self.assertIs(kwargs['durable'], self.native.durable)
            self.assertFalse(self.native.durable.state['scenarioOperations'])
            self.native.secrets.put_secret_value.assert_not_called()
            return private
        with patch('human_memberships.provision_human_memberships', side_effect=provision) as helper:
            receipt = self.native.provision()
        helper.assert_called_once()
        self.assertEqual(receipt['humanMemberships'], {
            'schema': private['schema'], 'enrollment': self.native.config['humanMemberships']['enrollment'],
            'receiptDigest': canonical_digest(private), 'qualification': False})
        self.assertNotIn('private-subject', json.dumps(receipt))
        self.assertNotIn('private-alias', json.dumps(receipt))
        self.assertEqual(self.native.secrets.put_secret_value.call_count, 3)

    def test_existing_native_intent_refuses_before_any_human_effect(self):
        operation = canonical_digest({'kind': 'native-bootstrap', 'candidateKey': self.native.config['candidateKey'],
            'operationId': self.native.config['operation']['operationId']})
        self.native.durable.state['scenarioOperations'] = {operation+'/start': {'retained': 'original'}}
        with patch('human_memberships.provision_human_memberships') as helper:
            with self.assertRaisesRegex(PackError, 'already started'):
                self.native.provision()
        helper.assert_not_called()
        self.native.secrets.put_secret_value.assert_not_called()

    def test_unresolved_membership_effect_preserves_original_record_without_starting_native_issuance(self):
        def incomplete(**kwargs):
            kwargs['durable'].state['scenarioOperations']['original-membership-intent'] = {'unresolved': True}
            raise TimeoutError('original membership response lost')
        with patch('human_memberships.provision_human_memberships', side_effect=incomplete):
            with self.assertRaises(TimeoutError):
                self.native.provision()
        self.assertEqual(self.native.durable.state['scenarioOperations'], {'original-membership-intent': {'unresolved': True}})
        self.native.secrets.put_secret_value.assert_not_called()

    def test_null_membership_configuration_preserves_existing_bootstrap_contract(self):
        self.native.config['humanMemberships'] = None
        with patch('human_memberships.provision_human_memberships') as helper:
            receipt = self.native.provision()
        helper.assert_not_called()
        self.assertNotIn('humanMemberships', receipt)

    def task(self):
        configuration = self.native.config
        config = {'taskDefinitionArn': 'exact:1', 'taskRoleArn': 'native-role', 'executionRoleArn': 'execution-role',
            'imageUri': configuration['imageUri'], 'configuration': configuration}
        injected = {name: 'fixture-reference' for name in ('PILOT_FIXTURE_MASTER_CONNECTION',
            'HONUA_AGENT_FIXTURE_CLEANUP_DSN', 'HONUA_AGENT_FIXTURE_OBSERVER_DSN')}
        for name, kind in (('HONUA_NATIVE_SCIM_TOKEN', 'scim'), ('PILOT_FIXTURE_ADMIN_PASSWORD', 'root')):
            reference = configuration['humanMemberships']['credentials'][kind]
            injected[name] = reference['secretArn']+':::'+reference['versionId']
        task = {'taskDefinitionArn': 'exact:1', 'taskRoleArn': 'native-role', 'executionRoleArn': 'execution-role',
            'networkMode': 'awsvpc', 'runtimePlatform': {'cpuArchitecture': 'X86_64', 'operatingSystemFamily': 'LINUX'},
            'containerDefinitions': [{'name': 'native-bootstrap', 'image': configuration['imageUri'],
                'command': dispatch.COMMAND, 'user': '10001:10001', 'readonlyRootFilesystem': True,
                'mountPoints': [{'sourceVolume': 'work', 'containerPath': '/work', 'readOnly': False}],
                'environment': [{'name': 'PILOT_NATIVE_BOOTSTRAP_CONFIG', 'value': json.dumps(configuration)}],
                'secrets': [{'name': name, 'valueFrom': value} for name, value in injected.items()]}]}
        return config, task

    def test_dispatch_requires_both_exact_secret_versions_and_no_duplicate_injections(self):
        config, task = self.task()
        ecs = MagicMock()
        ecs.describe_task_definition.return_value = {'taskDefinition': task}
        dispatch.verify_task(ecs, config)
        original = copy.deepcopy(task)
        for name in ('HONUA_NATIVE_SCIM_TOKEN', 'PILOT_FIXTURE_ADMIN_PASSWORD'):
            with self.subTest(name=name):
                bad = copy.deepcopy(original)
                next(row for row in bad['containerDefinitions'][0]['secrets'] if row['name'] == name)['valueFrom'] += 'changed'
                ecs.describe_task_definition.return_value = {'taskDefinition': bad}
                with self.assertRaisesRegex(ValueError, 'credential version'):
                    dispatch.verify_task(ecs, config)
        bad = copy.deepcopy(original)
        bad['containerDefinitions'][0]['secrets'].append(copy.deepcopy(bad['containerDefinitions'][0]['secrets'][0]))
        ecs.describe_task_definition.return_value = {'taskDefinition': bad}
        with self.assertRaisesRegex(ValueError, 'secret injection'):
            dispatch.verify_task(ecs, config)

    def test_scim_secret_cannot_be_injected_without_membership_configuration(self):
        config, task = self.task()
        del config['configuration']['humanMemberships']
        task['containerDefinitions'][0]['environment'][0]['value'] = json.dumps(config['configuration'])
        ecs = MagicMock()
        ecs.describe_task_definition.return_value = {'taskDefinition': task}
        with self.assertRaisesRegex(ValueError, 'secret injection'):
            dispatch.verify_task(ecs, config)
