"""Authenticate finite bootstrap producers while retaining every original event."""
from __future__ import annotations
import base64
from datetime import datetime
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import zipfile

from agent_e2e_pack import canonical_digest, digest_bytes
from controller_archive import require
from controller_archive import contract as runtime_contract
from budget_history import ACCOUNT, REGION, event_history

ROOT = Path(__file__).resolve().parents[2]
ENV_ORIGINAL = {
    '.github/workflows/hosted-pilot-foundation-environment.yml':'9a86a948f8702a939d00e8ac2c6bae9e4f13ca4bb087a4573cf91b44c74e90d2',
    'tools/hosted_pilot/foundation_environment.py':'5b3a4225b8eb5d738d05ee6a19907a341aaa3088645274fd325c553e32bbe89f',
    'tools/hosted_pilot/bind_github.py':'a548c91b113d488a5b0eff9db4a56ea22a53e129250053fadd694086ade19e4c',
    'tools/hosted_pilot/deploy.py':'875b556054f5e104277f56486077d29a78c66a05ab2a3692b253d90b0a35726f',
    'certification/hosted-pilot/foundation-environment.json':'a2aa5e4a94dc5a9fc6168aea18e213fb1f28e416f025ff8a66f8932bf875087b',
}
GITHUB_KINDS = {
    'github-environment-only':('.github/workflows/hosted-pilot-foundation-environment.yml',
        '.github/workflows/hosted-pilot-accounting-environment.yml'),
    'github-foundation-dispatch-only':('.github/workflows/hosted-pilot-bootstrap.yml',),
    'github-binding-only':('.github/workflows/hosted-pilot-bind.yml',),
}

KINDS = {
    'foundation':('honua-io/honua-iac','.github/workflows/hosted-pilot-bootstrap.yml','bindings.json'),
    'foundation-custody':('honua-io/honua-iac','.github/workflows/hosted-pilot-bootstrap.yml','custody-applied.json'),
    'foundation-plan':('honua-io/honua-iac','.github/workflows/hosted-pilot-bootstrap.yml','review.json'),
    'foundation-expired-before-apply':('honua-io/honua-iac','.github/workflows/hosted-pilot-bootstrap.yml','custody-recovery.json'),
    'foundation-plan-refused':('honua-io/honua-iac','.github/workflows/hosted-pilot-bootstrap.yml','custody-recovery.json'),
    'controller-image':('honua-io/honua-release','.github/workflows/hosted-pilot-controller-image.yml','hosted-pilot-controller-image.json'),
    'worker-image':('honua-io/honua-release','.github/workflows/hosted-pilot-worker-image.yml','hosted-pilot-worker-image.json'),
    'campaign-staging':('honua-io/honua-release','.github/workflows/hosted-pilot-deploy.yml','readiness.json'),
}


def invoke(argv):
    result=subprocess.run(argv,capture_output=True,check=False)
    require(result.returncode==0,'original history native metadata query refused')
    return result.stdout


def api(path, call):
    return json.loads(call(['gh','api',path]))


def source_file(repo, sha, path, call):
    require(re.fullmatch(r'[a-f0-9]{40}',sha),'original producer source SHA missing')
    value=api('repos/'+repo+'/contents/'+path+'?ref='+sha,call)
    require(value['type']=='file' and value['path']==path and value['encoding']=='base64','original source file differs')
    raw=base64.b64decode(value['content'])
    require(len(raw)<=2*1024*1024,'original source exceeds fixed bound')
    return raw


def verify_sources(repo, sha, mapping, call):
    require(mapping and all(re.fullmatch(r'[a-f0-9]{64}',value) for value in mapping.values()),'reviewed producer source contract missing')
    for path, expected in mapping.items():
        require(hashlib.sha256(source_file(repo,sha,path,call)).hexdigest()==expected,'original producer executable source changed')


def file_hashes(paths):
    return {path:hashlib.sha256((ROOT/path).read_bytes()).hexdigest() for path in paths}


