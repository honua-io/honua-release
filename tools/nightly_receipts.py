#!/usr/bin/env python3
"""Locate producer receipts and the dispatch run that published them.

The nightly train does not accept a hand-supplied URL. Capacity receipts are the
commit-pinned raw file honua-server's soak publisher writes; DR receipts are the
JSON artifact this repository's drill uploads. Neither lookup follows a branch tip.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

SHA = re.compile(r'[0-9a-f]{40}\Z')
RUN = re.compile(r'[1-9][0-9]*\Z')


def new_run_id(before: set[int], runs: list[dict], *, head_sha: str, event: str = 'workflow_dispatch') -> int | None:
    """Return the newest dispatch created after `before`, or None while it is still absent."""
    if not SHA.fullmatch(head_sha):
        raise ValueError('head sha must be a full commit')
    found = []
    for run in runs:
        if not isinstance(run, dict):
            raise ValueError('workflow run must be an object')
        if run.get('event') != event or run.get('headSha') != head_sha:
            continue
        identifier = run.get('databaseId')
        if not isinstance(identifier, int) or identifier in before:
            continue
        found.append(identifier)
    return max(found) if found else None


def capacity_receipt_url(repository: str, candidate_sha: str, run_id: str, commits: list[dict]) -> str:
    """The immutable raw URL of the soak receipt committed for this exact run."""
    if repository.count('/') != 1 or any(not part for part in repository.split('/')):
        raise ValueError('capacity repository must be owner/name')
    if not SHA.fullmatch(candidate_sha) or not RUN.fullmatch(str(run_id)):
        raise ValueError('capacity receipt coordinates must be a full sha and a numeric run id')
    message = f'capacity soak receipt for {candidate_sha} (run {run_id})'
    path = f'capacity/{candidate_sha}-{run_id}.json'
    matches = []
    for commit in commits:
        if not isinstance(commit, dict):
            raise ValueError('soak-receipts history must be commit objects')
        title = str(((commit.get('commit') or {}).get('message') or '')).splitlines()
        title = title[0] if title else ''
        if title != message:
            continue
        sha = str(commit.get('sha') or '')
        if not SHA.fullmatch(sha):
            raise ValueError('soak-receipts commit is not a full sha')
        matches.append(sha)
    if len(matches) != 1:
        raise LookupError(f'expected one published capacity receipt for run {run_id}, found {len(matches)}')
    return f'https://raw.githubusercontent.com/{repository}/{matches[0]}/{path}'


def dr_receipt_url(document: dict) -> str:
    url = document.get('receipt_url') if isinstance(document, dict) else None
    if not isinstance(url, str) or not url.startswith('https://') or url.startswith('https://github.com/'):
        raise ValueError('DR receipt location must be an immutable https raw URL')
    return url


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    new_run = commands.add_parser('new-run-id')
    new_run.add_argument('--before', type=Path, required=True)
    new_run.add_argument('--runs', type=Path, required=True)
    new_run.add_argument('--head-sha', required=True)
    capacity = commands.add_parser('capacity-url')
    capacity.add_argument('--repository', required=True)
    capacity.add_argument('--candidate-sha', required=True)
    capacity.add_argument('--run-id', required=True)
    capacity.add_argument('--commits', type=Path, required=True)
    drill = commands.add_parser('dr-url')
    drill.add_argument('--location', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'new-run-id':
            before = set(json.loads(args.before.read_text()))
            found = new_run_id(before, json.loads(args.runs.read_text()), head_sha=args.head_sha)
            if found is None:
                return 2
            print(found)
        elif args.command == 'capacity-url':
            print(capacity_receipt_url(args.repository, args.candidate_sha, args.run_id,
                                       json.loads(args.commits.read_text())))
        else:
            print(dr_receipt_url(json.loads(args.location.read_text())))
        return 0
    except (OSError, ValueError, LookupError, json.JSONDecodeError) as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
