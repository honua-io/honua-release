import copy
import hashlib
import json
import re
import subprocess

import pytest

import resolve_trunk_candidate as resolver

NEW, OLD = 'a' * 40, 'b' * 40


class GitHub:
    def commits(self, repository, limit):
        return iter([NEW, OLD][:limit])

    def green(self, name, repository, sha):
        return True, 'full CI green'


class Registry:
    def candidate_tags(self, repository, sha):
        return [] if sha == NEW else ['nightly-' + sha[:7]]

    def image(self, name, component, sha):
        if sha == NEW or not self.candidate_tags('honua-io/server', sha):
            raise resolver.ResolutionError('no published SHA-bound image')
        return {'image': 'ghcr.io/honua-io/server@sha256:' + 'c' * 64,
                'digest': 'sha256:' + 'c' * 64,
                'platformDigests': {'amd64': 'sha256:' + 'd' * 64},
                'artifactSourceRevision': sha}


def component():
    return {'repository': 'https://github.com/honua-io/server', 'image': 'ghcr.io/honua-io/server:old',
            'sha': 'e' * 40, 'architectures': ['amd64']}


def test_green_commit_without_published_image_is_refused():
    class Guard(GitHub):
        def green(self, *_):
            raise AssertionError('CI consulted before a published image')

    with pytest.raises(resolver.ResolutionError, match='server: no qualifying.*no published SHA-bound image'):
        resolver.select_component('server', component(), Guard(), Registry(), 1)


def test_newest_green_published_commit_wins_over_unpublished_head():
    selected = resolver.select_component('server', component(), GitHub(), Registry(), 2)
    assert selected['sha'] == OLD
    assert '@sha256:' in selected['image']
    assert selected['artifactSourceRevision'] == OLD


def test_source_snapshot_does_not_rewrite_published_artifact():
    source = {'repository': 'https://github.com/honua-io/sdk', 'artifact': 'npm:@honua/sdk',
              'sha': OLD, 'version': '1.2.3', 'artifactSourceRevision': OLD}
    selected = resolver.select_component('sdk', source, GitHub(), None, 2)
    assert selected['sha'] == NEW
    assert selected['version'] == '1.2.3'
    assert selected['artifactSourceRevision'] == OLD


def test_red_ci_never_reads_image_identity():
    class Red(GitHub):
        def green(self, *_):
            return False, 'required suite failed'

    class Tags(Registry):
        def candidate_tags(self, repository, sha):
            return ['nightly-' + sha[:7]]

        def image(self, *args):
            raise AssertionError('image identity read for a red commit')

    with pytest.raises(resolver.ResolutionError, match='required suite failed'):
        resolver.select_component('server', component(), Red(), Tags(), 1)


def test_floating_channel_tags_are_not_candidate_images():
    registry = resolver.Registry(None)
    registry.tags = lambda repository: [
        'latest', 'stable', 'nightly', 'nightly-aot', '2026.1',
        'nightly-aaaaaaa', 'nightly-aot-aaaaaaa', 'nightly-lambda-aot-aaaaaaa-amd64',
        'candidate-aaaaaaaaaaaa-9-1',
    ]
    assert registry.candidate_tags('honua-io/server', NEW) == [
        'candidate-aaaaaaaaaaaa-9-1', 'nightly-aaaaaaa', 'nightly-aot-aaaaaaa']


def registry_fixture(sha=NEW):
    documents = {}
    def add(value):
        raw = json.dumps(value).encode()
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        documents[digest] = raw
        return digest
    config = add({'architecture': 'amd64', 'os': 'linux',
                  'config': {'Labels': {'org.opencontainers.image.revision': sha}}})
    child = add({'config': {'digest': config}})
    index = add({'manifests': [{'digest': child, 'platform': {'architecture': 'amd64', 'os': 'linux'}}]})
    registry = resolver.Registry(None)
    def request(repository, suffix, **_):
        target = suffix.split('/')[-1]
        digest = index if target == 'nightly-aaaaaaa' else target
        return documents[digest], {'Docker-Content-Digest': digest}
    registry.request = request
    return registry, index, child


def test_registry_reads_index_and_children_from_actual_bytes():
    registry, index, child = registry_fixture()
    result = registry.identity('honua-io/server', 'nightly-aaaaaaa', NEW, ['amd64'])
    assert result['digest'] == index
    assert result['platformDigests'] == {'amd64': child}
    assert result['image'].endswith('@' + index)