def native_artifact(producer, reference, entry, call):
    require(set(reference)=={'id','sha256','name'} and type(reference['id']) is int and reference['id']>0 and
        re.fullmatch(r'sha256:[a-f0-9]{64}',reference['sha256']),'exact native artifact reference required')
    prefix='repos/'+producer['repository']+'/actions/artifacts/'+str(reference['id'])
    metadata=api(prefix,call)
    require(metadata['id']==reference['id'] and metadata['name']==reference['name'] and metadata['expired'] is False and
        metadata['workflow_run']['id']==producer['runId'] and metadata['workflow_run']['head_sha']==producer['sourceSha'],
        'original artifact producer/source differs')
    raw=call(['gh','api',prefix+'/zip'])
    require(len(raw)<=8*1024*1024 and digest_bytes(raw)==reference['sha256'] and
        metadata.get('digest') in (None,reference['sha256']),'original artifact bytes differ')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(archive.namelist()==[entry],'original artifact contains an unexpected file')
        item=archive.getinfo(entry)
        require(item.file_size<=4*1024*1024 and not item.is_dir(),'original artifact entry exceeds fixed bound')
        return json.loads(archive.read(item))


def producer_key(producer):
    return (producer['repository'],producer['runId'],producer['runAttempt'])


def authenticate(record, *, call=invoke):
    """Only named GitHub configuration and pre-model funding producers qualify."""
    kind,p=record['kind'],record['producer']
    require(set(p)=={'repository','runId','runAttempt','sourceSha','workflow'} and type(p['runId']) is int and p['runId']>0 and
        type(p['runAttempt']) is int and p['runAttempt']>0,'original producer identity missing')
    environment=kind in GITHUB_KINDS
    if environment:
        require(p['repository']=='honua-io/honua-release' and p['workflow'] in GITHUB_KINDS[kind],
            'unknown environment producer cannot be admitted')
    else:
        require(kind in KINDS and (p['repository'],p['workflow'])==KINDS[kind][:2],'unknown or model producer cannot be bootstrap history')
    if kind=='foundation-expired-before-apply':
        from history_expired_phase import authenticate as expired_phase
        return expired_phase(record,call)
    if kind=='foundation-plan-refused':
        from history_failed_plan import authenticate_refused_plan
        return authenticate_refused_plan(record,call)
    base='repos/'+p['repository']
    run=api(base+f"/actions/runs/{p['runId']}/attempts/{p['runAttempt']}",call)
    require((run['id'],run['run_attempt'],run['head_sha'],run['path'],run['head_branch'],run['event'],run['status'],run['conclusion'])==
        (p['runId'],p['runAttempt'],p['sourceSha'],p['workflow'],'trunk','workflow_dispatch','completed','success'),
        'history requires an exact completed original successful producer')
    require(api(base+'/branches/trunk',call)['protected'] is True,'original history requires protected source trunk')
    comparison=api(base+'/compare/'+p['sourceSha']+'...trunk',call)
    require(comparison['merge_base_commit']['sha']==p['sourceSha'],'original producer source is not on protected trunk')
    jobs=api(base+f"/actions/runs/{p['runId']}/attempts/{p['runAttempt']}/jobs?per_page=100",call)
    require(0<len(jobs['jobs'])==jobs['total_count'] and all(job['head_sha']==p['sourceSha'] and job['status']=='completed' and
        job['conclusion'] in ('success','skipped') for job in jobs['jobs']),'original producer jobs incomplete or failed')
    if environment:
        if kind=='github-environment-only':
            paths=[p['workflow'],'tools/hosted_pilot/foundation_environment.py','tools/hosted_pilot/bind_github.py','tools/hosted_pilot/deploy.py',
                'certification/hosted-pilot/'+('accounting' if 'accounting-environment' in p['workflow'] else 'foundation')+'-environment.json']
        elif kind=='github-binding-only':
            paths=[p['workflow'],'tools/hosted_pilot/bind_github.py','tools/hosted_pilot/deploy.py','certification/hosted-pilot/github-bindings.json']
        else:paths=[p['workflow']]
        approved=file_hashes(paths)
        if p['sourceSha']=='7342099da90f715f44a181f9d025c5740149b15a' and p['workflow']=='.github/workflows/hosted-pilot-foundation-environment.yml':
            approved=ENV_ORIGINAL
        verify_sources(p['repository'],p['sourceSha'],approved,call)
        require(record['artifact'] is None and record['planArtifact'] is None and not record['cloudEvents'],
                'environment-only history cannot claim AWS effects')
        return run,jobs,None,None
    entry=KINDS[kind][2]
    expected_name={'foundation':f"hosted-pilot-foundation-bindings-{p['runId']}",
        'foundation-plan':f"hosted-pilot-foundation-review-{p['runId']}",
        'foundation-custody':f"hosted-pilot-custody-applied-{p['runId']}-{p['runAttempt']}",
        'controller-image':f"hosted-pilot-controller-image-{p['runId']}-{p['runAttempt']}",
        'worker-image':f"hosted-pilot-worker-image-{p['runId']}-{p['runAttempt']}",
        'campaign-staging':f"hosted-pilot-infrastructure-readiness-{p['runId']}-{p['runAttempt']}"}[kind]
    if kind=='foundation-plan' and record['artifact']['name'].startswith('hosted-pilot-phase-review-'):
        from history_phase import review_name
        require(record['artifact']['name'] in [review_name(p,phase) for phase in ('custody-only','remote-backed-foundation')], 'phase artifact source/attempt differs')
        expected_name=record['artifact']['name']
    require(record['artifact']['name']==expected_name,'original producer artifact identity differs')
    receipt=native_artifact(p,record['artifact'],entry,call)
    execution_sha=p['sourceSha']
    if kind in ('foundation','foundation-plan','foundation-custody'):
        phase_protocol = kind=='foundation-custody' or receipt.get('schema') in ('honua.hosted-pilot-bootstrap-plan/v2','honua.hosted-pilot-bootstrap-bindings/v2')
        if kind=='foundation-plan':
            require(receipt['schema'] in ('honua.hosted-pilot-bootstrap-plan/v1','honua.hosted-pilot-bootstrap-plan/v2') and
                receipt['foundation_source_sha']==p['sourceSha'] and receipt['phase'] in ('custody-only','remote-backed-foundation') and
                record['planArtifact'] is None and record['ownerLedger'] is None and not record['cloudEvents'],
                'foundation plan cannot claim applied effects, a published owner version or new resources')
            artifacts=api(base+f"/actions/runs/{p['runId']}/artifacts?per_page=100",call)
            require(len(artifacts['artifacts'])==artifacts['total_count'] and not any(
                row['name']==f"hosted-pilot-foundation-bindings-{p['runId']}" for row in artifacts['artifacts']),
                'foundation plan classification conflicts with actual deployment artifact')
            if phase_protocol:
                from history_phase import plan_only
                plan_only(record,receipt,jobs,artifacts['artifacts'])
        elif kind=='foundation-custody':
            from history_phase import applied
            receipt=applied(record,receipt,jobs,native_artifact=native_artifact,api=api,source_file=source_file,call=call)
        else:
            require(receipt['schema'] in ('honua.hosted-pilot-bootstrap-bindings/v1','honua.hosted-pilot-bootstrap-bindings/v2') and receipt['foundation_source_sha']==p['sourceSha'] and
            int(receipt['foundation_run_id'])==p['runId'] and int(receipt['foundation_run_attempt'])==p['runAttempt'],
                'foundation receipt differs from original completed run')
            if phase_protocol:
                from history_phase import applied
                applied(record,receipt,jobs,native_artifact=native_artifact,api=api,source_file=source_file,call=call)
        execution_sha=receipt['release_root_sha']
        original_sources=None
        if kind=='foundation-plan':
            from history_failed_plan import original_success_sources
            original_sources=original_success_sources(record,receipt)
        workflow=original_sources[0] if original_sources is not None else {p['workflow']:hashlib.sha256((ROOT/'certification/hosted-pilot/bootstrap/foundation-bootstrap-workflow.yml').read_bytes()).hexdigest(),
            'scripts/check-hosted-pilot-foundation-inputs.py':hashlib.sha256((ROOT/'certification/hosted-pilot/bootstrap/foundation-input-check.py').read_bytes()).hexdigest()}
        verify_sources(p['repository'],p['sourceSha'],workflow,call)
        paths=['tools/hosted_pilot/bootstrap.py','tools/hosted_pilot/deploy.py','tools/hosted_pilot/foundation_environment.py',
            'tools/hosted_pilot/bind_github.py','certification/hosted-pilot/foundation-environment.json',
            'certification/hosted-pilot/bootstrap/.terraform.lock.hcl',
            *[str(path.relative_to(ROOT)) for path in sorted((ROOT/'certification/hosted-pilot/bootstrap').glob('*.tf')) if path.name!='recovery-imports.tf']]
        if phase_protocol:
            paths += ['tools/hosted_pilot/bootstrap_phase.py', 'tools/hosted_pilot/custody_completion.py']
        verify_sources('honua-io/honua-release',execution_sha,original_sources[1] if original_sources is not None else file_hashes(paths),call)
        config=source_file(p['repository'],p['sourceSha'],receipt['config_path'],call)
        require(digest_bytes(config)=='sha256:'+receipt['config_sha256'] and json.loads(config).get('image_preparation_funding') is True,
                'original foundation configuration changed or has unknown funding mode')
        actor=receipt.get('execution_identity') or {}
        require(actor.get('schema')=='honua.hosted-pilot-bootstrap-execution-identity/v1' and
            str(actor.get('run_id'))==str(p['runId']) and str(actor.get('run_attempt'))==str(p['runAttempt']) and
            actor.get('foundation_source_sha')==p['sourceSha'] and actor.get('release_root_sha')==execution_sha and
            actor.get('config_path')==receipt['config_path'] and actor.get('config_sha256')==receipt['config_sha256'] and
            actor.get('account_id')==ACCOUNT and actor.get('run_unique_session') is False and
            actor.get('provider_lock_sha256')==hashlib.sha256(source_file('honua-io/honua-release',execution_sha,
                'certification/hosted-pilot/bootstrap/.terraform.lock.hcl',call)).hexdigest(),
            'foundation original execution identity/run/source differs')
    else:
        paths=[p['workflow'],'tools/hosted_pilot/deploy.py']
        paths+=['tools/hosted_pilot/'+('campaign_readiness.py' if kind=='campaign-staging' else kind.replace('-','_')+'.py')]
        if kind in ('controller-image','worker-image'):
            paths+=['tools/hosted_pilot/controller_archive.py','tools/hosted_pilot/image_retention.py','tools/agent_e2e_pack.py']
        if kind=='worker-image':
            paths+=['tools/hosted_pilot/controller_image.py','tools/agent_e2e_knowledge.py','tools/bundle_agent_e2e_container.py','tools/smoke_agent_e2e_container.py']
        verify_sources(p['repository'],p['sourceSha'],file_hashes(paths),call)
        if kind=='campaign-staging':
            require(receipt['schema']=='honua.hosted-pilot-campaign-readiness/v1' and receipt['source_sha']==p['sourceSha'] and
                int(receipt['run_id'])==p['runId'] and receipt['qualification'] is False and receipt['dispatch_enabled'] is False and
                receipt['dispatcher']['reviewedPolicyMapVerified'] is True,'campaign history is not source-verified pre-model staging')
            plan=record['planArtifact']
            require(plan is not None,'staging requires its original saved-plan review')
            review=native_artifact(p,plan,'review.json',call)
            require(review['schema']=='honua.hosted-pilot-saved-plan/v1' and review['operation']=='apply' and
                review['root_path']=='certification/hosted-pilot/campaign' and review['context']['GITHUB_SHA']==p['sourceSha'] and
                int(review['context']['GITHUB_RUN_ID'])==p['runId'] and int(review['context']['GITHUB_RUN_ATTEMPT'])<=p['runAttempt'] and
                plan['name']==f"hosted-pilot-review-{p['runId']}-{review['context']['GITHUB_RUN_ATTEMPT']}",
                'staging saved-plan producer/source differs')
            config=source_file(p['repository'],p['sourceSha'],review['config_path'],call)
            require(digest_bytes(config)=='sha256:'+review['config_sha256'] and json.loads(config).get('dispatch_budget') is None,
                    'staging original configuration enables or changes paid dispatch')
        else:
            source=receipt['source_sha'] if kind=='controller-image' else receipt['sourceSha']
            run_id=receipt['run_id'] if kind=='controller-image' else receipt['runId']
            attempt=receipt['run_attempt'] if kind=='controller-image' else receipt['runAttempt']
            require(source==p['sourceSha'] and int(run_id)==p['runId'] and int(attempt)==p['runAttempt'] and
                record['planArtifact'] is None,'image preparation receipt producer differs')
            if kind=='controller-image':
                require(receipt['schema']=='honua.hosted-controller-image/v1' and 0<receipt['image_bytes']<=4*1024**3 and
                    receipt['runtime_contract_sha256']==runtime_contract(ROOT),'controller receipt lost its bounded source/runtime storage contract')
            else:
                require(receipt['schema']=='honua.hosted-worker-image/v1' and receipt['qualification'] is False and
                    receipt['modelExecution']=='not-run' and 0<receipt['imageBytes']<=512*1024**2,'worker image preparation is not a bounded model-free producer')
    return run,jobs,receipt,execution_sha


