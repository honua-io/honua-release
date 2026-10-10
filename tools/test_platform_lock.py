from __future__ import annotations

from pathlib import Path

import pytest

from sdk_baselines import SDK_COMPONENTS, content_digest

import yaml

import generate_platform_lock as generator
from platform_version import artifact_version
import validate_platform_lock as validator

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a" * 40
DIGEST = "sha256:" + "b" * 64
NOTES = f"https://github.com/honua-io/honua-release@{REVISION}:release-notes/2026.1.md#{DIGEST}"


def evidence_manifest(**overrides):
    """A manifest whose release-level facts are all declared, as the cut must declare them."""
    declared = {
        "contentDigests": {
            name: {"repository": "https://github.com/honua-io/honua-server",
                   "revision": REVISION, "path": f"content/{name}.json", "sha256": DIGEST}
            for name, _ in generator.CONTENT_DIGEST_FACTS
        },
        "fixtures": [{"repository": "https://github.com/honua-io/fixtures", "revision": REVISION}],
        "sbom": [{"component": "sdk", "uri": f"oci://example.test/sdk/sbom@{DIGEST}", "sha256": DIGEST}],
        "provenance": [{"component": "sdk", "uri": f"oci://example.test/sdk/provenance@{DIGEST}",
                        "sha256": DIGEST}],
        "notes": {"repository": "https://github.com/honua-io/honua-release", "revision": REVISION,
                  "path": "release-notes/2026.1.md", "sha256": DIGEST},
    }
    declared.update(overrides)
    component = {"repository": "https://github.com/honua-io/sdk", "sha": REVISION,
                 "lifecycleStatus": "GA", "sourcePinnedOnly": True,
                 "contractVersions": {"sdk": "1"}, "dbSchema": "1",
                 "migrationJournalSha256": DIGEST}
    return {"platformRelease": "2026.1", "components": {"sdk": component},
            "platformLockEvidence": declared}


def draft_of(tmp_path, manifest):
    manifest_path, matrix_path = tmp_path / "manifest.yaml", tmp_path / "matrix.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    matrix_path.write_text("contracts: {}\n", encoding="utf-8")
    return generator.generate(manifest_path, matrix_path)


def component():
    evidence = {"uri": "https://example.test/introduction", "sha256": DIGEST}
    content = {"capabilities": {
        "admin.read": {"minimumServerVersion": "1.0.0", "versionModel": "semver", "evidence": evidence},
        "admin.write": {"minimumServerVersion": "1.2.0", "versionModel": "semver", "evidence": evidence},
        "optional": {"minimumServerVersion": "9.0.0", "versionModel": "semver", "evidence": evidence},
    }}
    return {
        "source": {"revision": REVISION},
        "artifacts": [],
        "serverCompatibility": {
            "minimumServerVersion": "1.2.0",
            "manifests": [{
                "source": {"repository": "https://github.com/honua-io/honua-server", "revision": REVISION, "path": "capabilities.json"},
                "content": content, "sha256": content_digest(content),
                "requiredCapabilities": ["admin.read", "admin.write"],
            }],
            "declarations": [{"revision": REVISION, "path": "compatibility.json", "sha256": DIGEST, "minimumServerVersion": "1.2.0"}],
        },
    }


def valid_lock():
    lock = {
        "lockVersion": "platform-lock.v1",
        "platform": {"id": "honua-2026.1-rc.1", "status": "rc", "supportTier": "ga"},
        "sourceInputs": {
            "platformManifest": {"path": "platform-manifest.yaml", "sha256": DIGEST},
            "compatibilityMatrix": {"path": "compatibility-matrix.yaml", "sha256": DIGEST},
        },
        "contentDigests": {"geospatialMcp": DIGEST, "catalog": DIGEST, "okf": DIGEST},
        "fixtures": [{"repository": "https://github.com/honua-io/fixtures", "revision": REVISION}],
        "sbom": [],
        "provenance": [],
        "notes": NOTES,
        "components": {
            "sdk": {
                "source": {"repository": "https://github.com/honua-io/honua-sdk", "revision": REVISION},
                "lifecycleStatus": "GA", "supportTier": "ga", "artifactIdentityModel": "published", "contractVersions": {}, "schemaVersions": {},
                "artifacts": [{"kind": "npm", "coordinate": "@honua/sdk", "version": "1.2.3", "sourceRevision": REVISION, "integrity": "sha512-YWJjZA=="}],
            }
        },
    }

    for name in SDK_COMPONENTS:
        lock["components"][name] = {
            **lock["components"]["sdk"], **component(),
            "source": {"repository": f"https://github.com/honua-io/{name}", "revision": REVISION},
            "artifactIdentityModel": "source-pinned",
        }
    # Evidence is bound when every candidate component is covered by an immutable reference.
    for field in ("sbom", "provenance"):
        lock[field] = [{"component": name, "uri": f"oci://example.test/{name}/{field}@{DIGEST}",
                        "sha256": DIGEST} for name in sorted(lock["components"])]
    return lock


def assert_refused(lock, text):
    findings = validator.validate(lock)
    assert not findings.ok
    assert any(text in error for error in findings.errors), findings.errors


def test_generator_preserves_declared_schema_versions_and_legacy_database(tmp_path):
    manifest = evidence_manifest()
    manifest["components"]["sdk"]["schemaVersions"] = {"metadata": "2.0.0-alpha.1", "database": "1"}
    manifest_path, matrix_path = tmp_path / "manifest.yaml", tmp_path / "matrix.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest))
    matrix_path.write_text("contracts: {}\n")
    draft = generator.generate(manifest_path, matrix_path)
    assert draft.lock["components"]["sdk"]["schemaVersions"] == {"metadata": "2.0.0-alpha.1", "database": "1"}
    assert not any("schemaVersions" in refusal for refusal in draft.unresolved)


