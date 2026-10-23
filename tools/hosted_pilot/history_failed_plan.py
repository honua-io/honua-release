"""Preserve the finite original foundation-plan evidence and its limitations."""
from __future__ import annotations
import hashlib
import re
from controller_archive import require

PRODUCER = {'repository': 'honua-io/honua-iac',
 'runAttempt': 1,
 'runId': 40007575185,
 'sourceSha': '2cf6e807e0fa7a80cf2e4f124085ee7aae4253c9',
 'workflow': '.github/workflows/hosted-pilot-bootstrap.yml'}
RELEASE_SHA = '94fc5d304af840440c2d48c14186796418a0d2e0'
CONFIG_PATH = 'certification/hosted-pilot/bootstrap/deployments/foss4g-images.tfvars.json'
CONFIG_SHA256 = 'ebb42c3be45e9efa7bec217628ac006cc43106f9172630c5d9b62691e90e5f09'
ARTIFACT = {'id': 12524278730,
 'name': 'hosted-pilot-custody-recovery-40007575185',
 'sha256': 'sha256:6475d69308a800eb0466e214adaadccea8c5276ffbfcd4b404d26a23ed190921'}
APPROVAL_JOB_ID = 120599313852
BOOTSTRAP_JOB_ID = 120599500170
IAC_SOURCES = {'.github/workflows/hosted-pilot-bootstrap.yml': 'bfb974a982dc4dd729e1ce987eec74e3c42a598d0e8f574af1973f08299f2b99',
 'scripts/check-hosted-pilot-foundation-inputs.py': '9728d194f962cbbfcbba89cde20997e97af404c602f5c0fa5346e0d3bc06ba57'}
RELEASE_SOURCES = {'certification/hosted-pilot/bootstrap/.terraform.lock.hcl': '7fd30e70f88ed9c5de18a5d13a5d91303fe233a14719dede9d6ededd3a2d0851',
 'certification/hosted-pilot/bootstrap/agent-boundary.tf': '132d5634b423e1029956d88e6748fede60b7d4b4ff3f621ef812b9b78b3ddbb0',
 'certification/hosted-pilot/bootstrap/controller-image.tf': 'f3cae720c0ab2a7f4f68af55b41493d8dc8460a2fc3257ac48486c938ee210f3',
 'certification/hosted-pilot/bootstrap/main.tf': 'cd72ea34362b5bba22ac1cbc1a90514a4c71c344081b004bc92d4388d16e3a10',
 'certification/hosted-pilot/bootstrap/outputs.tf': '4cbf86842cbcbb502e92c8c43352e695a7a3e3342ea61e14e1ca4df82b90c173',
 'certification/hosted-pilot/bootstrap/variables.tf': 'bd691c2e8c966986f7b9f05ca9eb6d1e11fb15e0e78f9c311cc31b7febc01d76',
 'certification/hosted-pilot/bootstrap/versions.tf': '7e83fb16578c7c8379d3656869cb478bce596701eeccfaa0ce297ee1b0b161f5',
 'certification/hosted-pilot/bootstrap/worker-budget-read.tf': 'c2faa4045d384ba7f66ed1c9e7ad3a66c2393ac6c2879cbc91ccf18a72f5ca76',
 'certification/hosted-pilot/foundation-environment.json': 'a2aa5e4a94dc5a9fc6168aea18e213fb1f28e416f025ff8a66f8932bf875087b',
 'tools/hosted_pilot/bind_github.py': 'a548c91b113d488a5b0eff9db4a56ea22a53e129250053fadd694086ade19e4c',
 'tools/hosted_pilot/bootstrap.py': '4267e517cce36171026874f304620a8029f7e63beff0938375f6837d0846f394',
 'tools/hosted_pilot/deploy.py': 'fd8fe5e8f2578428416d088686209843a0454922a7be91e32dd02ce90fc13b08',
 'tools/hosted_pilot/foundation_environment.py': 'ff4ff20957736f36cc0cfd1573bf6bc86bef4dbb4cc8d19602d0dafcd7014ddb'}

def plan_header(raw):
    """Project runner metadata only; never retain/hash raw credential-bearing logs."""
    require(isinstance(raw,bytes) and len(raw)<=4*1024*1024,'original plan job log exceeds bound')
    try:
        text=raw.decode('utf-8')
    except UnicodeError:
        raise ValueError('original plan job log encoding differs') from None
    lines=[re.sub(r'\x1b\[[0-9;]*m','',re.sub(r'^\d{4}-\d{2}-\d{2}T[0-9:.]+Z ','',line)) for line in text.splitlines()]
    command='##[group]Run python3 tools/hosted_pilot/bootstrap.py "$MODE" --config "$CONFIG"'
    starts=[i for i,line in enumerate(lines) if line==command]
    require(len(starts)==1,'original plan runner invocation missing or duplicated')
    fields={};expected={'MODE':'plan','CONFIG':CONFIG_PATH,'PILOT_RELEASE_ROOT_SHA':RELEASE_SHA,'AWS_REGION':'us-east-1'}
    ended=False
    for line in lines[starts[0]+1:starts[0]+129]:
        if line=='##[endgroup]':
            ended=True;break
        for name in expected:
            prefix='  '+name+': '
            if line.startswith(prefix):
                require(name not in fields,'original plan runner metadata duplicated')
                fields[name]=line[len(prefix):]
    require(ended and fields==expected,'original failed invocation was not the exact reviewed plan mode')
    return fields


