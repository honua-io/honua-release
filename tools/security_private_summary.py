#!/usr/bin/env python3
"""Publish a candidate-bound aggregate without fetching private finding details."""
from __future__ import annotations

import datetime as dt
import json
from http.client import HTTPException
import os
from pathlib import Path
import re
import sys
import urllib.request

import rc_gate_window as gate

API = 'https://api.github.com/graphql'
RECEIPT = Path('security-open-high.json')
QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    isPrivate
    hasIssuesEnabled
    highLabel: label(name: "severity/high") { name }
    high: issues(states: OPEN, labels: ["severity/high"]) { totalCount }
  }
}
"""


class SummaryUnavailable(Exception):
    pass


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SummaryUnavailable()


def candidate_from_environment():
    candidate = gate.Candidate(
        gate.normalize_sha(os.environ.get('SERVER_SHA')),
        gate.normalize_digest(os.environ.get('SERVER_DIGEST')),
        gate.normalize_digest(os.environ.get('LOCK_DIGEST')),
    )
    if not all((candidate.server_sha, candidate.server_digest, candidate.platform_lock)):
        raise SummaryUnavailable()
    return candidate


def tracker_request():
    repository = os.environ.get('SECURITY_TRACKER_REPOSITORY', '')
    token = os.environ.get('SECURITY_TRACKER_TOKEN', '')
    if not token or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise SummaryUnavailable()
    owner, name = repository.split('/')
    payload = {'query': QUERY, 'variables': {'owner': owner, 'name': name}}
    return urllib.request.Request(API, data=json.dumps(payload).encode(), headers={
        'Authorization': 'Bearer ' + token,
        'Content-Type': 'application/json',
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'honua-release-security-summary',
    })


def open_high_count(document):
    if not isinstance(document, dict) or document.get('errors'):
        raise SummaryUnavailable()
    data = document.get('data')
    repository = data.get('repository') if isinstance(data, dict) else None
    if not isinstance(repository, dict):
        raise SummaryUnavailable()
    if repository.get('isPrivate') is not True or repository.get('hasIssuesEnabled') is not True:
        raise SummaryUnavailable()
    if repository.get('highLabel') != {'name': 'severity/high'}:
        raise SummaryUnavailable()
    high = repository.get('high')
    count = high.get('totalCount') if isinstance(high, dict) else None
    if type(count) is not int or count < 0:
        raise SummaryUnavailable()
    return count


def read_count(request):
    opener = urllib.request.build_opener(RejectRedirects())
    with opener.open(request, timeout=60) as response:
        data = response.read(4097)
    if len(data) > 4096:
        raise SummaryUnavailable()
    return open_high_count(json.loads(data))


def receipt(count, candidate):
    return {
        'schema': 'honua.release-promise-receipt/v1',
        'row': 'security-open-high',
        'status': 'PASS' if count == 0 else 'FAIL',
        'serverSha': candidate.server_sha,
        'imageDigest': candidate.server_digest,
        'platformLockDigest': candidate.platform_lock,
        'observedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
        'assertions': {'privateScoped': True},
        'openHighFindings': count,
    }


def main():
    RECEIPT.unlink(missing_ok=True)
    try:
        candidate = candidate_from_environment()
        count = read_count(tracker_request())
        RECEIPT.write_text(json.dumps(receipt(count, candidate), indent=2) + '\n')
    except (SummaryUnavailable, ValueError, TypeError, AttributeError, OSError, HTTPException):
        print('Private security summary unavailable.', file=sys.stderr)
        return 1
    return 0 if count == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
