"""A pinned image component stays on its published digest instead of walking trunk."""

import pytest

import resolve_trunk_candidate as resolver

# honua-console publishes no container since the static SPA ruling: the night keeps its pin.
PIN = 'c' * 40
PINNED_DIGEST = 'sha256:' + '5' * 64


def pinned_console(**overrides):
    row = {'repository': 'https://github.com/honua-io/honua-console', 'trunkSelection': 'pinned',
           'image': 'ghcr.io/honua-io/honua-console:nightly-cccccccccccc', 'digest': PINNED_DIGEST,
           'sha': PIN, 'artifactSourceRevision': PIN, 'architectures': ['amd64'],
           'platformDigests': {'amd64': 'sha256:' + '6' * 64}}
    row.update(overrides)
    return row


class PinnedRegistry:
    def __init__(self, digest=PINNED_DIGEST, platform='sha256:' + '6' * 64):
        self.digest, self.platform, self.calls = digest, platform, []

    def image(self, name, component, sha):
        self.calls.append(sha)
        return {'image': 'ghcr.io/honua-io/honua-console@' + self.digest, 'digest': self.digest,
                'platformDigests': {'amd64': self.platform}, 'artifactSourceRevision': sha}


class NoTrunkGitHub:
    def __init__(self, green=True):
        self.is_green = green

    def commits(self, repository, limit):
        raise AssertionError('a pinned image must not walk trunk')

    def green(self, name, repository, sha):
        return (True, 'full CI green') if self.is_green else (False, 'Validate Console failure')


def test_a_pinned_image_keeps_its_published_digest_and_never_walks_trunk():
    registry = PinnedRegistry()
    selected = resolver.select_pinned_image('honua-console', pinned_console(), NoTrunkGitHub(), registry)
    assert registry.calls == [PIN]
    assert selected['sha'] == PIN
    assert selected['digest'] == PINNED_DIGEST
    assert selected['image'] == 'ghcr.io/honua-io/honua-console@' + PINNED_DIGEST


@pytest.mark.parametrize('github,registry,row,reason', [
    (NoTrunkGitHub(), PinnedRegistry(digest='sha256:' + '7' * 64), pinned_console(), 'is not the pinned'),
    (NoTrunkGitHub(), PinnedRegistry(platform='sha256:' + '8' * 64), pinned_console(), 'platform digests differ'),
    (NoTrunkGitHub(green=False), PinnedRegistry(), pinned_console(), 'CI Validate Console failure'),
    (NoTrunkGitHub(), PinnedRegistry(), pinned_console(sha='trunk'), 'immutable commit'),
    (NoTrunkGitHub(), PinnedRegistry(), pinned_console(artifactSourceRevision='9' * 40), 'artifactSourceRevision'),
    (NoTrunkGitHub(), None, pinned_console(), 'no registry'),
])
def test_a_pinned_image_that_is_not_its_green_published_digest_is_refused(github, registry, row, reason):
    with pytest.raises(resolver.ResolutionError, match=f'honua-console: .*{reason}'):
        resolver.select_pinned_image('honua-console', row, github, registry)


def test_only_an_image_marked_pinned_skips_the_trunk_walk():
    assert resolver.pinned_image(pinned_console())
    assert not resolver.pinned_image({k: v for k, v in pinned_console().items() if k != 'trunkSelection'})
    assert not resolver.pinned_image(pinned_console(image=None))
    assert not resolver.pinned_image(pinned_console(trunkSelection='trunk'))


def test_an_unknown_trunk_selection_is_refused_not_walked():
    with pytest.raises(resolver.ResolutionError, match='trunkSelection must be'):
        resolver.pinned_image(pinned_console(trunkSelection='Pinned'))
