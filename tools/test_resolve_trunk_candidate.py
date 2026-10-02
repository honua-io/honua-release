import copy
import hashlib
import json
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
