"""Actual DurableExecution recovery without a new generation or allowance."""
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import budget_bootstrap
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_state import DurableExecution
import test_budget_initialization as fixture


class AccountingRecovery(unittest.TestCase):
    def setUp(self):
        self.original = fixture.InitialAccounting()
        self.original.setUp()
        self.commits = []
        commit = self.original.store.commit
        def tracked(state):
            self.commits.append(copy.deepcopy(state))
            commit(state)
        self.original.store.commit = tracked
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def initialize(self, name, **kwargs):
        return self.original.initialize(self.root/name, **kwargs)

    def test_process_loss_after_commit_recovers_receipt_without_second_generation(self):
        def lose_receipt(*args, **kwargs):
            durable = DurableExecution(*args, **kwargs)
            durable.close()
            raise SystemExit('process exited after commit and before receipt')
        with self.assertRaises(SystemExit):
            self.initialize('lost.json', execution=lose_receipt)
        state, generation = copy.deepcopy(self.original.store.state), copy.deepcopy(self.original.store.generation)
        receipt = self.initialize('recover.json', recover=True)
        self.assertEqual(len(self.commits), 1)
        self.assertEqual(self.original.store.state, state)
        self.assertEqual(receipt['generation'], generation)
        self.assertEqual(receipt['ledgerDigest'], canonical_digest(state['ledger']))
        self.assertEqual(receipt['bootstrapLedgerDigest'], canonical_digest(self.original.ledger))
        self.assertEqual(receipt['mode'], 'recovered')
        self.assertFalse(receipt['qualification'])
        self.assertEqual(receipt['modelExecution'], 'not-executed-by-recovery')
        again = self.initialize('recover-again.json', recover=True)
        self.assertEqual(receipt, again)
        self.assertEqual(len(self.commits), 1)

    def test_recovery_preserves_later_liability_and_reports_current_digest(self):
        first = self.initialize('first.json')
        ledger = self.original.store.state['ledger']
        ledger['operations'].append({'id':'later-observed-hold','operationId':'later-operation',
            'purpose':'telemetry','kind':'infrastructure','amountUsd':'5','basisDigest':'sha256:'+'f'*64,
            'state':'reserved','actualUsd':None})
        before = copy.deepcopy(self.original.store.state)
        recovered = self.initialize('recover.json', recover=True)
        self.assertEqual(self.original.store.state, before)
        self.assertEqual(recovered['generation'], first['generation'])
        self.assertEqual(recovered['ledgerDigest'], canonical_digest(ledger))
        self.assertNotEqual(recovered['ledgerDigest'], recovered['bootstrapLedgerDigest'])
        self.assertEqual(len(self.commits), 1)

    def test_original_report_aging_cannot_mint_new_state_but_can_recover_committed_receipt(self):
        self.initialize('first.json')
        later = datetime.now(timezone.utc)+timedelta(hours=2)
        class Later(datetime):
            @classmethod
            def now(cls, tz=None): return later if tz else later.replace(tzinfo=None)
        before = copy.deepcopy(self.original.store.state)
        with patch.object(budget_bootstrap, 'datetime', Later):
            with self.assertRaisesRegex(ValueError, 'stale, future or inconsistent'):
                self.initialize('not-a-new-initialization.json')
            recovered = self.initialize('recover.json', recover=True)
        self.assertEqual(recovered['mode'], 'recovered')
        self.assertEqual(self.original.store.state, before)
        self.assertEqual(len(self.commits), 1)

    def test_recovery_of_absent_state_never_falls_back_to_initialization(self):
        with self.assertRaisesRegex(PackError, 'requires explicit initialization'):
            self.initialize('absent.json', recover=True)
        self.assertIsNone(self.original.store.state)
        self.assertEqual(self.commits, [])

    def test_recovery_refuses_foreign_bootstrap_without_mutating_it(self):
        self.initialize('first.json')
        self.original.store.state['bootstrapImport']['artifactDigest'] = 'sha256:'+'f'*64
        before = copy.deepcopy(self.original.store.state)
        with self.assertRaisesRegex(ValueError, 'authenticated original bootstrap'):
            self.initialize('foreign.json', recover=True)
        self.assertEqual(self.original.store.state, before)
        self.assertEqual(len(self.commits), 1)

    def test_recovery_cannot_erase_original_spend_or_liabilities(self):
        self.initialize('first.json')
        self.original.store.state['ledger']['operations'][0]['amountUsd'] = '0'
        before = copy.deepcopy(self.original.store.state)
        with self.assertRaises(ValueError):
            self.initialize('reset.json', recover=True)
        self.assertEqual(self.original.store.state, before)
        self.assertEqual(len(self.commits), 1)

    def test_recovery_requires_actual_generation_metadata_and_original_expiry(self):
        self.initialize('first.json')
        self.original.store.generation = None
        with self.assertRaisesRegex(ValueError, 'immutable generation receipt'):
            self.initialize('unanchored.json', recover=True)
        self.original.config['expiresAt'] = '2000-01-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError, 'missing or expired'):
            self.initialize('expired.json', recover=True)
        self.assertEqual(len(self.commits), 1)


if __name__ == '__main__':
    unittest.main()
