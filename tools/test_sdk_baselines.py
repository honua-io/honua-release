from __future__ import annotations

import copy

import pytest
import yaml

from generate_compatibility_table import main, render
from sdk_baselines import SDK_COMPONENTS, check_component, content_digest, derive, findings
from test_platform_lock import DIGEST, REVISION, component, valid_lock
from validate_platform_lock import validate


def test_maximum_required_floor_excludes_optional_capabilities():
    assert check_component(component()) == "1.2.0"


def test_multiple_manifests_include_every_consumed_requirement():
    item = component()
    other = copy.deepcopy(item["serverCompatibility"]["manifests"][0])
    other["requiredCapabilities"] = ["optional"]
    item["serverCompatibility"]["manifests"].append(other)
    assert derive(item["serverCompatibility"]) == "9.0.0"


def test_rejects_tampered_manifest_content():
    item = component()
    item["serverCompatibility"]["manifests"][0]["content"]["capabilities"]["admin.write"]["minimumServerVersion"] = "0.1.0"
    with pytest.raises(ValueError, match="digest"):
        check_component(item)


@pytest.mark.parametrize("field", ["minimumServerVersion", "versionModel", "evidence"])
def test_missing_capability_introduction_is_unqualified(field):
    item = component()
    manifest = item["serverCompatibility"]["manifests"][0]
    del manifest["content"]["capabilities"]["admin.read"][field]
    manifest["sha256"] = content_digest(manifest["content"])
    with pytest.raises(ValueError, match="unqualified"):
        check_component(item)


def test_undeclared_required_capability_is_not_silently_ignored():
    item = component()
    item["serverCompatibility"]["manifests"][0]["requiredCapabilities"].append("absent")
    with pytest.raises(ValueError, match="absent"):
        check_component(item)


@pytest.mark.parametrize("location", ["lock", "declaration"])
def test_rejects_conflicting_baseline(location):
    item = component()
    target = item["serverCompatibility"] if location == "lock" else item["serverCompatibility"]["declarations"][0]
    target["minimumServerVersion"] = "0.1.0"
    with pytest.raises(ValueError, match="floor"):
        check_component(item)


def test_declaration_must_bind_source():
    item = component()
    item["serverCompatibility"]["declarations"][0]["revision"] = "b" * 40
    with pytest.raises(ValueError, match="source"):
        check_component(item)


def test_empty_lock_cannot_pass_by_omitting_sdk_roster():
    assert len(findings({})) == 4


def test_complete_lock_validator_requires_official_sdk_baseline():
    lock = valid_lock()
    lock["components"]["honua-sdk-js"] = lock["components"].pop("sdk")
    assert any("serverCompatibility" in error for error in validate(lock).errors)
    lock["components"]["honua-sdk-js"]["serverCompatibility"] = component()["serverCompatibility"]
    assert validate(lock).ok
    lock["components"]["honua-sdk-js"]["serverCompatibility"]["minimumServerVersion"] = "0.1.0"
    assert any("floor" in error for error in validate(lock).errors)


def test_table_is_deterministic_and_does_not_imply_upgrade_support():
    lock = valid_lock()
    del lock["components"]["honua-sdk-python"]
    output = render(lock)
    assert output == render(copy.deepcopy(lock))
    assert "## UPGRADE EDGES" in output
    assert "No qualified edge is pinned" in output
    assert "restoring a verified pre-upgrade backup is required" in output
    assert "| honua-sdk-python | source pin only | unqualified |" in output


def test_cli_documentation_check_cannot_be_confused_with_qualification(tmp_path):
    source, output = tmp_path / "lock.yaml", tmp_path / "table.md"
    lock = valid_lock()
    del lock["components"]["honua-sdk-python"]
    source.write_text(yaml.safe_dump(lock), encoding="utf-8")
    args = [str(source), "--output", str(output)]
    assert main(args) == 0
    assert main([*args, "--check-output"]) == 0
    assert main([*args, "--check"]) == 1
    output.write_text("stale", encoding="utf-8")
    assert main([*args, "--check-output"]) == 1


@pytest.mark.parametrize("name", SDK_COMPONENTS)
def test_nonempty_lock_cannot_omit_official_sdk(name):
    lock = valid_lock()
    del lock["components"][name]
    assert any(f"$.components.{name}: required official SDK" in error for error in validate(lock).errors)


