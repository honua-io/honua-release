"""Authenticate this workflow's explicit phase metadata without resetting originals."""
from datetime import timedelta
import json
import hashlib
import io
import zipfile
import re

from bootstrap_phase import stamp, validate_review
from deploy import require


def review_name(producer, phase):
    require(phase in ('custody-only', 'remote-backed-foundation'), 'unknown foundation phase')
    return f'hosted-pilot-phase-review-{producer["runId"]}-{producer["runAttempt"]}-{phase}'


def job(jobs, name):
    selected = [row for row in jobs['jobs'] if row['name'] == name]
    require(len(selected) == 1, 'original phase job missing or ambiguous')
    return selected[0]


def step(jobs, name):
    selected = [row for row in job(jobs, 'bootstrap')['steps'] if row['name'] == name]
    require(len(selected) == 1, 'original phase step missing or ambiguous')
    return selected[0]


def successful(row):
    require(row.get('status') == 'completed' and row.get('conclusion') == 'success', 'original phase observation was not successful')


def original_review(record, review, jobs):
    producer = record['producer']
    selected = review.get('phase')
    require(review.get('schema') == 'honua.hosted-pilot-bootstrap-plan/v2', 'phase review schema differs')
    environment = {'GITHUB_SHA': producer['sourceSha'], 'PILOT_RELEASE_ROOT_SHA': review['release_root_sha'],
        'GITHUB_RUN_ID': str(producer['runId']), 'GITHUB_RUN_ATTEMPT': str(producer['runAttempt'])}
    # Historical authentication uses original native preparation time, never a
    # fresh deadline or a reset of the original funding/history envelope.
    validate_review(review, phase=selected, environment=environment, now=stamp(review['prepared_at']))
    preparation = step(jobs, 'Prepare exactly one source-reviewed phase without applying')
    successful(preparation)
    prepared = stamp(review['prepared_at'])
    require(stamp(preparation['started_at']) - timedelta(seconds=30) <= prepared <= stamp(preparation['completed_at']) + timedelta(seconds=30),
        'original phase preparation time differs from native execution')
    return review


def plan_only(record, review, jobs, artifacts):
    original_review(record, review, jobs)
    producer = record['producer']
    require(record['artifact']['name'] == review_name(producer, review['phase']) and
        record['planArtifact'] is None and record['ownerLedger'] is None and not record['cloudEvents'],
        'a phase source plan cannot claim owner publication or applied effects')
    for name in ('Wait for original exact-plan approval from the separate protected job', 'Apply only the exact reviewed phase and stop'):
        require(step(jobs, name).get('conclusion') == 'skipped', 'source plan classification conflicts with native apply admission')
    for name in ('phase-approval', 'phase-watchdog', 'review-ready'):
        require(job(jobs, name).get('conclusion') == 'skipped', 'source plan classification conflicts with an active approval path')
    prefixes = ('hosted-pilot-phase-approval-', 'hosted-pilot-phase-admitted-', 'hosted-pilot-custody-applied-', 'hosted-pilot-foundation-bindings-')
    require(not any(row['name'].startswith(prefixes) for row in artifacts), 'source plan classification conflicts with actual execution artifacts')


def entry_digest(producer, reference, entry, call):
    raw = call(['gh', 'api', 'repos/'+producer['repository']+'/actions/artifacts/'+str(reference['id'])+'/zip'])
    require('sha256:'+hashlib.sha256(raw).hexdigest() == reference['sha256'], 'original phase archive changed')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(archive.namelist() == [entry], 'original phase entry differs')
        return hashlib.sha256(archive.read(entry)).hexdigest()


