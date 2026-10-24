"""Synthetic clock/native-lock controls; no deployment or real cleanup proof."""
import copy
import json
from datetime import datetime, timezone
import unittest
import shared_retirement as retire

class RetirementWaitControls(unittest.TestCase):
    def setUp(self):
        self.now=2000000000
        self.raw={'ID':'11111111-1111-1111-1111-111111111111','Operation':'OperationTypeApply','Info':'',
            'Who':'runner@original-host','Version':'1.13.5','Created':datetime.fromtimestamp(self.now-1,timezone.utc).isoformat(),
            'Path':'original-bucket/owned/terraform.tfstate'}
        self.lock=retire.lock_original(json.dumps(self.raw).encode(),bucket='original-bucket',key='owned/terraform.tfstate',
            expected_who='runner@original-host',earliest_epoch=self.now-30,now=self.now)
        self.binding={'originalFundingExpiresEpoch':self.now+903,'retirementAtEpoch':self.now+3,
            'credentialExpiresEpoch':self.now+3600,'jobStartedEpoch':self.now-30,
            'producer':{'runId':123,'runAttempt':1},'candidateKey':'sha256:'+'a'*64,
            'sourceConfiguration':{'path':'reviewed/config.json','sha256':'sha256:'+'b'*64},
            'plan':{'sha256':'sha256:'+'c'*64},'backend':{'sha256':'sha256:'+'d'*64},
            'nativeApproval':{'environment':'hosted-pilot-destroy','reviewerId':12301237}}
    def execute(self,*,reader=None,sleeper=None):
        self.records=[];self.elapsed=0
        def sleep(seconds):
            self.now+=seconds;self.elapsed+=seconds
        return retire.wait_for_original_deadline(self.binding,observe_lock=reader or (lambda:copy.deepcopy(self.lock)),
            retain=lambda row:self.records.append(copy.deepcopy(row)),clock=lambda:self.now,
            monotonic=lambda:self.elapsed,sleep=sleeper or sleep)
    def test_original_locked_wait_reaches_attempt_only_at_exact_deadline(self):
        self.execute()
        self.assertEqual(self.now,self.binding['retirementAtEpoch'])
        self.assertEqual([row['stage'] for row in self.records],['waiting','cleanup-attempt-starting'])
        self.assertTrue(all(row['activation'] is False and row['qualification'] is False for row in self.records))
        self.assertTrue(all('deleted' not in row and 'settled' not in row for row in self.records))
    def test_infeasible_actual_sts_expiry_refuses_before_wait_witness(self):
        self.binding['credentialExpiresEpoch']=self.binding['originalFundingExpiresEpoch']
        with self.assertRaisesRegex(ValueError,'cannot fit'):self.execute()
        self.assertEqual(self.records,[])
    def test_restart_cannot_move_original_deadline(self):
        self.binding['retirementAtEpoch']+=1
        with self.assertRaisesRegex(ValueError,'original funding'):self.execute()
    def test_changed_native_lock_never_reaches_cleanup(self):
        reads=0
        def read():
            nonlocal reads
            reads+=1
            value=copy.deepcopy(self.lock)
            if reads>1:value['id']='22222222-2222-2222-2222-222222222222'
            return value
        with self.assertRaisesRegex(ValueError,'lock changed'):self.execute(reader=read)
        self.assertEqual([row['stage'] for row in self.records],['waiting'])
    def test_cancellation_is_not_a_successful_wait(self):
        def interrupt(_):raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):self.execute(sleeper=interrupt)
        self.assertEqual([row['stage'] for row in self.records],['waiting'])
    def test_wrong_lock_owner_backend_operation_and_tainted_time_refuse(self):
        for field,value in [('Who','another@host'),('Path','foreign/key'),('Operation','OperationTypePlan'),
                            ('Created',datetime.fromtimestamp(self.now-40,timezone.utc).isoformat())]:
            with self.subTest(field=field):
                raw=copy.deepcopy(self.raw);raw[field]=value
                with self.assertRaises(ValueError):retire.lock_original(json.dumps(raw).encode(),bucket='original-bucket',
                    key='owned/terraform.tfstate',expected_who='runner@original-host',earliest_epoch=self.now-30,now=self.now)


