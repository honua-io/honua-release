#!/usr/bin/env python3
"""Render release-line rules and reject drift; never mutate GitHub settings.

GitHub's required_signatures rule verifies commits, not annotated tag signatures.
Tag immutability is checked separately and must never be reported as signing proof.
"""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import quote

import tag_signing

POLICY = Path(__file__).resolve().parents[1] / 'certification/release-controls/policy.json'


def github(path: str, *, paginated: bool = False):
    """Read only, bounded backoff, including rate-limit/403 cooling; never authenticate."""
    command = ['gh', 'api', path]
    if paginated:
        command += ['--paginate', '--slurp']
    for delay in (0, 10, 30, 60, 120, 60):
        if delay:
            time.sleep(delay)
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode == 0:
            data = json.loads(result.stdout)
            return [item for page in data for item in page] if paginated else data
        error = result.stderr.strip()
        if not any(s in error.lower() for s in (
            'error connecting', 'could not resolve host', 'connection reset',
            'timeout', 'http 403', 'status code: 499',
        )):
            return {'error': error}
    return {'error': error}


def capture_repository(repo: str) -> dict:
    base = f'repos/honua-io/{repo}'
    metadata = github(base)
    if 'error' in metadata:
        return metadata
    branch = metadata['default_branch']
    tip = github(f'{base}/branches/{quote(branch, safe="")}')
    if 'error' in tip:
        return tip
    sha = tip['commit']['sha']
    listed = github(f'{base}/rulesets?includes_parents=true&per_page=100', paginated=True)
    rulesets = [github(f'{base}/rulesets/{r["id"]}') for r in listed] if isinstance(listed, list) else listed
    collaborators = github(f'{base}/collaborators?per_page=100', paginated=True)
    reads = {}
    owners = None
    for path in ('.github/CODEOWNERS', 'CODEOWNERS', 'docs/CODEOWNERS'):
        response = github(f'{base}/contents/{path}?ref={sha}')
        reads[path] = {'error': response['error']} if 'error' in response else {'sha': response['sha']}
        if owners is None and 'content' in response:
            owners = {'path': path, 'content': base64.b64decode(response['content']).decode(),
                      'source_sha': sha}
    return {
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'source_sha': sha, 'default_branch': branch,
        'protection': github(f'{base}/branches/{quote(branch, safe="")}/protection'),
        'rulesets': rulesets, 'codeowners': owners, 'codeowners_reads': reads,
        'human_writers': [x['login'] for x in collaborators if x['type'] == 'User'
                          and x.get('permissions', {}).get('push')] if isinstance(collaborators, list) else [],
        'collaborator_read_error': collaborators.get('error') if isinstance(collaborators, dict) else None,
        'api_source': f'https://api.github.com/{base}',
    }


def branch_rules(policy: dict) -> dict:
    checks = policy['required_checks']
    if not checks or len(checks) != len(set(checks)) or any(not c.strip() for c in checks):
        raise ValueError('required_checks must be a nonempty, unique denominator')
    refs = policy['release_refs']
    if not refs or any(not r.startswith('refs/heads/') for r in refs):
        raise ValueError('release_refs must name full branch refs')
    return {
        'name': '2026.1 release-line controls',
        'target': 'branch', 'enforcement': 'active', 'bypass_actors': [],
        'conditions': {'ref_name': {'include': refs, 'exclude': []}},
        'rules': [
            {'type': 'deletion'}, {'type': 'non_fast_forward'},
            {'type': 'pull_request', 'parameters': {
                'required_approving_review_count': 1,
                'dismiss_stale_reviews_on_push': True,
                'require_code_owner_review': True,
                'require_last_push_approval': True,
                'required_review_thread_resolution': True,
                'allowed_merge_methods': ['merge', 'squash', 'rebase'],
            }},
            {'type': 'required_status_checks', 'parameters': {
                'strict_required_status_checks_policy': True,
                'do_not_enforce_on_create': False,
                'required_status_checks': [
                    {'context': c, 'integration_id': 15368} for c in checks
                ],
            }},
        ],
    }


def tag_rules(policy: dict) -> dict:
    if not policy['tag_refs']:
        raise ValueError('no native publication tags declared')
    return {
        'name': '2026.1 immutable publication tags',
        'target': 'tag', 'enforcement': 'active', 'bypass_actors': [],
        'conditions': {'ref_name': {'include': policy['tag_refs'], 'exclude': []}},
        'rules': [{'type': 'update'}, {'type': 'deletion'}],
    }


