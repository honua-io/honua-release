import base64
import copy
import hashlib
import json
import re
import subprocess

import pytest
import yaml

import verify_client_artifacts as verifier

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


class Dated(GitHub):
    """Trunk of `count` commits, one a day, newest first; only `qualifies` is published and green."""

    def __init__(self, count, qualifies):
        self.shas = [f'{index:040x}' for index in range(count, 0, -1)]
        self.qualifies = qualifies

    def commits(self, repository, limit):
        return iter(self.shas[:limit])

    def commit_date(self, sha):
        return f'2026-09-{30 - self.shas.index(sha):02d}T12:00:00Z'

    def green(self, name, repository, sha):
        return sha == self.qualifies, 'full CI green' if sha == self.qualifies else 'required suite failed'


class Published(Registry):
    def __init__(self, published):
        self.published = published

    def candidate_tags(self, repository, sha):
        return ['nightly-' + sha[:7]] if sha in self.published else []

    def image(self, name, component, sha):
        return {'image': 'ghcr.io/honua-io/server@sha256:' + 'c' * 64, 'digest': 'sha256:' + 'c' * 64,
                'platformDigests': {'amd64': 'sha256:' + 'd' * 64}, 'artifactSourceRevision': sha}


NOW = resolver.datetime(2026, 10, 4, 12, tzinfo=resolver.timezone.utc)


def test_a_qualifying_sha_far_behind_head_still_reports_every_skip_and_staleness(capsys):
    github = Dated(20, None)
    selected_sha = github.shas[18]
    github.qualifies = selected_sha
    # Every newer commit is passed over: odd ones have no image, even ones are published but red.
    registry = Published({sha for index, sha in enumerate(github.shas) if index % 2 == 0} | {selected_sha})
    walk = {}
    selected = resolver.select_component('server', component(), github, registry, 100, walk)
    assert selected['sha'] == selected_sha

    report = resolver.skip_report('server', walk, github, now=NOW)
    assert report['selected'] == selected_sha and report['trunkHead'] == github.shas[0]
    assert report['commitsBehind'] == 18 and report['skippedTotal'] == 18
    assert report['daysBehind'] == 18.0 and report['stale'] is True
    assert report['selectedAgeDays'] == 22.0
    assert [row['sha'] for row in report['skipped']] == github.shas[:10]
    assert report['skipped'][0]['reason'] == 'CI required suite failed'
    assert report['skipped'][1]['reason'] == 'no published SHA-bound image'

    lines = resolver.skip_report_lines(report)
    assert lines[0].startswith(f'SKIPS server: selected {selected_sha[:7]} (22.0 days old); 18 newer')
    assert '  ... 8 older skipped commit(s) not listed' in lines
    assert lines[-1] == f'STALE-CANDIDATE: server selected {selected_sha[:7]} (18 commits, 18.0 days behind)'


def test_a_fresh_selection_is_reported_but_not_stale():
    github = Dated(5, None)
    github.qualifies = github.shas[2]
    walk = {}
    resolver.select_component('server', component(), github, Published(set(github.shas)), 100, walk)
    report = resolver.skip_report('server', walk, github, now=NOW, stale_days=3)
    assert (report['commitsBehind'], report['daysBehind'], report['stale']) == (2, 2.0, False)
    assert not any(line.startswith('STALE-CANDIDATE') for line in resolver.skip_report_lines(report))


def test_the_head_itself_qualifying_reports_zero_skips():
    github = Dated(3, None)
    github.qualifies = github.shas[0]
    walk = {}
    resolver.select_component('server', component(), github, Published(set(github.shas)), 100, walk)
    report = resolver.skip_report('server', walk, github, now=NOW)
    assert (report['commitsBehind'], report['skippedTotal'], report['stale']) == (0, 0, False)


def test_an_image_read_error_is_a_named_skip_reason():
    github = Dated(2, None)
    github.qualifies = github.shas[0]

    class Broken(Published):
        def image(self, name, component, sha):
            raise resolver.ResolutionError('config revision is not bound to ' + sha)

    walk = {}
    with pytest.raises(resolver.ResolutionError, match='no qualifying'):
        resolver.select_component('server', component(), github, Broken({github.shas[0]}), 100, walk)
    report = resolver.skip_report('server', walk, github, now=NOW)
    assert report['selected'] is None and report['stale'] is None and report['commitsBehind'] is None
    assert report['skipped'][0] == {'sha': github.shas[0], 'reason': f'config revision is not bound to {github.shas[0]}'}
    assert resolver.skip_report_lines(report)[0] == 'SKIPS server: no qualifying trunk commit; 2 commit(s) skipped'


def test_unknown_commit_dates_never_claim_staleness():
    walk = {}
    resolver.select_component('server', component(), GitHub(), Registry(), 2, walk)
    report = resolver.skip_report('server', walk, GitHub(), now=NOW)
    assert (report['commitsBehind'], report['daysBehind'], report['stale']) == (1, None, None)


def test_a_lag_just_past_the_threshold_is_stale_though_it_displays_at_the_threshold():
    class Hours(GitHub):
        def commit_date(self, sha):
            return '2026-09-04T01:00:00Z' if sha == NEW else '2026-09-01T00:00:00Z'

    walk = {}
    resolver.select_component('server', component(), Hours(), Registry(), 2, walk)
    report = resolver.skip_report('server', walk, Hours(), now=NOW, stale_days=3)
    # 73 hours is 3.04 days: shown rounded, compared raw.
    assert (report['daysBehind'], report['stale']) == (3.0, True)
    assert resolver.skip_report_lines(report)[-1] == f'STALE-CANDIDATE: server selected {OLD[:7]} (1 commits, 3.0 days behind)'


def test_a_spec_read_that_raises_reports_an_aborted_walk_not_an_exhausted_one():
    class Missing(GitHub):
        def file(self, repository, sha, path):
            raise resolver.ResolutionError(f'{path} at {sha}: HTTP 404')

    spec = {'repository': 'https://github.com/honua-io/geospatial-mcp',
            'artifact': f'spec:https://github.com/honua-io/geospatial-mcp/blob/{OLD}/spec/schemas/index.json'}
    walk = {}
    with pytest.raises(resolver.ResolutionError, match='HTTP 404'):
        resolver.select_component('geospatial-mcp', spec, Missing(), None, 2, walk)
    report = resolver.skip_report('geospatial-mcp', walk, Missing(), now=NOW)
    assert report['selected'] is None and report['skippedTotal'] == 0
    assert report['aborted'] == {'sha': NEW, 'reason': f'spec/schemas/index.json at {NEW}: HTTP 404'}
    assert resolver.skip_report_lines(report) == [
        f'SKIPS geospatial-mcp: walk aborted at {NEW[:7]} (spec/schemas/index.json at {NEW}: HTTP 404); '
        '0 newer commit(s) skipped before it; older commits not examined']
    assert (f'| geospatial-mcp | - | walk aborted at {NEW[:7]} | 0 | aborted: spec/schemas/index.json at {NEW}: '
            'HTTP 404 |') in resolver.skip_report_markdown({'geospatial-mcp': report})


def test_a_trunk_listing_that_fails_after_a_skip_names_the_listing_not_the_skipped_sha():
    class Truncated(GitHub):
        def commits(self, repository, limit):
            yield NEW
            raise OSError('gh api: connection reset')

    walk = {}
    with pytest.raises(OSError):
        resolver.select_component('server', component(), Truncated(), Registry(), 2, walk)
    report = resolver.skip_report('server', walk, Truncated(), now=NOW)
    assert report['aborted'] == {'sha': None, 'reason': 'gh api: connection reset'}
    assert resolver.skip_report_lines(report)[0] == (
        'SKIPS server: walk aborted at trunk listing (gh api: connection reset); '
        '1 newer commit(s) skipped before it; older commits not examined')


def test_a_completed_walk_is_never_marked_aborted():
    walk = {}
    resolver.select_component('server', component(), GitHub(), Registry(), 2, walk)
    assert resolver.skip_report('server', walk, GitHub(), now=NOW)['aborted'] is None
    walk = {}
    with pytest.raises(resolver.ResolutionError, match='no qualifying'):
        resolver.select_component('server', component(), GitHub(), Registry(), 1, walk)
    assert resolver.skip_report('server', walk, GitHub(), now=NOW)['aborted'] is None


def test_github_commits_records_committer_dates(monkeypatch):
    row = {'sha': NEW, 'commit': {'committer': {'date': '2026-09-16T08:00:00Z'}}}

    def run(cmd, **kwargs):
        class Result:
            stdout, stderr, returncode = json.dumps([row]), '', 0
        return Result()

    monkeypatch.setattr(resolver.subprocess, 'run', run)
    github = resolver.GitHub()
    assert list(github.commits('honua-io/server', 1)) == [NEW]
    assert github.commit_date(NEW) == '2026-09-16T08:00:00Z'


def test_resolve_prints_the_skip_report_for_a_selected_component_then_still_refuses(monkeypatch, capsys):
    github = Dated(6, None)
    github.qualifies = github.shas[5]

    original = resolver.select_component
    monkeypatch.setattr(resolver, 'select_component',
                        lambda name, comp, gh, registry, limit, walk, **_: original(
                            name, comp, github, Published(set(github.shas)), limit, walk))
    monkeypatch.setattr(resolver, 'verify_manifest', lambda *a, **k: None)
    monkeypatch.setattr(resolver, 'component_versions', lambda *a: (_ for _ in ()).throw(
        resolver.ResolutionError('honua-server: release/component-versions.json is missing')))
    skips = {}
    with pytest.raises(resolver.ResolutionError, match='component-versions'):
        resolver.resolve({'components': {'honua-server': component()}}, {}, github, None, 100, 'produce', skips)
    out = capsys.readouterr().out
    assert f'STALE-CANDIDATE: honua-server selected {github.shas[5][:7]} (5 commits, 5.0 days behind)' in out
    assert skips['honua-server']['stale'] is True


