#!/usr/bin/env python3
"""Reconcile the checked-in GitHub IaC specification with AWS bindings."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from deploy import ROOT, digest, require


def api(method, endpoint, body=None):
    cmd = ['gh', 'api', '--method', method, endpoint]
    if body is not None:
        cmd += ['--input', '-']
    result = subprocess.run(cmd, input=None if body is None else json.dumps(body).encode(), capture_output=True)
    require(result.returncode == 0, 'GitHub binding API refused ' + method + ' ' + endpoint)
    return json.loads(result.stdout) if result.stdout else None


def verify_release_source(binding):
    """Consume an older reviewed root without freezing unrelated trunk work."""
    sha = os.environ['PILOT_BOOTSTRAP_ROOT_SHA']
    require(re.fullmatch(r'[a-f0-9]{40}', sha), 'immutable foundation release-root SHA required')
    require(binding['release_root_sha'] == sha, 'foundation release-root input mismatch')
    source = ROOT / '_pilot-bootstrap-source'
    checked = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'], capture_output=True)
    require(checked.returncode == 0 and checked.stdout.decode().strip() == sha, 'foundation source checkout mismatch')
    ancestor = subprocess.run(['git', '-C', str(source), 'merge-base', '--is-ancestor', sha, 'origin/trunk'], capture_output=True)
    require(ancestor.returncode == 0, 'foundation release-root source is not on trunk')
    branch = api('GET', 'repos/honua-io/honua-release/branches/trunk')
    require(branch['protected'], 'foundation release-root provenance requires protected trunk')
    contract = Path('certification/hosted-pilot/github-bindings.json')
    require(digest(source / contract) == digest(ROOT / contract), 'GitHub binding contract changed since foundation production; review and rerun foundation')


def main():
    e = os.environ
    require(e['GITHUB_REPOSITORY'] == 'honua-io/honua-release' and e['GITHUB_REF'] == 'refs/heads/trunk', 'GitHub binding only runs in release trunk')
    path = Path(e['RUNNER_TEMP']) / 'pilot-foundation-bindings/bindings.json'
    require(digest(path) == e['PILOT_BINDINGS_SHA256'], 'foundation bindings digest mismatch')
    binding = json.loads(path.read_text())
    require(binding['schema'] == 'honua.hosted-pilot-bootstrap-bindings/v1' and binding['foundation_run_id'] == e['PILOT_BOOTSTRAP_RUN_ID'], 'foundation binding identity mismatch')
    verify_release_source(binding)
    producer = api('GET', f"repos/honua-io/honua-iac/actions/runs/{e['PILOT_BOOTSTRAP_RUN_ID']}")
    workflow = api('GET', 'repos/honua-io/honua-iac/actions/workflows/hosted-pilot-bootstrap.yml')
    require(producer['workflow_id'] == workflow['id'] and producer['event'] == 'workflow_dispatch' and producer['head_branch'] == 'trunk' and producer['head_sha'] == binding['foundation_source_sha'] and producer['conclusion'] == 'success', 'untrusted foundation binding producer')
    artifact = api('GET', f"repos/honua-io/honua-iac/actions/artifacts/{e['PILOT_BINDINGS_ARTIFACT_ID']}")
    require(str(artifact['workflow_run']['id']) == e['PILOT_BOOTSTRAP_RUN_ID'] and artifact['workflow_run']['head_sha'] == binding['foundation_source_sha'] and not artifact['expired'], 'untrusted foundation binding artifact')
    spec = json.loads((ROOT / 'certification/hosted-pilot/github-bindings.json').read_text())
    require(spec['schema'] == 'honua.hosted-pilot-github-bindings/v1' and spec['repository'] == e['GITHUB_REPOSITORY'], 'unexpected GitHub desired state')
    base = 'repos/' + spec['repository']
    for environment in spec['environments']:
        endpoint = base + '/environments/' + environment
        current = api('GET', base + '/environments?per_page=100')['environments']
        existing = next((row for row in current if row['name'] == environment), None)
        # Preserve existing reviewer/wait policies, while narrowing deployment
        # branches. No independent reviewer requirement is created here.
        payload = {'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True}}
        if existing:
            detail = api('GET', endpoint)
            for rule in detail.get('protection_rules', []):
                if rule['type'] == 'required_reviewers':
                    payload['reviewers'] = [{'type': item['type'], 'id': item['reviewer']['id']} for item in rule['reviewers']]
                    payload['prevent_self_review'] = rule.get('prevent_self_review', False)
                elif rule['type'] == 'wait_timer':
                    payload['wait_timer'] = rule['wait_timer']
            payload['can_admins_bypass'] = detail.get('can_admins_bypass', True)
        api('PUT', endpoint, payload)
        policies = api('GET', endpoint + '/deployment-branch-policies?per_page=100')['branch_policies']
        require(all(row['name'] == spec['branch'] and row.get('type', 'branch') == 'branch' for row in policies), 'existing pilot environment permits other refs; review its desired state before reconciling')
        if not policies:
            api('POST', endpoint + '/deployment-branch-policies', {'name': spec['branch'], 'type': 'branch'})
    require(set(binding['deployment_bindings']) == set(spec['variables']), 'foundation variable allowlist mismatch')
    current_vars = {row['name'] for row in api('GET', base + '/actions/variables?per_page=100')['variables']}
    for name in spec['variables']:
        method = 'PATCH' if name in current_vars else 'POST'
        endpoint = base + '/actions/variables' + ('/' + name if method == 'PATCH' else '')
        api(method, endpoint, {'name': name, 'value': binding['deployment_bindings'][name]})
    print('Versioned pilot environment/trunk/OIDC bindings reconciled. Existing approval policy preserved.')


if __name__ == '__main__':
    main()
