"""Source-facts producer counts with the candidate's own policy and fails closed."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest
import yaml

import rc_gate_window as gate
import release_promise
import source_facts as facts

ROOT = Path(__file__).resolve().parents[1]
CHECKER = '''
def mentions_retired_cache(line):
    return "redis" in line.casefold()


def exempt(path):
    return path.endswith("CHANGELOG.md") or path == "scripts/ci/check-retired-cache.py"
'''
CLEAN_TREE = {
    'scripts/ci/check-retired-cache.py': CHECKER,
    'Directory.Packages.props': '<Project><PackageVersion Include="Npgsql" Version="9" /></Project>',
    'src/App/App.csproj': '<Project><PackageReference Include="Npgsql" /></Project>',
    'src/App/Store.cs': 'class Store { }\n',
    'src/App/CHANGELOG.md': 'Removed the Redis cache.\n',
}


def commit_tree(root: Path, files: dict) -> str:
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, Path):
            target.symlink_to(content)
        else:
            target.write_text(content)
    git = ['git', '-C', str(root), '-c', 'user.name=t', '-c', 'user.email=t@example.test']
    subprocess.run([*git, 'init', '-q'], check=True)
    subprocess.run([*git, 'add', '-A'], check=True)
    subprocess.run([*git, 'commit', '-q', '-m', 'tree'], check=True)
    return subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True).strip()


@pytest.fixture
def candidate_tree(monkeypatch, tmp_path):
    def build(extra=None, drop=()):
        files = {key: value for key, value in {**CLEAN_TREE, **(extra or {})}.items() if key not in drop}
        sha = commit_tree(tmp_path / 'server', files)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv('SERVER_SHA', sha)
        monkeypatch.setenv('SERVER_DIGEST', 'sha256:' + 'b' * 64)
        monkeypatch.setenv('LOCK_DIGEST', 'sha256:' + 'c' * 64)
        monkeypatch.setenv('SERVER_CHECKOUT', 'server')
        return gate.Candidate(sha, 'sha256:' + 'b' * 64, 'sha256:' + 'c' * 64)
    return build


def sha256_upper(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest().upper() + '\n'


def run_producer():
    code = facts.main()
    return code, json.loads(facts.RECEIPT.read_text()) if facts.RECEIPT.exists() else None


def test_clean_candidate_passes_the_contract_row(candidate_tree):
    candidate = candidate_tree()
    code, receipt = run_producer()
    assert code == 0
    assert receipt['status'] == 'PASS'
    assert receipt['assertions'] == {'redisPackages': 0, 'redisSourceReferences': 0}
    row = next(row for row in release_promise.load_contract() if row['id'] == 'cloud-no-redis-source')
    now = gate.dt.datetime.now(gate.dt.timezone.utc)
    stamp = lambda value: value.isoformat().replace('+00:00', 'Z')
    run = {'run_started_at': stamp(now - gate.dt.timedelta(minutes=1)), 'updated_at': stamp(now),
           'status': 'completed', 'conclusion': 'success', 'head_branch': 'trunk',
           'event': 'workflow_dispatch', 'path': '.github/workflows/gate-source-facts.yml',
           'reports': [receipt]}
    assert release_promise._run_passes(run, row, (candidate, now))


@pytest.mark.parametrize('extra, packages, sources', [
    ({'src/App/Store.cs': 'class Store { }\n// Redis fallback\nvar redis = 1;\n'}, 0, 2),
    ({'docker-compose.yml': 'services:\n  cache:\n    image: redis:7\n'}, 0, 1),
    ({'scripts/start.sh': 'export CACHE=redis://cache\n'}, 0, 1),
])
def test_retired_cache_lines_fail_with_counts(candidate_tree, extra, packages, sources):
    candidate_tree(extra)
    code, receipt = run_producer()
    assert code == 1
    assert receipt['status'] == 'FAIL'
    assert receipt['assertions'] == {'redisPackages': packages, 'redisSourceReferences': sources}
    assert len(receipt['findings']) == packages + sources


@pytest.mark.parametrize('path, text', [
    ('Directory.Packages.props', '<PackageVersion Include="StackExchange.Redis" Version="2" />'),
    ('src/App/App.csproj', '<PackageReference Update="Testcontainers.Redis" />'),
    ('src/App/App.csproj', "<PackageReference Include='StackExchange.Redis' />"),
    ('src/App/App.csproj', '<PackageReference\n    Version="2"\n    Include="StackExchange.Redis" />'),
    ('Directory.Build.props', '<GlobalPackageReference Include="Redis.Analyzers" />'),
    ('tools/web/package.json', json.dumps({'devDependencies': {'redis': '4'}})),
    ('tools/py/requirements-dev.txt', '# cache\nredis==5\n-r base.txt\n'),
    ('tools/py/pyproject.toml', '[project]\ndependencies = ["redis>=5"]\n'),
])
def test_package_declarations_count_as_packages_and_lines(candidate_tree, path, text):
    candidate_tree({path: text})
    code, receipt = run_producer()
    assert code == 1
    assert receipt['assertions']['redisPackages'] == 1
    assert receipt['assertions']['redisSourceReferences'] >= 1


def test_unparsed_manifest_form_still_counts_as_a_reference(candidate_tree):
    candidate_tree({'src/App/App.csproj': '<ProjectReference Include="../Cache.Redis/Cache.Redis.csproj" />'})
    assert run_producer()[1]['assertions'] == {'redisPackages': 0, 'redisSourceReferences': 1}


def test_candidate_exemptions_and_pinned_migrations_are_not_references(candidate_tree):
    migration = 'src/App/Migrations/001_Init.sql'
    text = '-- replaced the Redis lease\n'
    candidate_tree({migration: text,
                    f'eng/schema/migration-hashes/{migration}.sha256': sha256_upper(text),
                    'docs/cache.md': 'Redis is retired.\n',
                    'tests/Guards/NoCacheTests.cs': 'Assert.DoesNotContain("Redis", packages);\n'})
    assert run_producer()[1]['assertions'] == {'redisPackages': 0, 'redisSourceReferences': 0}


@pytest.mark.parametrize('pin', [None, 'ab\n'])
def test_unpinned_or_changed_migration_is_a_reference(candidate_tree, pin):
    migration = 'src/App/Migrations/002_Next.sql'
    candidate_tree({migration: '-- Redis\n', **({f'eng/schema/migration-hashes/{migration}.sha256': pin} if pin else {})})
    assert run_producer()[1]['assertions']['redisSourceReferences'] == 1


def test_symlinks_are_not_followed(candidate_tree, tmp_path):
    outside = tmp_path / 'outside.txt'
    outside.write_text('Redis\n')
    candidate_tree({'src/App/link.txt': outside})
    assert run_producer()[1]['assertions'] == {'redisPackages': 0, 'redisSourceReferences': 0}


def test_modified_checkout_is_refused(candidate_tree, tmp_path):
    candidate_tree()
    (tmp_path / 'server/src/App/Store.cs').write_text('class Changed { }\n')
    assert run_producer() == (1, None)


@pytest.mark.parametrize('variable', ['SERVER_SHA', 'SERVER_DIGEST', 'LOCK_DIGEST'])
def test_incomplete_candidate_leaves_no_receipt(candidate_tree, monkeypatch, variable):
    candidate_tree()
    facts.RECEIPT.write_text('old PASS')
    monkeypatch.delenv(variable)
    assert run_producer() == (1, None)


def test_checkout_of_another_commit_is_refused(candidate_tree, monkeypatch):
    candidate_tree()
    monkeypatch.setenv('SERVER_SHA', 'd' * 40)
    assert run_producer() == (1, None)


@pytest.mark.parametrize('checker', [None, 'def exempt(path):\n    return False\n'])
def test_missing_candidate_policy_fails_closed(candidate_tree, checker):
    path = 'scripts/ci/check-retired-cache.py'
    candidate_tree({path: checker} if checker else None, drop=() if checker else (path,))
    assert run_producer() == (1, None)


def test_unreadable_package_manifest_fails_closed(candidate_tree):
    candidate_tree({'tools/web/package.json': '{not json'})
    assert run_producer() == (1, None)


def test_workflow_binds_the_candidate_checkout_and_uploads_the_contract_receipt():
    workflow = yaml.safe_load((ROOT / '.github/workflows/gate-source-facts.yml').read_text())
    assert set(workflow[True]) == {'workflow_dispatch'}
    assert workflow['permissions'] == {'contents': 'read'}
    job = workflow['jobs']['facts']
    assert job['if'] == "github.repository == 'honua-io/honua-release' && github.ref == 'refs/heads/trunk'"
    server = next(step for step in job['steps'] if step.get('name') == 'Check out the candidate server source')
    assert server['with']['repository'] == 'honua-io/honua-server'
    assert server['with']['ref'] == '${{ inputs.server_sha }}'
    assert {'run': 'python -m pip install PyYAML'} in job['steps']
    upload = job['steps'][-1]
    assert upload['if'] == 'always()'
    assert upload['with']['name'] == 'release-promise-receipts'
    assert upload['with']['path'] == 'cloud-no-redis-source.json'
