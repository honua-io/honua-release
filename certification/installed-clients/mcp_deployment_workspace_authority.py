"""Actual owned-resource eligibility and non-bypass deployment authority journey.

This is a component of the complete TWO-client proof, never independent row
admission. It neither grants an admin denial nor executes a rollback proposal.
"""
from __future__ import annotations
import copy
import re
import mcp_deployment_workspace_validation as semantic
import mcp_map_package_validation as wire
from mcp_deployment_workspace_store_oracle import observation,instant
from mcp_deployment_identity_configuration import verify_configuration

SCHEMA='honua.mcp-deployment-workspace-authority/v1'
FIELDS={'schema','callsBefore','callsAfter','foreignWorkspacePrerequisite','personalWorkspacePrerequisite',
    'storeBefore','personalStoreBefore','storeAfter','personalStoreAfter','limitedRead','runtimeBefore','runtimeAfter'}


def scoped_refusal(result,message):
    if (not isinstance(result,dict) or set(result)!={'isError','content','structuredContent'} or result['isError'] is not True
            or not isinstance(result['content'],list) or len(result['content'])!=1 or not isinstance(result['structuredContent'],dict)):
        raise ValueError('valid managed scoped caller must receive typed authorization denial')
    item,value=result['content'][0],result['structuredContent']
    if (not isinstance(item,dict) or set(item)!={'type','text'} or item['type']!='text'
            or not wire.same_json(wire.strict_json(item['text']),value)
            or set(value)-{'status','code','message','error','correlationId'} or value.get('status')!='error'
            or value.get('code')!='permission_denied' or value.get('message')!=message
            or value.get('error')!={'kind':'AuthorizationDenied','message':message}
            or ('correlationId' in value and (not isinstance(value['correlationId'],str) or not 0<len(value['correlationId'])<=256))):
        raise ValueError('scoped platform refusal changed supported authorization semantics')


def prepared(preparation,runtime,nonce,selector,service_id):
    """Select public provisioning observations for progress, not final admission.

    The complete final oracle must match this ORIGINAL preparation to its fully
    rejudged terminal cohort; these partial objects cannot grant authority.
    """
    if (not isinstance(preparation,dict) or preparation.get('schema')!='honua.mcp-deployment-managed-cohort/v1'
            or preparation.get('nonce')!=nonce or preparation.get('serviceId')!=service_id
            or preparation.get('candidate')!={key:runtime.get(key) for key in ('containerId','imageId','candidateSourceSha')}
            or preparation.get('failures')!=[] or preparation.get('cleanupComplete') is not False):
        raise ValueError('original managed provisioning observations missing')
    checked=verify_configuration(preparation['configuration'],preparation['configurationObservation'],runtime)
    if checked['primaryTenant']!='mcp-deployment-'+nonce:raise ValueError('managed provisioning belongs to another nonce')
    pair=preparation.get('keys',{}).get(selector)
    if not isinstance(pair,dict) or set(pair)!={'primaryReceipt','foreignReceipt'}:
        raise ValueError('both normal provisioning identities missing')
    result={}
    for name,profile in [('primaryReceipt','admin'),('foreignReceipt','workspace-reader')]:
        receipt=pair[name];identity=receipt.get('identity') if isinstance(receipt,dict) else None
        if (receipt.get('profile')!=profile or receipt.get('credentialSalt')!=preparation.get('credentialSalt')
                or receipt.get('cleanupComplete') is not False or not isinstance(identity,dict)
                or receipt.get('keyId')!=identity.get('keyId')):
            raise ValueError('normal provisioning identity profile differs')
        semantic.guid(identity.get('keyId'))
        if identity.get('studioActorId')!=identity['keyId'] or identity.get('workspaceOwnerId')!=identity['keyId']:
            raise ValueError('normal provisioning owner identity differs')
        result['primary' if profile=='admin' else 'foreign']=identity
    return checked,result