def test_main_writes_skips_json_and_summary_even_on_refusal(monkeypatch, tmp_path):
    report = {'component': 'honua-server', 'trunkHead': NEW, 'selected': OLD, 'selectedCommittedAt': None,
              'selectedAgeDays': 18.0, 'commitsBehind': 40, 'daysBehind': 17.5, 'staleAfterDays': 3,
              'stale': True, 'skippedTotal': 40, 'skipped': [{'sha': NEW, 'reason': 'CI a | b'}], 'aborted': None}

    def refuse(manifest, matrix, github, registry, limit, ledger, skips, *rest):
        skips['honua-server'] = report
        raise resolver.ResolutionError('honua-server: release/component-versions.json is missing')

    monkeypatch.setattr(resolver, 'resolve', refuse)
    monkeypatch.setattr(resolver, 'Registry', lambda github: None)
    summary = tmp_path / 'summary.md'
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(summary))
    (tmp_path / 'm.yaml').write_text('{}\n')
    out = tmp_path / 'out'
    assert resolver.main(['--manifest', str(tmp_path / 'm.yaml'), '--matrix', str(tmp_path / 'm.yaml'),
                          '--out-dir', str(out)]) == 1
    assert json.loads((out / 'skips.json').read_text()) == {'components': {'honua-server': report}}
    text = summary.read_text()
    assert '| honua-server | bbbbbbb | **STALE-CANDIDATE** 40 commits, 17.5 days | 40 | CI a \\| b |' in text


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


def _add_document(documents, value):
    raw = json.dumps(value).encode()
    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
    documents[digest] = raw
    return digest


def _bound_image(documents, sha):
    config = _add_document(documents, {'architecture': 'amd64', 'os': 'linux',
                                       'config': {'Labels': {'org.opencontainers.image.revision': sha}}})
    child = _add_document(documents, {'config': {'digest': config},
                                      'mediaType': 'application/vnd.oci.image.manifest.v1+json'})
    index = _add_document(documents, {'manifests': [
        {'digest': child, 'platform': {'architecture': 'amd64', 'os': 'linux'}},
    ]})
    return index, child


def _lambda_registry(sha, shape, lambda_sha=None):
    """shape is 'manifest', 'index', or 'attestation-only'. The server image stays bound to sha."""
    documents = {}
    server, server_child = _bound_image(documents, sha)
    lambda_sha = sha if lambda_sha is None else lambda_sha
    _, lambda_child = _bound_image(documents, lambda_sha)
    attestation = _add_document(documents, {'mediaType': 'application/vnd.oci.image.manifest.v1+json',
                                            'layers': []})
    if shape == 'manifest':
        pinned = lambda_child
    elif shape == 'index':
        pinned = _add_document(documents, {
            'mediaType': 'application/vnd.oci.image.index.v1+json',
            'manifests': [
                {'digest': lambda_child, 'platform': {'architecture': 'amd64', 'os': 'linux'}},
                {'digest': attestation, 'platform': {'architecture': 'unknown', 'os': 'unknown'},
                 'annotations': {'vnd.docker.reference.type': 'attestation-manifest'}},
            ],
        })
    else:
        pinned = _add_document(documents, {'manifests': [
            {'digest': attestation, 'platform': {'architecture': 'unknown', 'os': 'unknown'}},
        ]})
        lambda_child = None
    registry = resolver.Registry(None)

    def request(repository, suffix, **_):
        target = suffix.split('/')[-1]
        if target == f'nightly-{sha[:7]}':
            body, digest = documents[server], server
        elif target == f'nightly-lambda-aot-{sha[:7]}-amd64':
            body, digest = documents[pinned], pinned
        else:
            body, digest = documents[target], target
        return body, {'Docker-Content-Digest': digest}

    registry.request = request
    registry.tags = lambda repository: [f'nightly-{sha[:7]}', f'nightly-lambda-aot-{sha[:7]}-amd64']
    return registry, server_child, lambda_child


def _lambda_component():
    return {'image': 'ghcr.io/honua-io/honua-server:old',
            'awsLambdaImage': 'ghcr.io/honua-io/honua-server:old-lambda',
            'architectures': ['amd64'], 'sha': OLD}


def test_lambda_image_pins_a_single_architecture_manifest():
    registry, _, child = _lambda_registry(NEW, 'manifest')
    result = registry.image('honua-server', _lambda_component(), NEW)
    assert result['awsLambdaDigest'] == child
    assert result['awsLambdaImage'].endswith('@' + child)
    assert result['awsLambdaEcrDigest'] == 'pending-ecr-mirror'


def test_lambda_attested_index_pins_the_linux_amd64_child():
    registry, server_child, child = _lambda_registry(NEW, 'index')
    result = registry.image('honua-server', _lambda_component(), NEW)
    assert result['awsLambdaDigest'] == child
    assert result['awsLambdaDigest'] != result['digest']
    assert result['platformDigests'] == {'amd64': server_child}


def test_lambda_index_without_a_linux_amd64_image_refuses():
    registry, _, _ = _lambda_registry(NEW, 'attestation-only')
    with pytest.raises(resolver.ResolutionError, match='single linux/amd64'):
        registry.image('honua-server', _lambda_component(), NEW)


def test_lambda_child_bound_to_another_revision_refuses():
    registry, _, _ = _lambda_registry(NEW, 'index', lambda_sha=OLD)
    with pytest.raises(resolver.ResolutionError, match='Lambda image is not bound to candidate source'):
        registry.image('honua-server', _lambda_component(), NEW)


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


def manifest_file(tmp_path, component_name="honua-sdk-js"):
    pins = recorded_pins()
    import yaml
    path = tmp_path / 'platform-manifest.yaml'
    path.write_text(yaml.safe_dump({'components': {component_name: {
        'repository': f'https://github.com/honua-io/{component_name}', 'sha': OLD,
        'artifact': 'npm:@honua/sdk-js'}}, 'clientArtifacts': pins}))
    matrix = tmp_path / 'compatibility-matrix.yaml'
    matrix.write_text('{}\n')
    return ['--manifest', str(path), '--matrix', str(matrix), '--out-dir', str(tmp_path / 'out')]


def test_a_404_from_a_repository_the_token_cannot_see_refuses_the_night(monkeypatch, tmp_path, capsys):
    replay_registry(monkeypatch)
    gh = FakeGh({'repos/honua-io/honua-iac/commits': failing('gh: Not Found (HTTP 404)')})
    monkeypatch.setattr(resolver.subprocess, 'run', gh)
    monkeypatch.setattr(resolver.time, 'sleep', lambda _: pytest.fail('a 404 is not transient'))
    assert resolver.main(manifest_file(tmp_path, 'honua-iac')) == 1
    err = capsys.readouterr().err
    assert err.startswith('REFUSED:') and 'honua-iac' in err and 'HTTP 404' in err
    assert not (tmp_path / 'out').exists()


def test_a_404_on_check_runs_refuses_rather_than_reading_as_no_checks(monkeypatch, tmp_path, capsys):
    replay_registry(monkeypatch)
    gh = FakeGh({'repos/honua-io/honua-sdk-js/commits/1102d2d55916340edca13cb28411df8da8206f92/check-runs': failing('gh: Not Found (HTTP 404)')})
    monkeypatch.setattr(resolver.subprocess, 'run', gh)
    assert resolver.main(manifest_file(tmp_path)) == 1
    assert re.search(r'check-runs\S* failed: gh: Not Found \(HTTP 404\)', capsys.readouterr().err)
    assert not (tmp_path / 'out').exists()


def test_an_exhausted_rate_limit_retries_then_refuses(monkeypatch, tmp_path, capsys):
    limited = failing('gh: API rate limit exceeded for installation ID 1. (HTTP 403)')
    replay_registry(monkeypatch)
    gh = FakeGh({'repos/honua-io/honua-iac/commits': limited})
    sleeps = []
    monkeypatch.setattr(resolver.subprocess, 'run', gh)
    monkeypatch.setattr(resolver.time, 'sleep', sleeps.append)
    assert resolver.main(manifest_file(tmp_path, 'honua-iac')) == 1
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


# The selected server's capability-key vocabulary (docs/gis/data/capability-keys.v1.json), trimmed to
# the keys the SDK baselines name plus one they do not.
SERVER_CAPABILITY_KEYS = json.dumps({'schemaVersion': '1.1.0', 'capabilities': [
    {'key': key, 'edition': 'Community'} for key in (
        'ai.mcp-discovery', 'discovery.capability-manifest', 'serve.geoservices-featureserver',
        'serve.ogc-api-features', 'serve.wms')]}).encode()
# The exact release/sdk-capability-baseline.json each SDK repository commits (#231 WI-5).
SDK_BASELINES = resolver.ROOT / 'tools' / 'fixtures' / 'sdk-capability-baselines'


def baseline_bytes(name):
    return (SDK_BASELINES / f'{name}.json').read_bytes()


# The bytes the selected server tree carries at the content-digest paths (#231 WI-7).
CONTENT_FILES = {resolver.OKF_CONTENT_SOURCE[1]: b'{"version": "honua.okf-bundle/v1"}\n',
                 resolver.CATALOG_CONTENT_SOURCE[1]: b'{"schemaVersion": "1.1.0"}\n'}


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
        if path in CONTENT_FILES:
            return CONTENT_FILES[path]
        if path == resolver.SERVER_CAPABILITY_KEYS[1]:
            return SERVER_CAPABILITY_KEYS
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


def resolve_fixture(monkeypatch, source, stale_journal, extra=None, **server_fields):
    server = {'repository': 'https://github.com/honua-io/honua-server', 'sha': NEW, 'dbSchema': '1',
              'migrationJournalSha256': stale_journal, **server_fields}
    manifest = {'components': {'honua-server': server},
                'protocolCertification': {'ledger': {'status': 'bound'}}}
    manifest.update(extra or {})
    matrix = {'data': {'honua-server': {'requiresDbSchema': '1'}}}
    monkeypatch.setattr(resolver, 'select_component', lambda name, component, *a, **k: copy.deepcopy(component))
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