def test_generator_refuses_conflicting_database_declarations(tmp_path):
    manifest = evidence_manifest()
    manifest["components"]["sdk"]["schemaVersions"] = {"database": "2"}
    manifest_path, matrix_path = tmp_path / "manifest.yaml", tmp_path / "matrix.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest))
    matrix_path.write_text("contracts: {}\n")
    draft = generator.generate(manifest_path, matrix_path)
    assert any("schemaVersions.database: conflicts with dbSchema" in refusal for refusal in draft.unresolved)


def _version_refusals(tmp_path, **mobile):
    manifest = evidence_manifest()
    manifest["components"]["mobile"] = {"repository": "https://github.com/honua-io/honua-mobile",
                                        "sha": REVISION, "lifecycleStatus": "Experimental", **mobile}
    draft = draft_of(tmp_path, manifest)
    return draft, [item for item in draft.unresolved if "$.components.mobile." in item and "Versions" in item]


def test_generator_accepts_an_explicit_empty_set_for_a_source_pinned_component(tmp_path):
    draft, refusals = _version_refusals(tmp_path, sourcePinnedOnly=True, contractVersions={}, schemaVersions={})
    assert refusals == []
    entry = draft.lock["components"]["mobile"]
    assert entry["contractVersions"] == {} and entry["schemaVersions"] == {}
    assert entry["artifactIdentityModel"] == "source-pinned"


def test_generator_still_refuses_an_absent_set_for_a_source_pinned_component(tmp_path):
    _, refusals = _version_refusals(tmp_path, sourcePinnedOnly=True)
    assert any("mobile.contractVersions: not declared" in item for item in refusals), refusals
    assert any("mobile.schemaVersions: not declared" in item for item in refusals), refusals


@pytest.mark.parametrize("value", [None, [], {"api": "latest"}, {"api": "TBD"}])
def test_generator_refuses_a_malformed_set_for_a_source_pinned_component(tmp_path, value):
    _, refusals = _version_refusals(tmp_path, sourcePinnedOnly=True, contractVersions=value, schemaVersions={})
    assert any("mobile.contractVersions:" in item and "[MECHANICAL]" in item for item in refusals), refusals


def test_generator_refuses_an_empty_set_for_a_published_component(tmp_path):
    _, refusals = _version_refusals(tmp_path, contractVersions={}, schemaVersions={})
    for group in ("contractVersions", "schemaVersions"):
        assert any(f"mobile.{group}: must be a non-empty mapping" in item for item in refusals), refusals
        assert any(f"mobile.{group}: not declared" in item for item in refusals), refusals


def test_refuses_tbd_anywhere():
    lock = valid_lock(); lock["notes"] = "TBD-at-publish"
    assert_refused(lock, "placeholder/TBD")


def test_refuses_floating_image_tag():
    lock = valid_lock(); artifact = lock["components"]["sdk"]["artifacts"][0]
    artifact.update(kind="image", coordinate="ghcr.io/honua/server:latest", digest=DIGEST, architectures=["amd64"])
    assert_refused(lock, "floating tag")


def test_refuses_carried_forward_marker():
    lock = valid_lock(); lock["notes"] = "carried-forward from rc.0"
    assert_refused(lock, "carried-forward")


def test_refuses_source_built_or_non_exact_version():
    lock = valid_lock(); lock["components"]["sdk"]["artifacts"][0]["version"] = "source-built"
    assert_refused(lock, "exact released SemVer")


def test_allows_artifact_provenance_to_predate_component_head():
    lock = valid_lock(); lock["components"]["sdk"]["artifacts"][0]["sourceRevision"] = "c" * 40
    assert validator.validate(lock).ok


def test_refuses_missing_type_specific_integrity():
    lock = valid_lock(); del lock["components"]["sdk"]["artifacts"][0]["integrity"]
    assert_refused(lock, "npm artifacts require")


def test_refuses_support_tier_that_drifts_from_lifecycle():
    lock = valid_lock(); lock["components"]["sdk"]["supportTier"] = "preview"
    assert_refused(lock, "must be derived from lifecycleStatus")


def test_accepts_source_pinned_component_without_published_artifacts():
    lock = valid_lock(); component = lock["components"]["sdk"]
    component["artifactIdentityModel"] = "source-pinned"
    component["artifacts"] = []
    assert validator.validate(lock).ok


def test_applies_schema_before_reporting_valid():
    lock = valid_lock(); lock["platform"] = None
    assert_refused(lock, "schema violation")


def test_refuses_terraform_without_integrity():
    lock = valid_lock(); artifact = lock["components"]["sdk"]["artifacts"][0]
    artifact.clear()
    artifact.update(kind="terraform", coordinate="registry.terraform.io/honua/platform", version="1.2.3", sourceRevision=REVISION)
    assert_refused(lock, "terraform artifacts require a sha256 hash")


