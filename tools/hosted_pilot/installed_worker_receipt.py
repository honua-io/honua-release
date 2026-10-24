"""Identity-only originals from checked worker apply; never runtime activation.

This module does not accept caller-declared slot identities. Consumers authenticate
an immutable original output, its successful plan/apply/source, retained MCP base,
and fresh ECS/Step Functions identities before using the projected slot.
"""
from __future__ import annotations
import base64
import hashlib
import json
from pathlib import Path
import re
import os
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from agent_e2e_pack import canonical_digest
from agent_e2e_worker_lifecycle import terraform_definition_digest
from agent_e2e_interactive_budget import validate_retained_funding
from enrollment_receipt import original_review, positive_integer, private_bytes
from fixture_handoff import require, encoded
from human_memberships import github_json

SCHEMA = 'honua.private-installed-worker-installation/v1'
WORKFLOW = '.github/workflows/hosted-pilot-deploy.yml'
ROOT_PATH = 'certification/hosted-pilot/worker'
REPOSITORY = 'honua-io/honua-release'
LIMIT = 2*1024*1024
SLOT_FIELDS = {'schema','candidateKey','runtimeId','slotId','region','stateMachineArn','stateMachineRoleArn',
    'definitionDigest','clusterArn','taskDefinitionArn','taskRoleArn','claimTable','originalFundingExpiresEpoch',
    'maxExecutionSeconds','commandQueueUrl','replyQueueUrl','mcpSecret','image','liability'}


def descriptor_shape(value):
    require(isinstance(value,dict) and set(value)==SLOT_FIELDS and value.get('schema')=='honua.installed-worker-slot/v1' and
        isinstance(value['mcpSecret'],dict) and set(value['mcpSecret'])=={'secretArn','versionId'} and
        isinstance(value['liability'],dict) and set(value['liability'])=={'basisDigest','maximumInfrastructureUsd','maximumCleanupUsd'},
        'installed output must be a closed identity-only descriptor')


def digest(raw):
    return 'sha256:'+hashlib.sha256(raw).hexdigest()


def reference(value):
    require(isinstance(value,dict) and set(value) == {'bucket','key','versionId','sha256'} and
        isinstance(value['bucket'],str) and isinstance(value['key'],str) and
        '..' not in value['key'].split('/') and isinstance(value['versionId'],str) and
        value['versionId'] not in ('','null') and len(value['versionId']) <= 1024 and
        re.fullmatch(r'sha256:[a-f0-9]{64}',value['sha256']), 'original installation object reference missing')


def read_original(s3, value, *, guard):
    reference(value); guard()
    observed = s3.get_object(Bucket=value['bucket'],Key=value['key'],VersionId=value['versionId'])
    require(observed.get('VersionId') == value['versionId'] and observed.get('ServerSideEncryption') == 'AES256',
        'original installation object encryption/version differs')
    raw = observed['Body'].read(LIMIT+1); guard()
    require(len(raw) <= LIMIT and digest(raw) == value['sha256'], 'original installation object bytes differ')
    return json.loads(raw), raw