class OriginalScopeControls(unittest.TestCase):
    def test_complete_plan_excludes_accounting_and_changes(self):
        def plan(addresses):return {'resource_changes':[{'address':address,'change':{'actions':['delete']}} for address in addresses]}
        addresses=[retire.GATE,'module.application.aws_ecs_service.server','module.network.aws_nat_gateway.this[0]']
        self.assertEqual(retire.target_only_plan(plan(addresses)),sorted(addresses))
        for extra in ['module.accounting.aws_dynamodb_table.state','aws_s3_bucket.handoff','module.application_neighbor.aws_s3_bucket.data']:
            with self.subTest(extra=extra),self.assertRaisesRegex(ValueError,'retained custody/accounting'):
                retire.target_only_plan(plan(addresses+[extra]))
        for actions in [['create'],['update'],['delete','create']]:
            value=plan(addresses);value['resource_changes'][0]['change']['actions']=actions
            with self.assertRaises(ValueError):retire.target_only_plan(value)
    def test_existing_gate_required_tainted_gate_is_never_accepted(self):
        expected={'originalFundingExpiresEpoch':2000001000,'retirementAtEpoch':2000000100}
        row={'mode':'managed','type':'terraform_data','name':'shared_retirement','instances':[{'index_key':0,
            'attributes':{'input':{'value':expected}},'dependencies':['module.application.aws_ecs_service.server','module.network.aws_nat_gateway.this[0]']}]}
        state={'version':4,'resources':[row]}
        retire.staged_gate(state,expected)
        for mutate in ('absent','tainted','changed','dependencies','bare-module'):
            value=copy.deepcopy(state)
            if mutate=='absent':value['resources']=[]
            if mutate=='tainted':value['resources'][0]['instances'][0]['status']='tainted'
            if mutate=='changed':value['resources'][0]['instances'][0]['attributes']['input']['value']['retirementAtEpoch']+=1
            if mutate=='dependencies':value['resources'][0]['instances'][0]['dependencies']=[]
            if mutate=='bare-module':value['resources'][0]['instances'][0]['dependencies']=['module.application','module.network']
            with self.subTest(mutate=mutate),self.assertRaises(ValueError):retire.staged_gate(value,expected)