def test_a_chart_identity_from_another_sha_cannot_take_tonights_version():
    digest, package = 'sha256:' + 'c' * 64, 'sha256:' + 'd' * 64
    components = {
        'honua-helm': {'artifact': 'oci-chart:honua', 'sha': NEW, 'digest': digest,
                       'artifactSourceRevision': OLD, 'artifactSha256': package,
                       'artifactVersion': '2026.1.0-rc.2'},
        'honua-server': {'image': 'ghcr.io/honua-io/honua-server@sha256:' + 'e' * 64, 'sha': NEW,
                         'artifactSourceRevision': NEW, 'digest': 'sha256:' + 'e' * 64,
                         'artifactVersion': '2026.1.0-rc.2', 'releaseVersion': '2026.1.0-rc.2'},
    }
    resolver.release_carried_platform_identity(components)
    helm = components['honua-helm']
    assert 'digest' not in helm and 'artifactSourceRevision' not in helm and 'artifactSha256' not in helm
    assert 'artifactVersion' not in helm
    assert components['honua-server']['digest'] == 'sha256:' + 'e' * 64
    assert components['honua-server']['artifactSourceRevision'] == NEW
    assert 'artifactVersion' not in components['honua-server']
    assert 'releaseVersion' not in components['honua-server']


@pytest.mark.parametrize('name', ['honua-helm', 'honua-server', 'honua-console'])
def test_a_carried_plain_imaged_version_does_not_survive_selection(name):
    """R22: an imaged row's version is pre-release; a version carried from another night is reset."""
    row = {'sha': NEW, 'version': '2026.1.0-rc.2', 'artifactVersion': '2026.1.0-rc.2'}
    if name == 'honua-helm':
        row.update(artifact='oci-chart:honua', digest='sha256:' + 'c' * 64, artifactSourceRevision=OLD,
                   artifactSha256='sha256:' + 'd' * 64)
    else:
        row.update(image=f'ghcr.io/honua-io/{name}@sha256:' + 'e' * 64, digest='sha256:' + 'e' * 64,
                   artifactSourceRevision=NEW)
    components = {name: row, 'honua-sdk-js': {'sha': NEW, 'version': '0.1.12', 'artifactVersion': '0.1.12'}}
    resolver.release_carried_platform_identity(components)
    assert components[name]['version'] == 'pre-release'
    assert 'artifactVersion' not in components[name]
    assert components['honua-sdk-js'] == {'sha': NEW, 'version': '0.1.12', 'artifactVersion': '0.1.12'}


def test_a_chart_identity_bound_to_the_selected_sha_is_kept_for_the_stamp():
    digest, package = 'sha256:' + 'c' * 64, 'sha256:' + 'd' * 64
    components = {'honua-helm': {'artifact': 'oci-chart:honua', 'sha': NEW, 'digest': digest,
                                 'artifactSourceRevision': NEW, 'artifactSha256': package,
                                 'artifactVersion': '2026.1.0-rc.2'}}
    resolver.release_carried_platform_identity(components)
    helm = components['honua-helm']
    assert (helm['digest'], helm['artifactSourceRevision'], helm['artifactSha256']) == (digest, NEW, package)
    assert 'artifactVersion' not in helm


def test_resolve_drops_a_carried_forward_platform_version_for_tonights_stamp(monkeypatch):
    # R22 (#231 WI-2): mint stamps the platform version of tonight's label beside tonight's image.
    candidate, _ = resolve_fixture(monkeypatch, MigrationSource(), 'sha256:' + 'f' * 64,
                                   version='pre-release', artifactVersion='2026.1.0-rc.2',
                                   releaseVersion='2026.1.0-rc.2')
    server = candidate['components']['honua-server']
    assert 'artifactVersion' not in server and 'releaseVersion' not in server
    assert server['version'] == 'pre-release'


def test_resolve_refuses_when_the_migration_tree_cannot_be_read(monkeypatch):
    with pytest.raises(resolver.ResolutionError, match='honua-server migrations: .*truncated'):
        resolve_fixture(monkeypatch, MigrationSource(truncated=True), 'sha256:' + 'f' * 64)



# --- lock content digests (honua-release#231 WI-7) ---

def test_resolve_declares_okf_and_catalog_at_the_selected_server_sha(monkeypatch):
    hand = {'repository': 'https://github.com/honua-io/honua-server', 'revision': OLD,
            'path': 'hand.json', 'sha256': 'sha256:' + 'f' * 64}
    mcp = {'repository': 'https://github.com/honua-io/geospatial-mcp', 'revision': OLD,
           'path': 'spec/schemas/index.json', 'sha256': 'sha256:' + 'e' * 64}
    candidate, _ = resolve_fixture(monkeypatch, MigrationSource(), 'sha256:' + 'f' * 64, extra={
        'platformLockEvidence': {'contentDigests': {'okf': dict(hand), 'catalog': dict(hand), 'geospatialMcp': mcp}}})
    digests = candidate['platformLockEvidence']['contentDigests']
    for name, (_, path) in (('okf', resolver.OKF_CONTENT_SOURCE), ('catalog', resolver.CATALOG_CONTENT_SOURCE)):
        # The expected digest is computed here from the fixture bytes, never from resolver output.
        assert digests[name] == {'repository': 'https://github.com/honua-io/honua-server', 'revision': NEW,
                                 'path': path, 'sha256': 'sha256:' + hashlib.sha256(CONTENT_FILES[path]).hexdigest()}
    assert resolver.OKF_CONTENT_SOURCE == ('honua-server', 'scripts/ci/okf-bundle.v1.json')
    # Declarations this packet does not own are untouched.
    assert digests['geospatialMcp'] == mcp


def test_resolve_declares_content_digests_on_a_manifest_that_has_none(monkeypatch):
    candidate, _ = resolve_fixture(monkeypatch, MigrationSource(), 'sha256:' + 'f' * 64)
    assert set(candidate['platformLockEvidence']['contentDigests']) == {'okf', 'catalog'}


@pytest.mark.parametrize('name', ['okf', 'catalog'])
def test_a_missing_content_file_refuses_and_keeps_no_hand_declaration(monkeypatch, name):
    class Missing(MigrationSource):
        def file(self, repository, revision, path):
            if path == resolver.CONTENT_DIGEST_SOURCES[name][1]:
                raise resolver.ResolutionError(f'gh api repos/{repository}/contents/{path}?ref={revision} '
                                               'failed: gh: Not Found (HTTP 404)')
            return super().file(repository, revision, path)

    manifest_evidence = {'contentDigests': {name: {'repository': 'https://github.com/honua-io/honua-server',
                                                   'revision': OLD, 'path': 'x', 'sha256': 'sha256:' + 'f' * 64}}}
    seen = {}

    def capture(candidate, *a, **k):
        # The refusal path still qualifies the candidate; record what it would have carried.
        seen['digests'] = candidate['platformLockEvidence']['contentDigests']
        return type('Findings', (), {'errors': []})()

    monkeypatch.setattr(resolver, 'select_component', lambda n, c, *a, **k: copy.deepcopy(c))
    monkeypatch.setattr(resolver, 'verify_manifest', lambda *a, **k: None)
    monkeypatch.setattr(resolver.validate_platform, 'validate', capture)
    with pytest.raises(resolver.ResolutionError, match=rf'contentDigests\.{name}: .*HTTP 404') as refused:
        resolver.resolve({'components': {'honua-server': {
                              'repository': 'https://github.com/honua-io/honua-server', 'sha': NEW, 'dbSchema': '1'}},
                          'protocolCertification': {'ledger': {'status': 'bound'}},
                          'platformLockEvidence': manifest_evidence},
                         {'data': {'honua-server': {}}}, Missing(), None)
    assert name not in seen['digests']
    other = 'catalog' if name == 'okf' else 'okf'
    assert seen['digests'][other]['revision'] == NEW
    assert not any(f'contentDigests.{other}' in line for line in str(refused.value).splitlines())


def test_the_catalog_ruling_can_bind_the_evidence_ledger_commit_in_one_line(monkeypatch):
    commit, path = 'c' * 40, 'data/protocol-certification.v1.json'
    monkeypatch.setitem(resolver.CONTENT_DIGEST_SOURCES, 'catalog', (resolver.LEDGER, path))
    source = Declarations({('honua-io/honua-evidence', commit, path): b'{"ledger": true}\n'})
    candidate = {'protocolCertification': {'ledger': {'status': 'bound', 'repository': 'honua-io/honua-evidence',
                                                      'commit': commit}}}
    assert resolver.content_digest_declaration(source, candidate, 'catalog') == {
        'repository': 'https://github.com/honua-io/honua-evidence', 'revision': commit, 'path': path,
        'sha256': 'sha256:' + hashlib.sha256(b'{"ledger": true}\n').hexdigest()}


@pytest.mark.parametrize('revision', ['pending', 'trunk', '', None])
def test_a_content_digest_is_never_read_at_a_moving_or_pending_revision(revision):
    class Guard:
        def file(self, *a):
            raise AssertionError('content read without an immutable revision')

    candidate = {'components': {'honua-server': {'repository': 'https://github.com/honua-io/honua-server',
                                                 'sha': revision}},
                 'protocolCertification': {'ledger': {'repository': 'honua-io/honua-evidence', 'commit': revision}}}
    with pytest.raises(resolver.ResolutionError, match='no immutable revision'):
        resolver.content_digest_declaration(Guard(), candidate, 'okf')


def test_the_catalog_is_the_servers_runtime_feature_catalog_r24():
    # Ruling R24 (#376): the bytes the server image embeds and FeatureCatalogResource serves.
    assert resolver.CATALOG_CONTENT_SOURCE == ('honua-server', 'docs/gis/data/feature-catalog.json')


def git_blob(raw):
    return hashlib.sha1(b'blob %d\0' % len(raw) + raw).hexdigest()


class ContentsApi:
    """`gh api .../contents` as GitHub answers it: base64 up to 1 MB, encoding "none" above."""

    def __init__(self, raw, served=None):
        self.raw, self.served, self.calls = raw, raw if served is None else served, []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        assert cmd[:2] == ['gh', 'api'] and '/contents/' in cmd[2]
        if 'Accept: application/vnd.github.raw+json' in cmd:
            assert 'text' not in kwargs, 'raw bytes must not be decoded as text'
            return type('Result', (), {'stdout': self.served, 'stderr': b'', 'returncode': 0})()
        large = len(self.raw) > 1024 * 1024
        body = {'type': 'file', 'size': len(self.raw), 'sha': git_blob(self.raw),
                'encoding': 'none' if large else 'base64',
                'content': '' if large else base64.b64encode(self.raw).decode()}
        return type('Result', (), {'stdout': json.dumps(body), 'stderr': '', 'returncode': 0})()