def test_generator_preserves_calendar_release_identity(tmp_path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("platformRelease: 2026.1-rc.2\ncomponents: {}\n", encoding="utf-8")
    matrix = tmp_path / "matrix.yaml"
    matrix.write_text("contracts: {}\n", encoding="utf-8")
    draft = generator.generate(manifest, matrix)
    assert draft.lock["platform"]["id"] == "honua-2026.1-rc.2"
    assert not any("platform.id" in item for item in draft.unresolved)


def test_generator_matches_matrix_contract_by_name(tmp_path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "platformRelease: 2026.1\ncomponents:\n  server:\n    sha: " + REVISION
        + "\n    contractVersions:\n      grpc: v1\n",
        encoding="utf-8",
    )
    matrix = tmp_path / "matrix.yaml"
    matrix.write_text("contracts:\n  admin:\n    version: v1\n", encoding="utf-8")
    draft = generator.generate(manifest, matrix)
    assert any("contract 'admin' version 'v1'" in item for item in draft.unresolved)


def test_generator_accepts_calendar_release_candidates_including_rc_zero(tmp_path):
    matrix = tmp_path / "matrix.yaml"
    matrix.write_text("contracts: {}\n", encoding="utf-8")
    for release in ("2026.1", "2026.1-rc.0", "2026.1-rc.2"):
        manifest = tmp_path / "manifest.yaml"
        manifest.write_text(f"platformRelease: {release}\ncomponents: {{}}\n", encoding="utf-8")
        draft = generator.generate(manifest, matrix)
        assert draft.lock["platform"]["id"] == f"honua-{release}"
        assert validator.validate({**valid_lock(), "platform": {"id": f"honua-{release}", "status": "rc", "supportTier": "ga"}}).ok


def test_generator_reports_terraform_sha256(tmp_path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "platformRelease: 2026.1-rc.0\ncomponents:\n  iac:\n    sha: " + REVISION
        + "\n    version: 1.2.3\n    artifact: terraform-registry:honua\n",
        encoding="utf-8",
    )
    matrix = tmp_path / "matrix.yaml"
    matrix.write_text("contracts: {}\n", encoding="utf-8")
    draft = generator.generate(manifest, matrix)
    assert "[MECHANICAL] $.components.iac.artifacts[0].sha256: package hash is not declared" in draft.unresolved


def test_generator_does_not_accept_unbound_evidence_references(tmp_path):
    """Evidence must name this candidate's own components, not an arbitrary label."""
    sbom = [{"component": "some-other-product", "uri": f"oci://example.test/x/sbom@{DIGEST}",
             "sha256": DIGEST}]
    draft = draft_of(tmp_path, evidence_manifest(sbom=sbom))
    assert any("$.sbom[0]: names 'some-other-product', which is not a component of this candidate"
               in item for item in draft.unresolved)
    assert draft.lock["sbom"] == []
    assert any("$.sbom: immutable SBOM references and hashes are not declared" in item
               for item in draft.unresolved)


def test_generator_requires_evidence_to_cover_every_published_component(tmp_path):
    """A candidate whose artifacts have no SBOM/provenance receipt is not bound evidence."""
    manifest = evidence_manifest()
    manifest["components"]["sdk"].pop("sourcePinnedOnly")
    manifest["components"]["sdk"].update(artifact="npm:@honua/sdk", version="1.2.3",
                                         artifactSourceRevision=REVISION)
    manifest["components"]["server"] = {
        "repository": "https://github.com/honua-io/honua-server", "sha": REVISION,
        "lifecycleStatus": "GA", "sourcePinnedOnly": True, "contractVersions": {"admin": "v1"},
        "dbSchema": "1", "migrationJournalSha256": DIGEST,
        "artifact": "npm:@honua/server", "version": "1.2.3", "artifactSourceRevision": REVISION}
    draft = draft_of(tmp_path, manifest)
    for field in ("sbom", "provenance"):
        assert any(f"$.{field}: no " in item and "reference covers the candidate artifacts of server"
                   in item for item in draft.unresolved), field


def test_generator_reports_all_current_unresolved_release_work():
    draft = generator.generate(ROOT / "platform-manifest.yaml", ROOT / "compatibility-matrix.yaml")
    joined = "\n".join(draft.unresolved)
    # The MCP standard content digest is declared and verified against its pinned source bytes
    # by tools/verify_content_digests.py; the runtime catalog/OKF digests need the candidate.
    assert "contentDigests.geospatialMcp" not in joined
    assert draft.lock["contentDigests"] == {
        "geospatialMcp": "sha256:595f0ac8e1e129d4b78e1c4c40abfb71fc87d2d4bf5566a6bede311ed81583c5"
    }
    assert "contentDigests.catalog" in joined
    assert "contentDigests.okf" in joined
    assert "fixtures" in joined and "$.sbom:" in joined and "$.provenance:" in joined
    assert "$.notes: immutable release-notes content/reference is not declared" in joined
    assert "notes" not in draft.lock
    assert "[DECISION]" not in joined
    # honua-iac is pinned to untagged trunk 57b9417e: the cut needs it published as a tag.
    assert "[PUBLISH] $.components.honua-iac.artifacts[0].version" in joined
    assert "sourceRevision" in joined
    assert "TBD" not in str(draft.lock)
    assert draft.lock["components"]["geospatial-mcp"]["artifacts"][0]["sha256"] == (
        "sha256:595f0ac8e1e129d4b78e1c4c40abfb71fc87d2d4bf5566a6bede311ed81583c5"
    )
    assert draft.lock["components"]["honua-iac"]["artifacts"][0]["sha256"] == (
        "sha256:07b6eef99805c6a7b8fa6e9d7d6d75c2479c077adf120faf97a25b56f93f4ee9"
    )
    assert draft.lock["components"]["honua-console"]["artifacts"][0]["architectures"] == ["amd64", "arm64"]
    assert "honua-server.artifacts[0].platformDigests" not in joined
    assert "honua-server.artifacts[0].architectures" not in joined
    assert all(
        component["supportTier"] == component["lifecycleStatus"].lower()
        for component in draft.lock["components"].values()
    )
    assert draft.lock["sbom"] == []
    assert draft.lock["provenance"] == []


def _server_image_manifest(platform_digests, architectures=None):
    server = {
        "repository": "https://github.com/honua-io/honua-server",
        "sha": REVISION,
        "lifecycleStatus": "GA",
        "image": "ghcr.io/honua-io/honua-server:candidate",
        "digest": DIGEST,
        "contractVersions": {"admin": "v1"},
        "dbSchema": "1",
    }
    if platform_digests is not None:
        server["platformDigests"] = platform_digests
    if architectures is not None:
        server["architectures"] = architectures
    return {"platformRelease": "2026.1", "components": {"honua-server": server}}


def _server_artifact(draft):
    return draft.lock["components"]["honua-server"]["artifacts"][0]


def test_committed_server_image_exposes_amd64_digest_to_rollback_certifier(registry_images):
    """The dry-run lock must carry the child the certifier dereferences, not the index."""
    import sys
    sys.path.insert(0, str(ROOT / "mcp"))
    import release_rollback as rollback
    import certify_release_rollback as certification

    draft = generator.generate(ROOT / "platform-manifest.yaml", ROOT / "compatibility-matrix.yaml")
    path = certification.artifact_path(draft.lock, "honua-server", "image", "platformDigests/amd64")
    expected = registry_images["honua-server"]
    amd64, arm64 = (expected["platformDigests"][arch] for arch in ("amd64", "arm64"))
    artifact = _server_artifact(draft)
    assert artifact["digest"] == expected["digest"]
    assert artifact["digest"] not in (amd64, arm64)
    assert artifact["platformDigests"] == {"amd64": amd64, "arm64": arm64}
    assert artifact["architectures"] == ["amd64", "arm64"]
    assert rollback.pointer(draft.lock, path) == amd64
    assert not any("honua-server.artifacts[0].platformDigests" in item for item in draft.unresolved)


def test_generator_copies_exact_amd64_platform_digest(tmp_path):
    amd64, arm64 = "sha256:" + "c" * 64, "sha256:" + "d" * 64
    draft = draft_of(tmp_path, _server_image_manifest({"amd64": amd64, "arm64": arm64}, ["amd64", "arm64"]))
    assert _server_artifact(draft)["platformDigests"] == {"amd64": amd64, "arm64": arm64}
    assert not any("platformDigests" in item for item in draft.unresolved)


@pytest.mark.parametrize("component", ["honua-server", "honua-console"])
@pytest.mark.parametrize("tamper", [None, "missing", "swapped", "index"])
def test_generator_cli_checks_registry_architectures(tmp_path, registry_docker, component, tamper, capsys):
    expected = registry_docker[component]
    manifest = yaml.safe_load((ROOT / "platform-manifest.yaml").read_text())
    image = manifest["components"][component]
    if tamper == "missing":
        del image["platformDigests"]
    elif tamper == "swapped":
        image["platformDigests"] = {"amd64": expected["platformDigests"]["arm64"],
                                   "arm64": expected["platformDigests"]["amd64"]}
    elif tamper == "index":
        image["platformDigests"]["amd64"] = expected["digest"]
    manifest_path, output = tmp_path / "manifest.yaml", tmp_path / "lock.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest))
    status = generator.main(["--manifest", str(manifest_path), "--matrix",
                             str(ROOT / "compatibility-matrix.yaml"), "--output", str(output)])
    # The pre-cut snapshot still has other honest refusals; architecture verification cannot waive them.
    assert status == 1
    refusal = f"$.components.{component}.artifacts[0].platformDigests:"
    errors = capsys.readouterr().err
    assert (refusal in errors) == (tamper is not None)
    lock = yaml.safe_load(output.read_text())
    artifact = lock["components"][component]["artifacts"][0]
    if tamper is None:
        assert artifact["platformDigests"] == expected["platformDigests"]
    else:
        assert "platformDigests" not in artifact
    if tamper == "swapped":
        # Exercise the same live inspector directly so refusal is proven, rather than inferred
        # from the snapshot's unrelated pre-cut refusals.
        import image_platforms
        with pytest.raises(ValueError, match="registry Linux architecture identities"):
            image_platforms.verify_image_platform_digests({**expected, "platformDigests": image["platformDigests"]})


@pytest.mark.parametrize("component", ["honua-server", "honua-console"])
@pytest.mark.parametrize("tamper", ["missing", "missing-arm64", "malformed", "index", "architectures"])
def test_validator_requires_every_image_platform_map(registry_images, component, tamper):
    import validate_platform
    manifest = yaml.safe_load((ROOT / "platform-manifest.yaml").read_text())
    image = manifest["components"][component]
    if tamper == "missing":
        del image["platformDigests"]
    elif tamper == "missing-arm64":
        del image["platformDigests"]["arm64"]
    elif tamper == "malformed":
        image["platformDigests"]["arm64"] = "sha256:abcd"
    elif tamper == "index":
        image["platformDigests"]["amd64"] = registry_images[component]["digest"]
    else:
        image["architectures"] = ["amd64"]
    findings = validate_platform.Findings()
    validate_platform.check_structure(manifest, yaml.safe_load((ROOT / "compatibility-matrix.yaml").read_text()), findings)
    assert any(f"{component}.platformDigests" in error for error in findings.errors)


def test_generator_allows_single_arch_digest_to_equal_the_image_digest(tmp_path):
    draft = draft_of(tmp_path, _server_image_manifest({"amd64": DIGEST}, ["amd64"]))
    assert _server_artifact(draft)["platformDigests"] == {"amd64": DIGEST}
    assert not any("platformDigests" in item for item in draft.unresolved)


@pytest.mark.parametrize("declared", [
    None,
    {},
    {"arm64": DIGEST},
    {"amd64": "sha256:abcd"},
    {"linux/amd64": DIGEST},
    {"amd64": DIGEST, "ppc64le": DIGEST},
])
def test_generator_does_not_copy_platform_digests_the_certifier_cannot_read(tmp_path, declared):
    draft = draft_of(tmp_path, _server_image_manifest(declared, ["amd64"]))
    assert "platformDigests" not in _server_artifact(draft)
    assert any(item.startswith("[AT-CUT]") and "platformDigests" in item for item in draft.unresolved)


def test_generator_refuses_platform_digest_that_repeats_the_multi_arch_index(tmp_path):
    declared = {"amd64": DIGEST, "arm64": "sha256:" + "c" * 64}
    draft = draft_of(tmp_path, _server_image_manifest(declared, ["amd64", "arm64"]))
    assert "platformDigests" not in _server_artifact(draft)
    assert any("multi-arch index" in item for item in draft.unresolved)


def test_generator_refuses_platform_digests_that_disagree_with_architectures(tmp_path):
    draft = draft_of(tmp_path, _server_image_manifest({"amd64": "sha256:" + "c" * 64}, ["amd64", "arm64"]))
    assert "platformDigests" not in _server_artifact(draft)
    assert any("architectures do not match platformDigests" in item for item in draft.unresolved)


def test_generator_derives_support_tier_from_lifecycle_status(tmp_path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "platformRelease: 2026.1\ncomponents:\n  sdk:\n"
        "    repository: https://github.com/honua-io/sdk\n"
        f"    sha: {REVISION}\n"
        "    lifecycleStatus: Preview\n"
        "    sourcePinnedOnly: true\n",
        encoding="utf-8",
    )
    matrix = tmp_path / "matrix.yaml"
    matrix.write_text("contracts: {}\n", encoding="utf-8")
    draft = generator.generate(manifest, matrix)
    assert draft.lock["platform"]["supportTier"] == "ga"
    assert draft.lock["components"]["sdk"]["supportTier"] == "preview"
    assert draft.lock["components"]["sdk"]["artifacts"] == []
    assert not any("[DECISION]" in item for item in draft.unresolved)


def test_generator_tracks_deferred_until_cut_as_signing_blockers(tmp_path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "platformRelease: 2026.1\ncomponents:\n  honua-server:\n    repository: https://github.com/honua-io/honua-server\n    sha: "
        + REVISION
        + "\n    image: ghcr.io/honua-io/honua-server:candidate\n    digest: sha256:"
        + "a" * 64
        + "\n    contractVersions:\n      admin: v1\n    dbSchema: 1\n",
        encoding="utf-8",
    )
    matrix = tmp_path / "matrix.yaml"
    matrix.write_text("contracts:\n  admin:\n    version: v1\n", encoding="utf-8")
    draft = generator.generate(manifest, matrix)
    assert draft.deferred_until_cut
    assert all(item in draft.unresolved for item in draft.deferred_until_cut)
    assert any("artifacts[0].sourceRevision" in item for item in draft.deferred_until_cut)


def test_generator_preserves_deployment_owned_dr_inventory(tmp_path):
    import yaml
    from validate_dr_receipt import expected_substrates

    manifest = yaml.safe_load((ROOT / "platform-manifest.yaml").read_text(encoding="utf-8"))
    inventory = {
        "topology": "single-tenant-test",
        "substrates": {"postgresql": True, "redis": True, "object-storage": True,
                       "job-queue": True, "transactional-outbox": False, "workflow-cursors": False},
    }
    manifest["disasterRecovery"] = inventory
    path = tmp_path / "platform-manifest.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    draft = generator.generate(path, ROOT / "compatibility-matrix.yaml")
    assert draft.lock["disasterRecovery"] == inventory
    assert expected_substrates(draft.lock) == ("single-tenant-test", {"postgresql", "redis", "object-storage", "job-queue"})


def test_lock_schema_checks_dr_enablement():
    lock = valid_lock()
    lock["disasterRecovery"] = {"topology": "test", "substrates": {"postgresql": True}}
    assert_refused(lock, "redis")


def _dr_inventory():
    return {
        "topology": "test",
        "objectives": {"rpoMs": 60000, "rtoMs": 300000},
        "substrates": {name: True for name in
                       ("postgresql", "redis", "object-storage", "job-queue",
                        "transactional-outbox", "workflow-cursors")},
    }


def test_lock_schema_requires_deployment_recovery_objectives():
    lock = valid_lock()
    lock["disasterRecovery"] = _dr_inventory()
    assert validator.validate(lock).ok
    del lock["disasterRecovery"]["objectives"]
    assert_refused(lock, "objectives")


@pytest.mark.parametrize("objectives", [
    {"rpoMs": 60000},
    {"rtoMs": 300000},
    {"rpoMs": 60000, "rtoMs": 0},
    {"rpoMs": -1, "rtoMs": 300000},
    {"rpoMs": 60000, "rtoMs": 300000, "extra": 1},
])
def test_lock_schema_refuses_incomplete_objectives(objectives):
    lock = valid_lock()
    lock["disasterRecovery"] = {**_dr_inventory(), "objectives": objectives}
    assert not validator.validate(lock).ok


# --- Release-level candidate facts (release#231) ------------------------------------------
# Everything the lock carries outside `components` must be declared by the frozen manifest and
# must name immutable bytes. Before these rules the generator could not emit these fields at
# all: contentDigests/fixtures were hard-coded empty and the notes refusal ignored its input.


def test_generator_emits_every_declared_release_fact(tmp_path):
    draft = draft_of(tmp_path, evidence_manifest())
    assert draft.lock["contentDigests"] == {name: DIGEST for name, _ in generator.CONTENT_DIGEST_FACTS}
    assert draft.lock["fixtures"] == [
        {"repository": "https://github.com/honua-io/fixtures", "revision": REVISION}]
    assert draft.lock["sbom"] == [
        {"component": "sdk", "uri": f"oci://example.test/sdk/sbom@{DIGEST}", "sha256": DIGEST}]
    assert draft.lock["notes"] == NOTES
    # Nothing release-level is left unresolved once every fact is declared and bound.
    assert not [item for item in draft.unresolved
                if item.partition("] ")[2].startswith(
                    ("$.contentDigests", "$.fixtures", "$.sbom", "$.provenance", "$.notes"))]


def test_qualification_records_pending_without_changing_default_signing_refusals(tmp_path):
    manifest = evidence_manifest()
    for key in ('sbom', 'provenance', 'notes', 'fixtures'):
        del manifest['platformLockEvidence'][key]
    frozen = draft_of(tmp_path, manifest)
    qualified = generator.generate(tmp_path / 'manifest.yaml', tmp_path / 'matrix.yaml', qualification=True)
    for field in ('sbom', 'provenance', 'notes', 'fixtures'):
        assert any(f'$.{field}:' in refusal for refusal in frozen.unresolved)
        assert not any(f'$.{field}:' in refusal for refusal in qualified.unresolved)
        assert qualified.lock[field] == {'status': 'post-gate pending', 'field': field}
    assert qualified.lock['sourceInputs'] == frozen.lock['sourceInputs']
    again = generator.generate(tmp_path / 'manifest.yaml', tmp_path / 'matrix.yaml')
    assert again.unresolved == frozen.unresolved and again.lock == frozen.lock


def test_qualification_never_defers_other_missing_facts(tmp_path):
    manifest = evidence_manifest()
    del manifest['platformLockEvidence']['contentDigests']
    draft_of(tmp_path, manifest)
    qualified = generator.generate(tmp_path / 'manifest.yaml', tmp_path / 'matrix.yaml', qualification=True)
    assert any('$.contentDigests' in refusal for refusal in qualified.unresolved)


def test_generator_refuses_content_digest_at_a_moving_revision(tmp_path):
    declared = evidence_manifest()["platformLockEvidence"]["contentDigests"]
    declared["catalog"] = {**declared["catalog"], "revision": "trunk"}
    draft = draft_of(tmp_path, evidence_manifest(contentDigests=declared))
    assert any("$.contentDigests.catalog: source revision must be an immutable" in item
               for item in draft.unresolved)
    assert "catalog" not in draft.lock["contentDigests"]


def test_generator_refuses_a_content_digest_the_lock_cannot_carry(tmp_path):
    declared = evidence_manifest()["platformLockEvidence"]["contentDigests"]
    declared["studio"] = declared["catalog"]
    draft = draft_of(tmp_path, evidence_manifest(contentDigests=declared))
    assert any("$.contentDigests.studio: the lock schema declares no such content digest" in item
               for item in draft.unresolved)
    assert "studio" not in draft.lock["contentDigests"]


def test_generator_refuses_two_revisions_of_one_fixture_source(tmp_path):
    fixtures = [{"repository": "https://github.com/honua-io/fixtures", "revision": REVISION},
                {"repository": "https://github.com/honua-io/fixtures", "revision": "c" * 40}]
    draft = draft_of(tmp_path, evidence_manifest(fixtures=fixtures))
    assert any("$.fixtures[1]: duplicate fixture source declaration" in item
               for item in draft.unresolved)
    assert draft.lock["fixtures"] == [fixtures[0]]


def test_generator_refuses_release_notes_that_are_not_an_immutable_reference(tmp_path):
    for notes in ("see the 2026.1 announcement",
                  "https://github.com/honua-io/honua-release/blob/trunk/release-notes/2026.1.md#" + DIGEST,
                  {"repository": "https://github.com/honua-io/honua-release", "revision": REVISION,
                   "path": "release-notes/2026.1.md"}):
        draft = draft_of(tmp_path, evidence_manifest(notes=notes))
        assert any(item.startswith("[AT-CUT] $.notes:") for item in draft.unresolved), notes
        assert "notes" not in draft.lock


def test_generator_refuses_evidence_published_under_a_moving_reference(tmp_path):
    sbom = [{"component": "server", "uri": "oci://ghcr.io/honua-io/honua-server:latest", "sha256": DIGEST}]
    draft = draft_of(tmp_path, evidence_manifest(sbom=sbom))
    assert any("$.sbom[0]: oci reference must be pinned" in item for item in draft.unresolved)
    assert draft.lock["sbom"] == []
    assert any("$.sbom: immutable SBOM references and hashes are not declared" in item
               for item in draft.unresolved)


def test_generator_keeps_every_undeclared_release_fact_refused(tmp_path):
    draft = draft_of(tmp_path, {"platformRelease": "2026.1", "components": {}})
    for message in ("$.contentDigests.geospatialMcp: certified content digest is not declared",
                    "$.contentDigests.catalog: catalog digest is not declared",
                    "$.contentDigests.okf: OKF digest is not declared",
                    "$.fixtures: fixture repository revisions are not declared",
                    "$.sbom: immutable SBOM references and hashes are not declared",
                    "$.provenance: immutable provenance references and hashes are not declared",
                    "$.notes: immutable release-notes content/reference is not declared"):
        assert f"[AT-CUT] {message}" in draft.unresolved


def test_validator_refuses_release_notes_bound_to_a_branch():
    lock = valid_lock()
    lock["notes"] = ("https://github.com/honua-io/honua-release/blob/release-2026.1/"
                     "release-notes/2026.1.md#" + DIGEST)
    assert_refused(lock, "git references must name a 40-character revision")
    lock["notes"] = "https://github.com/honua-io/honua-release/blob/trunk/NOTES.md#" + DIGEST
    assert_refused(lock, "floating tag references are forbidden")


def test_validator_refuses_evidence_hosted_at_a_moving_release_url():
    """A declared sha256 nobody fetches cannot make a moving URL immutable."""
    lock = valid_lock()
    lock["sbom"] = [{"component": "sdk", "uri": "https://host.test/releases/download/sbom.json",
                     "sha256": DIGEST}]
    assert_refused(lock, "must be content-addressed")
    lock["notes"] = "https://host.test/releases/latest/download/notes.md#" + DIGEST
    assert_refused(lock, "floating tag references are forbidden")


def test_validator_refuses_evidence_under_a_floating_tag():
    lock = valid_lock()
    lock["sbom"] = [{"component": "server", "uri": "https://artifacts.test/honua/latest", "sha256": DIGEST}]
    assert_refused(lock, "floating tag references are forbidden")


def test_validator_refuses_an_unpinned_oci_evidence_reference():
    lock = valid_lock()
    lock["provenance"] = [{"component": "server", "uri": "oci://ghcr.io/honua-io/honua-server", "sha256": DIGEST}]
    assert_refused(lock, "oci reference must be pinned")


def test_validator_refuses_two_revisions_of_one_fixture_source():
    lock = valid_lock()
    lock["fixtures"] = [{"repository": "https://github.com/honua-io/fixtures", "revision": REVISION},
                        {"repository": "https://github.com/honua-io/fixtures", "revision": "c" * 40}]
    assert_refused(lock, "one revision per fixture repository path")


def test_validator_refuses_a_fixture_pinned_to_a_branch():
    lock = valid_lock()
    lock["fixtures"] = [{"repository": "https://github.com/honua-io/fixtures", "revision": "trunk"}]
    assert_refused(lock, "immutable 40-character git revision")


def test_generator_refuses_two_identities_for_one_standard(tmp_path):
    """A content digest may not contradict the component artifact naming the same bytes."""
    manifest = evidence_manifest()
    manifest["components"]["geospatial-mcp"] = {
        "repository": "https://github.com/honua-io/geospatial-mcp", "sha": REVISION,
        "lifecycleStatus": "Experimental", "contractVersions": {"mcp": "1"}, "dbSchema": "1",
        "migrationJournalSha256": DIGEST,
        "artifact": f"spec:https://github.com/honua-io/geospatial-mcp/blob/{REVISION}/spec/schemas/index.json",
        "artifactVersion": "1.0.0", "artifactSourceRevision": REVISION, "artifactSha256": DIGEST,
    }
    digests = manifest["platformLockEvidence"]["contentDigests"]
    digests["geospatialMcp"] = {"repository": "https://github.com/honua-io/geospatial-mcp",
                                "revision": REVISION, "path": "spec/schemas/README.md",
                                "sha256": "sha256:" + "e" * 64}
    draft = draft_of(tmp_path, manifest)
    assert any("$.contentDigests.geospatialMcp: disagrees with "
               "$.components.geospatial-mcp.artifact on path, sha256" in item
               for item in draft.unresolved)
    # The contradicted digest never reaches the lock.
    assert "geospatialMcp" not in draft.lock["contentDigests"]
    # The agreeing declaration is accepted.
    digests["geospatialMcp"] = {"repository": "https://github.com/honua-io/geospatial-mcp",
                                "revision": REVISION, "path": "spec/schemas/index.json",
                                "sha256": DIGEST}
    agreed = draft_of(tmp_path, manifest)
    assert agreed.lock["contentDigests"]["geospatialMcp"] == DIGEST
    assert not any("disagrees with" in item for item in agreed.unresolved)


@pytest.mark.parametrize("value", ["false", "true", 1, None])
@pytest.mark.parametrize("empty", [True, False])
def test_generator_requires_boolean_source_pinned_only(tmp_path, value, empty):
    draft, refusals = _version_refusals(
        tmp_path, sourcePinnedOnly=value,
        contractVersions={} if empty else {"api": "1"},
        schemaVersions={} if empty else {"workspace": "1"})
    assert any("[MECHANICAL] $.components.mobile.sourcePinnedOnly: must be a boolean" in item
               for item in draft.unresolved)
    assert draft.lock["components"]["mobile"]["artifactIdentityModel"] == "published"
    assert any("mobile.artifacts: no artifact coordinate is declared" in item for item in draft.unresolved)
    if empty:
        for group in ("contractVersions", "schemaVersions"):
            assert any(f"mobile.{group}: must be a non-empty mapping" in item for item in refusals)
            assert any(f"mobile.{group}: not declared" in item for item in refusals)


def test_generator_false_does_not_allow_empty_version_sets(tmp_path):
    draft, refusals = _version_refusals(tmp_path, sourcePinnedOnly=False, contractVersions={}, schemaVersions={})
    assert not any("sourcePinnedOnly: must be a boolean" in item for item in draft.unresolved)
    for group in ("contractVersions", "schemaVersions"):
        assert any(f"mobile.{group}: must be a non-empty mapping" in item for item in refusals)


# --- R22: imaged components take the lock's platform version (#231 WI-2) ---

AMD64, ARM64 = "sha256:" + "c" * 64, "sha256:" + "d" * 64


def _imaged_manifest(release="2026.1-rc.3", **server):
    """honua-server, honua-console and honua-helm with every identity fact bound and stamped."""
    image = {"lifecycleStatus": "GA", "sha": REVISION, "digest": DIGEST, "artifactSourceRevision": REVISION,
             "architectures": ["amd64", "arm64"], "platformDigests": {"amd64": AMD64, "arm64": ARM64},
             "version": "pre-release", "artifactVersion": artifact_version(release)}
    rows = {
        "honua-server": {**image, "repository": "https://github.com/honua-io/honua-server",
                         "image": "ghcr.io/honua-io/honua-server:nightly-aaaaaaa",
                         "releaseVersion": artifact_version(release)},
        "honua-console": {**image, "repository": "https://github.com/honua-io/honua-console",
                          "image": "ghcr.io/honua-io/honua-console:candidate-aaaaaaaaaaaa-1-1"},
        "honua-helm": {"repository": "https://github.com/honua-io/honua-helm", "sha": REVISION,
                       "lifecycleStatus": "Preview", "artifact": "oci-chart:honua", "digest": DIGEST,
                       "artifactSourceRevision": REVISION, "architectures": ["amd64", "arm64"],
                       "artifactSha256": AMD64, "version": "pre-release",
                       "artifactVersion": artifact_version(release)},
    }
    rows["honua-server"].update(server)
    for row in rows.values():
        for key in [key for key, value in row.items() if value is None]:
            del row[key]
    return {"platformRelease": release, "components": rows}


def _version_items(draft, name):
    return [item for item in draft.unresolved
            if f"$.components.{name}.artifacts[0].version" in item or f"$.components.{name}.releaseVersion" in item]


@pytest.mark.parametrize("release,version", [("2026.1-rc.3", "2026.1.0-rc.3"), ("2026.1.0", "2026.1.0")])
def test_generator_accepts_the_platform_version_beside_bound_bytes(tmp_path, release, version):
    draft = draft_of(tmp_path, _imaged_manifest(release))
    for name in ("honua-server", "honua-console", "honua-helm"):
        assert draft.lock["components"][name]["artifacts"][0]["version"] == version
        assert _version_items(draft, name) == []
    # The first-release floor names the server image that ships.
    assert draft.lock["components"]["honua-server"]["releaseVersion"] == version


@pytest.mark.parametrize("name,field,missing", [
    ("honua-server", "digest", "digest"),
    ("honua-server", "artifactSourceRevision", "sourceRevision"),
    ("honua-server", "platformDigests", "platformDigests"),
    ("honua-console", "digest", "digest"),
    ("honua-console", "platformDigests", "platformDigests"),
    ("honua-helm", "artifactSha256", "sha256"),
    ("honua-helm", "artifactSourceRevision", "sourceRevision"),
])
def test_generator_refuses_a_platform_version_stamped_without_bound_bytes(tmp_path, name, field, missing):
    manifest = _imaged_manifest()
    del manifest["components"][name][field]
    draft = draft_of(tmp_path, manifest)
    assert "version" not in draft.lock["components"][name]["artifacts"][0]
    assert any(f"$.components.{name}.artifacts[0].version: platform version 2026.1.0-rc.3 is accepted "
               f"only when {missing} is bound" in item for item in draft.unresolved), draft.unresolved


@pytest.mark.parametrize("version", ["2026.1.0-rc.2", "2026.1-rc.3", "1.0.0", "0.4.0"])
@pytest.mark.parametrize("name", ["honua-server", "honua-console", "honua-helm"])
def test_generator_refuses_an_imaged_version_other_than_the_platform_version(tmp_path, name, version):
    manifest = _imaged_manifest()
    manifest["components"][name]["artifactVersion"] = version
    draft = draft_of(tmp_path, manifest)
    assert "version" not in draft.lock["components"][name]["artifacts"][0]
    assert any(f"[MECHANICAL] $.components.{name}.artifacts[0].version: {version!r} is not the lock's "
               "platform version '2026.1.0-rc.3'" in item for item in draft.unresolved), draft.unresolved


@pytest.mark.parametrize("name", ["honua-server", "honua-console", "honua-helm"])
def test_generator_still_refuses_pre_release(tmp_path, name):
    manifest = _imaged_manifest()
    del manifest["components"][name]["artifactVersion"]
    draft = draft_of(tmp_path, manifest)
    assert "version" not in draft.lock["components"][name]["artifacts"][0]
    assert any(f"$.components.{name}.artifacts[0].version: source snapshot/pre-release" in item
               for item in draft.unresolved)


def test_generator_refuses_an_imaged_version_when_the_release_names_no_platform_version(tmp_path):
    manifest = _imaged_manifest()
    manifest["platformRelease"] = "snapshot"
    draft = draft_of(tmp_path, manifest)
    assert all("version" not in draft.lock["components"][name]["artifacts"][0]
               for name in ("honua-server", "honua-console", "honua-helm"))
    assert any("is not the lock's platform version None" in item for item in draft.unresolved)


@pytest.mark.parametrize("release_version", ["2026.1.0-rc.2", "2026.1.0"])
def test_generator_refuses_a_release_version_no_locked_server_image_carries(tmp_path, release_version):
    draft = draft_of(tmp_path, _imaged_manifest(releaseVersion=release_version))
    assert "releaseVersion" not in draft.lock["components"]["honua-server"]
    assert any(f"[MECHANICAL] $.components.honua-server.releaseVersion: {release_version!r} is not the "
               "version of a locked honua-server artifact" in item for item in draft.unresolved)


def test_generator_drops_the_release_version_of_an_unversioned_server_image(tmp_path):
    manifest = _imaged_manifest()
    del manifest["components"]["honua-server"]["platformDigests"]
    draft = draft_of(tmp_path, manifest)
    assert "releaseVersion" not in draft.lock["components"]["honua-server"]
    assert any("honua-server.releaseVersion" in item for item in draft.unresolved)


def test_generator_keeps_sdk_semver_independent_of_the_platform_version(tmp_path):
    manifest = _imaged_manifest()
    manifest["components"]["honua-sdk-dotnet"] = {
        "repository": "https://github.com/honua-io/honua-sdk-dotnet", "sha": REVISION, "lifecycleStatus": "GA",
        "artifact": "nuget:Honua.Sdk", "version": "1.6.2", "artifactVersion": "1.6.2",
        "artifactSourceRevision": REVISION, "artifactSha256": DIGEST}
    draft = draft_of(tmp_path, manifest)
    assert draft.lock["components"]["honua-sdk-dotnet"]["artifacts"][0]["version"] == "1.6.2"
    assert _version_items(draft, "honua-sdk-dotnet") == []
