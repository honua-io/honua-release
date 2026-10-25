"""Deployment-produced installed poller identity, never runtime activation.

The dedicated runtime cannot write this provisioning prefix. Verification joins
original immutable S3 bytes to the successful protected plan/apply/configuration,
then compares complete current IAM and network originals before returning roles.
"""
from __future__ import annotations
import base64
import copy
import json
import os
from pathlib import Path
import re
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_pack import canonical_digest
from agent_e2e_interactive_budget import validate_retained_funding
from installed_worker_receipt import read_original, retained_put, digest, REPOSITORY, WORKFLOW
from enrollment_receipt import original_review, positive_integer, private_bytes
from fixture_handoff import require, encoded
from human_memberships import github_json

SCHEMA = 'honua.private-installed-controller-identity/v1'
ROOT_PATH = 'certification/hosted-pilot/campaign'
LIMIT = 2*1024*1024
IDENTITY_FIELDS = {'schema','candidateKey','runtimeId','region','accountId','taskRoleArn','executionRoleArn',
    'permissionsBoundaryArn','assumeRolePolicy','clusterArn','vpcId','subnetIds','securityGroupIds',
    'originalFundingExpiresEpoch','policies','activation','qualification','networkCidr'}


class ReadCLI:
    """Finite single-page read adapter over the checked runner's existing CLI."""
    OPERATIONS = {
        'iam': {'get_role': ('RoleName',), 'list_attached_role_policies': ('RoleName',),
                'list_role_policies': ('RoleName',), 'get_role_policy': ('RoleName','PolicyName')},
        'ec2': {'describe_security_groups': ('GroupIds',), 'describe_subnets': ('SubnetIds',)},
    }

    def __init__(self,service,*,region,aws,guard):
        require(service in self.OPERATIONS and re.fullmatch(r'[a-z]{2}-[a-z]+-[0-9]',region),
                'installed identity CLI service/region is unsupported')
        self.service,self.region,self.aws,self.guard=service,region,aws,guard

    def __getattr__(self,operation):
        if operation not in self.OPERATIONS[self.service]:raise AttributeError(operation)
        fields=self.OPERATIONS[self.service][operation]
        def call(**kwargs):
            require(set(kwargs)==set(fields),'installed identity CLI read arguments differ')
            arguments=[self.service,operation.replace('_','-')]
            for key in fields:
                value=kwargs[key]
                if key=='GroupIds':
                    require(isinstance(value,list) and len(value)==1 and
                            re.fullmatch(r'sg-[a-f0-9]{8,17}',str(value[0])),
                            'installed identity CLI needs one exact security group')
                    rendered=json.dumps(value)
                elif key=='SubnetIds':
                    require(isinstance(value,list) and 1 <= len(value) == len(set(value)) <= 6 and
                            all(re.fullmatch(r'subnet-[a-f0-9]{8,17}',str(item)) for item in value),
                            'installed identity CLI needs the exact reviewed subnets')
                    rendered=json.dumps(value)
                else:
                    require(isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9+=,.@_-]{1,'+
                            ('64' if key=='RoleName' else '128')+'}',value),
                            'installed identity CLI role/policy name differs')
                    rendered=value
                arguments.extend(['--'+re.sub(r'(?<!^)(?=[A-Z])','-',key).lower(),rendered])
            arguments.extend(['--region',self.region,'--no-paginate','--no-cli-pager',
                              '--cli-connect-timeout','5','--cli-read-timeout','20'])
            self.guard();answer=self.aws(*arguments);self.guard()
            return answer
        return call


def policies(iam, role_arn, *, guard):
    """No attachment/pagination fallback and no caller-supplied policy map."""
    role_name = role_arn.rsplit('/', 1)[-1]
    guard(); attached = iam.list_attached_role_policies(RoleName=role_name)
    guard(); inline = iam.list_role_policies(RoleName=role_name)
    require(attached.get('IsTruncated') is False and not attached.get('Marker') and
            attached.get('AttachedPolicies') == [] and inline.get('IsTruncated') is False and
            not inline.get('Marker') and isinstance(inline.get('PolicyNames'), list) and
            len(inline['PolicyNames']) == len(set(inline['PolicyNames'])) <= 8,
            'installed controller policy inventory is attached, truncated or ambiguous')
    result = {}
    for name in sorted(inline['PolicyNames']):
        guard(); answer = iam.get_role_policy(RoleName=role_name, PolicyName=name); guard()
        require(answer.get('RoleName') == role_name and answer.get('PolicyName') == name and
                isinstance(answer.get('PolicyDocument'), dict), 'installed controller actual inline policy differs')
        result[name] = answer['PolicyDocument']
    return result


