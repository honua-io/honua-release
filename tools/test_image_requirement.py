"""The published image variant is proved from image config labels (tools/image_requirement.py)."""
from pathlib import Path

import pytest
import yaml

import resolve_trunk_candidate as resolver
from test_resolve_trunk_candidate import GitHub, NEW, OLD, Registry, _add_document

AOT_RULE = {'required': {'honua.runtime.compilation': 'native-aot'},
            'whenDeclared': {'honua.store.profile': 'postgis'}}
AOT, JIT = {'honua.runtime.compilation': 'native-aot'}, {'honua.runtime.compilation': 'jit'}
POSTGIS, FULL = {'honua.store.profile': 'postgis'}, {'honua.store.profile': 'full'}


def _variant_registry(published, sha=NEW):
    """`published` maps a tag to the config labels of each architecture it serves."""
    documents, indexes = {}, {}
    for tag, platforms in published.items():
        children = []
        for arch, labels in platforms.items():
            config = _add_document(documents, {'architecture': arch, 'os': 'linux', 'config': {
                'Labels': {'org.opencontainers.image.revision': sha, **labels}}})
            child = _add_document(documents, {'config': {'digest': config}, 'variant': f'{tag}-{arch}'})
            children.append({'digest': child, 'platform': {'architecture': arch, 'os': 'linux'}})
        indexes[tag] = _add_document(documents, {'manifests': children})
        indexes[tag + '/amd64'] = children[0]['digest']
    registry = resolver.Registry(None)

    def request(repository, suffix, **_):
        target = suffix.split('/')[-1]
        digest = indexes.get(target, target)
        return documents[digest], {'Docker-Content-Digest': digest}

    registry.request = request
    registry.tags = lambda repository: list(published)
    return registry, indexes


def _server(rule=AOT_RULE):
    component = {'image': 'ghcr.io/honua-io/honua-server:old', 'architectures': ['amd64', 'arm64'], 'sha': OLD}
    return component if rule is None else {**component, 'imageLabels': rule}


def test_the_manifest_requires_a_native_aot_postgis_server_image():
    manifest = Path(__file__).resolve().parents[1] / 'platform-manifest.yaml'
    server = yaml.safe_load(manifest.read_text(encoding='utf-8'))['components']['honua-server']
    assert server['imageLabels'] == AOT_RULE


def test_the_variant_is_read_from_the_image_not_from_tag_order():
    tags = {f'nightly-{NEW[:7]}': {'amd64': {**JIT, **POSTGIS}, 'arm64': {**JIT, **POSTGIS}},
            f'nightly-aot-{NEW[:7]}': {'amd64': {**AOT, **POSTGIS}, 'arm64': {**AOT, **POSTGIS}}}
    registry, indexes = _variant_registry(tags)
    assert registry.candidate_tags('honua-io/honua-server', NEW)[0] == f'nightly-{NEW[:7]}'
    result = registry.image('honua-server', _server(), NEW)
    assert result['digest'] == indexes[f'nightly-aot-{NEW[:7]}']
    assert result['image'] == 'ghcr.io/honua-io/honua-server@' + result['digest']


@pytest.mark.parametrize('labels,message', [
    ({'amd64': JIT, 'arm64': JIT}, r'linux/amd64 image declares honua\.runtime\.compilation=jit'),
    ({'amd64': AOT, 'arm64': JIT}, r'linux/arm64 image declares honua\.runtime\.compilation=jit'),
    ({'amd64': {}, 'arm64': {}}, r'linux/amd64 image declares no honua\.runtime\.compilation'),
    ({'amd64': {**AOT, **FULL}, 'arm64': {**AOT, **FULL}}, r'linux/amd64 image declares honua\.store\.profile=full'),
])
def test_an_image_that_breaks_the_declared_labels_is_never_pinned(labels, message):
    registry, _ = _variant_registry({f'nightly-{NEW[:7]}': labels})
    with pytest.raises(resolver.ResolutionError, match=message):
        registry.image('honua-server', _server(), NEW)


