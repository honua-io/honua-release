import base64
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
        if path == resolver.COMPONENT_VERSIONS_PATH:
            return declaration_bytes('honua-server', schemaVersions={})
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


def resolve_fixture(monkeypatch, source, stale_journal, extra=None):
    server = {'repository': 'https://github.com/honua-io/honua-server', 'sha': NEW, 'dbSchema': '1',
              'migrationJournalSha256': stale_journal}
    manifest = {'components': {'honua-server': server},
                'protocolCertification': {'ledger': {'status': 'bound'}}}
    manifest.update(extra or {})
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


# --- release/component-versions.json (honua-release#231 WI-3a) ---

def declaration_bytes(component, **overrides):
    body = {'format': 'honua.component-versions/v1', 'component': component,
            'contractVersions': {'admin': 'v1'}, 'schemaVersions': {'metadata': '2.0.0'}}
    body.update(overrides)
    return json.dumps(body).encode()


class Declarations:
    """`GitHub.file` for one declaration per (repository, sha); anything else is a 404."""

    def __init__(self, files):
        self.files, self.reads = files, []

    def file(self, repository, revision, path):
        self.reads.append((repository, revision, path))
        try:
            return self.files[(repository, revision, path)]
        except KeyError:
            raise resolver.ResolutionError(f'gh api repos/{repository}/contents/{path}?ref={revision} '
                                           'failed: gh: Not Found (HTTP 404)') from None


def declared(name, raw, **component):
    row = {'repository': f'https://github.com/honua-io/{name}', 'sha': NEW, **component}
    source = Declarations({(f'honua-io/{name}', NEW, resolver.COMPONENT_VERSIONS_PATH): raw})
    return resolver.component_versions(source, name, row), source


def test_declaration_is_read_at_the_pinned_sha():
    versions, source = declared('honua-console', declaration_bytes(
        'honua-console', contractVersions={'console-api': '1.0.0'}, schemaVersions={'workspace': '3'}))
    assert versions == {'contractVersions': {'console-api': '1.0.0'}, 'schemaVersions': {'workspace': '3'}}
    assert source.reads == [('honua-io/honua-console', NEW, 'release/component-versions.json')]


def test_declaration_goes_through_the_contents_api_at_the_sha(monkeypatch):
    raw = declaration_bytes('honua-iac')
    gh = FakeGh({'repos/honua-io/honua-iac/contents/release/component-versions.json':
                 {'content': base64.b64encode(raw).decode(), 'encoding': 'base64'}})
    monkeypatch.setattr(resolver.subprocess, 'run', gh)
    component = {'repository': 'https://github.com/honua-io/honua-iac', 'sha': NEW}
    assert resolver.component_versions(resolver.GitHub(), 'honua-iac', component)['contractVersions'] == {'admin': 'v1'}
    assert gh.calls == [f'repos/honua-io/honua-iac/contents/release/component-versions.json?ref={NEW}']


def test_a_missing_declaration_refuses_the_component(monkeypatch):
    gh = FakeGh({'repos/honua-io/honua-helm/contents/': failing('gh: Not Found (HTTP 404)')})
    monkeypatch.setattr(resolver.subprocess, 'run', gh)
    component = {'repository': 'https://github.com/honua-io/honua-helm', 'sha': NEW}
    with pytest.raises(resolver.ResolutionError,
                       match=rf'^honua-helm: honua-io/honua-helm@{NEW}:release/component-versions.json '
                             r'is missing or unreadable: .*HTTP 404'):
        resolver.component_versions(resolver.GitHub(), 'honua-helm', component)


