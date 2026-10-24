"""History reconstruction refuses gaps and preserves nonzero uncertain costs."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from budget_history import ACCOUNT, cache_projection, collect, cost_period, costs, dispatch_history, event_history, producer_path


class BudgetHistory(unittest.TestCase):
    def test_billing_exclusive_end_covers_partial_utc_cutoff_day_and_exact_midnight(self):
        begin=datetime.fromisoformat('2026-10-01T00:00:00+00:00')
        for cutoff,end in [('2026-10-23T08:40:00+00:00','2026-10-24'),('2026-10-23T00:00:00+00:00','2026-10-23'),
                           ('2026-10-22T22:40:00-10:00','2026-10-24')]:
            with self.subTest(cutoff=cutoff):self.assertEqual(cost_period(begin,datetime.fromisoformat(cutoff)),{'Start':'2026-10-01','End':end})
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


class HistoryPrivacy(unittest.TestCase):
    SENTINEL = 'credential-sentinel-that-must-never-be-persisted'
    def native(self, event='RunTask', *, mention_only=False):
        request = {'cluster': 'ordinary-cluster' if mention_only else 'honua-pilot-unit-agent',
            'overrides': {'containerOverrides': [{'environment': [{'name': 'ADMIN_KEY', 'value': self.SENTINEL}]}]},
            'input': json.dumps({'private': self.SENTINEL, 'note': 'honua-pilot-unit-agent'})}
        value = {'eventName': event, 'awsRegion': 'us-east-1', 'eventTime': '2026-10-23T00:00:00Z',
                 'requestParameters': request, 'responseElements': {'private': self.SENTINEL},
                 'userIdentity': {'accessKeyId': self.SENTINEL}, 'errorMessage': self.SENTINEL}
        return {'Events': [{'EventId': 'original-event-id', 'EventName': event, 'CloudTrailEvent': json.dumps(value)}]}
    def usage(self):
        return {'ResultsByTime': [{'TimePeriod': {'Start': '2026-10-22', 'End': '2026-10-23'}, 'Estimated': True,
            'Groups': [{'Keys': ['Amazon ECS', kind], 'Metrics': {'UnblendedCost': {'Amount': amount, 'Unit': 'USD'}}}
                       for kind, amount in [('Usage', '149.4013538965'), ('Credit', '-149.4013538965')]]}],
                'unreviewedPrivateField': self.SENTINEL}
    def test_cloudtrail_keeps_scope_and_identity_without_request_or_credential_payloads(self):
        value = self.native()
        projected = cache_projection('cloudtrail-RunTask', value)
        self.assertEqual(projected['totalEvents'], 1)
        self.assertEqual(projected['pilotEvents'][0]['resourceIdentities'], ['honua-pilot-unit-agent'])
        self.assertNotIn(self.SENTINEL, json.dumps(projected))
        self.assertNotIn('bindings', projected['pilotEvents'][0])
        self.assertEqual(projected['allEventIdsDigest'], event_history(value, 'RunTask')['allEventIdsDigest'])
    def test_unprojected_pilot_mention_is_not_reinterpreted_as_zero_history(self):
        value = cache_projection('cloudtrail-RunTask', self.native(mention_only=True))
        self.assertEqual(len(value['pilotEvents']), 1)
        self.assertTrue(value['pilotEvents'][0]['unprojectedNamespaceReference'])
        self.assertNotIn(self.SENTINEL, json.dumps(value))
    def test_policy_caller_and_financial_extras_never_enter_cached_projections(self):
        for label, value in [('roles', {'Roles': [{'RoleName': 'honua-pilot-unit-agent', 'Arn': 'arn:aws:iam::'+ACCOUNT+':role/honua-pilot-unit-agent',
                'AssumeRolePolicyDocument': {'private': self.SENTINEL}}]}),
                ('caller', {'Account': ACCOUNT, 'Arn': 'arn:aws:iam::'+ACCOUNT+':user/operator', 'UserId': self.SENTINEL}),
                ('account-costs', self.usage())]:
            with self.subTest(label=label):
                self.assertNotIn(self.SENTINEL, json.dumps(cache_projection(label, value)))
        self.assertEqual(cache_projection('account-costs', self.usage())['grossUsageUsd'], '149.4013538965')
        with self.assertRaisesRegex(ValueError, 'no reviewed identity projection'):
            cache_projection('unreviewed-api', {'secret': self.SENTINEL})
    def test_complete_collection_writes_only_safe_projections_and_retains_raw_response_hashes(self):
        def call(argv):
            if argv[0] == 'gh':
                if '/branches/' in argv[-1]: return {'name': 'trunk', 'protected': True, 'commit': {'sha': 'a'*40, 'private': self.SENTINEL}}
                if '/contents/' in argv[-1]: return []
                return [{'total_count': 0, 'workflow_runs': [], 'private': self.SENTINEL}]
            command = tuple(argv[1:3])
            if command == ('sts', 'get-caller-identity'): return {'Account': ACCOUNT, 'Arn': 'arn:aws:iam::'+ACCOUNT+':user/operator', 'UserId': self.SENTINEL}
            if command == ('cloudtrail', 'lookup-events'):
                event = argv[argv.index('--lookup-attributes')+1].split('AttributeValue=')[1]
                return self.native() if event == 'RunTask' else {'Events': [], 'private': self.SENTINEL}
            if command == ('iam', 'list-roles'): return {'Roles': [], 'private': self.SENTINEL}
            if command == ('s3api', 'list-buckets'): return {'Buckets': [], 'private': self.SENTINEL}
            if command == ('dynamodb', 'list-tables'): return {'TableNames': [], 'private': self.SENTINEL}
            if command == ('ce', 'get-cost-and-usage'): return self.usage()
            raise AssertionError(argv)
        cutoff = datetime.now(timezone.utc)-timedelta(minutes=1)
        start = cutoff-timedelta(days=1)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'history'
            report = collect(start.isoformat(), cutoff.isoformat(), path, call=call)
            self.assertEqual(report['billing']['grossUsageUsd'], '149.4013538965')
            self.assertEqual(report['billing']['queryPeriod'],cost_period(start,cutoff))
            command=report['evidence']['account-costs']['command']
            self.assertEqual(command[command.index('--time-period')+1],
                'Start='+report['billing']['queryPeriod']['Start']+',End='+report['billing']['queryPeriod']['End'])
            self.assertFalse(report['wholeAccountZeroSpend'])
            self.assertEqual(report['cloudTrail']['RunTask']['totalEvents'], 1)
            self.assertEqual(len(report['cloudTrail']['RunTask']['pilotEvents']), 1)
            for file in path.iterdir():
                self.assertNotIn(self.SENTINEL, file.read_text(), file.name)
            for reference in report['evidence'].values():
                self.assertTrue(reference['responseSha256'].startswith('sha256:'))
                self.assertEqual(reference['projection'], 'identity-and-aggregate-usage-only')


class CreationIdentityCollection(HistoryPrivacy):
    def original(self):
        import bootstrap
        identity={'foundation_source_sha':'a'*40,'release_root_sha':'b'*40,
            'config_path':'certification/hosted-pilot/bootstrap/deployments/foss4g-images.tfvars.json',
            'config_sha256':'c'*64,'provider_lock_sha256':'d'*64}
        tags=bootstrap.creation_tags(identity,'42')
        name='honua-release-pilot-foss4g-foundation-apply'
        native={'eventID':'creation-event','eventName':'CreateRole','awsRegion':'us-east-1','recipientAccountId':ACCOUNT,
            'eventTime':'2026-10-23T15:00:00Z','requestParameters':{'roleName':name,'policy':self.SENTINEL},
            'userIdentity':{'type':'IAMUser','accountId':ACCOUNT,'arn':'arn:aws:iam::'+ACCOUNT+':user/pipeline',
                'principalId':'AIDAEXAMPLE123','accessKeyId':self.SENTINEL,'private':self.SENTINEL}}
        return tags,{'Events':[{'EventId':'creation-event','EventName':'CreateRole','CloudTrailEvent':json.dumps(native)}]}
    def test_real_collector_shape_retains_native_identity_and_tags_without_raw_credentials(self):
        tags,original=self.original();calls=[]
        def call(argv):
            calls.append(argv)
            if argv[0]=='gh':
                if '/branches/' in argv[-1]:return {'name':'trunk','protected':True,'commit':{'sha':'a'*40}}
                if '/contents/' in argv[-1]:return []
                return [{'total_count':0,'workflow_runs':[]}]
            command=tuple(argv[1:3])
            if command==('sts','get-caller-identity'):return {'Account':ACCOUNT,'Arn':'arn:aws:iam::'+ACCOUNT+':user/operator'}
            if command==('cloudtrail','lookup-events'):
                return original if 'AttributeKey=EventName,AttributeValue=CreateRole' in argv else {'Events':[]}
            if command==('iam','list-role-tags'):return {'Tags':[{'Key':key,'Value':value} for key,value in tags.items()]+
                [{'Key':'private-secret','Value':self.SENTINEL}], 'private':self.SENTINEL}
            if command==('iam','list-roles'):return {'Roles':[]}
            if command==('s3api','list-buckets'):return {'Buckets':[]}
            if command==('dynamodb','list-tables'):return {'TableNames':[]}
            if command==('ce','get-cost-and-usage'):return self.usage()
            raise AssertionError(argv)
        cutoff=datetime.now(timezone.utc)-timedelta(minutes=1)
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'history'
            report=collect((cutoff-timedelta(days=1)).isoformat(),cutoff.isoformat(),path,call=call)
            event=report['cloudTrail']['CreateRole']['pilotEvents'][0]
            self.assertEqual(event['principal'],{'type':'IAMUser','accountId':ACCOUNT,
                'arn':'arn:aws:iam::'+ACCOUNT+':user/pipeline','principalId':'AIDAEXAMPLE123'})
            self.assertTrue(event['nativeEventIdMatches'])
            observation=event['resourceTagObservation']
            self.assertTrue(observation['complete']);self.assertEqual(observation['tags'],tags)
            self.assertEqual(sum(tuple(row[1:3])==('iam','list-role-tags') for row in calls),1)
            for file in path.iterdir():self.assertNotIn(self.SENTINEL,file.read_text())
    def test_unknown_identity_never_becomes_an_empty_history_or_caller_human_label(self):
        from budget_history import event_history
        _,original=self.original();native=json.loads(original['Events'][0]['CloudTrailEvent'])
        native['userIdentity']={'type':'caller-says-bootstrap','arn':self.SENTINEL,'accessKeyId':self.SENTINEL}
        original['Events'][0]['CloudTrailEvent']=json.dumps(native)
        projected=event_history(original,'CreateRole')
        self.assertEqual(projected['totalEvents'],1);self.assertEqual(len(projected['pilotEvents']),1)
        self.assertIsNone(projected['pilotEvents'][0]['principal'])
        self.assertNotIn(self.SENTINEL,json.dumps(projected))
    def test_partial_or_malformed_native_creation_tags_remain_unqualified(self):
        from budget_history import creation_tags_projection
        tags,_=self.original();tags['BootstrapConfigSha256']=self.SENTINEL
        projected=creation_tags_projection({'Tags':[{'Key':key,'Value':value} for key,value in tags.items()]})
        self.assertFalse(projected['complete']);self.assertNotIn(self.SENTINEL,json.dumps(projected))
        with self.assertRaisesRegex(ValueError,'truncated'):
            creation_tags_projection({'Tags':[],'IsTruncated':True})