def test_an_image_built_before_the_store_split_declares_no_profile_and_still_binds():
    registry, indexes = _variant_registry({f'nightly-{NEW[:7]}': {'amd64': AOT, 'arm64': AOT}})
    assert registry.image('honua-server', _server(), NEW)['digest'] == indexes[f'nightly-{NEW[:7]}']


def test_a_lambda_image_of_the_wrong_variant_is_never_pinned():
    lambda_tag = f'nightly-lambda-aot-{NEW[:7]}-amd64'
    registry, _ = _variant_registry({f'nightly-{NEW[:7]}': {'amd64': AOT}, lambda_tag: {'amd64': JIT}})
    component = {**_server(), 'architectures': ['amd64'], 'awsLambdaImage': 'ghcr.io/honua-io/honua-server:old-lambda'}
    with pytest.raises(resolver.ResolutionError, match=r'Lambda image declares honua\.runtime\.compilation=jit'):
        registry.image('honua-server', component, NEW)


def test_a_lambda_image_without_a_store_profile_label_is_pinned():
    lambda_tag = f'nightly-lambda-aot-{NEW[:7]}-amd64'
    registry, images = _variant_registry({f'nightly-{NEW[:7]}': {'amd64': {**AOT, **POSTGIS}}, lambda_tag: {'amd64': AOT}})
    component = {**_server(), 'architectures': ['amd64'], 'awsLambdaImage': 'ghcr.io/honua-io/honua-server:old-lambda'}
    result = registry.image('honua-server', component, NEW)
    assert result['awsLambdaDigest'] == images[lambda_tag + '/amd64']
    assert result['digest'] == images[f'nightly-{NEW[:7]}']


def test_the_manifest_validator_reports_a_rule_the_resolver_would_refuse():
    import validate_platform

    findings = validate_platform.Findings()
    overlap = {'required': {'honua.store.profile': 'postgis'}, 'whenDeclared': {'honua.store.profile': 'postgis'}}
    validate_platform.check_image_label_rules(
        {'components': {'honua-server': {'imageLabels': overlap}, 'honua-console': {}, 'unset': None}}, findings)
    assert findings.errors == ["manifest: component 'honua-server': imageLabels names a label under both "
                               "required and whenDeclared"]


def test_a_component_without_a_label_rule_binds_by_revision_alone():
    registry, indexes = _variant_registry({f'nightly-{NEW[:7]}': {'amd64': JIT, 'arm64': JIT}})
    assert registry.image('honua-console', _server(rule=None), NEW)['digest'] == indexes[f'nightly-{NEW[:7]}']


@pytest.mark.parametrize('rule', [
    {}, {'required': {}}, {'required': {'a': 1}}, {'whenDeclared': {'a': 'b'}, 'unknown': {}},
    {'required': ['a']}, 'native-aot', {'required': {'a': 'b'}, 'whenDeclared': {'a': 'c'}},
])
def test_a_malformed_label_rule_refuses_instead_of_binding_any_image(rule):
    registry, _ = _variant_registry({f'nightly-{NEW[:7]}': {'amd64': AOT, 'arm64': AOT}})
    with pytest.raises(ValueError, match='imageLabels'):
        registry.image('honua-server', _server(rule), NEW)


def test_a_malformed_label_rule_refuses_before_the_trunk_walk():
    class Unlisted(GitHub):
        def commits(self, repository, limit):
            raise AssertionError('the walk must not start')

    component = {**_server({'required': {}}), 'repository': 'https://github.com/honua-io/honua-server'}
    with pytest.raises(ValueError, match='imageLabels'):
        resolver.select_component('honua-server', component, Unlisted(), Registry(), 10, {})


