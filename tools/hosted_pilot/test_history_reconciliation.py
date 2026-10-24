"""Original native history cannot disappear through a bootstrap classification."""
import base64
import copy
from datetime import datetime,timedelta,timezone
import io
import json
import unittest
from unittest.mock import patch
import zipfile

import budget_bootstrap as bootstrap
import history_reconciliation as reconciliation
from agent_e2e_pack import canonical_digest,digest_bytes
from budget_history import cost_period


class ReconciledHistory(unittest.TestCase):
    def setUp(self):
        self.now=datetime.now(timezone.utc)
        self.p={'repository':'honua-io/honua-release','runId':42,'runAttempt':1,'sourceSha':'b'*40,
            'workflow':'.github/workflows/hosted-pilot-foundation-environment.yml'}
        cutoff=self.now-timedelta(minutes=5)
        self.current={'schema':'honua.hosted-pilot-budget-history/v1','wholeAccountZeroSpend':False,
            'accountId':reconciliation.ACCOUNT,'resourceRegion':reconciliation.REGION,
            'observedAt':(self.now-timedelta(minutes=1)).isoformat(),'coverage':{'start':(cutoff-timedelta(days=1)).isoformat(),'cutoff':cutoff.isoformat()},
            'repositories':{'honua-io/honua-release':{'producerRuns':[{'id':42,'run_attempt':1,'head_sha':self.p['sourceSha'],
                'path':self.p['workflow'],'head_branch':'trunk','created_at':(self.now-timedelta(minutes=30)).isoformat(),
                'event':'workflow_dispatch','status':'completed','conclusion':'success'}]}},
            'cloudTrail':{'RunTask':{'pilotEvents':[]}},'billing':{'grossUsageUsd':'157.0291032026','queryPeriod':cost_period(cutoff-timedelta(days=1),cutoff)}}
        self.prior=copy.deepcopy(self.current);self.prior['repositories']['honua-io/honua-release']['producerRuns']=[]
        self.prior['billing']['grossUsageUsd']='149.4013538965'
        self.values={}
        def reference(name,value):
            self.values[name]=value
            return {'path':name,'sha256':canonical_digest(value)}
        self.prior_ref=reference('prior',self.prior)
        self.record={'schema':'honua.hosted-pilot-producer-reconciliation/v1','kind':'github-environment-only','producer':self.p,
            'fundingBasis':self.prior_ref,'fundingReservationIds':['initialization-history-usage'],
            'ownerLedger':None,'artifact':None,'planArtifact':None,'cloudEvents':[]}
        self.envelope={'schema':'honua.hosted-pilot-reconciled-history/v1','currentHistory':reference('current',self.current),
            'retainedHistories':[self.prior_ref],'producerReconciliations':[reference('record',self.record)]}
        self.ledger={'candidateKey':'sha256:'+'a'*64,'operations':[
            {'id':'initialization-history-usage','operationId':'original-usage','purpose':'telemetry','kind':'infrastructure',
                'amountUsd':'151','basisDigest':canonical_digest(self.prior),'state':'reserved','actualUsd':None},
            {'id':'initialization-history-usage-extension-current','operationId':'original-usage','purpose':'telemetry','kind':'infrastructure',
                'amountUsd':'8','basisDigest':canonical_digest(self.envelope),'state':'reserved','actualUsd':None}]}
        self.run={'id':42,'run_attempt':1,'head_sha':self.p['sourceSha'],'path':self.p['workflow'],'head_branch':'trunk',
            'event':'workflow_dispatch','status':'completed','conclusion':'success','run_started_at':(self.now-timedelta(minutes=30)).isoformat()}
        self.jobs={'total_count':1,'jobs':[{'head_sha':self.p['sourceSha'],'status':'completed','conclusion':'success',
            'completed_at':(self.now-timedelta(minutes=10)).isoformat()}]}
        self.branch_protected=True;self.source_drift=False;self.calls=[]
        self.real_authenticate=reconciliation.authenticate
        self.real_reconcile=reconciliation.reconcile
    def read(self,reference):return self.values[reference['path']]
    def native(self,argv):
        self.calls.append(argv);path=argv[-1]
        if '/contents/' in path:
            relative=path.split('/contents/',1)[1].split('?ref=',1)[0]
            raw=(reconciliation.ROOT/relative).read_bytes()+(b' ' if self.source_drift else b'')
            result={'type':'file','path':relative,'encoding':'base64','content':base64.b64encode(raw).decode()}
        elif '/branches/trunk' in path:result={'protected':self.branch_protected}
        elif '/compare/' in path:result={'merge_base_commit':{'sha':self.p['sourceSha']}}
        elif '/jobs?' in path:result=self.jobs
        else:result=self.run
        return json.dumps(result).encode()
    def validate(self,*,fresh=True):
        with patch('budget_bootstrap.reviewed',side_effect=self.read),patch('history_reconciliation.authenticate',
                side_effect=lambda record,**kwargs:self.real_authenticate(record,call=self.native)),patch(
                'history_reconciliation.reconcile',side_effect=lambda *args,**kwargs:self.real_reconcile(*args,**kwargs,call=self.native)):
            bootstrap.validate_history(self.envelope,self.ledger,now=self.now,require_fresh=fresh)
    def test_original151_plus8_extension_retains_real_environment_producer_and_native_proof(self):
        original=copy.deepcopy(self.ledger);self.validate()
        self.assertEqual(self.ledger,original)
        self.assertEqual(self.current['repositories']['honua-io/honua-release']['producerRuns'][0]['id'],42)
        self.assertEqual(sum('/contents/' in row[-1] for row in self.calls),5)
    def test_missing_duplicate_or_substituted_producer_proof_refuses(self):
        original=copy.deepcopy(self.envelope)
        for proof in ([],self.envelope['producerReconciliations']*2):
            self.envelope=copy.deepcopy(original);self.envelope['producerReconciliations']=proof
            with self.subTest(proof=len(proof)),self.assertRaisesRegex(ValueError,'complete finite reconciliation|duplicated'):
                self.validate()
    def test_unreviewed_native_source_branch_failed_jobs_or_attempt_refuse(self):
        for change in ('source','branch','jobs','attempt'):
            self.source_drift=False;self.branch_protected=True;self.jobs['jobs'][0]['conclusion']='success';self.run['run_attempt']=1
            if change=='source':self.source_drift=True
            elif change=='branch':self.branch_protected=False
            elif change=='jobs':self.jobs['jobs'][0]['conclusion']='failure'
            else:self.run['run_attempt']=2
            with self.subTest(change=change),self.assertRaises(ValueError):self.validate()
    def test_zeroed_settled_or_rebound_original_hold_cannot_fund_history(self):
        original=copy.deepcopy(self.ledger)
        for change in ('zero','settled','basis','missing'):
            self.ledger=copy.deepcopy(original);row=self.ledger['operations'][0]
            if change=='zero':row['amountUsd']='0'
            elif change=='settled':row.update(state='settled',actualUsd='0')
            elif change=='basis':row['basisDigest']='sha256:'+'f'*64
            else:self.ledger['operations'].pop(0)
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'funding was dropped'):self.validate()
    def test_extension_cannot_settle_unknown_as_zero_or_avoid_envelope_binding(self):
        original=copy.deepcopy(self.ledger)
        for change in ('zero','settled','basis','missing','underfunded'):
            self.ledger=copy.deepcopy(original);row=self.ledger['operations'][1]
            if change=='zero':row['amountUsd']='0'
            elif change=='settled':row.update(state='settled',actualUsd='0')
            elif change=='basis':row['basisDigest']='sha256:'+'f'*64
            elif change=='missing':self.ledger['operations'].pop()
            else:row['amountUsd']='7'
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'positive appended allowance|nonzero gross usage'):self.validate()
    def test_new_wrapper_does_not_launder_a_retained_older_model_producer(self):
        previous=copy.deepcopy(self.prior)
        model=dict(self.current['repositories']['honua-io/honua-release']['producerRuns'][0],id=99,path='.github/workflows/agent-e2e-campaign.yml')
        previous['repositories']['honua-io/honua-release']['producerRuns']=[model]
        self.values['older-model']=previous
        self.envelope['retainedHistories'].append({'path':'older-model','sha256':canonical_digest(previous)})
        with self.assertRaisesRegex(ValueError,'complete finite reconciliation'):self.validate()
    def test_model_workflow_or_task_execution_cannot_be_labeled_bootstrap(self):
        self.current['repositories']['honua-io/honua-release']['producerRuns'][0]['path']='.github/workflows/agent-e2e-campaign.yml'
        self.p['workflow']='.github/workflows/agent-e2e-campaign.yml'
        with self.assertRaisesRegex(ValueError,'model workflow'):self.validate()
    def test_stale_collection_refuses_before_original_producer_network_calls(self):
        self.now+=timedelta(hours=2)
        with self.assertRaisesRegex(ValueError,'stale, future'):self.validate()
        self.assertEqual(self.calls,[])
    def test_completed_producer_crossing_cutoff_requires_a_new_collection(self):
        self.jobs['jobs'][0]['completed_at']=(self.now-timedelta(minutes=4)).isoformat()
        original=copy.deepcopy(self.ledger)
        with self.assertRaisesRegex(ValueError,'completed after history cutoff'):self.validate()
        self.assertEqual(self.ledger,original)
        self.jobs['jobs'][0]['completed_at']=self.current['coverage']['cutoff']
        self.validate()
    def test_authenticated_existing_generation_recovery_does_not_requery_expired_original_artifacts(self):
        self.now+=timedelta(hours=2)
        self.validate(fresh=False)
        self.assertEqual(self.calls,[])

    def test_fresh_report_cannot_narrow_original_account_region_or_query_interval(self):
        original=copy.deepcopy(self.current)
        for field in ('accountId','resourceRegion','start','cutoff'):
            self.current.clear();self.current.update(copy.deepcopy(original))
            if field=='accountId':self.current[field]='999999999999'
            elif field=='resourceRegion':self.current[field]='us-west-2'
            elif field=='start':self.current['coverage'][field]=(self.now-timedelta(hours=1)).isoformat()
            else:self.current['coverage'][field]=(self.now-timedelta(minutes=10)).isoformat()
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'nonzero-account native|full original observation'):
                self.validate()
            self.assertEqual(self.calls,[])

    def test_required_github_dispatch_and_binding_steps_remain_funded_and_source_verified(self):
        for kind,path,count in [('github-foundation-dispatch-only','.github/workflows/hosted-pilot-bootstrap.yml',1),
            ('github-binding-only','.github/workflows/hosted-pilot-bind.yml',4)]:
            self.p['workflow']=path;self.run['path']=path;self.record['kind']=kind
            self.current['repositories']['honua-io/honua-release']['producerRuns'][0]['path']=path
            self.calls=[];original=copy.deepcopy(self.ledger);self.validate()
            self.assertEqual(self.ledger,original)
            self.assertEqual(sum('/contents/' in row[-1] for row in self.calls),count)
            self.record['cloudEvents']=[{'eventName':'RunTask'}]
            with self.assertRaisesRegex(ValueError,'cannot claim AWS effects'):self.validate()
            self.record['cloudEvents']=[]


