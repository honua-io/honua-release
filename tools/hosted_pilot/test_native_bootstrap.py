"""Native credential custody retains failed effects and never reissues on replay."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import native_bootstrap as bootstrap
import native_bootstrap_dispatch as dispatch
from agent_e2e_pack import PackError, canonical_digest


def configuration():
    candidate, generation, basis = ('sha256:'+char*64 for char in ('a', 'b', 'c'))
    return {'schemaVersion': 1, 'region': 'us-east-1', 'accountId': '123456789012', 'candidateKey': candidate,
        'generationDigest': generation, 'state': {'region': 'us-east-1', 'bucket': 'retained', 'table': 'retained'},
        'bootstrapArtifactDigest': 'sha256:'+'d'*64, 'operation': {'operationId': 'original-native-bootstrap', 'liabilityBasisDigest': basis,
        'reservationIds': {'infrastructure': 'original-infra', 'cleanup': 'original-cleanup'}},
        'databaseHost': 'isolated.rds.amazonaws.com', 'database': 'honua_agent_candidate',
        'appRole': 'honua_agent_app', 'cleanupRole': 'honua_agent_cleanup', 'observerRole': 'honua_agent_observer',
        'endpoint': 'https://agent.pilot.demo.honua.io', 'itemId': '11111111-1111-1111-1111-111111111111',
        'expiresAt': (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(), 'readGrants': ['read:parcels/0'],
        'secretArns': {kind: 'arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-pilot-unit-agent/native-agent/'+kind+'-ABC123' for kind in ('setup', 'observer', 'agent')},
        'bucket': 'private-native-custody', 'receiptKey': 'native/bootstrap/receipt.json',
        'clusterArn': 'arn:aws:ecs:us-east-1:123456789012:cluster/honua-pilot-unit-agent',
        'taskDefinitionFamily': 'arn:aws:ecs:us-east-1:123456789012:task-definition/honua-pilot-unit-agent-native-bootstrap',
        'imageUri': '123456789012.dkr.ecr.us-east-1.amazonaws.com/honua-pilot-unit-trusted-controller@sha256:'+'e'*64}


class CredentialCustody(unittest.TestCase):
    def setUp(self):
        self.config = configuration()
        self.durable = MagicMock()
        self.durable.store.generation = {'digest': self.config['generationDigest']}
        self.durable.state = {'bootstrapImport': {'artifactDigest': self.config['bootstrapArtifactDigest']},
            'ledger': {'schemaVersion': 2, 'candidateKey': self.config['candidateKey'], 'ceilingUsd': '500', 'cleanupReserveUsd': '50', 'entries': [],
            'operations': [{'id': 'original-'+kind, 'operationId': 'original-native-bootstrap', 'purpose': 'bootstrap', 'kind': ledger_kind,
                'amountUsd': '5', 'basisDigest': self.config['operation']['liabilityBasisDigest'], 'state': 'reserved', 'actualUsd': None}
                for kind, ledger_kind in [('infra', 'infrastructure'), ('cleanup', 'cleanup')]]}}
        self.secrets = MagicMock()
        self.secrets.put_secret_value.side_effect = lambda **kw: {'ARN': kw['SecretId'], 'VersionId': kw['ClientRequestToken']}
        self.permissions = {}
        def http(config, root_key, method, path, body=None):
            if method == 'POST':
                kind = body['name'].rsplit('-', 1)[1]
                key_id = {'setup': '1', 'observer': '2', 'agent': '3'}[kind]*8+'-1111-1111-1111-111111111111'
                self.permissions[key_id] = body['permissions']
                return {'apiKey': {'id': key_id, 'permissions': body['permissions'], 'expiresAt': body['expiresAt']}, 'key': 'private-key-'+kind+'x'*48}
            return {'canAuthenticate': True, 'permissions': self.permissions[path.split('/')[-2]]}
        self.http = http

    def provision(self):
        with patch.dict(os.environ, {'HONUA_AGENT_FIXTURE_CLEANUP_DSN': 'private-cleanup', 'HONUA_AGENT_FIXTURE_OBSERVER_DSN': 'private-observer'}), \
                patch('native_bootstrap.sql_identities', return_value={'qualification': False}), patch('native_bootstrap.http', side_effect=self.http):
            return bootstrap.provision(self.config, self.durable, connection=MagicMock(), secrets=self.secrets,
                                       root_key='private-root', native={'taskArn': 'native-task'})

    def test_actual_issued_values_go_only_to_private_secret_versions_and_never_retained_state(self):
        receipt = self.provision()
        self.assertFalse(receipt['qualification'])
        self.assertEqual(set(receipt['keys']), {'setup', 'observer', 'agent'})
        self.assertEqual(self.secrets.put_secret_value.call_count, 3)
        self.assertNotIn('private-key-', json.dumps(self.durable.state))
        self.assertNotIn('private-root', json.dumps(receipt))
        self.assertEqual(receipt['keys']['observer']['permissions'], ['admin:read'])
        self.assertIn('agent:studio-compose:'+self.config['itemId'], receipt['keys']['agent']['permissions'])

    def test_lost_key_issuance_retains_prior_sql_and_intent_and_refuses_replay(self):
        self.http = MagicMock(side_effect=TimeoutError('lost native mutation response'))
        with self.assertRaises(TimeoutError):
            self.provision()
        self.assertTrue(any(key.endswith('/sql') for key in self.durable.state['scenarioOperations']))
        self.assertTrue(any(key.endswith('/key-setup-intent') for key in self.durable.state['scenarioOperations']))
        with self.assertRaisesRegex(PackError, 'already started'):
            self.provision()
        self.assertEqual(self.http.call_count, 1)
        self.secrets.put_secret_value.assert_not_called()

    def test_settled_cleanup_or_changed_generation_cannot_authorize_more_native_effects(self):
        self.durable.state['ledger']['operations'][1].update(state='settled', actualUsd='1')
        with self.assertRaisesRegex(PackError, 'original liability'):
            self.provision()
        self.durable.store.generation = {'digest': 'sha256:'+'f'*64}
        with self.assertRaisesRegex(PackError, 'substitute history'):
            self.provision()

    def test_wildcard_data_or_shared_secret_custody_are_refused(self):
        for edit in [{'readGrants': ['read:parcels/*']}, {'secretArns': {'setup': 'same', 'observer': 'same', 'agent': 'same'}}]:
            with self.subTest(edit=edit), self.assertRaises(PackError):
                bootstrap.validate(self.config | edit)

    def test_privileged_issuer_does_not_forward_credentials_on_redirect(self):
        self.assertIsNone(bootstrap.NoRedirect().redirect_request(None, None, 302, 'redirect', {}, 'https://attacker.invalid'))

    def test_unknown_launch_acknowledgement_never_launches_another_paid_task(self):
        config = {'schemaVersion': 1, 'region': self.config['region'], 'accountId': self.config['accountId'],
            'clusterArn': self.config['clusterArn'], 'taskDefinitionArn': self.config['taskDefinitionFamily']+':1',
            'taskRoleArn': 'native-role', 'executionRoleArn': 'execution-role', 'containerName': 'native-bootstrap',
            'imageUri': self.config['imageUri'], 'subnetIds': ['subnet-123'], 'securityGroupIds': ['sg-123'],
            'configuration': self.config, 'configurationSha256': canonical_digest(self.config),
            'dispatchBudget': {'candidateKey': self.config['candidateKey'], 'state': self.config['state'], 'generationDigest': self.config['generationDigest'], 'claimTable': 'retained-claims'}}
        clients = {name: MagicMock() for name in ('ecs', 's3', 'dynamodb', 'sts')}
        clients['sts'].get_caller_identity.return_value = {'Account': self.config['accountId']}
        clients['dynamodb'].get_item.return_value = {'Item': {'bindingDigest': {'S': canonical_digest(config)}}}
        with patch('native_bootstrap_dispatch.sdk', side_effect=lambda name, region: clients[name]), \
                patch('native_bootstrap_dispatch.verify_task'), patch('native_bootstrap_dispatch.read_allowance', return_value={}), \
                patch('native_bootstrap_dispatch.consume', return_value=False):
            with self.assertRaisesRegex(ValueError, 'acknowledgement unknown'):
                dispatch.dispatch(config)
        clients['ecs'].run_task.assert_not_called()
        clients['ecs'].stop_task.assert_not_called()

    def test_task_command_override_is_refused_before_credentials_or_effects(self):
        config = {'taskDefinitionArn': 'exact:1', 'taskRoleArn': 'native-role', 'executionRoleArn': 'execution-role',
            'imageUri': self.config['imageUri'], 'configuration': self.config}
        task = {'taskDefinitionArn': 'exact:1', 'taskRoleArn': 'native-role', 'executionRoleArn': 'execution-role',
            'networkMode': 'awsvpc', 'runtimePlatform': {'cpuArchitecture': 'X86_64', 'operatingSystemFamily': 'LINUX'},
            'containerDefinitions': [{'name': 'native-bootstrap', 'image': self.config['imageUri'], 'command': ['sh', '-c', 'unreviewed'],
                                     'user': '10001:10001', 'readonlyRootFilesystem': True}]}
        ecs = MagicMock(); ecs.describe_task_definition.return_value = {'taskDefinition': task}
        with self.assertRaisesRegex(ValueError, 'command/isolation'):
            dispatch.verify_task(ecs, config)
