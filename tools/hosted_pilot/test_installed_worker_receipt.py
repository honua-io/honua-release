"""Synthetic original-source/custody controls; no cloud deployment or receipt."""
import base64
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import nullcontext
from unittest.mock import MagicMock, patch
import installed_worker_receipt as receipt
from agent_e2e_pack import canonical_digest, PackError
from fixture_handoff import encoded


class InstalledOutputOriginals(unittest.TestCase):
    def setUp(self):
        self.candidate='sha256:'+'a'*64; self.runtime='runtime'; self.slot='fixed-slot'; self.expiry=2000006000
        self.bucket='original-accounting'; self.sha='b'*40
        self.issued={'producer':'private-native-bootstrap','candidateKey':self.candidate,'value':{
            'apiKeyId':'11111111-1111-1111-1111-111111111111','permissions':['admin:read','agent:workspace','agent:runtime-task:runtime']}}
        self.bootstrap={'producer':'private-native-bootstrap','candidateKey':self.candidate,'value':{'schema':'honua.private-native-bootstrap/v1'}}
        issued_ref={'key':'original/key-agent','sha256':canonical_digest(self.issued)}
        bootstrap_ref={'key':'original/completed','sha256':canonical_digest(self.bootstrap)}
        self.configuration={'region':'us-east-1','account_id':'123456789012','candidate_digest':self.candidate,'attempt_id':self.slot,
            'worker_image':'123456789012.dkr.ecr.us-east-1.amazonaws.com/worker@sha256:'+'c'*64,
            'installed_slot':{'runtime_id':self.runtime,'slot_id':self.slot,'original_funding_expires_epoch':self.expiry,'max_execution_seconds':6000},
            'mcp_configuration':{'secret_arn':'arn:aws:secretsmanager:us-east-1:123456789012:secret:honua-agent-fixed-slot-mcp-AbCdEf','version_id':'d'*64},
            'accounting':{'operation':{'liabilityBasisDigest':'sha256:'+'e'*64},'maximumUsd':{'infrastructure':'2.00','cleanup':'1.00'}}}
        cfg=self.configuration
        base_configuration={'region':'us-east-1','secretArn':cfg['mcp_configuration']['secret_arn'],'endpoint':'https://api.pilot.demo.honua.io/mcp',
            'workflowView':'workspace','runtimeId':self.runtime,'slotId':self.slot,'targetId':'actual-target',
            'nativeBootstrapOperationId':'original-bootstrap','originalFundingExpiresEpoch':self.expiry,
            'mutationCeilings':{'createMap':False,'createDashboard':False,'compose':False,'proposePublication':False,
                'proposeRestore':False,'datasetImportScopes':[],'managedProcessIds':[]}}
        prefix='installed-worker-mcp:'+'d'*64
        public={'configuration':base_configuration,'issuedAgentKey':issued_ref,'nativeBootstrap':bootstrap_ref,'versionId':'d'*64}
        intent={'producer':'private-installed-worker-mcp','candidateKey':self.candidate,'value':public}
        principal={'actorId':'actual-opaque-agent','studioOwnerId':self.issued['value']['apiKeyId'],'authenticationKind':'api-key','isHumanSession':False}
        self.records={issued_ref['key']:self.issued,bootstrap_ref['key']:self.bootstrap,prefix+'/intent':intent}
        originals={}
        for stage,path,body in (
            ('target','/api/v1/agent/target',{'targetId':'actual-target','principal':principal}),
            ('effective','/api/v1/admin/api-keys/'+principal['studioOwnerId']+'/effective-permissions',{'success':True,'data':{
                'id':principal['studioOwnerId'],'status':'active','canAuthenticate':True,'permissions':self.issued['value']['permissions']}})):
            raw=encoded(body); answer={'httpStatus':200,'body':body,'bodyBase64':base64.b64encode(raw).decode(),
                'bodyBytes':len(raw),'bodySha256':receipt.digest(raw)}
            row={'producer':'private-installed-worker-mcp','candidateKey':self.candidate,'value':{
                'endpoint':'https://api.pilot.demo.honua.io','path':path,'credentialKind':'api-key','issuedAgentKey':issued_ref,'response':answer}}
            key=prefix+'/observation/0/'+stage;self.records[key]=row;originals[stage]={'key':key,'sha256':canonical_digest(row)}
        pending={'schema':'honua.private-installed-worker-mcp-binding/v1','status':'registration-pending','candidateKey':self.candidate,
            'runtimeId':self.runtime,'slotId':self.slot,'targetId':'actual-target','originalFundingExpiresEpoch':self.expiry,
            'secretArn':cfg['mcp_configuration']['secret_arn'],'versionId':'d'*64,'activation':False,'qualification':False,
            'issuedAgentKey':issued_ref,'nativeBootstrap':bootstrap_ref,'principal':principal,'publicBindingDigest':canonical_digest(public),
            'targetReference':originals['target'],'effectiveReference':originals['effective']}
        row={'producer':'private-installed-worker-mcp','candidateKey':self.candidate,'value':pending}
        key=prefix+'/completed';self.records[key]=row
        cfg['mcp_bootstrap_binding']={'key':key,'sha256':canonical_digest(row)}
        source_path='certification/hosted-pilot/worker/deployments/unit.tfvars.json';self.config_raw=encoded(cfg)
        producer={'repository':receipt.REPOSITORY,'workflow':receipt.WORKFLOW,'sourceSha':self.sha,'runId':11,'runAttempt':2,
            'jobKey':'apply','ref':'refs/heads/trunk','event':'workflow_dispatch'}
        prefix='agent-e2e/'+self.candidate[7:]+'/installed-workers/runtime/fixed-slot/11/2/'
        self.objects={}
        def store(name,value):
            raw=encoded(value);key=prefix+name;self.objects[(key,'object-version')]=raw
            return {'bucket':self.bucket,'key':key,'versionId':'object-version','sha256':receipt.digest(raw)}
        review={'schema':'honua.hosted-pilot-saved-plan/v1','root_path':receipt.ROOT_PATH,'operation':'apply',
            'context':{'GITHUB_SHA':self.sha,'GITHUB_RUN_ID':'11','GITHUB_RUN_ATTEMPT':'1','GITHUB_REPOSITORY':receipt.REPOSITORY},
            'config_path':source_path,'config_sha256':receipt.digest(self.config_raw)[7:],'plan_sha256':'f'*64,'s3_version_id':'plan-version'}
        review_ref=store('review.json',review)
        readiness={'schema':'honua.hosted-worker-readiness/v1','source_sha':self.sha,'run_id':'11','attempt_id':self.slot,'mode':'installed-slot','qualification':False}
        readiness.update({k:True for k in ('registered_task_definition_verified','immutable_agent_mcp_binding_verified',
            'worker_role_queue_only','independent_expiry_schedule_verified','cleanup_monitor_confirmed')})
        ready_ref=store('readiness.json',readiness)
        self.descriptor={'schema':'honua.installed-worker-slot/v1','candidateKey':self.candidate,'runtimeId':self.runtime,'slotId':self.slot,
            'originalFundingExpiresEpoch':self.expiry,'image':cfg['worker_image'],'mcpSecret':{'secretArn':pending['secretArn'],'versionId':pending['versionId']},
            'liability':{'basisDigest':'sha256:'+'e'*64,'maximumInfrastructureUsd':'2.00','maximumCleanupUsd':'1.00'},
            'region':'us-east-1','stateMachineArn':'arn:aws:states:us-east-1:123456789012:stateMachine:honua-agent-fixed-slot',
            'stateMachineRoleArn':'arn:aws:iam::123456789012:role/honua-agent-fixed-slot-lifecycle','definitionDigest':canonical_digest({}),
            'clusterArn':'arn:aws:ecs:us-east-1:123456789012:cluster/honua-pilot-unit-agent',
            'taskDefinitionArn':'arn:aws:ecs:us-east-1:123456789012:task-definition/honua-agent-fixed-slot:7',
            'taskRoleArn':'arn:aws:iam::123456789012:role/honua-agent-fixed-slot-worker','claimTable':'original-claims',
            'maxExecutionSeconds':6000,'commandQueueUrl':'https://sqs.us-east-1.amazonaws.com/123456789012/fixed-slot-commands.fifo',
            'replyQueueUrl':'https://sqs.us-east-1.amazonaws.com/123456789012/fixed-slot-replies.fifo'}
        self.carrier={'schema':receipt.SCHEMA,'accountId':'123456789012','candidateKey':self.candidate,'runtimeId':self.runtime,'slotId':self.slot,
            'originalFundingExpiresEpoch':self.expiry,'producer':producer,'sourceConfiguration':{'path':source_path,'sha256':receipt.digest(self.config_raw)},
            'plan':{'sha256':'sha256:'+'f'*64,'versionId':'plan-version','attempt':1,'reviewSha256':review_ref['sha256'],'reviewArtifactId':33,'review':review_ref},
            'descriptor':self.descriptor,'mcpBootstrap':cfg['mcp_bootstrap_binding'],'readiness':ready_ref,'activation':False,'qualification':False}
        self.selected=store('installation.json',self.carrier)
        self.s3=MagicMock()
        self.s3.get_object.side_effect=lambda **kw:{'VersionId':kw['VersionId'],'ServerSideEncryption':'AES256','Body':io.BytesIO(self.objects[(kw['Key'],kw['VersionId'])])}
        self.durable=MagicMock();self.durable.store.bucket=self.bucket;self.durable.store.candidate=self.candidate
        self.durable.state={'ledger':{'interactiveAccounting':{'originalExpiresEpoch':self.expiry}},'scenarioOperations':self.records}
        self.run_status='completed';self.apply_conclusion='success';self.duplicate_job=False
        def github(path):
            if '/compare/' in path:return {'status':'ahead','merge_base_commit':{'sha':self.sha}}
            if '/contents/' in path:return {'encoding':'base64','content':base64.b64encode(self.config_raw).decode()}
            attempt=2 if '/attempts/2' in path else 1
            if path.endswith('/jobs?per_page=100'):
                job={'name':'apply' if attempt==2 else 'plan','run_id':11,'head_sha':self.sha,'status':'completed',
                    'conclusion':self.apply_conclusion if attempt==2 else 'success','steps':[{'name':name,'conclusion':'success'} for name in (
                        'Verify identity and apply exactly the saved plan','Inspect exact worker image, agent-only secret version and independent cleanup','Retain installed worker identity-only output')]}
                return {'total_count':2 if self.duplicate_job else 1,'jobs':[job,copy.deepcopy(job)] if self.duplicate_job else [job]}
            return {'id':11,'run_attempt':attempt,'head_sha':self.sha,'head_branch':'trunk','event':'workflow_dispatch','path':receipt.WORKFLOW,
                'repository':{'full_name':receipt.REPOSITORY},'status':self.run_status,'conclusion':'success'}
        self.github=github
        self.native=MagicMock()
        self.native.inspect.return_value={'lifecycle':{'stateMachineArn':'actual-machine','roleArn':'actual-lifecycle-role','definition':'{}'},
            'taskDefinition':{'taskDefinition':{'taskDefinitionArn':'actual-task:7','taskRoleArn':'actual-worker-role',
                'containerDefinitions':[{'image':cfg['worker_image']}]}}}
        self.install_type=MagicMock(return_value=self.native)
        self.states=MagicMock(); self.ecs=MagicMock()
        self.states.describe_state_machine.return_value={'stateMachineArn':self.descriptor['stateMachineArn'],
            'roleArn':self.descriptor['stateMachineRoleArn'],'status':'ACTIVE','type':'STANDARD','definition':'{}'}
        transport={'schemaVersion':2,'mode':'installed-slot',**{key:self.descriptor[key] for key in
            ('candidateKey','runtimeId','slotId','commandQueueUrl','replyQueueUrl','originalFundingExpiresEpoch','maxExecutionSeconds')}}
        self.ecs.describe_task_definition.return_value={'taskDefinition':{'taskDefinitionArn':self.descriptor['taskDefinitionArn'],
            'taskRoleArn':self.descriptor['taskRoleArn'],'status':'ACTIVE','containerDefinitions':[{'image':cfg['worker_image'],
                'environment':[{'name':'HONUA_AGENT_TRANSPORT','value':json.dumps(transport)}, {'name':'AWS_DEFAULT_REGION','value':'us-east-1'}],
                'secrets':[{'name':'HONUA_AGENT_MCP_CONFIG','valueFrom':self.descriptor['mcpSecret']['secretArn']+':::'+self.descriptor['mcpSecret']['versionId']}]}]}}

    def verify(self, *, real_inspector=False):
        inspection = nullcontext() if real_inspector else patch.dict(sys.modules,{'agent_e2e_installed_worker':SimpleNamespace(InstalledSlot=self.install_type)})
        with patch.object(receipt,'validate_retained_funding') as funding, inspection:
            answer=receipt.verify_installation(self.selected,durable=self.durable,runtime_id=self.runtime,target_id='actual-target',
                original_expires_epoch=self.expiry,s3=self.s3,ecs=self.ecs,states=self.states,github=self.github,guard=MagicMock())
            funding.assert_called_once_with(self.durable.state)
            return answer

    def replace_carrier(self):
        raw=encoded(self.carrier);self.objects[(self.selected['key'],self.selected['versionId'])]=raw;self.selected['sha256']=receipt.digest(raw)

    def test_actual_source_jobs_mcp_originals_and_native_pins_are_joined(self):
        answer=self.verify();self.native.inspect.assert_called_once()
        self.assertEqual(answer['descriptor'],self.descriptor)
        self.assertEqual(answer['proof']['nativeEndpoint'],'https://api.pilot.demo.honua.io')
        self.assertEqual(answer['issuedAgentKey'],self.carrier['mcpBootstrap'] and self.records[self.carrier['mcpBootstrap']['key']]['value']['issuedAgentKey'])
        self.assertFalse(answer['activation']);self.assertFalse(answer['qualification'])

    def test_real_installed_inspector_reads_actual_sdk_shapes_without_a_descriptor_mock(self):
        answer=self.verify(real_inspector=True)
        self.assertEqual(answer['proof']['native']['taskDefinitionArn'],self.descriptor['taskDefinitionArn'])
        self.ecs.describe_task_definition.assert_called_once_with(taskDefinition=self.descriptor['taskDefinitionArn'])
        self.states.describe_state_machine.assert_called_once_with(stateMachineArn=self.descriptor['stateMachineArn'])

    def test_proof_replays_terraform_definition_bytes_without_float_decoding(self):
        raw = '{"Comment":"<>&","precise":0.12345678901234567890123456789}'
        self.native.inspect.return_value['lifecycle']['definition'] = raw
        answer = self.verify()
        self.assertEqual(answer['proof']['native']['definitionDigest'], receipt.terraform_definition_digest(raw))
        self.assertNotEqual(answer['proof']['native']['definitionDigest'], canonical_digest(json.loads(raw)))

    def test_visible_output_before_job_completion_is_not_successful_apply(self):
        self.run_status='in_progress'
        with self.assertRaisesRegex(PackError,'not successfully completed'):self.verify()
        self.native.inspect.assert_not_called()

    def test_failed_apply_cannot_be_relabelled_by_successful_run(self):
        self.apply_conclusion='failure'
        with self.assertRaisesRegex(PackError,'job did not succeed'):self.verify()

    def test_duplicate_original_job_is_refused(self):
        self.duplicate_job=True
        with self.assertRaisesRegex(PackError,'job did not succeed'):self.verify()

    def test_rehashed_caller_source_bytes_cannot_replace_original_github_configuration(self):
        self.carrier['sourceConfiguration']['sha256']='sha256:'+'0'*64;self.replace_carrier()
        with self.assertRaises(PackError):self.verify()

    def test_unknown_receipt_fields_cannot_smuggle_caller_slots(self):
        self.carrier['slots']=[{'taskDefinitionArn':'invented'}];self.replace_carrier()
        with self.assertRaisesRegex(PackError,'producer shape'):self.verify()

    def test_original_expiry_cannot_be_renewed(self):
        self.durable.state['ledger']['interactiveAccounting']['originalExpiresEpoch']+=1
        with self.assertRaisesRegex(PackError,'original authenticated'):self.verify()

    def test_raw_native_original_bytes_are_replayed(self):
        base=self.records[self.configuration['mcp_bootstrap_binding']['key']]['value'];ref=base['targetReference']
        row=self.records[ref['key']];row['value']['response']['bodyBase64']=base64.b64encode(b'{}').decode()
        ref['sha256']=canonical_digest(row)
        self.configuration['mcp_bootstrap_binding']['sha256']=canonical_digest(self.records[self.configuration['mcp_bootstrap_binding']['key']])
        self.config_raw=encoded(self.configuration)
        self.carrier['sourceConfiguration']['sha256']=receipt.digest(self.config_raw)
        review_ref=self.carrier['plan']['review'];raw=self.objects[(review_ref['key'],review_ref['versionId'])]
        review=json.loads(raw);review['config_sha256']=receipt.digest(self.config_raw)[7:]
        raw=encoded(review);self.objects[(review_ref['key'],review_ref['versionId'])]=raw
        review_ref['sha256']=self.carrier['plan']['reviewSha256']=receipt.digest(raw)
        self.replace_carrier()
        with self.assertRaisesRegex(PackError,'original native body bytes differ'):self.verify()

    def test_missing_original_cleanup_observation_cannot_be_relabelled_readiness(self):
        ref=self.carrier['readiness']; original=json.loads(self.objects[(ref['key'],ref['versionId'])])
        original.pop('cleanup_monitor_confirmed')
        raw=encoded(original);self.objects[(ref['key'],ref['versionId'])]=raw;ref['sha256']=receipt.digest(raw)
        self.replace_carrier()
        with self.assertRaisesRegex(PackError,'original installed native readiness differs'):self.verify()
        self.native.inspect.assert_not_called()

    def test_native_definition_failure_cannot_be_masked_by_terraform_output(self):
        self.native.inspect.side_effect=PackError('actual native task differs')
        with self.assertRaisesRegex(PackError,'actual native task differs'):self.verify()