class OriginalArtifact(unittest.TestCase):
    def setUp(self):
        self.producer={'repository':'honua-io/honua-release','runId':42,'sourceSha':'a'*40}
        target=io.BytesIO()
        with zipfile.ZipFile(target,'w') as archive:archive.writestr('bindings.json','{"original":true}')
        self.raw=target.getvalue()
        self.reference={'id':7,'name':'original-native-artifact','sha256':digest_bytes(self.raw)}
        self.metadata={'id':7,'name':self.reference['name'],'expired':False,'digest':digest_bytes(self.raw),
            'workflow_run':{'id':42,'head_sha':'a'*40}}
    def native(self,argv):return self.raw if argv[-1].endswith('/zip') else json.dumps(self.metadata).encode()
    def test_actual_zip_digest_native_run_source_and_single_entry_are_required(self):
        self.assertEqual(reconciliation.native_artifact(self.producer,self.reference,'bindings.json',self.native),{'original':True})
    def test_another_run_expired_artifact_digest_or_entry_refuses(self):
        original=copy.deepcopy(self.metadata)
        for change in ('run','expired','digest','entry'):
            self.metadata=copy.deepcopy(original);entry='bindings.json'
            if change=='run':self.metadata['workflow_run']['id']=99
            elif change=='expired':self.metadata['expired']=True
            elif change=='digest':self.metadata['digest']='sha256:'+'f'*64
            else:entry='other.json'
            with self.subTest(change=change),self.assertRaises(ValueError):
                reconciliation.native_artifact(self.producer,self.reference,entry,self.native)


