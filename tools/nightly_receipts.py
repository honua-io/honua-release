#!/usr/bin/env python3
"""Dispatch the nightly producers and locate the receipts their runs published.

The nightly train does not accept a hand-supplied URL. The workflow-dispatch API takes a
branch or tag, never a commit (a SHA answers 422 "No ref found"), so each producer runs on
a dispatchable ref and the exact commit travels as an input it checks out and verifies:

- capacity: honua-server's producer refuses unless its own workflow source, its checkout
  and `candidate_sha` are one commit, and the release checker requires the same. It is
  dispatched on refs/tags/nightly-candidate/<sha>, a tag whose head is that commit.
- DR: gate-dr accepts only receipts attested from refs/heads/trunk. The drill is
  dispatched on trunk with `candidate_ref`; candidate-input verifies the snapshot descends
  from reviewed trunk even when trunk has moved since the nightly started.

- protocol certification: each producer the candidate's requirements catalog names
  (`production.producers`) runs at its pinned source revision with the candidate image,
  source sha and cut as inputs. honua-evidence harvests producer runs by that revision, so a
  pin that is the producer's trunk head is dispatched on trunk and any other pin on
  refs/tags/nightly-candidate/<pin>. honua-evidence aggregate.yml then joins them on trunk.
  Every producer also receives `nightly_dispatch_id`, this nightly's correlation id, and records
  it with its own run identity in each receipt, so the bind accepts only runs this night dispatched.

Run ids come from the dispatch response (`return_run_details`), not a racy run listing.
Neither receipt lookup follows a branch tip.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

SHA = re.compile(r'[0-9a-f]{40}\Z')
RUN = re.compile(r'[1-9][0-9]*\Z')
DISPATCH_REF = re.compile(r'refs/(?:heads|tags)/[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z')
SERVER_IMAGE = re.compile(r'ghcr\.io/honua-io/honua-server@sha256:[0-9a-f]{64}\Z')
CANDIDATE_TAG = 'refs/tags/nightly-candidate/'
CAPACITY_WORKFLOW = 'capacity-soak-candidate.yml'
DR_WORKFLOW = 'dr-drill-local-docker.yml'
AGGREGATE_WORKFLOW = 'aggregate.yml'
CANDIDATE_PLACEHOLDERS = ('{server_image}', '{server_sha}', '{cut_at}')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
CUT = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z')
DISPATCH_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
DISPATCH_ID_INPUT = 'nightly_dispatch_id'
DELAYS = (0, 10, 30, 60, 120, 60)


class ApiError(RuntimeError):
    def __init__(self, status: int | None, message: str):
        super().__init__(f'HTTP {status}: {message}' if status else message)
        self.status = status


class Gh:
    """`gh api` with JSON bodies. Reads retry transient failures for about five minutes;
    a write retries only when the connection was never made, so nothing is dispatched twice."""

    def __init__(self, run=subprocess.run, sleep=time.sleep):
        self.run, self.sleep = run, sleep

    def __call__(self, method: str, path: str, body: dict | None = None):
        command = ['gh', 'api', '--method', method, path]
        if body is not None:
            command += ['--input', '-']
        env = {**os.environ, 'NO_COLOR': '1', 'GH_FORCE_TTY': '0'}
        for index, delay in enumerate(DELAYS):
            if delay:
                self.sleep(delay)
            result = self.run(command, input=None if body is None else json.dumps(body),
                              capture_output=True, text=True, env=env)
            if result.returncode == 0:
                return json.loads(result.stdout) if result.stdout.strip() else None
            status, message = _error(result)
            detail = (result.stderr or '').lower()
            unconnected = any(term in detail for term in ('error connecting', 'could not resolve host'))
            transient = unconnected or (method == 'GET' and (
                status in {403, 429, 500, 502, 503, 504}
                or any(term in detail for term in ('connection reset', 'timeout', 'timed out', 'tls'))))
            if not transient or index == len(DELAYS) - 1:
                raise ApiError(status, message)
        raise AssertionError('unreachable')


def _error(result) -> tuple[int | None, str]:
    try:
        body = json.loads(result.stdout or '')
    except json.JSONDecodeError:
        body = {}
    status = body.get('status') if isinstance(body, dict) else None
    match = re.search(r'\(HTTP (\d{3})\)', result.stderr or '')
    if status is None and match:
        status = match.group(1)
    message = (body.get('message') if isinstance(body, dict) else None) or (result.stderr or '').strip()
    return (int(status) if str(status).isdigit() else None), message


def _repository(value: str) -> str:
    if value.count('/') != 1 or any(not part for part in value.split('/')):
        raise ValueError('repository must be owner/name')
    return value


def ensure_candidate_tag(api, repository: str, sha: str) -> str:
    """A tag whose head is the candidate commit; an existing tag must already point there."""
    if not SHA.fullmatch(sha):
        raise ValueError('candidate must be a full commit sha')
    ref = CANDIDATE_TAG + sha
    try:
        api('POST', f'repos/{repository}/git/refs', {'ref': ref, 'sha': sha})
    except ApiError as exc:
        if exc.status != 422:  # 422 is "Reference already exists"; anything else is fatal.
            raise
    existing = api('GET', f'repos/{repository}/git/ref/{ref.removeprefix("refs/")}')
    target = (existing or {}).get('object') or {}
    if (existing or {}).get('ref') != ref or target.get('type') != 'commit' or target.get('sha') != sha:
        raise LookupError(f'{repository} {ref} does not point at commit {sha}')
    return ref


def dispatch(api, repository: str, workflow: str, ref: str, inputs: dict[str, str]) -> int:
    if not DISPATCH_REF.fullmatch(ref):
        raise ValueError(f'workflow dispatch takes refs/heads/<branch> or refs/tags/<tag>, not {ref!r}')
    if not all(isinstance(value, str) for value in inputs.values()):
        raise ValueError('workflow dispatch inputs must be strings')
    response = api('POST', f'repos/{repository}/actions/workflows/{workflow}/dispatches',
                   {'ref': ref, 'inputs': inputs, 'return_run_details': True})
    run_id = (response or {}).get('workflow_run_id') if isinstance(response, dict) else None
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
        raise LookupError(f'{repository} {workflow} dispatch returned no run id')
    return run_id


def verify_run(run, *, run_id: int, workflow: str, head_sha: str | None = None,
               head_branch: str | None = None) -> dict:
    if not isinstance(run, dict):
        raise ValueError('workflow run must be an object')
    problems = []
    if run.get('id') != run_id:
        problems.append(f"id {run.get('id')!r}")
    if run.get('event') != 'workflow_dispatch':
        problems.append(f"event {run.get('event')!r}")
    if run.get('path') != f'.github/workflows/{workflow}':
        problems.append(f"path {run.get('path')!r}")
    if head_sha is not None and run.get('head_sha') != head_sha:
        problems.append(f"head {run.get('head_sha')!r} is not {head_sha}")
    if head_branch is not None and run.get('head_branch') != head_branch:
        problems.append(f"branch {run.get('head_branch')!r} is not {head_branch}")
    if problems:
        raise LookupError(f'run {run_id} is not the dispatched {workflow}: ' + ', '.join(problems))
    return run


def dispatch_capacity(api, repository: str, candidate_sha: str, candidate_image: str, lock_ref: str) -> int:
    _repository(repository)
    if not SHA.fullmatch(candidate_sha) or not SHA.fullmatch(lock_ref):
        raise ValueError('capacity candidate and lock ref must be full commit shas')
    if not SERVER_IMAGE.fullmatch(candidate_image):
        raise ValueError('capacity candidate image must be the GHCR digest reference')
    ref = ensure_candidate_tag(api, repository, candidate_sha)
    run_id = dispatch(api, repository, CAPACITY_WORKFLOW, ref, {
        'candidate_sha': candidate_sha, 'candidate_image': candidate_image,
        'lock_ref': lock_ref, 'publish': 'true'})
    verify_run(api('GET', f'repos/{repository}/actions/runs/{run_id}'), run_id=run_id,
               workflow=CAPACITY_WORKFLOW, head_sha=candidate_sha)
    return run_id


def dispatch_dr(api, repository: str, candidate_ref: str, reviewed_sha: str) -> int:
    _repository(repository)
    if not SHA.fullmatch(candidate_ref) or not SHA.fullmatch(reviewed_sha):
        raise ValueError('DR candidate ref and reviewed commit must be full shas')
    run_id = dispatch(api, repository, DR_WORKFLOW, 'refs/heads/trunk', {'candidate_ref': candidate_ref})
    run = verify_run(api('GET', f'repos/{repository}/actions/runs/{run_id}'), run_id=run_id,
                     workflow=DR_WORKFLOW, head_branch='trunk')
    head = str(run.get('head_sha') or '')
    if not SHA.fullmatch(head):
        raise LookupError(f'DR run {run_id} has no head commit')
    # Trunk may have moved since the nightly started; it must not have been rewritten past it.
    compare = api('GET', f'repos/{repository}/compare/{reviewed_sha}...{head}')
    if (compare or {}).get('status') not in ('identical', 'ahead'):
        raise LookupError(f'DR run {run_id} head {head} does not descend from reviewed {reviewed_sha}')
    return run_id


def wait_for_run(api, repository: str, run_id: int, *, timeout: int, poll: int = 60,
                 sleep=time.sleep, clock=time.monotonic) -> dict:
    deadline = clock() + timeout
    while True:
        run = api('GET', f'repos/{repository}/actions/runs/{run_id}')
        if not isinstance(run, dict) or run.get('id') != run_id:
            raise LookupError(f'run {run_id} read back as a different object')
        if run.get('status') == 'completed':
            return run
        if clock() >= deadline:
            raise TimeoutError(f'run {run_id} still {run.get("status")} after {timeout}s')
        sleep(poll)


def protocol_candidate(manifest: dict) -> dict[str, str]:
    """The exact candidate every protocol producer certifies, read from the resolved manifest."""
    server = ((manifest or {}).get('components') or {}).get('honua-server') or {}
    certification = (manifest or {}).get('protocolCertification') or {}
    candidate = {'server_image': str(server.get('image') or ''), 'server_sha': str(server.get('sha') or ''),
                 'image_digest': str(server.get('digest') or ''), 'cut_at': str(certification.get('candidateCutAt') or '')}
    if not SERVER_IMAGE.fullmatch(candidate['server_image']) or not SHA.fullmatch(candidate['server_sha']) \
            or not DIGEST.fullmatch(candidate['image_digest']) or not CUT.fullmatch(candidate['cut_at']):
        raise ValueError(f'resolved candidate is not an immutable image, sha, digest and cut: {candidate}')
    if not candidate['server_image'].endswith('@' + candidate['image_digest']):
        raise ValueError('candidate image does not reference its own digest')
    return candidate


def protocol_inputs(template: dict, candidate: dict[str, str], dispatch_id: str) -> dict[str, str]:
    values = {'{server_image}': candidate['server_image'], '{server_sha}': candidate['server_sha'],
              '{cut_at}': candidate['cut_at']}
    if DISPATCH_ID_INPUT in template:
        raise ValueError(f'producer inputs may not set {DISPATCH_ID_INPUT}; the nightly supplies it')
    rendered = {DISPATCH_ID_INPUT: dispatch_id}
    for key, value in template.items():
        if not isinstance(value, str) or ('{' in value and value not in CANDIDATE_PLACEHOLDERS):
            raise ValueError(f'producer input {key} is not a literal or a governed candidate placeholder')
        rendered[key] = values.get(value, value)
    if not set(CANDIDATE_PLACEHOLDERS) <= set(template.values()):
        raise ValueError('producer inputs do not carry the candidate image, sha and cut')
    return rendered


def protocol_dispatch_ref(api, repository: str, pin: str) -> str:
    """trunk when its head is the pin (the run is then a trunk run at the pin), else a tag at the pin."""
    head = api('GET', f'repos/{repository}/commits/trunk')
    if isinstance(head, dict) and head.get('sha') == pin:
        return 'refs/heads/trunk'
    return ensure_candidate_tag(api, repository, pin)


def dispatch_protocol(api, catalog: dict, manifest: dict, dispatch_id: str) -> list[dict]:
    """Dispatch every producer the catalog names at its pin, bound to the resolved candidate.

    Refuses before the first dispatch when any producer is unpinned, so a night never runs a
    partial producer set; a dispatch failure part-way refuses the night as well. Each run row
    keeps `dispatch_id`, the correlation id every producer receives as an input and echoes in
    its receipts; the bind refuses a receipt from any run not recorded here.
    """
    if not isinstance(dispatch_id, str) or not DISPATCH_ID.fullmatch(dispatch_id):
        raise ValueError(f'nightly dispatch id {dispatch_id!r} is not a correlation id')
    candidate = protocol_candidate(manifest)
    production = (catalog or {}).get('production') or {}
    producers = production.get('producers')
    revisions = (catalog or {}).get('source_revisions') or {}
    if not isinstance(producers, list) or not producers:
        raise ValueError('requirements catalog names no protocol certification producers')
    plan = []
    # A producer whose lanes the staged denominator no longer has (cells 0) has nothing to certify.
    for producer in (producer for producer in producers if producer.get('cells') != 0):
        repository = _repository(str(producer.get('repository') or ''))
        source = revisions.get(producer.get('source_revision_key')) or {}
        pin = str(source.get('commit') or '')
        if source.get('repository') != repository or not SHA.fullmatch(pin):
            raise ValueError(f"producer {producer.get('producer')} has no pinned revision in {repository}")
        if producer.get('source_revision_key') in ('server', 'server-certification') and pin != candidate['server_sha']:
            raise ValueError(f"producer {producer.get('producer')} is pinned to {pin}, not the candidate server")
        plan.append((producer, repository, pin, protocol_inputs(producer.get('inputs') or {}, candidate, dispatch_id)))
    runs = []
    for producer, repository, pin, inputs in plan:
        workflow = str(producer.get('workflow') or '')
        ref = protocol_dispatch_ref(api, repository, pin)
        run_id = dispatch(api, repository, workflow, ref, inputs)
        verify_run(api('GET', f'repos/{repository}/actions/runs/{run_id}'), run_id=run_id,
                   workflow=workflow, head_sha=pin)
        runs.append({'producer': producer['producer'], 'repository': repository, 'workflow': workflow,
                     'ref': ref, 'head_sha': pin, 'run_id': run_id, 'dispatch_id': dispatch_id,
                     'client_lanes': list(producer.get('client_lanes') or []),
                     'deployment_targets': list(producer.get('deployment_targets') or [])})
    return runs


def wait_protocol(api, runs: list[dict], *, timeout: int, sleep=time.sleep, clock=time.monotonic) -> list[dict]:
    """Wait for every producer under one deadline; any producer that did not succeed refuses."""
    if not isinstance(runs, list) or not runs:
        raise ValueError('no protocol producer runs to wait for')
    deadline = clock() + timeout
    finished, refused = [], []
    for row in runs:
        repository, run_id = _repository(row['repository']), row['run_id']
        run = wait_for_run(api, repository, run_id, timeout=max(0, int(deadline - clock())),
                           sleep=sleep, clock=clock)
        verify_run(run, run_id=run_id, workflow=row['workflow'], head_sha=row['head_sha'])
        finished.append({**row, 'conclusion': run.get('conclusion'), 'run_attempt': run.get('run_attempt')})
        if run.get('conclusion') != 'success':
            refused.append(f"{row['producer']} {repository} run {run_id} concluded {run.get('conclusion')!r}")
    if refused:
        raise LookupError('protocol producers did not succeed; the ledger stays unbound: ' + '; '.join(refused))
    return finished


def dispatch_aggregate(api, repository: str, requirements_revision: str, manifest: dict) -> int:
    """honua-evidence joins the producers' runs at the staged requirements commit for this candidate."""
    _repository(repository)
    if not SHA.fullmatch(requirements_revision):
        raise ValueError('requirements revision must be a full commit sha')
    candidate = protocol_candidate(manifest)
    run_id = dispatch(api, repository, AGGREGATE_WORKFLOW, 'refs/heads/trunk', {
        'requirements_revision': requirements_revision, 'candidate_source_sha': candidate['server_sha'],
        'candidate_image_digest': candidate['image_digest'], 'candidate_cut_at': candidate['cut_at']})
    verify_run(api('GET', f'repos/{repository}/actions/runs/{run_id}'), run_id=run_id,
               workflow=AGGREGATE_WORKFLOW, head_branch='trunk')
    return run_id