def pending_binding(payload, configuration):
    selected = configuration.get('mcp_bootstrap_binding')
    require(isinstance(selected,dict) and set(selected) == {'key','sha256'} and
            re.fullmatch(r'sha256:[a-f0-9]{64}',str(selected.get('sha256'))), 'original pending MCP reference required')
    row = payload.get('scenarioOperations',{}).get(selected['key'])
    require(isinstance(row,dict) and canonical_digest(row) == selected['sha256'] and
        row.get('producer') == 'private-installed-worker-mcp' and row.get('candidateKey') == configuration['candidate_digest'],
        'installed worker MCP is not an authenticated original base')
    value = row['value']; slot = configuration['installed_slot']
    require(value.get('schema') == 'honua.private-installed-worker-mcp-binding/v1' and
        value.get('status') == 'registration-pending' and value.get('activation') is False and value.get('qualification') is False and
        value.get('runtimeId') == slot['runtime_id'] and value.get('slotId') == slot['slot_id'] == configuration['attempt_id'] and
        value.get('originalFundingExpiresEpoch') == slot['original_funding_expires_epoch'] and
        value.get('secretArn') == configuration['mcp_configuration']['secret_arn'] and
        value.get('versionId') == configuration['mcp_configuration']['version_id'],
        'installed worker substituted native base identity, version or original expiry')
    for field in ('issuedAgentKey','nativeBootstrap'):
        ref = value.get(field,{})
        original = payload.get('scenarioOperations',{}).get(ref.get('key'))
        require(set(ref) == {'key','sha256'} and isinstance(original,dict) and canonical_digest(original) == ref['sha256'] and
            original.get('producer') == 'private-native-bootstrap' and original.get('candidateKey') == configuration['candidate_digest'],
            'installed MCP lost its original native bootstrap provenance')
    prefix = selected['key'].removesuffix('/completed')
    require(prefix != selected['key'], 'pending MCP reference is not its original completion')
    intent = payload['scenarioOperations'].get(prefix+'/intent')
    require(isinstance(intent,dict) and intent.get('producer') == 'private-installed-worker-mcp' and
        intent.get('candidateKey') == configuration['candidate_digest'] and
        canonical_digest(intent.get('value')) == value.get('publicBindingDigest'), 'pending MCP original intent/source differs')
    public = intent['value']; original_configuration = public['configuration']
    from installed_mcp_configuration import validate as validate_mcp_configuration
    validate_mcp_configuration(original_configuration)
    require(all(original_configuration[k] == value[k] for k in ('runtimeId','slotId','targetId','originalFundingExpiresEpoch')) and
        original_configuration['secretArn'] == value['secretArn'] and
        public['issuedAgentKey'] == value['issuedAgentKey'] and public['nativeBootstrap'] == value['nativeBootstrap'],
        'pending MCP source identity differs from original completion')
    target_body = None
    for field,path,stage in (
        ('targetReference','/api/v1/agent/target','target'),
        ('effectiveReference','/api/v1/admin/api-keys/'+value['principal']['studioOwnerId']+'/effective-permissions','effective')):
        ref=value.get(field,{})
        observed=payload['scenarioOperations'].get(ref.get('key'))
        require(set(ref)=={'key','sha256'} and isinstance(observed,dict) and canonical_digest(observed)==ref['sha256'] and
            observed.get('producer')=='private-installed-worker-mcp' and observed.get('candidateKey')==configuration['candidate_digest'] and
            ref['key'].startswith(prefix+'/observation/') and ref['key'].endswith('/'+stage), 'pending MCP native original differs')
        observation=observed['value']; answer=observation['response']
        require(observation.get('endpoint')==original_configuration['endpoint'].removesuffix('/mcp') and
            observation.get('path')==path and observation.get('credentialKind')=='api-key' and
            observation.get('issuedAgentKey')==value['issuedAgentKey'] and answer.get('httpStatus')==200,
            'pending MCP native metadata transport differs')
        raw=base64.b64decode(answer['bodyBase64'],validate=True)
        require(len(raw)<=1024*1024 and len(raw)==answer['bodyBytes'] and digest(raw)==answer['bodySha256'] and
            json.loads(raw)==answer['body'], 'pending MCP original native body bytes differ')
        if stage=='target':
            target_body=answer['body']
            require(target_body.get('targetId')==value['targetId'] and target_body.get('principal')==value['principal'] and
                value['principal'].get('authenticationKind')=='api-key' and value['principal'].get('isHumanSession') is False,
                'pending MCP original target/actor differs')
        else:
            data=answer['body'].get('data',{})
            issued=payload['scenarioOperations'][value['issuedAgentKey']['key']]['value']
            require(answer['body'].get('success') is True and data.get('id')==issued['apiKeyId']==value['principal']['studioOwnerId'] and
                data.get('status')=='active' and data.get('canAuthenticate') is True and
                sorted(data.get('permissions',[]))==sorted(issued['permissions']), 'pending MCP original effective key differs')
    return value | {'_nativeEndpoint':original_configuration['endpoint'].removesuffix('/mcp')}