class PaidOriginalFoundation(unittest.TestCase):
    """A complete native artifact/funding path, not an environment-only shortcut."""
    def setUp(self):
        ReconciledHistory.setUp(self)
        self.p.update(repository='honua-io/honua-iac',workflow='.github/workflows/hosted-pilot-bootstrap.yml')
        self.run.update(path=self.p['workflow'])
        row=self.current['repositories']['honua-io/honua-release']['producerRuns'].pop()
        row['path']=self.p['workflow']
        self.current['repositories']['honua-io/honua-iac']={'producerRuns':[row]}
        self.release_sha='c'*40
        self.funding_path='certification/hosted-pilot/foundation-funding-basis.json'
        funding_bytes=(reconciliation.ROOT/self.funding_path).read_bytes()
        self.values[self.funding_path]=json.loads(funding_bytes)
        basis={'path':self.funding_path,'sha256':digest_bytes(funding_bytes)}
        owner={'schema':'honua.hosted-pilot-budget/v1','source_sha':self.release_sha,
            'config_sha256':reconciliation.hashlib.sha256((reconciliation.ROOT/'certification/hosted-pilot/deployments/foss4g-image-preparation.tfvars.json').read_bytes()).hexdigest(),
            'total_budget_usd':2000,'cleanup_reserve_usd':200,'reserved_usd':294,'deployment_reservation_usd':5}
        self.values['owner']=owner
        self.record.update(kind='foundation',fundingBasis=basis,fundingReservationIds=['foundation-infrastructure','foundation-cleanup'],
            ownerLedger={'path':'owner','sha256':canonical_digest(owner)},
            artifact={'id':7,'name':'hosted-pilot-foundation-bindings-42','sha256':'pending'})
        for name,kind,amount in [('foundation-infrastructure','infrastructure','25'),('foundation-cleanup','cleanup','10')]:
            self.ledger['operations'].append({'id':name,'operationId':'foundation','purpose':'bootstrap','kind':kind,
                'amountUsd':amount,'basisDigest':canonical_digest(self.values[self.funding_path]),'state':'reserved','actualUsd':None})
        config_path='certification/hosted-pilot/bootstrap/deployments/foss4g-images.tfvars.json'
        self.receipt={'schema':'honua.hosted-pilot-bootstrap-bindings/v1','foundation_source_sha':self.p['sourceSha'],
            'foundation_run_id':'42','foundation_run_attempt':'1','release_root_sha':self.release_sha,'config_path':config_path,
            'config_sha256':reconciliation.hashlib.sha256((reconciliation.ROOT/config_path).read_bytes()).hexdigest(),
            'budget_binding':{'sha256':canonical_digest(owner).split(':',1)[1]}}
        import bootstrap as foundation
        identity={key:self.receipt[key] for key in ('foundation_source_sha','release_root_sha','config_path','config_sha256')}
        identity['provider_lock_sha256']=reconciliation.hashlib.sha256((reconciliation.ROOT/'certification/hosted-pilot/bootstrap/.terraform.lock.hcl').read_bytes()).hexdigest()
        self.receipt['execution_identity']=foundation.execution_identity({'Account':reconciliation.ACCOUNT,
            'Arn':'arn:aws:iam::'+reconciliation.ACCOUNT+':user/pipeline','UserId':'AIDAEXAMPLE123'},identity,reconciliation.ACCOUNT,'42','1')
        self.seal()
    read=ReconciledHistory.read
    validate=ReconciledHistory.validate
    def seal(self):
        target=io.BytesIO()
        with zipfile.ZipFile(target,'w') as archive:archive.writestr(getattr(self,'entry','bindings.json'),json.dumps(self.receipt))
        self.artifact=target.getvalue();self.record['artifact']['sha256']=digest_bytes(self.artifact)
    def native(self,argv):
        path=argv[-1]
        if '/actions/runs/42/artifacts?' in path:
            return json.dumps({'total_count':len(self.artifact_list),'artifacts':self.artifact_list}).encode()
        if '/actions/artifacts/7' in path:
            self.calls.append(argv)
            if path.endswith('/zip'):return self.artifact
            return json.dumps({'id':7,'name':self.record['artifact']['name'],'expired':False,'digest':digest_bytes(self.artifact),
                'workflow_run':{'id':42,'head_sha':self.p['sourceSha']}}).encode()
        if '/contents/scripts/check-hosted-pilot-foundation-inputs.py' in path:
            relative='scripts/check-hosted-pilot-foundation-inputs.py'
            raw=(reconciliation.ROOT/'certification/hosted-pilot/bootstrap/foundation-input-check.py').read_bytes()
            return json.dumps({'type':'file','path':relative,'encoding':'base64','content':base64.b64encode(raw).decode()}).encode()
        if '/contents/.github/workflows/hosted-pilot-bootstrap.yml' in path:
            relative='.github/workflows/hosted-pilot-bootstrap.yml'
            raw=(reconciliation.ROOT/'certification/hosted-pilot/bootstrap/foundation-bootstrap-workflow.yml').read_bytes()
            return json.dumps({'type':'file','path':relative,'encoding':'base64','content':base64.b64encode(raw).decode()}).encode()
        return ReconciledHistory.native(self,argv)
    # Do not inherit environment-only expectations as paid-path assertions.
    def test_original151_plus8_extension_retains_real_environment_producer_and_native_proof(self):
        original=copy.deepcopy(self.ledger);self.validate();self.assertEqual(self.ledger,original)
        self.assertGreater(sum('/contents/' in row[-1] for row in self.calls),5)
    def test_unreviewed_native_source_branch_failed_jobs_or_attempt_refuse(self):
        ReconciledHistory.test_unreviewed_native_source_branch_failed_jobs_or_attempt_refuse(self)
    def test_zeroed_settled_or_rebound_original_hold_cannot_fund_history(self):
        original=copy.deepcopy(self.ledger)
        for field,value in [('amountUsd','0'),('basisDigest','sha256:'+'f'*64),('actualUsd','0')]:
            self.ledger=copy.deepcopy(original);row=self.ledger['operations'][-1];row[field]=value
            if field=='actualUsd':row['state']='settled'
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'funding was dropped'):self.validate()
    def test_model_workflow_or_task_execution_cannot_be_labeled_bootstrap(self):
        self.p['workflow']='.github/workflows/agent-e2e-campaign.yml'
        with self.assertRaisesRegex(ValueError,'model workflow'):self.validate()
    def test_owner_version_source_budget_or_live_original_allowance_cannot_be_replaced(self):
        original=copy.deepcopy(self.values['owner'])
        for field,value in [('source_sha','f'*40),('total_budget_usd',500),('cleanup_reserve_usd',50),
            ('reserved_usd',0),('deployment_reservation_usd',26)]:
            self.values['owner']=copy.deepcopy(original);self.values['owner'][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'owner version/funding'):self.validate()
        self.values['owner']=original
        self.receipt['budget_binding']['sha256']='f'*64;self.seal()
        with self.assertRaisesRegex(ValueError,'owner version/funding'):self.validate()

    def test_original_successful_plan_cannot_be_relabeled_as_deployment_or_hide_bindings(self):
        self.entry='review.json';self.record['kind']='foundation-plan';self.record['ownerLedger']=None
        self.record['artifact']['name']='hosted-pilot-foundation-review-42'
        self.receipt.update(schema='honua.hosted-pilot-bootstrap-plan/v1',phase='custody-only')
        self.artifact_list=[{'name':'hosted-pilot-foundation-review-42'}];self.seal();self.validate()
        self.artifact_list.append({'name':'hosted-pilot-foundation-bindings-42'})
        with self.assertRaisesRegex(ValueError,'conflicts with actual deployment artifact'):self.validate()
    def test_same_run_artifact_from_a_previous_attempt_cannot_satisfy_original_identity(self):
        self.receipt['execution_identity']['run_attempt']='2';self.seal()
        with self.assertRaisesRegex(ValueError,'execution identity/run/source differs'):self.validate()


