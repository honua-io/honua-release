"""Parser/source vectors only; no AWS calls or live cleanup proof."""
from dataclasses import replace
import pytest
from devops_provisioning_contract import Refusal, canonical
import devops_aws_resource_fate as fate


def resource(ownership='owned'):
    return fate.Resource('arn:aws:rds:us-west-2:123456789012:db:owned','rds-instance','owned',
                         '123456789012','us-west-2',ownership)


def read(value=None, *, error=None, command=None, target=None):
    target=target or resource()
    return fate.OriginalRead(command or fate.expected_command(target),255 if error else 0,
                             b'' if error else canonical(value),error or b'')


def tags(arns=(),token='',command=None):
    command=command or ('aws','resourcegroupstaggingapi','get-resources','--region','us-west-2',
             '--tag-filters','Key=honua-release:run-id,Values=123','Key=honua-release:cell,Values=original',
             '--no-paginate','--output','json')
    return fate.OriginalRead(command,0,canonical({'ResourceTagMappingList':[
        {'ResourceARN':arn,'Tags':[{'Key':'honua-release:run-id','Value':'123'},
                                  {'Key':'honua-release:cell','Value':'original'}]} for arn in arns],
        'PaginationToken':token}),b'')


def test_real_cli_notfound_framing_and_exact_operation_only():
    target=resource()
    raw=b'\nAn error occurred (DBInstanceNotFound) when calling the DescribeDBInstances operation: missing\n'
    assert fate.verdict(target,read(error=raw))['verdict']=='absent'
    for error in [raw.replace(b'DBInstanceNotFound',b'AccessDenied'),
                  raw.replace(b'DescribeDBInstances',b'DeleteDBInstance'),
                  b'warning\n'+raw,b'transport failure NotFound',b'{"Error":"DBInstanceNotFound"}']:
        assert fate.verdict(target,read(error=error))['verdict']=='unverified'


@pytest.mark.parametrize('value',[{}, {'DBInstances':[]},{'DBInstances':'bad'},
    {'DBInstances':[{'DBInstanceArn':'foreign','DBInstanceStatus':'available'}]},
    {'DBInstances':[{'DBInstanceArn':resource().arn,'DBInstanceStatus':'available'}],'Marker':'more'}])
def test_missing_identity_or_incomplete_read_is_never_absence(value):
    assert fate.verdict(resource(),read(value))['verdict']=='unverified'


def test_live_resource_and_wrong_read_command():
    value={'DBInstances':[{'DBInstanceArn':resource().arn,'DBInstanceStatus':'available'}]}
    assert fate.verdict(resource(),read(value))['verdict']=='live'
    with pytest.raises(Refusal,match='command'):
        fate.verdict(resource(),read(value,command=('aws','rds','delete-db-instance')))


def test_unknown_kind_never_inherits_a_known_provider_absence():
    target=replace(resource(),kind='future-provider')
    raw=fate.OriginalRead(('aws','future','read'),255,b'',b'NotFound')
    assert fate.verdict(target,raw)['verdict']=='unverified'


def test_known_id_is_read_even_after_all_tags_disappear():
    target=resource();raw=read(error=b'An error occurred (DBInstanceNotFound) when calling the DescribeDBInstances operation: missing')
    result=fate.judge_fate({target.arn:target},{target.arn:raw},tag_pages=(tags(),),
                           region='us-west-2',run_id='123',cell_id='original')
    assert result['status']=='destroyed' and result['candidateQualified'] is False
    bad=fate.OriginalRead(raw.command,255,b'',b'AccessDenied')
    result=fate.judge_fate({target.arn:target},{target.arn:bad},tag_pages=(tags(),),
                           region='us-west-2',run_id='123',cell_id='original')
    assert result['status']=='inventory-indeterminate' and result['unverified']==[target.arn]


def test_external_resource_must_remain_live_with_original_identity():
    target=resource('externally-owned')
    live=read({'DBInstances':[{'DBInstanceArn':target.arn,'DBInstanceStatus':'available'}]},target=target)
    result=fate.judge_fate({target.arn:target},{target.arn:live},tag_pages=(tags(),),
                           region='us-west-2',run_id='123',cell_id='original')
    assert result['retainedExternalResources']==[target.arn]
    missing=read(error=b'An error occurred (DBInstanceNotFound) when calling the DescribeDBInstances operation: missing',target=target)
    with pytest.raises(Refusal,match='external-owned-resource-lost'):
        fate.judge_fate({target.arn:target},{target.arn:missing},tag_pages=(tags(),),
                       region='us-west-2',run_id='123',cell_id='original')


def test_original_pagination_requires_exact_complete_followup_and_no_unknown_rows():
    first=tags(token='original-next')
    command=first.command+('--pagination-token','original-next')
    second=tags(command=command)
    assert fate.tag_census((first,second),region='us-west-2',run_id='123',cell_id='original')==()
    with pytest.raises(Refusal,match='incomplete'):
        fate.tag_census((first,),region='us-west-2',run_id='123',cell_id='original')
    with pytest.raises(Refusal,match='page-read'):
        fate.tag_census((first,tags()),region='us-west-2',run_id='123',cell_id='original')
    target=resource()
    with pytest.raises(Refusal,match='unclassified'):
        fate.judge_fate({target.arn:target},{target.arn:read({})},tag_pages=(tags(('arn:aws:unknown:us-west-2:123456789012:new',)),),
                       region='us-west-2',run_id='123',cell_id='original')


