import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import bootstrap
import bootstrap_phase as phase

NOW = datetime(2026, 10, 23, 20, 0, tzinfo=timezone.utc)
ENV = {'GITHUB_REPOSITORY': phase.REPOSITORY, 'GITHUB_REF': 'refs/heads/trunk',
       'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2', 'GITHUB_SHA': 'a'*40,
       'PILOT_RELEASE_ROOT_SHA': 'b'*40, 'GITHUB_WORKFLOW_REF': 'honua-io/honua-iac/.github/workflows/hosted-pilot-bootstrap.yml@refs/heads/trunk', 'GITHUB_WORKFLOW_SHA': 'a'*40, 'GITHUB_JOB': 'bootstrap'}
CONFIG = {'expires_at': (NOW+timedelta(hours=2)).isoformat(),
          'budget_ledger': {'expires_at': (NOW+timedelta(hours=1)).isoformat(), 'reserved_usd': 334}}


def review():
    return {'schema': phase.SCHEMA, 'phase': 'custody-only', 'run_id': '123', 'run_attempt': '2',
            'foundation_source_sha': 'a'*40, 'release_root_sha': 'b'*40,
            'config_path': 'certification/hosted-pilot/bootstrap/deployments/foss4g-images.tfvars.json',
            **{key: 'c'*64 for key in ('config_sha256','provider_lock_sha256','resolved_config_sha256','plan_sha256')},
            'resource_changes': [{'address': 'aws_kms_key.custody', 'actions': ['create']}],
            **phase.deadlines(CONFIG, now=NOW)}


class Native:
    def __init__(self):
        self.calls = []
        self.run = {'id':123,'run_attempt':2,'head_sha':'a'*40,'head_branch':'trunk','event':'workflow_dispatch','workflow_id':88,'status':'in_progress'}
        self.workflow = {'id':88,'path':phase.WORKFLOW}
        self.detail = {'id':44,'name':'terraform-live-approval',
            'deployment_branch_policy':{'protected_branches':False,'custom_branch_policies':True},
            'protection_rules':[{'type':'required_reviewers','prevent_self_review':False,
                'reviewers':[{'type':'User','reviewer':{'id':12301237,'login':'mikemcdougall'}}]}]}
        self.approvals = [{'state':'approved','user':{'id':12301237,'login':'mikemcdougall','type':'User'},'environments':[{'id':44,'name':'terraform-live-approval'}]}]
        self.jobs = {'total_count':3,'jobs':[{'id':n,'name':name,'head_sha':'a'*40,'run_id':123,'status':'completed','conclusion':'success'} for n,name in enumerate(['bootstrap','review-ready','phase-approval'],1)]}
        self.jobs['jobs'][0].update(status='in_progress',conclusion=None)
        self.rows = []; self.zips = {}
        self.public = review()
        reference = self.add(phase.names('custody-only',ENV)[0], 'review.json', self.public)
        self.receipt = {'schema':'honua.hosted-pilot-phase-approval/v1','review':self.public,
            'review_reference':reference,'human_approval':{'reviewer_id':12301237,'reviewer_login':'mikemcdougall','environment_id':44,'environment':'terraform-live-approval'}}
        self.add(phase.names('custody-only',ENV)[1], 'approved.json', self.receipt)

    def add(self,name,filename,value):
        content = phase.canonical(value)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:z.writestr(filename,content)
        raw=stream.getvalue(); identifier=len(self.rows)+1
        self.zips[identifier]=raw
        self.rows.append({'id':identifier,'name':name,'expired':False,'size_in_bytes':len(raw),
            'workflow_run':{'id':123,'head_sha':'a'*40},'digest':'sha256:'+phase.sha(raw)})
        return {'artifact_id':identifier,'artifact_sha256':phase.sha(raw),'content_sha256':phase.sha(content)}

    def __call__(self,path,*,method='GET',binary=False):
        self.calls.append((method,path))
        if method=='POST':
            assert path=='repos/honua-io/honua-iac/actions/runs/123/cancel'
            return b''
        if path.endswith('/attempts/2'):return copy.deepcopy(self.run)
        if path.endswith('/actions/workflows/hosted-pilot-bootstrap.yml'):return copy.deepcopy(self.workflow)
        if '/artifacts?per_page=' in path:return {'artifacts':copy.deepcopy(self.rows)}
        if path.endswith('/zip'):return self.zips[int(path.split('/')[-2])]
        if '/jobs?per_page=' in path:return copy.deepcopy(self.jobs)
        if path.endswith('/approvals'):return copy.deepcopy(self.approvals)
        if '/environments/' in path:return copy.deepcopy(self.detail)
        raise AssertionError(path)