SERVER_AT_NEW = {'components': {'honua-server': {'repository': 'https://github.com/honua-io/honua-server', 'sha': NEW}}}
EMPTY_SHA256 = 'sha256:' + hashlib.sha256(b'').hexdigest()


def large_catalog():
    """A >1 MB catalog like feature-catalog.json (~1.58 MB), deterministic and non-repeating."""
    rows = [{'key': f'feature-{index}', 'digest': hashlib.sha256(str(index).encode()).hexdigest()}
            for index in range(16000)]
    raw = json.dumps({'features': rows}, indent=2).encode() + b'\n'
    assert len(raw) > 1024 * 1024
    return raw


def test_a_catalog_over_one_megabyte_declares_the_digest_of_its_real_bytes(monkeypatch):
    raw = large_catalog()
    api = ContentsApi(raw)
    monkeypatch.setattr(resolver.subprocess, 'run', api)
    declaration = resolver.content_digest_declaration(resolver.GitHub(), SERVER_AT_NEW, 'catalog')
    assert declaration['sha256'] == 'sha256:' + hashlib.sha256(raw).hexdigest()
    assert declaration['sha256'] != EMPTY_SHA256
    assert declaration['path'] == 'docs/gis/data/feature-catalog.json' and declaration['revision'] == NEW
    assert [call[2] for call in api.calls] == [f'repos/honua-io/honua-server/contents/{declaration["path"]}?ref={NEW}'] * 2


def test_a_small_file_is_still_read_from_the_base64_body(monkeypatch):
    raw = b'{"version": "honua.okf-bundle/v1"}\n'
    api = ContentsApi(raw)
    monkeypatch.setattr(resolver.subprocess, 'run', api)
    assert resolver.GitHub().file('honua-io/honua-server', NEW, 'scripts/ci/okf-bundle.v1.json') == raw
    assert len(api.calls) == 1


@pytest.mark.parametrize('served', [b'', b'truncated', None])
def test_a_large_read_that_is_short_or_substituted_refuses(monkeypatch, served):
    raw = large_catalog()
    if served is None:
        # Same length, different bytes: only the blob sha catches it.
        served = raw[:-2] + b'X\n'
    monkeypatch.setattr(resolver.subprocess, 'run', ContentsApi(raw, served))
    with pytest.raises(resolver.ResolutionError, match=f'not the {len(raw)} bytes of blob {git_blob(raw)}'):
        resolver.GitHub().file('honua-io/honua-server', NEW, 'docs/gis/data/feature-catalog.json')


def test_an_encoding_none_body_without_raw_access_refuses_rather_than_hashing_nothing(monkeypatch):
    raw = large_catalog()
    api = ContentsApi(raw)

    def run(cmd, **kwargs):
        if 'Accept: application/vnd.github.raw+json' in cmd:
            raise subprocess.CalledProcessError(1, cmd, output=b'', stderr=b'gh: Not Found (HTTP 404)')
        return api(cmd, **kwargs)

    monkeypatch.setattr(resolver.subprocess, 'run', run)
    with pytest.raises(resolver.ResolutionError, match='HTTP 404'):
        resolver.content_digest_declaration(resolver.GitHub(), SERVER_AT_NEW, 'catalog')


def test_the_real_generator_clears_exactly_the_okf_and_catalog_rows(monkeypatch, tmp_path):
    """#231 WI-7: the trunk manifest, resolved with and without the content-digest step, through the
    real generate_platform_lock. Only $.contentDigests.okf and .catalog (inventory rows 33-34) clear."""
    import generate_platform_lock as generator

    manifest = yaml.safe_load((resolver.ROOT / 'platform-manifest.yaml').read_text())
    matrix = yaml.safe_load((resolver.ROOT / 'compatibility-matrix.yaml').read_text())
    # A bound ledger keeps resolve on its success path; the generator does not read it.
    manifest['protocolCertification']['ledger']['status'] = 'bound'
    server = manifest['components']['honua-server']
    repository, sha = server['repository'].removeprefix('https://github.com/'), server['sha']
    rows = {**manifest['components'], **(manifest.get('experimental') or {})}

    class Server:
        def file(self, repo, revision, path):
            assert (repo, revision) == (repository, sha)
            return CONTENT_FILES[path]

    # Everything except the content-digest step carries the manifest through unchanged.
    monkeypatch.setattr(resolver, 'verify_manifest', lambda *a, **k: None)
    monkeypatch.setattr(resolver, 'select_component', lambda name, component, *a, **k: copy.deepcopy(component))
    monkeypatch.setattr(resolver, 'select_sdk', lambda name, component, *a: copy.deepcopy(component))
    def versions(github, name, selected):
        # The server's own declaration always carries schemaVersions; resolve adds the derived floor.
        declared = {'schemaVersions': {}} if name == 'honua-server' else {}
        declared.update({group: copy.deepcopy(rows[name][group])
                         for group in ('contractVersions', 'schemaVersions') if group in rows[name]})
        return declared

    monkeypatch.setattr(resolver, 'component_versions', versions)
    monkeypatch.setattr(resolver, 'advertised_capabilities', lambda *a: frozenset())
    monkeypatch.setattr(resolver, 'sdk_capability_baseline', lambda *a: {})
    monkeypatch.setattr(resolver, 'migration_tree', lambda *a: [])
    monkeypatch.setattr(resolver, 'migration_floor', lambda *a: server['dbSchema'])
    monkeypatch.setattr(resolver, 'migration_journal', lambda *a: ['001_CreateHonuaSchema.sql'])
    monkeypatch.setattr(resolver.validate_platform, 'validate', lambda *a, **k: type('Findings', (), {'errors': []})())
    monkeypatch.setattr(resolver.validate_platform, 'check_legacy_evidence_pin_coherence', lambda *a: None)

    def generated(label, sources):
        monkeypatch.setattr(resolver, 'CONTENT_DIGEST_SOURCES', sources)
        candidate, candidate_matrix = resolver.resolve(manifest, matrix, Server(), None)
        # The cut timestamps are wall-clock; pin them so the two runs differ only in what is under test.
        candidate['protocolCertification']['candidateCutAt'] = '2026-10-03T00:00:00Z'
        candidate['snapshotDate'] = '2026-10-03'
        out = tmp_path / label
        out.mkdir()
        (out / 'platform-manifest.yaml').write_text(yaml.safe_dump(candidate, sort_keys=False))
        (out / 'compatibility-matrix.yaml').write_text(yaml.safe_dump(candidate_matrix, sort_keys=False))
        return candidate, generator.generate(out / 'platform-manifest.yaml', out / 'compatibility-matrix.yaml')

    sources = dict(resolver.CONTENT_DIGEST_SOURCES)
    control_candidate, control = generated('control', {})
    resolved_candidate, resolved = generated('resolved', sources)

    cleared = [row for row in control.unresolved if row not in resolved.unresolved]
    assert cleared == ['[AT-CUT] $.contentDigests.catalog: catalog digest is not declared',
                       '[AT-CUT] $.contentDigests.okf: OKF digest is not declared']
    assert [row for row in control.unresolved if row not in cleared] == resolved.unresolved
    assert [row for row in control.deferred_until_cut if row not in cleared] == resolved.deferred_until_cut

    expected = {name: 'sha256:' + hashlib.sha256(CONTENT_FILES[path]).hexdigest()
                for name, (_, path) in sources.items()}
    assert {k: v for k, v in resolved.lock['contentDigests'].items() if k in expected} == expected
    # Nothing else in the lock moved: the same draft once the two digests and the manifest's own
    # file identity (it now carries the declarations) are set aside.
    for draft in (control, resolved):
        for name in expected:
            draft.lock['contentDigests'].pop(name, None)
        draft.lock['sourceInputs'].pop('platformManifest')
    assert resolved.lock == control.lock
    for candidate in (control_candidate, resolved_candidate):
        for name in expected:
            candidate['platformLockEvidence']['contentDigests'].pop(name, None)
    assert resolved_candidate == control_candidate


# --- release/component-versions.json (honua-release#231 WI-3a) ---

def declaration_bytes(component, **overrides):
    body = {'format': 'honua.component-versions/v1', 'component': component,
            'contractVersions': {'admin': 'v1'}, 'schemaVersions': {'metadata': '2.0.0'}}
    body.update(overrides)
    return json.dumps(body).encode()


class Declarations:
    """`GitHub.file` for one declaration per (repository, sha); anything else is a 404."""

    def __init__(self, files, hidden=()):
        self.files, self.reads, self.hidden = files, [], set(hidden)

    def json(self, path):
        # Pinned commits are readable unless the repository is hidden from the token.
        self.reads.append(path)
        repository, _, sha = path.removeprefix('repos/').rpartition('/commits/')
        if repository in self.hidden:
            raise resolver.ResolutionError(f'gh api {path} failed: gh: Not Found (HTTP 404)', status=404)
        return {'sha': sha}

    def file(self, repository, revision, path):
        self.reads.append((repository, revision, path))
        try:
            return self.files[(repository, revision, path)]
        except KeyError:
            raise resolver.ResolutionError(f'gh api repos/{repository}/contents/{path}?ref={revision} '
                                           'failed: gh: Not Found (HTTP 404)', status=404) from None


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
                 {'content': base64.b64encode(raw).decode(), 'encoding': 'base64',
                  'size': len(raw), 'sha': git_blob(raw)}})
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


@pytest.mark.parametrize('name', ['honua-mobile', 'honua-collect'])
def test_source_pinned_previews_allow_missing_and_empty_declarations(name):
    row = {'repository': f'https://github.com/honua-io/{name}', 'sha': NEW,
           'sourcePinnedOnly': True}
    assert resolver.component_versions(Declarations({}), name, row) == {
        'contractVersions': {}, 'schemaVersions': {}}
    for raw in (b'', b' \n', declaration_bytes(name, contractVersions={}, schemaVersions={})):
        assert declared(name, raw, sourcePinnedOnly=True)[0] == {
            'contractVersions': {}, 'schemaVersions': {}}