def advance(fixture,bootstrap,value,*,nonce,canonical,version,target_id,preparation,stage,semantic_observations=None):
    if not isinstance(value,dict) or set(value)!=FIELDS or value['schema']!=SCHEMA or stage not in ('before','after'):
        raise ValueError('full authority observations missing or unknown')
    selector='sdk' if canonical=='Official MCP TypeScript SDK' else 'inspector' if canonical=='MCP Inspector' else None
    if selector is None or not isinstance(version,str) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',version):
        raise ValueError('actual canonical authority client missing')
    runtime=value['runtimeBefore'];selected=dict(bootstrap);selected['runtimeBinding']=runtime;wire.scenario(fixture,selected)
    config,identities=prepared(preparation,runtime,nonce,selector,bootstrap['ownedArea']['serviceId'])
    authored=semantic.scenario(fixture,bootstrap,nonce,canonical)
    foreign=copy.deepcopy(authored)
    foreign.update(packageKey=authored['packageKey']+'-foreign',route=authored['route']+'-foreign',workspaceLabel=authored['workspaceLabel']+'-foreign')
    personal=copy.deepcopy(authored);personal['workspaceLabel']=authored['workspaceLabel']+'-personal'
    calls=value['callsBefore'];after_calls=value['callsAfter'];position=0;sessions=set();fingerprints={}
    if not isinstance(calls,list) or not isinstance(after_calls,list):raise ValueError('actual authority calls missing')
    def take(spec,*,role='primary',public=True):
        nonlocal position
        expected=copy.deepcopy(spec);expected['credentialRole']=role
        if role=='primary':expected['tenantId']=config['foreignTenant'] if public else config['primaryTenant']
        if position==len(calls):raise semantic.Pending({'action':'invoke','call':expected})
        result=semantic.actual_call(calls[position],expected,canonical,version,sessions,fingerprints,
            tenant_id=expected.get('tenantId'),timed=True)
        position+=1;return result
    def need(slot,action):
        if not isinstance(value.get(slot),dict):raise semantic.Pending(action)
        return value[slot]
    try:
        first=semantic.initial_calls(fixture,bootstrap,foreign)
        first[0]['id']='foreign-fixture-map';first[1]['id']='foreign-fixture-draft'
        first[1]['params']['arguments']['ownerId']=identities['foreign']['studioActorId']
        mapped,created=take(first[0]),take(first[1])
        update=semantic.studio_update(fixture,bootstrap,foreign,mapped,created);update['id']='foreign-fixture-update'
        updated=take(update)
        validate,save=semantic.studio_save(updated,update['params']['arguments']['draftId'])
        validate['id']='foreign-fixture-validate';save['id']='foreign-fixture-save'
        checked=wire.json_tool_output(take(validate))
        if checked.get('status')!='valid' or checked.get('unsupportedCapabilities')!=[]:
            raise ValueError('real foreign-owned canonical map did not validate')
        saved=take(save);semantic.operation_completed(wire.json_tool_output(saved))
        publish=semantic.studio_publish(saved,foreign);publish['id']='foreign-fixture-publication'
        published=take(publish);output=wire.json_tool_output(published);semantic.operation_completed(output)
        if output.get('humanConfirmationRequired') is not False:
            raise ValueError('real foreign-owned publication did not use supported admin-direct authorization')
        expected=semantic.published_identity(published,publish,update['params']['arguments']['body']);expected['targetId']=target_id
        foreign_gp=need('foreignWorkspacePrerequisite',{'action':'create-workspace','slot':'foreignWorkspacePrerequisite',
            'credentialRole':'foreign','serviceId':bootstrap['ownedArea']['serviceId'],'label':foreign['workspaceLabel']})
        personal_gp=need('personalWorkspacePrerequisite',{'action':'create-workspace','slot':'personalWorkspacePrerequisite',
            'credentialRole':'primary','tenantId':config['foreignTenant'],'serviceId':bootstrap['ownedArea']['serviceId'],'label':personal['workspaceLabel']})
        before=need('storeBefore',{'action':'observe-stores','slot':'storeBefore','targetId':target_id,
            'deploymentId':expected['deploymentId'],'workspaceId':foreign_gp['workspaceId']})
        personal_before=need('personalStoreBefore',{'action':'observe-stores','slot':'personalStoreBefore','targetId':target_id,
            'deploymentId':expected['deploymentId'],'workspaceId':personal_gp['workspaceId']})
        stores=observation(before,target_id,expected['deploymentId'],foreign_gp['workspaceId'])
        personal_stores=observation(personal_before,target_id,expected['deploymentId'],personal_gp['workspaceId'])
        foreign_workspace=semantic.workspace_prerequisite(foreign_gp,foreign,bootstrap,stores,strong=True)
        personal_workspace=semantic.workspace_prerequisite(personal_gp,personal,bootstrap,personal_stores,strong=True)
        own_deployment=semantic.deployment_read(take(semantic.read('honua://deployments/'+expected['deploymentId'],id='foreign-own-deployment'),role='foreign'),expected,stores)
        own_workspace=semantic.workspace_read(take(semantic.read('honua://workspaces/'+foreign_gp['workspaceId'],id='foreign-own-workspace'),role='foreign'),foreign_workspace,stores)
        personal_body=semantic.workspace_read(take(semantic.read('honua://workspaces/'+personal_gp['workspaceId'],id='personal-workspace-owner')),personal_workspace,personal_stores)
        need('limitedRead',{'action':'limited-data-read','credentialRole':'foreign'})
        scoped_refusal(take(semantic.tool('honua_deploy_operations',{},id='foreign-deploy-role-denial'),role='foreign'),
            'Caller is not authorized to read platform operations.')
        scoped_refusal(take(semantic.tool('honua_propose_rollback',{'targetId':target_id,'reason':'Owned role refusal only; no actuation',
            'idempotencyKey':authored['packageKey']+'-foreign-denied'},id='foreign-rollback-role-denial'),role='foreign'),
            'Caller is not authorized to propose platform mutations.')
        wire.resource_refusal(take(semantic.read('honua://workspaces/'+personal_gp['workspaceId'],id='foreign-personal-workspace'),role='foreign'),-32002,'not_found')
        if position!=len(calls):raise ValueError('unexpected additional authority call')
        facts={'foreignDeploymentId':expected['deploymentId'],'foreignWorkspaceId':foreign_gp['workspaceId'],
            'personalWorkspaceId':personal_gp['workspaceId'],'foreignStores':stores,'personalStores':personal_stores}
        if stage=='before':return {'action':'complete','facts':facts}
        if not isinstance(semantic_observations,dict):raise ValueError('complete primary semantic observations missing')
        # Reuse the same independent parser and session set across both authority
        # phases. No copied result or reused transport session can provide readback.
        calls=after_calls;position=0
        if value['runtimeBefore']!=semantic_observations['runtimeBefore'] or value['runtimeAfter']!=semantic_observations['runtimeAfter']:
            raise ValueError('authority and semantic phases did not use the same actual restart')
        if semantic.deployment_read(take(semantic.read('honua://deployments/'+expected['deploymentId'],id='restarted-foreign-own-deployment'),role='foreign'),expected,stores)!=own_deployment:
            raise ValueError('foreign deployment changed after the actual same-container restart')
        if semantic.workspace_read(take(semantic.read('honua://workspaces/'+foreign_gp['workspaceId'],id='restarted-foreign-own-workspace'),role='foreign'),foreign_workspace,stores)!=own_workspace:
            raise ValueError('foreign workspace changed after the actual same-container restart')
        if semantic.workspace_read(take(semantic.read('honua://workspaces/'+personal_gp['workspaceId'],id='restarted-personal-workspace-owner')),personal_workspace,personal_stores)!=personal_body:
            raise ValueError('personal workspace changed after actual restart')
        wire.resource_refusal(take(semantic.read('honua://workspaces/'+personal_gp['workspaceId'],id='restarted-foreign-personal-workspace'),role='foreign'),-32002,'not_found')
        if position!=len(calls):raise ValueError('unexpected authority readback call')
        for slot,original,workspace_id in [('storeAfter',before,foreign_gp['workspaceId']),('personalStoreAfter',personal_before,personal_gp['workspaceId'])]:
            final=need(slot,{'action':'observe-stores','slot':slot,'targetId':target_id,'deploymentId':expected['deploymentId'],'workspaceId':workspace_id})
            parsed=observation(final,target_id,expected['deploymentId'],workspace_id)
            if not wire.same_json(parsed,original['stores']) or not wire.same_json(final['succeededWorkflowHistory'],original['succeededWorkflowHistory']):
                raise ValueError('actual persisted authority resources changed across restart')
            if not instant(original['completedAt'])<=instant(semantic_observations['runtimeRestarted']['startedAt'])<=instant(final['startedAt']):
                raise ValueError('independent authority stores do not bracket actual restart')
        return {'action':'complete','facts':facts}
    except semantic.Pending as pending:return pending.value