def applied(record, receipt, jobs, *, native_artifact, api, source_file, call):
    producer = record['producer']; base = 'repos/' + producer['repository']
    custody = record['kind'] == 'foundation-custody'
    if custody:
        require(receipt.get('schema') == 'honua.hosted-pilot-custody-applied/v1' and record['ownerLedger'] is None,
            'custody-only execution cannot invent a published owner ledger')
        review = receipt['review']; proof = receipt['approval']
        creator = receipt.get('original_creation')
        require(isinstance(creator, dict) and set(creator) == {'run_id', 'identity'} and re.fullmatch(r'[0-9]+', creator['run_id']) and
            set(creator['identity']) == {'foundation_source_sha', 'release_root_sha', 'config_path', 'config_sha256', 'provider_lock_sha256'} and creator == review.get('original_creation'), 'original custody creation identity was lost')
        require(review['phase'] == 'custody-only', 'custody-only receipt names a different phase')
    else:
        review = receipt['phase_review']; proof = receipt['phase_approval']
        require(review['phase'] == 'remote-backed-foundation', 'full foundation receipt names a different phase')
    original_review(record, review, jobs)
    reference = record['planArtifact']
    require(reference is not None and reference['name'] == review_name(producer, review['phase']), 'original phase review artifact is absent')
    require(native_artifact(producer, reference, 'review.json', call) == review, 'applied phase differs from its original source/plan review')
    for name in ('review-ready', 'phase-approval', 'phase-watchdog'):
        successful(job(jobs, name))
    gate = job(jobs, 'phase-approval')
    require(proof.get('approval_job_id') == gate['id'] and stamp(gate['completed_at']) < stamp(review['approval_expires_at']),
        'original protected approval did not finish before its immutable deadline')
    for name in ('Wait for original exact-plan approval from the separate protected job', 'Apply only the exact reviewed phase and stop'):
        successful(step(jobs, name))
    expected = f'hosted-pilot-phase-approval-{producer["runId"]}-{producer["runAttempt"]}-{review["phase"]}'
    original = proof['reference']
    approval_ref = {'id': original['artifact_id'], 'name': expected, 'sha256': 'sha256:'+original['artifact_sha256']}
    approval = native_artifact(producer, approval_ref, 'approved.json', call)
    require(entry_digest(producer, approval_ref, 'approved.json', call) == original['content_sha256'], 'original approval entry hash changed')
    require(approval == proof['receipt'] and approval['schema'] == 'honua.hosted-pilot-phase-approval/v1' and approval['review'] == review and
        approval['review_reference'] == {'artifact_id': reference['id'], 'artifact_sha256': reference['sha256'].removeprefix('sha256:'), 'content_sha256': entry_digest(producer, reference, 'review.json', call)},
        'original protected approval artifact differs from the applied phase')
    policy = json.loads(source_file('honua-io/honua-release', review['release_root_sha'], 'certification/hosted-pilot/foundation-environment.json', call))
    human = approval['human_approval']
    permitted = {(row['id'], row['login']) for row in policy['reviewers'] if row['type'] == 'User'}
    require((human['reviewer_id'], human['reviewer_login']) in permitted and human['environment'] == policy['environment'], 'original phase reviewer differs from its reviewed source policy')
    native_reviews = api(base+f'/actions/runs/{producer["runId"]}/approvals', call)
    require(any(row.get('state') == 'approved' and row.get('user', {}).get('type') == 'User' and
        (row['user'].get('id'), row['user'].get('login')) == (human['reviewer_id'], human['reviewer_login']) and
        any(env.get('id') == human['environment_id'] and env.get('name') == human['environment'] for env in row.get('environments', [])) for row in native_reviews),
        'original phase lacks actual native human approval history')
    listing = api(base+f'/actions/runs/{producer["runId"]}/artifacts?per_page=100', call)
    require(len(listing['artifacts']) == listing['total_count'], 'original phase artifact inventory truncated')
    name = f'hosted-pilot-phase-admitted-{producer["runId"]}-{producer["runAttempt"]}-{review["phase"]}'
    admitted = [row for row in listing['artifacts'] if row['name'] == name]
    require(len(admitted) == 1 and re.fullmatch(r'sha256:[a-f0-9]{64}', admitted[0].get('digest', '')), 'original apply admission artifact missing')
    require(re.fullmatch(r'[a-f0-9]{64}', receipt.get('apply_admission_sha256', '')), 'applied receipt lost its original admission digest')
    admission = native_artifact(producer, {'id': admitted[0]['id'], 'name': name, 'sha256': admitted[0]['digest']}, 'apply-admitted.json', call)
    admission_ref = {'id': admitted[0]['id'], 'name': name, 'sha256': admitted[0]['digest']}
    require(entry_digest(producer, admission_ref, 'apply-admitted.json', call) == receipt['apply_admission_sha256'], 'original admission entry hash changed')
    execution = step(jobs, 'Apply only the exact reviewed phase and stop')
    require(admission.get('schema') == 'honua.hosted-pilot-phase-admitted/v1' and admission['review'] == review and admission['approval'] == proof and
        admission.get('financial_settlement') is False and admission.get('partial_failure_liability_retained') is True and
        stamp(execution['started_at']) <= stamp(admission['admitted_at']) < stamp(review['approval_expires_at']) and
        stamp(admission['admitted_at']) <= stamp(execution['completed_at']) <= stamp(review['execution_expires_at']),
        'original exact-plan admission, execution deadline or liability retention differs')
    # Supply the same original actor/creation-resource shape used by independent
    # CloudTrail actor + exact planned-name + actual current tag authentication.
    return dict(review, original_creation=receipt['original_creation'], foundation_run_id=review['run_id'], foundation_run_attempt=review['run_attempt'], saved_plan_sha256=review['plan_sha256']) if custody else receipt