@pytest.mark.parametrize('name,source_pinned', [
    ('honua-mobile', False), ('honua-collect', False), ('honua-helm', True),
    ('honua-server', True), ('honua-sdk-js', True)])
def test_missing_declaration_exemption_is_only_for_source_pinned_previews(name, source_pinned):
    with pytest.raises(resolver.ResolutionError, match='missing or unreadable'):
        resolver.component_versions(Declarations({}), name, {
            'repository': f'https://github.com/honua-io/{name}', 'sha': NEW,
            'sourcePinnedOnly': source_pinned})


@pytest.mark.parametrize('name', ['honua-mobile', 'honua-collect'])
def test_preview_404_from_an_invisible_repository_is_not_an_empty_set(name):
    source = Declarations({}, hidden={f'honua-io/{name}'})
    with pytest.raises(resolver.ResolutionError, match='pinned revision is not readable'):
        resolver.component_versions(source, name, {
            'repository': f'https://github.com/honua-io/{name}', 'sha': NEW,
            'sourcePinnedOnly': True})
    assert source.reads[-1] == f'repos/honua-io/{name}/commits/{NEW}'


def test_preview_404_needs_the_commit_probe_to_answer_the_pinned_sha():
    class OtherCommit(Declarations):
        def json(self, path):
            return {'sha': OLD}
    with pytest.raises(resolver.ResolutionError, match='did not answer that commit'):
        resolver.component_versions(OtherCommit({}), 'honua-mobile', {
            'repository': 'https://github.com/honua-io/honua-mobile', 'sha': NEW,
            'sourcePinnedOnly': True})


@pytest.mark.parametrize('detail,status', [
    ('HTTP 403', 403), ('HTTP 500', 500), ('connection reset by peer', None),
    # A message that merely mentions a 404 is not an API 404.
    ('blob HTTP 404 in the body text', None)])
def test_preview_declaration_read_errors_are_not_empty_sets(detail, status):
    class Unreadable:
        def file(self, *args):
            raise resolver.ResolutionError(detail, status=status)
    with pytest.raises(resolver.ResolutionError, match='missing or unreadable'):
        resolver.component_versions(Unreadable(), 'honua-mobile', {
            'repository': 'https://github.com/honua-io/honua-mobile', 'sha': NEW,
            'sourcePinnedOnly': True})


def test_preview_invalid_declaration_still_refuses():
    with pytest.raises(resolver.ResolutionError, match='not a JSON document'):
        declared('honua-mobile', b'{', sourcePinnedOnly=True)


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

    def json(self, path):
        if '/commits/' in path:
            return self.declarations.json(path)
        return super().json(path)


@pytest.mark.parametrize("missing_preview", [False, True])
def test_resolve_replaces_hand_version_maps_with_the_declarations(monkeypatch, missing_preview):
    path = resolver.COMPONENT_VERSIONS_PATH
    source = TreeAndDeclarations({
        ('honua-io/honua-console', NEW, path): declaration_bytes(
            'honua-console', contractVersions={'console-api': '2'}, schemaVersions={'workspace': '3'}),
        ('honua-io/honua-mobile', OLD, path): declaration_bytes(
            'honua-mobile', contractVersions={}, schemaVersions={}),
    })
    if missing_preview:
        source.declarations.files.pop(('honua-io/honua-mobile', OLD, path))
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
    for name in ('honua-console', 'honua-helm'):
        assert any(line.startswith(f'{name}: ') and 'release/component-versions.json is missing' in line
                   for line in lines), (name, lines)
    assert not any(line.startswith(('honua-server: ', 'honua-collect: ')) for line in lines)


# --- a trunk commit without a declaration is skipped, not refused (honua-release#471) ---

class DeclaredTrunk(Declarations):
    """A green, published trunk of NEW (newest) then OLD, with declarations only where given."""

    def commits(self, repository, limit):
        return iter([NEW, OLD][:limit])

    def green(self, name, repository, sha):
        return True, 'full CI green'


def walk_declared(files, limit=2):
    source = DeclaredTrunk(files)
    walk = {}
    selected = resolver.select_component(
        'geospatial-mcp', {'repository': 'https://github.com/honua-io/geospatial-mcp', 'sha': OLD},
        source, None, limit, walk,
        declare=lambda row: resolver.component_versions(source, 'geospatial-mcp', row))
    return selected, walk, source


def test_a_commit_without_a_declaration_is_skipped_and_the_walk_takes_the_next_one():
    path = resolver.COMPONENT_VERSIONS_PATH
    selected, walk, source = walk_declared(
        {('honua-io/geospatial-mcp', OLD, path): declaration_bytes('geospatial-mcp')})
    assert selected['sha'] == OLD
    assert walk['skipped'] == [{'sha': NEW, 'reason': f'no {path} at this commit'}]
    assert walk.get('aborted') is None
    assert ('honua-io/geospatial-mcp', NEW, path) in source.reads


def test_a_trunk_with_no_declared_commit_still_refuses_and_names_each_skip():
    with pytest.raises(resolver.ResolutionError,
                       match=r'geospatial-mcp: no qualifying trunk commit in newest 2 commits; '
                             rf'{NEW}: no release/component-versions.json at this commit; '
                             rf'{OLD}: no release/component-versions.json at this commit'):
        walk_declared({})


def test_an_invalid_declaration_stops_the_walk_instead_of_being_skipped():
    path = resolver.COMPONENT_VERSIONS_PATH
    with pytest.raises(resolver.ResolutionError, match='is not a JSON document'):
        walk_declared({('honua-io/geospatial-mcp', NEW, path): b'{',
                       ('honua-io/geospatial-mcp', OLD, path): declaration_bytes('geospatial-mcp')})


@pytest.mark.parametrize('detail,status', [('HTTP 403', 403), ('HTTP 500', 500), ('connection reset', None)])
def test_a_declaration_read_error_that_is_not_a_404_stops_the_walk(detail, status):
    class Unreadable(DeclaredTrunk):
        def file(self, *args):
            raise resolver.ResolutionError(detail, status=status)
    walk = {}
    with pytest.raises(resolver.ResolutionError, match='missing or unreadable') as refused:
        source = Unreadable({})
        resolver.select_component(
            'geospatial-mcp', {'repository': 'https://github.com/honua-io/geospatial-mcp', 'sha': OLD},
            source, None, 2, walk,
            declare=lambda row: resolver.component_versions(source, 'geospatial-mcp', row))
    assert not isinstance(refused.value, resolver.MissingDeclaration)
    assert walk['aborted']['sha'] == NEW and walk['skipped'] == []


def test_a_missing_declaration_at_the_pinned_sha_is_still_a_refusal():
    with pytest.raises(resolver.MissingDeclaration, match='is missing or unreadable'):
        resolver.component_versions(Declarations({}), 'geospatial-mcp', {
            'repository': 'https://github.com/honua-io/geospatial-mcp', 'sha': NEW})
    assert issubclass(resolver.MissingDeclaration, resolver.ResolutionError)


def test_without_a_declare_hook_the_walk_never_reads_a_declaration():
    selected, walk, source = DeclaredTrunk({}), {}, None
    selected = resolver.select_component(
        'geospatial-mcp', {'repository': 'https://github.com/honua-io/geospatial-mcp', 'sha': OLD},
        DeclaredTrunk({}), None, 2, walk)
    assert selected['sha'] == NEW and walk['skipped'] == []


def test_resolve_walks_trunk_components_with_the_declaration_hook(monkeypatch):
    seen = {}

    def capture(name, component, github, registry, limit, walk, declare=None):
        seen[name] = declare
        raise resolver.ResolutionError(f'{name}: stop after capture')

    monkeypatch.setattr(resolver, 'select_component', capture)
    monkeypatch.setattr(resolver, 'verify_manifest', lambda *a, **k: None)
    reads = []
    monkeypatch.setattr(resolver, 'component_versions',
                        lambda github, name, row: reads.append((name, row['sha'])) or {})
    with pytest.raises(resolver.ResolutionError, match='geospatial-mcp: stop after capture'):
        resolver.resolve({'components': {'geospatial-mcp': {
            'repository': 'https://github.com/honua-io/geospatial-mcp', 'sha': OLD}}}, {}, None, None)
    assert callable(seen['geospatial-mcp'])
    seen['geospatial-mcp']({'sha': NEW})
    assert reads == [('geospatial-mcp', NEW)]


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


# --- Published SDK identities (honua-release#231 WI-4) ---
RECORDED = resolver.ROOT / 'tools' / 'fixtures' / 'client-artifacts'


def recorded_pins():
    return json.loads((RECORDED / 'pins.json').read_text())


def replay_registry(monkeypatch):
    urls = json.loads((RECORDED / 'urls.json').read_text())
    responses = {url: (RECORDED / filename).read_bytes() for filename, url in urls.items()}
    # Project metadata is synthetic, not a captured PyPI response. See the fixture README.
    pin = recorded_pins()['python']
    metadata_url = f"https://pypi.org/pypi/{pin['package']}/{pin['version']}/json"
    assert metadata_url not in responses
    responses[metadata_url] = (RECORDED / 'pypi-metadata.synthetic.json').read_bytes()
    monkeypatch.setattr(verifier, '_request', lambda url, **kw: responses[url])


class PublishedSource(TreeAndDeclarations):
    def __init__(self, pins, red=False):
        files = {(pin['repository'], pin['sourceSha'], resolver.COMPONENT_VERSIONS_PATH):
                 declaration_bytes('honua-sdk-' + name)
                 for name, pin in pins.items()}
        files.update({(pin['repository'], pin['sourceSha'], resolver.SDK_BASELINE_PATH):
                      baseline_bytes('honua-sdk-' + name) for name, pin in pins.items()})
        super().__init__(files)
        self.checked, self.red = [], red

    def commits(self, repository, limit):
        assert repository == 'honua-io/honua-server', 'SDK must never select an unpublished trunk head'
        return iter([NEW])

    def green(self, name, repository, sha):
        self.checked.append((name, repository, sha))
        return (not self.red or name == 'honua-server'), 'required suite failed' if self.red else 'green'