@pytest.mark.parametrize('failures',[None,[],[{'arn':'foreign','reason':'MISSING'}],
    [{'arn':'arn:aws:ecs:us-west-2:123456789012:task/cluster/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','reason':'AccessDenied'}]])
def test_ecs_missing_is_only_exact_authoritative_failure(failures):
    target=fate.Resource('arn:aws:ecs:us-west-2:123456789012:task/cluster/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','ecs-task','a'*32,
                         '123456789012','us-west-2','owned','arn:aws:ecs:us-west-2:123456789012:cluster/cluster')
    original=read({'tasks':[],'failures':failures},target=target)
    assert fate.verdict(target,original)['verdict']=='unverified'
    original=read({'tasks':[],'failures':[{'arn':target.arn,'reason':'MISSING'}]},target=target)
    assert fate.verdict(target,original)['verdict']=='absent'


FORMATS=[
 ('rds-instance','rds','db:owned','owned','foreign'),
 ('elasticache-cluster','elasticache','cluster:owned','owned','foreign'),
 ('elasticache-replication-group','elasticache','replicationgroup:owned','owned','foreign'),
 ('alb','elasticloadbalancing','loadbalancer/app/owned/0123456789abcdef',None,'foreign'),
 ('efs','elasticfilesystem','file-system/fs-0123456789abcdef0','fs-0123456789abcdef0','fs-1123456789abcdef0'),
 ('ec2-volume','ec2','volume/vol-0123456789abcdef0','vol-0123456789abcdef0','vol-1123456789abcdef0'),
 ('nat-gateway','ec2','natgateway/nat-0123456789abcdef0','nat-0123456789abcdef0','nat-1123456789abcdef0'),
 ('elastic-ip','ec2','elastic-ip/eipalloc-0123456789abcdef0','eipalloc-0123456789abcdef0','eipalloc-1123456789abcdef0'),
 ('ecs-cluster','ecs','cluster/owned','owned','foreign'),
 ('ecs-service','ecs','service/owned/service-a','service-a','service-b'),
 ('ecs-task','ecs','task/owned/'+'a'*32,'a'*32,'b'*32),
]


@pytest.mark.parametrize('kind,service,suffix,identifier,foreign',FORMATS)
def test_actual_arn_identifier_formats_are_bound_before_query(kind,service,suffix,identifier,foreign):
    arn='arn:aws:'+service+':us-west-2:123456789012:'+suffix
    parent='arn:aws:ecs:us-west-2:123456789012:cluster/owned' if kind in ('ecs-service','ecs-task') else None
    target=fate.Resource(arn,kind,identifier or arn,'123456789012','us-west-2','owned',parent)
    assert fate.expected_command(target)[0]=='aws'
    # Existing ARN A cannot be declared absent by querying absent resource B.
    with pytest.raises(Refusal,match='identifier-binding'):
        fate.expected_command(replace(target,identity=foreign))
    wrong_service='ec2' if service!='ec2' else 'rds'
    with pytest.raises(Refusal,match='kind-arn-binding'):
        fate.expected_command(replace(target,arn=arn.replace(':'+service+':',':'+wrong_service+':',1)))


def test_ecs_arn_parent_and_actual_returned_service_cluster_are_bound():
    target=fate.Resource('arn:aws:ecs:us-west-2:123456789012:service/owned/service-a',
                         'ecs-service','service-a','123456789012','us-west-2','owned',
                         'arn:aws:ecs:us-west-2:123456789012:cluster/owned')
    with pytest.raises(Refusal,match='arn-parent'):
        fate.expected_command(replace(target,parent=target.parent.replace('/owned','/foreign')))
    response={'services':[{'serviceArn':target.arn,'clusterArn':target.parent,'status':'INACTIVE'}],'failures':[]}
    assert fate.verdict(target,read(response,target=target))['verdict']=='settled'
    response['services'][0]['clusterArn']=target.parent.replace('/owned','/foreign')
    assert fate.verdict(target,read(response,target=target))['verdict']=='unverified'


def test_external_s3_reference_binds_bucket_name_owner_and_actual_region():
    target=fate.Resource('arn:aws:s3:::owned-external-bucket','s3-bucket','owned-external-bucket',
                         '123456789012','us-west-2','externally-owned')
    command=fate.expected_command(target)
    assert command==('aws','s3api','get-bucket-location','--bucket','owned-external-bucket',
                      '--expected-bucket-owner','123456789012','--region','us-west-2',
                      '--no-paginate','--output','json')
    assert fate.verdict(target,read({'LocationConstraint':'us-west-2'},target=target))['verdict']=='live'
    for value in ({},{'LocationConstraint':False},{'LocationConstraint':'us-east-2'}):
        assert fate.verdict(target,read(value,target=target))['verdict']=='unverified'
    with pytest.raises(Refusal,match='identifier-binding'):
        fate.expected_command(replace(target,identity='different-absent-bucket'))
    denied=read(error=b'An error occurred (AccessDenied) when calling the GetBucketLocation operation: denied',target=target)
    assert fate.verdict(target,denied)['verdict']=='unverified'
    missing=read(error=b'An error occurred (NoSuchBucket) when calling the GetBucketLocation operation: missing',target=target)
    assert fate.verdict(target,missing)['verdict']=='absent'