def reconcile(history, ledger, references, *, read, cutoff, call=invoke, authenticate_originals=True):
    """Every observed producer/event needs exactly one original proof and funding."""
    producers={(repo,row['id'],row['run_attempt']):row for repo,value in history['repositories'].items() for row in value['producerRuns']}
    events={(name,row['eventId']):row for name,value in history['cloudTrail'].items() for row in value['pilotEvents']}
    covered_producers,covered_events=set(),set()
    for reference in references:
        record=read(reference)
        require(set(record)=={'schema','kind','producer','fundingBasis','fundingReservationIds','ownerLedger','artifact','planArtifact','cloudEvents'} and
            record['schema']=='honua.hosted-pilot-producer-reconciliation/v1','reviewed original producer reconciliation missing')
        p=record['producer'];key=producer_key(p)
        require(record['kind'] in GITHUB_KINDS or record['kind'] in KINDS,'unknown or model history cannot be bootstrap')
        allowed = ('honua-io/honua-release',p['workflow']) if record['kind'] in GITHUB_KINDS else KINDS[record['kind']][:2]
        require((p['repository'],p['workflow'])==allowed and (record['kind'] not in GITHUB_KINDS or
            p['workflow'] in GITHUB_KINDS[record['kind']]),
            'unknown or model workflow cannot be bootstrap history')
        require(key in producers and key not in covered_producers and producers[key]['head_sha']==p['sourceSha'] and
            producers[key]['path']==p['workflow'],'producer reconciliation is missing, duplicated or substituted')
        basis=read(record['fundingBasis'])
        ids=record['fundingReservationIds']
        require(isinstance(ids,list) and ids and len(set(ids))==len(ids),'original nonzero funding reservation IDs missing')
        rows=[row for row in ledger['operations'] if row['id'] in ids]
        require(len(rows)==len(ids) and all(row['basisDigest']==canonical_digest(basis) and Decimal(row['amountUsd']).is_finite() and Decimal(row['amountUsd'])>0 and
            (row['state']=='reserved' and row['actualUsd'] is None or row['state']=='settled' and Decimal(row['actualUsd']).is_finite() and Decimal(row['actualUsd'])>0)
            for row in rows),'original producer funding was dropped, zeroed or rebound')
        require(record['kind'] in GITHUB_KINDS or
            {row['kind'] for row in rows}=={'infrastructure','cleanup'} and all(row['purpose']=='bootstrap' for row in rows),
            'paid bootstrap must retain both original infrastructure and cleanup funding')
        if record['kind']=='foundation-expired-before-apply':
            from history_expired_phase import validate_funding_rows
            validate_funding_rows(record,rows,ledger)
        run,jobs,receipt,execution_sha=authenticate(record,call=call) if authenticate_originals else (None,None,None,None)
        if authenticate_originals:
            limit=datetime.fromisoformat(cutoff.replace('Z','+00:00'))
            require(limit.tzinfo is not None and all(
                datetime.fromisoformat(job['completed_at'].replace('Z','+00:00'))<=limit for job in jobs['jobs']),
                'original producer completed after history cutoff; recollect before initial import')
        if record['kind'] in GITHUB_KINDS:
            require(record['ownerLedger'] is None and set(ids)=={'initialization-history-usage'} and
                all(row['purpose']=='telemetry' and row['kind']=='infrastructure' for row in rows),
                'environment history must retain its original diagnostic/usage funding')
        if authenticate_originals and record['kind'] not in GITHUB_KINDS:
            actual=source_file('honua-io/honua-release',execution_sha,record['fundingBasis']['path'],call)
            require(digest_bytes(actual)==record['fundingBasis']['sha256'],'funding basis did not exist in original executable source')
            if record['kind'] in ('foundation-plan','foundation-plan-refused','foundation-custody','foundation-expired-before-apply'):
                require(record['ownerLedger'] is None,'a source plan cannot invent a published owner ledger')
            else:
                validate_original_owner(record,receipt,rows,execution_sha,p,read,call)
        for item in record['cloudEvents']:
            event_key=(item['eventName'],item['eventId'])
            require(set(item)=={'eventName','eventId','nativeEventSha256'} and event_key in events and event_key not in covered_events and
                events[event_key]['nativeEventSha256']==item['nativeEventSha256'] and item['eventName'] in ('CreateRole','CreateBucket','CreateStateMachine'),
                'unknown, duplicated or model/task execution event cannot become bootstrap')
            event=events[event_key]
            require(record['kind'] in ('foundation','foundation-custody','campaign-staging') and event['resourceIdentities'] and not event['unprojectedNamespaceReference'],
                    'AWS event lacks independently bounded bootstrap resource identities')
            if authenticate_originals:
                authenticate_event(event,record,run,jobs,receipt,ledger['candidateKey'])
            covered_events.add(event_key)
        covered_producers.add(key)
    require(covered_producers==set(producers) and covered_events==set(events),
            'original producer/event history requires complete finite reconciliation before initial import')