def resolve_published(monkeypatch, pins=None, source=None):
    pins = recorded_pins() if pins is None else pins
    replay_registry(monkeypatch)
    source = source or PublishedSource(pins)
    components = {'honua-server': {'repository': 'https://github.com/honua-io/honua-server', 'sha': NEW}}
    for name, pin in pins.items():
        components['honua-sdk-' + name] = {
            'repository': 'https://github.com/' + pin['repository'],
            'artifact': pin['ecosystem'] + ':' + pin['package'], 'sha': NEW, 'version': '9.9.9',
            'artifactVersion': 'hand-version', 'artifactSourceRevision': NEW,
            'artifactSha256': 'sha256:' + 'f' * 64, 'contractVersions': {'hand': '9'},
        }
    manifest = {'components': components, 'clientArtifacts': pins,
                'platformRelease': '2026.1.0-rc.3', 'protocolCertification': {'ledger': {'status': 'bound'}}}
    monkeypatch.setattr(resolver.validate_platform, 'validate',
                        lambda *a, **kw: type('Findings', (), {'errors': []})())
    monkeypatch.setattr(resolver.validate_platform, 'check_legacy_evidence_pin_coherence', lambda *a: None)
    candidate, matrix = resolver.resolve(manifest, {}, source, None, limit=1)
    assert manifest['components']['honua-sdk-dotnet']['sha'] == NEW  # input is not mutated
    return candidate, matrix, source


def test_resolve_pins_every_sdk_to_verified_primary_package_and_reads_declarations_there(monkeypatch, tmp_path):
    import generate_platform_lock as generator
    import convergence_rebind
    candidate, matrix, source = resolve_published(monkeypatch)
    expected = {
        'dotnet': ('1.10.1', '8a0a06c815baefd49e7398d38a9f22642a8c80c5',
                   'sha256:65e096cdea4d6f2e35226ae3ed3d769fea5f19fc1c4d3612a6339e75a42a8bbd'),
        'js': ('0.1.12', '1102d2d55916340edca13cb28411df8da8206f92',
               'sha256:679e0873ae1347be0f7de33ae9876ac82ebd4cf1af6a160e8bcf1e8dc70a7b62'),
        'python': ('0.1.12', '12670676a1e8acb835e911c358adbf46a731120a',
                   'sha256:4ca00c6d585a7325ccb39e15c5e3e4e036e3d91b9bed3471cd5037e1e36efcf2'),
    }
    for name, (version, revision, digest) in expected.items():
        sdk = candidate['components']['honua-sdk-' + name]
        assert (sdk['sha'], sdk['artifactSourceRevision'], sdk['artifactVersion'], sdk['artifactSha256']) == (
            revision, revision, version, digest)
        assert sdk['version'] == sdk['artifactVersion'] == version
        assert sdk['contractVersions'] == {'admin': 'v1'}
        assert ('honua-sdk-' + name, 'honua-io/honua-sdk-' + name, revision) in source.checked
        assert ('honua-io/honua-sdk-' + name, revision, 'release/component-versions.json') in source.declarations.reads
    # Use the ledger's canonical client row names to prove S5 is removed.
    candidate['clientArtifacts'] = {dict(dotnet='honua-sdk-dotnet', js='honua-sdk-js',
                                       python='honua-sdk-python-wheel')[name]: pin
                                    for name, pin in candidate['clientArtifacts'].items()}
    convergence_rebind.require_published_sdk_pins(candidate)
    manifest_path, matrix_path = tmp_path / 'manifest.yaml', tmp_path / 'matrix.yaml'
    manifest_path.write_text(yaml.safe_dump(candidate))
    matrix_path.write_text(yaml.safe_dump(matrix))
    draft = generator.generate(manifest_path, matrix_path)
    for name, (version, revision, digest) in expected.items():
        sdk = draft.lock['components']['honua-sdk-' + name]
        assert sdk['source']['revision'] == revision
        artifact = sdk['artifacts'][0]
        assert artifact['version'] == version and artifact['sourceRevision'] == revision
        if name != 'js':
            assert artifact['sha256'] == digest
        else:
            assert artifact['integrity'] == 'sha512-oPdSohKcUVyFplQJ8znruPpjWK3Bbt23+voU6/v9bA7zy6PKCZvkNZMzklM05iT50MDLQQTeUL3qjM/CLVU4pQ=='
    assert not any('published identity conflicts' in row or 'package hash is not declared' in row
                   or 'registry provenance must bind' in row and 'honua-sdk-' in row
                   for row in draft.unresolved)
    assert any('serverCompatibility' in row for row in draft.unresolved)  # unrelated gates still refuse


def test_a_new_published_pin_advances_source_and_artifact_identity_together(monkeypatch):
    old_pins = recorded_pins()
    old_pins['dotnet'].update(version='1.10.0', filename='honua.sdk.1.10.0.nupkg',
        sourceSha='d81067a035854a1bc4c396ed763ba0de6b18864e',
        digest='sha256:dcc6bb0477e64982854f38ee704709abafae43e373d7f963af15b23c540859aa')
    older, _, _ = resolve_published(monkeypatch, old_pins)
    newer, _, _ = resolve_published(monkeypatch)
    old = older['components']['honua-sdk-dotnet']
    new = newer['components']['honua-sdk-dotnet']
    assert old['version'] == old['artifactVersion'] == '1.10.0'
    assert new['version'] == new['artifactVersion'] == '1.10.1'
    assert (old['sha'], old['artifactVersion'], old['artifactSha256']) == (
        'd81067a035854a1bc4c396ed763ba0de6b18864e', '1.10.0',
        'sha256:dcc6bb0477e64982854f38ee704709abafae43e373d7f963af15b23c540859aa')
    assert (new['sha'], new['artifactVersion'], new['artifactSha256']) == (
        '8a0a06c815baefd49e7398d38a9f22642a8c80c5', '1.10.1',
        'sha256:65e096cdea4d6f2e35226ae3ed3d769fea5f19fc1c4d3612a6339e75a42a8bbd')


def test_red_published_sdk_source_refuses_without_advancing_to_green_head(monkeypatch):
    pins = recorded_pins()
    with pytest.raises(resolver.ResolutionError, match='honua-sdk-dotnet: published source .*required suite failed'):
        resolve_published(monkeypatch, pins, PublishedSource(pins, red=True))


@pytest.mark.parametrize('change,message', [
    ('missing', 'exactly one'), ('duplicate', 'exactly one'), ('wrong-repository', 'repository'),
    ('optional', 'not verified'), ('unpublished', 'not published/promoted'),
    ('wrong-digest', 'manifest digest'),
])
def test_ambiguous_or_unverified_primary_package_refuses(monkeypatch, change, message):
    pins = recorded_pins()
    replay_registry(monkeypatch)
    component = {'repository': 'https://github.com/honua-io/honua-sdk-dotnet', 'artifact': 'nuget:Honua.Sdk'}
    if change == 'missing':
        del pins['dotnet']
    elif change == 'duplicate':
        pins['also-dotnet'] = copy.deepcopy(pins['dotnet'])
    elif change == 'wrong-repository':
        component['repository'] = 'https://github.com/other/repo'
    elif change == 'optional':
        pins['dotnet']['required'] = False
    elif change == 'unpublished':
        pins['dotnet']['publicationState'] = 'pending'
    elif change == 'wrong-digest':
        pins['dotnet']['digest'] = 'sha256:' + '0' * 64
    with pytest.raises(ValueError, match=message):
        identities = verifier.verify_manifest({'clientArtifacts': pins}, include_identities=True)
        resolver.select_sdk('honua-sdk-dotnet', component, pins, identities, PublishedSource(recorded_pins()))


def test_companion_publication_cannot_choose_the_sdk_source(monkeypatch):
    pins = recorded_pins()
    replay_registry(monkeypatch)
    identities = verifier.verify_manifest({'clientArtifacts': pins}, include_identities=True)
    # The companion's publication is distinct and already verified by the manifest verifier;
    # this selector only joins the primary coordinate, regardless of row ordering or naming.
    pins = {'companion': {'ecosystem': 'npm', 'package': '@honua/mcp-server',
                         'repository': 'honua-io/honua-sdk-js', 'sourceSha': NEW}, **pins}
    identities['companion'] = {'version': '0.1.12', 'sourceRevision': NEW, 'sha256': 'sha256:' + 'e' * 64}
    component = {'repository': 'https://github.com/honua-io/honua-sdk-js', 'artifact': 'npm:@honua/sdk-js'}
    selected = resolver.select_sdk('honua-sdk-js', component, pins, identities, PublishedSource(recorded_pins()))
    assert selected['sha'] == '1102d2d55916340edca13cb28411df8da8206f92'
    assert pins['companion']['sourceSha'] == NEW


# --- SDK capability baselines (honua-release#231 WI-5) ---
DOTNET_PUBLISHED = '8a0a06c815baefd49e7398d38a9f22642a8c80c5'
RECEIPT_EVIDENCE = {
    'uri': 'https://github.com/honua-io/honua-release/blob/0dd9b7a37ab4ee0dd02c17632e3de9e9eeeeddbd/'
           'certification/sources/server-publication-history.v1.json',
    'sha256': 'sha256:3069fde14a32cc579e4ee92cbe7e86bd88a14c1405df94457e1393c63fe092d1',
}
ADVERTISED = frozenset(row['key'] for row in json.loads(SERVER_CAPABILITY_KEYS)['capabilities'])
SDK_REQUIRED = ['discovery.capability-manifest', 'serve.geoservices-featureserver', 'serve.ogc-api-features']


def sdk_row(name, revision=DOTNET_PUBLISHED):
    return {'repository': f'https://github.com/honua-io/{name}', 'sha': revision, 'artifactSourceRevision': revision}