@pytest.mark.parametrize('raw,message', [
    (b'{"format": ', 'is not a JSON document'),
    (b'\xff\xfe', 'is not a JSON document'),
    (b'{"format": "honua.component-versions/v1", "format": "x", "component": "honua-console", '
     b'"contractVersions": {}, "schemaVersions": {}}', "duplicate key 'format'"),
    (b'{"contractVersions": {"a": "1", "a": "2"}}', "duplicate key 'a'"),
    (b'[]', 'does not match'),
    (declaration_bytes('honua-console', format='honua.component-versions/v2'), 'format'),
    (declaration_bytes('honua-console', contractVersions=None), 'contractVersions'),
    (json.dumps({'format': 'honua.component-versions/v1', 'component': 'honua-console',
                 'contractVersions': {'a': '1'}}).encode(), "'schemaVersions' is a required property"),
    (declaration_bytes('honua-console', extra='x'), 'Additional properties'),
    (declaration_bytes('honua-console', contractVersions={'api': 1}), 'contractVersions/api'),
    (declaration_bytes('honua-console', contractVersions={'api': ' 1'}), 'contractVersions/api'),
    (declaration_bytes('honua-console', schemaVersions={'database': '120'}), 'schemaVersions'),
    (declaration_bytes('honua-sdk-js'), "declares component 'honua-sdk-js', not 'honua-console'"),
    (declaration_bytes('honua-console', contractVersions={'api': 'pending'}), 'not an exact version'),
    (declaration_bytes('honua-console', contractVersions={'api': '^1.0.0'}), 'not an exact version'),
    (declaration_bytes('honua-console', schemaVersions={'workspace': 'latest'}), 'not an exact version'),
])
def test_an_invalid_declaration_refuses_the_component(raw, message):
    with pytest.raises(resolver.ResolutionError, match=r'^honua-console: .*' + re.escape(message)):
        declared('honua-console', raw)


@pytest.mark.parametrize('group', ['contractVersions', 'schemaVersions'])
def test_an_empty_map_is_refused_for_a_component_that_is_not_source_pinned(group):
    with pytest.raises(resolver.ResolutionError, match=f'{group} must be a non-empty mapping.*sourcePinnedOnly'):
        declared('honua-helm', declaration_bytes('honua-helm', **{group: {}}))


def test_an_explicit_empty_set_is_a_declaration_for_a_source_pinned_component():
    versions, _ = declared('honua-mobile', declaration_bytes(
        'honua-mobile', contractVersions={}, schemaVersions={}), sourcePinnedOnly=True)
    assert versions == {'contractVersions': {}, 'schemaVersions': {}}


def test_server_may_leave_schema_versions_to_the_derived_database_floor():
    versions, _ = declared('honua-server', declaration_bytes('honua-server', schemaVersions={}))
    assert versions['schemaVersions'] == {}
    with pytest.raises(resolver.ResolutionError, match='contractVersions must be a non-empty'):
        declared('honua-server', declaration_bytes('honua-server', contractVersions={}))


def test_a_source_pinned_component_still_needs_the_file():
    with pytest.raises(resolver.ResolutionError, match='honua-collect: .*missing or unreadable'):
        resolver.component_versions(Declarations({}), 'honua-collect', {
            'repository': 'https://github.com/honua-io/honua-collect', 'sha': NEW, 'sourcePinnedOnly': True})


def test_a_declaration_is_never_read_at_a_moving_ref():
    with pytest.raises(resolver.ResolutionError, match='no immutable revision'):
        resolver.component_versions(Declarations({}), 'honua-mobile', {
            'repository': 'https://github.com/honua-io/honua-mobile', 'sha': 'trunk'})


class TreeAndDeclarations(MigrationSource):
    """The selected server tree, plus one declaration per other repository at its pinned sha."""

    def __init__(self, files, **kwargs):
        super().__init__(**kwargs)
        self.declarations = Declarations(files)

    def file(self, repository, revision, path):
        if repository == 'honua-io/honua-server':
            return super().file(repository, revision, path)
        return self.declarations.file(repository, revision, path)