def test_published_declaration_cannot_bind_newer_component_head():
    item = component()
    item["artifacts"] = [{"sourceRevision": "b" * 40}]
    with pytest.raises(ValueError, match="source"):
        check_component(item)
    item["serverCompatibility"]["declarations"][0]["revision"] = "b" * 40
    assert check_component(item) == "1.2.0"


def test_declarations_must_cover_all_shipped_artifact_revisions():
    item = component()
    item["artifacts"] = [{"sourceRevision": REVISION}, {"sourceRevision": "b" * 40}]
    with pytest.raises(ValueError, match="every artifact source revision"):
        check_component(item)
    declaration = copy.deepcopy(item["serverCompatibility"]["declarations"][0])
    declaration["revision"] = "b" * 40
    item["serverCompatibility"]["declarations"].append(declaration)
    assert check_component(item) == "1.2.0"


# --- The first-release floor is the lock's platform version (honua-release#231 WI-5, R22) ---

from generate_platform_lock import generate
from sdk_baselines import platform_version, release_context
from test_platform_lock import evidence_manifest
import resolve_trunk_candidate as resolver

BASELINES = resolver.ROOT / "tools" / "fixtures" / "sdk-capability-baselines"
PUBLISHED = "8a0a06c815baefd49e7398d38a9f22642a8c80c5"
RECEIPT = {"path": "certification/sources/server-publication-history.v1.json",
           "uri": "https://github.com/honua-io/honua-release/blob/0dd9b7a37ab4ee0dd02c17632e3de9e9eeeeddbd/"
                  "certification/sources/server-publication-history.v1.json",
           "sha256": "sha256:3069fde14a32cc579e4ee92cbe7e86bd88a14c1405df94457e1393c63fe092d1",
           "maxAgeDays": 14}
ADVERTISED = frozenset({"ai.mcp-discovery", "discovery.capability-manifest",
                        "serve.geoservices-featureserver", "serve.ogc-api-features"})


class Baselines:
    def file(self, repository, revision, path):
        assert (revision, path) == (PUBLISHED, resolver.SDK_BASELINE_PATH)
        return (BASELINES / (repository.split("/", 1)[1] + ".json")).read_bytes()


def resolved_compatibility(name):
    row = {"repository": f"https://github.com/honua-io/{name}", "sha": PUBLISHED, "artifactSourceRevision": PUBLISHED}
    return resolver.sdk_capability_baseline(Baselines(), name, row, {}, ADVERTISED)


def first_release_lock(platform_id="honua-2026.1-rc.3", server_version="2026.1.0-rc.3", receipt=True):
    lock = valid_lock()
    lock["platform"]["id"] = platform_id
    server = {"kind": "image", "coordinate": "ghcr.io/honua-io/honua-server", "version": server_version,
              "sourceRevision": REVISION, "digest": DIGEST, "platformDigests": {"amd64": DIGEST},
              "architectures": ["amd64"]}
    lock["components"]["honua-server"] = {
        **lock["components"]["sdk"], "source": {"repository": "https://github.com/honua-io/honua-server",
                                                "revision": REVISION}, "artifacts": [server]}
    if receipt:
        lock["components"]["honua-server"]["publicationHistory"] = dict(RECEIPT)
    for name in SDK_COMPONENTS:
        lock["components"][name]["source"]["revision"] = PUBLISHED
        lock["components"][name]["serverCompatibility"] = resolved_compatibility(name)
    return lock


@pytest.mark.parametrize("platform_id,version", [
    ("honua-2026.1-rc.3", "2026.1.0-rc.3"),
    ("honua-2026.1", "2026.1.0"),
    ("honua-2026.1.2-rc.1", "2026.1.2-rc.1"),
    ("honua-2026.1.2", "2026.1.2"),
    ("2026.1-rc.3", None),
    ("honua-2026.1-rc", None),
    ("honua-2026.1-beta.1", None),
    (None, None),
])
def test_platform_version_is_the_r22_image_version(platform_id, version):
    assert platform_version({"platform": {"id": platform_id}}) == version


