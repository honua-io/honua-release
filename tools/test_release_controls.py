"""Independent GitHub API fixture and mutation tests for the release controls."""
import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from release_controls import audit, audit_repository, branch_rules, rule_drift, tag_rules


POLICY = {'release_refs': ['refs/heads/release/2026.1'],
          'required_checks': ['validate'], 'tag_refs': [],
          'code_owners': ['mikemcdougall', 'independent-reviewer']}
# Written from the contract/API shape, not captured from the renderer.
LIVE = {
    'target': 'branch', 'enforcement': 'active', 'bypass_actors': [],
    'conditions': {'ref_name': {'include': ['refs/heads/release/2026.1'], 'exclude': []}},
    'rules': [
        {'type': 'deletion'}, {'type': 'non_fast_forward'},
        {'type': 'pull_request', 'parameters': {
            'required_approving_review_count': 1, 'dismiss_stale_reviews_on_push': True,
            'require_code_owner_review': True, 'require_last_push_approval': True,
            'required_review_thread_resolution': True,
            'allowed_merge_methods': ['merge', 'squash', 'rebase'],
        }},
        {'type': 'required_status_checks', 'parameters': {
            'strict_required_status_checks_policy': True, 'do_not_enforce_on_create': False,
            'required_status_checks': [{'context': 'validate', 'integration_id': 15368}],
        }},
    ],
}


def snapshot():
    return {'source_sha': 'a' * 40, 'rulesets': [copy.deepcopy(LIVE)],
            'codeowners': {'content': '* @mikemcdougall @independent-reviewer\n', 'source_sha': 'a' * 40},
            'human_writers': ['mikemcdougall', 'independent-reviewer']}


def test_explicit_contract_values_and_qualifying_fixture():
    rendered = branch_rules(POLICY)
    assert {k: v for k, v in rendered.items() if k != 'name'} == LIVE
    assert audit_repository(POLICY, snapshot()) == []


@pytest.mark.parametrize('path,value', [
    (('enforcement',), 'evaluate'), (('target',), 'tag'),
    (('bypass_actors',), [{'actor_type': 'RepositoryRole', 'actor_id': 5, 'bypass_mode': 'always'}]),
    (('conditions', 'ref_name', 'exclude'), ['refs/heads/release/2026.1']),
    (('conditions', 'ref_name', 'include'), ['refs/heads/trunk']),
    (('rules', 2, 'parameters', 'required_approving_review_count'), 0),
    (('rules', 2, 'parameters', 'require_code_owner_review'), False),
    (('rules', 2, 'parameters', 'dismiss_stale_reviews_on_push'), False),
    (('rules', 2, 'parameters', 'require_last_push_approval'), False),
    (('rules', 2, 'parameters', 'required_review_thread_resolution'), False),
    (('rules', 3, 'parameters', 'strict_required_status_checks_policy'), False),
    (('rules', 3, 'parameters', 'do_not_enforce_on_create'), True),
    (('rules', 3, 'parameters', 'required_status_checks'), []),
    (('rules', 3, 'parameters', 'required_status_checks'), [{'context': 'validate'}]),
    (('rules', 3, 'parameters', 'required_status_checks'), [{'context': 'validate', 'integration_id': 42}]),
    (('rules', 3, 'parameters', 'required_status_checks'), [{'context': 'shadow validate', 'integration_id': 15368}]),
])
def test_each_bypass_or_missing_requirement_is_rejected(path, value):
    live = copy.deepcopy(LIVE)
    item = live
    for key in path[:-1]:
        item = item[key]
    item[path[-1]] = value
    assert rule_drift(branch_rules(POLICY), live)


@pytest.mark.parametrize('index', range(4))
def test_deleting_any_required_rule_fails(index):
    live = copy.deepcopy(LIVE)
    del live['rules'][index]
    assert rule_drift(branch_rules(POLICY), live)


def test_stronger_review_count_and_additional_check_are_accepted():
    live = copy.deepcopy(LIVE)
    live['rules'][2]['parameters']['required_approving_review_count'] = 2
    live['rules'][3]['parameters']['required_status_checks'].append(
        {'context': 'security', 'integration_id': 15368})
    assert not rule_drift(branch_rules(POLICY), live)


def test_bypassable_fragments_cannot_be_combined_into_a_passing_rule():
    data = snapshot()
    checks = copy.deepcopy(LIVE)
    checks['bypass_actors'] = [{'actor_type': 'RepositoryRole', 'actor_id': 5, 'bypass_mode': 'always'}]
    reviews = copy.deepcopy(LIVE)
    reviews['rules'].pop()
    data['rulesets'] = [checks, reviews]
    assert 'complete active release-line ruleset missing or drifted' in audit_repository(POLICY, data)


@pytest.mark.parametrize('change', ['missing', 'override', 'different-sha', 'sole-writer', 'unreadable'])
def test_ownership_and_read_failures_are_not_success(change):
    data = snapshot()
    if change == 'missing': data.pop('codeowners')
    if change == 'override': data['codeowners']['content'] += '.github/\n'
    if change == 'different-sha': data['codeowners']['source_sha'] = 'b' * 40
    if change == 'sole-writer': data['human_writers'] = ['mikemcdougall']
    if change == 'unreadable': data['rulesets'] = {'error': 'HTTP 403'}
    assert audit_repository(POLICY, data)


