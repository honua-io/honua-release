"""Journal source vectors, separate from actual local TLS tunnel controls."""
import pytest
from devops_fault_tunnel import FaultProfile
from devops_provisioning_contract import canonical, sha256, Refusal
from devops_fault_originals import verify_fault_original


def vector():
    profile = FaultProfile('a'*64, ('ec2.us-west-2.amazonaws.com', 'sts.us-west-2.amazonaws.com'),
                           ('ec2.us-west-2.amazonaws.com',))
    kwargs = {'profile':profile, 'operation_id':'urn:honua:provisioning:'+'1'*32,
              'mutation_original':b'original mutation', 'resource_original':b'original AWS read',
              'terminal_original':b'original process observation'}
    records = [('started',{'contextSha256':profile.context_sha256}),
               ('connected',{'host':profile.fault_hosts[0]})]
    for kind, original in [('mutation-bound','mutation_original'),('armed','resource_original')]:
        records.append((kind,{'operationId':kwargs['operation_id'],'originalSha256':sha256(kwargs[original])}))
    records.extend([('connection-dropped',{'host':profile.fault_hosts[0],'phase':'existing'}),
        ('restored',{'operationId':kwargs['operation_id'],'originalSha256':sha256(kwargs['terminal_original'])}),
        ('closed',{}), ('connection-ended',{})])
    value={'contextSha256':profile.context_sha256,'journal':[
        dict(ordinal=i,kind=kind,monotonicNs=i+1,**fields) for i,(kind,fields) in enumerate(records)],
        'journalExhausted':False,'faultedConnections':1,'acceptedConnections':1,
        'armed':False,'closed':True,'candidateQualified':False,'sixAwsCellsQualified':False}
    return value, kwargs


@pytest.mark.parametrize('phase',['existing','new','connecting'])
def test_all_actual_helper_drop_phases_and_worker_close_tail(phase):
    value, kwargs=vector();value['journal'][4]['phase']=phase
    result=verify_fault_original(canonical(value),**kwargs)
    assert result['status']=='fault-transport-joined' and result['sixAwsCellsQualified'] is False


@pytest.mark.parametrize('mutation',['missing','unselected','wrong-original','wrong-operation',
    'exhausted','no-drops','counter','connection-counter','live','wrong-context','reorder','unknown','after-close-traffic'])
def test_journal_refuses_incomplete_or_substituted_originals(mutation):
    value,kwargs=vector()
    if mutation=='missing':value['journal'].pop(5)
    if mutation=='unselected':value['journal'][4]['host']='sts.us-west-2.amazonaws.com'
    if mutation=='wrong-original':kwargs['terminal_original']+=b'changed'
    if mutation=='wrong-operation':kwargs['operation_id']='urn:honua:provisioning:'+'2'*32
    if mutation=='exhausted':value['journalExhausted']=True
    if mutation=='no-drops':value['faultedConnections']=0
    if mutation=='counter':value['faultedConnections']=2
    if mutation=='connection-counter':value['acceptedConnections']=2
    if mutation=='live':value['closed']=False
    if mutation=='wrong-context':value['contextSha256']='f'*64
    if mutation=='reorder':value['journal'][1],value['journal'][2]=value['journal'][2],value['journal'][1]
    if mutation=='unknown':value['journal'][7]['kind']='ready'
    if mutation=='after-close-traffic':value['journal'][7].update(kind='connected',host=kwargs['profile'].allowed_hosts[0])
    with pytest.raises(Refusal):verify_fault_original(canonical(value),**kwargs)
