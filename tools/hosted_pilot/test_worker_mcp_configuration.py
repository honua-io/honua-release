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
        human = {'actorId': 'actual-human', 'studioOwnerId': 'actual-human', 'tenantId': 'native-tenant',
                 'authenticationKind': 'operator-bearer', 'isHumanSession': True}
        principal = {'actorId': 'apikey:api-key:'+key['apiKeyId'], 'studioOwnerId': key['apiKeyId'],
                     'tenantId': 'native-tenant', 'authenticationKind': 'api-key', 'isHumanSession': False}
        value.update(studioOwnerPrincipalId=key['apiKeyId'], workspaceAgentPrincipalId=principal['actorId'],
                     agentPrincipal=principal, humanCreatorPrincipal=human)
        originals = {'original-key-agent': issued}
        for name, path, body in [('workspaceReadback', '/api/v1/agent/workspaces/actual-workspace?viewId=studio-native', workspace),
                                 ('turnReadback', '/api/v1/agent/workspaces/actual-workspace/turns/actual-turn', turn),
                                 ('agentTarget', '/api/v1/agent/target', {'targetId': 'actual-target', 'principal': principal}),
                                 ('target', '/api/v1/agent/target', {'targetId': 'actual-target', 'principal': human})]:
            intent = {'candidateKey': self.candidate, 'attemptId': 'native-context-'+self.phase[7:31],
                'method': 'GET', 'path': path, 'endpoint': native['endpoint'], 'credentialKind': 'bearer' if name == 'target' else 'api-key'}
            intent_ref = {'key': name+'-intent', 'sha256': canonical_digest(intent)}
            response = {'candidateKey': self.candidate, 'attemptId': intent['attemptId'], 'intentReference': intent_ref,
                'intentDigest': canonical_digest(intent), 'response': {'httpStatus': 200, 'body': body}}
            originals[intent_ref['key']] = intent; originals[name+'-response'] = response
            value['nativeOriginals'][name] = {'reference': {'key': name+'-response', 'sha256': canonical_digest(response)}}
        self.reference = {'key': self.phase+'/completed', 'sha256': canonical_digest(value)}
        self.durable.state = {'ledger': ledger, 'scenarioOperations': originals, 'studioBootstrapOperations': {self.reference['key']: value}}
        self.versions = {}
        self.secrets = MagicMock()
        def put(**kw):
            response = {'ARN': kw['SecretId'], 'VersionId': kw['ClientRequestToken'], 'SecretString': kw['SecretString']}
            self.versions[kw['ClientRequestToken']] = response
            return {key: response[key] for key in ['ARN', 'VersionId']}
        self.put = put
        self.secrets.put_secret_value.side_effect = put
        self.secrets.get_secret_value.side_effect = lambda **kw: self.versions[kw['VersionId']]
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
        self.assertEqual(self.provision(), result)
        self.assertEqual(self.secrets.put_secret_value.call_count, 1)
        self.secrets.get_secret_value.assert_called_once_with(SecretId=self.cfg['secretArn'], VersionId=result['versionId'])

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
        def lost(**kw):
            self.put(**kw)
            raise TimeoutError('native secret write acknowledgement lost')
        self.secrets.put_secret_value.side_effect = lost
        with self.assertRaises(TimeoutError):
            self.provision()
        records = self.durable.state['scenarioOperations']
        self.assertTrue(any(key.startswith('native-worker-mcp:') and key.endswith('/intent') for key in records))
        self.assertFalse(any(key.startswith('native-worker-mcp:') and key.endswith('/completed') for key in records))
        result = self.provision()
        self.assertEqual(self.secrets.put_secret_value.call_count, 1)
        self.assertEqual(result['versionId'], next(iter(self.versions)))
        self.assertFalse(result['qualification'])
        self.assertNotIn('actual-private-agent-key-', json.dumps(self.durable.state))

    def test_crash_after_write_before_completion_persist_recovers_original_version(self):
        persisted = []
        def persist():
            if persisted:
                raise RuntimeError('completion persist lost')
            persisted.append(copy.deepcopy(self.durable.state))
        self.durable._persist.side_effect = persist
        with self.assertRaisesRegex(RuntimeError, 'completion persist lost'):
            self.provision()
        self.durable.state = persisted[0]
        self.durable._persist.side_effect = None
        result = self.provision()
        self.assertEqual(self.secrets.put_secret_value.call_count, 1)
        self.assertEqual(self.secrets.get_secret_value.call_count, 1)
        self.assertEqual(result['versionId'], next(iter(self.versions)))

    def test_unknown_write_without_original_version_never_remints(self):
        self.secrets.put_secret_value.side_effect = TimeoutError('unknown write')
        with self.assertRaises(TimeoutError):
            self.provision()
        with self.assertRaises(KeyError):
            self.provision()
        self.assertEqual(self.secrets.put_secret_value.call_count, 1)
        self.assertFalse(any(key.endswith('/completed') for key in self.durable.state['scenarioOperations']))

    def test_recovery_rejects_changed_version_metadata_or_private_bytes(self):
        result = self.provision()
        original = self.versions[result['versionId']]
        for field, value in [('ARN', self.cfg['secretArn']+'other'), ('VersionId', 'f'*64),
                             ('SecretString', original['SecretString']+' '), ('SecretBinary', b'unexpected')]:
            with self.subTest(field=field):
                self.versions[result['versionId']] = {**original, field: value}
                with self.assertRaisesRegex(PackError, 'immutable secret version or private bytes differ'):
                    self.provision()
        self.assertEqual(self.secrets.put_secret_value.call_count, 1)

    def test_recovery_rejects_changed_public_intent_before_reading_secret(self):
        self.provision()
        self.cfg['workflowView'] = 'changed-view'
        with self.assertRaisesRegex(PackError, 'retained provisioning intent differs'):
            self.provision()
        self.secrets.get_secret_value.assert_not_called()
        self.assertEqual(self.secrets.put_secret_value.call_count, 1)