def test_every_repository_baseline_resolves_to_the_lock_platform_version():
    lock = first_release_lock()
    assert findings(lock) == []
    context = release_context(lock)
    assert {name: check_component(lock["components"][name], context) for name in SDK_COMPONENTS} == {
        "honua-sdk-js": "2026.1.0-rc.3", "honua-sdk-dotnet": "2026.1.0-rc.3",
        "honua-sdk-python": "2026.1.0-rc.3", "geospatial-mcp": "2026.1.0-rc.3"}
    assert not any("serverCompatibility" in error for error in validate(lock).errors)


def test_a_platform_version_no_server_artifact_ships_is_not_a_floor():
    errors = findings(first_release_lock(server_version="2026.1.0-rc.2"))
    assert len(errors) == 4
    assert all("2026.1.0-rc.3, which no locked publisher artifact declares" in error for error in errors)


def test_a_lock_without_a_platform_version_or_receipt_stays_unqualified():
    assert all("does not name" in error for error in findings(first_release_lock(platform_id="snapshot")))
    assert all("publication-history receipt" in error for error in findings(first_release_lock(receipt=False)))


def test_an_explicit_publisher_release_version_takes_precedence():
    lock = first_release_lock(server_version="2026.1.0")
    lock["components"]["honua-server"]["releaseVersion"] = "2026.1.0"
    assert findings(lock) == []


def test_a_declared_first_release_cannot_stand_for_a_numeric_floor():
    """The sentinel resolves to the first release; it never matches a lower, numeric-only floor."""
    item = component()
    item["serverCompatibility"]["minimumServerVersion"] = "first-release"
    with pytest.raises(ValueError, match="no consumed manifest introduces"):
        check_component(item, release_context(first_release_lock()))
    item = component()
    item["serverCompatibility"]["declarations"][0]["minimumServerVersion"] = "first-release"
    with pytest.raises(ValueError, match="no consumed manifest introduces"):
        check_component(item, release_context(first_release_lock()))
    with pytest.raises(ValueError, match="no consumed manifest introduces"):
        check_component(item, {})


def test_a_numeric_only_manifest_cannot_declare_first_release_even_at_the_same_version():
    """Equal values are not enough: the sentinel needs a first-release introduction to summarise."""
    context = {"firstReleaseVersion": "1.2.0"}
    assert check_component(component(), context) == "1.2.0"
    item = component()
    item["serverCompatibility"]["minimumServerVersion"] = "first-release"
    with pytest.raises(ValueError, match="no consumed manifest introduces"):
        check_component(item, context)
    item = component()
    item["serverCompatibility"]["declarations"][0]["minimumServerVersion"] = "first-release"
    with pytest.raises(ValueError, match="no consumed manifest introduces"):
        check_component(item, context)


def test_the_generator_resolves_the_floor_once_every_component_is_built(tmp_path):
    """Through the real generator: honua-server is listed after the SDKs, and still names the floor."""
    def draft(platform_release):
        manifest = evidence_manifest()
        manifest["platformRelease"] = platform_release
        for name in SDK_COMPONENTS:
            manifest["components"][name] = {
                "repository": f"https://github.com/honua-io/{name}", "sha": PUBLISHED, "lifecycleStatus": "GA",
                "sourcePinnedOnly": True, "contractVersions": {}, "schemaVersions": {},
                "serverCompatibility": resolved_compatibility(name)}
        manifest["components"]["honua-server"] = {
            "repository": "https://github.com/honua-io/honua-server", "sha": REVISION, "lifecycleStatus": "GA",
            "contractVersions": {"admin": "v1"}, "schemaVersions": {"metadata": "1"},
            "image": "ghcr.io/honua-io/honua-server", "version": "2026.1.0-rc.3",
            "digest": DIGEST, "architectures": ["amd64"], "platformDigests": {"amd64": DIGEST},
            "artifactSourceRevision": REVISION, "publicationHistory": dict(RECEIPT)}
        manifest_path, matrix_path = tmp_path / f"{platform_release}.yaml", tmp_path / "matrix.yaml"
        manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
        matrix_path.write_text("contracts: {}\n", encoding="utf-8")
        return [row for row in generate(manifest_path, matrix_path).unresolved if "serverCompatibility" in row]

    assert draft("2026.1-rc.3") == []
    refused = draft("2026.1-rc.4")
    assert [row.split(".serverCompatibility")[0] for row in refused] == [
        f"[PUBLISH] $.components.{name}" for name in SDK_COMPONENTS]
    assert all("2026.1.0-rc.4, which no locked publisher artifact declares" in row for row in refused)
