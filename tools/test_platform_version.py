from __future__ import annotations

import copy

import pytest

from platform_version import IMAGED_COMPONENTS, artifact_version, stamp_platform_version, unbound_identity

REVISION = "a" * 40
DIGEST = "sha256:" + "b" * 64
AMD64, ARM64 = "sha256:" + "c" * 64, "sha256:" + "d" * 64


@pytest.mark.parametrize("label,version", [
    ("2026.1-rc.3", "2026.1.0-rc.3"),
    ("2026.1-rc.12", "2026.1.0-rc.12"),
    ("honua-2026.1-rc.3", "2026.1.0-rc.3"),
    ("2026.1.2-rc.1", "2026.1.2-rc.1"),
    ("honua-2026.1.2-rc.1", "2026.1.2-rc.1"),
    # GA: the promoted label and the bare calendar label both name the .0 release.
    ("2026.1.0", "2026.1.0"),
    ("honua-2026.1.0", "2026.1.0"),
    ("2026.1", "2026.1.0"),
    ("2026.1.2", "2026.1.2"),
])
def test_label_maps_to_the_r22_artifact_version(label, version):
    assert artifact_version(label) == version


@pytest.mark.parametrize("label", [
    "", None, "pre-release", "2026.1-rc", "2026.1-beta.1", "2026.1.1.0", "v2026.1-rc.3",
    "2026-rc.3", "nightly-87966c3", "2026.1-rc.3 ", "honua-2026.1-rc.3-aot",
])
def test_anything_but_a_platform_label_is_refused(label):
    with pytest.raises(ValueError, match="not a platform label"):
        artifact_version(label)


def image(**overrides):
    row = {"repository": "https://github.com/honua-io/honua-server", "sha": REVISION,
           "version": "pre-release", "image": "ghcr.io/honua-io/honua-server:nightly-aaaaaaa",
           "digest": DIGEST, "artifactSourceRevision": REVISION,
           "architectures": ["amd64", "arm64"], "platformDigests": {"amd64": AMD64, "arm64": ARM64}}
    row.update(overrides)
    return {key: value for key, value in row.items() if value is not None}


def chart(**overrides):
    row = {"repository": "https://github.com/honua-io/honua-helm", "sha": REVISION,
           "version": "pre-release", "artifact": "oci-chart:honua", "digest": DIGEST,
           "artifactSourceRevision": REVISION, "artifactSha256": AMD64}
    row.update(overrides)
    return {key: value for key, value in row.items() if value is not None}


def test_bound_identities_are_complete():
    assert unbound_identity(image()) == []
    assert unbound_identity(chart()) == []


@pytest.mark.parametrize("row,missing", [
    (image(digest=None), ["digest"]),
    (image(digest="sha256:abcd"), ["digest"]),
    (image(artifactSourceRevision=None), ["artifactSourceRevision"]),
    (image(platformDigests=None), ["platformDigests"]),
    (image(platformDigests={}), ["platformDigests"]),
    (image(platformDigests={"amd64": "nightly"}), ["platformDigests"]),
    (image(digest=None, artifactSourceRevision=None, platformDigests=None),
     ["digest", "artifactSourceRevision", "platformDigests"]),
    (chart(artifactSha256=None), ["artifactSha256"]),
    (chart(digest=None, artifactSourceRevision=None), ["digest", "artifactSourceRevision"]),
])
def test_unbound_identity_names_every_missing_fact(row, missing):
    assert unbound_identity(row) == missing


def manifest():
    return {"platformRelease": "2026.1-rc.3", "components": {
        "honua-server": image(),
        "honua-console": image(repository="https://github.com/honua-io/honua-console",
                               image="ghcr.io/honua-io/honua-console:candidate-x"),
        "honua-helm": chart(),
        "honua-sdk-js": {"repository": "https://github.com/honua-io/honua-sdk-js", "sha": REVISION,
                         "version": "0.1.12", "artifactVersion": "0.1.12", "artifact": "npm:@honua/sdk-js"},
        "geospatial-mcp": {"repository": "https://github.com/honua-io/geospatial-mcp", "sha": REVISION,
                           "artifactVersion": "1.0.0+aaaaaaaa", "artifactSourceRevision": REVISION,
                           "artifactSha256": DIGEST, "image": "ghcr.io/honua-io/geospatial-mcp:x",
                           "digest": DIGEST, "platformDigests": {"amd64": AMD64}},
    }}


def test_stamp_gives_every_bound_imaged_component_the_platform_version():
    data = manifest()
    assert stamp_platform_version(data, "2026.1-rc.3") == dict.fromkeys(IMAGED_COMPONENTS, "2026.1.0-rc.3")
    components = data["components"]
    for name in IMAGED_COMPONENTS:
        assert components[name]["artifactVersion"] == "2026.1.0-rc.3"
        # The manifest pin model is unchanged; only the artifact identity is versioned.
        assert components[name]["version"] == "pre-release"
    assert components["honua-server"]["releaseVersion"] == "2026.1.0-rc.3"
    assert "releaseVersion" not in components["honua-console"]


def test_stamp_at_ga_names_the_ga_version():
    data = manifest()
    stamp_platform_version(data, "2026.1.0")
    assert data["components"]["honua-server"]["artifactVersion"] == "2026.1.0"
    assert data["components"]["honua-server"]["releaseVersion"] == "2026.1.0"


def test_an_sdk_or_mcp_component_is_never_stamped():
    data = manifest()
    before = copy.deepcopy(data["components"])
    stamp_platform_version(data, "2026.1-rc.3")
    for name in ("honua-sdk-js", "geospatial-mcp"):
        assert data["components"][name] == before[name]


def test_an_unbound_imaged_component_is_not_stamped_and_loses_a_carried_version():
    data = manifest()
    data["components"]["honua-helm"].pop("artifactSha256")
    data["components"]["honua-helm"]["artifactVersion"] = "2026.1.0-rc.2"
    data["components"]["honua-server"].pop("platformDigests")
    data["components"]["honua-server"].update(artifactVersion="2026.1.0-rc.2", releaseVersion="2026.1.0-rc.2")
    assert stamp_platform_version(data, "2026.1-rc.3") == {"honua-console": "2026.1.0-rc.3"}
    for name in ("honua-helm", "honua-server"):
        assert "artifactVersion" not in data["components"][name]
    assert "releaseVersion" not in data["components"]["honua-server"]


def test_stamp_refuses_a_label_that_names_no_platform_version():
    data = manifest()
    with pytest.raises(ValueError, match="not a platform label"):
        stamp_platform_version(data, "nightly")
    assert "artifactVersion" not in data["components"]["honua-server"]
