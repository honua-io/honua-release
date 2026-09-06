#!/usr/bin/env python3
"""Render release-line rules and reject drift; never mutate GitHub settings.

GitHub's required_signatures rule verifies commits, not annotated tag signatures.
Tag immutability is checked separately and must never be reported as signing proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

POLICY = Path(__file__).resolve().parents[1] / 'certification/release-controls/policy.json'


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


def audit_repository(policy: dict, snapshot: dict) -> list[str]:
    errors = []
    try:
        expected = branch_rules(policy)
    except ValueError as exc:
        return [str(exc)]
    rulesets = snapshot.get('rulesets')
    if not isinstance(rulesets, list):
        return ['rulesets unreadable']
    if not any(isinstance(r, dict) and not rule_drift(expected, r) for r in rulesets):
        errors.append('complete active release-line ruleset missing or drifted')
    # The normalized snapshot binds CODEOWNERS to the exact inspected source SHA.
    owners = snapshot.get('codeowners')
    if not isinstance(owners, dict) or owners.get('content') != '* @mikemcdougall\n':
        errors.append('catch-all human CODEOWNERS not verified')
    elif owners.get('source_sha') != snapshot.get('source_sha') or not owners.get('source_sha'):
        errors.append('CODEOWNERS source SHA differs')
    # A designated owner must be able to approve a change authored by the release owner.
    humans = snapshot.get('human_writers', [])
    if not any(h != 'mikemcdougall' for h in humans):
        errors.append('no independent human writer available to approve owner-authored changes')
    if policy['tag_refs']:
        tags = tag_rules(policy)
        if not any(isinstance(r, dict) and not rule_drift(tags, r) for r in rulesets):
            errors.append('immutable native publication tag ruleset missing or drifted')
        # Deliberately unresolved until a reviewed signing producer and trust policy exist.
        # A receipt boolean or GitHub required_signatures rule is not signing evidence.
        errors.append('native signed-tag producer and trusted verification not qualified')
    return errors


def audit(policy: dict, snapshot: dict) -> dict:
    expected = policy['repositories']
    if not isinstance(snapshot.get('repositories'), dict):
        raise ValueError('snapshot repositories missing')
    observed = snapshot['repositories']
    results = {repo: audit_repository(row, observed[repo]) if repo in observed
               else ['repository missing from snapshot'] for repo, row in expected.items()}
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
    args = parser.parse_args()
    policy = json.loads(args.policy.read_text())
    if args.command == 'render':
        row = policy['repositories'][args.repository]
        print(json.dumps(tag_rules(row) if args.tags else branch_rules(row), indent=2))
        return 0
    raw = args.snapshot.read_bytes()
    result = audit(policy, json.loads(raw))
    result['snapshot_sha256'] = hashlib.sha256(raw).hexdigest()
    result['policy_sha256'] = hashlib.sha256(args.policy.read_bytes()).hexdigest()
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(f"release controls: {result['status']} ({len(result['repositories'])} repositories)")
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
