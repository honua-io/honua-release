"""Replay projected native originals and byte-exact source/artifact fixtures."""
import base64
import copy
import gzip
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import history_expired_phase as expired
import history_reconciliation as history
from agent_e2e_pack import canonical_digest


class NativeOriginals:
    def __init__(self):
        self.data = json.loads((Path(__file__).parent / 'fixtures/expired-custody-40039656211.json').read_text())
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[:2] != ['gh', 'api']:
            raise AssertionError('only native metadata GETs are permitted')
        path = argv[2]
        if '/contents/' in path:
            repository, tail = path.split('/contents/', 1)
            repository = repository.removeprefix('repos/')
            filename, source = tail.rsplit('?ref=', 1)
            data = gzip.decompress(base64.b64decode(self.data['sourceBytes'][repository + '@' + source + ':' + filename]))
            result = {'type': 'file', 'path': filename, 'encoding': 'base64', 'content': base64.b64encode(data).decode()}
        elif path.endswith('/branches/trunk'):
            result = {'protected': True}
        elif '/compare/' in path:
            result = {'merge_base_commit': {'sha': path.split('/compare/')[1].split('...')[0]}}
        elif '/actions/artifacts/' in path:
            token = path.split('/actions/artifacts/')[1]
            identifier = token.split('/')[0]
            plan = self.data['observedPlan']
            if int(identifier) == expired.PLAN_ARTIFACT['id']:
                metadata, raw = plan['metadata'], plan['archiveBytes']
            else:
                metadata, raw = self.data['metadata'][identifier], self.data['archiveBytes'][identifier]
            if token.endswith('/zip'):
                return base64.b64decode(raw)
            result = metadata
        else:
            plan = '/actions/runs/40038616357/' in path
            data = self.data['observedPlan'] if plan else self.data
            if '/attempts/1/jobs?' in path:
                result = data['jobs']
            elif path.endswith('/attempts/1'):
                result = data['run']
            elif '/artifacts?' in path:
                result = {'total_count': 1, 'artifacts': [data['metadata']]} if plan else data['artifacts']
            elif path.endswith('/approvals'):
                result = self.data['approvals']
            else:
                raise AssertionError('unexpected native observation: ' + path)
        return json.dumps(result).encode()


def record():
    return {'schema': 'honua.hosted-pilot-producer-reconciliation/v1', 'kind': expired.KIND,
            'producer': copy.deepcopy(expired.PRODUCER), 'fundingBasis': copy.deepcopy(expired.FUNDING_BASIS),
            'fundingReservationIds': [], 'ownerLedger': None, 'artifact': copy.deepcopy(expired.RECOVERY),
            'planArtifact': copy.deepcopy(expired.REVIEW), 'cloudEvents': []}


