"""Complete explicitly offline two-client vectors, NEVER candidate evidence.

Normal fixture/oracle implementations are exercised through controlled API/store
adapters. Expected product DTOs follow canonical source contracts independently;
no authority boolean is stubbed and no production client manifest is replaced.
"""
from __future__ import annotations
import base64
import copy
from datetime import datetime,timedelta,timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
import mcp_deployment_workspace_cohort as full
import mcp_deployment_workspace_authority as authority
import mcp_deployment_workspace_validation as semantic
import mcp_deployment_workspace_validation_controls as legacy
import mcp_deployment_identity_role as role
import mcp_managed_identity_fixture as managed
import mcp_managed_identity_fixture_controls as keys
from mcp_deployment_identity_role_controls import OfflineRoleApi
from mcp_deployment_identity_configuration import declaration,digest,ENV
from mcp_render_validation_controls import FIXTURE,bootstrap,runtime
from mcp_map_package_validation_controls import bind_launch,responses as map_responses

BASE=datetime(2026,10,20,tzinfo=timezone.utc)
NONCE='abcdef123456'
SERVICE='allocated-service-id'
CLUSTER={'clusterId':'1234567','database':'honua_test','user':'honua'}
BASE_GP=legacy.prerequisite
BASE_STORES=legacy.stores
BASE_REBIND=legacy.rebind
BASE_TOOL=legacy.tool_output

def at(seconds):return (BASE+timedelta(seconds=seconds)).isoformat()
def guid(label):return str(uuid.uuid5(uuid.NAMESPACE_URL,'explicit-offline-cohort:'+label))
def dep(label):return 'dep-'+hashlib.sha256(label.encode()).hexdigest()[:32]
def encode(raw):return base64.b64encode(raw).decode(),'sha256:'+hashlib.sha256(raw).hexdigest()
def replace(value,changes):
    if isinstance(value,dict):return {key:replace(item,changes) for key,item in value.items()}
    if isinstance(value,list):return [replace(item,changes) for item in value]
    if isinstance(value,str):return changes.get(value,value)
    return value


def bind(call,selector,identity,tenant,label,seconds):
    call=copy.deepcopy(call)
    BASE_REBIND({'calls':[call]},selector)
    wire=call['wire'];session='sha256:'+hashlib.sha256(('offline-session:'+selector+':'+label).encode()).hexdigest()
    wire['expectedCredentialFingerprint']=identity['credentialFingerprint']
    wire['initialize']['response']['sessionSha256']=session
    wire['invocation']['request']['sessionSha256']=session
    for phase in ('initialize','invocation'):
        wire[phase]['request']['credentialFingerprint']=identity['credentialFingerprint'] if call['authenticated'] else None
        if tenant is not None:wire[phase]['request']['tenantId']=tenant
    if tenant is not None:wire['expectedTenantId']=tenant
    if selector=='sdk':call['client']['sessionSha256']=session
    bind_launch(call['client'],wire,selector)
    if tenant is not None:call['client']['launch']['request']['tenantId']=tenant
    call['startedAt'],call['completedAt']=at(seconds),at(seconds+.01)
    return call