@pytest.mark.parametrize('sha,architectures,message', [(OLD, ['amd64'], 'not bound'),
    (NEW, ['amd64', 'arm64'], 'missing architectures')])
def test_registry_rejects_wrong_sha_or_incomplete_architectures(sha, architectures, message):
    registry, _, _ = registry_fixture(sha)
    with pytest.raises(resolver.ResolutionError, match=message):
        registry.identity('honua-io/server', 'nightly-aaaaaaa', NEW, architectures)


def test_github_api_disables_color_before_parsing_json(monkeypatch):
    seen = {}

    def run(cmd, **kwargs):
        seen['env'] = kwargs.get('env')
        class Result:
            stdout = json.dumps([{'sha': 'a' * 40}])
            stderr = ''
            returncode = 0
        return Result()

    monkeypatch.setattr(resolver.subprocess, 'run', run)
    assert next(resolver.GitHub().commits('honua-io/server', 1)) == 'a' * 40
    assert seen['env']['NO_COLOR'] == '1'
    assert seen['env']['GH_FORCE_TTY'] == '0'


def test_transient_network_retry_keeps_same_command(monkeypatch):
    calls, sleeps = [], []
    def request():
        calls.append('same')
        if len(calls) < 3:
            raise subprocess.CalledProcessError(1, ['gh', 'api'], stderr='error connecting to api.github.com')
        return 42
    monkeypatch.setattr(resolver.time, 'sleep', sleeps.append)
    assert resolver.retry(request) == 42
    assert sleeps == [10, 30]
    assert calls == ['same'] * 3


class FakeGh:
    """`gh api` as subprocess.run sees it: a JSON body per path, or a failing exit."""

    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def __call__(self, cmd, **kwargs):
        path = cmd[2]
        self.calls.append(path)
        for prefix, answer in self.responses.items():
            if path.startswith(prefix):
                break
        else:
            raise AssertionError(f'unexpected gh api {path}')
        answer = answer(path) if callable(answer) else answer
        if isinstance(answer, subprocess.CalledProcessError):
            raise subprocess.CalledProcessError(answer.returncode, cmd, output='', stderr=answer.stderr)
        class Result:
            stdout, stderr, returncode = json.dumps(answer), '', 0
        return Result()


def failing(stderr):
    return subprocess.CalledProcessError(1, ['gh', 'api'], stderr=stderr)


def manifest_file(tmp_path):
    import yaml
    path = tmp_path / 'platform-manifest.yaml'
    path.write_text(yaml.safe_dump({'components': {'honua-sdk-js': {
        'repository': 'https://github.com/honua-io/honua-sdk-js', 'sha': OLD, 'artifact': 'npm:@honua/sdk'}}}))
    matrix = tmp_path / 'compatibility-matrix.yaml'
    matrix.write_text('{}\n')
    return ['--manifest', str(path), '--matrix', str(matrix), '--out-dir', str(tmp_path / 'out')]


def test_a_404_from_a_repository_the_token_cannot_see_refuses_the_night(monkeypatch, tmp_path, capsys):
    gh = FakeGh({'repos/honua-io/honua-sdk-js/commits': failing('gh: Not Found (HTTP 404)')})
    monkeypatch.setattr(resolver.subprocess, 'run', gh)
    monkeypatch.setattr(resolver.time, 'sleep', lambda _: pytest.fail('a 404 is not transient'))
    assert resolver.main(manifest_file(tmp_path)) == 1
    err = capsys.readouterr().err
    assert err.startswith('REFUSED:') and 'honua-sdk-js' in err and 'HTTP 404' in err
    assert not (tmp_path / 'out').exists()


def test_a_404_on_check_runs_refuses_rather_than_reading_as_no_checks(monkeypatch, tmp_path, capsys):
    gh = FakeGh({'repos/honua-io/honua-sdk-js/commits?': [{'sha': NEW}],
                 f'repos/honua-io/honua-sdk-js/commits/{NEW}/check-runs': failing('gh: Not Found (HTTP 404)')})
    monkeypatch.setattr(resolver.subprocess, 'run', gh)
    assert resolver.main(manifest_file(tmp_path)) == 1
    assert re.search(r'check-runs\S* failed: gh: Not Found \(HTTP 404\)', capsys.readouterr().err)
    assert not (tmp_path / 'out').exists()