def role_original(iam, arn, *, guard):
    guard(); answer = iam.get_role(RoleName=arn.rsplit('/', 1)[-1]); guard()
    role = answer.get('Role', {})
    require(role.get('Arn') == arn and isinstance(role.get('AssumeRolePolicyDocument'), dict) and
            role.get('PermissionsBoundary', {}).get('PermissionsBoundaryType') == 'Policy' and
            re.fullmatch(r'arn:aws:iam::[0-9]{12}:policy/[A-Za-z0-9+=,.@_/-]+',
                         str(role.get('PermissionsBoundary', {}).get('PermissionsBoundaryArn'))),
            'installed controller actual role/trust/boundary is unavailable')
    return {'roleArn':arn, 'trust':role['AssumeRolePolicyDocument'],
            'permissionsBoundaryArn':role['PermissionsBoundary']['PermissionsBoundaryArn'],
            'policies':policies(iam, arn, guard=guard)}


def network_original(ec2, identity, *, guard):
    expected = identity['securityGroupIds']
    require(isinstance(expected, list) and len(expected) == 1 and
            all(re.fullmatch(r'sg-[a-f0-9]{8,17}', value) for value in expected),
            'installed controller needs its exact isolated security group')
    guard(); answer = ec2.describe_security_groups(GroupIds=expected); guard()
    groups = answer.get('SecurityGroups')
    require(isinstance(groups, list) and len(groups) == 1 and
            groups[0].get('GroupId') == expected[0] and groups[0].get('VpcId') == identity['vpcId'] and
            groups[0].get('OwnerId') == identity['accountId'] and groups[0].get('IpPermissions') == [],
            'installed controller current security group identity or ingress differs')
    group = groups[0]
    permissions = group.get('IpPermissionsEgress')
    require(isinstance(permissions, list) and len(permissions) == 3, 'installed controller current egress differs')
    observed = []
    for row in permissions:
        require(not row.get('UserIdGroupPairs') and not row.get('Ipv6Ranges') and not row.get('PrefixListIds') and
                isinstance(row.get('IpRanges'), list) and len(row['IpRanges']) == 1,
                'installed controller egress has an unreviewed destination')
        observed.append({'protocol':row.get('IpProtocol'), 'from':row.get('FromPort'), 'to':row.get('ToPort'),
                         'cidr':row['IpRanges'][0].get('CidrIp')})
    wanted = [{'protocol':'tcp','from':443,'to':443,'cidr':'0.0.0.0/0'},
              {'protocol':'tcp','from':53,'to':53,'cidr':identity['networkCidr']},
              {'protocol':'udp','from':53,'to':53,'cidr':identity['networkCidr']}]
    require(sorted(observed,key=canonical_digest) == sorted(wanted,key=canonical_digest),
            'installed controller current egress is not HTTPS/private DNS only')
    subnets = identity['subnetIds']
    require(isinstance(subnets, list) and 1 <= len(subnets) == len(set(subnets)) <= 6 and
            all(re.fullmatch(r'subnet-[a-f0-9]{8,17}', str(value)) for value in subnets),
            'installed controller needs its exact reviewed subnets')
    guard(); answer = ec2.describe_subnets(SubnetIds=sorted(subnets)); guard()
    rows = answer.get('Subnets')
    require(isinstance(rows, list) and not answer.get('NextToken') and
            sorted(row.get('SubnetId') for row in rows) == sorted(subnets) and
            all(row.get('VpcId') == identity['vpcId'] and row.get('OwnerId') == identity['accountId'] and
                row.get('State') == 'available' for row in rows),
            'installed controller current subnets are not the reviewed VPC/account subnets')
    return {'securityGroupId':expected[0], 'vpcId':identity['vpcId'], 'accountId':identity['accountId'],
            'subnetIds':sorted(subnets), 'ingress':[], 'egress':sorted(observed,key=canonical_digest)}


def observe_identity(identity, *, iam, ec2, guard):
    """Independent original/current provider observations; no role or SG writes."""
    require(isinstance(identity,dict) and set(identity) == IDENTITY_FIELDS and
            identity['schema'] == 'honua.installed-controller-identity-observation/v1' and
            identity['activation'] is False and identity['qualification'] is False and
            isinstance(identity['policies'],dict) and set(identity['policies']) == {'task','execution'},
            'installed controller identity must contain only closed nonsecret observations')
    result = {name:role_original(iam, identity[name+'RoleArn'], guard=guard) for name in ('task','execution')}
    require(result['task']['permissionsBoundaryArn'] == result['execution']['permissionsBoundaryArn'] ==
            identity['permissionsBoundaryArn'], 'installed controller current permissions boundary differs')
    require(result['task']['trust'] == result['execution']['trust'] == identity['assumeRolePolicy'],
            'installed controller current trust differs from reviewed ECS account-bound trust')
    require(result['task']['policies'] == identity['policies']['task'] and
            result['execution']['policies'] == identity['policies']['execution'],
            'installed controller complete current policy map differs from reviewed apply')
    result['network'] = network_original(ec2, identity, guard=guard)
    return result


