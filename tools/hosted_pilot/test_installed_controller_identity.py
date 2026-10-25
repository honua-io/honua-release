"""Actual SDK-shape role/policy/network drift controls; synthetic accounts only."""
import copy
import base64
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import installed_controller_identity as subject
from installed_controller_identity import observe_identity
from fixture_handoff import encoded


class CurrentControllerIdentity(unittest.TestCase):
    def setUp(self):
        self.guard=MagicMock()
        trust={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Principal':{'Service':'ecs-tasks.amazonaws.com'},
            'Action':'sts:AssumeRole','Condition':{'StringEquals':{'aws:SourceAccount':'123456789012'},
            'ArnLike':{'aws:SourceArn':'arn:aws:ecs:us-east-1:123456789012:*'}}}]}
        self.identity={'accountId':'123456789012','vpcId':'vpc-0123456789abcdef0','networkCidr':'10.90.0.0/16',
            'securityGroupIds':['sg-0123456789abcdef0'],'taskRoleArn':'arn:aws:iam::123456789012:role/poller',
            'executionRoleArn':'arn:aws:iam::123456789012:role/poller-execution',
            'permissionsBoundaryArn':'arn:aws:iam::123456789012:policy/boundary','assumeRolePolicy':trust,
            'policies':{'task':{'runtime':{'Version':'2012-10-17','Statement':[{'Effect':'Allow',
                'Action':'dynamodb:GetItem','Resource':'arn:aws:dynamodb:us-east-1:123456789012:table/original'}]}},
                'execution':{'pull':{'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'ecr:GetAuthorizationToken','Resource':'*'}]}}}}
        self.identity.update(schema='honua.installed-controller-identity-observation/v1',candidateKey='sha256:'+'a'*64,
            runtimeId='runtime',region='us-east-1',clusterArn='arn:aws:ecs:us-east-1:123456789012:cluster/fixture',
            subnetIds=['subnet-0123456789abcdef0'],originalFundingExpiresEpoch=2000000000,activation=False,qualification=False)
        self.iam=MagicMock()
        self.roles={}
        for name in ('task','execution'):
            arn=self.identity[name+'RoleArn'];role_name=arn.rsplit('/',1)[-1]
            self.roles[role_name]={'Arn':arn,'AssumeRolePolicyDocument':copy.deepcopy(trust),
                'PermissionsBoundary':{'PermissionsBoundaryType':'Policy','PermissionsBoundaryArn':self.identity['permissionsBoundaryArn']}}
        self.actual=copy.deepcopy(self.identity['policies'])
        self.iam.get_role.side_effect=lambda **kw:{'Role':self.roles[kw['RoleName']]}
        self.iam.list_attached_role_policies.return_value={'IsTruncated':False,'AttachedPolicies':[]}
        def group(role_name):return 'execution' if role_name.endswith('-execution') else 'task'
        self.iam.list_role_policies.side_effect=lambda **kw:{'IsTruncated':False,'PolicyNames':list(self.actual[group(kw['RoleName'])])}
        self.iam.get_role_policy.side_effect=lambda **kw:{**kw,'PolicyDocument':self.actual[group(kw['RoleName'])][kw['PolicyName']]}
        self.group={'GroupId':self.identity['securityGroupIds'][0],'VpcId':self.identity['vpcId'],'OwnerId':'123456789012',
            'IpPermissions':[],'IpPermissionsEgress':[{'IpProtocol':proto,'FromPort':port,'ToPort':port,
            'IpRanges':[{'CidrIp':cidr}],'Ipv6Ranges':[],'UserIdGroupPairs':[],'PrefixListIds':[]}
            for proto,port,cidr in [('tcp',443,'0.0.0.0/0'),('tcp',53,'10.90.0.0/16'),('udp',53,'10.90.0.0/16')]]}
        self.ec2=MagicMock();self.ec2.describe_security_groups.side_effect=lambda **kw:{'SecurityGroups':[self.group]}
        self.subnets=[{'SubnetId':self.identity['subnetIds'][0],'VpcId':self.identity['vpcId'],'OwnerId':'123456789012','State':'available'}]
        self.ec2.describe_subnets.side_effect=lambda **kw:{'Subnets':copy.deepcopy(self.subnets)}

    def observe(self):return observe_identity(self.identity,iam=self.iam,ec2=self.ec2,guard=self.guard)

    def test_exact_sdk_originals_include_complete_policies_boundaries_trust_and_network(self):
        result=self.observe()
        self.assertEqual(result['task']['policies'],self.identity['policies']['task'])
        self.assertEqual(result['execution']['trust'],self.identity['assumeRolePolicy'])
        self.ec2.describe_security_groups.assert_called_once_with(GroupIds=self.identity['securityGroupIds'])
        self.ec2.describe_subnets.assert_called_once_with(SubnetIds=self.identity['subnetIds'])
        self.assertEqual(result['network']['subnetIds'],self.identity['subnetIds'])
        self.assertGreaterEqual(self.guard.call_count,14)
        self.assertEqual({call[0] for call in self.iam.mock_calls},
            {'get_role','list_attached_role_policies','list_role_policies','get_role_policy'})

    def test_extra_receipt_fields_cannot_carry_private_data(self):
        self.identity['privateToken']='must-never-export'
        with self.assertRaisesRegex(ValueError,'closed nonsecret'):self.observe()
        self.iam.get_role.assert_not_called()

    def test_extra_inline_policy_is_not_ignored(self):
        self.actual['task']['extra']={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'iam:PutRolePolicy','Resource':'*'}]}
        with self.assertRaisesRegex(ValueError,'complete current policy map'):self.observe()

    def test_attached_managed_policy_is_not_a_readiness_shortcut(self):
        self.iam.list_attached_role_policies.return_value={'IsTruncated':False,'AttachedPolicies':[{'PolicyArn':'arn:aws:iam::aws:policy/AdministratorAccess'}]}
        with self.assertRaisesRegex(ValueError,'attached, truncated'):self.observe()

    def test_truncated_inline_inventory_refuses_before_policy_reads(self):
        self.iam.list_role_policies.side_effect=lambda **kw:{'IsTruncated':True,'Marker':'next','PolicyNames':['runtime']}
        with self.assertRaisesRegex(ValueError,'attached, truncated'):self.observe()
        self.iam.get_role_policy.assert_not_called()

    def test_removed_permissions_boundary_refuses(self):
        self.roles['poller'].pop('PermissionsBoundary')
        with self.assertRaisesRegex(ValueError,'role/trust/boundary'):self.observe()

    def test_foreign_trust_cannot_become_original_authority(self):
        self.roles['poller']['AssumeRolePolicyDocument']['Statement'][0]['Principal']={'AWS':'*'}
        with self.assertRaisesRegex(ValueError,'reviewed ECS account-bound trust'):self.observe()

    def test_sql_egress_cannot_replace_private_dns(self):
        self.group['IpPermissionsEgress'][1].update(FromPort=5432,ToPort=5432)
        with self.assertRaisesRegex(ValueError,'HTTPS/private DNS'):self.observe()

    def test_foreign_vpc_is_not_accepted_from_matching_sg_id(self):
        self.group['VpcId']='vpc-fffffffffffffffff'
        with self.assertRaisesRegex(ValueError,'identity or ingress'):self.observe()

    def test_subnets_are_observed_live_in_the_reviewed_vpc_and_account(self):
        for flip in ({'VpcId':'vpc-fffffffffffffffff'},{'OwnerId':'999999999999'},{'State':'pending'},
                     {'SubnetId':'subnet-fffffffffffffffff'}):
            with self.subTest(flip=flip):
                self.subnets=[{**self.subnets[0],**flip}]
                with self.assertRaisesRegex(ValueError,'reviewed VPC/account subnets'):self.observe()
        self.subnets=[]
        with self.assertRaisesRegex(ValueError,'reviewed VPC/account subnets'):self.observe()
        self.identity['subnetIds']=['subnet-0123456789abcdef0','subnet-0123456789abcdef0']
        with self.assertRaisesRegex(ValueError,'exact reviewed subnets'):self.observe()
        self.assertEqual(self.ec2.describe_subnets.call_count,5)


class OriginalControllerIdentity(unittest.TestCase):
    """Synthetic original workflow/SDK bytes, never an applied identity receipt."""
    def setUp(self):
        CurrentControllerIdentity.setUp(self)
        self.sha='a'*40;self.head='b'*40;self.expiry=2000000000
        self.prefix='honua-pilot-unit-agent';self.bucket='original-accounting'
        boundary='arn:aws:iam::123456789012:policy/honua-release-pilot-unit-foundation-installed-controller-boundary'
        self.identity['permissionsBoundaryArn']=boundary
        self.roles={}
        for kind in ('task','execution'):
            arn='arn:aws:iam::123456789012:role/'+self.prefix+'-installed-controller'+('-execution' if kind=='execution' else '')
            self.identity[kind+'RoleArn']=arn
            self.roles[arn.rsplit('/',1)[-1]]={'Arn':arn,'AssumeRolePolicyDocument':copy.deepcopy(self.identity['assumeRolePolicy']),
                'PermissionsBoundary':{'PermissionsBoundaryType':'Policy','PermissionsBoundaryArn':boundary}}
        self.configuration={'candidate_digest':self.identity['candidateKey'],'region':'us-east-1','account_id':'123456789012',
            'name_prefix':self.prefix,'network_cidr':self.identity['networkCidr'],
            'permissions_boundary_arn':'arn:aws:iam::123456789012:policy/legacy-cap',
            'installed_controller_identity':{'runtimeId':'runtime','originalFundingExpiresEpoch':self.expiry,
                'permissionsBoundaryArn':boundary}}
        self.path='certification/hosted-pilot/campaign/deployments/unit.tfvars.json'
        self.producer={'repository':subject.REPOSITORY,'workflow':subject.WORKFLOW,'sourceSha':self.sha,
            'runId':11,'runAttempt':2,'jobKey':'apply','ref':'refs/heads/trunk','event':'workflow_dispatch'}
        self.review={'schema':'honua.hosted-pilot-saved-plan/v1','operation':'apply','root_path':subject.ROOT_PATH,
            'context':{'GITHUB_REPOSITORY':subject.REPOSITORY,'GITHUB_SHA':self.sha,'GITHUB_RUN_ID':'11','GITHUB_RUN_ATTEMPT':'1'},
            'config_path':self.path,'config_sha256':subject.digest(encoded(self.configuration))[7:],
            'plan_sha256':'c'*64,'s3_version_id':'original-plan-version'}
        self.base=subject.prefix(self.identity['candidateKey'],'runtime',11,2)
        self.objects={}
        self.review_ref=self.store('review.json',self.review)
        self.carrier={'schema':subject.SCHEMA,'candidateKey':self.identity['candidateKey'],'runtimeId':'runtime',
            'originalFundingExpiresEpoch':self.expiry,'producer':self.producer,
            'sourceConfiguration':{'path':self.path,'sha256':subject.digest(encoded(self.configuration))},
            'plan':{'review':self.review_ref,'sha256':'sha256:'+'c'*64,'versionId':'original-plan-version','attempt':1},
            'identity':self.identity,'originals':observe_identity(self.identity,iam=self.iam,ec2=self.ec2,guard=self.guard),
            'activation':False,'qualification':False}
        self.selected=self.store('identity.json',self.carrier)
        self.durable=SimpleNamespace(store=SimpleNamespace(bucket=self.bucket,candidate=self.identity['candidateKey']),
            state={'ledger':{'interactiveAccounting':{'originalExpiresEpoch':self.expiry}}})
        self.s3=MagicMock()
        self.s3.get_object.side_effect=lambda **kw:{'VersionId':kw['VersionId'],'ServerSideEncryption':'AES256',
            'Body':io.BytesIO(self.objects[(kw['Key'],kw['VersionId'])])}
        self.protected=True;self.branch_name='trunk';self.status='completed';self.apply='success';self.calls=[]
        self.compare_status='ahead';self.duplicate_step=False;self.now=self.expiry-1
        self.github=MagicMock(side_effect=self.github_reply)

    def store(self,name,value):
        raw=encoded(value);key=self.base+name;self.objects[(key,'original-version')]=raw
        return {'bucket':self.bucket,'key':key,'versionId':'original-version','sha256':subject.digest(raw)}

    def github_reply(self,path):
        self.calls.append(path)
        if path.endswith('/branches/trunk'):
            return {'name':self.branch_name,'protected':self.protected,'commit':{'sha':self.head}}
        if '/compare/' in path:
            self.assertEqual(path,'repos/'+subject.REPOSITORY+'/compare/'+self.sha+'...'+self.head)
            return {'status':self.compare_status,'merge_base_commit':{'sha':self.sha}}
        if '/contents/' in path:
            self.assertEqual(path,'repos/'+subject.REPOSITORY+'/contents/'+self.path+'?ref='+self.sha)
            return {'encoding':'base64','content':base64.b64encode(encoded(self.configuration)).decode()}
        attempt=2 if '/attempts/2' in path else 1
        if path.endswith('/jobs?per_page=100'):
            return {'total_count':1,'jobs':[{'name':'apply' if attempt==2 else 'plan','run_id':11,'head_sha':self.sha,
                'status':self.status,'conclusion':self.apply if attempt==2 else 'success','steps':[
                    {'name':name,'conclusion':'success'} for name in ('Verify identity and apply exactly the saved plan',
                    'Retain installed controller identity-only output')]*(2 if self.duplicate_step else 1)}]}
        return {'id':11,'run_attempt':attempt,'head_sha':self.sha,'head_branch':'trunk','event':'workflow_dispatch',
            'path':subject.WORKFLOW,'repository':{'full_name':subject.REPOSITORY},'status':self.status,'conclusion':self.apply}

    def verify(self):
        with patch.object(subject,'validate_retained_funding') as funding:
            value=subject.verify_controller_identity(self.selected,durable=self.durable,runtime_id='runtime',s3=self.s3,
                iam=self.iam,ec2=self.ec2,guard=self.guard,github=self.github,clock=lambda:self.now)
            funding.assert_called_once_with(self.durable.state)
            return value

    def test_original_successful_apply_joins_exact_source_head_and_current_identity(self):
        value=self.verify()
        self.assertFalse(value['activation']);self.assertFalse(value['qualification'])
        self.assertEqual(value['permissionsBoundaryArn'],self.identity['permissionsBoundaryArn'])
        self.assertEqual(value['producer'],self.producer)
        self.assertTrue(any('/compare/'+self.sha+'...'+self.head in row for row in self.calls))

    def test_artifact_visible_before_apply_completion_remains_unqualified(self):
        self.status='in_progress'
        with self.assertRaisesRegex(ValueError,'incomplete or unsuccessful'):self.verify()

    def test_failed_original_apply_cannot_authorize_current_matching_roles(self):
        self.apply='failure'
        with self.assertRaisesRegex(ValueError,'incomplete or unsuccessful'):self.verify()

    def test_unprotected_current_branch_cannot_authenticate_historical_producer(self):
        self.protected=False
        with self.assertRaisesRegex(ValueError,'protected source branch'):self.verify()
        self.assertFalse(any('/compare/' in row for row in self.calls))

    def test_wrong_branch_identity_refuses_before_ancestry(self):
        self.branch_name='other'
        with self.assertRaisesRegex(ValueError,'protected source branch'):self.verify()

    def test_unknown_branch_head_refuses_instead_of_comparing_mutable_name(self):
        self.head='unknown'
        with self.assertRaisesRegex(ValueError,'protected source branch'):self.verify()

    def test_legacy_boundary_cannot_substitute_dedicated_cap(self):
        self.configuration['installed_controller_identity']['permissionsBoundaryArn']=self.configuration['permissions_boundary_arn']
        self.carrier['sourceConfiguration']['sha256']=subject.digest(encoded(self.configuration))
        self.review['config_sha256']=self.carrier['sourceConfiguration']['sha256'][7:]
        self.carrier['plan']['review']=self.store('review.json',self.review)
        self.selected=self.store('identity.json',self.carrier)
        with self.assertRaisesRegex(ValueError,'applied roles/network differ'):self.verify()

    def test_each_receipt_or_producer_binding_flip_refuses(self):
        def restore(carrier):self.selected=self.store('identity.json',carrier)
        flips=[('ref',lambda c:c['producer'].update(ref='refs/heads/feature'),'producer/source is invalid'),
               ('event',lambda c:c['producer'].update(event='push'),'producer/source is invalid'),
               ('plan attempt above apply attempt',lambda c:c['plan'].update(attempt=3),'producer/source is invalid'),
               ('extra producer field',lambda c:c['producer'].update(actor='someone'),'producer/source is invalid'),
               ('foreign workflow',lambda c:c['producer'].update(workflow='.github/workflows/other.yml'),'producer/source is invalid'),
               ('extra receipt field',lambda c:c.update(note='x'),'candidate/runtime/expiry differs'),
               ('wrong funding expiry',lambda c:c.update(originalFundingExpiresEpoch=self.expiry+1),'candidate/runtime/expiry differs'),
               ('activation claimed',lambda c:c.update(activation=True),'candidate/runtime/expiry differs'),
               ('foreign candidate',lambda c:c.update(candidateKey='sha256:'+'b'*64),'candidate/runtime/expiry differs')]
        for name,flip,message in flips:
            with self.subTest(flip=name):
                carrier=copy.deepcopy(self.carrier);flip(carrier);restore(carrier)
                with self.assertRaisesRegex(ValueError,message):self.verify()
        restore(self.carrier)
        for name,setting,message in [('behind',{'compare_status':'behind'},'not protected trunk history'),
                                     ('diverged',{'compare_status':'diverged'},'not protected trunk history'),
                                     ('duplicate producer step',{'duplicate_step':True},'producer step did not succeed'),
                                     ('in-progress run',{'status':'in_progress'},'incomplete or unsuccessful'),
                                     ('expired funding',{'now':self.expiry},'lost original funding/custody')]:
            with self.subTest(flip=name):
                saved={k:getattr(self,k) for k in setting};[setattr(self,k,v) for k,v in setting.items()]
                try:
                    with self.assertRaisesRegex(ValueError,message):self.verify()
                finally:[setattr(self,k,v) for k,v in saved.items()]
        self.assertEqual(self.verify()['taskRoleArn'],self.identity['taskRoleArn'])

    def test_current_policy_drift_cannot_hide_behind_authentic_old_apply(self):
        self.actual['task']['extra']={'Version':'2012-10-17','Statement':[{'Effect':'Allow','Action':'iam:PutRolePolicy','Resource':'*'}]}
        with self.assertRaisesRegex(ValueError,'complete current policy map'):self.verify()

    def test_source_configuration_digest_is_exact_original_bytes(self):
        self.configuration['network_cidr']='10.99.0.0/16'
        with self.assertRaisesRegex(ValueError,'source bytes differ'):self.verify()

    def test_real_emitter_bytes_feed_real_original_verifier(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cfg=root/self.path;cfg.parent.mkdir(parents=True);cfg.write_bytes(encoded(self.configuration))
            work=root/'private';work.mkdir()
            review=copy.deepcopy(self.review)
            review.update(backend_sha256='d'*64,lock_sha256='e'*64,budget={'version_id':'original-budget','sha256':'f'*64},
                created_at='2033-05-18T03:33:19Z',resource_changes=[],action_counts={})
            (work/'review.json').write_bytes(encoded(review))
            env={'GITHUB_REPOSITORY':subject.REPOSITORY,'GITHUB_SHA':self.sha,'GITHUB_RUN_ID':'11','GITHUB_RUN_ATTEMPT':'2',
                'GITHUB_REF':'refs/heads/trunk','GITHUB_EVENT_NAME':'workflow_dispatch','PILOT_PLAN_ATTEMPT':'1',
                'PILOT_OPERATION':'apply','PILOT_REVIEW_SHA256':subject.digest(encoded(review))[7:],
                'PILOT_BUDGET_VERSION_ID':'original-budget','PILOT_BUDGET_SHA256':'f'*64}
            writes=[]
            def aws(*args):
                key=args[args.index('--key')+1]
                if args[1]=='put-object':
                    self.assertEqual(args[args.index('--if-none-match')+1],'*')
                    self.assertEqual(args[args.index('--server-side-encryption')+1],'AES256')
                    raw=Path(args[args.index('--body')+1]).read_bytes();self.objects[(key,'written-original')]=raw;writes.append(key)
                    return {'VersionId':'written-original'}
                self.assertEqual(args[1],'get-object')
                raw=self.objects[(key,'written-original')];Path(args[-1]).write_bytes(raw)
                return {'VersionId':'written-original','ServerSideEncryption':'AES256'}
            outputs={'installed_controller_identity':{'value':copy.deepcopy(self.identity)},
                'accounting_staging':{'value':{'candidateKey':self.identity['candidateKey'],'state':{'bucket':self.bucket}}}}
            self.selected=subject.emit(cfg=cfg,root=root,outputs=outputs,review=review,work=work,env=env,aws=aws,
                iam=self.iam,ec2=self.ec2,guard=self.guard)
            self.assertEqual(writes,[self.base+'review.json',self.base+'identity.json'])
            self.assertEqual(json.loads((work/'installed-controller-reference.json').read_bytes()),self.selected)
            answer=self.verify()
            self.assertEqual(answer['taskRoleArn'],self.identity['taskRoleArn'])
            self.assertFalse(answer['activation'])

    def test_fixed_production_entry_none_staging_uses_no_provider_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);cfg=root/'configuration.json';cfg.write_text('{}')
            output=root/'github-output';output.touch()
            context=MagicMock();aws=MagicMock();client=MagicMock(side_effect=AssertionError('provider call'))
            module=SimpleNamespace(ROOT=root,TF_ROOT=subject.ROOT_PATH,aws=aws,config=lambda path:cfg,context=context)
            with patch.dict('sys.modules',{'deploy':module,'boto3':SimpleNamespace(client=client)}), \
                 patch.dict(os.environ,{'PILOT_OPERATION':'apply','RUNNER_TEMP':directory,'PILOT_CONFIG_PATH':'ignored',
                    'GITHUB_OUTPUT':str(output)}),patch.object(subject.sys,'argv',['installed_controller_identity.py']):
                subject.main()
            context.assert_called_once();aws.assert_not_called();client.assert_not_called()
            self.assertEqual(output.read_text(),'installed_controller_identity_reference=false\n')

    def test_fixed_production_entry_refuses_caller_arguments_before_context(self):
        context=MagicMock()
        module=SimpleNamespace(ROOT=Path('/unused'),TF_ROOT=subject.ROOT_PATH,aws=MagicMock(),config=MagicMock(),context=context)
        with patch.dict('sys.modules',{'deploy':module}),patch.object(subject.sys,'argv',['entry.py','--override']):
            with self.assertRaisesRegex(ValueError,'no caller arguments'):subject.main()
        context.assert_not_called()

    def test_configured_fixed_entry_uses_existing_cli_without_sdk_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);work=root/'hosted-pilot-private';work.mkdir()
            cfg=root/'configuration.json';cfg.write_text(json.dumps({'region':'us-east-1','installed_controller_identity':{'runtimeId':'runtime'}}))
            for name in ('outputs','review'):(work/(name+'.json')).write_text('{}')
            output=root/'github-output';output.touch();aws=MagicMock(return_value={'Role':{'Arn':'actual-observation'}})
            module=SimpleNamespace(ROOT=root,TF_ROOT=subject.ROOT_PATH,aws=aws,config=lambda path:cfg,context=MagicMock())
            def emit(**kwargs):
                self.assertIsInstance(kwargs['iam'],subject.ReadCLI);self.assertIsInstance(kwargs['ec2'],subject.ReadCLI)
                self.assertEqual(kwargs['iam'].get_role(RoleName='actual-role'),{'Role':{'Arn':'actual-observation'}})
                return self.selected
            with patch.dict('sys.modules',{'deploy':module,'boto3':None,'botocore':None}), \
                 patch.dict(os.environ,{'PILOT_OPERATION':'apply','RUNNER_TEMP':directory,'PILOT_CONFIG_PATH':'ignored',
                    'PILOT_AWS_EXPIRATION':'2033-05-18T03:33:20Z','GITHUB_OUTPUT':str(output)}), \
                 patch.object(subject.sys,'argv',['installed_controller_identity.py']),patch.object(subject,'emit',side_effect=emit):
                subject.main()
            aws.assert_called_once_with('iam','get-role','--role-name','actual-role','--region','us-east-1',
                '--no-paginate','--no-cli-pager','--cli-connect-timeout','5','--cli-read-timeout','20')
            self.assertEqual(output.read_text(),'installed_controller_identity_reference=true\n')


class FiniteReadAdapter(unittest.TestCase):
    def setUp(self):
        self.aws=MagicMock(return_value={'original':'response'});self.guard=MagicMock()
        self.iam=subject.ReadCLI('iam',region='us-east-1',aws=self.aws,guard=self.guard)

    def test_native_single_page_read_keeps_timeouts_region_and_guard(self):
        self.assertEqual(self.iam.get_role_policy(RoleName='actual-role',PolicyName='actual-policy'),{'original':'response'})
        self.aws.assert_called_once_with('iam','get-role-policy','--role-name','actual-role','--policy-name','actual-policy',
            '--region','us-east-1','--no-paginate','--no-cli-pager','--cli-connect-timeout','5','--cli-read-timeout','20')
        self.assertEqual(self.guard.call_count,2)

    def test_mutation_or_secret_service_cannot_be_expressed(self):
        with self.assertRaises(AttributeError):self.iam.put_role_policy(RoleName='role',PolicyName='name')
        with self.assertRaisesRegex(ValueError,'service/region'):
            subject.ReadCLI('secretsmanager',region='us-east-1',aws=self.aws,guard=self.guard)
        self.aws.assert_not_called()

    def test_pagination_or_unreviewed_arguments_refuse_before_wire(self):
        with self.assertRaisesRegex(ValueError,'read arguments'):
            self.iam.list_role_policies(RoleName='role',Marker='next')
        self.aws.assert_not_called()

    def test_only_exact_reviewed_subnets_are_requested(self):
        client=subject.ReadCLI('ec2',region='us-east-1',aws=self.aws,guard=self.guard)
        client.describe_subnets(SubnetIds=['subnet-0123456789abcdef0','subnet-0123456789abcdef1'])
        self.assertEqual(self.aws.call_args.args[:4],('ec2','describe-subnets','--subnet-ids','["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"]'))
        self.aws.reset_mock()
        for bad in ([],['subnet-0123456789abcdef0']*2,['sg-0123456789abcdef0'],['subnet-%017x'%n for n in range(7)]):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError,'exact reviewed subnets'):
                client.describe_subnets(SubnetIds=bad)
        self.aws.assert_not_called()

    def test_only_exact_observed_security_group_is_requested(self):
        client=subject.ReadCLI('ec2',region='us-east-1',aws=self.aws,guard=self.guard)
        client.describe_security_groups(GroupIds=['sg-0123456789abcdef0'])
        self.assertEqual(self.aws.call_args.args[:4],('ec2','describe-security-groups','--group-ids','["sg-0123456789abcdef0"]'))
        self.aws.reset_mock()
        with self.assertRaisesRegex(ValueError,'one exact security group'):
            client.describe_security_groups(GroupIds=['sg-0123456789abcdef0','sg-fffffffffffffffff'])
        self.aws.assert_not_called()


if __name__=='__main__':unittest.main()
