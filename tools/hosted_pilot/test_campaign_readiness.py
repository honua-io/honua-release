"""Initial staging observes real API shapes without fabricating paid handoffs."""
import copy
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import campaign_readiness as readiness
from agent_e2e_pack import canonical_digest
from controller_handoff import COMMAND as CONTROLLER_COMMAND
from controller_handoff import validate_configuration
from fixture_handoff import COMMAND as FIXTURE_COMMAND


class CampaignStaging(unittest.TestCase):
    def setUp(self):
        self.candidate = 'sha256:'+'a'*64
        self.account = '585192672263'
        self.prefix = 'honua-pilot-unit-agent'
        role = 'arn:aws:iam::'+self.account+':role/'+self.prefix
        ecs = 'arn:aws:ecs:us-east-1:'+self.account+':'
        image = {'uri':self.account+'.dkr.ecr.us-east-1.amazonaws.com/honua-pilot-unit-trusted-controller@sha256:'+'b'*64,
            'sourceSha':'c'*40,'treeId':'d'*40,'archiveSha256':'e'*64,'runtimeContractSha256':'sha256:'+'f'*64}
        self.config = {'schemaVersion':1,'region':'us-east-1','accountId':self.account,'clusterArn':ecs+'cluster/'+self.prefix+'-cluster',
            'taskDefinitionArn':ecs+'task-definition/'+self.prefix+'-campaign-controller:1','taskRoleArn':role+'-controller',
            'executionRoleArn':role+'-controller-execution','containerName':'campaign-controller','image':image,
            'network':{'subnetIds':['subnet-123abc'],'securityGroupIds':['sg-123abc']},
            'lifecycle':{'stateMachineArn':'arn:aws:states:us-east-1:'+self.account+':stateMachine:'+self.prefix+'-campaign-controller:1',
                'stateMachineRoleArn':role+'-controller-lifecycle','definitionDigest':None},
            'custody':{'bucket':self.prefix+'-controller-'+self.account,**{key:'controller/'+key+'/' for key in ('requestPrefix','sourcePrefix','bundlePrefix','outcomePrefix','evidencePrefix')}},'maxSeconds':300}
        definition = {'StartAt':'RunController','TimeoutSeconds':300,'States':{'RunController':{'Type':'Task','Resource':'arn:aws:states:::ecs:runTask.sync','End':True,
            'Parameters':{'Cluster':self.config['clusterArn'],'TaskDefinition':self.config['taskDefinitionArn'],'LaunchType':'FARGATE','Count':1,'EnableExecuteCommand':False,
                'NetworkConfiguration':{'AwsvpcConfiguration':{'Subnets':['subnet-123abc'],'SecurityGroups':['sg-123abc'],'AssignPublicIp':'DISABLED'}},
                'Overrides':{'ContainerOverrides':[{'Name':'campaign-controller','Environment':[{'Name':'PILOT_CONTROLLER_REQUEST','Value.$':'$.request'}]}]}}}}}
        self.config['lifecycle']['definitionDigest']=canonical_digest(definition)
        self.controller = {'taskDefinitionArn':self.config['taskDefinitionArn'],'taskRoleArn':self.config['taskRoleArn'],
            'executionRoleArn':self.config['executionRoleArn'],'networkMode':'awsvpc','runtimePlatform':{'cpuArchitecture':'X86_64','operatingSystemFamily':'LINUX'},
            'containerDefinitions':[{'name':'campaign-controller','image':image['uri'],'command':CONTROLLER_COMMAND,'readonlyRootFilesystem':True,'user':'10001:10001'}]}
        fixture = {'taskDefinitionArn':ecs+'task-definition/'+self.prefix+'-fixture:1','controllerRoleArn':role+'-fixture',
            'executionRoleArn':role+'-fixture-execution','containerName':'fixture-provisioner','imageUri':image['uri'],'provisionerConfigSha256':canonical_digest({'original':True})}
        self.fixture = {'taskDefinitionArn':fixture['taskDefinitionArn'],'taskRoleArn':fixture['controllerRoleArn'],
            'executionRoleArn':fixture['executionRoleArn'],'networkMode':'awsvpc','runtimePlatform':self.controller['runtimePlatform'],
            'containerDefinitions':[{'name':'fixture-provisioner','image':image['uri'],'command':FIXTURE_COMMAND,'readonlyRootFilesystem':True,'user':'10001:10001',
                'environment':[{'name':'PILOT_PROVISIONER_CONFIG','value':json.dumps({'original':True})}]}]}
        state_name='honua-agent-'+self.candidate[7:31]
        self.state={'region':'us-east-1','bucket':state_name+'-'+self.account,'table':state_name+'-state'}
        self.stage={'controller':self.config,'fixture':fixture,'dispatcherRoleArn':role+'-controller-dispatch',
            'claimTable':self.prefix+'-controller-dispatch-claims','state':self.state,'candidateKey':self.candidate}
        self.outputs={key:{'value':value} for key,value in {'controller_handoff':None,'fixture_handoff':None,
            'initialization_readiness':self.stage,'accounting_staging':{'dispatchEnabled':False},
            'target_readiness':{'service':self.prefix,'workerSecurityGroupIds':['sg-456def']},'fixture_staging':{'nativeAgentConfigured':False}}.items()}
        self.cfg={'region':'us-east-1','account_id':self.account,'name_prefix':self.prefix,'candidate_digest':self.candidate,
            'controller_image':image,'permissions_boundary_arn':'arn:aws:iam::'+self.account+':policy/original-boundary',
            'server_image':'ghcr.io/honua-io/honua-server@sha256:'+'9'*64,'dispatch_budget':None}
        self.sm,self.ecs,self.iam=MagicMock(),MagicMock(),MagicMock()
        self.sm.describe_state_machine.return_value={'type':'STANDARD','roleArn':self.config['lifecycle']['stateMachineRoleArn'],'definition':json.dumps(definition)}
        self.ecs.describe_task_definition.side_effect=lambda **k:{'taskDefinition':self.controller if k['taskDefinition']==self.config['taskDefinitionArn'] else self.fixture}
        self.iam.get_role.return_value={'Role':{'Arn':self.stage['dispatcherRoleArn'],'PermissionsBoundary':{'PermissionsBoundaryArn':self.cfg['permissions_boundary_arn']}}}
        self.iam.list_attached_role_policies.return_value={'AttachedPolicies':[],'IsTruncated':False}
        self.iam.list_role_policies.return_value={'PolicyNames':['original-dispatch'],'IsTruncated':False}
        self.policy={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':['ecs:DescribeTasks','dynamodb:UpdateItem'],'Resource':'arn:aws:dynamodb:us-east-1:'+self.account+':table/original-claims'}]}
        self.stage['dispatcherPolicyDigests']={'original-dispatch':canonical_digest(self.policy)}
        self.iam.get_role_policy.side_effect=lambda **k:{'RoleName':k['RoleName'],'PolicyName':k['PolicyName'],'PolicyDocument':self.policy}
        self.head,self.versions={},{}
        self.reads=[]
        self.addCleanup(patch.stopall)
        patch.dict(os.environ,{'GITHUB_SHA':'1'*40,'GITHUB_RUN_ID':'42'}).start()
        patch('campaign_readiness.NativeCLI',side_effect=lambda service:{'stepfunctions':self.sm,'ecs':self.ecs,'iam':self.iam}[service]).start()
        self.wait=patch('campaign_readiness.run').start()
        self.service=patch('campaign_readiness.observed_service',return_value=[{'source':'native-service-observation'}]).start()
        patch('campaign_readiness.aws',side_effect=self.aws).start()

    def aws(self,*args):
        self.reads.append(args)
        if args[:2]==('dynamodb','describe-table'):return {'Table':{'DeletionProtectionEnabled':True,'TableStatus':'ACTIVE'}}
        if args[:2]==('dynamodb','get-item'):return {'Item':self.head}
        if args[:2]==('s3api','get-bucket-versioning'):return {'Status':'Enabled'}
        if args[:2]==('s3api','list-object-versions'):return self.versions
        if args[:2]==('ec2','describe-security-groups'):return {'SecurityGroups':[{'IpPermissions':[]},{'IpPermissions':[]}]}
        raise AssertionError('unreviewed API effect '+args[0]+'/'+args[1])

    def test_controller_resource_digest_replays_raw_terraform_encoding(self):
        import controller_handoff as handoff
        raw = json.loads(self.sm.describe_state_machine.return_value['definition'])
        raw['Comment'] = '<>&'
        text = json.dumps(raw)
        self.sm.describe_state_machine.return_value['definition'] = text
        self.config['lifecycle']['definitionDigest'] = handoff.terraform_definition_digest(text)
        result = readiness.inspect(self.outputs, self.cfg)
        self.assertTrue(result['native_controller_lifecycle_verified'])
        self.config['lifecycle']['definitionDigest'] = canonical_digest(raw)
        with self.assertRaisesRegex(ValueError, 'lifecycle source/identity'):
            readiness.inspect(self.outputs, self.cfg)

    def test_null_handoffs_still_verify_actual_controller_fixture_target_and_custody(self):
        result=readiness.inspect(self.outputs,self.cfg)
        self.assertFalse(result['qualification'])
        self.assertFalse(result['dispatch_enabled'])
        self.assertEqual(result['accounting']['status'],'no-head-or-object-history-observed')
        self.assertFalse(result['accounting']['initializationPerformed'])
        self.assertTrue(result['native_controller_lifecycle_verified'])
        self.assertEqual(self.ecs.describe_task_definition.call_count,2)
        self.service.assert_called_once_with(self.config['clusterArn'],self.prefix,{'honua':self.cfg['server_image']})
        self.assertTrue(any(row[:2]==('dynamodb','get-item') and '--consistent-read' in row for row in self.reads))

    def test_head_or_deleted_object_history_never_becomes_fresh_initialization(self):
        for kind in ('head','version','deleted','truncated'):
            self.head={'leaseOwner':{'S':'original'}} if kind=='head' else {}
            self.versions={'version':{'Versions':[{'VersionId':'original'}]},'deleted':{'DeleteMarkers':[{'VersionId':'original'}]},'truncated':{'IsTruncated':True}}.get(kind,{})
            with self.subTest(kind=kind):
                result=readiness.inspect(self.outputs,self.cfg)
                self.assertTrue(result['accounting']['historyPresent'])
                self.assertEqual(result['accounting']['status'],'existing-or-partial-history-reconciliation-required')

    def test_any_unreviewed_grant_including_policy_mutation_blocks_staging(self):
        for action in ('ecs:RunTask','states:StartExecution','iam:PassRole','bedrock:InvokeModel','iam:PutRolePolicy','*','ecs:*'):
            self.policy['Statement']=[{'Effect':'Allow','Action':action,'Resource':'*'}]
            with self.subTest(action=action),self.assertRaisesRegex(ValueError,'differs from reviewed Terraform'):
                readiness.inspect(self.outputs,self.cfg)
        self.policy['Statement']=[{'Effect':'Allow','NotAction':'s3:GetObject','Resource':'*'}]
        with self.assertRaisesRegex(ValueError,'differs from reviewed Terraform'):readiness.inspect(self.outputs,self.cfg)

    def test_resource_condition_and_effect_drift_or_extra_policy_refuse(self):
        original=copy.deepcopy(self.policy)
        for field,value in (('Resource','*'),('Condition',{'StringEquals':{'unreviewed':'value'}}),('Effect','Deny')):
            self.policy=copy.deepcopy(original)
            self.policy['Statement'][0][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'differs from reviewed Terraform'):
                readiness.inspect(self.outputs,self.cfg)
        self.policy=original
        for names in (['original-dispatch','extra'],['replaced'],[]):
            self.iam.list_role_policies.return_value={'PolicyNames':names,'IsTruncated':False}
            with self.subTest(names=names),self.assertRaisesRegex(ValueError,'policy set differs|inventory is incomplete'):
                readiness.inspect(self.outputs,self.cfg)

    def test_attached_policy_or_incomplete_policy_inventory_refuses(self):
        self.iam.list_attached_role_policies.return_value={'AttachedPolicies':[{'PolicyArn':'foreign'}]}
        with self.assertRaisesRegex(ValueError,'additional managed'):readiness.inspect(self.outputs,self.cfg)
        self.iam.list_attached_role_policies.return_value={'AttachedPolicies':[]}
        self.iam.list_role_policies.return_value={'PolicyNames':['readonly'],'IsTruncated':True}
        with self.assertRaisesRegex(ValueError,'inventory is incomplete'):readiness.inspect(self.outputs,self.cfg)

    def test_stale_controller_or_fixture_isolation_is_not_skipped(self):
        for task in (self.controller,self.fixture):
            task['containerDefinitions'][0]['readonlyRootFilesystem']=False
            with self.assertRaisesRegex(ValueError,'isolation'):readiness.inspect(self.outputs,self.cfg)
            task['containerDefinitions'][0]['readonlyRootFilesystem']=True

    def test_unfunded_nonnull_handoff_and_substituted_state_refuse(self):
        self.outputs['controller_handoff']['value']=self.config
        with self.assertRaisesRegex(ValueError,'executable handoff'):readiness.inspect(self.outputs,self.cfg)
        self.outputs['controller_handoff']['value']=None
        self.stage['state']=dict(self.state,table='fresh-unreviewed-state')
        with self.assertRaisesRegex(ValueError,'original candidate'):readiness.inspect(self.outputs,self.cfg)

    def test_readonly_descriptor_never_becomes_an_executable_dispatch_configuration(self):
        with self.assertRaisesRegex(ValueError,'invalid reviewed private controller'):
            validate_configuration(self.config)

    def test_funded_stage_retains_strict_dispatch_configuration_and_native_checks(self):
        budget={'state':self.state,'candidateKey':self.candidate,'generationDigest':'sha256:'+'2'*64,
            'reservationId':'original-infra','operationId':'original-bootstrap','basisDigest':'sha256:'+'3'*64,
            'perDispatchCeilingUsd':'1','maximumDispatches':4,'claimTable':self.stage['claimTable']}
        self.cfg['dispatch_budget']=budget
        self.outputs['controller_handoff']['value']=self.config | {'dispatchBudget':budget}
        self.outputs['fixture_handoff']['value']=self.stage['fixture']
        result=readiness.inspect(self.outputs,self.cfg)
        self.assertFalse(result['qualification'])
        self.assertNotIn('accounting',result)
        self.assertTrue(result['native_controller_lifecycle_verified'])
        self.iam.get_role.assert_not_called()