def test_an_exhausted_rate_limit_retries_then_refuses(monkeypatch, tmp_path, capsys):
    limited = failing('gh: API rate limit exceeded for installation ID 1. (HTTP 403)')
    gh = FakeGh({'repos/honua-io/honua-sdk-js/commits': limited})
    sleeps = []
    monkeypatch.setattr(resolver.subprocess, 'run', gh)
    monkeypatch.setattr(resolver.time, 'sleep', sleeps.append)
    assert resolver.main(manifest_file(tmp_path)) == 1
    assert sleeps == [10, 30, 60, 120, 60]
    assert len(gh.calls) == len(resolver.DELAYS)
    assert 'rate limit exceeded' in capsys.readouterr().err
    assert not (tmp_path / 'out').exists()


@pytest.mark.parametrize('pages', [
    # GitHub declared three check runs and returned two.
    [{'total_count': 3, 'check_runs': [{'id': 1}, {'id': 2}]}],
    # A full first page, then the count moved under the reader.
    [{'total_count': 150, 'check_runs': [{'id': n} for n in range(100)]},
     {'total_count': 120, 'check_runs': [{'id': n} for n in range(20)]}],
    # No count at all.
    [{'check_runs': []}],
    # Not a page.
    [['not', 'a', 'page']],
])
def test_a_truncated_check_run_page_refuses_before_ci_is_judged(monkeypatch, pages):
    served = iter(pages)
    gh = FakeGh({f'repos/honua-io/server/commits/{NEW}/check-runs': lambda _: next(served),
                 f'repos/honua-io/server/actions/runs': {'total_count': 0, 'workflow_runs': []}})
    monkeypatch.setattr(resolver.subprocess, 'run', gh)
    monkeypatch.setattr(resolver.ci, 'evaluate', lambda *a, **k: pytest.fail('CI judged on a partial page'))
    with pytest.raises(resolver.ResolutionError, match='truncated|total_count|has no check_runs'):
        resolver.GitHub().green('server', 'honua-io/server', NEW)


def test_a_complete_multi_page_listing_is_read_in_full(monkeypatch):
    first = {'total_count': 101, 'check_runs': [{'id': n} for n in range(100)]}
    second = {'total_count': 101, 'check_runs': [{'id': 100}]}
    served = iter([first, second])
    gh = FakeGh({'repos/honua-io/server/commits': lambda _: next(served)})
    monkeypatch.setattr(resolver.subprocess, 'run', gh)
    rows = list(resolver.GitHub().pages('repos/honua-io/server/commits/x/check-runs', 'check_runs'))
    assert [row['id'] for row in rows] == list(range(101))
    assert gh.calls[1].endswith('page=2')


def test_non_json_answer_refuses(monkeypatch):
    def run(cmd, **kwargs):
        class Result:
            stdout, stderr, returncode = '<html>rate limited</html>', '', 0
        return Result()
    monkeypatch.setattr(resolver.subprocess, 'run', run)
    with pytest.raises(resolver.ResolutionError, match='non-JSON'):
        resolver.GitHub().json('repos/honua-io/server')


# Hand-computed outside Python:
#   printf '%s\n' '["Honua.Server.Migrations.001_CreateHonuaSchema.sql","Honua.Server.Migrations.002_AddThing.sql"]' | sha256sum
FIXTURE_JOURNAL_SHA256 = 'sha256:7d59f866183bbaa6fcaba855f876ec6f42ff262020d23ca0fa2a8df84e1cc63b'
ADOPTION_DECLARATION = b'''internal static class ServerCoreSchemaMigrations
{
    internal static readonly PostgresCoreSchemaMigrationManifest Manifest = new(
        "Honua.Server",
        "Honua.Server.Migrations.109_AdoptConfiguredGuardedSchema.sql");
}
'''


