"""Native controller dispatch keeps immutable source, paid allowance and output custody."""
import copy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import controller_archive as archive
import controller_budget as budget
import controller_entry as entry
import controller_handoff as handoff


class NativeBudget(unittest.TestCase):
    def setUp(self):
        self.candidate, self.basis = 'sha256:' + 'a' * 64, 'sha256:' + 'b' * 64
        marker = archive.canonical({'schemaVersion': 1, 'candidateKey': self.candidate, 'generationId': 'original'})
        self.config = {'state': {'region': 'us-east-1', 'bucket': 'retained', 'table': 'state'}, 'candidateKey': self.candidate,
            'generationDigest': archive.digest(marker), 'reservationId': 'controller-original', 'operationId': 'controller-shared',
            'basisDigest': self.basis, 'perDispatchCeilingUsd': '2', 'maximumDispatches': 4, 'claimTable': 'claims'}
        self.ledger = {'schemaVersion': 2, 'candidateKey': self.candidate, 'ceilingUsd': '500', 'cleanupReserveUsd': '50', 'entries': [],
            'operations': [{'id': 'controller-original', 'operationId': 'controller-shared', 'purpose': 'shared-environment', 'kind': 'infrastructure', 'amountUsd': '8', 'basisDigest': self.basis, 'state': 'reserved', 'actualUsd': None}]}
        self.marker = marker
        self.s3, self.db = MagicMock(), MagicMock()
        self.reset()

    def reset(self, *, payload=None):
        snapshot = archive.canonical({'candidateKey': self.candidate, 'generationDigest': self.config['generationDigest'], 'version': 7, 'payload': payload or {'ledger': self.ledger}})
        prefix = 'agent-e2e/' + self.candidate[7:] + '/'
        self.row = {'generationDigest': {'S': self.config['generationDigest']}, 'generationKey': {'S': prefix + 'initialization.json'}, 'generationVersion': {'S': 'gen-v1'}, 'snapshotKey': {'S': prefix + 'snapshot.json'}, 'snapshotVersion': {'S': 'snapshot-v7'}, 'snapshotDigest': {'S': archive.digest(snapshot)}, 'stateVersion': {'N': '7'}}
        self.db.get_item.return_value = {'Item': self.row}
        def get(**kwargs):
            content = self.marker if kwargs['VersionId'] == 'gen-v1' else snapshot
            return {'VersionId': kwargs['VersionId'], 'Body': io.BytesIO(content)}
        self.s3.get_object.side_effect = get

    def test_existing_native_generation_snapshot_and_reserved_operation_bind_capacity(self):
        self.assertEqual(budget.read_allowance(self.config, self.s3, self.db), self.row)
        self.db.get_item.return_value = {}
        budget.consume(self.config, db=self.db, row=self.row, invocation='i', binding_digest=self.basis, execution_arn='arn:execution')
        operations = self.db.transact_write_items.call_args.kwargs['TransactItems']
        self.assertEqual(len(operations), 3)
        self.assertEqual(operations[0]['ConditionCheck']['ExpressionAttributeValues'][':snapshot'], self.row['snapshotDigest'])
        self.assertIn('used < :maximum', operations[1]['Update']['ConditionExpression'])
        self.assertIn('attribute_not_exists', operations[2]['Put']['ConditionExpression'])
        self.s3.put_object.assert_not_called()
        self.db.update_item.assert_not_called()

    def test_missing_native_generation_never_initializes_allowance(self):
        self.db.get_item.return_value = {}
        with self.assertRaisesRegex(ValueError, 'never initialize'):
            budget.read_allowance(self.config, self.s3, self.db)
        self.s3.put_object.assert_not_called()
        self.db.transact_write_items.assert_not_called()

    def test_capacity_cannot_exceed_original_reserved_amount(self):
        self.config['maximumDispatches'] = 5
        with self.assertRaisesRegex(ValueError, 'exceeds its original reservation'):
            budget.read_allowance(self.config, self.s3, self.db)

    def test_settled_or_other_basis_operation_cannot_pay_new_dispatch(self):
        for changes in [{'state': 'settled', 'actualUsd': '0'}, {'basisDigest': 'sha256:' + 'f' * 64}]:
            with self.subTest(changes=changes):
                original = copy.deepcopy(self.ledger)
                self.ledger['operations'][0].update(changes)
                self.reset()
                with self.assertRaisesRegex(ValueError, 'retained reserved'):
                    budget.read_allowance(self.config, self.s3, self.db)
                self.ledger = original

    def test_dispatch_does_not_double_allocate_backend_overhead_reservation(self):
        self.reset(payload={'ledger': self.ledger, 'nativeOverheadAllocations': {'existing': {'reservationId': 'controller-original'}}})
        with self.assertRaisesRegex(ValueError, 'already allocated'):
            budget.read_allowance(self.config, self.s3, self.db)

    def test_lost_ack_observes_same_claim_without_consuming_capacity_twice(self):
        self.db.get_item.return_value = {'Item': {'bindingDigest': {'S': self.basis}, 'executionArn': {'S': 'same'}}}
        self.assertFalse(budget.consume(self.config, db=self.db, row=self.row, invocation='same', binding_digest=self.basis, execution_arn='same'))
        self.db.transact_write_items.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'different inputs'):
            budget.consume(self.config, db=self.db, row=self.row, invocation='same', binding_digest='other', execution_arn='same')


