#!/usr/bin/env python3
"""Check or create the source-defined foundation approval environment in CI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from bind_github import api
from deploy import ROOT, require


def validate_policy(policy):
    require(policy['schema'] == 'honua.hosted-pilot-foundation-environment/v1', 'unknown environment policy')
    require(policy['repository'] == 'honua-io/honua-iac' and policy['environment'] == 'terraform-live-approval'
            and policy['branch'] == 'trunk', 'foundation environment target differs')
    require(isinstance(policy['prevent_self_review'], bool), 'invalid review policy')
    reviewers = policy['reviewers']
    require(bool(reviewers) and all(row['type'] == 'User' and isinstance(row['id'], int)
                                   and row['id'] > 0 and row['login'] for row in reviewers), 'human reviewers required')
    require(len({row['id'] for row in reviewers}) == len(reviewers), 'duplicate reviewers')


def verify_detail(policy, detail):
    require(detail.get('name') == policy['environment'], 'environment identity differs')
    require(detail.get('deployment_branch_policy') == {'protected_branches': False, 'custom_branch_policies': True}, 'explicit trunk branch policy required')
    rules = [row for row in detail.get('protection_rules', []) if row.get('type') == 'required_reviewers']
    require(len(rules) == 1 and rules[0].get('prevent_self_review', False) == policy['prevent_self_review'], 'reviewer protection differs')
    actual = {(row['type'], row['reviewer']['id'], row['reviewer']['login']) for row in rules[0].get('reviewers', [])}
    expected = {(row['type'], row['id'], row['login']) for row in policy['reviewers']}
    require(actual == expected, 'reviewer identity policy differs; review configuration before changing it')


def reconcile(policy, *, create=False):
    validate_policy(policy)
    base = 'repos/' + policy['repository']
    endpoint = base + '/environments/' + policy['environment']
    environments = []
    page = 1
    while True:
        rows = api('GET', base + f'/environments?per_page=100&page={page}')['environments']
        environments.extend(rows)
        if len(rows) < 100:
            break
        page += 1
    present = any(row['name'] == policy['environment'] for row in environments)
    require(present or create, 'foundation approval environment is absent; run the checked-in environment workflow first')
    if not present:
        for row in policy['reviewers']:
            user = api('GET', 'users/' + row['login'])
            require(user['id'] == row['id'] and user['type'] == 'User', 'reviewer account identity differs')
        api('PUT', endpoint, {
            'reviewers': [{'type': row['type'], 'id': row['id']} for row in policy['reviewers']],
            'prevent_self_review': policy['prevent_self_review'],
            'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True},
        })
    verify_detail(policy, api('GET', endpoint))
    branches = api('GET', endpoint + '/deployment-branch-policies?per_page=100')['branch_policies']
    if not branches and create:
        api('POST', endpoint + '/deployment-branch-policies', {'name': policy['branch'], 'type': 'branch'})
        branches = api('GET', endpoint + '/deployment-branch-policies?per_page=100')['branch_policies']
    require(len(branches) == 1 and branches[0]['name'] == policy['branch'] and branches[0]['type'] == 'branch', 'only trunk deployments may use foundation credentials')
    verify_detail(policy, api('GET', endpoint))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['check', 'create'])
    args = parser.parse_args()
    if args.mode == 'create':
        require(os.environ.get('GITHUB_REPOSITORY') == 'honua-io/honua-release'
                and os.environ.get('GITHUB_REF') == 'refs/heads/trunk', 'environment setup only runs from release trunk')
    policy = json.loads((ROOT / 'certification/hosted-pilot/foundation-environment.json').read_text())
    reconcile(policy, create=args.mode == 'create')
    print('Foundation environment matches its reviewed human-approval and trunk policy.')


if __name__ == '__main__':
    main()
