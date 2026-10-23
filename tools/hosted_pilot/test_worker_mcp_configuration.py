"""Immutable MCP payloads use only the actual retained agent/context and private sink."""
import copy
import json
import os
import unittest
from unittest.mock import MagicMock, patch
import worker_mcp_configuration as mcp
from agent_e2e_pack import PackError, canonical_digest
from test_native_bootstrap import configuration


class WorkerMcpCustody(unittest.TestCase):
    def setUp(self):
        native = configuration()
        self.operation = native['operation']
        self.candidate = native['candidateKey']
        self.task = 'arn:aws:ecs:us-east-1:123456789012:task/honua-pilot-unit-agent/'+'f'*32
        self.cfg = {'region': 'us-east-1', 'secretArn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-agent-candidate-mcp-ABC123',
                    'endpoint': native['endpoint']+'/mcp', 'workflowView': None}
        self.durable = MagicMock(); self.durable.store.candidate = self.candidate
        ledger = {'schemaVersion': 2, 'candidateKey': self.candidate, 'ceilingUsd': '500', 'cleanupReserveUsd': '50', 'entries': [],
            'operations': [{'id': reservation, 'operationId': self.operation['operationId'], 'purpose': 'bootstrap', 'kind': kind,
                'amountUsd': '5', 'basisDigest': self.operation['liabilityBasisDigest'], 'state': 'reserved', 'actualUsd': None}
                for kind, reservation in self.operation['reservationIds'].items()]}
        key = {'apiKeyId': '11111111-1111-1111-1111-111111111111', 'secretArn': native['secretArns']['agent'],
               'versionId': 'c'*64, 'permissions': ['admin:read', 'agent:workspace', 'agent:studio-compose:'+native['itemId']]}
        issued = {'producer': 'private-native-bootstrap', 'candidateKey': self.candidate, 'value': key}
        self.phase = 'sha256:'+'d'*64
        workspace = {'workspaceId': 'actual-workspace', 'targetId': 'actual-target', 'viewId': 'studio-native',
                     'revision': 0, 'selectedResources': [], 'task': None, 'tenantId': 'native-tenant'}
        turn = {'turnId': 'actual-turn', 'context': workspace, 'status': 'Active', 'messages': []}
        value = {'schema': 'honua.native-agent-context/v1', 'candidateKey': self.candidate,
            'agentContext': {'workspaceId': workspace['workspaceId'], 'turnId': turn['turnId']},
            'workspace': workspace, 'turn': turn, 'issuedAgentKey': key, 'nativeOriginals': {},
            'nativeBootstrapReference': {'key': 'original-key-agent', 'sha256': canonical_digest(issued)}}
        originals = {'original-key-agent': issued}
        for name, path, body in [('workspaceReadback', '/api/v1/agent/workspaces/actual-workspace?viewId=studio-native', workspace),
                                 ('turnReadback', '/api/v1/agent/workspaces/actual-workspace/turns/actual-turn', turn)]:
            intent = {'candidateKey': self.candidate, 'attemptId': 'native-context-'+self.phase[7:31],
                'method': 'GET', 'path': path, 'endpoint': native['endpoint'], 'credentialKind': 'api-key'}
            intent_ref = {'key': name+'-intent', 'sha256': canonical_digest(intent)}
            response = {'candidateKey': self.candidate, 'attemptId': intent['attemptId'], 'intentReference': intent_ref,
                'intentDigest': canonical_digest(intent), 'response': {'httpStatus': 200, 'body': body}}
            originals[intent_ref['key']] = intent; originals[name+'-response'] = response
            value['nativeOriginals'][name] = {'reference': {'key': name+'-response', 'sha256': canonical_digest(response)}}
        self.reference = {'key': self.phase+'/completed', 'sha256': canonical_digest(value)}
        self.durable.state = {'ledger': ledger, 'scenarioOperations': originals, 'studioBootstrapOperations': {self.reference['key']: value}}
        self.secrets = MagicMock(); self.secrets.put_secret_value.side_effect = lambda **kw: {'ARN': kw['SecretId'], 'VersionId': kw['ClientRequestToken']}
        self.ecs = MagicMock()
        self.ecs.describe_tasks.return_value = {'tasks': [{'taskArn': self.task, 'taskDefinitionArn': 'exact-controller:1'}]}
        self.ecs.describe_task_definition.return_value = {'taskDefinition': {'containerDefinitions': [{'name': 'campaign-controller',
            'secrets': [{'name': 'HONUA_AGENT_REQUESTER_KEY', 'valueFrom': key['secretArn']+':::'+key['versionId']}]}]}}

    def provision(self):
        with patch.dict(os.environ, {'PILOT_CONTROLLER_TASK_ARN': self.task, 'HONUA_AGENT_REQUESTER_KEY': 'actual-private-agent-key-'+'k'*48}):
            return mcp.provision(self.durable, self.reference, self.operation, self.cfg, secrets=self.secrets, ecs=self.ecs)

    def test_only_agent_key_and_real_context_reach_immutable_private_mcp_payload(self):
        result = self.provision()
        written = self.secrets.put_secret_value.call_args.kwargs
        payload = json.loads(written['SecretString'])
        self.assertEqual(set(payload), {'endpoint', 'credentialKind', 'credential', 'workflowView', 'agentContext'})
        self.assertEqual(payload['agentContext'], {'workspaceId': 'actual-workspace', 'turnId': 'actual-turn'})
        self.assertEqual(written['ClientRequestToken'], result['versionId'])
        self.assertFalse(result['qualification'])
        self.assertNotIn('actual-private-agent-key-', json.dumps(self.durable.state))
        with self.assertRaisesRegex(PackError, 'already started'):
            self.provision()
        self.assertEqual(self.secrets.put_secret_value.call_count, 1)

    def test_wrong_injected_key_version_cannot_be_copied_into_worker(self):
        self.ecs.describe_task_definition.return_value['taskDefinition']['containerDefinitions'][0]['secrets'][0]['valueFrom'] += 'different'
        with self.assertRaisesRegex(PackError, 'exact original immutable'):
            self.provision()
        self.secrets.put_secret_value.assert_not_called()

    def test_changed_native_original_is_refused_even_with_rehashed_declared_reference(self):
        value = self.durable.state['studioBootstrapOperations'][self.reference['key']]
        reference = value['nativeOriginals']['turnReadback']['reference']
        original = self.durable.state['scenarioOperations'][reference['key']]
        original['response']['body'] = {'turnId': 'forged-other-turn'}
        reference['sha256'] = canonical_digest(original)
        self.reference['sha256'] = canonical_digest(value)
        with self.assertRaisesRegex(PackError, 'changed after observation'):
            self.provision()
        self.secrets.put_secret_value.assert_not_called()

    def test_lost_secret_write_retains_intent_without_publishing_a_success(self):
        self.secrets.put_secret_value.side_effect = TimeoutError('native secret write acknowledgement lost')
        with self.assertRaises(TimeoutError):
            self.provision()
        records = self.durable.state['scenarioOperations']
        self.assertTrue(any(key.startswith('native-worker-mcp:') and key.endswith('/intent') for key in records))
        self.assertFalse(any(key.startswith('native-worker-mcp:') and key.endswith('/completed') for key in records))
        with self.assertRaisesRegex(PackError, 'already started'):
            self.provision()