def rule_drift(expected: dict, actual: dict) -> list[str]:
    """Require one complete no-bypass ruleset; never combine bypassable fragments."""
    errors = []
    for field in ('target', 'enforcement', 'conditions', 'bypass_actors'):
        if actual.get(field) != expected[field]:
            errors.append(f'{field} differs')
    rows = actual.get('rules')
    if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
        return errors + ['rules missing or malformed']
    types = [r.get('type') for r in rows]
    if len(types) != len(set(types)):
        errors.append('duplicate rule type')
    rules = {r.get('type'): r for r in rows}
    for required in expected['rules']:
        kind = required['type']
        found = rules.get(kind)
        if found is None:
            errors.append(f'{kind} missing')
            continue
        for key, value in required.get('parameters', {}).items():
            live = found.get('parameters', {}).get(key)
            if key == 'required_status_checks':
                if not isinstance(live, list) or any(c not in live for c in value):
                    errors.append('required_status_checks denominator/source differs')
            elif key == 'required_approving_review_count':
                if type(live) is not int or live < value:
                    errors.append('human approval missing')
            elif live != value:
                errors.append(f'{kind}.{key} differs')
    return errors


def audit_repository(policy: dict, snapshot: dict, receipt=None, repository: str = '') -> list[str]:
    errors = []
    try:
        expected = branch_rules(policy)
    except ValueError as exc:
        errors.append(str(exc))
        expected = None
    rulesets = snapshot.get('rulesets')
    if not isinstance(rulesets, list):
        return ['rulesets unreadable']
    if expected and not any(isinstance(r, dict) and not rule_drift(expected, r) for r in rulesets):
        errors.append('complete active release-line ruleset missing or drifted')
    # The normalized snapshot binds CODEOWNERS to the exact inspected source SHA.
    owners = snapshot.get('codeowners')
    content = owners.get('content', '') if isinstance(owners, dict) else ''
    entries = [line.strip() for line in content.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    designated = policy['code_owners']
    if entries != ['* ' + ' '.join('@' + owner for owner in designated)]:
        errors.append('catch-all human CODEOWNERS not verified')
    elif owners.get('source_sha') != snapshot.get('source_sha') or not re.fullmatch('[0-9a-f]{40}', owners.get('source_sha', '')):
        errors.append('CODEOWNERS source SHA differs')
    # A designated owner must be able to approve a change authored by the release owner.
    humans = snapshot.get('human_writers', [])
    if not any(h != 'mikemcdougall' and h in designated for h in humans):
        errors.append('no independent human code owner available to approve owner-authored changes')
    if policy['tag_refs']:
        tags = tag_rules(policy)
        if not any(isinstance(r, dict) and not rule_drift(tags, r) for r in rulesets):
            errors.append('immutable native publication tag ruleset missing or drifted')
        # A receipt boolean or a GitHub required_signatures rule is never signing evidence.
        # Only a tag_signing receipt bound to the committed trust policy resolves this, and the
        # policy nominates no signer today, so this stays red until the owner nominates one.
        reason = (tag_signing.qualify_receipt(repository, policy['tag_refs'], receipt)
                  if receipt is not None else None)
        if receipt is None or reason:
            errors.append('native signed-tag producer and trusted verification not qualified'
                          + (f': {reason}' if reason else ''))
    return errors


def audit(policy: dict, snapshot: dict, receipts: dict | None = None) -> dict:
    expected = policy['repositories']
    if not isinstance(snapshot.get('repositories'), dict):
        raise ValueError('snapshot repositories missing')
    observed = snapshot['repositories']
    results = {repo: audit_repository(row, observed[repo], (receipts or {}).get(repo), repo)
               if repo in observed else ['repository missing from snapshot']
               for repo, row in expected.items()}
    extras = sorted(set(observed) - set(expected))
    return {'schema_version': 1, 'issue': 'honua-io/honua-release#236',
            'status': 'fail' if extras or any(results.values()) else 'pass',
            'unexpected_repositories': extras, 'repositories': results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, default=POLICY)
    subs = parser.add_subparsers(dest='command', required=True)
    render = subs.add_parser('render')
    render.add_argument('repository')
    render.add_argument('--tags', action='store_true')
    check = subs.add_parser('audit')
    check.add_argument('snapshot', type=Path)
    check.add_argument('--output', type=Path, required=True)
    check.add_argument('--signing-receipts', type=Path,
                       help='signed publication-tag receipts by repository; absent means unqualified')
    capture = subs.add_parser('capture')
    capture.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    policy = json.loads(args.policy.read_text())
    if args.command == 'capture':
        repos = list(policy['repositories'])
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(capture_repository, repos))
        args.output.write_text(json.dumps({'schema_version': 1, 'repositories': dict(zip(repos, rows))}, indent=2) + '\n', encoding='utf-8', newline='\n')
        return 0
    if args.command == 'render':
        row = policy['repositories'][args.repository]
        print(json.dumps(tag_rules(row) if args.tags else branch_rules(row), indent=2))
        return 0
    raw = args.snapshot.read_bytes()
    receipts = json.loads(args.signing_receipts.read_text()) if args.signing_receipts else None
    result = audit(policy, json.loads(raw), receipts)
    result['snapshot_sha256'] = hashlib.sha256(raw).hexdigest()
    result['policy_sha256'] = hashlib.sha256(args.policy.read_bytes()).hexdigest()
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8', newline='\n')
    print(f"release controls: {result['status']} ({len(result['repositories'])} repositories)")
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
