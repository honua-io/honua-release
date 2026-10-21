"""Finite read-only AWS resource-fate parsing for original DevOps cleanup proof.

These functions interpret retained ORIGINAL AWS CLI reads. They issue no AWS
requests and never delete resources. The caller must independently bind each
command/account/region/owned identity and its original byte hashes. Unknown
providers or incomplete reads remain unverified, never absent.
"""
from __future__ import annotations
from dataclasses import dataclass
import re
from typing import Mapping
from devops_provisioning_contract import Refusal, document, require, sha256


@dataclass(frozen=True)
class Resource:
    arn: str
    kind: str
    identity: str
    account: str
    region: str
    ownership: str  # owned or externally-owned; authenticated pre-cell declaration
    parent: str | None = None  # exact independently observed ECS cluster ARN only


# AWS CLI ClientError framing, not a substring search for NotFound.
ERROR = re.compile(r'An error occurred \(([A-Za-z0-9_.-]+)\) when calling the ([A-Za-z0-9]+) operation: [^\r\n]*\Z')
READS = {
    's3-bucket': ('s3api','get-bucket-location','GetBucketLocation','NoSuchBucket',None,None,None),
    'ecs-task': ('ecs','describe-tasks','DescribeTasks',None,'tasks','taskArn','lastStatus'),
    'ecs-service': ('ecs','describe-services','DescribeServices',None,'services','serviceArn','status'),
    'ecs-cluster': ('ecs','describe-clusters','DescribeClusters',None,'clusters','clusterArn','status'),
    'rds-instance': ('rds', 'describe-db-instances', 'DescribeDBInstances', 'DBInstanceNotFound',
                     'DBInstances', 'DBInstanceArn', 'DBInstanceStatus'),
    'elasticache-cluster': ('elasticache', 'describe-cache-clusters', 'DescribeCacheClusters', 'CacheClusterNotFound',
                            'CacheClusters', 'ARN', 'CacheClusterStatus'),
    'elasticache-replication-group': ('elasticache', 'describe-replication-groups', 'DescribeReplicationGroups', 'ReplicationGroupNotFoundFault',
                                      'ReplicationGroups', 'ARN', 'Status'),
    'alb': ('elbv2', 'describe-load-balancers', 'DescribeLoadBalancers', 'LoadBalancerNotFound',
            'LoadBalancers', 'LoadBalancerArn', None),
    'efs': ('efs', 'describe-file-systems', 'DescribeFileSystems', 'FileSystemNotFound',
            'FileSystems', 'FileSystemArn', 'LifeCycleState'),
    'ec2-volume': ('ec2', 'describe-volumes', 'DescribeVolumes', 'InvalidVolume.NotFound',
                   'Volumes', 'VolumeId', 'State'),
    'nat-gateway': ('ec2', 'describe-nat-gateways', 'DescribeNatGateways', 'InvalidNatGatewayID.NotFound',
                    'NatGateways', 'NatGatewayId', 'State'),
    'elastic-ip': ('ec2', 'describe-addresses', 'DescribeAddresses', 'InvalidAllocationID.NotFound',
                   'Addresses', 'AllocationId', None),
}


