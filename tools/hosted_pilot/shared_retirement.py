"""A bounded scheduled cleanup attempt using the existing saved Terraform plan.

This module never synthesizes a backend lock or a human approval. Waiting is
inside Terraform's destroy-time provisioner after its native backend lock has
been acquired. A successful wait authorizes continuation of that saved plan;
it does not report deletion, settlement, or returned capacity.
"""
from __future__ import annotations
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import time
from datetime import datetime, timezone
from uuid import UUID

CLEANUP_SECONDS = 900
SESSION_SECONDS = 3600
JOB_SECONDS = 90*60
SAFETY_SECONDS = 120
LOCK_LIMIT = 65536
REPOSITORY = 'honua-io/honua-release'
WORKFLOW = '.github/workflows/hosted-pilot-deploy.yml'
SCHEMA = 'honua.shared-retirement-wait/v1'


def require(value, reason):
    if not value:
        raise ValueError(reason)


def epoch(value):
    require(isinstance(value, str), 'retirement requires original UTC time')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0,
            'retirement time must use UTC')
    result = parsed.timestamp()
    require(result > 0 and result == int(result), 'retirement time must use whole seconds')
    return int(result)


def window(*, funding_expiry, retirement_at, credential_expiry, job_started, now):
    values = (funding_expiry, retirement_at, credential_expiry, job_started, now)
    require(all(type(v) is int and v > 0 for v in values), 'retirement window needs original integral times')
    require(retirement_at == funding_expiry-CLEANUP_SECONDS and job_started <= now < retirement_at,
            'scheduled cleanup attempt must preserve its original funding deadline')
    require(funding_expiry+SAFETY_SECONDS <= min(credential_expiry, job_started+JOB_SECONDS),
            'scheduled cleanup attempt cannot fit original credentials and job timeout')
    # Expiration is the actual STS response, not a caller's requested duration.
    require(now < credential_expiry <= now+SESSION_SECONDS+60,
            'retirement credentials are missing, expired or outside the existing grant')
    return {'retirementAtEpoch': retirement_at, 'plannedCompletionByEpoch': funding_expiry,
            'credentialExpiresEpoch': credential_expiry, 'jobExpiresEpoch': job_started+JOB_SECONDS,
            'cleanupSeconds': CLEANUP_SECONDS, 'safetySeconds': SAFETY_SECONDS}



def recovery_window(*, funding_expiry, retirement_at, credential_expiry, job_started, now):
    values=(funding_expiry,retirement_at,credential_expiry,job_started,now)
    require(all(type(value) is int and value > 0 for value in values) and
            retirement_at == funding_expiry-CLEANUP_SECONDS and job_started <= now < job_started+CLEANUP_SECONDS and now >= retirement_at,
            'cleanup recovery requires the unchanged elapsed original deadline')
    require(job_started+CLEANUP_SECONDS+SAFETY_SECONDS <= min(credential_expiry,job_started+JOB_SECONDS) and
            now < credential_expiry <= now+SESSION_SECONDS+60,
            'cleanup recovery cannot fit its current single credential grant and job')
    return {'retirementAtEpoch':retirement_at,'originalFundingExpiresEpoch':funding_expiry,
            'currentCleanupAttemptByEpoch':job_started+CLEANUP_SECONDS,'credentialExpiresEpoch':credential_expiry,
            'jobExpiresEpoch':job_started+JOB_SECONDS,'cleanupSeconds':CLEANUP_SECONDS,'safetySeconds':SAFETY_SECONDS}

def lock_original(raw, *, bucket, key, expected_who, earliest_epoch, now):
    require(type(raw) is bytes and 0 < len(raw) <= LOCK_LIMIT, 'native Terraform lock is missing or oversized')
    value = json.loads(raw)
    require(isinstance(value, dict) and set(value) == {'ID','Operation','Info','Who','Version','Created','Path'},
            'native Terraform lock has an unrecognized shape')
    lock_id = str(UUID(value['ID']))
    require(lock_id == value['ID'] and value['Operation'] == 'OperationTypeApply' and
            value['Who'] == expected_who and value['Version'] == '1.13.5' and
            value['Path'] == bucket+'/'+key and isinstance(value['Info'], str),
            'native Terraform lock owner, operation, backend or tool version differs')
    # Terraform preserves subsecond precision; compare it without inventing a
    # rounded timestamp on the existing accounting operation.
    created = datetime.fromisoformat(value['Created'].replace('Z', '+00:00'))
    require(created.tzinfo is not None and earliest_epoch <= created.timestamp() <= now+5,
            'native Terraform lock does not belong to this admitted job')
    return {'id': lock_id, 'sha256':'sha256:'+hashlib.sha256(raw).hexdigest(),
            'original': value}


