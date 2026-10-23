"""Initial accounting retains uncertain nonzero liabilities and refuses resets."""
import copy
from datetime import datetime, timedelta, timezone
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
import budget_bootstrap
import initialize_accounting
from agent_e2e_budget import history_digest, validate
from agent_e2e_gate import component_binding
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_state import DurableExecution, preserve_history


class Store:
    def __init__(self, candidate):
        self.candidate, self.state, self.generation = candidate, None, None
    def acquire(self): return copy.deepcopy(self.state)
    def renew(self): pass
    def release(self): pass
    def commit(self, state):
        preserve_history(self.state, state)
        self.state = copy.deepcopy(state)
        self.generation = {'key': 'actual-created-initialization', 'version': 'actual-native-version', 'digest': 'sha256:'+'e'*64}


class InitialAccounting(unittest.TestCase):
    def setUp(self):
        self.candidate = 'sha256:'+'a'*64
        self.history = {'schema': 'honua.hosted-pilot-budget-history/v1', 'wholeAccountZeroSpend': False,
            'repositories': {'honua-io/honua-release': {'producerRuns': []}},
            'cloudTrail': {'RunTask': {'pilotEvents': []}}, 'billing': {'grossUsageUsd': '149.4013538965'}}
        now = datetime.now(timezone.utc)
        cutoff = (now-timedelta(minutes=10)).replace(microsecond=123456)
        self.history.update(observedAt=(now-timedelta(minutes=5)).isoformat(),
            coverage={'start':(cutoff-timedelta(days=1)).isoformat(), 'cutoff':cutoff.isoformat()})
        self.history['billing']['queryPeriod'] = {'Start':(cutoff-timedelta(days=1)).date().isoformat(),
            'End':(cutoff+timedelta(days=1)).date().isoformat()}
        self.ledger = {'schemaVersion': 2, 'candidateKey': self.candidate, 'ceilingUsd': '500', 'cleanupReserveUsd': '50', 'entries': [], 'operations': []}
        for id, purpose, kind, amount, basis in [('initialization-history-usage', 'telemetry', 'infrastructure', '151', canonical_digest(self.history)),
                ('bootstrap-infra', 'bootstrap', 'infrastructure', '100', 'sha256:'+'b'*64),
                ('bootstrap-cleanup', 'bootstrap', 'cleanup', '20', 'sha256:'+'b'*64)]:
            self.ledger['operations'].append({'id': id, 'operationId': 'history' if purpose=='telemetry' else 'bootstrap',
                'purpose': purpose, 'kind': kind, 'amountUsd': amount, 'basisDigest': basis, 'state': 'reserved', 'actualUsd': None})
        prior = copy.deepcopy(self.ledger)
        self.ledger.update(schemaVersion=3, ceilingUsd='2000', cleanupReserveUsd='200', paidAdmissions=[],
            policyAmendment={'policyId': 'foss4g-ai-2026-owner-2000-200', 'priorLedger': prior, 'priorLedgerDigest': history_digest(prior)})
        source = {'repository': 'honua-io/honua-release', 'workflow': '.github/workflows/agent-e2e-campaign.yml',
                  'sourceSha': 'c'*40, 'runId': 17, 'runAttempt': 1}
        self.imported = {'artifactDigest': 'sha256:'+'d'*64,
            'artifact': {'schemaVersion': 1, 'candidateKey': self.candidate, 'ledger': self.ledger, 'source': source}}
        self.config = {'schemaVersion': 1, 'candidateKey': self.candidate,
            'state': {'region': 'us-east-1', 'bucket': 'honua-agent-'+'a'*24+'-585192672263', 'table': 'honua-agent-'+'a'*24+'-state'},
            'initializerRoleArn': 'arn:aws:iam::585192672263:role/honua-pilot-test-agent-accounting-initializer',
            'bootstrap': {'artifactDigest': self.imported['artifactDigest'], 'sourceSha': source['sourceSha'], 'runId': 17, 'runAttempt': 1},
            'history': {'path': 'reviewed', 'sha256': canonical_digest(self.history)}, 'expiresAt': '2099-01-01T00:00:00Z'}
        self.sts = MagicMock(); self.sts.get_caller_identity.return_value = {'Account': '585192672263',
            'Arn': 'arn:aws:sts::585192672263:assumed-role/honua-pilot-test-agent-accounting-initializer/checked-workflow'}
        self.store = Store(self.candidate)

    def initialize(self, path, **kwargs):
        with patch('initialize_accounting.reviewed', return_value=self.history):
            return initialize_accounting.initialize(self.config, Path('original-artifact.json'), path,
                verify=lambda *args: self.imported, open_state=lambda *args: self.store, sts=self.sts, **kwargs)

    def producer_case(self):
        lock = {'components': {'server': {'sha': 'f'*40}}, 'contentDigests': {'corpus': 'sha256:'+'b'*64}}
        candidate = component_binding(lock)
        ledger = copy.deepcopy(self.ledger)
        ledger['candidateKey'] = candidate
        ledger['policyAmendment']['priorLedger']['candidateKey'] = candidate
        ledger['policyAmendment']['priorLedgerDigest'] = history_digest(ledger['policyAmendment']['priorLedger'])
        config = {'schemaVersion': 1, 'candidateKey': candidate, 'ledger': ledger,
            'history': {'path': 'history', 'sha256': canonical_digest(self.history)}, 'expiresAt': '2099-01-01T00:00:00Z',
            'liveLiabilityReferences': [{'path': 'live', 'sha256': 'sha256:'+'c'*64}]}
        live = {'schema': 'honua.hosted-pilot-retained-liability/v1', 'candidateKey': candidate,
                'operationIds': ['bootstrap'], 'observation': {'status': 'retained-original-liabilities'}}
        ledger['operations'].append({'id': 'legacy-live-cleanup-reservation', 'operationId': 'legacy-live',
            'purpose': 'retention', 'kind': 'cleanup', 'amountUsd': '200',
            'basisDigest': 'sha256:'+'f'*64, 'state': 'reserved', 'actualUsd': None})
        live['operationIds'] = ['legacy-live']
        provenance = {'qualificationLockDigest': 'sha256:'+'d'*64}
        return config, lock, provenance, live, ledger, candidate

    def test_bootstrap_producer_seals_only_the_actual_candidate_and_preserved_live_hold(self):
        config, lock, provenance, live, ledger, candidate = self.producer_case()
        with patch('budget_bootstrap.reviewed', side_effect=[self.history, live]), patch.dict('os.environ',
                {'GITHUB_SHA': 'e'*40, 'GITHUB_RUN_ID': '5', 'GITHUB_RUN_ATTEMPT': '1'}):
            artifact = budget_bootstrap.produce(config, lock, provenance, provenance['qualificationLockDigest'])
        self.assertEqual(artifact['ledger'], ledger)
        self.assertEqual(artifact['source']['runId'], 5)
        self.assertEqual(artifact['candidateKey'], candidate)
        with self.assertRaisesRegex(ValueError, 'authenticated frozen candidate'):
            budget_bootstrap.produce(config, lock, provenance, 'sha256:'+'f'*64)

    def test_bootstrap_refuses_missing_live_reference_or_using_reservation_id_as_operation_group(self):
        for mutation in ('missing-reference', 'missing-operation', 'reservation-id-instead-of-group'):
            config, lock, provenance, live, _, _ = self.producer_case()
            if mutation == 'missing-reference': config['liveLiabilityReferences'] = []
            elif mutation == 'missing-operation': config['ledger']['operations'].pop()
            else: live['operationIds'] = ['legacy-live-cleanup-reservation']
            with self.subTest(mutation=mutation), patch('budget_bootstrap.reviewed', side_effect=[self.history, live]), patch.dict('os.environ',
                    {'GITHUB_SHA': 'e'*40, 'GITHUB_RUN_ID': '5', 'GITHUB_RUN_ATTEMPT': '1'}):
                with self.assertRaisesRegex(ValueError, 'liabilities must be explicitly reviewed|liability cannot disappear'):
                    budget_bootstrap.produce(config, lock, provenance, provenance['qualificationLockDigest'])

    def test_original_nonzero_hold_survives_actual_durable_import_and_second_init_refuses(self):
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder)/'first.json'; receipt = self.initialize(first)
            self.assertEqual(validate(self.store.state['ledger'])['liabilityUsd'], '271')
            self.assertEqual(self.store.state['bootstrapImport'], self.imported)
            self.assertEqual(json.loads(first.read_text()), self.ledger)
            self.assertEqual(receipt['generation'], self.store.generation)
            self.assertFalse(receipt['qualification'])
            with self.assertRaisesRegex(PackError, 'history already exists'):
                self.initialize(Path(folder)/'second.json')
            self.assertEqual(self.store.state['ledger']['operations'][0]['amountUsd'], '151')

    def test_unknown_gross_usage_cannot_be_deleted_zeroed_or_settled(self):
        for mutation in ['remove', 'zero', 'settled', 'wrong-basis']:
            ledger = copy.deepcopy(self.ledger); row = ledger['operations'][0]
            if mutation == 'remove': ledger['operations'].pop(0)
            elif mutation == 'zero': row['amountUsd'] = '0'
            elif mutation == 'settled': row.update(state='settled', actualUsd='0')
            else: row['basisDigest'] = 'sha256:'+'f'*64
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, 'nonzero gross usage'):
                budget_bootstrap.validate_history(self.history, ledger)

    def test_real_producer_history_cannot_initialize_as_no_attempts(self):
        self.history['repositories']['honua-io/honua-release']['producerRuns'] = [{'id': 99}]
        with self.assertRaisesRegex(ValueError, 'requires reconciliation'):
            budget_bootstrap.validate_history(self.history, self.ledger)

    def test_wrong_live_caller_is_refused_before_state_open(self):
        self.sts.get_caller_identity.return_value['Arn'] = 'arn:aws:sts::585192672263:assumed-role/unrelated/session'
        opened = MagicMock()
        with self.assertRaisesRegex(ValueError, 'actual AWS caller'):
            initialize_accounting.initialize(self.config, Path('artifact'), Path('unused'), open_state=opened, sts=self.sts)
        opened.assert_not_called()

    def test_substituted_namespace_is_refused_before_authentication_or_state_open(self):
        self.config['state']['bucket'] += '-fresh'
        authenticated, opened = MagicMock(), MagicMock()
        with self.assertRaisesRegex(ValueError, 'substitute a fresh accounting namespace'):
            initialize_accounting.initialize(self.config, Path('artifact'), Path('unused'), verify=authenticated, open_state=opened, sts=self.sts)
        authenticated.assert_not_called(); opened.assert_not_called()

    def test_history_cannot_outlive_fixed_recent_cutoff_even_with_far_future_config_expiry(self):
        self.history['coverage']['cutoff']=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
        self.history['observedAt']=(datetime.now(timezone.utc)-timedelta(hours=2)+timedelta(minutes=1)).isoformat()
        config,lock,provenance,live,_,_=self.producer_case()
        with patch('budget_bootstrap.reviewed',side_effect=[self.history,live]),patch.dict('os.environ',
                {'GITHUB_SHA':'e'*40,'GITHUB_RUN_ID':'5','GITHUB_RUN_ATTEMPT':'1'}):
            with self.assertRaisesRegex(ValueError,'stale, future or inconsistent'):
                budget_bootstrap.produce(config,lock,provenance,provenance['qualificationLockDigest'])
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError,'stale, future or inconsistent'):self.initialize(Path(folder)/'ledger.json')
        self.assertIsNone(self.store.state)
    def test_future_or_timezone_less_cutoff_and_omitted_cutoff_day_refuse(self):
        original=copy.deepcopy(self.history)
        for change in ('future-observation','timezone-less','cutoff-after-observation','exclusive-end-omits-day'):
            value=copy.deepcopy(original)
            if change=='future-observation':value['observedAt']=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()
            elif change=='timezone-less':value['coverage']['cutoff']='2026-10-23T08:40:00'
            elif change=='cutoff-after-observation':value['coverage']['cutoff']=value['observedAt'];value['observedAt']=original['coverage']['cutoff']
            else:value['billing']['queryPeriod']['End']=datetime.fromisoformat(value['coverage']['cutoff']).date().isoformat()
            with self.subTest(change=change),self.assertRaises(ValueError):budget_bootstrap.validate_history(value,self.ledger)