def test_resolve_replaces_hand_version_maps_with_the_declarations(monkeypatch):
    path = resolver.COMPONENT_VERSIONS_PATH
    source = TreeAndDeclarations({
        ('honua-io/honua-console', NEW, path): declaration_bytes(
            'honua-console', contractVersions={'console-api': '2'}, schemaVersions={'workspace': '3'}),
        ('honua-io/honua-mobile', OLD, path): declaration_bytes(
            'honua-mobile', contractVersions={}, schemaVersions={}),
    })
    experimental = {'honua-mobile': {'repository': 'https://github.com/honua-io/honua-mobile', 'sha': OLD,
                                     'sourcePinnedOnly': True, 'contractVersions': {'hand': '1'}}}
    candidate, _ = resolve_fixture(monkeypatch, source, 'sha256:' + 'f' * 64, extra={
        'experimental': experimental, 'components': {
            'honua-server': {'repository': 'https://github.com/honua-io/honua-server', 'sha': NEW,
                             'dbSchema': '1', 'contractVersions': {'hand': '9'},
                             'schemaVersions': {'database': '1'}},
            'honua-console': {'repository': 'https://github.com/honua-io/honua-console', 'sha': NEW,
                              'contractVersions': {'hand': '9'}}}})
    server = candidate['components']['honua-server']
    # The server declares its contracts; the database floor comes from the selected migration tree.
    assert server['contractVersions'] == {'admin': 'v1'}
    assert server['schemaVersions'] == {'database': '109'} and server['dbSchema'] == '109'
    assert candidate['components']['honua-console']['contractVersions'] == {'console-api': '2'}
    assert candidate['components']['honua-console']['schemaVersions'] == {'workspace': '3'}
    # An experimental row declares at the sha the manifest pins, not at a trunk head.
    assert candidate['experimental']['honua-mobile']['contractVersions'] == {}
    assert candidate['experimental']['honua-mobile']['schemaVersions'] == {}
    assert ('honua-io/honua-mobile', OLD, path) in source.declarations.reads


def test_resolve_names_every_component_without_a_declaration_and_keeps_no_hand_map(monkeypatch):
    source = TreeAndDeclarations({})
    hand = {'contractVersions': {'hand': '9'}, 'schemaVersions': {'hand': '9'}}
    components = {
        'honua-server': {'repository': 'https://github.com/honua-io/honua-server', 'sha': NEW, 'dbSchema': '1'},
        'honua-console': {'repository': 'https://github.com/honua-io/honua-console', 'sha': NEW, **hand},
        'honua-helm': {'repository': 'https://github.com/honua-io/honua-helm', 'sha': NEW, **hand},
    }
    experimental = {'honua-collect': {'repository': 'https://github.com/honua-io/honua-collect', 'sha': OLD,
                                      'sourcePinnedOnly': True}}
    with pytest.raises(resolver.ResolutionError) as refused:
        resolve_fixture(monkeypatch, source, 'sha256:' + 'f' * 64,
                        extra={'components': components, 'experimental': experimental})
    lines = str(refused.value).splitlines()
    for name in ('honua-console', 'honua-helm', 'honua-collect'):
        assert any(line.startswith(f'{name}: ') and 'release/component-versions.json is missing' in line
                   for line in lines), (name, lines)
    assert not any(line.startswith('honua-server: ') for line in lines)


def test_the_documented_example_is_a_valid_declaration():
    doc = (resolver.ROOT / 'docs' / 'COMPONENT-VERSION-DECLARATIONS.md').read_text(encoding='utf-8')
    example = re.search(r'```json\n(.*?)```', doc, re.S).group(1)
    name = json.loads(example)['component']
    versions, _ = declared(name, example.encode())
    assert versions['contractVersions'] and versions['schemaVersions']


@pytest.mark.parametrize('value', ['false', 'true', 1, None])
@pytest.mark.parametrize('empty', [True, False])
def test_source_pinned_only_requires_a_boolean(value, empty):
    maps = {'contractVersions': {}, 'schemaVersions': {}} if empty else {}
    with pytest.raises(resolver.ResolutionError, match='sourcePinnedOnly must be a boolean'):
        declared('honua-mobile', declaration_bytes('honua-mobile', **maps), sourcePinnedOnly=value)


def test_false_does_not_allow_empty_version_sets():
    with pytest.raises(resolver.ResolutionError, match='contractVersions must be a non-empty mapping'):
        declared('honua-mobile', declaration_bytes('honua-mobile', contractVersions={}, schemaVersions={}),
                 sourcePinnedOnly=False)