def test_a_jit_only_commit_is_skipped_by_name_and_an_older_aot_commit_is_selected():
    class Walk(GitHub):
        def commits(self, repository, limit):
            return [NEW, OLD]

    new, _ = _variant_registry({f'nightly-{NEW[:7]}': {'amd64': JIT}}, NEW)
    old, indexes = _variant_registry({f'nightly-aot-{OLD[:7]}': {'amd64': AOT}}, OLD)

    class Split:
        def candidate_tags(self, repository, sha):
            return (new if sha == NEW else old).candidate_tags(repository, sha)

        def image(self, name, component, sha):
            return (new if sha == NEW else old).image(name, component, sha)

    skips = {}
    component = {**_server(), 'architectures': ['amd64'], 'repository': 'https://github.com/honua-io/honua-server'}
    selected = resolver.select_component('honua-server', component, Walk(), Split(), 10, skips)
    assert (selected['sha'], selected['digest']) == (OLD, indexes[f'nightly-aot-{OLD[:7]}'])
    assert selected['imageLabels'] == AOT_RULE
    assert [row['sha'] for row in skips['skipped']] == [NEW]
    assert 'honua.runtime.compilation=jit' in skips['skipped'][0]['reason']


def _prior_for(published_by_sha, monkeypatch):
    """The Compose upgrade prior resolved from live-shaped trunk and registry fakes."""
    import compose_upgrade_prior as upgrade

    candidate_sha = 'c' * 40
    registries = {sha: _variant_registry(tags, sha) for sha, tags in published_by_sha.items()}

    class Trunk:
        def commits(self, repository, limit):
            return [candidate_sha, *published_by_sha]

        def green(self, name, repository, sha):
            return True, ''

    class Published:
        def candidate_tags(self, repository, sha):
            return registries[sha][0].candidate_tags(repository, sha)

        def first_admitted(self, repository, tags, requirement):
            return registries[requirement.sha][0].first_admitted(repository, tags, requirement)

    monkeypatch.setattr(resolver, 'GitHub', Trunk)
    monkeypatch.setattr(resolver, 'Registry', lambda github: Published())
    manifest = {'components': {'honua-server': {
        'sha': candidate_sha, 'image': 'ghcr.io/honua-io/honua-server:nightly-ccccccc', 'dbSchema': '136',
        'digest': 'sha256:' + 'c' * 64, 'platformDigests': {'amd64': 'sha256:' + 'd' * 64},
        'imageLabels': AOT_RULE}}}
    prior = upgrade.resolve(manifest, yaml.safe_dump(manifest).encode(), releases=[], nightly_locks=[],
                            certified_nightlies=upgrade.certified_nightlies_from_github())
    return prior, {sha: indexes for sha, (_, indexes) in registries.items()}


def test_the_upgrade_prior_skips_a_nightly_of_another_variant(monkeypatch):
    prior, images = _prior_for({NEW: {f'nightly-{NEW[:7]}': {'amd64': JIT}},
                                OLD: {f'nightly-{OLD[:7]}': {'amd64': AOT}}}, monkeypatch)
    assert prior['prior_source_revision'] == OLD
    assert prior['prior'] == 'ghcr.io/honua-io/honua-server@' + images[OLD][f'nightly-{OLD[:7]}/amd64']
    assert 'image not admitted' in prior['skipped'] and 'honua.runtime.compilation=jit' in prior['skipped']


def test_the_upgrade_prior_takes_the_aot_tag_of_a_commit_whose_default_tag_is_jit(monkeypatch):
    prior, images = _prior_for({NEW: {f'nightly-{NEW[:7]}': {'amd64': JIT}, f'nightly-aot-{NEW[:7]}': {'amd64': AOT}},
                                OLD: {f'nightly-{OLD[:7]}': {'amd64': AOT}}}, monkeypatch)
    assert prior['prior_source_revision'] == NEW
    assert prior['prior'] == 'ghcr.io/honua-io/honua-server@' + images[NEW][f'nightly-aot-{NEW[:7]}/amd64']
    assert prior['prior_selected_from'].startswith(f'nightly-aot-{NEW[:7]} ')


def test_a_malformed_rule_refuses_the_upgrade_prior_before_the_trunk_walk(monkeypatch):
    import compose_upgrade_prior as upgrade

    class Unlisted:
        def commits(self, repository, limit):
            raise AssertionError('the walk must not start')

    monkeypatch.setattr(resolver, 'GitHub', Unlisted)
    observe = upgrade.certified_nightlies_from_github()
    with pytest.raises(ValueError, match='imageLabels'):
        list(observe({'sourceRevision': 'c' * 40, 'imageLabels': {'required': {}}}))