def prefix(candidate, runtime, run, attempt):
    return 'agent-e2e/'+candidate[7:]+'/installed-controllers/'+runtime+'/identity/'+str(run)+'/'+str(attempt)+'/'


def source_identity(configuration, identity, *, candidate, runtime, expiry):
    declaration=configuration.get('installed_controller_identity',{})
    require(configuration.get('candidate_digest') == candidate == identity['candidateKey'] and
            declaration.get('runtimeId') == identity['runtimeId'] == runtime and
            declaration.get('originalFundingExpiresEpoch') == identity['originalFundingExpiresEpoch'] == expiry and
            identity['networkCidr'] == configuration.get('network_cidr') and identity['region'] == configuration.get('region') and
            identity['accountId'] == configuration.get('account_id') and
            identity['taskRoleArn'] == 'arn:aws:iam::'+identity['accountId']+':role/'+configuration['name_prefix']+'-installed-controller' and
            identity['executionRoleArn'] == identity['taskRoleArn']+'-execution' and
            identity['permissionsBoundaryArn'] == declaration.get('permissionsBoundaryArn') ==
            'arn:aws:iam::'+identity['accountId']+':policy/honua-release-'+configuration['name_prefix'].removesuffix('-agent').removeprefix('honua-')+'-foundation-installed-controller-boundary' and
            identity['activation'] is False and identity['qualification'] is False,
            'installed controller applied roles/network differ from reviewed source')


def emit(*, cfg, root, outputs, review, work, env, aws, iam, ec2, guard):
    configuration = json.loads(Path(cfg).read_bytes())
    source = configuration.get('installed_controller_identity')
    if source is None:
        return None
    cfg_path = str(Path(cfg).relative_to(root)); raw = (work/'review.json').read_bytes()
    original_review(review, raw, Path(cfg).read_bytes(), cfg_path, env, ROOT_PATH)
    identity = copy.deepcopy(outputs['installed_controller_identity']['value'])
    require(identity['candidateKey'] == configuration['candidate_digest'] and identity['runtimeId'] == source['runtimeId'] and
            identity['originalFundingExpiresEpoch'] == source['originalFundingExpiresEpoch'] and
            identity['activation'] is False and identity['qualification'] is False,
            'installed controller identity output/source differs')
    # The network CIDR is the reviewed source input, not caller metadata.
    identity['networkCidr'] = configuration['network_cidr']
    source_identity(configuration,identity,candidate=configuration['candidate_digest'],runtime=source['runtimeId'],
                    expiry=source['originalFundingExpiresEpoch'])
    observed = observe_identity(identity, iam=iam, ec2=ec2, guard=guard)
    producer = {'repository':REPOSITORY,'workflow':WORKFLOW,'sourceSha':env['GITHUB_SHA'],
                'runId':positive_integer(env['GITHUB_RUN_ID']),'runAttempt':positive_integer(env['GITHUB_RUN_ATTEMPT']),
                'jobKey':'apply','ref':env['GITHUB_REF'],'event':env['GITHUB_EVENT_NAME']}
    base = prefix(identity['candidateKey'],identity['runtimeId'],producer['runId'],producer['runAttempt'])
    # The state bucket comes from the actual applied accounting output.
    accounting = outputs['accounting_staging']['value']
    require(accounting['candidateKey'] == identity['candidateKey'], 'identity destination is outside original candidate accounting')
    bucket = accounting['state']['bucket']
    review_ref = retained_put(work/'review.json', bucket=bucket, key=base+'review.json', aws=aws)
    receipt = {'schema':SCHEMA,'candidateKey':identity['candidateKey'],'runtimeId':identity['runtimeId'],
               'originalFundingExpiresEpoch':identity['originalFundingExpiresEpoch'],'producer':producer,
               'sourceConfiguration':{'path':cfg_path,'sha256':digest(Path(cfg).read_bytes())},
               'plan':{'review':review_ref,'sha256':'sha256:'+review['plan_sha256'],'versionId':review['s3_version_id'],
                       'attempt':positive_integer(env['PILOT_PLAN_ATTEMPT'])},
               'identity':identity,'originals':observed,'activation':False,'qualification':False}
    private_bytes(work/'installed-controller-identity.json',encoded(receipt))
    selected = retained_put(work/'installed-controller-identity.json',bucket=bucket,key=base+'identity.json',aws=aws)
    private_bytes(work/'installed-controller-reference.json',encoded(selected))
    return selected