def wait_for_job(api, repository: str, run_id: int, job: str, *, timeout: int, poll: int = 60,
                 sleep=time.sleep, clock=time.monotonic) -> dict:
    """Wait for one named job of a run; a later job parked on an environment does not hold it."""
    deadline = clock() + timeout
    while True:
        listing = api('GET', f'repos/{repository}/actions/runs/{run_id}/jobs?per_page=100')
        jobs = [row for row in (listing or {}).get('jobs') or [] if isinstance(row, dict) and row.get('name') == job]
        if len(jobs) > 1:
            raise LookupError(f'run {run_id} has {len(jobs)} jobs named {job}')
        if jobs and jobs[0].get('run_id') != run_id:
            raise LookupError(f'job {job} read back for a different run')
        if jobs and jobs[0].get('status') == 'completed':
            return jobs[0]
        if clock() >= deadline:
            raise TimeoutError(f'run {run_id} job {job} not completed after {timeout}s')
        sleep(poll)


def capacity_receipt_url(repository: str, candidate_sha: str, run_id: str, run_attempt: str,
                         commits: list[dict]) -> str:
    """The immutable raw URL of the evidence ZIP honua-server's publisher committed for this run."""
    _repository(repository)
    if not SHA.fullmatch(candidate_sha) or not RUN.fullmatch(str(run_id)) or not RUN.fullmatch(str(run_attempt)):
        raise ValueError('capacity receipt coordinates must be a full sha and a numeric run id and attempt')
    # scripts/soak/publish_receipt.py --path/--message in capacity-soak-candidate.yml.
    message = f'capacity evidence for {candidate_sha} (run {run_id}/{run_attempt})'
    path = f'capacity/{candidate_sha}-{run_id}-{run_attempt}.zip'
    if not isinstance(commits, list):
        raise ValueError('soak-receipts history must be a list of commits')
    matches = []
    for commit in commits:
        if not isinstance(commit, dict):
            raise ValueError('soak-receipts history must be commit objects')
        title = str(((commit.get('commit') or {}).get('message') or '')).splitlines()
        if (title[0] if title else '') != message:
            continue
        sha = str(commit.get('sha') or '')
        if not SHA.fullmatch(sha):
            raise ValueError('soak-receipts commit is not a full sha')
        matches.append(sha)
    if len(matches) != 1:
        raise LookupError(f'expected one published capacity receipt for run {run_id}/{run_attempt}, found {len(matches)}')
    return f'https://raw.githubusercontent.com/{repository}/{matches[0]}/{path}'