def wait_for_original_deadline(binding, *, observe_lock, retain, clock=time.time,
                               monotonic=time.monotonic, sleep=time.sleep):
    """Wait under one observed native lock; interruption is always non-success.

    Injectable readers/clock/retainer are a Python testing interface only. The
    fixed CLI constructs them from actual SDK/job observations; no CLI bypass
    or caller-supplied lock/approval JSON is accepted.
    """
    now = int(clock())
    recovery = binding.get('cleanupRecovery',False)
    require(type(recovery) is bool,'invalid cleanup recovery selection')
    original_window = (recovery_window if recovery else window)(funding_expiry=binding['originalFundingExpiresEpoch'],
        retirement_at=binding['retirementAtEpoch'], credential_expiry=binding['credentialExpiresEpoch'],
        job_started=binding['jobStartedEpoch'], now=now)
    original_lock = observe_lock()
    require(isinstance(original_lock, dict) and set(original_lock) == {'id','sha256','original'},
            'retirement did not observe an original Terraform lock')
    base = {'schema':SCHEMA, 'producer':binding['producer'], 'candidateKey':binding['candidateKey'],
            'sourceConfiguration':binding['sourceConfiguration'], 'plan':binding['plan'],
            'backend':binding['backend'], 'nativeApproval':binding['nativeApproval'],
            'window':original_window, 'lock':original_lock, 'activation':False, 'qualification':False}
    if recovery:
        require(observe_lock() == original_lock,'original cleanup recovery lock changed')
        retain({**base,'stage':'recovery-cleanup-attempt-starting'})
        return base
    retain({**base, 'stage':'waiting'})
    start = monotonic()
    maximum_elapsed = original_window['retirementAtEpoch']-now
    while int(clock()) < original_window['retirementAtEpoch']:
        require(monotonic()-start <= maximum_elapsed+5,
                'retirement wall clock moved backwards; preserve unknown obligations')
        require(int(clock())+CLEANUP_SECONDS+SAFETY_SECONDS <=
                min(original_window['credentialExpiresEpoch'], original_window['jobExpiresEpoch']),
                'original retirement credential or job window was exhausted')
        require(observe_lock() == original_lock, 'native Terraform backend lock changed while waiting')
        sleep(min(5, max(0, original_window['retirementAtEpoch']-clock())))
    require(int(clock()) < original_window['plannedCompletionByEpoch'] and observe_lock() == original_lock,
            'retirement deadline/lock was lost; no late continuation')
    retain({**base, 'stage':'cleanup-attempt-starting'})
    return base

TARGETS = ('module.application', 'module.network', 'aws_security_group.controller',
           'aws_security_group.worker', 'terraform_data.shared_retirement')
GATE = 'terraform_data.shared_retirement[0]'


def target_only_plan(plan):
    """Inspect the complete native plan, not a caller's selected action list."""
    rows = plan.get('resource_changes')
    require(isinstance(rows, list) and rows, 'scheduled cleanup plan is empty or incomplete')
    deleting = []
    for row in rows:
        address, actions = row.get('address'), row.get('change', {}).get('actions')
        require(isinstance(address, str) and isinstance(actions, list), 'scheduled cleanup plan row is malformed')
        if actions in (['no-op'], ['read']):
            continue
        require(actions == ['delete'] and any(address == prefix or address.startswith(prefix+'.') or
                address.startswith(prefix+'[') for prefix in TARGETS),
                'scheduled cleanup plan changes retained custody/accounting or unreviewed resources')
        deleting.append(address)
    require(GATE in deleting and any(p.startswith('module.application.') for p in deleting) and
            any(p.startswith('module.network.') for p in deleting),
            'scheduled cleanup plan must delete the staged gate and complete target scope')
    return sorted(deleting)


def staged_gate(state, expected):
    require(isinstance(state, dict) and state.get('version') == 4, 'original Terraform state is unsupported')
    rows = [row for row in state.get('resources', []) if row.get('mode') == 'managed' and
            row.get('type') == 'terraform_data' and row.get('name') == 'shared_retirement' and not row.get('module')]
    require(len(rows) == 1 and len(rows[0].get('instances', [])) == 1,
            'cleanup gate must already exist before the saved destroy plan')
    instance = rows[0]['instances'][0]
    require(instance.get('index_key') == 0 and instance.get('status') != 'tainted' and
            instance.get('attributes', {}).get('input', {}).get('value') == expected,
            'original cleanup gate is missing, tainted or has a changed deadline')
    require(all(any(isinstance(dependency,str) and dependency.startswith(prefix+'.')
            for dependency in instance.get('dependencies',[])) for prefix in ('module.application','module.network')),
            'original cleanup gate lacks target deletion dependencies')
    return instance