def validate_original_owner(record,receipt,rows,execution_sha,p,read,call):
    owner=read(record['ownerLedger'])
    native_budget=receipt['budget_binding'] if record['kind']=='foundation' else receipt['owner_budget'] if record['kind']=='controller-image' else receipt['ownerBudget'] if record['kind']=='worker-image' else native_artifact(p,record['planArtifact'],'review.json',call)['budget']
    require(record['ownerLedger']['sha256']=='sha256:'+native_budget['sha256'] and
        owner['schema']=='honua.hosted-pilot-budget/v1' and owner['source_sha']==execution_sha and
        owner['total_budget_usd']==2000 and owner['cleanup_reserve_usd']==200 and owner['reserved_usd']>0 and
        0<owner['deployment_reservation_usd']<=sum(Decimal(row['amountUsd']) for row in rows if row['kind']=='infrastructure'),
        'bootstrap original owner version/funding was lost or replaced')
    if record['kind'] in ('foundation','controller-image','worker-image'):
        config_bytes=source_file('honua-io/honua-release',execution_sha,
            'certification/hosted-pilot/deployments/foss4g-image-preparation.tfvars.json',call)
        require(owner['config_sha256']==hashlib.sha256(config_bytes).hexdigest(),'original image funding configuration differs')
    if record['kind'] in ('controller-image','worker-image'):
        require(owner.get('reservation_id') in record['fundingReservationIds'],'image history lost its original single-use funding reservation')