class ProvenanceAndSelections(unittest.TestCase):
    def campaign(self, mode='development'):
        return {'campaignMode': mode, 'componentBindingDigest': 'sha256:' + 'a' * 64,
            'controller': {'repository': 'honua-io/honua-release', 'workflow': handoff.WORKFLOW,
                           'sourceSha': 'b' * 40, 'runId': 20, 'runAttempt': 1}}

    def test_invocation_is_deterministic_and_separates_operation_case_and_shard(self):
        args = {'operation': 'execute', 'case_id': 'P10:A:aws-ecs-redis-on', 'target': None, 'shard': 0, 'shards': 6}
        first = handoff.invocation_identity(self.campaign(), **args)
        self.assertEqual(first, handoff.invocation_identity(self.campaign(), **args))
        self.assertNotEqual(first, handoff.invocation_identity(self.campaign(), **(args | {'case_id': 'P10:B:aws-ecs-redis-on'})))
        for bad in [args | {'shards': 1}, args | {'case_id': None}, args | {'operation': 'finalize'}]:
            with self.assertRaises(ValueError):
                handoff.invocation_identity(self.campaign(), **bad)
        with self.assertRaisesRegex(ValueError, 'partial campaign'):
            handoff.invocation_identity(self.campaign('qualification'), **args)

    def test_source_verification_uses_original_native_run_attempt_tree_and_protected_trunk(self):
        identity = self.campaign()['controller']
        source = {'sourceSha': identity['sourceSha'], 'treeId': 'c' * 40}
        responses = [{'id': 9}, {'id': 20, 'workflow_id': 9, 'head_sha': 'b' * 40, 'head_branch': 'trunk', 'event': 'workflow_dispatch', 'run_attempt': 1, 'repository': {'full_name': identity['repository']}},
            {'protected': True}, {'merge_base_commit': {'sha': 'b' * 40}, 'base_commit': {'sha': 'b' * 40, 'commit': {'tree': {'sha': 'c' * 40}}}}]
        with patch('controller_entry.http_json', side_effect=responses) as http:
            receipt = entry.github_provenance(identity, source)
        self.assertEqual(receipt['treeId'], 'c' * 40)
        self.assertIn('/attempts/1', http.call_args_list[1].args[0])
        responses[2] = {'protected': False}
        with patch('controller_entry.http_json', side_effect=responses), self.assertRaisesRegex(ValueError, 'protected trunk'):
            entry.github_provenance(identity, source)

    def test_source_tree_substitution_is_refused(self):
        identity = self.campaign()['controller']
        responses = [{'id': 9}, {'id': 20, 'workflow_id': 9, 'head_sha': 'b' * 40, 'head_branch': 'trunk', 'event': 'workflow_dispatch', 'run_attempt': 1, 'repository': {'full_name': identity['repository']}},
            {'protected': True}, {'merge_base_commit': {'sha': 'b' * 40}, 'base_commit': {'sha': 'b' * 40, 'commit': {'tree': {'sha': 'wrong'}}}}]
        with patch('controller_entry.http_json', side_effect=responses), self.assertRaisesRegex(ValueError, 'source/tree'):
            entry.github_provenance(identity, {'sourceSha': 'b' * 40, 'treeId': 'c' * 40})