def resource_context(resource: Resource):
    require(resource.ownership in ('owned', 'externally-owned'), 'resource-ownership')
    require(type(resource.account) is str and re.fullmatch(r'[0-9]{12}', resource.account) is not None
            and type(resource.region) is str and re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-[0-9]', resource.region) is not None,
            'resource-account-region')
    require(type(resource.arn) is str and resource.arn.startswith('arn:aws:')
            and not any(character.isspace() for character in resource.arn), 'resource-arn')
    parts=resource.arn.split(':',5)
    require(len(parts)==6,'resource-arn-shape')
    if resource.kind=='s3-bucket':
        bucket=parts[5]
        require(parts[2:5]==['s3','',''] and 3<=len(bucket)<=63
                and re.fullmatch(r'[a-z0-9][a-z0-9.-]*[a-z0-9]',bucket)
                and '..' not in bucket,'resource-kind-arn-binding')
        require(resource.identity==bucket,'resource-arn-identifier-binding')
        return
    require(parts[3]==resource.region and parts[4]==resource.account,'resource-arn-account-region')
    require(type(resource.identity) is str and bool(resource.identity)
            and not any(character.isspace() for character in resource.identity), 'resource-identity')
    formats={
        'rds-instance':('rds',r'db:([A-Za-z][A-Za-z0-9-]{0,62})'),
        'elasticache-cluster':('elasticache',r'cluster:([A-Za-z][A-Za-z0-9-]{0,49})'),
        'elasticache-replication-group':('elasticache',r'replicationgroup:([A-Za-z][A-Za-z0-9-]{0,39})'),
        'alb':('elasticloadbalancing',r'loadbalancer/app/([A-Za-z0-9-]{1,32}/[0-9a-f]{16})'),
        'efs':('elasticfilesystem',r'file-system/(fs-(?:[0-9a-f]{8}|[0-9a-f]{17}))'),
        'ec2-volume':('ec2',r'volume/(vol-(?:[0-9a-f]{8}|[0-9a-f]{17}))'),
        'nat-gateway':('ec2',r'natgateway/(nat-(?:[0-9a-f]{8}|[0-9a-f]{17}))'),
        'elastic-ip':('ec2',r'elastic-ip/(eipalloc-(?:[0-9a-f]{8}|[0-9a-f]{17}))'),
        'ecs-cluster':('ecs',r'cluster/([A-Za-z0-9_-]{1,255})'),
        'ecs-service':('ecs',r'service/((?:[A-Za-z0-9_-]{1,255}/)?[A-Za-z0-9_-]{1,255})'),
        'ecs-task':('ecs',r'task/((?:[A-Za-z0-9_-]{1,255}/)?[0-9a-f]{32})'),
    }
    if resource.kind not in formats:return
    service,pattern=formats[resource.kind]
    match=re.fullmatch(pattern,parts[5])
    require(parts[2]==service and match is not None,'resource-kind-arn-binding')
    identifier=match.group(1)
    allowed={identifier,resource.arn} if resource.kind in ('rds-instance','ecs-cluster','ecs-service','ecs-task') else {identifier}
    if resource.kind in ('ecs-service','ecs-task'):allowed.add(identifier.rsplit('/',1)[-1])
    if resource.kind=='alb':allowed={resource.arn}
    require(resource.identity in allowed,'resource-arn-identifier-binding')
    if resource.kind in ('ecs-service','ecs-task'):
        require(type(resource.parent) is str,'resource-ecs-parent')
        parent=re.fullmatch(r'arn:aws:ecs:'+re.escape(resource.region)+':'+re.escape(resource.account)+r':cluster/([A-Za-z0-9_-]{1,255})',resource.parent)
        require(parent is not None,'resource-ecs-parent')
        if '/' in identifier:
            cluster,identifier=identifier.split('/',1)
            require(cluster==parent.group(1),'resource-ecs-arn-parent-binding')
        require(resource.identity in {match.group(1),identifier,resource.arn},'resource-arn-identifier-binding')


@dataclass(frozen=True)
class OriginalRead:
    command: tuple[str, ...]
    exit_code: int
    stdout: bytes
    stderr: bytes


ID_FLAGS = {'rds-instance':'--db-instance-identifier', 'elasticache-cluster':'--cache-cluster-id',
            'elasticache-replication-group':'--replication-group-id', 'alb':'--load-balancer-arns',
            'efs':'--file-system-id', 'ec2-volume':'--volume-ids', 'nat-gateway':'--nat-gateway-ids',
            'elastic-ip':'--allocation-ids'}