def authenticate_event(event,record,run,jobs,receipt,candidate_key):
    """Original native actor and run-bound session are necessary ownership proof."""
    require(event.get('nativeEventIdMatches') is True and event.get('recipientAccountId')==ACCOUNT,
        'original AWS event identity/account differs')
    identity=event.get('principal') or {}
    issuer=identity.get('sessionIssuer') or {}
    if record['kind'] in ('foundation','foundation-custody'):
        actor=receipt.get('execution_identity') or {}
        require(actor.get('schema')=='honua.hosted-pilot-bootstrap-execution-identity/v1' and
            actor.get('foundation_source_sha')==record['producer']['sourceSha'] and
            actor.get('release_root_sha')==receipt['release_root_sha'] and
            str(actor.get('run_id'))==str(record['producer']['runId']) and
            str(actor.get('run_attempt'))==str(record['producer']['runAttempt']) and
            actor.get('account_id')==ACCOUNT and actor.get('run_unique_session') is False and
            identity.get('type') in ('IAMUser','AssumedRole') and identity.get('accountId')==ACCOUNT and
            identity.get('arn')==actor.get('principal_arn') and identity.get('principalId')==actor.get('principal_id'),
            'foundation event lacks original actual STS principal proof; static identity alone is not run-unique')
        resource_type={'CreateRole':'aws_iam_role','CreateBucket':'aws_s3_bucket'}.get(event['eventName'])
        rows=[row for row in receipt.get('creation_resources',[]) if row['type']==resource_type]
        name=event.get('creationResourceName')
        matches=[row for row in rows if row['name']==name]
        require(len(matches)==1,'creation event lacks its exact original saved-plan physical resource')
        planned=matches[0]
        original=receipt.get('original_creation') if record['kind']=='foundation-custody' else None
        scope=original['identity'] if original else actor
        creator=original['run_id'] if original else str(record['producer']['runId'])
        require(planned['creation_tags'].get('BootstrapExecution')==creator and
            planned['creation_tags'].get('BootstrapFoundationSha')==scope.get('foundation_source_sha') and
            planned['creation_tags'].get('BootstrapReleaseSha')==scope.get('release_root_sha') and
            all(planned['creation_tags'].get(tag)==scope.get(key) for key,tag in {
                'config_path':'BootstrapConfigPath','config_sha256':'BootstrapConfigSha256',
                'provider_lock_sha256':'BootstrapLockSha256'}.items()),
            'planned resource does not belong to this original source/run')
        observation=event.get('resourceTagObservation') or {}
        args=['aws','iam','list-role-tags','--role-name',name] if event['eventName']=='CreateRole' else [
            'aws','s3api','get-bucket-tagging','--bucket',name,'--region',REGION]
        require(observation.get('resourceName')==name and observation.get('complete') is True and
            observation.get('evidence',{}).get('command')==[*args,'--output','json'] and
            observation['evidence'].get('projection')=='identity-and-aggregate-usage-only' and
            re.fullmatch(r'sha256:[a-f0-9]{64}',observation['evidence'].get('responseSha256','')),
            'foundation event lacks its original native resource-tag observation')
        response=observation['tags']
        require(all(response.get(key)==value for key,value in planned['creation_tags'].items()),
            'actual resource creation tags differ from original source and saved plan')
        prefix='honua-release-pilot-foss4g-foundation'
        require(name.startswith(prefix+'-'),'foundation event escaped reviewed namespace')
        if event['eventName']=='CreateRole':
            require(all(value in (name,'arn:aws:iam::'+ACCOUNT+':role/'+name) for value in event['resourceIdentities']),
                'creation event contains a foreign resource identity')
        else:
            require(all(value in (name,'arn:aws:s3:::'+name) for value in event['resourceIdentities']),
                'creation event contains a foreign resource identity')
    else:
        role='arn:aws:iam::'+ACCOUNT+':role/honua-release-pilot-foss4g-foundation-apply'
        session='pilot-apply-'+str(record['producer']['runId'])
        require(identity.get('type')=='AssumedRole' and identity.get('accountId')==ACCOUNT and
            issuer.get('arn')==role and issuer.get('type')=='Role' and issuer.get('accountId')==ACCOUNT and
            re.fullmatch(r'AROA[A-Z0-9]+',issuer.get('principalId','')) and
            identity.get('arn')=='arn:aws:sts::'+ACCOUNT+':assumed-role/'+role.rsplit('/',1)[1]+'/'+session and
            identity.get('principalId')==issuer['principalId']+':'+session,
            'original AWS creation actor/session is not the reviewed run-bound apply role')
        prefixes=('honua-pilot-foss4g-agent','honua-agent-'+candidate_key[7:31])
        require(all(any(re.search(r'(^|[:/])'+re.escape(prefix)+r'($|[-/:])',identity) for prefix in prefixes)
            for identity in event['resourceIdentities']),'AWS event escaped original reviewed resource namespaces')
        name=event.get('creationResourceName')
        require(isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9+=,.@_-]{1,128}',name),'actual creation name missing')
        arn={'CreateRole':'arn:aws:iam::'+ACCOUNT+':role/','CreateBucket':'arn:aws:s3:::',
            'CreateStateMachine':'arn:aws:states:'+REGION+':'+ACCOUNT+':stateMachine:'}[event['eventName']]+name
        require(all(value in (name,arn) for value in event['resourceIdentities']),
            'creation event contains a foreign account/region/resource identity')
    start=datetime.fromisoformat(run['run_started_at'].replace('Z','+00:00'))
    end=max(datetime.fromisoformat(job['completed_at'].replace('Z','+00:00')) for job in jobs['jobs'])
    observed=datetime.fromisoformat(event['eventTime'].replace('Z','+00:00'))
    require(start<=observed<=end,'AWS event does not belong to original producer execution interval')