class OriginalAwsActor(unittest.TestCase):
    def setUp(self):
        from budget_history import ACCOUNT,event_history
        self.account=ACCOUNT;self.record={'kind':'campaign-staging','producer':{'runId':42}}
        role='honua-release-pilot-foss4g-foundation-apply';principal='AROAEXAMPLE123';session='pilot-apply-42'
        self.receipt={}
        self.native={'eventID':'one','recipientAccountId':ACCOUNT,'eventName':'CreateRole','awsRegion':'us-east-1','eventTime':'2026-10-23T15:00:00Z',
            'requestParameters':{'roleName':'honua-pilot-foss4g-agent-worker'},
            'userIdentity':{'type':'AssumedRole','accountId':ACCOUNT,'principalId':principal+':'+session,
                'arn':'arn:aws:sts::'+ACCOUNT+':assumed-role/'+role+'/'+session,
                'sessionContext':{'sessionIssuer':{'type':'Role','accountId':ACCOUNT,'principalId':principal,
                    'arn':'arn:aws:iam::'+ACCOUNT+':role/'+role}}}}
        self.run={'run_started_at':'2026-10-23T14:59:00Z'}
        self.jobs={'jobs':[{'completed_at':'2026-10-23T15:01:00Z'}]}
    def raw(self):return {'Events':[{'EventId':'one','EventName':'CreateRole','CloudTrailEvent':json.dumps(self.native)}]}
    def verify(self,event=None):
        from budget_history import event_history
        event=event or event_history(self.raw(),'CreateRole')['pilotEvents'][0]
        reconciliation.authenticate_event(event,self.record,self.run,self.jobs,self.receipt,'sha256:'+'a'*64)
    def test_original_native_projection_and_reviewed_run_bound_actor_are_required(self):self.verify()
    def test_same_namespace_and_interval_cannot_admit_other_actor_role_or_run(self):
        original=copy.deepcopy(self.native)
        for change in ('actor','issuer','session','account'):
            self.native=copy.deepcopy(original)
            if change=='actor':self.native['userIdentity']['arn']=self.native['userIdentity']['arn'].replace('foundation-apply','foundation-other')
            elif change=='issuer':self.native['userIdentity']['sessionContext']['sessionIssuer']['arn']+='-other'
            elif change=='session':self.native['userIdentity']['principalId']='AROAEXAMPLE123:pilot-apply-99'
            else:self.native['userIdentity']['accountId']='999999999999'
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'actor/session'):self.verify()
    def test_projected_account_identity_and_out_of_window_event_refuse(self):
        from budget_history import event_history
        for field,value in [('recipientAccountId','999999999999'),('nativeEventIdMatches',False),('eventTime','2026-10-23T15:30:00Z')]:
            event=event_history(self.raw(),'CreateRole')['pilotEvents'][0];event[field]=value
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'identity/account|execution interval'):self.verify(event)