def verify_controller_identity(selected, *, durable, runtime_id, s3, iam, ec2, guard, github=github_json, clock=time.time):
    """Read-only proof; no fabricated live lease, identity or future registration."""
    validate_retained_funding(durable.state)
    expiry = durable.state['ledger'].get('interactiveAccounting',{}).get('originalExpiresEpoch')
    require(type(expiry) is int and clock() < expiry and selected.get('bucket') == durable.store.bucket,
            'installed controller identity lost original funding/custody')
    receipt,_ = read_original(s3,selected,guard=guard)
    require(set(receipt) == {'schema','candidateKey','runtimeId','originalFundingExpiresEpoch','producer',
            'sourceConfiguration','plan','identity','originals','activation','qualification'} and
            receipt['schema'] == SCHEMA and receipt['candidateKey'] == durable.store.candidate and receipt['runtimeId'] == runtime_id and
            receipt['originalFundingExpiresEpoch'] == expiry and receipt['activation'] is False and receipt['qualification'] is False,
            'installed controller identity receipt candidate/runtime/expiry differs')
    producer, source, plan = receipt['producer'], receipt['sourceConfiguration'], receipt['plan']
    require(set(producer) == {'repository','workflow','sourceSha','runId','runAttempt','jobKey','ref','event'} and
            producer['repository'] == REPOSITORY and producer['workflow'] == WORKFLOW and producer['jobKey'] == 'apply' and
            producer['ref'] == 'refs/heads/trunk' and producer['event'] == 'workflow_dispatch' and
            re.fullmatch(r'[a-f0-9]{40}',producer['sourceSha']) and all(type(producer[k]) is int and producer[k] > 0 for k in ('runId','runAttempt')) and
            set(source) == {'path','sha256'} and re.fullmatch(re.escape(ROOT_PATH)+r'/deployments/[A-Za-z0-9_.-]+\.tfvars\.json',source['path']) and
            set(plan) == {'review','sha256','versionId','attempt'} and type(plan['attempt']) is int and 0 < plan['attempt'] <= producer['runAttempt'],
            'installed controller original protected producer/source is invalid')
    base = prefix(receipt['candidateKey'],runtime_id,producer['runId'],producer['runAttempt'])
    require(selected['key'] == base+'identity.json' and plan['review']['bucket'] == durable.store.bucket and
            plan['review']['key'] == base+'review.json', 'installed controller original identity/review prefix differs')
    review,_ = read_original(s3,plan['review'],guard=guard)
    require(review.get('schema') == 'honua.hosted-pilot-saved-plan/v1' and review.get('operation') == 'apply' and
            review.get('root_path') == ROOT_PATH and review.get('config_path') == source['path'] and
            'sha256:'+review.get('config_sha256','') == source['sha256'] and
            'sha256:'+review.get('plan_sha256','') == plan['sha256'] and review.get('s3_version_id') == plan['versionId'] and
            review.get('context') == {'GITHUB_REPOSITORY':REPOSITORY,'GITHUB_SHA':producer['sourceSha'],
                'GITHUB_RUN_ID':str(producer['runId']),'GITHUB_RUN_ATTEMPT':str(plan['attempt'])},
            'installed controller identity does not bind the exact original saved plan')
    proofs = {}
    for attempt,name in {(producer['runAttempt'],'apply'),(plan['attempt'],'plan')}:
        path = 'repos/'+REPOSITORY+'/actions/runs/'+str(producer['runId'])+'/attempts/'+str(attempt)
        guard(); run=github(path); jobs=github(path+'/jobs?per_page=100'); guard()
        require(run.get('id') == producer['runId'] and run.get('run_attempt') == attempt and run.get('head_sha') == producer['sourceSha'] and
                run.get('head_branch') == 'trunk' and run.get('event') == 'workflow_dispatch' and run.get('path','').split('@')[0] == WORKFLOW and
                run.get('repository',{}).get('full_name') == REPOSITORY and
                run.get('status') == 'completed' and (name != 'apply' or run.get('conclusion') == 'success') and
                type(jobs.get('total_count')) is int and len(jobs.get('jobs',[])) == jobs['total_count'] <= 100,
                'installed controller original producing run is incomplete or unsuccessful')
        matches=[job for job in jobs['jobs'] if job.get('name') == name]
        require(len(matches) == 1 and matches[0].get('run_id') == producer['runId'] and matches[0].get('head_sha') == producer['sourceSha'] and
                matches[0].get('status') == 'completed' and matches[0].get('conclusion') == 'success',
                'installed controller original plan/apply job did not succeed')
        if name == 'apply':
            for step in ('Verify identity and apply exactly the saved plan','Retain installed controller identity-only output'):
                require(sum(row.get('name') == step and row.get('conclusion') == 'success' for row in matches[0].get('steps',[])) == 1,
                        'installed controller original apply/identity producer step did not succeed')
        proofs[str(attempt)+'/'+name]={'run':run,'jobs':jobs}
    guard(); branch=github('repos/'+REPOSITORY+'/branches/trunk'); guard()
    head=branch.get('commit',{}).get('sha')
    require(branch.get('name')=='trunk' and branch.get('protected') is True and
            re.fullmatch(r'[a-f0-9]{40}',str(head)), 'installed controller protected source branch is unavailable')
    guard(); ancestor=github('repos/'+REPOSITORY+'/compare/'+producer['sourceSha']+'...'+head); guard()
    require(ancestor.get('status') in ('ahead','identical') and
            ancestor.get('merge_base_commit',{}).get('sha') == producer['sourceSha'], 'installed controller producer is not protected trunk history')
    guard(); original=github('repos/'+REPOSITORY+'/contents/'+source['path']+'?ref='+producer['sourceSha']); guard()
    require(original.get('encoding') == 'base64', 'installed controller original source is unavailable')
    raw=base64.b64decode(''.join(original['content'].split()),validate=True)
    require(len(raw) <= LIMIT and digest(raw) == source['sha256'], 'installed controller original source bytes differ')
    configuration=json.loads(raw)
    identity=receipt['identity']
    source_identity(configuration,identity,candidate=receipt['candidateKey'],runtime=runtime_id,expiry=expiry)
    observed=observe_identity(identity,iam=iam,ec2=ec2,guard=guard)
    require(observed == receipt['originals'], 'installed controller current trust/policy/network drifted from original apply')
    guard()
    return {field:copy.deepcopy(identity[field]) for field in ('taskRoleArn','executionRoleArn',
            'permissionsBoundaryArn','securityGroupIds','subnetIds','vpcId','policies')} | {
            'producer':producer,'proof':{'original':selected,'review':plan['review'],'publicOriginals':proofs,
                'sourceConfiguration':source,'plan':plan,'roleOriginals':{'task':observed['task'],'execution':observed['execution']},
                'networkOriginal':observed['network']},'activation':False,'qualification':False}