class NativeApprovalControls(unittest.TestCase):
    def setUp(self):
        self.sha='a'*40
        self.policy={'repository':retire.REPOSITORY,'environment':'hosted-pilot-apply','can_admins_bypass':False,
            'prevent_self_review':False,'reviewers':[{'type':'User','id':12301237,'login':'mikemcdougall'}]}
        self.run={'id':123,'run_attempt':1,'head_sha':self.sha,'head_branch':'trunk','event':'workflow_dispatch',
            'path':retire.WORKFLOW,'status':'in_progress'}
        self.jobs={'total_count':2,'jobs':[{'name':'plan','status':'completed','conclusion':'success'},
            {'name':'apply','status':'in_progress','conclusion':None,'started_at':'2033-05-18T03:30:00Z'}]}
        self.detail={'id':777,'name':'hosted-pilot-apply','can_admins_bypass':False,
            'deployment_branch_policy':{'protected_branches':False,'custom_branch_policies':True},
            'protection_rules':[{'type':'required_reviewers','prevent_self_review':False,'reviewers':[
                {'type':'User','reviewer':{'id':12301237,'login':'mikemcdougall'}}]}]}
        self.approvals=[{'state':'approved','user':{'type':'User','id':12301237,'login':'mikemcdougall'},
            'environments':[{'id':777,'name':'hosted-pilot-apply'}]}]
        self.calls=[]
    def call(self,path):
        self.calls.append(path)
        if '/environments/' in path:return self.detail
        if path.endswith('/approvals'):return self.approvals
        if '/jobs?' in path:return self.jobs
        return self.run
    def check(self):return retire.native_job(source_sha=self.sha,run_id=123,run_attempt=1,policy=self.policy,call=self.call)
    def test_same_run_native_job_and_human_approval_are_observed(self):
        result=self.check()
        self.assertEqual(result['approval'],self.approvals[0])
        self.assertTrue(all(path.startswith('repos/honua-io/honua-release/') for path in self.calls))
    def test_absent_wrong_user_environment_or_rejected_approval_refuses(self):
        original=copy.deepcopy(self.approvals)
        for change in ('absent','user','environment','rejected'):
            self.approvals=copy.deepcopy(original)
            if change=='absent':self.approvals=[]
            if change=='user':self.approvals[0]['user']['id']=1
            if change=='environment':self.approvals[0]['environments'][0]['id']=778
            if change=='rejected':self.approvals[0]['state']='rejected'
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'human approval'):self.check()
    def test_changed_source_attempt_workflow_and_cancelled_run_refuse(self):
        original=copy.deepcopy(self.run)
        for field,value in [('head_sha','b'*40),('run_attempt',2),('path','unreviewed.yml'),('status','completed')]:
            self.run=original|{field:value}
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'workflow'):self.check()
    def test_cancelled_apply_failed_plan_and_incomplete_jobs_refuse(self):
        original=copy.deepcopy(self.jobs)
        for change in ('cancelled','failedplan','pagination'):
            self.jobs=copy.deepcopy(original)
            if change=='cancelled':self.jobs['jobs'][1]|={'status':'completed','conclusion':'cancelled'}
            if change=='failedplan':self.jobs['jobs'][0]['conclusion']='failure'
            if change=='pagination':self.jobs['total_count']=101
            with self.subTest(change=change),self.assertRaises(ValueError):self.check()
    def test_current_native_environment_cannot_bypass_reviewer(self):
        self.detail['can_admins_bypass']=True
        with self.assertRaisesRegex(ValueError,'bypass'):self.check()


class CleanupRecoveryControls(unittest.TestCase):
    setUp=RetirementWaitControls.setUp
    execute=RetirementWaitControls.execute
    def test_late_scheduled_wait_never_continues(self):
        self.now=self.binding['originalFundingExpiresEpoch']+1
        with self.assertRaisesRegex(ValueError,'original funding'):self.execute()
        self.assertEqual(self.records,[])
    def test_distinct_recovery_is_cleanup_only_under_original_deadline(self):
        original_expiry=self.binding['originalFundingExpiresEpoch']
        self.now=original_expiry+1
        self.binding['cleanupRecovery']=True
        self.binding['jobStartedEpoch']=self.now-30
        self.execute()
        self.assertEqual([row['stage'] for row in self.records],['recovery-cleanup-attempt-starting'])
        row=self.records[0]
        self.assertFalse(row['activation']);self.assertFalse(row['qualification'])
        self.assertEqual(row['window']['originalFundingExpiresEpoch'],original_expiry)
        self.assertNotIn('plannedCompletionByEpoch',row['window'])
        self.assertEqual(row['window']['currentCleanupAttemptByEpoch'],self.binding['jobStartedEpoch']+900)
    def test_recovery_cannot_remove_current_credential_cleanup_margin(self):
        self.now=self.binding['originalFundingExpiresEpoch']+1
        self.binding['cleanupRecovery']=True
        self.binding['jobStartedEpoch']=self.now-30
        self.binding['credentialExpiresEpoch']=self.now+800
        with self.assertRaisesRegex(ValueError,'cannot fit'):self.execute()
        self.assertEqual(self.records,[])


class FixedRetirementEntry(unittest.TestCase):
    def test_missing_native_context_refuses_without_ambient_pythonpath_or_credential_output(self):
        import os,subprocess,sys
        sentinel='synthetic-private-credential-never-exported'
        result=subprocess.run([sys.executable,retire.__file__],capture_output=True,
            env={'PATH':os.environ['PATH'],'GH_TOKEN':sentinel})
        self.assertNotEqual(result.returncode,0)
        self.assertIn(b'scheduled cleanup refused',result.stderr)
        self.assertNotIn(sentinel.encode(),result.stdout+result.stderr)

if __name__=='__main__':unittest.main()