def gp(authored,identity,tenant,requested_tenant,label,seconds):
    value=BASE_GP(authored);old=value['jobId'];job='job-'+hashlib.sha256(label.encode()).hexdigest()[:24]
    value.update(workspaceId='workspace-'+hashlib.sha256(label.encode()).hexdigest()[:20],jobId=job,
        ownerId=identity['workspaceOwnerId'],scopeId=tenant)
    for index,entry in enumerate([value['submitted'],*value['polls'],value['output']]):
        entry['path']=entry['path'].replace(old,job)
        body=json.loads(base64.b64decode(entry['responseBodyBase64']))
        if body.get('jobId')==old:body['jobId']=job
        raw=json.dumps(body).encode();entry['responseBodyBase64'],entry['responseBodySha256']=encode(raw)
        entry.update(credentialFingerprint=identity['credentialFingerprint'],tenantId=requested_tenant,
            startedAt=at(seconds+index),completedAt=at(seconds+index+.1))
    claims=[{'type':'api_key_id','value':identity['keyId']},{'type':'api_key_name','value':identity.get('keyName','explicit-offline-managed-key')},
        *({'type':'permission','value':permission} for permission in identity['permissions']),
        {'type':'http://schemas.microsoft.com/ws/2008/06/identity/claims/role','value':'admin' if identity['permissions']==['admin'] else 'scoped-api-key'}]
    security={'principalId':identity['canonicalActorId'],'tenantId':tenant,'ownerActorId':identity['canonicalActorId'],
        'workspaceOwnerId':identity['workspaceOwnerId'],'roleClaimType':'http://schemas.microsoft.com/ws/2008/06/identity/claims/role','claims':claims}
    raw=json.dumps({'operationId':job,'status':3,'audit':{'requestedBy':identity['canonicalActorId'],'submitterSecurityContext':security}}).encode()
    value['durableJobBytesBase64'],value['durableJobSha256']=encode(raw);value['submitterSecurityContext']=security
    return value


def stores(authored,expected,workspace,seconds,owner,tenant):
    value=BASE_STORES(authored,expected,workspace,seconds)
    value=replace(value,{'controlled-owner':owner,'controlled-tenant':tenant})
    value['stores']['databaseIdentity']=copy.deepcopy(CLUSTER)
    value['stores']['workspace']['owner_id']=workspace['ownerId'];value['stores']['workspace']['scope_id']=workspace['scopeId']
    value['stores']['workspace']['created_at']=workspace['submitted']['completedAt'];value['stores']['workspace']['uri']='honua://workspace-data/'+workspace['workspaceId']
    value['stores']['artifacts'][0]['created_at']=workspace['polls'][-1]['completedAt']
    return value


def deployment_body(expected,stores_value):
    document=stores_value['deployment']['document'];source,target=document['source'],document['target']
    return {'deploymentId':expected['deploymentId'],'resourceUri':'honua://deployments/'+expected['deploymentId'],
        'status':'Active','publicationState':'Published','rolloutState':'Promoted','runtimeHealth':'Unknown',
        'sourceKind':'StudioPublishedVersion','sourceId':source['sourceId'],'targetId':target['targetId'],
        'targetKind':target['kind'],'hostingMode':target['hostingMode'],'routePrefix':target['routePrefix'],
        **{key:source[key] for key in ('studioItemId','studioVersionId','studioContentHash','studioOwnerId','studioPublicationRequestId')},
        'createdAt':document['createdAt'],'updatedAt':document['updatedAt'],
        'etag':f'W/"{legacy.store.ticks(document["updatedAt"]):x}-Active"','transitions':document['transitions']}

def workspace_body(stores_value):
    row=stores_value['workspace'];return {'workspaceId':row['workspace_id'],'kind':'scratch','label':row['label'],
        'uri':row['uri'],'expiresAt':row['expires_at'],'lifecycleState':'active'}
def resource(uri,value):return {'contents':[{'uri':uri,'mimeType':'application/json','text':json.dumps(value)}]}
def refusal():return {'error':{'code':-32002,'message':'Explicit offline resource denial','data':{'code':'not_found'}}}
def scoped(message):return legacy.tool_output({'status':'error','code':'permission_denied','message':message,
    'error':{'kind':'AuthorizationDenied','message':message}},True)


class ClockRoleApi(OfflineRoleApi):
    def __init__(self,clock):super().__init__();self.clock=clock
    def open(self,request,timeout):
        response=super().open(request,timeout)
        if request.method=='DELETE':self.role['deletedAt']=self.clock()
        return response
    def observe(self,identifier):return {**super().observe(identifier),'startedAt':self.clock()}


