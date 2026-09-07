from __future__ import annotations

from pathlib import Path

import pytest

from sdk_baselines import SDK_COMPONENTS, content_digest

import yaml

import generate_platform_lock as generator
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
    assert draft.lock["sbom"] == []
    assert draft.lock["provenance"] == []


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