class AdoptedWorkerMcp(unittest.TestCase):
    setUp = WorkerMcpCustody.setUp
    provision = WorkerMcpCustody.provision

    def adopt(self):
        from agent_e2e_pack import digest_bytes, load_json, select
        from browser_session import operator_partition
        from pathlib import Path
        value = self.durable.state['studioBootstrapOperations'][self.reference['key']]
        records = self.durable.state['scenarioOperations']
        human = value['humanCreatorPrincipal']; human['subjectId'] = 'actual-console-subject'
        pack_path = Path(__file__).resolve().parents[2]/'certification/agent-e2e/pack.v2.json'
        case_id = 'P14:A:aws-ecs-redis-on'
        case, scenario = select(load_json(pack_path), case_id)
        turn = value['turn']
        turn.update(originatingActorId=human['actorId'], status='accepted', messages=[
            {'id': turn['turnId']+'-user', 'turnId': turn['turnId'], 'role': 'user', 'content': scenario['prompts'][case['variant']]}])
        profile = {'id': 'actual-profile', 'serverUri': self.cfg['endpoint'].removesuffix('/mcp')}
        observed = {'consoleProfile': profile, 'nativeTarget': {'targetId': value['workspace']['targetId'], 'principal': human},
            'session': {'authenticated': True, 'environmentId': profile['id'], 'principalId': operator_partition(human['subjectId'], profile)},
            'deployment': {'actual': 'deployment'}}
        custody = {'schema': 'honua.private-console-human-session/v1', 'producer': 'private-console-human-capture',
            'candidateKey': self.candidate, 'qualification': False, 'configuration': {'consoleProfile': profile}, 'observed': observed}
        records['original-session'] = custody
        declaration = {'workspaceId': value['workspace']['workspaceId'], 'sessionCustody': {'key': 'original-session', 'sha256': canonical_digest(custody)},
            'pack': {'path': 'certification/agent-e2e/pack.v2.json', 'sha256': digest_bytes(pack_path.read_bytes())}, 'caseId': case_id,
            'contextSha256': canonical_digest(value['workspace']), 'sourceSha256': canonical_digest({
                name: None for name in ('draft', 'localProcessSnapshot', 'datasetImportContext', 'browserFileSelection')})}
        value['consoleTurn'] = {'configuration': declaration, 'sessionObservation': observed}
        for name, original_name in [('workspace', 'workspaceReadback'), ('turn', 'turnReadback')]:
            source = value['nativeOriginals'][original_name]['reference']
            response = copy.deepcopy(records[source['key']])
            original_intent = records[response['intentReference']['key']]
            intent = dict(original_intent, credentialKind='bearer')
            intent_ref = {'key': name+'-human-intent', 'sha256': canonical_digest(intent)}
            records[intent_ref['key']] = intent
            response.update(intentReference=intent_ref, intentDigest=intent_ref['sha256'])
            records[name+'-human-response'] = response
            value['nativeOriginals'][name] = {'reference': {'key': name+'-human-response', 'sha256': canonical_digest(response)}}
        for name in ('target', 'turnReadback', 'workspace', 'turn'):
            reference = value['nativeOriginals'][name]['reference']
            reference['sha256'] = canonical_digest(records[reference['key']])
        self.reference['sha256'] = canonical_digest(value)
        return value

    def test_original_turn_gets_a_distinct_immutable_version_without_rebinding_fixture_context(self):
        preparatory = self.provision()
        value = self.adopt()
        adopted = self.provision()
        self.assertNotEqual(preparatory['versionId'], adopted['versionId'])
        self.assertEqual(adopted['nativeContext'], self.reference)
        self.assertEqual(self.provision(), adopted)
        self.assertEqual(self.secrets.put_secret_value.call_count, 2)
        self.assertEqual(value['turn']['originatingActorId'], value['humanCreatorPrincipal']['actorId'])

    def test_rehashed_turn_with_wrong_original_author_refuses_before_secret_write(self):
        value = self.adopt()
        value['turn']['originatingActorId'] = 'different-human'
        for name in ('turn', 'turnReadback'):
            ref = value['nativeOriginals'][name]['reference']
            self.durable.state['scenarioOperations'][ref['key']]['response']['body'] = value['turn']
            ref['sha256'] = canonical_digest(self.durable.state['scenarioOperations'][ref['key']])
        self.reference['sha256'] = canonical_digest(value)
        with self.assertRaisesRegex(ValueError, 'human-authored'):
            self.provision()
        self.secrets.put_secret_value.assert_not_called()

    def test_missing_original_human_turn_read_cannot_be_replaced_by_agent_readback(self):
        value = self.adopt()
        reference = value['nativeOriginals']['turn']['reference']
        del self.durable.state['scenarioOperations'][reference['key']]
        with self.assertRaisesRegex(ValueError, 'absent or changed'):
            self.provision()
        self.secrets.put_secret_value.assert_not_called()
