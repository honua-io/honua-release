"""Source preparation must expose real CI refusals without changing release authority."""
import copy
import json
from pathlib import Path
import subprocess

import pytest

import prepare_mcp_source_context as context
import resolve_trunk_candidate as resolver

SOURCE = 'a' * 40
HEAD = 'b' * 40


class GitHub:
    def __init__(self):
        self.protected = True
        self.ancestor = True
        self.check_source = SOURCE
        self.reads = []
        self.snapshots = {}
        for path in context.FILES:
            self.snapshots[path] = (context.ROOT / 'certification/sources/geospatial-mcp' / Path(path).name).read_bytes()
        package = json.loads(self.snapshots[context.FILES[0]])
        lock = json.loads(self.snapshots[context.FILES[1]])
        for name, version in (('@modelcontextprotocol/sdk', '1.32.1'), ('@modelcontextprotocol/inspector', '2.10.1')):
            package['dependencies'][name] = version
            lock['packages']['']['dependencies'][name] = version
            entry = lock['packages']['node_modules/' + name]
            entry['version'] = version
            entry['resolved'] = f'https://registry.npmjs.org/{name}/-/{name.rsplit("/", 1)[1]}-{version}.tgz'
        # Controlled source responses, never a claim about published artifact bytes.
        self.snapshots = {context.FILES[0]: json.dumps(package).encode(), context.FILES[1]: json.dumps(lock).encode()}

    def json(self, path):
        if path == f'repos/{context.REPOSITORY}':
            return {'full_name': context.REPOSITORY, 'default_branch': 'trunk'}
        if path == f'repos/{context.REPOSITORY}/branches/trunk':
            return {'name': 'trunk', 'protected': self.protected, 'commit': {'sha': HEAD}}
        assert path == f'repos/{context.REPOSITORY}/compare/{SOURCE}...{HEAD}'
        return {'status': 'ahead', 'base_commit': {'sha': SOURCE},
                'merge_base_commit': {'sha': SOURCE if self.ancestor else 'c' * 40}}

    def file(self, repository, source, path):
        self.reads.append((repository, source, path))
        assert (repository, source) == (context.REPOSITORY, SOURCE)
        return self.snapshots[path]

    def green(self, name, repository, source):
        assert (name, repository, source) == ('geospatial-mcp', context.REPOSITORY, SOURCE)
        return False, 'Protocol Certification failed before client execution'

    def pages(self, path, key):
        assert path == f'repos/{context.REPOSITORY}/commits/{SOURCE}/check-runs'
        assert key == 'check_runs'
        return [{'id': 123, 'name': 'Protocol Certification', 'head_sha': self.check_source,
                 'status': 'completed', 'conclusion': 'failure', 'details_url': 'https://github.com/example/run/123'}]

    def commits(self, repository, limit):
        assert repository == context.REPOSITORY
        return [SOURCE]


def catalog():
    return json.loads((context.ROOT / context.convergence.CATALOG).read_bytes())


def test_real_generator_stages_complete_context_and_keeps_red_selector_refusal(tmp_path):
    github = GitHub()
    frozen = {path: (context.ROOT / path).read_bytes() for path in
              ('platform-manifest.yaml', 'customer-install-manifest.json',
               str(context.convergence.CATALOG), str(context.convergence.REVISIONS))}
    output = tmp_path / 'review'
    report = context.prepare(context.ROOT, output, SOURCE, github)
    assert report['candidateAdmission'] is False
    assert report['requirements']['qualifiedRows'] == 0
    assert report['producer']['normalCandidateCi'] == {
        'pass': False, 'reason': 'Protocol Certification failed before client execution'}
    assert report['producer']['checks'][0]['conclusion'] == 'failure'
    assert report['requirements']['clientVersions'] == {
        'mcp-typescript-sdk': '1.32.1', 'mcp-inspector': '2.10.1'}
    assert report['requirements']['originalRows'] == report['requirements']['proposedRows'] >= 2713
    assert report['requirements']['mcpRows'] == 82
    assert github.reads == [(context.REPOSITORY, SOURCE, path) for path in context.FILES]
    assert all((context.ROOT / path).read_bytes() == data for path, data in frozen.items())
    assert not (output / 'platform-manifest.yaml').exists()
    assert not (output / 'customer-install-manifest.json').exists()
    inventory = json.loads((output / 'source-context-inventory.json').read_bytes())
    for path, expected in inventory.items():
        assert context.digest((output / path).read_bytes()) == expected
    for path in context.FILES:
        assert (output / 'certification/sources/geospatial-mcp' / Path(path).name).read_bytes() == github.snapshots[path]
    with pytest.raises(resolver.ResolutionError, match='no qualifying trunk commit.*Protocol Certification failed'):
        resolver.select_component('geospatial-mcp', {'repository': context.REPOSITORY}, github, None, 1)