def authenticate_refused_plan(record,call):
    import history_reconciliation as h
    p=record['producer']
    require(record['kind']=='foundation-plan-refused' and p==PRODUCER,
            'only the original observed refused plan may use this diagnostic classification')
    require(record['ownerLedger'] is None and record['planArtifact'] is None and not record['cloudEvents'] and
            record['artifact']==ARTIFACT,'refused plan cannot claim applied state, resources or replacement artifacts')
    base='repos/'+p['repository']
    run=h.api(base+f"/actions/runs/{p['runId']}/attempts/{p['runAttempt']}",call)
    require((run['id'],run['run_attempt'],run['head_sha'],run['path'],run['head_branch'],run['event'],run['status'],run['conclusion'])==
            (p['runId'],1,p['sourceSha'],p['workflow'],'trunk','workflow_dispatch','completed','failure'),
            'original diagnostic run must retain its actual completed failure')
    require(h.api(base+'/branches/trunk',call)['protected'] is True,'original refused-plan source trunk must be protected')
    comparison=h.api(base+'/compare/'+p['sourceSha']+'...trunk',call)
    require(comparison['merge_base_commit']['sha']==p['sourceSha'],'original refused-plan source is not on protected trunk')
    require(h.api('repos/honua-io/honua-release/branches/trunk',call)['protected'] is True,
            'original execution source trunk must be protected')
    release_comparison=h.api('repos/honua-io/honua-release/compare/'+RELEASE_SHA+'...trunk',call)
    require(release_comparison['merge_base_commit']['sha']==RELEASE_SHA,'original execution source left protected trunk')
    jobs=h.api(base+f"/actions/runs/{p['runId']}/attempts/1/jobs?per_page=100",call)
    expected={'approval-policy':(APPROVAL_JOB_ID,'success'),'bootstrap':(BOOTSTRAP_JOB_ID,'failure')}
    require(len(jobs['jobs'])==jobs['total_count']==2 and
            {j['name']:(j['id'],j['conclusion']) for j in jobs['jobs']}==expected and
            all(j['status']=='completed' and j['head_sha']==p['sourceSha'] and j['run_id']==p['runId'] for j in jobs['jobs']),
            'original refused-plan jobs/source/failure differ')
    job=next(j for j in jobs['jobs'] if j['id']==BOOTSTRAP_JOB_ID)
    required={
        'Verify exact candidate-independent configuration and release compiler before AWS credentials':'success',
        'Custody-first source-reviewed bootstrap in one ephemeral job':'failure',
        'Preserve initial-custody recovery identities after failure or cancellation':'success',
        'Remove private executor material':'success',
    }
    require(all(len([s for s in job['steps'] if s['name']==name and s['status']=='completed' and s['conclusion']==result])==1
                for name,result in required.items()) and
            len([s for s in job['steps'] if s['conclusion']=='failure'])==1,
            'original failed plan did not retain source checks, exact refused step and private cleanup')
    artifacts=h.api(base+f"/actions/runs/{p['runId']}/artifacts?per_page=100",call)
    require(len(artifacts['artifacts'])==artifacts['total_count']==1 and
            artifacts['artifacts'][0]['id']==ARTIFACT['id'] and artifacts['artifacts'][0]['name']==ARTIFACT['name'],
            'original refused plan has unexpected review/deployment artifacts')
    receipt=h.native_artifact(p,record['artifact'],'custody-recovery.json',call)
    require(receipt['schema']=='honua.hosted-pilot-custody-recovery/v1' and
            receipt['foundation_source_sha']==p['sourceSha'] and receipt['release_root_sha']==RELEASE_SHA and
            str(receipt['bootstrap_run_id'])==str(p['runId']) and receipt['config_path']==CONFIG_PATH and
            receipt['config_sha256']==CONFIG_SHA256 and
            receipt['provider_lock_sha256']==RELEASE_SOURCES['certification/hosted-pilot/bootstrap/.terraform.lock.hcl'] and
            receipt['cloud_discovery_complete'] is False and
            receipt['resources']==[] and receipt['remote_recovery_state'] is None and
            receipt['unmapped_owned_key_ids']==[] and receipt['unverified_bucket_names']==[],
            'original incomplete discovery changed; do not treat it as zero-resource evidence')
    h.verify_sources(p['repository'],p['sourceSha'],IAC_SOURCES,call)
    h.verify_sources('honua-io/honua-release',RELEASE_SHA,RELEASE_SOURCES,call)
    config=h.source_file(p['repository'],p['sourceSha'],CONFIG_PATH,call)
    require(hashlib.sha256(config).hexdigest()==CONFIG_SHA256,'original refused-plan configuration changed')
    # A failed artifact has no preserved STS actor. Do not synthesize an actor or
    # use time/IP as ownership proof: this kind is forbidden from claiming events.
    plan_header(call(['gh','api','--allow-escape-sequences',base+f'/actions/jobs/{BOOTSTRAP_JOB_ID}/logs']))
    return run,jobs,receipt,RELEASE_SHA