def native_job(*, source_sha, run_id, run_attempt, policy, call):
    """Actual GitHub job/approval originals. No approval JSON is accepted."""
    base = f'repos/{REPOSITORY}'
    run = call(base+f'/actions/runs/{run_id}')
    require(run.get('id') == run_id and run.get('run_attempt') == run_attempt and
            run.get('head_sha') == source_sha and run.get('head_branch') == 'trunk' and
            run.get('event') == 'workflow_dispatch' and run.get('path') == WORKFLOW and
            run.get('status') == 'in_progress', 'scheduled cleanup original workflow is not current')
    jobs = call(base+f'/actions/runs/{run_id}/attempts/{run_attempt}/jobs?per_page=100')
    require(type(jobs.get('total_count')) is int and jobs['total_count'] <= 100 and
            jobs['total_count'] == len(jobs.get('jobs', [])), 'scheduled cleanup jobs are incomplete')
    apply = [row for row in jobs['jobs'] if row.get('name') == 'apply']
    plan = [row for row in jobs['jobs'] if row.get('name') == 'plan']
    require(len(apply) == len(plan) == 1 and apply[0].get('status') == 'in_progress' and
            apply[0].get('conclusion') is None and plan[0].get('status') == 'completed' and
            plan[0].get('conclusion') == 'success', 'original plan/apply job is absent, cancelled or unsuccessful')
    from foundation_environment import verify_detail
    require(policy['repository'] == REPOSITORY and policy['environment'] == 'hosted-pilot-apply' and
            policy.get('can_admins_bypass') is False, 'scheduled cleanup requires its reviewed native protection')
    detail = call(base+'/environments/'+policy['environment'])
    verify_detail(policy, detail)
    require(type(detail.get('id')) is int, 'native protected environment identifier is absent')
    permitted = {(row['id'], row['login']) for row in policy['reviewers'] if row['type'] == 'User'}
    approvals = call(base+f'/actions/runs/{run_id}/approvals')
    accepted = [row for row in approvals if row.get('state') == 'approved' and
        row.get('user', {}).get('type') == 'User' and
        (row['user'].get('id'),row['user'].get('login')) in permitted and
        any(env.get('id') == detail['id'] and env.get('name') == policy['environment']
            for env in row.get('environments', []))]
    require(accepted, 'actual same-run human approval is absent')
    approval = accepted[-1]
    started = epoch(apply[0]['started_at'])
    return {'run':run, 'job':apply[0], 'planJob':plan[0], 'approval':approval, 'jobStartedEpoch':started}


