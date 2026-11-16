#!/usr/bin/env python3
"""Verify public finding dispositions against merged public pull requests."""
import argparse
import json
from pathlib import Path
import re
import sys

from github_api import gh

CONTRACT = Path(__file__).resolve().parents[1] / 'contracts/security-findings.json'


def validate_findings(rows, expected):
    if not isinstance(rows, list) or not rows:
        raise ValueError('security_findings: missing; every GA-blocking finding needs a public row')
    if type(expected) is not int or expected != len(rows):
        raise ValueError(f'security_findings: {len(rows)} rows, contract counts {expected} GA blockers')
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('security_findings: each row must be an object')
        sid = row.get('id')
        if not isinstance(sid, str) or not re.fullmatch(r'SEC-[1-9][0-9]*', sid) or sid in seen:
            raise ValueError('security_findings: invalid or duplicate id')
        seen.add(sid)
        repo = row.get('repo')
        if not isinstance(repo, str) or not re.fullmatch(r'honua-io/[a-z0-9-]+', repo):
            raise ValueError(f'{sid}: invalid repo')
        expected_keys = {'id', 'repo', 'status'}
        if row.get('status') == 'fixed':
            expected_keys.add('fixedBy')
            number = row.get('fixedBy')
            if type(number) not in (str, int) or not re.fullmatch(r'[1-9][0-9]*', str(number)):
                raise ValueError(f'{sid}: a fixed row needs fixedBy: the number of the merged PR')
        elif row.get('status') != 'open':
            raise ValueError(f'{sid}: status must be open or fixed')
        if set(row) != expected_keys:
            raise ValueError(f'{sid}: unexpected or missing fields')
    return rows


def load_contract(path):
    data = json.loads(path.read_text(encoding='utf-8'))
    keys = {'schema_version', 'release_line', 'expected_count', 'findings'}
    if (not isinstance(data, dict) or set(data) != keys
            or type(data['schema_version']) is not int or data['schema_version'] != 1
            or data['release_line'] != '2026.1'):
        raise ValueError('Invalid security findings contract')
    return validate_findings(data['findings'], data['expected_count'])


def verify_fixed_findings(rows, fetch):
    errors = []
    for row in rows:
        if row['status'] != 'fixed':
            continue
        pr = fetch('api', f"repos/{row['repo']}/pulls/{row['fixedBy']}")
        base = pr.get('base') or {}
        default = (base.get('repo') or {}).get('default_branch')
        text = (pr.get('title') or '') + '\n' + (pr.get('body') or '')
        if not pr.get('merged_at'):
            errors.append(f"{row['id']}: {row['repo']}#{row['fixedBy']} is not merged")
        elif not default or base.get('ref') != default:
            errors.append(f"{row['id']}: {row['repo']}#{row['fixedBy']} not the default branch")
        if not re.search(r'\b' + re.escape(row['id']) + r'\b', text):
            errors.append(f"{row['id']}: {row['repo']}#{row['fixedBy']} does not cite {row['id']}")
    return errors


def verify_contract(path, fetch=gh):
    rows = load_contract(path)
    errors = verify_fixed_findings(rows, fetch)
    if any(row['status'] == 'open' for row in rows):
        errors.append('GA-blocking security findings remain open')
    if errors:
        raise ValueError('Security findings gate failed:\n' + '\n'.join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contract', type=Path, default=CONTRACT)
    args = parser.parse_args()
    try:
        verify_contract(args.contract)
    except (ValueError, OSError, TypeError, KeyError) as error:
        print(f'Security findings gate failed: {error}', file=sys.stderr)
        return 1
    print('Security findings: PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