SUCCESS_PRODUCER = {'repository': 'honua-io/honua-iac',
 'runAttempt': 1,
 'runId': 40014973402,
 'sourceSha': '2cf6e807e0fa7a80cf2e4f124085ee7aae4253c9',
 'workflow': '.github/workflows/hosted-pilot-bootstrap.yml'}
SUCCESS_ARTIFACT = {'id': 12527672687,
 'name': 'hosted-pilot-foundation-review-40014973402',
 'sha256': 'sha256:e1403f2819edd18a2e50889bd38f1da930ed6836036080535e4fe0b32f38625c'}
SUCCESS_RELEASE_SHA = '60a66ea6ac09bf5578bde14c359d0d935b57ee47'
SUCCESS_RELEASE_SOURCES = {'certification/hosted-pilot/bootstrap/.terraform.lock.hcl': '7fd30e70f88ed9c5de18a5d13a5d91303fe233a14719dede9d6ededd3a2d0851',
 'certification/hosted-pilot/bootstrap/agent-boundary.tf': '132d5634b423e1029956d88e6748fede60b7d4b4ff3f621ef812b9b78b3ddbb0',
 'certification/hosted-pilot/bootstrap/controller-image.tf': 'f3cae720c0ab2a7f4f68af55b41493d8dc8460a2fc3257ac48486c938ee210f3',
 'certification/hosted-pilot/bootstrap/human-enrollment.tf': 'fd69a45d304562927e69144cb598a7b97fec52cbcfedf27bf35899bef43642e8',
 'certification/hosted-pilot/bootstrap/main.tf': 'cd72ea34362b5bba22ac1cbc1a90514a4c71c344081b004bc92d4388d16e3a10',
 'certification/hosted-pilot/bootstrap/outputs.tf': '4cbf86842cbcbb502e92c8c43352e695a7a3e3342ea61e14e1ca4df82b90c173',
 'certification/hosted-pilot/bootstrap/variables.tf': 'bd691c2e8c966986f7b9f05ca9eb6d1e11fb15e0e78f9c311cc31b7febc01d76',
 'certification/hosted-pilot/bootstrap/versions.tf': '7e83fb16578c7c8379d3656869cb478bce596701eeccfaa0ce297ee1b0b161f5',
 'certification/hosted-pilot/bootstrap/worker-budget-read.tf': 'c2faa4045d384ba7f66ed1c9e7ad3a66c2393ac6c2879cbc91ccf18a72f5ca76',
 'certification/hosted-pilot/foundation-environment.json': 'a2aa5e4a94dc5a9fc6168aea18e213fb1f28e416f025ff8a66f8932bf875087b',
 'tools/hosted_pilot/bind_github.py': 'a548c91b113d488a5b0eff9db4a56ea22a53e129250053fadd694086ade19e4c',
 'tools/hosted_pilot/bootstrap.py': '8c684cb615b7e2ccba3e75f3e01f615af29a7fa5a59df3b12639f04e4a5e7d1d',
 'tools/hosted_pilot/deploy.py': '09fbbc96a3858015aacc47695b993c2d560af5b4fe3e30617c75a35b884655df',
 'tools/hosted_pilot/foundation_environment.py': 'ff4ff20957736f36cc0cfd1573bf6bc86bef4dbb4cc8d19602d0dafcd7014ddb'}


def original_success_sources(record,receipt):
    """Keep this actually observed plan valid after unrelated helper evolution."""
    if record['producer'] != SUCCESS_PRODUCER:
        return None
    require(record['kind']=='foundation-plan' and record['artifact']==SUCCESS_ARTIFACT and
            receipt['release_root_sha']==SUCCESS_RELEASE_SHA and receipt['phase']=='custody-only' and
            receipt['plan_sha256']=='be3d7edfae7fab75fc565a8073168f469ed173f0cf619bd1199b7c23fa69a2fd',
            'original successful custody plan/artifact changed')
    # The normal successful-run, actor, no-effects, config and funding checks
    # still execute in history_reconciliation.authenticate/reconcile.
    return IAC_SOURCES,SUCCESS_RELEASE_SOURCES
