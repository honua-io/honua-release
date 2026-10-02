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
        else:
            print(dr_receipt_url(json.loads(args.location.read_text())))
        return 0
    except (OSError, ValueError, LookupError, RuntimeError, TimeoutError) as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