def test_immutable_tags_and_signed_commits_do_not_prove_signed_tags():
    policy = {**POLICY, 'tag_refs': ['refs/tags/honua-2026.1.*']}
    data = snapshot()
    data['rulesets'].append({
        'target': 'tag', 'enforcement': 'active', 'bypass_actors': [],
        'conditions': {'ref_name': {'include': ['refs/tags/honua-2026.1.*'], 'exclude': []}},
        'rules': [{'type': 'update'}, {'type': 'deletion'}, {'type': 'required_signatures'}],
    })
    errors = audit_repository(policy, data)
    assert errors == ['native signed-tag producer and trusted verification not qualified']
    assert [r['type'] for r in tag_rules(policy)['rules']] == ['update', 'deletion']


@pytest.mark.parametrize('checks', [[], ['validate', 'validate'], [' ']])
def test_empty_or_ambiguous_denominator_cannot_render(checks):
    with pytest.raises(ValueError): branch_rules({**POLICY, 'required_checks': checks})


def test_missing_and_unexpected_repositories_fail():
    assert audit({'repositories': {'a': POLICY}}, {'repositories': {}})['status'] == 'fail'
    result = audit({'repositories': {'a': POLICY}}, {'repositories': {'a': snapshot(), 'b': snapshot()}})
    assert result['status'] == 'fail'
    assert result['unexpected_repositories'] == ['b']


def test_cli_returns_nonzero_and_binds_failure_receipt(tmp_path):
    policy = tmp_path / 'policy.json'
    policy.write_text(json.dumps({'repositories': {'example': POLICY}}))
    observed = tmp_path / 'snapshot.json'
    observed.write_text('{"repositories": {}}')
    output = tmp_path / 'result.json'
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('release_controls.py')),
                             '--policy', str(policy), 'audit', str(observed), '--output', str(output)],
                            capture_output=True, text=True)
    assert result.returncode == 1
    receipt = json.loads(output.read_text())
    assert receipt['repositories'] == {'example': ['repository missing from snapshot']}
    import hashlib
    assert receipt['snapshot_sha256'] == hashlib.sha256(observed.read_bytes()).hexdigest()
    assert receipt['policy_sha256'] == hashlib.sha256(policy.read_bytes()).hexdigest()


def test_an_independent_writer_without_code_ownership_cannot_approve():
    data = snapshot()
    policy = {**POLICY, 'code_owners': ['mikemcdougall']}
    data['codeowners']['content'] = '* @mikemcdougall\n'
    assert 'no independent human code owner available to approve owner-authored changes' in audit_repository(policy, data)


def test_comments_in_codeowners_do_not_remove_coverage():
    data = snapshot()
    data['codeowners']['content'] = '# Ownership includes workflows\n\n' + data['codeowners']['content']
    assert not audit_repository(POLICY, data)


def test_committed_inventory_covers_the_adopted_denominator_and_manifest():
    import yaml
    root = Path(__file__).resolve().parents[1]
    policy = json.loads((root / 'certification/release-controls/policy.json').read_text())
    expected = set('honua-server honua-sdk-js honua-sdk-python honua-sdk-dotnet honua-studio honua-console honua-release honua-iac honua-devops honua-helm honua-site honua-demo-infra honua-evidence honua-esri-compat geospatial-grpc geospatial-mcp honua-support'.split())
    assert set(policy['repositories']) == expected
    manifest = yaml.safe_load((root / 'platform-manifest.yaml').read_text())
    assert set(manifest['components']) <= expected
    assert all(row['release_refs'] == ['refs/heads/release/*'] for row in policy['repositories'].values())


@pytest.mark.parametrize('error', ['error connecting to api.github.com', 'Could not resolve host',
                                  'Connection reset by peer', 'net/http: TLS handshake timeout',
                                  'gh: Forbidden (HTTP 403)'])
def test_network_backoff_retries_the_same_read_without_reauthentication(monkeypatch, error):
    import release_controls as controls
    calls, sleeps = [], []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, '', error)
    monkeypatch.setattr(controls.subprocess, 'run', run)
    monkeypatch.setattr(controls.time, 'sleep', sleeps.append)
    assert controls.github('repos/honua-io/example') == {'error': error}
    assert sleeps == [10, 30, 60, 120, 60]
    assert calls == [['gh', 'api', 'repos/honua-io/example']] * 6


def test_paginated_repository_controls_are_not_truncated(monkeypatch):
    import release_controls as controls
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, '[[{"id":1}],[{"id":2}]]', '')
    monkeypatch.setattr(controls.subprocess, 'run', run)
    assert controls.github('repos/example/rulesets', paginated=True) == [{'id': 1}, {'id': 2}]
    assert calls == [['gh', 'api', 'repos/example/rulesets', '--paginate', '--slurp']]
