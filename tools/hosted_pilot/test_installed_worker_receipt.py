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
import test_worker_runtime_publication as publication_fixtures
from agent_e2e_pack import canonical_digest, PackError
from fixture_handoff import encoded


class InstalledOutputOriginals(unittest.TestCase):
    def setUp(self):
        self.publication = publication_fixtures.WorkerRuntimePublication()
        self.publication.setUp()
        self.candidate='sha256:'+'a'*64; self.runtime='runtime'; self.slot='fixed-slot'; self.expiry=2000006000
        self.bucket='original-accounting'; self.sha='b'*40
        self.issued={'producer':'private-native-bootstrap','candidateKey':self.candidate,'value':{
            'apiKeyId':'11111111-1111-1111-1111-111111111111','permissions':['admin:read','agent:workspace','agent:runtime-task:runtime']}}
        self.bootstrap={'producer':'private-native-bootstrap','candidateKey':self.candidate,'value':{'schema':'honua.private-native-bootstrap/v1'}}
        issued_ref={'key':'original/key-agent','sha256':canonical_digest(self.issued)}
        bootstrap_ref={'key':'original/completed','sha256':canonical_digest(self.bootstrap)}
        self.configuration={'region':'us-east-1','account_id':'585192672263','candidate_digest':self.candidate,'attempt_id':self.slot,
            'worker_image':self.publication.image,
            'permissions_boundary_arn':getattr(self,'source_boundary','arn:aws:iam::585192672263:policy/original-worker-boundary'),
            'installed_slot':{'runtime_id':self.runtime,'slot_id':self.slot,'original_funding_expires_epoch':self.expiry,'max_execution_seconds':6000},
            'mcp_configuration':{'secret_arn':'arn:aws:secretsmanager:us-east-1:585192672263:secret:honua-agent-fixed-slot-mcp-AbCdEf','version_id':'d'*64},
            'accounting':{'state':{'region':'us-east-1','bucket':self.bucket,'table':'original-head'},
                'operation':{'liabilityBasisDigest':'sha256:'+'e'*64},'maximumUsd':{'infrastructure':'90.00','cleanup':'70.00'}}}
        self.configuration['execution_liability']={'basisDigest':'sha256:'+'6'*64,'maximumInfrastructureUsd':'2.00','maximumCleanupUsd':'1.00'}
        self.configuration['worker_publication']=self.publication.binding
        self.configuration['public_bundle_digest']=self.publication.public
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
            'liability':self.configuration['execution_liability'],
            'region':'us-east-1','stateMachineArn':'arn:aws:states:us-east-1:585192672263:stateMachine:honua-agent-fixed-slot',
            'stateMachineRoleArn':'arn:aws:iam::585192672263:role/honua-agent-fixed-slot-lifecycle','definitionDigest':canonical_digest({}),
            'clusterArn':'arn:aws:ecs:us-east-1:585192672263:cluster/honua-pilot-unit-agent',
            'taskDefinitionArn':'arn:aws:ecs:us-east-1:585192672263:task-definition/honua-agent-fixed-slot:7',
            'taskRoleArn':'arn:aws:iam::585192672263:role/honua-agent-fixed-slot-worker','claimTable':'original-claims',
            'maxExecutionSeconds':6000,'commandQueueUrl':'https://sqs.us-east-1.amazonaws.com/585192672263/fixed-slot-commands.fifo',
            'replyQueueUrl':'https://sqs.us-east-1.amazonaws.com/585192672263/fixed-slot-replies.fifo'}
        self.carrier={'schema':receipt.SCHEMA,'accountId':'585192672263','candidateKey':self.candidate,'runtimeId':self.runtime,'slotId':self.slot,
            'originalFundingExpiresEpoch':self.expiry,'producer':producer,'sourceConfiguration':{'path':source_path,'sha256':receipt.digest(self.config_raw)},
            'plan':{'sha256':'sha256:'+'f'*64,'versionId':'plan-version','attempt':1,'reviewSha256':review_ref['sha256'],'reviewArtifactId':33,'review':review_ref},
            'descriptor':self.descriptor,'mcpBootstrap':cfg['mcp_bootstrap_binding'],'readiness':ready_ref,'activation':False,'qualification':False}
        pub_refs={}
        for field,name,body in (('publication','worker-publication.zip',self.publication.archive),('indexReference','runtime-index.json',self.publication.index_raw)):
            key=prefix+name;self.objects[(key,'object-version')]=body
            pub_refs[field]={'bucket':self.bucket,'key':key,'versionId':'object-version','sha256':receipt.digest(body)}
        self.carrier['workerPublication']={'binding':cfg['worker_publication'],**pub_refs}
        self.selected=store('installation.json',self.carrier)
        self.s3=MagicMock()
        self.s3.get_object.side_effect=lambda **kw:{'VersionId':kw['VersionId'],'ServerSideEncryption':'AES256','Body':io.BytesIO(self.objects[(kw['Key'],kw['VersionId'])])}
        self.durable=MagicMock();self.durable.store.bucket=self.bucket;self.durable.store.candidate=self.candidate
        self.durable.state={'ledger':{'interactiveAccounting':{'originalExpiresEpoch':self.expiry}},'scenarioOperations':self.records}
        self.run_status='completed';self.apply_conclusion='success';self.duplicate_job=False
        def github(path):
            if (('/attempts/2' in path and '/runs/42/' in path) or '/actions/artifacts/99' in path or
                path.endswith('/branches/trunk') or '/compare/'+self.publication.source+'...' in path):
                return self.publication.github(path)
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

    def emit_original(self, *, corrupt_index=False, lose_zip_ack=False):
        import worker_allowance
        review_ref=self.carrier['plan']['review'];ready_ref=self.carrier['readiness']
        review_raw=self.objects[(review_ref['key'],review_ref['versionId'])]
        readiness_raw=self.objects[(ready_ref['key'],ready_ref['versionId'])]
        self.objects={};stored={};calls=[]
        def aws(*args):
            calls.append(args)
            key=args[args.index('--key')+1]
            if args[:2]==('s3api','put-object'):
                self.assertEqual(args[args.index('--if-none-match')+1],'*')
                self.assertEqual(args[args.index('--server-side-encryption')+1],'AES256')
                if key in stored:raise ValueError('already retained')
                raw=Path(args[args.index('--body')+1]).read_bytes()
                stored[key]=raw;self.objects[(key,'emitted-version')]=raw
                if lose_zip_ack and key.endswith('/worker-publication.zip'):raise ValueError('write reply lost')
                return {'VersionId':'emitted-version'}
            self.assertEqual(args[:2],('s3api','get-object'))
            Path(args[-1]).write_bytes(stored[key])
            return {'VersionId':'emitted-version','ServerSideEncryption':'AES256'}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);work=root/'private';work.mkdir(mode=0o700)
            cfg=root/self.carrier['sourceConfiguration']['path'];cfg.parent.mkdir(parents=True)
            cfg.write_bytes(self.config_raw)
            (work/'review.json').write_bytes(review_raw);(work/'readiness.json').write_bytes(readiness_raw)
            # Execute the checked installer used by deploy.py; do not inject a
            # capability/index proof directly into the consumer's return value.
            import worker_publication_installation
            worker_publication_installation.prepare(self.configuration,work,guard=MagicMock(),
                github=self.publication.github,download=lambda _,guard:self.publication.archive)
            if corrupt_index:
                (work/'runtime-index.json').write_bytes(b'{}')
            env={'GITHUB_SHA':self.sha,'GITHUB_RUN_ID':'11','GITHUB_RUN_ATTEMPT':'2','GITHUB_REF':'refs/heads/trunk',
                'GITHUB_EVENT_NAME':'workflow_dispatch','PILOT_PLAN_ATTEMPT':'1','PILOT_REVIEW_SHA256':review_ref['sha256'][7:],
                'PILOT_REVIEW_ARTIFACT_ID':'33'}
            with patch.object(receipt,'original_review'), patch.object(receipt,'validate_retained_funding'), \
                    patch.object(worker_allowance,'verify',return_value={'payload':self.durable.state}):
                try:
                    selected=receipt.emit(cfg=cfg,root=root,outputs={'installed_worker_slot':{'value':self.descriptor}},
                        review=json.loads(review_raw),work=work,env=env,aws=aws,github=self.publication.github)
                except PackError:
                    self.assertEqual(calls,[],'invalid publication must fail before any output retention')
                    raise
        return selected,stored,calls

    def test_emitter_retains_exact_zip_index_then_real_verifier_replays_them(self):
        self.selected,stored,_=self.emit_original()
        value=json.loads(stored[self.selected['key']])
        self.assertEqual(stored[value['workerPublication']['publication']['key']],self.publication.archive)
        self.assertEqual(stored[value['workerPublication']['indexReference']['key']],self.publication.index_raw)
        self.assertEqual({key.rsplit('/',1)[1] for key in stored},
            {'installation.json','review.json','readiness.json','worker-publication.zip','runtime-index.json'})
        verified=self.verify(real_inspector=True)
        from agent_e2e_runtime_configuration import validate_worker_runtime
        validate_worker_runtime(verified)
        self.assertEqual(verified['proof']['runtime']['index'],self.publication.index)
        self.assertEqual(verified['descriptor']['liability'],self.configuration['execution_liability'])

    def test_emitter_invalid_index_refuses_before_any_retention_write(self):
        with self.assertRaisesRegex(PackError,'ZIP member'):self.emit_original(corrupt_index=True)

    def test_emitter_lost_zip_write_reply_recovers_exact_original_without_rewrite(self):
        self.selected,stored,calls=self.emit_original(lose_zip_ack=True)
        writes=[row for row in calls if row[:2]==('s3api','put-object') and
            row[row.index('--key')+1].endswith('/worker-publication.zip')]
        self.assertEqual(len(writes),1)
        self.assertEqual(self.verify()['proof']['runtime']['publication']['versionId'],'emitted-version')

    def test_unprotected_installation_source_refuses_before_native_inspection(self):
        self.publication.branch['protected']=False
        with self.assertRaisesRegex(PackError,'observed protected trunk'):self.verify()
        self.native.inspect.assert_not_called()

    def test_publication_index_byte_tamper_refuses_before_native_calls(self):
        ref=self.carrier['workerPublication']['indexReference']
        self.objects[(ref['key'],ref['versionId'])]=b'{}'
        ref['sha256']=receipt.digest(b'{}');self.replace_carrier()
        with self.assertRaisesRegex(PackError,'ZIP member'):self.verify()
        self.native.inspect.assert_not_called()

    def test_historical_output_without_authentic_runtime_proof_refuses(self):
        del self.carrier['workerPublication'];self.replace_carrier()
        with self.assertRaisesRegex(PackError,'producer shape'):self.verify()
        self.native.inspect.assert_not_called()

    def test_runtime_originals_cannot_escape_producing_installation(self):
        ref=self.carrier['workerPublication']['publication'];ref['key']='another-run/publication.zip'
        self.replace_carrier()
        with self.assertRaisesRegex(PackError,'output custody'):self.verify()
        self.native.inspect.assert_not_called()

    def test_shared_installation_hold_cannot_be_copied_into_each_execution(self):
        self.carrier['descriptor']['liability']={'basisDigest':self.configuration['accounting']['operation']['liabilityBasisDigest'],
            'maximumInfrastructureUsd':self.configuration['accounting']['maximumUsd']['infrastructure'],
            'maximumCleanupUsd':self.configuration['accounting']['maximumUsd']['cleanup']}
        self.replace_carrier()
        with self.assertRaisesRegex(PackError,'descriptor/source/quote differs'):self.verify()
        self.native.inspect.assert_not_called()

    def test_actual_source_jobs_mcp_originals_and_native_pins_are_joined(self):
        answer=self.verify();self.native.inspect.assert_called_once()
        self.assertEqual(answer['descriptor'],self.descriptor)
        self.assertEqual(answer['proof']['nativeEndpoint'],'https://api.pilot.demo.honua.io')
        self.assertEqual(answer['issuedAgentKey'],self.carrier['mcpBootstrap'] and self.records[self.carrier['mcpBootstrap']['key']]['value']['issuedAgentKey'])
        self.assertEqual(answer['proof']['runtime']['index'],self.publication.index)
        self.assertEqual(answer['proof']['runtime']['publication'],self.carrier['workerPublication']['publication'])
        self.assertFalse(answer['activation']);self.assertFalse(answer['qualification'])

    def test_real_installed_inspector_reads_actual_sdk_shapes_without_a_descriptor_mock(self):
        answer=self.verify(real_inspector=True)
        self.assertEqual(answer['proof']['native']['taskDefinitionArn'],self.descriptor['taskDefinitionArn'])
        self.assertEqual(answer['proof']['native']['permissionsBoundaryArn'],self.configuration['permissions_boundary_arn'])
        self.ecs.describe_task_definition.assert_called_once_with(taskDefinition=self.descriptor['taskDefinitionArn'])
        self.states.describe_state_machine.assert_called_once_with(stateMachineArn=self.descriptor['stateMachineArn'])

    def test_foreign_authenticated_source_boundary_is_refused_before_native_inspection(self):
        other=type(self)()
        other.source_boundary='arn:aws:iam::999999999999:policy/foreign'
        self.addCleanup(other.doCleanups);other.setUp()
        with self.assertRaisesRegex(PackError,'source permissions boundary is absent or foreign'):
            other.verify()
        other.native.inspect.assert_not_called()

    def test_proof_replays_terraform_definition_bytes_without_float_decoding(self):
        raw = '{"Comment":"<>&","precise":0.58519267226334567890123456789}'
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