class ExpiredOriginalHistory(unittest.TestCase):
    def setUp(self):
        self.native = NativeOriginals()
        self.record = record()

    def observe(self):
        return history.authenticate(self.record, call=self.native)

    def test_actual_cancelled_source_jobs_immutable_originals_authenticate_without_settlement(self):
        before = copy.deepcopy(self.record)
        run, jobs, receipt, source = self.observe()
        self.assertEqual(run['conclusion'], 'cancelled')
        self.assertEqual(receipt['resources'], [])
        self.assertTrue(receipt['cloud_discovery_complete'])
        self.assertEqual(source, expired.RELEASE_SHA)
        self.assertEqual(self.record, before)
        self.assertTrue(all(call[:2] == ['gh', 'api'] and '--method' not in call for call in self.native.calls))

    def test_successful_observed_plan_keeps_its_old_sources_after_policy_evolution(self):
        self.record.update(kind='foundation-plan', producer=copy.deepcopy(expired.PLAN_PRODUCER),
                           artifact=copy.deepcopy(expired.PLAN_ARTIFACT), planArtifact=None)
        run, jobs, receipt, source = self.observe()
        self.assertEqual(run['conclusion'], 'success')
        self.assertEqual(source, expired.RELEASE_SHA)
        self.assertEqual(receipt['phase'], 'custody-only')

    def test_unknown_run_attempt_source_model_workflow_or_kind_cannot_use_original_exception(self):
        for field, value in [('runId', 40039656212), ('runAttempt', 2), ('sourceSha', 'f' * 40),
                             ('workflow', '.github/workflows/terminal-model-canary.yml'),
                             ('repository', 'honua-io/foreign')]:
            with self.subTest(field=field):
                original = self.record['producer'][field]
                self.record['producer'][field] = value
                with self.assertRaises(ValueError): self.observe()
                self.record['producer'][field] = original

    def test_original_cancelled_outcome_cannot_be_relabelled_success_or_failure(self):
        for conclusion in ['success', 'failure', 'in_progress']:
            self.native.data['run']['conclusion'] = conclusion
            with self.assertRaises(ValueError): self.observe()

    def test_cloud_effect_or_published_owner_cannot_be_inferred_from_empty_discovery(self):
        for field, value in [('cloudEvents', [{'eventId': 'invented'}]), ('ownerLedger', {'path': 'invented'}),
                             ('fundingBasis', {'path': 'other', 'sha256': 'sha256:' + '0' * 64})]:
            with self.subTest(field=field):
                original = self.record[field]; self.record[field] = value
                with self.assertRaises(ValueError): self.observe()
                self.record[field] = original

    def test_real_exact_apply_must_be_positively_skipped(self):
        row = next(j for j in self.native.data['jobs']['jobs'] if j['name'] == 'bootstrap')
        apply = next(s for s in row['steps'] if s['name'] == 'Apply only the exact reviewed phase and stop')
        for result in ['success', 'failure', 'cancelled', None]:
            apply['conclusion'] = result
            with self.assertRaises(ValueError): self.observe()
        row['steps'].remove(apply)
        with self.assertRaises(ValueError): self.observe()

    def test_even_nonaccepted_human_approval_history_is_different_from_original_absence(self):
        for rows in [None, [{'state': 'approved'}], [{'state': 'rejected'}]]:
            self.native.data['approvals'] = rows
            with self.assertRaises(ValueError): self.observe()

    def test_failed_wait_before_original_deadline_is_not_this_expired_original(self):
        row = next(j for j in self.native.data['jobs']['jobs'] if j['name'] == 'bootstrap')
        wait = next(s for s in row['steps'] if s['name'].startswith('Wait for original'))
        wait['completed_at'] = '2026-10-23T23:50:00Z'
        with self.assertRaises(ValueError): self.observe()

    def test_partial_truncated_or_changed_job_identity_refuses(self):
        original = copy.deepcopy(self.native.data['jobs'])
        for field, value in [('id', 1), ('head_sha', 'f' * 40), ('status', 'in_progress')]:
            with self.subTest(field=field):
                self.native.data['jobs'] = copy.deepcopy(original)
                self.native.data['jobs']['jobs'][0][field] = value
                with self.assertRaises(ValueError): self.observe()
        self.native.data['jobs'] = copy.deepcopy(original)
        self.native.data['jobs']['total_count'] += 1
        with self.assertRaises(ValueError): self.observe()

    def test_actual_source_body_tampering_refuses_with_real_digest_verifier(self):
        key = next(key for key in self.native.data['sourceBytes'] if key.endswith('bootstrap_phase.py'))
        self.native.data['sourceBytes'][key] = base64.b64encode(gzip.compress(b'forged source')).decode()
        with self.assertRaisesRegex(ValueError, 'executable source changed'): self.observe()

    def test_archive_bytes_source_and_expiry_are_independently_checked(self):
        original = copy.deepcopy(self.native.data)
        for mutation in ['bytes', 'source', 'expiry']:
            with self.subTest(mutation=mutation):
                self.native.data = copy.deepcopy(original)
                identifier = str(expired.REVIEW['id'])
                if mutation == 'bytes': self.native.data['archiveBytes'][identifier] = base64.b64encode(b'forged zip').decode()
                elif mutation == 'source': self.native.data['metadata'][identifier]['workflow_run']['head_sha'] = 'f' * 40
                else: self.native.data['metadata'][identifier]['expired'] = True
                with self.assertRaises(ValueError): self.observe()

    def test_changed_artifact_reference_and_added_apply_receipt_refuse(self):
        original = copy.deepcopy(self.record)
        self.record['planArtifact']['sha256'] = 'sha256:' + 'f' * 64
        with self.assertRaises(ValueError): self.observe()
        self.record = original
        self.native.data['artifacts']['total_count'] += 1
        self.native.data['artifacts']['artifacts'].append({'id': 1, 'name': 'hosted-pilot-phase-admitted-forged'})
        with self.assertRaises(ValueError): self.observe()

    def financial_fixture(self):
        key = 'honua-io/honua-release@' + expired.RELEASE_SHA + ':' + expired.FUNDING_BASIS['path']
        basis = json.loads(gzip.decompress(base64.b64decode(self.native.data['sourceBytes'][key])))
        self.record['fundingReservationIds'] = ['fixture-original-infra', 'fixture-original-cleanup']
        rows = [{'id': name, 'kind': kind, 'purpose': 'bootstrap', 'amountUsd': amount,
                 'basisDigest': canonical_digest(basis), 'state': 'reserved', 'actualUsd': None}
                for name, kind, amount in [('fixture-original-infra', 'infrastructure', '25'),
                                           ('fixture-original-cleanup', 'cleanup', '10')]]
        rows.append({'id': 'fixture-remaining-original-liabilities', 'kind': 'infrastructure', 'purpose': 'telemetry',
                     'amountUsd': '299', 'basisDigest': canonical_digest(basis), 'state': 'reserved', 'actualUsd': None})
        ledger = {'operations': rows}
        report = {'repositories': {'honua-io/honua-iac': {'producerRuns': [self.native.data['run']]}}, 'cloudTrail': {}}
        def read(reference): return self.record if reference == {'path': 'fixture-original'} else basis
        return ledger, report, read

    def reconcile(self, cutoff='2026-10-23T23:54:00Z'):
        ledger, report, read = self.financial_fixture()
        original = copy.deepcopy(ledger)
        history.reconcile(report, ledger, [{'path': 'fixture-original'}], read=read, cutoff=cutoff, call=self.native)
        self.assertEqual(ledger, original)

    def test_reconciliation_retains_all334_original_amounts_and_both_original_foundation_rows(self):
        self.reconcile()

    def test_original_completed_after_cutoff_requires_fresh_collection(self):
        with self.assertRaisesRegex(ValueError, 'after history cutoff'): self.reconcile('2026-10-23T23:53:00Z')

    def test_missing_cleanup_zeroed_foundation_or_discarded_other_liabilities_refuse(self):
        for mutation in ['missing-cleanup', 'zero-infra', 'zero-other']:
            with self.subTest(mutation=mutation):
                ledger, report, read = self.financial_fixture()
                if mutation == 'missing-cleanup': ledger['operations'].pop(1)
                elif mutation == 'zero-infra': ledger['operations'][0]['amountUsd'] = '0'
                else: ledger['operations'][2]['amountUsd'] = '0'
                with self.assertRaises(ValueError):
                    history.reconcile(report, ledger, [{'path': 'fixture-original'}], read=read,
                                      cutoff='2026-10-23T23:54:00Z', call=self.native)


if __name__ == '__main__':
    unittest.main()