def expected_command(resource):
    resource_context(resource)
    service,operation,*_=READS[resource.kind]
    if resource.kind=='s3-bucket':
        return ('aws','s3api','get-bucket-location','--bucket',resource.identity,
                '--expected-bucket-owner',resource.account,'--region',resource.region,
                '--no-paginate','--output','json')
    if resource.kind.startswith('ecs-'):
        if resource.kind=='ecs-cluster': args=('--clusters',resource.arn)
        else:
            require(type(resource.parent) is str and resource.parent.startswith(
                f'arn:aws:ecs:{resource.region}:{resource.account}:cluster/'),'resource-ecs-parent')
            args=('--cluster',resource.parent,'--tasks' if resource.kind=='ecs-task' else '--services',resource.arn)
        return ('aws',service,operation,*args,'--region',resource.region,'--no-paginate','--output','json')
    identity=resource.arn if resource.kind=='alb' else resource.identity
    return ('aws',service,operation,ID_FLAGS[resource.kind],identity,'--region',resource.region,
            '--no-paginate','--output','json')


def verdict(resource: Resource, original: OriginalRead):
    """No read failure, malformed response or unrecognized state can mean absent."""
    resource_context(resource)
    exit_code,stdout,stderr=original.exit_code,original.stdout,original.stderr
    require(type(stdout) is bytes and type(stderr) is bytes and len(stdout)<=8*1024*1024
            and len(stderr)<=1024*1024, 'resource-original-bound')
    result = {'arn':resource.arn, 'kind':resource.kind, 'ownership':resource.ownership,
              'stdoutSha256':sha256(stdout), 'stderrSha256':sha256(stderr), 'verdict':'unverified'}
    if resource.kind not in READS:
        return result
    require(original.command==expected_command(resource), 'resource-original-command')
    service, operation, api_name, not_found, rows_key, identity_key, status_key = READS[resource.kind]
    require(type(exit_code) is int, 'resource-read-exit')
    if exit_code != 0:
        try:
            error = ERROR.fullmatch(stderr.decode('utf-8').strip())
        except UnicodeError:
            return result
        if not stdout.strip() and error and error.groups() == (not_found, api_name):
            result['verdict']='absent'
        return result
    if stderr.strip():
        return result
    try:
        response = document(stdout)
        if response.get('NextToken') or response.get('Marker') or response.get('NextMarker'):
            return result
        if resource.kind=='s3-bucket':
            # Existing supported owner-bound readonly API, not a policy/grant.
            # AWS documents null=us-east-1 and legacy EU=eu-west-1.
            if set(response)!={'LocationConstraint'}: return result
            location=response['LocationConstraint']
            actual_region='us-east-1' if location is None else ('eu-west-1' if location=='EU' else location)
            if type(actual_region) is str and actual_region==resource.region:
                result['verdict']='live'
            return result
        rows = response.get(rows_key)
        if type(rows) is not list:
            return result
        if resource.kind.startswith('ecs-'):
            failures=response.get('failures')
            if type(failures) is not list: return result
            if (not rows and len(failures)==1 and type(failures[0]) is dict
                    and failures[0].get('arn')==resource.arn and failures[0].get('reason')=='MISSING'):
                result['verdict']='absent';return result
            if failures: return result
        if not rows:
            # Exact identity-filtered EC2 reads can authoritatively return no rows.
            # Other services' documented NotFound must be observed, not inferred.
            if resource.kind in ('nat-gateway', 'elastic-ip', 'ec2-volume'):
                result['verdict']='absent'
            return result
        if len(rows)!=1 or type(rows[0]) is not dict:
            return result
        row=rows[0]
        expected_identity = resource.identity if identity_key in ('VolumeId','NatGatewayId','AllocationId') else resource.arn
        if row.get(identity_key)!=expected_identity:
            return result
        if resource.kind in ('ecs-task','ecs-service') and row.get('clusterArn')!=resource.parent:
            return result
        if resource.kind=='ecs-task' and row.get('lastStatus')=='STOPPED':
            result['verdict']='settled'
        elif resource.kind in ('ecs-service','ecs-cluster') and row.get('status')=='INACTIVE':
            result['verdict']='settled'
        elif resource.kind=='nat-gateway' and row.get('State')=='deleted':
            result['verdict']='settled'
        elif resource.kind=='ec2-volume' and row.get('State')=='deleted':
            result['verdict']='settled'
        elif status_key is None or type(row.get(status_key)) is str:
            # Even unknown/new provider states are never a settled/absent verdict.
            result['verdict']='live'
        return result
    except Refusal:
        return result


