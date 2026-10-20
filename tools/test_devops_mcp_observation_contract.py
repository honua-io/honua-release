"""Decoded-message source vectors; not an installed binary or cloud receipt."""
import copy
import pytest
from devops_provisioning_contract import Refusal,canonical
from devops_mcp_observation_contract import verify_call,verify_cancel


def call_vectors():
    args={'stack':'aws-ecs','size':'small','action':'apply','variablesJson':'{}',
          'confirmed':True,'confirmation':'apply:aws-ecs:it:'+'a'*32,'approvalReceiptJson':'original-signed-input'}
    request={'jsonrpc':'2.0','id':17,'method':'tools/call','params':{
             'name':'provision_infrastructure','arguments':args}}
    response={'jsonrpc':'2.0','id':17,'result':{'structuredContent':{'status':'infrastructure-provisioned'},
              'content':[{'type':'text','text':'{"Status":"infrastructure-provisioned"}'}]}}
    return request,response,args


def test_actual_public_dto_case_variants_with_same_status_are_supported():
    request,response,args=call_vectors()
    result=verify_call(canonical(request),canonical(response),request_id=17,
                       expected_arguments=args,allowed_statuses=('infrastructure-provisioned',))
    assert result['candidateQualified'] is False and result['sixAwsCellsQualified'] is False
    del response['result']['structuredContent']
    assert verify_call(canonical(request),canonical(response),request_id=17,
        expected_arguments=args,allowed_statuses=('infrastructure-provisioned',))['status']=='infrastructure-provisioned'


@pytest.mark.parametrize('mutation',['wrong-id','id-type','tool','approval','action','tool-error','rpc-error',
                                    'wrong-status','conflicting-alias','conflicting-content','malformed-content'])
def test_coherent_original_wire_mutations_do_not_pass_dispatch(mutation):
    request,response,args=call_vectors();args=copy.deepcopy(args)
    if mutation=='wrong-id': response['id']=18
    if mutation=='id-type': response['id']='17'
    if mutation=='tool': request['params']['name']='other_tool'
    if mutation=='approval': request['params']['arguments']['approvalReceiptJson']='foreign'
    if mutation=='action': request['params']['arguments']['action']='destroy'
    if mutation=='tool-error': response['result']['isError']=True
    if mutation=='rpc-error': response['error']={'code':-32800,'message':'cancelled'}
    if mutation=='wrong-status': response['result']['structuredContent']['status']='terraform-plan-ready'
    if mutation=='conflicting-alias':response['result']['structuredContent']['Status']='failed'
    if mutation=='conflicting-content':response['result']['content'][0]['text']='{"Status":"failed"}'
    if mutation=='malformed-content':response['result']['content']='not-an-array'
    with pytest.raises(Refusal):verify_call(canonical(request),canonical(response),request_id=17,
        expected_arguments=args,allowed_statuses=('infrastructure-provisioned',))


def test_matching_cancel_wire_is_not_cessation_proof():
    notification={'jsonrpc':'2.0','method':'notifications/cancelled','params':{'requestId':17,'reason':'owned'}}
    result=verify_cancel(canonical(notification),owned_request_id=17)
    assert result['candidateQualified'] is False and result['sixAwsCellsQualified'] is False
    for foreign in (18,'17',True):
        notification['params']['requestId']=foreign
        with pytest.raises(Refusal):verify_cancel(canonical(notification),owned_request_id=17)