def retained_put(path, *, bucket, key, aws):
    """One create-only write; lost ACK is read-only original-byte recovery."""
    raw = Path(path).read_bytes()
    try:
        result = aws('s3api','put-object','--bucket',bucket,'--key',key,'--body',str(path),
            '--if-none-match','*','--server-side-encryption','AES256')
    except (ValueError,OSError):
        result = {}
    readback = Path(path).with_suffix('.readback')
    args = ['s3api','get-object','--bucket',bucket,'--key',key]
    if result.get('VersionId') not in (None,'null'): args += ['--version-id',result['VersionId']]
    observed = aws(*args,str(readback)); readback.chmod(0o600)
    require(observed.get('VersionId') not in (None,'','null') and observed.get('ServerSideEncryption') == 'AES256' and
        (not result.get('VersionId') or result['VersionId'] == observed['VersionId']) and readback.read_bytes() == raw,
        'installed worker original output write/readback differs; preserve unknown effects')
    return {'bucket':bucket,'key':key,'versionId':observed['VersionId'],'sha256':digest(raw)}


def emit(*, cfg, root, outputs, review, work, env, aws):
    """Called only after exact worker apply AND independent native readiness."""
    configuration = json.loads(Path(cfg).read_bytes())
    if configuration.get('installed_slot') is None: return None
    raw = (work/'review.json').read_bytes()
    path = str(Path(cfg).relative_to(root))
    original_review(review,raw,Path(cfg).read_bytes(),path,env,ROOT_PATH)
    from worker_allowance import verify
    accounting = verify(configuration,work,aws=aws,require=require,include_payload=True)
    payload = accounting['payload']
    validate_retained_funding(payload)
    slot = configuration['installed_slot']
    require(payload['ledger'].get('interactiveAccounting',{}).get('originalExpiresEpoch') == slot['original_funding_expires_epoch'],
        'installation changed original authenticated funding expiry')
    base = pending_binding(payload,configuration)
    descriptor = outputs['installed_worker_slot']['value']
    descriptor_shape(descriptor)
    require(descriptor['candidateKey'] == configuration['candidate_digest'] and descriptor['runtimeId'] == slot['runtime_id'] and
        descriptor['slotId'] == slot['slot_id'] == configuration['attempt_id'] and descriptor['image'] == configuration['worker_image'] and
        descriptor['originalFundingExpiresEpoch'] == slot['original_funding_expires_epoch'] and
        descriptor['mcpSecret'] == {'secretArn':base['secretArn'],'versionId':base['versionId']},
        'applied installed worker descriptor differs from original configuration')
    readiness_raw = (work/'readiness.json').read_bytes(); readiness = json.loads(readiness_raw)
    require(readiness.get('schema') == 'honua.hosted-worker-readiness/v1' and readiness.get('source_sha') == env['GITHUB_SHA'] and
        readiness.get('run_id') == env['GITHUB_RUN_ID'] and readiness.get('attempt_id') == configuration['attempt_id'] and
        readiness.get('mode') == 'installed-slot' and readiness.get('qualification') is False and all(readiness.get(k) is True for k in
        ('registered_task_definition_verified','immutable_agent_mcp_binding_verified','worker_role_queue_only',
         'independent_expiry_schedule_verified','cleanup_monitor_confirmed')), 'actual independent installed readiness is missing')
    producer = {'repository':REPOSITORY,'workflow':WORKFLOW,'sourceSha':env['GITHUB_SHA'],
        'runId':positive_integer(env['GITHUB_RUN_ID']),'runAttempt':positive_integer(env['GITHUB_RUN_ATTEMPT']),
        'jobKey':'apply','ref':env['GITHUB_REF'],'event':env['GITHUB_EVENT_NAME']}
    candidate = configuration['candidate_digest']
    prefix = 'agent-e2e/'+candidate[7:]+'/installed-workers/'+slot['runtime_id']+'/'+slot['slot_id']+'/'+str(producer['runId'])+'/'+str(producer['runAttempt'])+'/'
    bucket = configuration['accounting']['state']['bucket']
    review_ref = retained_put(work/'review.json',bucket=bucket,key=prefix+'review.json',aws=aws)
    readiness_ref = retained_put(work/'readiness.json',bucket=bucket,key=prefix+'readiness.json',aws=aws)
    carrier = {'schema':SCHEMA,'accountId':configuration['account_id'],'candidateKey':candidate,
        'runtimeId':slot['runtime_id'],'slotId':slot['slot_id'],'originalFundingExpiresEpoch':slot['original_funding_expires_epoch'],
        'producer':producer,'sourceConfiguration':{'path':path,'sha256':'sha256:'+review['config_sha256']},
        'plan':{'sha256':'sha256:'+review['plan_sha256'],'versionId':review['s3_version_id'],
            'attempt':positive_integer(env['PILOT_PLAN_ATTEMPT']),'reviewSha256':'sha256:'+env['PILOT_REVIEW_SHA256'],
            'reviewArtifactId':positive_integer(env['PILOT_REVIEW_ARTIFACT_ID']),'review':review_ref},
        'descriptor':descriptor,'mcpBootstrap':configuration['mcp_bootstrap_binding'],
        'readiness':readiness_ref,'activation':False,'qualification':False}
    private_bytes(work/'installed-worker-identity.json',encoded(carrier))
    result = retained_put(work/'installed-worker-identity.json',bucket=bucket,key=prefix+'installation.json',aws=aws)
    private_bytes(work/'installed-worker-reference.json',encoded(result))
    return result