class ConditionalOutputRecovery(unittest.TestCase):
    def test_lost_write_ack_uses_one_put_then_actual_exact_get(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'identity.json';path.write_bytes(b'{"identity":"original"}')
            calls=[]
            def aws(*args):
                calls.append(args)
                if args[1]=='put-object':raise ValueError('acknowledgement lost')
                Path(args[-1]).write_bytes(path.read_bytes())
                return {'VersionId':'original-version','ServerSideEncryption':'AES256'}
            result=receipt.retained_put(path,bucket='original',key='original/identity.json',aws=aws)
            self.assertEqual([row[1] for row in calls],['put-object','get-object'])
            self.assertEqual(result['versionId'],'original-version')

    def test_lost_write_mismatched_original_is_never_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'identity.json';path.write_bytes(b'{"identity":"original"}')
            calls=[]
            def aws(*args):
                calls.append(args)
                if args[1]=='put-object':raise ValueError('acknowledgement lost')
                Path(args[-1]).write_bytes(b'{}')
                return {'VersionId':'different-version','ServerSideEncryption':'AES256'}
            with self.assertRaisesRegex(PackError,'write/readback differs'):
                receipt.retained_put(path,bucket='original',key='original/identity.json',aws=aws)
            self.assertEqual([row[1] for row in calls],['put-object','get-object'])


if __name__=='__main__':unittest.main()