def tag_census(originals: tuple[OriginalRead, ...], *, region: str, run_id: str, cell_id: str):
    """Rejudge every original explicit page and its exact next-page command."""
    require(type(originals) is tuple and 0<len(originals)<=1000,'inventory-page-bound')
    require(all(type(value) is str and value and not any(ch.isspace() for ch in value)
                for value in (region,run_id,cell_id)), 'inventory-query-context')
    token=None;seen_tokens=set();arns=[]
    for index,read in enumerate(originals):
        command=('aws','resourcegroupstaggingapi','get-resources','--region',region,
                 '--tag-filters','Key=honua-release:run-id,Values='+run_id,
                 'Key=honua-release:cell,Values='+cell_id,'--no-paginate','--output','json')
        if token: command+=('--pagination-token',token)
        require(read.command==command and type(read.exit_code) is int and read.exit_code==0
                and not read.stderr.strip(), 'inventory-page-read')
        page=document(read.stdout);rows=page.get('ResourceTagMappingList')
        require(type(rows) is list,'inventory-page-shape')
        for row in rows:
            require(type(row) is dict and type(row.get('ResourceARN')) is str
                    and row['ResourceARN'].startswith('arn:aws:'),'inventory-tag-identity')
            tags=row.get('Tags')
            require(type(tags) is list and all(type(tag) is dict and set(tag)=={'Key','Value'}
                    and type(tag['Key']) is str and type(tag['Value']) is str for tag in tags),
                    'inventory-tag-shape')
            require(len({tag['Key'] for tag in tags})==len(tags),'inventory-tag-duplicate-key')
            values={tag['Key']:tag['Value'] for tag in tags}
            require(values.get('honua-release:run-id')==run_id
                    and values.get('honua-release:cell')==cell_id,'inventory-tag-query-binding')
            arns.append(row['ResourceARN'])
        token=page.get('PaginationToken','')
        require(type(token) is str and not any(ch.isspace() for ch in token),'inventory-page-token')
        if token:
            require(token not in seen_tokens and index<len(originals)-1,'inventory-page-incomplete')
            seen_tokens.add(token)
        else: require(index==len(originals)-1,'inventory-page-extra')
    require(not token and len(set(arns))==len(arns),'inventory-page-completion')
    return tuple(arns)


def judge_fate(expected: Mapping[str, Resource], originals: Mapping[str, OriginalRead], *,
               tag_pages: tuple[OriginalRead, ...], region: str, run_id: str, cell_id: str):
    """Rejudge raw reads: normalized fate rows or caller-complete flags are not inputs."""
    tag_arns=tag_census(tag_pages,region=region,run_id=run_id,cell_id=cell_id)
    require(set(expected)==set(originals) and bool(expected), 'inventory-known-identity-closure')
    require(set(tag_arns)<=set(expected), 'inventory-unclassified-resource')
    survivors=[]; unknown=[]; retained=[]
    for arn, resource in expected.items():
        resource_context(resource)
        require(arn==resource.arn and resource.region==region,'inventory-resource-binding')
        row=verdict(resource,originals[arn]);state=row['verdict']
        if state=='unverified':unknown.append(arn)
        elif resource.ownership=='externally-owned':
            if state=='live':retained.append(arn)
            else:raise Refusal('external-owned-resource-lost')
        elif state=='live':survivors.append(arn)
    status='inventory-indeterminate' if unknown else ('cleanup-failed' if survivors else 'destroyed')
    return {'status':status,'survivors':sorted(survivors),'unverified':sorted(unknown),
            'retainedExternalResources':sorted(retained),'candidateQualified':False,'sixAwsCellsQualified':False}