def envelope(value, *, read):
    """Keep raw collections intact; the envelope is explicit reviewed interpretation."""
    require(set(value)=={'schema','currentHistory','retainedHistories','producerReconciliations'} and
        value['schema']=='honua.hosted-pilot-reconciled-history/v1' and isinstance(value['retainedHistories'],list) and
        len(value['retainedHistories'])<=32 and isinstance(value['producerReconciliations'],list) and
        len(value['producerReconciliations'])<=128,'bounded reviewed history envelope missing')
    current=read(value['currentHistory'])
    prior=[read(reference) for reference in value['retainedHistories']]
    reports=[*prior,current]
    require(all(row.get('schema')=='honua.hosted-pilot-budget-history/v1' and row.get('wholeAccountZeroSpend') is False and
        row.get('accountId')==ACCOUNT and row.get('resourceRegion')==REGION for row in reports),
        'history envelopes must retain original nonzero-account native collections')
    def instant(value):return datetime.fromisoformat(value.replace('Z','+00:00'))
    require(all(instant(current['coverage']['start'])<=instant(row['coverage']['start']) and
        instant(current['coverage']['cutoff'])>=instant(row['coverage']['cutoff']) for row in prior),
        'fresh collection must cover the full original observation interval without gaps')
    producers,events={},{}
    for report in reports:
        for repo,scope in report['repositories'].items():
            for row in scope['producerRuns']:
                key=(repo,row['id'],row['run_attempt'])
                if key in producers:
                    require(all(producers[key][field]==row[field] for field in ('path','head_sha','head_branch','created_at','event')),
                        'retained original producer identity was changed')
                producers[key]=row
        for name,scope in report['cloudTrail'].items():
            for row in scope['pilotEvents']:
                key=(name,row['eventId'])
                # Live tag observations may be refreshed; the original event
                # and every retained report remain immutable source references.
                if key in events:
                    original={name:value for name,value in events[key].items() if name!='resourceTagObservation'}
                    require(all(name in row and canonical_digest(row[name])==canonical_digest(value)
                        for name,value in original.items()),'retained original AWS event changed')
                events[key]=row
    merged={'repositories':{},'cloudTrail':{}}
    for (repo,_,_),row in producers.items():merged['repositories'].setdefault(repo,{'producerRuns':[]})['producerRuns'].append(row)
    for (name,_),row in events.items():merged['cloudTrail'].setdefault(name,{'pilotEvents':[]})['pilotEvents'].append(row)
    return current,prior,merged
