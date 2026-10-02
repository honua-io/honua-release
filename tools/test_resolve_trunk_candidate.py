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
    def image(self, name, component, sha):
        if sha == NEW:
            raise resolver.ResolutionError('no published SHA-bound image')
        return {'image': 'ghcr.io/honua-io/server@sha256:' + 'c' * 64,
                'digest': 'sha256:' + 'c' * 64,
                'platformDigests': {'amd64': 'sha256:' + 'd' * 64},
                'artifactSourceRevision': sha}


def component():
    return {'repository': 'https://github.com/honua-io/server', 'image': 'ghcr.io/honua-io/server:old',
            'sha': 'e' * 40, 'architectures': ['amd64']}


def test_green_commit_without_published_image_is_refused():
    with pytest.raises(resolver.ResolutionError, match='server: no qualifying.*no published SHA-bound image'):
        resolver.select_component('server', component(), GitHub(), Registry(), 1)


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


def test_red_ci_never_queries_registry():
    class Red(GitHub):
        def green(self, *_):
            return False, 'required suite failed'
    with pytest.raises(resolver.ResolutionError, match='required suite failed'):
        resolver.select_component('server', component(), Red(), None, 1)


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


def test_registry_reads_index_and_children_from_actual_bytes(monkeypatch):
    registry, index, child = registry_fixture()
    monkeypatch.setattr(subprocess, 'run', lambda *_a, **_k: subprocess.CompletedProcess([], 0))
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