@pytest.mark.parametrize('mutation', ['facet', 'auth', 'tier', 'non-mcp', 'missing', 'duplicate', 'other-source'])
def test_review_refuses_any_unrelated_or_weakened_obligation(mutation):
    before = catalog()
    after = copy.deepcopy(before)
    after['source_revisions']['geospatial-mcp']['commit'] = SOURCE
    row = next(row for row in after['requirements'] if row['surface'] == 'mcp')
    if mutation == 'facet': row['scenario_facets'].remove('auth')
    elif mutation == 'auth': row['auth_policy_revision'] = 'anonymous-public-v1' if row['auth_policy_revision'] != 'anonymous-public-v1' else 'mcp-protected-operation-v1'
    elif mutation == 'tier': row['required_tier'] = 'release' if row['required_tier'] != 'release' else 'pr'
    elif mutation == 'non-mcp': next(row for row in after['requirements'] if row['surface'] != 'mcp')['client_version'] = 'different'
    elif mutation == 'missing': after['requirements'].remove(row)
    elif mutation == 'duplicate': after['requirements'].append(copy.deepcopy(row))
    elif mutation == 'other-source': after['source_revisions']['server']['commit'] = SOURCE
    with pytest.raises(context.PreparationError):
        context.preserve_obligations(before, after, SOURCE)


@pytest.mark.parametrize('case', ['short-source', 'mutable-source', 'unprotected', 'not-ancestor', 'wrong-check'])
def test_untrusted_or_mixed_source_leaves_no_output(tmp_path, case):
    github = GitHub()
    source = SOURCE
    if case == 'short-source': source = SOURCE[:7]
    elif case == 'mutable-source': source = 'trunk'
    elif case == 'unprotected': github.protected = False
    elif case == 'not-ancestor': github.ancestor = False
    elif case == 'wrong-check': github.check_source = HEAD
    output = tmp_path / 'refused'
    with pytest.raises(context.PreparationError):
        context.prepare(context.ROOT, output, source, github)
    assert not output.exists()


def test_actual_generator_refuses_mixed_package_lock_before_output(tmp_path):
    github = GitHub()
    lock = json.loads(github.snapshots[context.FILES[1]])
    lock['packages']['']['dependencies']['@modelcontextprotocol/inspector'] = '2.3.0'
    github.snapshots[context.FILES[1]] = json.dumps(lock).encode()
    output = tmp_path / 'refused'
    with pytest.raises(subprocess.CalledProcessError):
        context.prepare(context.ROOT, output, SOURCE, github)
    assert not output.exists()


@pytest.mark.parametrize('case', ['inside', 'existing'])
def test_output_cannot_overwrite_release_source_or_existing_artifacts(tmp_path, case):
    output = context.ROOT / 'do-not-create-context' if case == 'inside' else tmp_path
    with pytest.raises(context.PreparationError, match='new and outside'):
        context.prepare(context.ROOT, output, SOURCE, GitHub())


def test_source_preparation_cannot_reuse_a_bound_ledger(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    for path in ('customer-install-manifest.json', str(context.convergence.REVISIONS),
                 str(context.convergence.CATALOG), context.POLICY, context.CHECKER):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('{}')
    (root / 'platform-manifest.yaml').write_text('protocolCertification:\n  ledger:\n    status: bound\n')
    output = tmp_path / 'refused'
    github = GitHub()
    with pytest.raises(context.PreparationError, match='explicitly pending'):
        context.prepare(root, output, SOURCE, github)
    assert not github.reads
    assert not output.exists()