def main():
    from deploy import ROOT, TF_ROOT, aws, config, context
    require(len(sys.argv)==1,'installed controller identity emitter accepts no caller arguments')
    context()
    require(TF_ROOT==ROOT_PATH and os.environ.get('PILOT_OPERATION')=='apply',
            'installed controller identity emission requires exact campaign apply')
    work=Path(os.environ['RUNNER_TEMP'])/'hosted-pilot-private'
    cfg=config(os.environ['PILOT_CONFIG_PATH'])
    configuration=json.loads(cfg.read_bytes())
    if configuration.get('installed_controller_identity') is None:
        result=None
    else:
        from datetime import datetime,timezone
        expires=datetime.fromisoformat(os.environ['PILOT_AWS_EXPIRATION'].replace('Z','+00:00'))
        require(expires.tzinfo is not None,'original apply credential expiry is required')
        def guard():
            require(datetime.now(timezone.utc)<expires,'original apply credential expired; retain evidence without renewal')
        guard()
        clients={name:ReadCLI(name,region=configuration['region'],aws=aws,guard=guard) for name in ('iam','ec2')}
        result=emit(cfg=cfg,root=ROOT,outputs=json.loads((work/'outputs.json').read_bytes()),
            review=json.loads((work/'review.json').read_bytes()),work=work,env=os.environ,aws=aws,
            iam=clients['iam'],ec2=clients['ec2'],guard=guard)
    with open(os.environ['GITHUB_OUTPUT'],'a') as out:
        out.write('installed_controller_identity_reference='+('true' if result else 'false')+'\n')
    print('Controller identity staging retained; activation remains false.' if result else 'Existing controller path retained.')


if __name__=='__main__':
    try:main()
    except Exception:
        print('private installed controller identity refused; retain original evidence and liabilities',file=sys.stderr)
        sys.exit(1)