def verify_installation(selected, *, durable, runtime_id, target_id, original_expires_epoch,
                        s3, ecs, states, github=github_json, guard):
    """Authenticate original producing jobs/config/plan and actual fixed resources."""
    validate_retained_funding(durable.state)
    require(durable.state['ledger'].get('interactiveAccounting',{}).get('originalExpiresEpoch') == original_expires_epoch,
        'registration requires original authenticated installation expiry')
    expected_bucket = durable.store.bucket
    require(selected.get('bucket') == expected_bucket, 'installation object escaped original accounting custody')
    value,_ = read_original(s3,selected,guard=guard)
    require(isinstance(value,dict) and set(value) == {'schema','accountId','candidateKey','runtimeId','slotId',
        'originalFundingExpiresEpoch','producer','sourceConfiguration','plan','descriptor','mcpBootstrap','readiness','activation','qualification'} and
        value['schema'] == SCHEMA and value['candidateKey'] == durable.store.candidate and value['runtimeId'] == runtime_id and
        value['originalFundingExpiresEpoch'] == original_expires_epoch and value['activation'] is False and value['qualification'] is False,
        'original installation candidate/runtime/expiry or producer shape differs')
    producer,plan,source = value['producer'],value['plan'],value['sourceConfiguration']
    require(set(producer) == {'repository','workflow','sourceSha','runId','runAttempt','jobKey','ref','event'} and
        producer['repository'] == REPOSITORY and producer['workflow'] == WORKFLOW and producer['jobKey'] == 'apply' and
        producer['ref'] == 'refs/heads/trunk' and producer['event'] == 'workflow_dispatch' and
        re.fullmatch(r'[a-f0-9]{40}',producer['sourceSha']) and all(type(producer[k]) is int and producer[k]>0 for k in ('runId','runAttempt')),
        'original installation workflow source identity differs')
    require(set(plan) == {'sha256','versionId','attempt','reviewSha256','reviewArtifactId','review'} and
        type(plan['attempt']) is int and 0 < plan['attempt'] <= producer['runAttempt'] and
        type(plan['reviewArtifactId']) is int and plan['reviewArtifactId'] > 0 and
        set(source) == {'path','sha256'} and re.fullmatch(re.escape(ROOT_PATH)+r'/deployments/[A-Za-z0-9_.-]+\.tfvars\.json',source['path']),
        'original installation source/plan binding differs')
    prefix = 'agent-e2e/'+value['candidateKey'][7:]+'/installed-workers/'+runtime_id+'/'+value['slotId']+'/'+str(producer['runId'])+'/'+str(producer['runAttempt'])+'/'
    require(selected['key'] == prefix+'installation.json' and all(value[k]['bucket'] == expected_bucket for k in ('readiness',)) and
        value['readiness']['key'] == prefix+'readiness.json' and plan['review']['bucket'] == expected_bucket and
        plan['review']['key'] == prefix+'review.json' and plan['review']['sha256'] == plan['reviewSha256'],
        'original installation/review/readiness object escaped exact producing run')
    review,_ = read_original(s3,plan['review'],guard=guard)
    require(review.get('schema') == 'honua.hosted-pilot-saved-plan/v1' and review.get('root_path') == ROOT_PATH and
        review.get('operation') == 'apply' and review.get('context') == {'GITHUB_SHA':producer['sourceSha'],
            'GITHUB_RUN_ID':str(producer['runId']),'GITHUB_RUN_ATTEMPT':str(plan['attempt']),'GITHUB_REPOSITORY':REPOSITORY} and
        review.get('config_path') == source['path'] and 'sha256:'+review.get('config_sha256','') == source['sha256'] and
        'sha256:'+review.get('plan_sha256','') == plan['sha256'] and review.get('s3_version_id') == plan['versionId'],
        'installation was not the original source/config/saved-plan apply')
    proofs = {}
    for attempt,name in {(producer['runAttempt'],'apply'),(plan['attempt'],'plan')}:
        path = 'repos/'+REPOSITORY+'/actions/runs/'+str(producer['runId'])+'/attempts/'+str(attempt)
        guard(); run=github(path); jobs=github(path+'/jobs?per_page=100'); guard()
        require(run.get('id') == producer['runId'] and run.get('run_attempt') == attempt and run.get('head_sha') == producer['sourceSha'] and
            run.get('head_branch') == 'trunk' and run.get('event') == 'workflow_dispatch' and run.get('path','').split('@')[0] == WORKFLOW and
            run.get('repository',{}).get('full_name') == REPOSITORY and run.get('status') == 'completed' and
            (name != 'apply' or run.get('conclusion') == 'success') and type(jobs.get('total_count')) is int and jobs['total_count'] <= 100,
            'original installation run is not successfully completed; visible output is not completed apply')
        matches=[job for job in jobs.get('jobs',[]) if job.get('name') == name]
        require(len(matches)==1 and matches[0].get('run_id')==producer['runId'] and matches[0].get('head_sha')==producer['sourceSha'] and
            matches[0].get('status')=='completed' and matches[0].get('conclusion')=='success', 'original installation plan/apply job did not succeed')
        if name == 'apply':
            for step in ('Verify identity and apply exactly the saved plan','Inspect exact worker image, agent-only secret version and independent cleanup',
                         'Retain installed worker identity-only output'):
                require(sum(row.get('name')==step and row.get('conclusion')=='success' for row in matches[0].get('steps',[]))==1,
                    'original installed apply/readiness/receipt step did not succeed')
        proofs[str(attempt)+'/'+name]={'run':run,'jobs':jobs}
    guard(); ancestor=github('repos/'+REPOSITORY+'/compare/'+producer['sourceSha']+'...trunk'); guard()
    require(ancestor.get('status') in ('ahead','identical') and ancestor.get('merge_base_commit',{}).get('sha')==producer['sourceSha'],
        'installation source is not protected trunk history')
    guard(); original=github('repos/'+REPOSITORY+'/contents/'+source['path']+'?ref='+producer['sourceSha']); guard()
    require(original.get('encoding')=='base64','original installation configuration unavailable')
    raw=base64.b64decode(''.join(original['content'].split()),validate=True)
    require(len(raw)<=LIMIT and digest(raw)==source['sha256'],'original installation source bytes differ')
    configuration=json.loads(raw)
    require(configuration.get('candidate_digest')==value['candidateKey'] and configuration.get('account_id')==value['accountId'] and
        configuration.get('attempt_id')==value['slotId'] and configuration.get('installed_slot',{}).get('runtime_id')==runtime_id and
        configuration.get('mcp_bootstrap_binding')==value['mcpBootstrap'], 'installation configuration substituted candidate/allocation/MCP original')
    require(re.fullmatch('arn:aws:iam::'+re.escape(configuration['account_id'])+r':policy/[A-Za-z0-9+=,.@_/-]+',
            str(configuration.get('permissions_boundary_arn'))),
            'installed worker original source permissions boundary is absent or foreign')
    base=pending_binding(durable.state,configuration)
    require(base.get('targetId')==target_id,'installed pending MCP target differs')
    descriptor=value['descriptor'];slot=configuration['installed_slot']
    descriptor_shape(descriptor)
    require(descriptor.get('candidateKey')==value['candidateKey'] and descriptor.get('runtimeId')==runtime_id and descriptor.get('slotId')==value['slotId'] and
        descriptor.get('originalFundingExpiresEpoch')==original_expires_epoch==slot['original_funding_expires_epoch'] and
        descriptor.get('image')==configuration['worker_image'] and descriptor.get('mcpSecret')=={'secretArn':base['secretArn'],'versionId':base['versionId']} and
        descriptor.get('liability')=={'basisDigest':configuration['accounting']['operation']['liabilityBasisDigest'],
            'maximumInfrastructureUsd':configuration['accounting']['maximumUsd']['infrastructure'],
            'maximumCleanupUsd':configuration['accounting']['maximumUsd']['cleanup']},'installed original descriptor/source/quote differs')
    readiness,_=read_original(s3,value['readiness'],guard=guard)
    require(readiness.get('schema')=='honua.hosted-worker-readiness/v1' and readiness.get('source_sha')==producer['sourceSha'] and
        readiness.get('run_id')==str(producer['runId']) and readiness.get('attempt_id')==value['slotId'] and
        readiness.get('mode')=='installed-slot' and readiness.get('qualification') is False and all(readiness.get(k) is True for k in
        ('registered_task_definition_verified','immutable_agent_mcp_binding_verified','worker_role_queue_only',
         'independent_expiry_schedule_verified','cleanup_monitor_confirmed')),'original installed native readiness differs')
    from agent_e2e_installed_worker import InstalledSlot
    guard(); actual=InstalledSlot(descriptor,states=states,ecs=ecs,db=None).inspect(); guard()
    return {'descriptor':descriptor,'mcpBootstrap':value['mcpBootstrap'],'issuedAgentKey':base['issuedAgentKey'],
        'nativeBootstrap':base['nativeBootstrap'],'producer':producer,'sourceConfiguration':source,'plan':plan,
        'proof':{'nativeEndpoint':base['_nativeEndpoint'],'installation':selected,'readiness':value['readiness'],'review':plan['review'],'publicOriginals':proofs,
                 'native':{'stateMachineArn':actual['lifecycle']['stateMachineArn'],
                     'stateMachineRoleArn':actual['lifecycle']['roleArn'],
                     'definitionDigest':terraform_definition_digest(actual['lifecycle']['definition']),
                     'taskDefinitionArn':actual['taskDefinition']['taskDefinition']['taskDefinitionArn'],
                     'taskRoleArn':actual['taskDefinition']['taskDefinition']['taskRoleArn'],
                     'permissionsBoundaryArn':configuration['permissions_boundary_arn'],
                     'image':actual['taskDefinition']['taskDefinition']['containerDefinitions'][0]['image']}},'activation':False,'qualification':False}


def main():
    from deploy import ROOT, TF_ROOT, aws, config, context
    context()
    require(TF_ROOT==ROOT_PATH and os.environ.get('PILOT_OPERATION')=='apply','installed output emission requires exact worker apply')
    work=Path(os.environ['RUNNER_TEMP'])/'hosted-pilot-private'
    cfg=config(os.environ['PILOT_CONFIG_PATH'])
    result=emit(cfg=cfg,root=ROOT,outputs=json.loads((work/'outputs.json').read_text()),
        review=json.loads((work/'review.json').read_text()),work=work,env=os.environ,aws=aws)
    with open(os.environ['GITHUB_OUTPUT'],'a') as out:
        out.write('installed_worker_reference='+('true' if result else 'false')+'\n')
    print('Installed worker identity reference retained; native registration and activation remain separate.' if result else 'Static campaign worker path retained.')


if __name__=='__main__':
    try: main()
    except Exception:
        print('private installed worker identity refused; retain original evidence and liabilities',file=sys.stderr)
        sys.exit(1)