def capacity_url(api, repository: str, candidate_sha: str, run_id: int) -> str:
    run = api('GET', f'repos/{repository}/actions/runs/{run_id}')
    verify_run(run, run_id=run_id, workflow=CAPACITY_WORKFLOW, head_sha=candidate_sha)
    attempt = run.get('run_attempt')
    path = f'capacity/{candidate_sha}-{run_id}-{attempt}.zip'
    commits = api('GET', f'repos/{repository}/commits?sha=soak-receipts&path={path}&per_page=100')
    return capacity_receipt_url(repository, candidate_sha, str(run_id), str(attempt), commits)


def dr_receipt_url(document: dict) -> str:
    url = document.get('receipt_url') if isinstance(document, dict) else None
    if not isinstance(url, str) or not url.startswith('https://') or url.startswith('https://github.com/'):
        raise ValueError('DR receipt location must be an immutable https raw URL')
    return url


def main(argv=None, api=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    capacity = commands.add_parser('dispatch-capacity')
    capacity.add_argument('--repository', required=True)
    capacity.add_argument('--candidate-sha', required=True)
    capacity.add_argument('--candidate-image', required=True)
    capacity.add_argument('--lock-ref', required=True)
    drill = commands.add_parser('dispatch-dr')
    drill.add_argument('--repository', required=True)
    drill.add_argument('--candidate-ref', required=True)
    drill.add_argument('--reviewed-sha', required=True)
    wait = commands.add_parser('wait')
    wait.add_argument('--repository', required=True)
    wait.add_argument('--run-id', required=True, type=int)
    wait.add_argument('--timeout', required=True, type=int)
    url = commands.add_parser('capacity-url')
    url.add_argument('--repository', required=True)
    url.add_argument('--candidate-sha', required=True)
    url.add_argument('--run-id', required=True, type=int)
    location = commands.add_parser('dr-url')
    location.add_argument('--location', type=Path, required=True)
    protocol = commands.add_parser('dispatch-protocol')
    protocol.add_argument('--catalog', type=Path, required=True)
    protocol.add_argument('--manifest', type=Path, required=True)
    protocol.add_argument('--out', type=Path, required=True)
    protocol.add_argument('--dispatch-id', required=True)
    protocol_wait = commands.add_parser('wait-protocol')
    protocol_wait.add_argument('--runs', type=Path, required=True)
    protocol_wait.add_argument('--timeout', required=True, type=int)
    job = commands.add_parser('wait-job')
    job.add_argument('--repository', required=True)
    job.add_argument('--run-id', required=True, type=int)
    job.add_argument('--job', required=True)
    job.add_argument('--timeout', required=True, type=int)
    aggregate = commands.add_parser('dispatch-aggregate')
    aggregate.add_argument('--repository', required=True)
    aggregate.add_argument('--requirements-revision', required=True)
    aggregate.add_argument('--manifest', type=Path, required=True)
    args = parser.parse_args(argv)
    api = api or Gh()
    try:
        if args.command == 'dispatch-capacity':
            print(dispatch_capacity(api, args.repository, args.candidate_sha, args.candidate_image, args.lock_ref))
        elif args.command == 'dispatch-dr':
            print(dispatch_dr(api, args.repository, args.candidate_ref, args.reviewed_sha))
        elif args.command == 'wait':
            run = wait_for_run(api, _repository(args.repository), args.run_id, timeout=args.timeout)
            print(run.get('conclusion'))
            return 0 if run.get('conclusion') == 'success' else 3
        elif args.command == 'capacity-url':
            print(capacity_url(api, _repository(args.repository), args.candidate_sha, args.run_id))
        elif args.command == 'dispatch-protocol':
            import yaml  # only the protocol-ledger job installs PyYAML
            runs = dispatch_protocol(api, json.loads(args.catalog.read_text()),
                                     yaml.safe_load(args.manifest.read_text()), args.dispatch_id)
            args.out.write_text(json.dumps(runs, indent=2) + '\n')
            for row in runs:
                print(f"{row['producer']}: https://github.com/{row['repository']}/actions/runs/{row['run_id']}")
        elif args.command == 'wait-protocol':
            runs = wait_protocol(api, json.loads(args.runs.read_text()), timeout=args.timeout)
            args.runs.write_text(json.dumps(runs, indent=2) + '\n')
            print(f'{len(runs)} protocol producers succeeded')
        elif args.command == 'wait-job':
            finished = wait_for_job(api, _repository(args.repository), args.run_id, args.job, timeout=args.timeout)
            print(finished.get('conclusion'))
            if finished.get('conclusion') != 'success':
                raise LookupError(f"run {args.run_id} job {args.job} concluded {finished.get('conclusion')!r}")
        elif args.command == 'dispatch-aggregate':
            import yaml  # only the protocol-ledger job installs PyYAML
            print(dispatch_aggregate(api, args.repository, args.requirements_revision,
                                     yaml.safe_load(args.manifest.read_text())))
        else:
            print(dr_receipt_url(json.loads(args.location.read_text())))
        return 0
    except (OSError, ValueError, LookupError, RuntimeError, TimeoutError) as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