class MigrationSource:
    """The selected honua-server tree: two numbered roots plus files DbUp never embeds."""

    def __init__(self, paths=None, declaration=ADOPTION_DECLARATION, truncated=False):
        self.paths = paths if paths is not None else [
            'src/Honua.Server/Migrations/002_AddThing.sql',
            'src/Honua.Server/Migrations/001_CreateHonuaSchema.sql',
            'src/Honua.Server/Migrations/109_AdoptConfiguredGuardedSchema.sql',
            'src/Honua.Server/Migrations/README.md',
            'src/Honua.Server/Migrations/Archive/000_NotEmbedded.sql',
            'src/Honua.Db/Postgres/Migrations/001_CreateRasterTables.sql',
            'src/Honua.Server/Startup/ServerCoreSchemaMigrations.cs',
        ]
        self.declaration, self.truncated, self.reads = declaration, truncated, []

    def json(self, path):
        self.reads.append(path)
        assert path == f'repos/honua-io/honua-server/git/trees/{NEW}?recursive=1'
        return {'truncated': self.truncated, 'tree': [{'path': path, 'type': 'blob'} for path in self.paths]}

    def file(self, repository, revision, path):
        self.reads.append(path)
        assert (repository, revision) == ('honua-io/honua-server', NEW)
        assert path == 'src/Honua.Server/Startup/ServerCoreSchemaMigrations.cs'
        return self.declaration


def test_migration_journal_of_a_fixture_tree_matches_the_hand_computed_hash():
    source = MigrationSource()
    paths = resolver.migration_tree(source, 'honua-io/honua-server', NEW)
    journal = resolver.migration_journal(source, paths, 'honua-io/honua-server', NEW)
    # Raster provider, adoption, nested and non-SQL files are not the default deployment's journal.
    assert sorted(journal) == ['Honua.Server.Migrations.001_CreateHonuaSchema.sql',
                               'Honua.Server.Migrations.002_AddThing.sql']
    assert resolver.upgrade_lock_binding.journal_digest(journal) == FIXTURE_JOURNAL_SHA256


def test_migration_journal_changes_when_one_script_is_added():
    source = MigrationSource()
    source.paths.append('src/Honua.Server/Migrations/003_AddAnother.sql')
    paths = resolver.migration_tree(source, 'honua-io/honua-server', NEW)
    journal = resolver.migration_journal(source, paths, 'honua-io/honua-server', NEW)
    assert resolver.upgrade_lock_binding.journal_digest(journal) != FIXTURE_JOURNAL_SHA256


@pytest.mark.parametrize('source,message', [
    (MigrationSource(truncated=True), 'truncated'),
    (MigrationSource(paths=['src/Honua.Server/Migrations/001_CreateHonuaSchema.sql']), 'is gone'),
    (MigrationSource(declaration=b'new("Honua.Server", "Honua.Server.Migrations.150_AdoptAgain.sql")'), 'no longer names'),
])
def test_migration_journal_refuses_instead_of_guessing(source, message):
    with pytest.raises(resolver.ResolutionError, match=message):
        paths = resolver.migration_tree(source, 'honua-io/honua-server', NEW)
        resolver.migration_journal(source, paths, 'honua-io/honua-server', NEW)


def resolve_fixture(monkeypatch, source, stale_journal):
    server = {'repository': 'https://github.com/honua-io/honua-server', 'sha': NEW, 'dbSchema': '1',
              'migrationJournalSha256': stale_journal}
    manifest = {'components': {'honua-server': server},
                'protocolCertification': {'ledger': {'status': 'bound'}}}
    matrix = {'data': {'honua-server': {'requiresDbSchema': '1'}}}
    monkeypatch.setattr(resolver, 'select_component', lambda name, component, *a: copy.deepcopy(component))
    monkeypatch.setattr(resolver, 'verify_manifest', lambda *a, **k: None)
    monkeypatch.setattr(resolver.validate_platform, 'validate',
                        lambda *a, **k: type('Findings', (), {'errors': []})())
    monkeypatch.setattr(resolver.validate_platform, 'check_legacy_evidence_pin_coherence', lambda *a: None)
    return resolver.resolve(manifest, matrix, source, None)


def test_resolve_replaces_a_hand_journal_with_the_selected_tree(monkeypatch):
    candidate, matrix = resolve_fixture(monkeypatch, MigrationSource(), 'sha256:' + 'f' * 64)
    server = candidate['components']['honua-server']
    assert server['migrationJournalSha256'] == FIXTURE_JOURNAL_SHA256
    assert server['dbSchema'] == '109'
    assert matrix['data']['honua-server']['requiresDbSchema'] == '109'


def test_resolve_refuses_when_the_migration_tree_cannot_be_read(monkeypatch):
    with pytest.raises(resolver.ResolutionError, match='honua-server migrations: .*truncated'):
        resolve_fixture(monkeypatch, MigrationSource(truncated=True), 'sha256:' + 'f' * 64)
