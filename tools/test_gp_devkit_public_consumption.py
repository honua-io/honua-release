import hashlib
import io

import pytest

import gp_devkit_artifacts as gate
import gp_devkit_public_consumption as public


URL = "https://github.com/honua-io/honua-server/releases/download/gp-devkit-v1.0.0/Honua.Geoprocessing.Cli.1.0.0.nupkg"
IMAGE = "ghcr.io/honua-io/honua-gp-workspace@sha256:" + "a" * 64
CONFIG = "sha256:" + "b" * 64


@pytest.mark.parametrize("url", [
    URL.replace("https:", "http:"),
    URL.replace("github.com", "github.com.evil.example"),
    URL.replace("github.com", "user:password@github.com"),
    URL.replace("honua-server", "another-repository"),
    URL + "?token=secret",
    URL + "#fragment",
    "https://release-assets.githubusercontent.com/asset.nupkg",
])
def test_initial_download_refuses_noncanonical_or_credential_urls(url):
    with pytest.raises(gate.ArtifactError):
        public.validate_asset_url(url)


def test_signed_github_asset_redirect_is_allowed_but_unrelated_hosts_are_not():
    assert public.validate_asset_url("https://release-assets.githubusercontent.com/path?signature=opaque", redirect=True)
    with pytest.raises(gate.ArtifactError):
        public.validate_asset_url("https://example.com/path?signature=opaque", redirect=True)


@pytest.mark.parametrize("changed", [True, False])
def test_anonymous_download_checks_bytes_before_publishing_file(tmp_path, monkeypatch, changed):
    content = b"qualified package bytes"

    class Response(io.BytesIO):
        status = 200

    class Opener:
        def open(self, url, timeout):
            assert url == URL
            return Response(content)

    monkeypatch.setattr(public, "build_opener", lambda *args: Opener())
    target = tmp_path / "package.nupkg"
    digest = hashlib.sha256(b"different" if changed else content).hexdigest()
    if changed:
        with pytest.raises(gate.ArtifactError, match="differs from the qualified package"):
            public.download_package(URL, target, digest)
        assert not target.exists()
    else:
        assert public.download_package(URL, target, digest) == len(content)
        assert target.read_bytes() == content
    assert not target.with_suffix(".partial").exists()


@pytest.mark.parametrize("change", [
    {"Id": "sha256:" + "c" * 64},
    {"Architecture": "arm64"},
    {"Os": "windows"},
    {"RepoDigests": []},
])
def test_anonymous_pull_cannot_substitute_another_image_or_platform(change):
    release = {"imageConfigDigest": CONFIG, "sandboxImage": IMAGE}
    actual = {"Id": CONFIG, "Os": "linux", "Architecture": "amd64", "RepoDigests": [IMAGE]}
    public.require_image_identity(actual, release)
    with pytest.raises(gate.ArtifactError, match="differs from the qualified"):
        public.require_image_identity(actual | change, release)