def full_controls():
    selected=runtime();clock=[BASE];clock_string=lambda:clock[0].isoformat()
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return clock[0] if tz is not None else clock[0].replace(tzinfo=None)
    configuration=declaration('mcp-deployment-'+NONCE)
    observed_config={'schema':'honua.mcp-deployment-configuration-observation/v1','declarationSha256':digest(configuration),
        'containerId':selected['containerId'],'imageId':selected['imageId'],'environment':dict(ENV),'observedAt':at(0)}
    api=ClockRoleApi(clock_string)
    role_fixture=role.ManagedRoleFixture(admin_credential='controlled-private-admin',configuration=configuration,
        configuration_observation=observed_config,inspect_owned=runtime,observe=api.observe)
    state={'schema':'honua.mcp-deployment-managed-cohort/v1','nonce':NONCE,'serviceId':SERVICE,
        'candidateImage':selected['imageReference'],'candidate':{key:selected[key] for key in ('containerId','imageId','candidateSourceSha')},
        'credentialSalt':keys.SALT,'configuration':configuration,'configurationObservation':observed_config,
        'roleReceipt':role_fixture.receipt,'keys':{},'failures':[],'cleanupComplete':False}
    members={};pending=[]
    original_advance=semantic.advance;original_tool=legacy.tool_output;original_rebind=legacy.rebind
    original_gp=legacy.prerequisite;original_stores=legacy.stores;original_history=legacy.history;original_proposal=legacy.proposal_state
    with tempfile.TemporaryDirectory() as directory,patch.object(role,'build_opener',return_value=api),patch.object(role,'stamp',side_effect=clock_string),patch.object(managed,'stamp',side_effect=clock_string),patch.object(managed,'datetime',Clock),patch.object(keys,'datetime',Clock):
        role_fixture.create();clock[0]=BASE+timedelta(seconds=1)
        for selector in ('sdk','inspector'):
            state['keys'][selector]={}
            for name,profile in [('primaryReceipt','admin'),('foreignReceipt','workspace-reader')]:
                index=len(pending)+1;key_id=f'{index:08x}-1111-4111-8111-111111111111'
                secret='hnua_'+base64.urlsafe_b64encode(bytes([index])*32).decode().rstrip('=')
                normal=keys.OfflineNormalApi();identity=managed.ManagedIdentityFixture(admin_credential=keys.PRIVATE_ADMIN,
                    private_path=Path(directory)/f'{index}.private.json',receipt_path=Path(directory)/f'{index}.receipt.json',
                    inspect_owned=runtime,redis=normal.redis,credential_salt=keys.SALT,profile=profile,
                    **({'service_id':SERVICE} if profile=='workspace-reader' else {}))
                with patch.object(keys,'KEY_ID',key_id),patch.object(keys,'PRIVATE_KEY',secret),patch.object(managed,'build_opener',return_value=normal):identity.create()
                pending.append((identity,normal,key_id,secret));state['keys'][selector][name]=identity.receipt
        preparation=copy.deepcopy(state)
        with patch.object(legacy,'stamp',side_effect=at):frozen_history=original_history()
        for selector,offset in [('sdk',0),('inspector',1000)]:
            canonical,version=('Official MCP TypeScript SDK','1.32.1') if selector=='sdk' else ('MCP Inspector','2.10.1')
            pair=preparation['keys'][selector];primary={**pair['primaryReceipt']['identity'],'keyName':pair['primaryReceipt']['name']};foreign={**pair['foreignReceipt']['identity'],'keyName':pair['foreignReceipt']['name']}
            before=runtime()
            if selector=='inspector':before['startedAt']=at(300)
            value={'schema':full.SCHEMA,'nonce':NONCE,'canonicalClient':canonical,'clientVersion':version,'targetId':'owned-target',
                'identityPreparation':copy.deepcopy(preparation),'calls':[],
                'authority':{'schema':authority.SCHEMA,'callsBefore':[],'callsAfter':[],'foreignWorkspacePrerequisite':None,
                    'personalWorkspacePrerequisite':None,'storeBefore':None,'personalStoreBefore':None,'storeAfter':None,'personalStoreAfter':None,
                    'limitedRead':None,'runtimeBefore':copy.deepcopy(before),'runtimeAfter':None},
                'semantic':{'nonce':NONCE,'canonicalClient':canonical,'clientVersion':version,'targetId':'owned-target','calls':[],
                    'workspacePrerequisite':None,'storeBefore':None,'storeAfter':None,'proposalBefore':None,'proposalAfter':None,
                    'runtimeBefore':copy.deepcopy(before),'runtimeRestarted':None,'runtimeAfter':copy.deepcopy(before)}}
            context={}
            with patch.object(legacy,'stamp',side_effect=lambda seconds:at(offset+seconds)),patch.object(legacy,'history',side_effect=lambda:copy.deepcopy(frozen_history)):
                authority_controls(value,selector,offset,primary,foreign,frozen_history,'before',context)
            value['semantic']=semantic_controls(selector,offset,primary,foreign,before,frozen_history,original_advance)
            value['authority']['runtimeAfter']=copy.deepcopy(value['semantic']['runtimeAfter'])
            with patch.object(legacy,'stamp',side_effect=lambda seconds:at(offset+seconds)),patch.object(legacy,'history',side_effect=lambda:copy.deepcopy(frozen_history)):
                authority_controls(value,selector,offset,primary,foreign,frozen_history,'after',context)
            value['calls']=[*value['authority']['callsBefore'],*value['semantic']['calls'],*value['authority']['callsAfter']]
            members[selector]={'schema':'honua.mcp-deployment-client-journey/v1','canonicalClient':canonical,'cohortId':NONCE,
                'candidate':copy.deepcopy(state['candidate']),'configurationSha256':digest(configuration),
                'startedAt':at(offset+2),'completedAt':at(offset+730),'observations':value}
        clock[0]=BASE+timedelta(seconds=1800);state['cleanupStartedAt']=clock_string()
        for index,(identity,normal,key_id,secret) in enumerate(pending):
            clock[0]=BASE+timedelta(seconds=1810+index)
            with patch.object(keys,'KEY_ID',key_id),patch.object(keys,'PRIVATE_KEY',secret),patch.object(managed,'build_opener',return_value=normal):identity.revoke()
        role_fixture.bind_journeys([json.dumps(members[selector]).encode() for selector in ('sdk','inspector')])
        clock[0]=BASE+timedelta(seconds=1820);role_fixture.revoke();state['cleanupComplete']=True
    return {'terminal':state,'members':{selector:envelope['observations'] for selector,envelope in members.items()}}