class PhaseApproval(unittest.TestCase):
    def setUp(self):
        self.native = Native()
        self.addCleanup(patch.stopall)
        patch.dict(os.environ,ENV,clear=True).start()
        patch.object(phase,'utcnow',return_value=NOW).start()

    def observe(self,value=None):
        return phase.approved_review(value or self.native.public,phase='custody-only',call=self.native)

    def test_actual_same_run_jobs_artifacts_and_human_review_join(self):
        result=self.observe()
        self.assertEqual(result['approval_job_id'],3)
        self.assertEqual(result['receipt'],self.native.receipt)
        self.assertTrue(all(method=='GET' for method,_ in self.native.calls))

    def test_missing_approval_is_only_pending_not_permission(self):
        self.native.rows=self.native.rows[:1]
        self.assertIsNone(self.observe())

    def test_wrong_native_source_run_attempt_workflow_and_ref_refuse(self):
        for key,value in [('id',124),('head_sha','d'*40),('run_attempt',1),('workflow_id',99),('head_branch','other'),('event','push')]:
            with self.subTest(key=key),patch.dict(self.native.run,{key:value}):
                with self.assertRaises(ValueError):self.observe()

    def test_private_review_identity_phase_hash_and_time_drift_refuse(self):
        for key,value in [('phase','remote-backed-foundation'),('run_id','124'),('run_attempt','1'),('foundation_source_sha','d'*40),('release_root_sha','d'*40),('plan_sha256','d'*64),('config_sha256','short'),('approval_expires_at',NOW.isoformat()),('execution_expires_at',(NOW+timedelta(days=1)).isoformat())]:
            with self.subTest(key=key):
                changed=copy.deepcopy(self.native.public);changed[key]=value
                with self.assertRaises(ValueError):self.observe(changed)

    def test_arbitrary_or_rejected_human_and_environment_refuse(self):
        for update in [[],[{'state':'rejected','user':self.native.approvals[0]['user'],'environments':self.native.approvals[0]['environments']}],
                       [{'state':'approved','user':{'id':1,'login':'foreign','type':'User'},'environments':self.native.approvals[0]['environments']}],
                       [{'state':'approved','user':dict(self.native.approvals[0]['user'],type='Bot'),'environments':self.native.approvals[0]['environments']}],
                       [{'state':'approved','user':self.native.approvals[0]['user'],'environments':[{'id':45,'name':'terraform-live-approval'}]}]]:
            with self.subTest(update=update),patch.object(self.native,'approvals',update):
                with self.assertRaises(ValueError):self.observe()

    def test_native_environment_policy_drift_refuses(self):
        self.native.detail['protection_rules'][0]['prevent_self_review']=True
        with self.assertRaises(ValueError):self.observe()

    def test_native_approval_job_must_complete_successfully_current_source(self):
        for key,value in [('conclusion','failure'),('head_sha','d'*40),('run_id',124)]:
            with self.subTest(key=key),patch.dict(self.native.jobs['jobs'][2],{key:value}):
                with self.assertRaises(ValueError):self.observe()

    def test_missing_and_truncated_job_inventories_refuse(self):
        for jobs in [{'total_count':1000,'jobs':self.native.jobs['jobs']}]:
            with patch.object(self.native,'jobs',jobs),self.assertRaises(ValueError):self.observe()

    def test_artifact_tampering_different_producer_duplicate_and_expiry_refuse(self):
        for key,value in [('expired',True),('digest','sha256:'+'0'*64),('workflow_run',{'id':124,'head_sha':'a'*40}),('size_in_bytes',5*1024*1024)]:
            with self.subTest(key=key),patch.dict(self.native.rows[0],{key:value}):
                with self.assertRaises(ValueError):self.observe()
        self.native.rows.append(copy.deepcopy(self.native.rows[0]))
        with self.assertRaises(ValueError):self.observe()

    def test_unexpected_archive_path_is_not_extracted(self):
        self.native.rows=[];self.native.zips={}
        self.native.add(phase.names('custody-only',ENV)[0],'../private.tfplan',{})
        with self.assertRaises(ValueError):phase.artifact_json(phase.names('custody-only',ENV)[0],'review.json',call=self.native)

    def test_approval_artifact_visible_before_job_completion_stays_pending(self):
        with patch.dict(self.native.jobs['jobs'][2],{'status':'in_progress','conclusion':None}):
            self.assertIsNone(self.observe())
        self.assertIsNotNone(self.observe())
        self.assertTrue(all(method=='GET' for method,_ in self.native.calls))

    def test_deadlines_preserve_original_configuration_and_expiry(self):
        cfg=copy.deepcopy(CONFIG);cfg['budget_ledger']['expires_at']=(NOW+timedelta(minutes=22)).isoformat()
        original=copy.deepcopy(cfg);result=phase.deadlines(cfg,now=NOW)
        self.assertEqual(cfg,original)
        self.assertEqual(phase.stamp(result['approval_expires_at']),NOW+timedelta(minutes=7))
        self.assertEqual(phase.stamp(result['execution_expires_at']),NOW+timedelta(minutes=22))
        cfg['budget_ledger']['expires_at']=(NOW+timedelta(minutes=15)).isoformat()
        with self.assertRaises(ValueError):phase.deadlines(cfg,now=NOW)

    def test_old_or_future_review_cannot_admit_apply(self):
        for now in [NOW-timedelta(seconds=1),NOW+timedelta(minutes=20),NOW+timedelta(hours=2)]:
            with self.subTest(now=now),self.assertRaises(ValueError):phase.validate_review(self.native.public,phase='custody-only',now=now)

    def test_watchdog_cancels_only_own_bound_run_with_no_aws_calls(self):
        self.native.jobs['jobs'].append({'id':4,'name':'phase-watchdog','head_sha':'a'*40,'run_id':123,'status':'in_progress','conclusion':None})
        self.native.jobs['total_count']=4
        with patch.object(phase,'api',self.native),patch.dict(os.environ,{'GITHUB_JOB':'phase-watchdog'}),patch.object(phase,'utcnow',return_value=NOW+timedelta(minutes=36)),patch('sys.argv',['gate','watchdog','--phase','custody-only']):
            phase.main()
        self.assertEqual([row for row in self.native.calls if row[0]!='GET'],[('POST','repos/honua-io/honua-iac/actions/runs/123/cancel')])

    def test_watchdog_wrong_source_never_posts(self):
        with patch.object(phase,'api',self.native),patch.dict(os.environ,{'GITHUB_SHA':'e'*40}),patch('sys.argv',['gate','watchdog','--phase','custody-only']):
            with self.assertRaises(ValueError):phase.main()
        self.assertTrue(all(method=='GET' for method,_ in self.native.calls))

    def test_watchdog_allows_only_preapproved_execution_window(self):
        jobs={row['name']:row for row in self.native.jobs['jobs']}
        self.assertFalse(phase.watchdog_decision(self.native.public,jobs,now=NOW+timedelta(minutes=21)))
        jobs['phase-approval']['status']='in_progress'
        self.assertTrue(phase.watchdog_decision(self.native.public,jobs,now=NOW+timedelta(minutes=20)))
        self.assertTrue(phase.watchdog_decision(self.native.public,jobs,now=NOW+timedelta(minutes=35)))


