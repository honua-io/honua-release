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
        "platform": {"id": "honua-2026.1.0-rc.1", "status": "rc", "supportTier": "standard"},
        "sourceInputs": {}, "contentDigests": {}, "fixtures": [], "sbom": [], "provenance": [], "notes": "notes/v1",
        "components": {
            "sdk": {
                "source": {"repository": "https://github.com/honua-io/honua-sdk", "revision": REVISION},
                "lifecycleStatus": "GA", "supportTier": "standard", "contractVersions": {}, "schemaVersions": {},
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


def test_refuses_source_head_artifact_identity_conflict():
    lock = valid_lock(); lock["components"]["sdk"]["artifacts"][0]["sourceRevision"] = "c" * 40
    assert_refused(lock, "conflicts with component source revision")


def test_refuses_missing_type_specific_integrity():
    lock = valid_lock(); del lock["components"]["sdk"]["artifacts"][0]["integrity"]
    assert_refused(lock, "npm artifacts require")


def test_generator_reports_all_current_unresolved_release_work():
    draft = generator.generate(ROOT / "platform-manifest.yaml", ROOT / "compatibility-matrix.yaml")
    joined = "\n".join(draft.unresolved)
    assert "contentDigests.geospatialMcp" in joined
    assert "contentDigests.catalog" in joined
    assert "contentDigests.okf" in joined
    assert "fixtures" in joined and "sbom" in joined and "provenance" in joined
    assert "lifecycleStatus" in joined and "sourceRevision" in joined
    assert "TBD" not in str(draft.lock)