def read_baseline(name, raw=None, revision=DOTNET_PUBLISHED, advertised=ADVERTISED, artifacts=None, files=None):
    source = Declarations(files if files is not None else {
        (f'honua-io/{name}', revision, resolver.SDK_BASELINE_PATH): baseline_bytes(name) if raw is None else raw})
    return resolver.sdk_capability_baseline(source, name, sdk_row(name, revision), artifacts or {}, advertised), source


def edited(name, change):
    body = json.loads(baseline_bytes(name))
    change(body)
    return json.dumps(body).encode()


def test_the_dotnet_baseline_is_recorded_at_its_published_revision():
    compatibility, source = read_baseline('honua-sdk-dotnet')
    first_release = {'versionModel': 'semver', 'introductionModel': 'first-release', 'evidence': RECEIPT_EVIDENCE}
    assert compatibility == {
        'minimumServerVersion': 'first-release',
        'manifests': [{
            'source': {'repository': 'https://github.com/honua-io/honua-sdk-dotnet',
                       'revision': '8a0a06c815baefd49e7398d38a9f22642a8c80c5',
                       'path': 'release/sdk-capability-baseline.json'},
            'sha256': 'sha256:b0da605db407df1efd3ec14ca7b2b156b94cdfc7f6176de7f751cef167e4819f',
            'content': {
                'format': 'honua.sdk-capability-baseline/v1',
                'component': 'honua-sdk-dotnet',
                'minimumServerVersion': 'first-release',
                'requiredCapabilities': ['discovery.capability-manifest', 'serve.geoservices-featureserver',
                                         'serve.ogc-api-features'],
                'capabilities': {'discovery.capability-manifest': first_release,
                                 'serve.geoservices-featureserver': first_release,
                                 'serve.ogc-api-features': first_release},
            },
            'requiredCapabilities': ['discovery.capability-manifest', 'serve.geoservices-featureserver',
                                     'serve.ogc-api-features'],
        }],
        'declarations': [{'revision': '8a0a06c815baefd49e7398d38a9f22642a8c80c5',
                          'path': 'release/sdk-capability-baseline.json',
                          'sha256': 'sha256:525159b5140fb0d606f5be2db9b3751cb44d98ea6b05a4c9ce2edc0d0dd92378',
                          'minimumServerVersion': 'first-release'}],
    }
    assert source.reads == [('honua-io/honua-sdk-dotnet', DOTNET_PUBLISHED, 'release/sdk-capability-baseline.json')]


@pytest.mark.parametrize('name,required,content,raw', [
    ('honua-sdk-dotnet', SDK_REQUIRED,
     'sha256:b0da605db407df1efd3ec14ca7b2b156b94cdfc7f6176de7f751cef167e4819f',
     'sha256:525159b5140fb0d606f5be2db9b3751cb44d98ea6b05a4c9ce2edc0d0dd92378'),
    ('honua-sdk-js', SDK_REQUIRED,
     'sha256:5b03a44d6d38db9880f0e9ba87207c58a919fd818cefe5c66a08bbc53c6da486',
     'sha256:a84081b075e17759c09f602704fa462b39d5de4ce3fe1fc66d7b6e712483a325'),
    ('honua-sdk-python', SDK_REQUIRED,
     'sha256:8ab57cc64c1b4f71486e97c0a44b718d1dae84982414c09a944b4ac3bd02da38',
     'sha256:ae0711439652ac14c847e4ce6d80d692a0d7c92f0b4da6309e6e6032fe282252'),
    ('geospatial-mcp', ['ai.mcp-discovery', 'discovery.capability-manifest'],
     'sha256:6b28e7e6997f8eb69ec1e4e2f0e5c874697a0ea7ba9ea2ef3c29f5bb2888c825',
     'sha256:bd286450cb67d429eaf122eaf81aef5b5f015207bebeb4666194b5112b0311b9'),
])
def test_each_repository_baseline_resolves_to_its_literal_lock_entry(name, required, content, raw):
    compatibility, _ = read_baseline(name)
    manifest, = compatibility['manifests']
    declaration, = compatibility['declarations']
    assert compatibility['minimumServerVersion'] == declaration['minimumServerVersion'] == 'first-release'
    assert manifest['requiredCapabilities'] == manifest['content']['requiredCapabilities'] == required
    assert all(entry == {'versionModel': 'semver', 'introductionModel': 'first-release',
                         'evidence': RECEIPT_EVIDENCE} for entry in manifest['content']['capabilities'].values())
    assert (manifest['sha256'], declaration['sha256']) == (content, raw)
    assert manifest['source'] == {'repository': f'https://github.com/honua-io/{name}', 'revision': DOTNET_PUBLISHED,
                                  'path': 'release/sdk-capability-baseline.json'}


def test_a_capability_the_candidate_server_does_not_advertise_is_refused():
    with pytest.raises(resolver.ResolutionError,
                       match=r'^honua-sdk-python: .*the candidate honua-server does not advertise required '
                             r'capability serve\.ogc-api-features$'):
        read_baseline('honua-sdk-python', advertised=ADVERTISED - {'serve.ogc-api-features'})


def test_a_missing_baseline_refuses_the_sdk():
    with pytest.raises(resolver.ResolutionError,
                       match=rf'^honua-sdk-dotnet: honua-io/honua-sdk-dotnet@{DOTNET_PUBLISHED}:'
                             r'release/sdk-capability-baseline\.json is missing or unreadable: .*HTTP 404'):
        read_baseline('honua-sdk-dotnet', files={})


def _numeric(body, floors=('1.0.0', '1.2.0', '1.1.0'), top='1.2.0'):
    body['minimumServerVersion'] = top
    for key, floor in zip(body['requiredCapabilities'], floors):
        body['capabilities'][key] = {'versionModel': 'semver', 'minimumServerVersion': floor,
                                     'evidence': RECEIPT_EVIDENCE}


@pytest.mark.parametrize('raw,message', [
    (b'{"format": ', 'is not a JSON document'),
    (b'{"format": "honua.sdk-capability-baseline/v1", "format": "x"}', "duplicate key 'format'"),
    (b'[]', 'does not match'),
    (edited('honua-sdk-js', lambda b: b.update(format='honua.sdk-capability-baseline/v2')), 'format'),
    (edited('honua-sdk-js', lambda b: b.update(component='honua-sdk-python')),
     "declares component 'honua-sdk-python', not 'honua-sdk-js'"),
    (edited('honua-sdk-js', lambda b: b.update(extra=1)), 'Additional properties'),
    (edited('honua-sdk-js', lambda b: b.update(requiredCapabilities=[])), 'requiredCapabilities'),
    (edited('honua-sdk-js', lambda b: b.update(minimumServerVersion='latest')), 'minimumServerVersion'),
    (edited('honua-sdk-js', lambda b: b.update(minimumServerVersion='2026.3')), 'minimumServerVersion'),
    (edited('honua-sdk-js', lambda b: b['capabilities']['serve.ogc-api-features'].pop('evidence')),
     "'evidence' is a required property"),
    (edited('honua-sdk-js', lambda b: b['capabilities']['serve.ogc-api-features']['evidence'].update(
        uri='http://insecure')), 'evidence/uri'),
    (edited('honua-sdk-js', lambda b: b['capabilities']['serve.ogc-api-features']['evidence'].update(
        sha256='sha256:short')), 'evidence/sha256'),
    (edited('honua-sdk-js', lambda b: b['capabilities']['serve.ogc-api-features'].update(versionModel='calver')),
     'versionModel'),
    (edited('honua-sdk-js', lambda b: b['capabilities']['serve.ogc-api-features'].update(
        minimumServerVersion='1.0.0')), 'serve.ogc-api-features'),
    (edited('honua-sdk-js', lambda b: b['capabilities']['serve.ogc-api-features'].pop('introductionModel')),
     'serve.ogc-api-features'),
    (edited('honua-sdk-js', lambda b: b['capabilities'].pop('serve.ogc-api-features')),
     'one introduction per required capability'),
    (edited('honua-sdk-js', lambda b: b['requiredCapabilities'].remove('serve.ogc-api-features')),
     'one introduction per required capability'),
    (edited('honua-sdk-js', lambda b: b.update(minimumServerVersion='1.0.0')),
     "minimumServerVersion '1.0.0' is not the maximum of its required capabilities ('first-release')"),
    (edited('honua-sdk-js', lambda b: _numeric(b, top='1.1.0')),
     "minimumServerVersion '1.1.0' is not the maximum of its required capabilities ('1.2.0')"),
    (edited('honua-sdk-js', lambda b: _numeric(b, top='first-release')),
     "minimumServerVersion 'first-release' is not the maximum of its required capabilities ('1.2.0')"),
])
def test_an_invalid_baseline_refuses_the_sdk(raw, message):
    with pytest.raises(resolver.ResolutionError, match=r'^honua-sdk-js: .*' + re.escape(message)):
        read_baseline('honua-sdk-js', raw)


def test_a_numeric_baseline_records_its_maximum_floor():
    compatibility, _ = read_baseline('honua-sdk-js', edited('honua-sdk-js', _numeric))
    assert compatibility['minimumServerVersion'] == '1.2.0'
    assert compatibility['declarations'][0]['minimumServerVersion'] == '1.2.0'


@pytest.mark.parametrize('revision', ['trunk', 'pending', '', None])
def test_a_baseline_is_never_read_at_a_moving_or_missing_revision(revision):
    class Guard:
        def file(self, *a):
            raise AssertionError('baseline read without a published revision')

    row = {'repository': 'https://github.com/honua-io/honua-sdk-js', 'sha': NEW, 'artifactSourceRevision': revision}
    with pytest.raises(resolver.ResolutionError, match='honua-sdk-js: no published source revision'):
        resolver.sdk_capability_baseline(Guard(), 'honua-sdk-js', row, {}, ADVERTISED)


def companion_artifacts(revision):
    return {'honua-sdk-js': {'repository': 'honua-io/honua-sdk-js', 'package': '@honua/sdk-js',
                             'sourceSha': DOTNET_PUBLISHED},
            'honua-mcp-server': {'repository': 'honua-io/honua-sdk-js', 'package': '@honua/mcp-server',
                                 'sourceSha': revision},
            'honua-sdk-dotnet': {'repository': 'honua-io/honua-sdk-dotnet', 'sourceSha': OLD}}


