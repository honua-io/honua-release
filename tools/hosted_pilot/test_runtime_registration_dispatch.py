"""Permanent launch fencing, acknowledgement loss and original task observation."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from controller_archive import canonical, digest
import runtime_registration_dispatch as dispatch
import test_registration_dispatch_admission as admission_fixture


class OriginalRegistrationDispatch(unittest.TestCase):
    def setUp(self):
        original = admission_fixture.OriginalDispatchAdmission()
        self.addCleanup(original.doCleanups); original.setUp()
        self.original = original
        self.verified = original.verify()
        cfg = original.config; base = cfg['taskDefinitionFamily'].replace(':ecs:',':states:').split(':task-definition/')[0]
        machine = base+':stateMachine:'+cfg['taskDefinitionFamily'].split('/')[-1]
        self.value = {'configuration':cfg,'configurationSha256':digest(canonical(cfg)),
            'taskDefinitionArn':cfg['taskDefinitionFamily']+':7', 'stateMachineBaseArn':machine,'stateMachineArn':machine+':3',
            'dispatchBudget':{'candidateKey':cfg['candidateKey'],'generationDigest':cfg['generationDigest'],
                'reservationId':'dispatch-original','maximumDispatches':4,'state':cfg['state'],'claimTable':'claims'}}
        self.value['installation'] = {'claimKey':'runtime-registration:'+self.value['configurationSha256'][7:],
            'claimTable':'claims','originalFundingExpiresEpoch':self.verified['admission']['originalFundingExpiresEpoch'],
            'startedBy':'reg-'+self.value['configurationSha256'][7:39]}
        self.db = MagicMock(); self.db.get_item.return_value = {}
        self.states = MagicMock(); self.ecs = MagicMock(); self.scheduler = MagicMock()

    def claim(self):
        return dispatch.claim_once(self.value,self.verified,db=self.db,overhead_head=self.verified['head'])

    def test_new_launch_claim_atomically_fences_every_actual_head_field_and_capacity(self):
        created, request, execution, claim = self.claim()
        self.assertTrue(created)
        transaction = self.db.transact_write_items.call_args.kwargs['TransactItems']
        condition = transaction[0]['ConditionCheck']
        self.assertEqual(condition['TableName'],self.original.config['state']['table'])
        for key,value in self.verified['head'].items():
            self.assertIn(key+' =',condition['ConditionExpression'])
            self.assertIn(value,condition['ExpressionAttributeValues'].values())
        self.assertEqual(transaction[2]['Put']['Item'],claim)
        self.assertEqual(transaction[2]['Put']['ConditionExpression'],'attribute_not_exists(claimKey)')
        self.assertEqual(claim['inputDigest']['S'],digest(canonical(request)))
        self.assertIn('used < :maximum',transaction[1]['Update']['ConditionExpression'])
        self.assertEqual(claim['deadlineEpoch']['N'],str(self.verified['admission']['deadlineEpoch']))
        self.assertTrue(execution.endswith(':registration-'+self.value['configurationSha256'][7:]))

    def test_head_drift_between_admission_and_dispatch_overhead_refuses_before_claim(self):
        different = copy.deepcopy(self.verified['head']); different['stateVersion'] = {'N':'9'}
        with self.assertRaisesRegex(ValueError,'different original heads'):
            dispatch.claim_once(self.value,self.verified,db=self.db,overhead_head=different)
        self.db.transact_write_items.assert_not_called()

    def test_original_claim_recovery_never_consumes_capacity_again(self):
        _, expected, execution, claim = self.claim()
        self.db.reset_mock(); self.db.get_item.return_value = {'Item':claim}
        created, request, observed, retained = self.claim()
        self.assertFalse(created); self.assertEqual(request,expected); self.assertEqual(observed,execution)
        self.assertEqual(retained,claim); self.db.transact_write_items.assert_not_called()

    def test_changed_original_input_or_deadline_cannot_replace_a_claim(self):
        _, _, _, original = self.claim()
        for key,value in [('inputDigest',{'S':'sha256:'+'0'*64}),('deadlineEpoch',{'N':'9999999999'}),
                          ('stateMachineArn',{'S':self.value['stateMachineBaseArn']+':4'})]:
            with self.subTest(key=key):
                row = copy.deepcopy(original); row[key] = value
                self.db.get_item.return_value = {'Item':row}
                with self.assertRaisesRegex(ValueError,'permanent registration claim differs'): self.claim()

    def native_execution(self, request, execution):
        return {'executionArn':execution,'stateMachineArn':self.value['stateMachineBaseArn'],
            'stateMachineVersionArn':self.value['stateMachineArn'],'input':canonical(request).decode(),
            'inputDetails':{'included':True},'redriveCount':0,'status':'SUCCEEDED'}

    def native_task(self, request):
        cfg = self.value['configuration']; arn = cfg['clusterArn'].replace(':cluster/',':task/')+'/original'
        launched = {'Tasks':[{'TaskArn':arn,'TaskDefinitionArn':self.value['taskDefinitionArn'],'ClusterArn':cfg['clusterArn']}],'Failures':[]}
        self.states.get_execution_history.return_value = {'events':[{'type':'TaskStateExited','stateExitedEventDetails':{
            'name':'RunRegistrar','outputDetails':{'truncated':False},'output':json.dumps({'launched':launched})}}]}
        task = {'taskArn':arn,'clusterArn':cfg['clusterArn'],'taskDefinitionArn':self.value['taskDefinitionArn'],
            'lastStatus':'STOPPED','launchType':'FARGATE','enableExecuteCommand':False,'startedBy':self.value['installation']['startedBy'],
            'tags':[{'key':key,'value':value} for key,value in {'honua-registration':self.value['configurationSha256'],
                'honua-registration-admission':request['admissionDigest'],'honua-registration-candidate':cfg['candidateKey']}.items()],
            'containers':[{'name':'runtime-registration','image':cfg['imageUri'],'exitCode':0}]}
        self.ecs.describe_tasks.return_value = {'tasks':[task],'failures':[]}
        return task

    def test_exact_published_version_and_original_input_are_required(self):
        _, request, execution, _ = self.claim()
        original = self.native_execution(request,execution)
        self.states.describe_execution.return_value = original
        self.assertEqual(dispatch.observed_execution(self.states,self.value,request,execution),original)
        for changed in [{'stateMachineVersionArn':self.value['stateMachineBaseArn']+':4'}, {'redriveCount':1},
                        {'inputDetails':{'included':False}}, {'input':'{}'}]:
            with self.subTest(changed=changed):
                self.states.describe_execution.return_value = original|changed
                with self.assertRaisesRegex(ValueError,'input, version or redrive'):
                    dispatch.observed_execution(self.states,self.value,request,execution)

    def test_original_history_positive_stopped_task_and_all_binding_tags_are_required(self):
        _, request, execution, _ = self.claim(); task = self.native_task(request)
        actual, pages = dispatch.original_task(self.states,self.ecs,self.value,execution,request)
        self.assertEqual(actual,task); self.assertEqual(len(pages),1)
        for changed in [{'lastStatus':'RUNNING'},{'tags':[]},{'taskDefinitionArn':self.value['taskDefinitionArn']+'0'}]:
            with self.subTest(changed=changed):
                self.ecs.describe_tasks.return_value = {'tasks':[task|changed],'failures':[]}
                with self.assertRaisesRegex(ValueError,'binding or stopped'):
                    dispatch.original_task(self.states,self.ecs,self.value,execution,request)
        self.ecs.describe_tasks.return_value = {'failures':[{'arn':task['taskArn'],'reason':'MISSING'}],'tasks':[]}
        with self.assertRaisesRegex(ValueError,'positively observed'):
            dispatch.original_task(self.states,self.ecs,self.value,execution,request)

    def test_lost_start_acknowledgement_and_reentry_make_only_one_start_call(self):
        _, request, execution, claim = self.claim()
        self.db.reset_mock(); self.db.get_item.return_value = {}
        self.states.start_execution.side_effect = TimeoutError('lost acknowledgement')
        self.states.describe_execution.return_value = self.native_execution(request,execution)
        self.native_task(request); retained = []
        store = SimpleNamespace(db=self.db,s3=MagicMock())
        with patch.object(dispatch,'validate_handoff',return_value=self.value['configuration']), \
             patch.object(dispatch,'inspect_lifecycle',return_value={}), patch.object(dispatch,'inspect_task_definition',return_value={}), \
             patch.object(dispatch,'verify_original_dispatch_admission',return_value=self.verified), \
             patch.object(dispatch,'read_allowance',return_value=self.verified['head']):
            answer = dispatch.dispatch(self.value,store=store,ecs=self.ecs,states=self.states,scheduler=self.scheduler,
                clock=lambda:self.original.now,retain=retained.append)
            self.db.get_item.return_value = {'Item':claim}
            recovered = dispatch.dispatch(self.value,store=store,ecs=self.ecs,states=self.states,scheduler=self.scheduler,
                clock=lambda:self.original.now,retain=retained.append)
        self.assertEqual(self.states.start_execution.call_count,1)
        self.assertEqual(answer['executionArn'],recovered['executionArn'])
        self.assertFalse(recovered['activation']); self.assertFalse(recovered['qualification'])

    def test_unobserved_start_keeps_permanent_claim_without_relaunch(self):
        _, request, execution, claim = self.claim(); self.db.reset_mock(); self.db.get_item.return_value = {'Item':claim}
        self.states.describe_execution.side_effect = ValueError('ExecutionDoesNotExist')
        store = SimpleNamespace(db=self.db,s3=MagicMock())
        with patch.object(dispatch,'validate_handoff',return_value=self.value['configuration']), \
             patch.object(dispatch,'inspect_lifecycle',return_value={}), patch.object(dispatch,'inspect_task_definition',return_value={}), \
             patch.object(dispatch,'verify_original_dispatch_admission',return_value=self.verified), \
             patch.object(dispatch,'read_allowance',return_value=self.verified['head']), self.assertRaises(ValueError):
            dispatch.dispatch(self.value,store=store,ecs=self.ecs,states=self.states,scheduler=self.scheduler,
                clock=lambda:self.original.now,retain=MagicMock())
        self.states.start_execution.assert_not_called(); self.db.transact_write_items.assert_not_called()



class FixedWorkflowCustody(unittest.TestCase):
    def test_resolves_hash_locked_wheels_before_aws_credentials_and_uses_fixed_entry(self):
        import yaml
        root=Path(__file__).resolve().parents[2]
        workflow=yaml.safe_load((root/'.github/workflows/hosted-pilot-runtime-registration.yml').read_text())
        preflight=workflow['jobs']['preflight'];register=workflow['jobs']['register']
        self.assertNotIn('id-token',preflight.get('permissions',{}))
        self.assertNotIn('environment',preflight)
        self.assertFalse(any('configure-aws-credentials@' in row.get('uses','') for row in preflight['steps']))
        self.assertTrue(any('pip download --require-hashes --only-binary=:all:' in row.get('run','') for row in preflight['steps']))
        self.assertEqual(register['needs'],'preflight')
        installations=[(i,row['run']) for i,row in enumerate(register['steps']) if 'pip install' in row.get('run','')]
        auth=next(i for i,row in enumerate(register['steps']) if 'configure-aws-credentials@' in row.get('uses',''))
        self.assertEqual(len(installations),1);self.assertLess(installations[0][0],auth)
        self.assertIn('--no-index',installations[0][1]);self.assertIn('--require-hashes',installations[0][1])
        self.assertIn('--only-binary=:all:',installations[0][1])
        runs='\n'.join(row.get('run','') for row in register['steps'])
        self.assertIn('python tools/hosted_pilot/runtime_registration_dispatch.py',runs)
        self.assertNotIn('secrets.',json.dumps(register))

if __name__ == '__main__': unittest.main()