def semantic_controls(selector,offset,primary,foreign,before,frozen_history,original_advance):
    label=selector+'-primary';tenant='mcp-deployment-'+NONCE
    changes={'11111111-1111-4111-8111-111111111111':guid(label+'-draft'),
        '22222222-2222-4222-8222-222222222222':guid(label+'-item'),
        '44444444-4444-4444-8444-444444444444':guid(label+'-version'),
        'dep-'+'6'*32:dep(label),'honua://deployments/dep-'+'6'*32:'honua://deployments/'+dep(label),'map_controlled':'map_'+hashlib.sha256(label.encode()).hexdigest()[:20],
        'honua://map-packages/map_controlled':'honua://map-packages/map_'+hashlib.sha256(label.encode()).hexdigest()[:20]}
    def translated_result(value):
        result=replace(value,changes)
        for item in result.get('content',[]) if isinstance(result,dict) else []:
            if isinstance(item,dict) and item.get('type')=='text':
                try:item['text']=json.dumps(replace(json.loads(item['text']),changes))
                except (ValueError,TypeError):pass
        for item in result.get('contents',[]) if isinstance(result,dict) else []:
            item['text']=json.dumps(replace(json.loads(item['text']),changes))
        return result
    def output(value,error=False):return BASE_TOOL(replace(value,changes),error)
    def setup(authored):return gp(authored,primary,tenant,tenant,label,offset+55)
    def observed_stores(authored,expected,workspace,seconds):
        selected=copy.deepcopy(expected);selected['deploymentId']=dep(label)
        return stores(authored,selected,workspace,70 if seconds==40 else 700,primary['studioActorId'],tenant)
    def proposal_state(key):
        value=copy.deepcopy(legacy_proposal_base(key));identifier='proposal-'+hashlib.sha256(f'{len(tenant)}:{tenant}:Deploy:control-plane.deploy:{key}'.encode()).hexdigest()[:32]
        value['proposalId']=identifier;value['record'].update(proposalId=identifier,tenantId=tenant,
            requestedBy=primary['canonicalActorId'],requestedByAgent='agent:'+primary['canonicalActorId'])
        value['audit'].update(tenantId=tenant,requestedBy=primary['canonicalActorId']);return value
    def rebound(value,selected_selector='sdk'):
        for index,call in enumerate(value['calls']):
            call['result']=translated_result(call['result'])
            if call['id'] in ('current-deploy','prior-deploy'):
                payload=call['result']['structuredContent'];source=payload['evidencePosture']['sources'][0]
                expiry=datetime.fromisoformat(source['observedAt'])+timedelta(seconds=300)
                source['validUntil']=expiry.isoformat();stale=expiry<BASE+timedelta(seconds=offset+75)
                source['reasonCodes']=['stale'] if stale else [];source['completeness']='unavailable' if stale else 'complete'
                payload['evidencePosture']['status']=source['completeness'];call['result']=BASE_TOOL(payload)
            identity=foreign if call.get('credentialRole')=='foreign' else primary
            selected_tenant=tenant if call['authenticated'] and call.get('credentialRole','primary')=='primary' else None
            seconds=offset+(310+index*.1 if value['runtimeRestarted'] is not None and index>=12 else 75+index*.1 if index>=6 else 50+index*.1)
            call.update(bind(call,selected_selector,identity,selected_tenant,label+':'+str(index),seconds))
        return value
    with patch.object(legacy,'stamp',side_effect=lambda seconds:at(offset+seconds)),patch.object(legacy,'runtime',side_effect=lambda:copy.deepcopy(before)),patch.object(legacy,'history',side_effect=lambda:copy.deepcopy(frozen_history)),patch.object(legacy,'tool_output',side_effect=output),patch.object(legacy,'prerequisite',side_effect=setup),patch.object(legacy,'stores',side_effect=observed_stores),patch.object(legacy,'proposal_state',side_effect=proposal_state),patch.object(legacy,'rebind',side_effect=rebound),patch.object(semantic,'advance',side_effect=lambda fixture,boot,value:original_advance(fixture,boot,value,tenant_id=tenant)):
        return legacy.controls(selector,nonce=NONCE)

