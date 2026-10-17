"""Verify actual Actions execution against the immutable server shard inventory.

Only the GitHub REST adapter supplies these observations. This pure verifier does not
make locally claimed JSON or a public check URL into trusted release evidence.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from urllib.parse import quote

ACTIONS_APP_ID = 15368


def positive(value):
    return type(value) is int and value > 0


def selected_run(payload, policy, sha):
    runs = [r for r in payload.get('_workflow_runs', []) if isinstance(r, dict)
            and r.get('path') == policy['workflow'] and r.get('event') in policy['events']
            and r.get('head_sha') == sha and r.get('status') == 'completed']
    if not runs:
        raise ValueError('missing completed full-matrix run for the pinned sha')
    return max(runs, key=lambda r: (str(r.get('updated_at') or r.get('created_at') or ''),
                                    r.get('id') if positive(r.get('id')) else 0))


def collect(repo, sha, run, policy, headers, fetch_collection, fetch_json):
    """Fetch all attempt histories, not only the first API page or the rerun's failed subset."""
    if not positive(run.get('id')) or not positive(run.get('run_attempt')) or not positive(run.get('check_suite_id')):
        raise ValueError('missing authoritative Actions run/attempt/suite identity')
    base = f'https://api.github.com/repos/{repo}'
    jobs = fetch_collection(f'{base}/actions/runs/{run["id"]}/jobs?filter=all', headers, 'jobs')['jobs']
    registry = fetch_json(f'{base}/contents/{quote(policy["shardInventory"]["path"], safe="/")}?ref={sha}', headers)
    if registry.get('type') != 'file' or registry.get('encoding') != 'base64':
        raise ValueError('pinned shard inventory is not a GitHub file')
    raw = base64.b64decode(registry['content'].replace('\n', ''), validate=True)
    blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    if registry.get('sha') != blob:
        raise ValueError('pinned shard inventory blob integrity differs')
    return {'repository': repo, 'runId': run['id'], 'runAttempt': run['run_attempt'], 'headSha': sha,
            'checkSuiteId': run['check_suite_id'], 'registryPath': policy['shardInventory']['path'],
            'registryBlobSha': blob, 'registryContent': raw.decode('utf-8'), 'jobs': jobs}


def verify(payload, run, policy, sha):
    """Return bounded failures; every required job links to its authentic Actions check id."""
    evidence = payload.get('_matrix_execution')
    if not isinstance(evidence, dict):
        return ['missing actual Actions job and pinned shard selection evidence']
    repo = policy['repository']
    if (not all(positive(run.get(k)) for k in ('id', 'run_attempt', 'check_suite_id'))
            or (evidence.get('repository'), evidence.get('runId'), evidence.get('runAttempt'),
                evidence.get('headSha'), evidence.get('checkSuiteId')) !=
               (repo, run['id'], run['run_attempt'], sha, run['check_suite_id'])
            or evidence.get('registryPath') != policy['shardInventory']['path']):
        return ['actual Actions execution belongs to another source/run/attempt/suite']
    try:
        raw = evidence['registryContent'].encode('utf-8')
        blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        if blob != evidence['registryBlobSha']:
            raise ValueError()
        def pairs(items):
            result = {}
            for k, v in items:
                if k in result:
                    raise ValueError()
                result[k] = v
            return result
        registry = json.loads(raw, object_pairs_hook=pairs)
        shards = registry['shards']
        names = [r['shard_name'] for r in shards]
        if (not names or any(not isinstance(n, str) or not n.strip() for n in names)
                or len(names) != len(set(names))):
            raise ValueError()
        matrix = {policy['shardInventory']['jobName'].format(shard_name=n) for n in names}
    except (ValueError, TypeError, KeyError, AttributeError):
        return ['missing, corrupt or ambiguous pinned shard inventory']
    jobs = evidence.get('jobs')
    if not isinstance(jobs, list) or not jobs:
        return ['missing actual Actions jobs']
    latest, identities = {}, set()
    for job in jobs:
        if (not isinstance(job, dict) or not positive(job.get('id')) or not positive(job.get('run_attempt'))
                or job.get('id') in identities or job.get('run_id') != run['id'] or job.get('head_sha') != sha
                or job['run_attempt'] > run['run_attempt'] or not isinstance(job.get('name'), str)):
            return ['actual Actions jobs have duplicate or wrong source/run/attempt identity']
        identities.add(job['id'])
        key = job['name']
        previous = latest.get(key)
        if previous and previous['run_attempt'] == job['run_attempt'] and key in (policy['requiredChecks'] | matrix):
            return ['actual Actions jobs have duplicate names in one attempt']
        if previous is None or previous['run_attempt'] < job['run_attempt']:
            latest[key] = job
    expected = policy['requiredChecks'] | matrix
    missing = sorted(expected - latest.keys())
    if missing:
        return ['missing expected lanes ' + str(missing)]
    prefix = policy['shardInventory']['jobName'].split('{shard_name}', 1)[0]
    extra = sorted({name for name in latest if name.startswith(prefix)} - matrix)
    if extra:
        return ['unexpected shard lanes outside pinned inventory ' + str(extra)]
    checks = {}
    for check in payload.get('check_runs', []):
        if isinstance(check, dict) and positive(check.get('id')):
            if check['id'] in checks:
                return ['duplicate Actions check identity']
            checks[check['id']] = check
    problems = []
    for name in sorted(expected):
        job = latest[name]
        if job.get('status') != 'completed' or job.get('conclusion') != 'success':
            problems.append('expected lane not successful: ' + name)
            continue
        match = re.fullmatch(r'https://api\.github\.com/repos/' + re.escape(repo) + r'/check-runs/([1-9][0-9]*)',
                             str(job.get('check_run_url', '')))
        check = checks.get(int(match.group(1))) if match else None
        if (not check or check.get('name') != name or check.get('head_sha') != sha
                or check.get('app', {}).get('id') != ACTIONS_APP_ID or check.get('app', {}).get('slug') != 'github-actions'
                or check.get('check_suite', {}).get('id') != run['check_suite_id']
                or check.get('status') != 'completed' or check.get('conclusion') != 'success'):
            problems.append('missing authentic Actions publisher/check suite: ' + name)
        if name in matrix:
            steps = [s for s in job.get('steps', []) if isinstance(s, dict) and s.get('name') == policy['shardInventory']['testStep']]
            if len(steps) != 1 or steps[0].get('status') != 'completed' or steps[0].get('conclusion') != 'success':
                problems.append('actual shard test step missing, skipped or failed: ' + name)
    return problems
