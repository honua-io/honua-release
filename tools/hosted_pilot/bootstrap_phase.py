#!/usr/bin/env python3
"""Same-run, exact-phase approval custody. No AWS calls or binary-plan transport."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import time
import zipfile

REPOSITORY = 'honua-io/honua-iac'
WORKFLOW = '.github/workflows/hosted-pilot-bootstrap.yml'
PHASES = ('custody-only', 'remote-backed-foundation')
SCHEMA = 'honua.hosted-pilot-bootstrap-plan/v2'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def utcnow():
    return datetime.now(timezone.utc)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def api(path, *, method='GET', binary=False):
    result = subprocess.run(['gh', 'api', '--method', method, path], capture_output=True)
    require(result.returncode == 0, 'native workflow observation failed')
    return result.stdout if binary else json.loads(result.stdout)


def own_run(environment=None, call=None):
    call = api if call is None else call
    e = os.environ if environment is None else environment
    require(e.get('GITHUB_REPOSITORY') == REPOSITORY and e.get('GITHUB_REF') == 'refs/heads/trunk', 'phase gate requires protected IaC trunk')
    require(re.fullmatch(r'[0-9]+', e.get('GITHUB_RUN_ID', '')) and re.fullmatch(r'[0-9]+', e.get('GITHUB_RUN_ATTEMPT', '')), 'invalid own run identity')
    require(re.fullmatch(r'[a-f0-9]{40}', e.get('GITHUB_SHA', '')), 'invalid workflow source')
    require(e.get('GITHUB_WORKFLOW_REF') == REPOSITORY+'/'+WORKFLOW+'@refs/heads/trunk' and e.get('GITHUB_WORKFLOW_SHA') == e['GITHUB_SHA'], 'phase gate requires its exact source-bound workflow context')
    base = f'repos/{REPOSITORY}'
    run = call(f'{base}/actions/runs/{e["GITHUB_RUN_ID"]}/attempts/{e["GITHUB_RUN_ATTEMPT"]}')
    workflow = call(f'{base}/actions/workflows/hosted-pilot-bootstrap.yml')
    require(run.get('id') == int(e['GITHUB_RUN_ID']) and run.get('run_attempt') == int(e['GITHUB_RUN_ATTEMPT']) and
            run.get('head_sha') == e['GITHUB_SHA'] and run.get('head_branch') == 'trunk' and run.get('event') == 'workflow_dispatch' and
            run.get('workflow_id') == workflow.get('id') and workflow.get('path') == WORKFLOW,
            'native run differs from exact current source, attempt or workflow')
    return run


def deadlines(configuration, *, now=None):
    now = now or utcnow()
    # The apply window is reserved in advance; approval never extends expiry.
    outer = min(stamp(configuration['expires_at']), stamp(configuration['budget_ledger']['expires_at']))
    execution = min(now + timedelta(minutes=35), outer)
    approval = min(now + timedelta(minutes=20), execution - timedelta(minutes=15))
    require(approval > now, 'original reservation leaves no bounded approval/apply window')
    return {'prepared_at': now.isoformat(), 'approval_expires_at': approval.isoformat(), 'execution_expires_at': execution.isoformat()}


def validate_review(review, *, phase, environment=None, now=None, approval=True, observation_only=False):
    e = os.environ if environment is None else environment
    require(review.get('schema') == SCHEMA and phase in PHASES and review.get('phase') == phase, 'review phase differs')
    require(review.get('foundation_source_sha') == e['GITHUB_SHA'] and review.get('release_root_sha') == e['PILOT_RELEASE_ROOT_SHA'] and
            review.get('run_id') == e['GITHUB_RUN_ID'] and review.get('run_attempt') == e['GITHUB_RUN_ATTEMPT'], 'review is not from this exact source and attempt')
    for field in ('config_sha256', 'provider_lock_sha256', 'resolved_config_sha256', 'plan_sha256'):
        require(isinstance(review.get(field), str) and re.fullmatch(r'[a-f0-9]{64}', review[field]), 'review lacks an exact digest')
    prepared, deadline, execution = [stamp(review[key]) for key in ('prepared_at', 'approval_expires_at', 'execution_expires_at')]
    require(prepared < deadline <= prepared + timedelta(minutes=20) and deadline + timedelta(minutes=15) <= execution <= prepared + timedelta(minutes=35), 'review deadlines are not bounded')
    current = now or utcnow()
    require(prepared <= current and (observation_only or current < (deadline if approval else execution)), 'phase approval or execution expired')
    creator = review.get('original_creation')
    require(isinstance(creator, dict) and set(creator) == {'run_id', 'identity'} and isinstance(creator['run_id'], str) and re.fullmatch(r'[0-9]+', creator['run_id']), 'original custody creation run is absent')
    origin = creator['identity']
    require(isinstance(origin, dict) and set(origin) == {'foundation_source_sha', 'release_root_sha', 'config_path', 'config_sha256', 'provider_lock_sha256'} and
        all(isinstance(origin[key], str) and re.fullmatch(r'[a-f0-9]{40}', origin[key]) for key in ('foundation_source_sha', 'release_root_sha')) and
        all(isinstance(origin[key], str) and re.fullmatch(r'[a-f0-9]{64}', origin[key]) for key in ('config_sha256', 'provider_lock_sha256')) and
        isinstance(origin['config_path'], str) and re.fullmatch(r'certification/hosted-pilot/bootstrap/deployments/[A-Za-z0-9._-]+\.tfvars\.json', origin['config_path']), 'original custody creation source is invalid')
    require(isinstance(review.get('resource_changes'), list), 'resource review is absent')
    return review


def artifacts(environment=None, call=None):
    call = api if call is None else call
    e = os.environ if environment is None else environment
    result = []
    for page in range(1, 11):
        rows = call(f'repos/{REPOSITORY}/actions/runs/{e["GITHUB_RUN_ID"]}/artifacts?per_page=100&page={page}').get('artifacts', [])
        result += rows
        if len(rows) < 100:
            return result
    raise ValueError('artifact inventory was truncated')


def artifact_json(name, filename, *, environment=None, call=None):
    call = api if call is None else call
    e = os.environ if environment is None else environment
    rows = [row for row in artifacts(e, call) if row.get('name') == name]
    if not rows:
        return None
    require(len(rows) == 1, 'ambiguous same-run artifact')
    row = rows[0]
    producer = row.get('workflow_run') or {}
    require(row.get('expired') is False and producer.get('id') == int(e['GITHUB_RUN_ID']) and producer.get('head_sha') == e['GITHUB_SHA'], 'artifact producer differs')
    require(isinstance(row.get('size_in_bytes'), int) and 0 < row['size_in_bytes'] <= 4 * 1024 * 1024, 'artifact is too large')
    raw = call(f'repos/{REPOSITORY}/actions/artifacts/{row["id"]}/zip', binary=True)
    require(len(raw) <= 4 * 1024 * 1024 and row.get('digest') == 'sha256:' + sha(raw), 'native artifact digest differs')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        require(archive.namelist() == [filename] and archive.getinfo(filename).file_size <= 4 * 1024 * 1024, 'unexpected artifact content')
        content = archive.read(filename)
    return json.loads(content), {'artifact_id': row['id'], 'artifact_sha256': sha(raw), 'content_sha256': sha(content)}


def names(phase, environment=None):
    e = os.environ if environment is None else environment
    suffix = f'{e["GITHUB_RUN_ID"]}-{e["GITHUB_RUN_ATTEMPT"]}-{phase}'
    return 'hosted-pilot-phase-review-' + suffix, 'hosted-pilot-phase-approval-' + suffix


def current_jobs(environment=None, call=None):
    call = api if call is None else call
    e = os.environ if environment is None else environment
    result = call(f'repos/{REPOSITORY}/actions/runs/{e["GITHUB_RUN_ID"]}/attempts/{e["GITHUB_RUN_ATTEMPT"]}/jobs?per_page=100')
    rows = result['jobs']
    require(result['total_count'] == len(rows) and len(rows) <= 100, 'job inventory truncated')
    require(all(row.get('head_sha') == e['GITHUB_SHA'] and row.get('run_id') == int(e['GITHUB_RUN_ID']) for row in rows), 'job source or run differs')
    require(len({row['name'] for row in rows}) == len(rows), 'ambiguous job identities')
    return {row['name']: row for row in rows}


def human_approval(policy, *, environment=None, call=None):
    call = api if call is None else call
    e = os.environ if environment is None else environment
    detail = call(f'repos/{REPOSITORY}/environments/{policy["environment"]}')
    from foundation_environment import verify_detail
    verify_detail(policy, detail)
    require(isinstance(detail.get('id'), int), 'native environment identity absent')
    rows = call(f'repos/{REPOSITORY}/actions/runs/{e["GITHUB_RUN_ID"]}/approvals')
    permitted = {(row['id'], row['login']) for row in policy['reviewers'] if row['type'] == 'User'}
    accepted = []
    for row in rows:
        user = row.get('user') or {}
        if (row.get('state') == 'approved' and user.get('type') == 'User' and (user.get('id'), user.get('login')) in permitted and
                any(env.get('id') == detail['id'] and env.get('name') == policy['environment'] for env in row.get('environments', []))):
            accepted.append({'reviewer_id': user['id'], 'reviewer_login': user['login'], 'environment_id': detail['id'], 'environment': policy['environment']})
    require(accepted, 'actual reviewed human environment approval is absent')
    return accepted[-1]


def approved_review(review, *, phase, environment=None, call=None):
    call = api if call is None else call
    e = os.environ if environment is None else environment
    own_run(e, call)
    validate_review(review, phase=phase, environment=e)
    found = artifact_json(names(phase, e)[1], 'approved.json', environment=e, call=call)
    if found is None:
        return None
    receipt, reference = found
    jobs = current_jobs(e, call)
    gate = jobs.get('phase-approval') or {}
    ready = jobs.get('review-ready') or {}
    # The upload artifact can be visible before the same job finishes post-steps.
    # Treat that native lifecycle window as pending, never as permission to apply.
    if gate.get('status') != 'completed' or ready.get('status') != 'completed':
        return None
    require(gate.get('conclusion') == 'success' and ready.get('conclusion') == 'success', 'native exact-plan approval job is not successful')
    original = artifact_json(names(phase, e)[0], 'review.json', environment=e, call=call)
    require(original is not None and original[0] == review, 'approved review differs from private original')
    require(receipt.get('schema') == 'honua.hosted-pilot-phase-approval/v1' and receipt.get('review') == review and
            receipt.get('review_reference') == original[1], 'approval receipt source, phase or saved plan differs')
    policy = json.loads((Path(__file__).resolve().parents[2] / 'certification/hosted-pilot/foundation-environment.json').read_text())
    require(receipt.get('human_approval') == human_approval(policy, environment=e, call=call), 'approval reviewer or environment differs')
    return {'receipt': receipt, 'reference': reference, 'approval_job_id': gate['id']}


def watchdog_decision(review, jobs, *, now=None):
    now = now or utcnow()
    bootstrap = jobs.get('bootstrap') or {}
    if bootstrap.get('status') == 'completed':
        return bootstrap.get('conclusion') != 'success'
    if now >= stamp(review['execution_expires_at']):
        return True
    gate = jobs.get('phase-approval') or {}
    return now >= stamp(review['approval_expires_at']) and not (gate.get('status') == 'completed' and gate.get('conclusion') == 'success')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['review-ready', 'approve', 'wait', 'watchdog'])
    parser.add_argument('--phase', required=True, choices=PHASES)
    parser.add_argument('--review-path')
    args = parser.parse_args()
    own_run()
    begin = time.monotonic()
    review = None
    while review is None:
        found = artifact_json(names(args.phase)[0], 'review.json')
        if found:
            review, reference = found
            validate_review(review, phase=args.phase, observation_only=args.operation == 'watchdog')
            break
        jobs = current_jobs()
        require((jobs.get('bootstrap') or {}).get('status') != 'completed', 'bootstrap ended without a retained review')
        require(time.monotonic() - begin < 600, 'review publication deadline reached')
        time.sleep(30)
    if args.operation == 'review-ready':
        print('Same-run phase review available; exact saved plan remains private.')
        return
    if args.operation == 'approve':
        policy = json.loads((Path(__file__).resolve().parents[2] / 'certification/hosted-pilot/foundation-environment.json').read_text())
        receipt = {'schema': 'honua.hosted-pilot-phase-approval/v1', 'review': review, 'review_reference': reference, 'human_approval': human_approval(policy)}
        path = Path(os.environ['RUNNER_TEMP']) / 'approved.json'
        path.write_bytes(canonical(receipt)); path.chmod(0o600)
        return
    if args.operation == 'watchdog':
        require(os.environ.get('GITHUB_JOB') == 'phase-watchdog', 'cancellation is confined to the source-defined watchdog job')
        while True:
            if own_run().get('status') == 'completed':
                return
            jobs = current_jobs()
            watcher = jobs.get('phase-watchdog') or {}
            require(type(watcher.get('id')) is int and watcher['id'] > 0 and watcher.get('status') == 'in_progress', 'current native watchdog job is not running')
            if (jobs.get('bootstrap') or {}).get('status') == 'completed' and (jobs.get('bootstrap') or {}).get('conclusion') == 'success':
                return
            if watchdog_decision(review, jobs):
                # No caller-supplied repository/run path is accepted. This job
                # alone has actions:write; it has no AWS credential injection.
                api(f'repos/{REPOSITORY}/actions/runs/{os.environ["GITHUB_RUN_ID"]}/cancel', method='POST', binary=True)
                print('Cancelled own expired or failed phase workflow; liabilities retained.')
                return
            time.sleep(30)
    require(args.review_path is not None, 'private exact review path required')
    private_review = json.loads(Path(args.review_path).read_text())
    require(review == private_review, 'published review differs from private prepared plan')
    while approved_review(private_review, phase=args.phase) is None:
        time.sleep(30)
    print('Actual same-run environment approval and exact phase receipt verified.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError, json.JSONDecodeError, zipfile.BadZipFile):
        raise SystemExit('phase gate refused; no approval or deadline bypass') from None