legacy_proposal_base=legacy.proposal_state


def authority_controls(value,selector,offset,primary,foreign,frozen_history,stage,context):
    auth=value['authority'];canonical=value['canonicalClient'];foreign_authored=semantic.scenario(FIXTURE,bootstrap(),NONCE,canonical)
    foreign_authored.update(packageKey=foreign_authored['packageKey']+'-foreign',route=foreign_authored['route']+'-foreign',workspaceLabel=foreign_authored['workspaceLabel']+'-foreign')
    personal_authored=semantic.scenario(FIXTURE,bootstrap(),NONCE,canonical);personal_authored['workspaceLabel']+='-personal'
    label=selector+'-foreign'
    for _ in range(64):
        action=authority.advance(FIXTURE,bootstrap(),auth,nonce=NONCE,canonical=canonical,version=value['clientVersion'],target_id='owned-target',
            preparation=value['identityPreparation'],stage=stage,semantic_observations=value['semantic'] if stage=='after' else None)
        if action['action']=='complete':return
        if action['action']=='create-workspace':
            personal=action['slot']=='personalWorkspacePrerequisite'
            auth[action['slot']]=gp(personal_authored if personal else foreign_authored,primary if personal else foreign,'public','public' if personal else None,
                selector+('-personal' if personal else '-foreign'),offset+(18 if personal else 15))
        elif action['action']=='observe-stores':
            personal=action['slot'].startswith('personal');gp_value=auth['personalWorkspacePrerequisite' if personal else 'foreignWorkspacePrerequisite']
            seconds=(722 if personal else 720) if action['slot'].endswith('After') else (26 if personal else 25)
            auth[action['slot']]=stores(personal_authored if personal else foreign_authored,context['expected'],gp_value,seconds,foreign['studioActorId'],'public')
        elif action['action']=='limited-data-read':
            import mcp_ops_events_data_read as data
            response=json.dumps({'count':len(json.loads(FIXTURE)['area']['features']),'exceededTransferLimit':False})
            auth['limitedRead']={'schema':data.SCHEMA,'method':'GET','url':data.data_url(FIXTURE,bootstrap()),'status':200,
                'responseText':response,'responseSha256':'sha256:'+hashlib.sha256(response.encode()).hexdigest(),
                'credentialFingerprint':foreign['credentialFingerprint'],'runtimeBefore':copy.deepcopy(auth['runtimeBefore']),
                'runtimeAfter':copy.deepcopy(auth['runtimeBefore']),'startedAt':at(offset+33),'completedAt':at(offset+33.1)}
        elif action['action']=='invoke':
            call=copy.deepcopy(action['call']);identifier=call['id'];arguments=call['params'].get('arguments',{})
            if identifier=='foreign-fixture-map':
                result=copy.deepcopy(map_responses()[0]);body=result['structuredContent'];identifier_map='map_'+hashlib.sha256(label.encode()).hexdigest()[:20]
                body.update(packageId=identifier_map,resourceUri='honua://map-packages/'+identifier_map)
                body['package'].update(mapPackageId=identifier_map,createdAt=at(offset+9));result=BASE_TOOL(body)
            elif identifier=='foreign-fixture-draft':result=BASE_TOOL({'draftId':guid(label+'-draft'),'generation':1,'operation':legacy.operation()})
            elif identifier=='foreign-fixture-update':
                context['body']=copy.deepcopy(arguments['body']);result=BASE_TOOL({'draftId':guid(label+'-draft'),'generation':2,'operation':legacy.operation()})
            elif identifier=='foreign-fixture-validate':result=BASE_TOOL({'status':'valid','unsupportedCapabilities':[],'diagnostics':[],'generatedAt':at(offset+10)})
            elif identifier=='foreign-fixture-save':
                raw=json.dumps({'family':'map','body':context['body']},separators=(',',':')).encode();digest_value=hashlib.sha256(raw).hexdigest()
                result=BASE_TOOL({'operation':legacy.operation(),'version':{'itemId':guid(label+'-item'),'versionId':guid(label+'-version'),'contentHash':digest_value}})
            elif identifier=='foreign-fixture-publication':
                context['expected']={'deploymentId':dep(label),'studioItemId':arguments['itemId'],'studioVersionId':arguments['versionId'],
                    'studioContentHash':arguments['contentHash'],'route':arguments['route'],'mapBody':context['body']}
                result=BASE_TOOL({'status':'Published','humanConfirmationRequired':False,'operation':legacy.operation(),'deploymentId':dep(label),
                    'deploymentResourceUri':'honua://deployments/'+dep(label),'shareUrl':'/api/v1/studio/published'+foreign_authored['route']})
            elif identifier in ('foreign-own-deployment','restarted-foreign-own-deployment'):
                result=resource(call['params']['uri'],deployment_body(context['expected'],auth['storeBefore']['stores']))
            elif identifier in ('foreign-own-workspace','restarted-foreign-own-workspace'):
                result=resource(call['params']['uri'],workspace_body(auth['storeBefore']['stores']))
            elif identifier in ('personal-workspace-owner','restarted-personal-workspace-owner'):
                result=resource(call['params']['uri'],workspace_body(auth['personalStoreBefore']['stores']))
            elif identifier=='foreign-deploy-role-denial':result=scoped('Caller is not authorized to read platform operations.')
            elif identifier=='foreign-rollback-role-denial':result=scoped('Caller is not authorized to propose platform mutations.')
            elif identifier in ('foreign-personal-workspace','restarted-foreign-personal-workspace'):result=refusal()
            else:raise AssertionError('unknown explicit offline authority request '+identifier)
            call['result']=result
            sequence=auth['callsBefore' if stage=='before' else 'callsAfter'];index=len(sequence)
            times={'foreign-own-deployment':30,'foreign-own-workspace':31,'personal-workspace-owner':32,
                'foreign-deploy-role-denial':34,'foreign-rollback-role-denial':35,'foreign-personal-workspace':36}
            seconds=offset+(710+index if stage=='after' else times.get(identifier,10+index*.1))
            identity=foreign if call['credentialRole']=='foreign' else primary
            sequence.append(bind(call,selector,identity,call.get('tenantId'),label+':'+stage+':'+str(index),seconds))
        else:raise AssertionError('unknown controlled authority action')
    raise AssertionError('controlled authority action bound exceeded')