def main():
    """Fixed destroy provisioner entry. No lock/approval/clock override exists."""
    import deploy
    from bind_github import api
    root = Path(__file__).resolve().parents[2]
    e = os.environ
    require((e.get('PILOT_SCHEDULED_CLEANUP'),e.get('PILOT_CLEANUP_RECOVERY','false')) in (('true','false'),('false','true')) and e.get('PILOT_OPERATION') == 'destroy' and
            e.get('PILOT_TERRAFORM_ROOT') == 'certification/hosted-pilot/campaign' and
            e.get('GITHUB_REPOSITORY') == REPOSITORY and e.get('GITHUB_REF') == 'refs/heads/trunk' and
            e.get('GITHUB_EVENT_NAME') == 'workflow_dispatch' and
            e.get('GITHUB_WORKFLOW_REF') == REPOSITORY+'/'+WORKFLOW+'@refs/heads/trunk',
            'scheduled cleanup requires its exact protected workflow')
    work = Path(e['RUNNER_TEMP'])/'hosted-pilot-private'
    cfg = deploy.config(e['PILOT_CONFIG_PATH'])
    review = json.loads((work/'review.json').read_text())
    backend_path = root/e['PILOT_TERRAFORM_ROOT']/'backend.tf.json'
    backend = json.loads(backend_path.read_text())['terraform']['backend']['s3']
    backend_hash = hashlib.sha256(json.dumps(backend,sort_keys=True).encode()).hexdigest()
    deploy.verify(review, work, cfg, backend_hash)
    scope = configuration(json.loads(cfg.read_text()))
    recovery=e.get('PILOT_CLEANUP_RECOVERY') == 'true'
    require(review.get('scheduled_cleanup') == dict(scope,mode='recovery' if recovery else 'scheduled') and
            epoch(e['PILOT_WAIT_FUNDING_TIME']) == scope['originalFundingExpiresEpoch'] and
            epoch(e['PILOT_WAIT_RETIREMENT_TIME']) == scope['retirementAtEpoch'],
            'saved plan or staged gate has a different original deadline')
    budget_path = Path(e['RUNNER_TEMP'])/'pilot-budget.json'
    deploy.aws('s3api','get-object','--bucket',e['PILOT_PLAN_BUCKET'],'--key','hosted-pilot/budget/ledger.json',
        '--version-id',e['PILOT_BUDGET_VERSION_ID'],str(budget_path))
    require(deploy.digest(budget_path) == e['PILOT_BUDGET_SHA256'] and
            epoch(json.loads(budget_path.read_text())['expires_at']) == scope['originalFundingExpiresEpoch'],
            'original versioned budget expiry differs; scheduled cleanup cannot renew funding')
    call = lambda path: api('GET',path)
    policy = json.loads((root/'certification/hosted-pilot/apply-environment.json').read_text())
    def observe_job():
        return native_job(source_sha=e['GITHUB_SHA'],run_id=int(e['GITHUB_RUN_ID']),
            run_attempt=int(e['GITHUB_RUN_ATTEMPT']),policy=policy,call=call)
    observed = observe_job()
    backend = json.loads(backend_path.read_text())['terraform']['backend']['s3']
    binding = {**scope,'cleanupRecovery':recovery,'credentialExpiresEpoch':epoch(e['PILOT_AWS_EXPIRATION']),
        'jobStartedEpoch':observed['jobStartedEpoch'],
        'producer':{'runId':int(e['GITHUB_RUN_ID']),'runAttempt':int(e['GITHUB_RUN_ATTEMPT']),
                    'sourceSha':e['GITHUB_SHA'],'workflow':WORKFLOW,'jobId':observed['job']['id']},
        'candidateKey':'sha256:'+json.loads(cfg.read_text())['application']['candidate']['manifest_sha256'],
        'sourceConfiguration':{'path':e['PILOT_CONFIG_PATH'],'sha256':'sha256:'+deploy.digest(cfg)},
        'plan':{'sha256':'sha256:'+review['plan_sha256'],'reviewSha256':'sha256:'+e['PILOT_REVIEW_SHA256'],
                'versionId':review['s3_version_id']},
        'backend':{'sha256':'sha256:'+backend_hash,'bucket':backend['bucket'],'key':backend['key']},
        'nativeApproval':observed['approval']}
    lock_file = work/'original-backend-lock.json'
    last_job = time.monotonic()
    def observe_lock():
        nonlocal last_job
        if time.monotonic()-last_job >= 30:
            current = observe_job()
            require(current['job']['id'] == observed['job']['id'] and current['approval'] == observed['approval'] and
                    current['jobStartedEpoch'] == observed['jobStartedEpoch'], 'original scheduled cleanup authority changed')
            last_job = time.monotonic()
        deploy.aws('s3api','get-object','--bucket',backend['bucket'],'--key',backend['key']+'.tflock',str(lock_file))
        return lock_original(lock_file.read_bytes(),bucket=backend['bucket'],key=backend['key'],
            expected_who=getpass.getuser()+'@'+socket.gethostname(),earliest_epoch=observed['jobStartedEpoch'],now=time.time())
    def retain(value):
        path = work/('scheduled-cleanup-'+value['stage']+'.json')
        deploy.private_json(path,value)
        key = 'hosted-pilot/reservations/scheduled-cleanup/'+e['GITHUB_RUN_ID']+'/'+e['GITHUB_RUN_ATTEMPT']+'/'+value['stage']+'.json'
        receipt = deploy.aws('s3api','put-object','--bucket',e['PILOT_PLAN_BUCKET'],'--key',key,'--body',str(path),
            '--if-none-match','*','--server-side-encryption','aws:kms','--ssekms-key-id',e['PILOT_PLAN_KMS_ARN'])
        require(receipt.get('VersionId') not in (None,'','null'), 'scheduled cleanup witness lacks immutable custody')
    wait_for_original_deadline(binding,observe_lock=observe_lock,retain=retain)


def configuration(cfg):
    value = cfg.get('shared_retirement')
    require(isinstance(value,dict) and set(value) == {'original_funding_expires_at','retirement_at'},
            'scheduled cleanup needs the original source-defined staging deadline')
    funding, retirement = epoch(value['original_funding_expires_at']),epoch(value['retirement_at'])
    require(retirement == funding-CLEANUP_SECONDS, 'scheduled cleanup must preserve its original funding deadline')
    return {'originalFundingExpiresEpoch':funding,'retirementAtEpoch':retirement,'targets':list(TARGETS)}


if __name__ == '__main__':
    try:
        main()
    except (ValueError,KeyError,OSError,json.JSONDecodeError):
        raise SystemExit('scheduled cleanup refused; preserve unresolved obligations and original executor diagnostics')