class OriginalFoundationActor(OriginalAwsActor):
    def setUp(self):
        super().setUp()
        import bootstrap
        self.record={'kind':'foundation','producer':{'runId':42,'runAttempt':1,'sourceSha':'b'*40}}
        identity={'foundation_source_sha':'b'*40,'release_root_sha':'c'*40,
            'config_path':'certification/hosted-pilot/bootstrap/deployments/foss4g-images.tfvars.json',
            'config_sha256':'d'*64,'provider_lock_sha256':'e'*64}
        caller={'Account':self.account,'Arn':'arn:aws:iam::'+self.account+':user/pipeline','UserId':'AIDAEXAMPLE123'}
        name='honua-release-pilot-foss4g-foundation-apply'
        self.tags=bootstrap.creation_tags(identity,'42');self.actual_tags=copy.deepcopy(self.tags)
        self.receipt={'release_root_sha':'c'*40,'execution_identity':bootstrap.execution_identity(caller,identity,self.account,'42','1'),
            'creation_resources':bootstrap.planned_creation_resources({'resource_changes':[{'address':'aws_iam_role.executor["apply"]',
                'type':'aws_iam_role','change':{'actions':['create'],'after':{'name':name,'tags_all':self.tags}}}]})}
        self.native['requestParameters']['roleName']=name
        self.native['userIdentity']={'type':'IAMUser','accountId':self.account,'arn':caller['Arn'],'principalId':caller['UserId']}
    def verify(self,event=None):
        from budget_history import event_history
        event=event or event_history(self.raw(),'CreateRole')['pilotEvents'][0]
        tags={'Tags':[{'Key':key,'Value':value} for key,value in self.actual_tags.items()]}
        event['resourceTagObservation']={'resourceName':self.native['requestParameters']['roleName'],
            **__import__('budget_history').creation_tags_projection(tags),'evidence':{
                'command':['aws','iam','list-role-tags','--role-name',self.native['requestParameters']['roleName'],'--output','json'],
                'projection':'identity-and-aggregate-usage-only','responseSha256':canonical_digest(tags)}}
        reconciliation.authenticate_event(event,self.record,self.run,self.jobs,self.receipt,'sha256:'+'a'*64)
    def test_same_namespace_and_interval_cannot_admit_other_actor_role_or_run(self):
        for field,value in [('arn','arn:aws:iam::'+self.account+':user/other'),('principalId','AIDAOTHER'),('accountId','999999999999')]:
            original=copy.deepcopy(self.native);self.native['userIdentity'][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'actual STS principal proof'):self.verify()
            self.native=original
    def test_projected_account_identity_and_out_of_window_event_refuse(self):
        OriginalAwsActor.test_projected_account_identity_and_out_of_window_event_refuse(self)
    def test_actual_tags_original_plan_name_run_source_and_account_must_all_match(self):
        original=copy.deepcopy(self.receipt)
        self.actual_tags['BootstrapExecution']='99'
        with self.assertRaisesRegex(ValueError,'actual resource creation tags differ'):self.verify()
        self.actual_tags=copy.deepcopy(self.tags)
        self.receipt['creation_resources'][0]['name']+='-other'
        with self.assertRaisesRegex(ValueError,'exact original saved-plan'):self.verify()
        self.receipt=copy.deepcopy(original);self.receipt['creation_resources'][0]['creation_tags']['BootstrapExecution']='99'
        with self.assertRaisesRegex(ValueError,'original source/run'):self.verify()
        self.receipt=copy.deepcopy(original);self.receipt.pop('execution_identity')
        with self.assertRaisesRegex(ValueError,'actual STS principal proof'):self.verify()
