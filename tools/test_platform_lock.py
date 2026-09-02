from __future__ import annotations

from pathlib import Path

import generate_platform_lock as generator
import validate_platform_lock as validator

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a" * 40
DIGEST = "sha256:" + "b" * 64


def valid_lock():
    return {
        "lockVersion": "platform-lock.v1",
        "platform": {"id": "honua-2026.1-rc.1", "status": "rc", "supportTier": "ga"},
        "sourceInputs": {
            "platformManifest": {"path": "platform-manifest.yaml", "sha256": DIGEST},
            "compatibilityMatrix": {"path": "compatibility-matrix.yaml", "sha256": DIGEST},
        },
        "contentDigests": {"geospatialMcp": DIGEST, "catalog": DIGEST, "okf": DIGEST},
        "fixtures": [{"repository": "https://github.com/honua-io/fixtures", "revision": REVISION}],
        "sbom": [{"component": "sdk", "uri": "https://example.test/sbom", "sha256": DIGEST}],
        "provenance": [{"component": "sdk", "uri": "https://example.test/provenance", "sha256": DIGEST}],
        "notes": "notes/v1",
        "components": {
            "sdk": {
                "source": {"repository": "https://github.com/honua-io/honua-sdk", "revision": REVISION},
                "lifecycleStatus": "GA", "supportTier": "ga", "artifactIdentityModel": "published", "contractVersions": {}, "schemaVersions": {},
                "artifacts": [{"kind": "npm", "coordinate": "@honua/sdk", "version": "1.2.3", "sourceRevision": REVISION, "integrity": "sha512-YWJjZA=="}],
            }
        },
    }


def assert_refused(lock, text):
    findings = validator.validate(lock)
    assert not findings.ok
    assert any(text in error for error in findings.errors), findings.errors


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


def test_generator_reports_all_current_unresolved_release_work():
    draft = generator.generate(ROOT / "platform-manifest.yaml", ROOT / "compatibility-matrix.yaml")
    joined = "\n".join(draft.unresolved)
    assert "contentDigests.geospatialMcp" in joined
    assert "contentDigests.catalog" in joined
    assert "contentDigests.okf" in joined
    assert "fixtures" in joined
    assert "$.sbom:" not in joined and "$.provenance:" not in joined
    assert "[DECISION]" not in joined
    assert "sourceRevision" in joined
    assert "TBD" not in str(draft.lock)
    assert draft.lock["components"]["geospatial-mcp"]["artifacts"][0]["sha256"] == (
        "sha256:595f0ac8e1e129d4b78e1c4c40abfb71fc87d2d4bf5566a6bede311ed81583c5"
    )
    assert draft.lock["components"]["honua-iac"]["artifacts"][0]["sha256"] == (
        "sha256:58e80786f381ddd3ae835ccacc69f49c0a7d159758df3823ad9615f4da5792ed"
    )
    assert draft.lock["components"]["honua-console"]["artifacts"][0]["architectures"] == ["amd64", "arm64"]
    assert all(
        component["supportTier"] == component["lifecycleStatus"].lower()
        for component in draft.lock["components"].values()
    )
    assert len(draft.lock["sbom"]) == 2
    assert len(draft.lock["provenance"]) == 4


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