class ExactPhaseApply(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.work=Path(self.temp.name);self.binary=self.work/'custody.tfplan';self.binary.write_bytes(b'private original plan')
        self.cfg=copy.deepcopy(CONFIG);self.identity={key:value for key,value in review().items() if key in ['foundation_source_sha','release_root_sha','config_path','config_sha256','provider_lock_sha256']}
        self.actor={'principal_arn':'actual-native-sts-identity'}
        self.r=review();self.r['plan_sha256']=bootstrap.digest(self.binary);self.r['execution_identity']=self.actor
        self.r['resolved_config_sha256']='d'*64
        bootstrap.private_json(self.work/'review.json',self.r)
        bootstrap.private_json(self.work/'custody-recovery.json',{'actual':'journal'})
        self.prepared={'schema':'honua.private-bootstrap-prepared/v1','review_sha256':bootstrap.digest(self.work/'review.json'),'review':self.r,'binary':'custody.tfplan','binding':{},'remote':False,'state_version_id':None}
        self.addCleanup(patch.stopall);patch.dict(os.environ,ENV,clear=True).start();patch.object(phase,'utcnow',return_value=NOW).start()
        self.approve=patch.object(phase,'approved_review',return_value={'receipt':'native-verified'}).start()
        self.apply=patch.object(bootstrap,'exact_apply').start()
        self.run=patch.object(bootstrap,'run').start()
        patch.object(bootstrap,'secure_backend',return_value={'bucket':'custody','key':'original','kms_key_id':'key'}).start()
        patch.object(bootstrap,'save_recovery',return_value={'version_id':'immutable'}).start()
        patch.object(bootstrap,'exists',return_value=True).start()

    def execute(self):
        bootstrap.private_json(self.work/'prepared.json',self.prepared)
        bootstrap.apply_prepared(self.identity,self.actor,self.cfg,'d'*64,'custody-only',{},self.work)

    def test_exact_custody_apply_stops_before_any_full_plan_or_binding(self):
        self.execute()
        self.apply.assert_called_once_with(self.binary,self.r['plan_sha256'],self.work)
        self.assertFalse((self.work/'bindings.json').exists())
        self.assertTrue((self.work/'custody-applied.json').exists())
        self.assertEqual([args[0][0][:2] for args in self.run.call_args_list],[['terraform','init']])

    def test_altered_binary_is_rejected_without_apply(self):
        self.binary.write_bytes(b'changed plan')
        with self.assertRaises(ValueError):self.execute()
        self.apply.assert_not_called()

    def test_missing_actual_approval_is_rejected_without_apply(self):
        self.approve.return_value=None
        with self.assertRaises(ValueError):self.execute()
        self.apply.assert_not_called()

    def test_changed_source_configuration_or_saved_phase_refuses(self):
        for key,value in [('schema','foreign'),('binary','saved.tfplan'),('review_sha256','0'*64),('binding',{'bucket':'foreign'})]:
            with self.subTest(key=key),patch.dict(self.prepared,{key:value}):
                with self.assertRaises(ValueError):self.execute()
        self.apply.assert_not_called()

    def test_original_funding_expiry_cannot_be_extended_by_review(self):
        self.cfg['budget_ledger']['expires_at']=(NOW+timedelta(minutes=25)).isoformat()
        with self.assertRaises(ValueError):self.execute()
        self.apply.assert_not_called()

    def test_approval_deadline_rechecked_before_apply(self):
        with patch.object(phase,'utcnow',return_value=NOW+timedelta(minutes=20)),self.assertRaises(ValueError):self.execute()
        self.apply.assert_not_called()


class MainPhaseRouting(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.release=self.root/'release';self.source=self.root/'iac';self.tf=self.release/'certification/hosted-pilot/bootstrap'
        self.tf.mkdir(parents=True);(self.tf/'.terraform.lock.hcl').write_text('original lock')
        self.config_path='certification/hosted-pilot/bootstrap/deployments/test.tfvars.json'
        config=self.source/self.config_path;config.parent.mkdir(parents=True)
        self.cfg=copy.deepcopy(CONFIG)|{'region':'us-east-1','name_prefix':'honua-pilot-offline','account_id':'123456789012'}
        bootstrap.private_json(config,self.cfg)
        self.environment=ENV|{'RUNNER_TEMP':str(self.root/'runner'),'PILOT_FOUNDATION_SOURCE_ROOT':str(self.source),'AWS_REGION':'us-east-1'}
        (self.root/'runner').mkdir()
        self.addCleanup(patch.stopall);patch.dict(os.environ,self.environment,clear=True).start()
        patch.object(bootstrap,'ROOT',self.release).start();patch.object(bootstrap,'TF',self.tf).start()
        patch.object(phase,'utcnow',return_value=NOW).start()
        self.commands=[]
        def run(args,**kwargs):
            self.commands.append(args)
            if args[0]=='git' and args[-2:]==['rev-parse','HEAD']:
                return ((ENV['GITHUB_SHA'] if '-C' in args else ENV['PILOT_RELEASE_ROOT_SHA'])+'\n').encode()
            return b''
        patch.object(bootstrap,'run',side_effect=run).start()
        patch.object(bootstrap,'aws',side_effect=lambda *args: {'VersionId':'original-state-version'} if args[:2]==('s3api','head-object') else {'Account':'123456789012','Arn':'arn:aws:iam::123456789012:user/test','UserId':'AIDATEST'}).start()
        self.exists=patch.object(bootstrap,'exists',return_value=False).start()
        patch.object(bootstrap,'recover_imports').start();patch.object(bootstrap,'refuse_unbound_initial_custody').start()
        patch.object(bootstrap,'secure_backend',side_effect=lambda value:dict(value,kms_key_id='actual-key')).start()
        self.old_tags=bootstrap.creation_tags({'foundation_source_sha':'1'*40,'release_root_sha':'2'*40,'config_path':self.config_path,'config_sha256':'3'*64,'provider_lock_sha256':'4'*64},'99')
        patch.object(bootstrap,'observe_original_custody',return_value=self.old_tags).start()
        self.plan_changes=[{'address':'aws_iam_role.executor["plan"]','actions':['create']}]
        def plan(config,work,*,custody_only=False):
            path=work/('custody.tfplan' if custody_only else 'saved.tfplan');path.write_bytes(b'exact binary')
            bootstrap.private_json(work/'creation-resources.json',[])
            return path,([{'address':'aws_kms_key.custody','actions':['create']}] if custody_only else self.plan_changes),bootstrap.digest(path)
        self.plan=patch.object(bootstrap,'plan',side_effect=plan).start()
        self.apply=patch.object(bootstrap,'exact_apply').start()

    def execute(self,selected,mode='prepare'):
        with patch('sys.argv',['bootstrap',mode,'--phase',selected,'--config',self.config_path]):bootstrap.main()

    def test_foundation_refuses_absent_canonical_custody_without_initial_plan_or_apply(self):
        with self.assertRaises(ValueError):self.execute('remote-backed-foundation')
        self.plan.assert_not_called();self.apply.assert_not_called()
        self.assertFalse(any(cmd[0]=='terraform' for cmd in self.commands))

    def test_initial_custody_refuses_existing_canonical_state(self):
        self.exists.return_value=True
        with self.assertRaises(ValueError):self.execute('custody-only')
        self.plan.assert_not_called();self.apply.assert_not_called()

    def test_fresh_custody_preparation_produces_only_its_private_binary(self):
        self.execute('custody-only')
        self.assertEqual(self.plan.call_count,1)
        self.assertTrue(self.plan.call_args.kwargs['custody_only'])
        self.apply.assert_not_called()
        work=self.root/'runner/hosted-pilot-bootstrap-private'
        self.assertTrue((work/'custody.tfplan').exists());self.assertFalse((work/'saved.tfplan').exists())
        self.assertEqual(json.loads((work/'review.json').read_text())['phase'],'custody-only')

    def test_foundation_preserves_old_custody_tags_but_current_new_resource_identity(self):
        self.exists.return_value=True
        self.execute('remote-backed-foundation')
        self.assertEqual(self.plan.call_count,1)
        self.assertFalse(self.plan.call_args.kwargs)
        self.assertEqual(os.environ['PILOT_CREATION_RUN_ID'],'123')
        self.assertEqual(json.loads(os.environ['PILOT_CREATION_IDENTITY'])['release_root_sha'],'b'*40)
        cfg=json.loads((self.root/'runner/hosted-pilot-bootstrap-private/prepared-config.json').read_text())
        self.assertEqual(cfg['bootstrap_custody_creation_tags'],self.old_tags)
        self.apply.assert_not_called()

    def test_foundation_custody_update_cannot_get_a_prepared_approval_record(self):
        self.exists.return_value=True;self.plan_changes=[{'address':'aws_kms_key.custody','actions':['update']}]
        with self.assertRaises(ValueError):self.execute('remote-backed-foundation')
        self.assertFalse((self.root/'runner/hosted-pilot-bootstrap-private/prepared.json').exists())
        self.apply.assert_not_called()

    def test_missing_private_saved_plan_does_not_fall_back_to_replan(self):
        with self.assertRaises(OSError):self.execute('custody-only',mode='apply')
        self.plan.assert_not_called();self.apply.assert_not_called()


class WorkflowGraph(unittest.TestCase):
    def test_approval_graph_cannot_wait_on_still_running_plan_job(self):
        import yaml
        workflow = yaml.safe_load((Path(phase.__file__).resolve().parents[2]/'certification/hosted-pilot/bootstrap/foundation-bootstrap-workflow.yml').read_text())
        jobs = workflow['jobs']
        self.assertEqual(jobs['bootstrap']['needs'],'approval-policy')
        self.assertEqual(jobs['review-ready']['needs'],'approval-policy')
        self.assertEqual(jobs['phase-approval']['needs'],['approval-policy','review-ready'])
        self.assertNotIn('bootstrap',jobs['phase-approval']['needs'])
        self.assertEqual(jobs['phase-approval']['environment'],'terraform-live-approval')
        self.assertEqual(jobs['phase-watchdog']['permissions'],{'contents':'read','actions':'write'})
        self.assertEqual(workflow['permissions'],{'contents':'read','actions':'read'})
        self.assertNotIn('AWS',json.dumps(jobs['phase-watchdog'].get('env',{})))
        self.assertTrue(all('configure-aws-credentials' not in str(step) for step in jobs['phase-watchdog']['steps']))
        names=[step.get('name','') for step in jobs['bootstrap']['steps']]
        self.assertLess(names.index('Publish value-free review before waiting for native approval'),names.index('Wait for original exact-plan approval from the separate protected job'))
        self.assertLess(names.index('Wait for original exact-plan approval from the separate protected job'),names.index('Apply only the exact reviewed phase and stop'))
        for job in jobs.values():
            for step in job['steps']:
                if 'upload-artifact@' in step.get('uses',''):
                    self.assertTrue(step['with']['path'].endswith('.json'))

    def test_legacy_deploy_refuses_before_any_aws_or_terraform_call(self):
        with patch('sys.argv',['bootstrap','deploy','--config','anything']),patch.object(bootstrap,'aws') as aws,patch.object(bootstrap,'run') as run,patch('sys.stderr',io.StringIO()):
            with self.assertRaises(SystemExit) as result:bootstrap.main()
        self.assertEqual(result.exception.code,2)
        aws.assert_not_called();run.assert_not_called()


if __name__=='__main__':unittest.main()
