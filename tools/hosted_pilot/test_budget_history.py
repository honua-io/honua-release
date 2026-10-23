"""History reconstruction refuses gaps and preserves nonzero uncertain costs."""
import json
import unittest
from budget_history import costs, dispatch_history, event_history, producer_path


class BudgetHistory(unittest.TestCase):
    def run_row(self, id=1, path='.github/workflows/terminal-model-canary.yml'):
        return dict(id=id, path=path, head_sha='a'*40, head_branch='trunk', created_at='2026-10-22T00:00:00Z',
                    event='workflow_dispatch', status='completed', conclusion='success', run_attempt=1)

    def test_observed_reader_canary_and_contract_are_not_paid_campaign_attempts(self):
        rows = [self.run_row(), self.run_row(2, '.github/workflows/gate-agent-e2e.yml')]
        self.assertEqual(dispatch_history([{'total_count': 2, 'workflow_runs': rows}])['producerRuns'], [])
        self.assertTrue(producer_path('.github/workflows/hosted-pilot-contract.yml'))
        self.assertTrue(producer_path('.github/workflows/agent-e2e-campaign.yml'))

    def test_real_producer_dispatch_cannot_be_erased_by_failed_conclusion(self):
        row = self.run_row(path='.github/workflows/hosted-pilot-deploy.yml'); row['conclusion'] = 'failure'
        self.assertEqual(dispatch_history([{'total_count': 1, 'workflow_runs': [row]}])['producerRuns'], [row])

    def test_dispatch_pagination_cap_missing_page_duplicate_and_changing_total_refused(self):
        for pages in [[{'total_count': 2, 'workflow_runs': [self.run_row()]}],
                      [{'total_count': 1001, 'workflow_runs': []}],
                      [{'total_count': 2, 'workflow_runs': [self.run_row(), self.run_row()]}],
                      [{'total_count': 1, 'workflow_runs': []}, {'total_count': 2, 'workflow_runs': [self.run_row()]}]]:
            with self.subTest(pages=pages), self.assertRaisesRegex(ValueError, 'incomplete, changed or capped'):
                dispatch_history(pages)

    def test_cloudtrail_failed_creation_still_requires_reconciliation(self):
        native = dict(eventName='RunTask', awsRegion='us-east-1', eventTime='2026-10-22T00:00:00Z',
                      requestParameters={'cluster': 'honua-pilot-unit-agent'}, errorCode='Denied')
        event = dict(EventId='one', EventName='RunTask', CloudTrailEvent=json.dumps(native))
        result = event_history({'Events': [event]}, 'RunTask')
        self.assertEqual(result['pilotEvents'][0]['errorCode'], 'Denied')
        self.assertNotIn('userIdentity', result['pilotEvents'][0])
        with self.assertRaisesRegex(ValueError, 'pagination incomplete'):
            event_history({'Events': [], 'NextToken': 'more'}, 'RunTask')

    def test_gross_usage_survives_credits_and_estimated_status(self):
        result = costs({'ResultsByTime': [{'TimePeriod': {'Start': '2026-10-01', 'End': '2026-10-02'}, 'Estimated': True,
            'Groups': [{'Keys': ['Amazon ECS', kind], 'Metrics': {'UnblendedCost': {'Amount': amount, 'Unit': 'USD'}}}
                       for kind, amount in [('Usage', '149.4013538965'), ('Credit', '-149.4013538965')]]}]})
        self.assertEqual(result['grossUsageUsd'], '149.4013538965')
        self.assertEqual(result['recordTypeUsd']['Credit'], '-149.4013538965')
        self.assertIsNone(result['settledPilotActualUsd'])
        self.assertFalse(result['creditsReduceConservativeHold'])
        self.assertTrue(result['periods'][0]['estimated'])

    def test_empty_or_incomplete_billing_cannot_prove_zero(self):
        for value in [{'ResultsByTime': []}, {'ResultsByTime': [{}], 'NextPageToken': 'more'}]:
            with self.assertRaisesRegex(ValueError, 'incomplete'):
                costs(value)
