"""Decoded stock-SDK JSON-RPC observation joins for the production native provisioning tool.

This is a pure consumer, not a substitute client or protocol server. It can only
rejudge decoded messages collected through the stock installed official MCP client.
Outer JSON whitespace is not an acceptance requirement; canonical artifact originals
are independently resolved through production immutable evidence references.
"""
from __future__ import annotations
from devops_provisioning_contract import document, require, canonical, sha256


def field(value, name):
    """Support actual public DTO's two serializers without ambiguous aliases."""
    upper=name[0].upper()+name[1:]
    require(not(name in value and upper in value), 'wire-conflicting-dto-alias')
    return value.get(name, value.get(upper))


def operation_result(response):
    result=response.get('result')
    require(type(result) is dict and result.get('isError',False) is False, 'wire-tool-error')
    require(type(result.get('content',[])) is list
            and all(type(row) is dict for row in result.get('content',[])), 'wire-content-shape')
    structured=result.get('structuredContent')
    texts=[row.get('text') for row in result.get('content',[]) if type(row) is dict and row.get('type')=='text']
    require(structured is not None or len(texts)==1, 'wire-operation-content')
    parsed=structured if structured is not None else document(texts[0].encode())
    require(type(parsed) is dict, 'wire-operation-object')
    status=field(parsed,'status')
    require(type(status) is str and bool(status), 'wire-operation-status')
    if structured is not None and texts:
        require(len(texts)==1 and type(texts[0]) is str, 'wire-operation-content')
        textual=document(texts[0].encode())
        def normalized(value):
            result=dict(value)
            for name in ('status','summary','findings','actions','validationChecks','risks'):
                upper=name[0].upper()+name[1:]
                require(not(name in result and upper in result),'wire-conflicting-dto-alias')
                if upper in result: result[name]=result.pop(upper)
            return result
        require(normalized(textual)==normalized(parsed),'wire-operation-conflicting-content')
    return parsed,status


def verify_call(request_bytes: bytes, response_bytes: bytes, *, request_id,
                expected_arguments: dict, allowed_statuses: tuple[str,...]):
    request=document(request_bytes);response=document(response_bytes)
    require(type(request_id) in (str,int) and type(request_id) is not bool, 'wire-request-id')
    require(request.get('jsonrpc')=='2.0' and response.get('jsonrpc')=='2.0'
            and type(request.get('id')) is type(request_id) and request['id']==request_id
            and type(response.get('id')) is type(request_id) and response['id']==request_id,
            'wire-original-request-response-id')
    require(request.get('method')=='tools/call' and request.get('params')=={
            'name':'provision_infrastructure','arguments':expected_arguments},'wire-fixed-tool-arguments')
    require('error' not in response,'wire-rpc-error')
    operation,status=operation_result(response)
    require(status in allowed_statuses,'wire-unexpected-operation-status')
    return {'decodedRequestJournalSha256':sha256(request_bytes),'decodedResponseJournalSha256':sha256(response_bytes),
            'requestId':request_id,'status':status,'candidateQualified':False,'sixAwsCellsQualified':False}


def verify_cancel(notification_bytes: bytes, *, owned_request_id):
    notification=document(notification_bytes)
    require(type(owned_request_id) in (str,int) and type(owned_request_id) is not bool,'wire-request-id')
    require(notification.get('jsonrpc')=='2.0' and notification.get('method')=='notifications/cancelled'
            and 'id' not in notification and type(notification.get('params')) is dict,
            'wire-cancellation-notification')
    params=notification['params']
    require(set(params)<={'requestId','reason'} and type(params.get('requestId')) is type(owned_request_id)
            and params['requestId']==owned_request_id
            and ('reason' not in params or type(params['reason']) is str), 'wire-owned-cancellation-id')
    return {'decodedNotificationJournalSha256':sha256(notification_bytes),'requestId':owned_request_id,
            'candidateQualified':False,'sixAwsCellsQualified':False}


def plan_artifacts(response_bytes: bytes, *, operation_id: str, action: str,
                   stack: str, environment: str, plan_sha256: str, metadata_digest: str):
    """Use the production approvalRequest/evidenceRefs projection, never summary prose."""
    response=document(response_bytes)
    operation,status=operation_result(response)
    require(action in ('apply','destroy') and status==('terraform-plan-ready' if action=='apply'
            else 'terraform-destroy-plan-ready'),'plan-response-status')
    lineage=operation.get('provisioningLineage');approval=operation.get('approvalRequest')
    require(type(lineage) is dict and type(approval) is dict,'plan-response-structured-lineage')
    require(lineage.get('provisioningOperationId')==operation_id
            and lineage.get('planSha256')==plan_sha256
            and lineage.get('planMetadataDigest')==metadata_digest,'plan-response-original-lineage')
    require(approval.get('provisioningOperationId')==operation_id
            and approval.get('planSha256')==plan_sha256 and approval.get('planMetadataDigest')==metadata_digest
            and approval.get('action')==action and approval.get('stack')==stack
            and approval.get('environment')==environment
            and approval.get('receiptSchemaVersion')=='honua.devops.provision-approval/v1',
            'plan-response-approval-projection')
    references=lineage.get('evidenceRefs')
    require(type(references) is list and all(type(row) is dict for row in references),
            'plan-response-reference-shape')
    require(len({row.get('kind') for row in references})==len(references)
            and {row.get('kind') for row in references}=={'plan','plan-metadata'},
            'plan-response-reference-closure')
    refs={row['kind']:row for row in references}
    require(refs['plan'].get('sha256')==plan_sha256,'plan-response-binary-reference')
    # The plan metadata's SHA256 bytes differs from its self-excluding canonical digest.
    # read_reference rejudges that separate immutable byte identity/length.
    return {'evidenceRefs':refs,'approvalRequest':approval,'candidateQualified':False,'sixAwsCellsQualified':False}