def test_a_companion_package_revision_is_declared_too():
    path = resolver.SDK_BASELINE_PATH
    files = {('honua-io/honua-sdk-js', revision, path): baseline_bytes('honua-sdk-js')
             for revision in (DOTNET_PUBLISHED, NEW)}
    compatibility, source = read_baseline('honua-sdk-js', files=files, artifacts=companion_artifacts(NEW))
    assert [d['revision'] for d in compatibility['declarations']] == [DOTNET_PUBLISHED, NEW]
    assert [m['source']['revision'] for m in compatibility['manifests']] == [DOTNET_PUBLISHED, NEW]
    # Another repository's package never contributes a revision.
    assert all(read[0] == 'honua-io/honua-sdk-js' for read in source.reads)


def test_a_companion_revision_without_the_baseline_refuses():
    files = {('honua-io/honua-sdk-js', DOTNET_PUBLISHED, resolver.SDK_BASELINE_PATH): baseline_bytes('honua-sdk-js')}
    with pytest.raises(resolver.ResolutionError, match=rf'^honua-sdk-js: honua-io/honua-sdk-js@{NEW}:.*missing'):
        read_baseline('honua-sdk-js', files=files, artifacts=companion_artifacts(NEW))


def test_an_optional_companion_row_contributes_no_revision():
    """verify_manifest skips `required: false` rows, so their sourceSha is not a published revision."""
    files = {('honua-io/honua-sdk-js', DOTNET_PUBLISHED, resolver.SDK_BASELINE_PATH): baseline_bytes('honua-sdk-js')}
    artifacts = companion_artifacts(NEW)
    artifacts['honua-mcp-server'].update(required=False, publicationState='unpublished')
    compatibility, source = read_baseline('honua-sdk-js', files=files, artifacts=artifacts)
    assert [d['revision'] for d in compatibility['declarations']] == [DOTNET_PUBLISHED]
    assert all(read[1] != NEW for read in source.reads)


def test_published_revisions_that_declare_different_floors_refuse():
    path = resolver.SDK_BASELINE_PATH
    files = {('honua-io/honua-sdk-js', DOTNET_PUBLISHED, path): baseline_bytes('honua-sdk-js'),
             ('honua-io/honua-sdk-js', NEW, path): edited('honua-sdk-js', _numeric)}
    with pytest.raises(resolver.ResolutionError, match="declare different minimumServerVersion values"):
        read_baseline('honua-sdk-js', files=files, artifacts=companion_artifacts(NEW))


def test_resolve_records_every_sdk_baseline_at_its_published_revision(monkeypatch, tmp_path):
    import generate_platform_lock as generator
    pins = recorded_pins()
    candidate, matrix, source = resolve_published(monkeypatch, pins)
    for name, pin in pins.items():
        sdk = candidate['components']['honua-sdk-' + name]
        declaration, = sdk['serverCompatibility']['declarations']
        assert declaration['revision'] == pin['sourceSha'] == sdk['artifactSourceRevision']
        assert declaration['sha256'] == 'sha256:' + hashlib.sha256(baseline_bytes('honua-sdk-' + name)).hexdigest()
        assert (pin['repository'], pin['sourceSha'], resolver.SDK_BASELINE_PATH) in source.declarations.reads
    # The advertised vocabulary is the selected server's (MigrationSource asserts the sha).
    assert resolver.SERVER_CAPABILITY_KEYS[1] in source.reads
    manifest_path, matrix_path = tmp_path / 'manifest.yaml', tmp_path / 'matrix.yaml'
    manifest_path.write_text(yaml.safe_dump(candidate))
    matrix_path.write_text(yaml.safe_dump(matrix))
    rows = [row for row in generator.generate(manifest_path, matrix_path).unresolved if 'serverCompatibility' in row]
    # The manifest is pinned now; what remains is the first-release lock fact, not a missing manifest.
    assert len(rows) == 3 and not any('no consumed protocol/capability manifest' in row for row in rows)
    assert all('publication-history receipt' in row for row in rows), rows


def test_resolve_refuses_an_sdk_without_a_baseline_and_keeps_no_hand_value(monkeypatch):
    pins = recorded_pins()
    source = PublishedSource(pins)
    del source.declarations.files[(pins['python']['repository'], pins['python']['sourceSha'],
                                   resolver.SDK_BASELINE_PATH)]
    seen = {}

    def capture(candidate, *a, **k):
        seen.update(candidate['components'])
        return type('Findings', (), {'errors': []})()

    replay_registry(monkeypatch)
    components = {'honua-server': {'repository': 'https://github.com/honua-io/honua-server', 'sha': NEW}}
    for name, pin in pins.items():
        components['honua-sdk-' + name] = {
            'repository': 'https://github.com/' + pin['repository'], 'artifact': pin['ecosystem'] + ':' + pin['package'],
            'sha': NEW, 'serverCompatibility': {'minimumServerVersion': '1.0.0', 'manifests': [], 'declarations': []}}
    monkeypatch.setattr(resolver.validate_platform, 'validate', capture)
    with pytest.raises(resolver.ResolutionError) as refused:
        resolver.resolve({'components': components, 'clientArtifacts': pins,
                          'protocolCertification': {'ledger': {'status': 'bound'}}}, {}, source, None, limit=1)
    lines = str(refused.value).splitlines()
    assert any(line.startswith('honua-sdk-python: ') and 'sdk-capability-baseline.json is missing' in line
               for line in lines), lines
    assert not any(line.startswith(('honua-sdk-js: ', 'honua-sdk-dotnet: ')) for line in lines), lines
    assert 'serverCompatibility' not in seen['honua-sdk-python']
    assert seen['honua-sdk-js']['serverCompatibility']['minimumServerVersion'] == 'first-release'


def test_an_unadvertised_capability_refuses_the_night(monkeypatch):
    pins = recorded_pins()

    class Narrow(PublishedSource):
        def file(self, repository, revision, path):
            if path == resolver.SERVER_CAPABILITY_KEYS[1]:
                return json.dumps({'capabilities': [{'key': 'serve.wms'}]}).encode()
            return super().file(repository, revision, path)

    with pytest.raises(resolver.ResolutionError) as refused:
        resolve_published(monkeypatch, pins, Narrow(pins))
    for name in pins:
        assert f'honua-sdk-{name}: ' in str(refused.value)
    assert 'does not advertise required capabilities discovery.capability-manifest, ' \
           'serve.geoservices-featureserver, serve.ogc-api-features' in str(refused.value)


@pytest.mark.parametrize('body,message', [
    (b'{"capabilities": []}', 'has no capabilities list'),
    (b'{"capabilities": [{"displayName": "x"}]}', 'without a key'),
    (b'{', 'is not a JSON document'),
])
def test_an_unreadable_server_vocabulary_refuses(body, message):
    class Server:
        def file(self, repository, revision, path):
            return body

    candidate = {'components': {'honua-server': {'repository': 'https://github.com/honua-io/honua-server', 'sha': NEW}}}
    with pytest.raises(resolver.ResolutionError, match=message):
        resolver.advertised_capabilities(Server(), candidate)


def test_green_passes_advisory_and_accepts_the_server_fixture_sha():
    """c19f29d is green only when the resolver hands the advisory list to the evaluator."""
    captured = json.loads((
        resolver.ROOT / 'tools' / 'fixtures' / 'candidate-resolution-2026-10-06' / 'honua-server-c19f29d.json'
    ).read_text())
    seen = {}
    original = resolver.ci.evaluate

    def evaluate(*args, **kwargs):
        seen['advisory'] = kwargs.get('advisory')
        return original(*args, **kwargs)

    github = resolver.GitHub()

    def pages(path, key=None):
        assert captured['sha'] in path
        if key == 'check_runs':
            return iter(captured['check_runs'])
        if key == 'workflow_runs':
            return iter(captured['workflow_runs'])
        raise AssertionError(path)

    github.pages = pages
    resolver.ci.evaluate = evaluate
    try:
        ok, why = github.green('honua-server', 'honua-io/honua-server', captured['sha'])
    finally:
        resolver.ci.evaluate = original
    assert ok, why
    assert 'full-matrix run 37435142851 completed successfully' in why
    workflows = seen['advisory']['honua-server']['workflows']
    assert '.github/workflows/server-test-prebuild-observe.yml' in workflows
    assert '.github/workflows/ci.yml' not in workflows


def test_the_documented_baseline_is_the_dotnet_fixture_and_resolves():
    doc = (resolver.ROOT / 'docs' / 'SDK-SERVER-BASELINE-RULE.md').read_text(encoding='utf-8')
    section = doc.split('## The SDK capability baseline file', 1)[1]
    example = re.search(r'```json\n(.*?)```', section, re.S).group(1)
    assert json.loads(example) == json.loads(baseline_bytes('honua-sdk-dotnet'))
    compatibility, _ = read_baseline('honua-sdk-dotnet', example.encode())
    assert compatibility['minimumServerVersion'] == 'first-release'


def test_the_nightly_uploads_the_skip_report_even_when_resolution_refuses():
    workflow = yaml.safe_load((resolver.ROOT / '.github/workflows/nightly-certification.yml').read_text())
    steps = workflow['jobs']['resolve']['steps']
    upload = next(step for step in steps if step.get('with', {}).get('name') == 'resolved-candidate-skips')
    assert upload['if'] == 'always()'
    assert upload['with']['path'] == 'resolved-candidate/skips.json'
    resolve = next(step for step in steps if step.get('name') == 'Resolve the newest qualifying trunk candidate')
    assert '--out-dir resolved-candidate' in resolve['run']


@pytest.mark.parametrize('stdout,stderr,status', [
    ('{"message": "Not Found", "status": "404"}', 'gh: Not Found (HTTP 404)', 404),
    ('', 'gh: Not Found (HTTP 404)', 404),
    (b'{"message": "Server Error", "status": "502"}', b'', 502),
    ('', 'error connecting to api.github.com', None)])
def test_gh_failures_carry_the_api_status(stdout, stderr, status):
    exc = subprocess.CalledProcessError(1, ['gh', 'api'], output=stdout, stderr=stderr)
    assert resolver.api_status(exc) == status