def rehash_member(vector,selector,mutation):
    """Mutate original retained bytes, preserving their real integrity contract."""
    canonical=full.CANONICAL[selector]
    binding=next(item for item in vector['terminal']['roleReceipt']['cohort']['journeys']
        if item['canonicalClient']==canonical)
    envelope=json.loads(base64.b64decode(binding['journeyBytesBase64']))
    mutation(envelope)
    raw=json.dumps(envelope).encode()
    binding['journeyBytesBase64'],binding['journeySha256']=encode(raw)
    for field in ('canonicalClient','startedAt','completedAt'):binding[field]=envelope[field]
    vector['members'][selector]=copy.deepcopy(envelope['observations'])


class FullOriginalCohortControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.golden=full_controls()

    def test_complete_both_original_clients_join_real_oracles(self):
        for selector in ('sdk','inspector'):
            with self.subTest(client=selector):
                result=full.judge(FIXTURE,bootstrap(),self.golden['members'][selector],self.golden['terminal'])
                self.assertFalse(result['semanticOnly'])
                for field in ('normalManagedIdentityJoined','originalTwoClientCohortJoined',
                        'ownResourceEligibility','tenantOwnerIsolation','sameTenantPersonalOwnerRefusal',
                        'scopedPlatformRoleRefusal','limitedDataReadPositive'):
                    self.assertIs(result[field],True)

    def test_missing_other_original_client_refused(self):
        vector=copy.deepcopy(self.golden)
        vector['terminal']['roleReceipt']['cohort']['journeys'].pop()
        with self.assertRaises(ValueError):
            full.judge(FIXTURE,bootstrap(),vector['members']['sdk'],vector['terminal'])

    def test_rehashed_original_preparation_tamper_refused(self):
        vector=copy.deepcopy(self.golden)
        rehash_member(vector,'inspector',lambda envelope:envelope['observations']['identityPreparation'].update(failures=['retained failure']))
        with self.assertRaisesRegex(ValueError,'exact managed provisioning state'):
            full.judge(FIXTURE,bootstrap(),vector['members']['sdk'],vector['terminal'])

    def test_semantic_component_is_not_full_original_observation(self):
        with self.assertRaisesRegex(ValueError,'complete original client observations'):
            full.judge(FIXTURE,bootstrap(),self.golden['members']['sdk']['semantic'],self.golden['terminal'])


if __name__=='__main__':unittest.main()
