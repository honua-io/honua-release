"""Private preparation preserves actual native bindings and source-only expectations."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import controller_handoff as handoff
import prepare
import preparation_review
from agent_e2e_pack import canonical_digest


class PreparationBoundary(unittest.TestCase):
    def manifest(self):
        return {'schema': 'honua.private-controller-preparation/v1', 'componentBindingDigest': 'sha256:'+'a'*64,
            'controller': {'repository': 'honua-io/honua-release', 'workflow': handoff.PREPARE_WORKFLOW, 'sourceSha': 'b'*40, 'runId': 7, 'runAttempt': 1},
            'expiresAt': '2026-10-30T00:00:00Z', 'state': {'region': 'us-east-1', 'bucket': 'retained', 'table': 'retained'},
            'operation': 'prepare-native-context', 'configuration': {'path': 'certification/agent-e2e/context-prepare.json', 'sha256': 'sha256:'+'c'*64}}

    def test_preparation_manifest_has_distinct_schema_and_cannot_select_campaign_cases(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'preparation.json'; path.write_text(json.dumps(self.manifest()))
            document, identity, state = handoff.bundle_identity(folder, operation='prepare-native-context', case_id=None, target=None, shard=0, shards=6)
            self.assertEqual(identity['schema'], 'honua.private-controller-preparation-invocation/v1')
            self.assertNotIn('campaignDigest', identity)
            self.assertEqual(state, document['state'])
            with self.assertRaisesRegex(ValueError, 'cannot select'):
                handoff.bundle_identity(folder, operation='prepare-native-context', case_id='P14:A:aws-ecs-redis-on', target=None, shard=0, shards=6)
            bad = self.manifest(); bad['controller']['workflow'] = handoff.WORKFLOW; path.write_text(json.dumps(bad))
            with self.assertRaisesRegex(ValueError, 'distinct private preparation'):
                handoff.bundle_identity(folder, operation='prepare-native-context', case_id=None, target=None, shard=0, shards=6)

    def context_setup(self, *, changed_context=False):
        candidate = 'sha256:'+'a'*64
        config = {'candidateKey': candidate, 'operation': {'operationId': 'original-context', 'liabilityBasisDigest': 'sha256:'+'b'*64,
            'reservationIds': {'infrastructure': 'original-infra', 'cleanup': 'original-cleanup'}},
            'nativeBootstrapOperationId': 'original-native', 'endpoint': 'https://agent.pilot.demo.honua.io',
            'requesterCredentialEnv': 'HONUA_AGENT_REQUESTER_KEY', 'viewId': 'studio-native', 'turnId': 'actual-requested-turn'}
        key = canonical_digest({'kind': 'native-bootstrap', 'candidateKey': candidate, 'operationId': 'original-native'})
        durable = MagicMock(); durable.store.candidate = candidate
        issued = {'apiKeyId': '11111111-1111-1111-1111-111111111111', 'secretArn': 'private-key-ref', 'versionId': 'immutable', 'permissions': ['admin:read', 'agent:workspace']}
        durable.state = {'scenarioOperations': {key+'/key-agent': {'producer': 'private-native-bootstrap', 'candidateKey': candidate, 'value': issued}}}
        workspace = {'workspaceId': 'actual-server-workspace', 'targetId': 'actual-target', 'viewId': 'studio-native',
            'revision': 0, 'selectedResources': [], 'task': None, 'tenantId': 'actual-tenant'}
        turn = {'turnId': 'actual-requested-turn', 'status': 'Active', 'context': copy.deepcopy(workspace), 'messages': []}
        if changed_context:
            turn['context']['revision'] = 1
        client = MagicMock()
        client.json.side_effect = [{'observation': {'httpStatus': 200, 'body': value}} for value in
            ({'targetId': 'actual-target'}, workspace, turn, workspace, turn)]
        return config, durable, client, workspace

    def test_actual_workspace_and_turn_are_read_back_under_issued_identity_without_guessed_headers(self):
        config, durable, client, workspace = self.context_setup()
        with patch('prepare.BootstrapHttp', return_value=client), patch.dict(os.environ, {'PILOT_CONTROLLER_TASK_ARN': 'actual-native-task'}):
            result = prepare.native_context(durable, config)
        self.assertFalse(result['qualification'])
        self.assertEqual(result['value']['agentContext'], {'workspaceId': workspace['workspaceId'], 'turnId': config['turnId']})
        self.assertEqual(client.json.call_args_list[2].args[3]['contextRevision'], workspace['revision'])
        self.assertEqual(client.json.call_count, 5)
        with patch('prepare.BootstrapHttp', return_value=client), patch.dict(os.environ, {'PILOT_CONTROLLER_TASK_ARN': 'actual-native-task'}):
            with self.assertRaisesRegex(ValueError, 'already started'):
                prepare.native_context(durable, config)
        self.assertEqual(client.json.call_count, 5)

    def test_changed_native_turn_context_retains_original_start_and_never_retries(self):
        config, durable, client, _ = self.context_setup(changed_context=True)
        with patch('prepare.BootstrapHttp', return_value=client), patch.dict(os.environ, {'PILOT_CONTROLLER_TASK_ARN': 'actual-native-task'}):
            with self.assertRaisesRegex(ValueError, 'actual context/turn'):
                prepare.native_context(durable, config)
        self.assertEqual(client.json.call_count, 3)
        self.assertTrue(any(key.endswith('/start') for key in durable.state['studioBootstrapOperations']))
        self.assertFalse(any(key.endswith('/completed') for key in durable.state['studioBootstrapOperations']))

    def test_review_projection_refuses_task_or_candidate_substitution(self):
        result = {'schema': 'honua.private-controller-preparation-result/v1', 'operation': 'prepare-studio-fixture',
            'candidateKey': 'sha256:'+'a'*64, 'sourceConfiguration': {}, 'producerTaskArn': 'actual-task', 'qualification': False,
            'requiresSourceReviewBeforeFreeze': True, 'result': {'key': 'sha256:'+'b'*64, 'sha256': 'sha256:'+'c'*64,
            'fixture': {'itemId': 'actual-item', 'draftId': 'actual-draft', 'versionId': 'actual-version', 'contentHash': 'actual-hash', 'namespace': 'actual-ns', 'ownerPrincipalId': 'actual-owner', 'privateBody': 'must-not-project'}}}
        outcome = {'status': 'completed', 'exitCode': 0, 'taskArn': 'actual-task', 'outcomeObject': {'sha256': 'original'},
            'invocation': {'candidateKey': result['candidateKey'], 'operation': result['operation']}}
        projected = preparation_review.project(result, outcome)
        self.assertNotIn('must-not-project', json.dumps(projected))
        self.assertTrue(projected['requiresSourceReviewBeforeFreeze'])
        with self.assertRaisesRegex(ValueError, 'verified native outcome'):
            preparation_review.project(result, outcome | {'taskArn': 'other-task'})
