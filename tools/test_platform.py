"""Tests for the Phase 0 source-of-truth gate.

Two duties:
  1. Lock the SemVer range semantics the matrix relies on.
  2. PROVE the gate can FAIL — every validate() rule is exercised with a real violation that must
     produce an error. A gate that only ever goes green is the exact anti-pattern AGENTS.md forbids.

The real repo files (platform-manifest.yaml + compatibility-matrix.yaml) must pass structure +
coherence as committed; that is asserted too, so a bad edit to either file reddens here.

Run: python -m pytest tools/test_platform.py    (or: python tools/test_platform.py)
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

import semver
import validate_platform as vp
import trunk_reachability as tr

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---- SemVer ---------------------------------------------------------------------------------------
def test_semver_ordering_prerelease_below_release():
    assert semver.parse("1.0.0-alpha") < semver.parse("1.0.0")
    assert semver.parse("1.0.0-alpha.1") < semver.parse("1.0.0-alpha.2")
    assert semver.parse("1.0.0-alpha.1") < semver.parse("1.0.0-alpha.beta")  # numeric < alphanumeric
    assert semver.parse("0.0.14-alpha.0") < semver.parse("0.1.0")
    assert semver.parse("2.0.0") > semver.parse("1.9.9")


def test_semver_satisfies_matrix_style_ranges():
    assert semver.satisfies("1.3.0", ">=1.3.0 <2.0.0")
    assert not semver.satisfies("2.0.0", ">=1.3.0 <2.0.0")
    assert semver.satisfies("0.0.14-alpha.0", ">=0.0.14-alpha.0 <0.1.0")
    assert not semver.satisfies("0.1.4", ">=0.0.14-alpha.0 <0.1.0")
    assert semver.satisfies("0.1.4", ">=0.1.4")  # open ceiling


def test_range_floor_ceiling():
    r = semver.parse_range(">=1.3.0 <2.0.0")
    assert str(r.floor) == "1.3.0" and str(r.ceiling) == "2.0.0"
    assert semver.parse_range(">=0.1.4").ceiling is None


# ---- the real repo files pass ---------------------------------------------------------------------
def _real_files():
    return (
        vp._load_yaml(REPO_ROOT / "platform-manifest.yaml"),
        vp._load_yaml(REPO_ROOT / "compatibility-matrix.yaml"),
    )


def test_committed_manifest_and_matrix_are_valid():
    manifest, matrix = _real_files()
    requirements = vp._load_json(REPO_ROOT / "certification/protocol-certification-requirements.v1.json")
    f = vp.validate(manifest, matrix, baseline_matrix=None, requirements=requirements)
    assert f.ok, f"committed files must pass structure+coherence, got: {f.errors}"


@pytest.mark.parametrize("section", ["components", "experimental"])
@pytest.mark.parametrize("group", ["contractVersions", "schemaVersions"])
@pytest.mark.parametrize("value", [None, {}, [], {"api": "latest"}, {"api": "^1"}, {"api": 1}, {"api": "TBD"}])
def test_manifest_rejects_malformed_version_declarations(section, group, value):
    manifest, matrix = _real_files()
    name = next(iter(manifest[section]))
    # An explicit empty set is a declaration only for a sourcePinnedOnly row (see the next test).
    manifest[section][name].pop("sourcePinnedOnly", None)
    manifest[section][name][group] = value
    findings = vp.validate(manifest, matrix, None)
    assert any(f"{section}.{name}.{group}" in error for error in findings.errors)


@pytest.mark.parametrize("group", ["contractVersions", "schemaVersions"])
@pytest.mark.parametrize("value", [None, [], {"api": "latest"}, {"api": "TBD"}])
def test_manifest_accepts_only_an_exact_or_explicit_empty_set_for_source_pinned_rows(group, value):
    manifest, matrix = _real_files()
    name, row = next((name, row) for name, row in manifest["experimental"].items() if row.get("sourcePinnedOnly"))
    row[group] = {}
    assert not any(f"experimental.{name}.{group}" in error for error in vp.validate(manifest, matrix, None).errors)
    row[group] = value
    assert any(f"experimental.{name}.{group}" in error for error in vp.validate(manifest, matrix, None).errors)


def test_manifest_rejects_conflicting_database_version():
    manifest, matrix = _real_files()
    manifest["components"]["honua-server"]["schemaVersions"] = {"database": "999999"}
    findings = vp.validate(manifest, matrix, None)
    assert any("database conflicts with dbSchema" in error for error in findings.errors)


SDK_PINS = {
    "sdk-dotnet": "1" * 40,
    "sdk-python": "2" * 40,
    "sdk-js": "3" * 40,
}
SDK_ARTIFACTS = {
    "sdk-dotnet": "honua-sdk-dotnet",
    "sdk-python": "honua-sdk-python-wheel",
    "sdk-js": "honua-sdk-js",
}


def _bound_sdk_fixture():
    manifest, matrix = _real_files()
    manifest["protocolCertification"]["ledger"].update(
        status="bound", commit="a" * 40,
        requirementsSourceRevision="b" * 40, sha256="sha256:" + "c" * 64,
    )
    requirements = {"source_revisions": {}}
    for source, sha in SDK_PINS.items():
        manifest["components"]["honua-" + source]["sha"] = sha
        requirements["source_revisions"][source] = {"commit": sha}
        manifest["clientArtifacts"][SDK_ARTIFACTS[source]]["sourceSha"] = sha
    # Identity-bound imaged components (console, today) carry the label's platform version.
    # An exact candidate refuses a bound image that is still unstamped (R22).
    from platform_version import stamp_platform_version
    stamp_platform_version(manifest, manifest["platformRelease"])
    return manifest, matrix, requirements


def test_bound_ledger_accepts_equal_sdk_catalog_manifest_pins():
    manifest, matrix, requirements = _bound_sdk_fixture()
    f = vp.validate(manifest, matrix, None, requirements=requirements)
    assert f.ok, f.errors


@pytest.mark.parametrize("source", SDK_PINS)
@pytest.mark.parametrize("bad_pin", ["f" * 40, None, "", "trunk", " " + "1" * 40])
def test_bound_ledger_rejects_sdk_catalog_manifest_pin_mismatch(source, bad_pin):
    manifest, matrix, requirements = _bound_sdk_fixture()
    requirements["source_revisions"][source]["commit"] = bad_pin
    f = vp.validate(manifest, matrix, None, requirements=requirements)
    assert not f.ok
    assert any(
        f"source_revisions.{source}.commit" in error
        and f"components.honua-{source}.sha" in error
        for error in f.errors
    )


@pytest.mark.parametrize("source", SDK_PINS)
@pytest.mark.parametrize("producer", [None, [], "invalid", {}])
def test_bound_ledger_rejects_missing_or_malformed_sdk_producer(source, producer):
    manifest, matrix, requirements = _bound_sdk_fixture()
    requirements["source_revisions"][source] = producer
    f = vp.validate(manifest, matrix, None, requirements=requirements)
    assert any(f"source_revisions.{source}.commit" in error for error in f.errors)


@pytest.mark.parametrize("source", SDK_PINS)
@pytest.mark.parametrize("published_sha", ["e" * 40, None])
def test_bound_ledger_rejects_nonshipping_or_missing_provenance(source, published_sha):
    manifest, matrix, requirements = _bound_sdk_fixture()
    artifact = SDK_ARTIFACTS[source]
    manifest["clientArtifacts"][artifact]["sourceSha"] = published_sha
    # The working component and catalog agree; only package provenance differs.
    f = vp.validate(manifest, matrix, None, exact_candidate=True, requirements=requirements)
    assert not f.ok
    assert any(
        f"source_revisions.{source}.commit" in error
        and f"clientArtifacts.{artifact}.sourceSha" in error
        for error in f.errors
    )


def test_historical_dotnet_working_pin_cannot_certify_published_package():
    manifest, matrix, requirements = _bound_sdk_fixture()
    manifest["components"]["honua-sdk-dotnet"]["sha"] = "8e4dd3d9d23f86b7f07d946ef0736d4529d332b6"
    requirements["source_revisions"]["sdk-dotnet"]["commit"] = "8e4dd3d9d23f86b7f07d946ef0736d4529d332b6"
    manifest["clientArtifacts"]["honua-sdk-dotnet"]["sourceSha"] = "a88a7fbb3643cb046e70d6ef4d38ae70a025a2a4"
    f = vp.validate(manifest, matrix, None, exact_candidate=True, requirements=requirements)
    # Pending GA deploy qualification is a separate finding (test_deploy_qualification.py);
    # the stale published-package binding must be the only certification error left.
    certification_errors = [e for e in f.errors if ".architectures." not in e]
    assert certification_errors == [
        "manifest: bound protocol certification ledger requires catalog source_revisions."
        "sdk-dotnet.commit to equal clientArtifacts.honua-sdk-dotnet.sourceSha "
        "(catalog=8e4dd3d9d23f86b7f07d946ef0736d4529d332b6, "
        "published=a88a7fbb3643cb046e70d6ef4d38ae70a025a2a4)"
    ]


def test_548b7a5_candidate_sdk_pins_cannot_bind_the_ledger():
    # 2026-09-15 candidate triple, read independently from each registry and repository:
    # components are the green working pins, the catalog is still the 2026-08-27 rebind,
    # and clientArtifacts are the published bytes (nuspec/gitHead/release tag).
    manifest, matrix, requirements = _bound_sdk_fixture()
    for source, component, catalog, published in (
        ("sdk-dotnet", "6ba49ec32ea846c64bc2094807761d4884dbc4bf",
         "8e4dd3d9d23f86b7f07d946ef0736d4529d332b6", "a88a7fbb3643cb046e70d6ef4d38ae70a025a2a4"),
        ("sdk-python", "40ecf7318573214fb6c702b12ebd56b3ad47ba60",
         "516c727dc03cae3b6b312595a9f4bfcee5f34cad", "f7930b6e9c3ce47ade148bba3d4510eeffd2ccc4"),
        ("sdk-js", "d7cec2d510e053fc86252b125bde21313a7e6e7c",
         "c99e71197dd940ed952aecb024c6de273456f2ae", "c99e71197dd940ed952aecb024c6de273456f2ae"),
    ):
        manifest["components"]["honua-" + source]["sha"] = component
        requirements["source_revisions"][source]["commit"] = catalog
        manifest["clientArtifacts"][SDK_ARTIFACTS[source]]["sourceSha"] = published
    f = vp.validate(manifest, matrix, None, requirements=requirements)
    prefix = "manifest: bound protocol certification ledger requires catalog source_revisions."
    assert [e for e in f.errors if e.startswith(prefix)] == [
        prefix + "sdk-dotnet.commit to equal components.honua-sdk-dotnet.sha "
        "(catalog=8e4dd3d9d23f86b7f07d946ef0736d4529d332b6, manifest=6ba49ec32ea846c64bc2094807761d4884dbc4bf)",
        prefix + "sdk-dotnet.commit to equal clientArtifacts.honua-sdk-dotnet.sourceSha "
        "(catalog=8e4dd3d9d23f86b7f07d946ef0736d4529d332b6, published=a88a7fbb3643cb046e70d6ef4d38ae70a025a2a4)",
        prefix + "sdk-python.commit to equal components.honua-sdk-python.sha "
        "(catalog=516c727dc03cae3b6b312595a9f4bfcee5f34cad, manifest=40ecf7318573214fb6c702b12ebd56b3ad47ba60)",
        prefix + "sdk-python.commit to equal clientArtifacts.honua-sdk-python-wheel.sourceSha "
        "(catalog=516c727dc03cae3b6b312595a9f4bfcee5f34cad, published=f7930b6e9c3ce47ade148bba3d4510eeffd2ccc4)",
        # JS catalog already names the published bytes; only the working component pin differs.
        prefix + "sdk-js.commit to equal components.honua-sdk-js.sha "
        "(catalog=c99e71197dd940ed952aecb024c6de273456f2ae, manifest=d7cec2d510e053fc86252b125bde21313a7e6e7c)",
    ]


def test_bound_ledger_requires_catalog_source_revisions():
    manifest, matrix, _ = _bound_sdk_fixture()
    f = vp.validate(manifest, matrix, None, requirements={})
    assert not f.ok
    assert any("requires catalog source_revisions" in error for error in f.errors)


def test_bound_validation_loads_catalog_when_not_supplied(tmp_path, monkeypatch):
    manifest, matrix, requirements = _bound_sdk_fixture()
    requirements["source_revisions"]["sdk-python"]["commit"] = "f" * 40
    catalog = tmp_path / "requirements.json"
    catalog.write_text(json.dumps(requirements))
    monkeypatch.setattr(vp, "REQUIREMENTS_PATH", catalog)
    f = vp.validate(manifest, matrix, None)
    assert any("source_revisions.sdk-python.commit" in error for error in f.errors)


def test_cli_rejects_bound_catalog_manifest_mismatch(tmp_path, capsys):
    manifest, matrix, requirements = _bound_sdk_fixture()
    requirements["source_revisions"]["sdk-js"]["commit"] = "f" * 40
    for name, value in (("manifest", manifest), ("matrix", matrix), ("requirements", requirements)):
        (tmp_path / (name + ".json")).write_text(json.dumps(value))
    assert vp.main([
        "--manifest", str(tmp_path / "manifest.json"),
        "--matrix", str(tmp_path / "matrix.json"),
        "--requirements", str(tmp_path / "requirements.json"),
    ]) == 1
    assert "source_revisions.sdk-js.commit" in capsys.readouterr().out


def test_pending_ledger_allows_catalog_transition_but_cannot_certify():
    manifest, matrix, requirements = _bound_sdk_fixture()
    manifest["protocolCertification"]["ledger"].update(
        status="pending", commit="pending", requirementsSourceRevision="pending", sha256="pending"
    )
    requirements["source_revisions"]["sdk-python"]["commit"] = "f" * 40
    f = vp.validate(manifest, matrix, None, requirements=requirements)
    assert f.ok, f.errors
    f = vp.validate(manifest, matrix, None, requirements=requirements, exact_candidate=True)
    assert not f.ok
    assert "exact-candidate: protocol certification ledger must be bound before certification" in f.errors


class StubCompareClient:
    def __init__(self, statuses=None, branches=None):
        self.statuses = statuses or {}
        self.branches = branches or {}

    def json(self, path):
        if path.endswith("/branches-where-head"):
            return [{"name": name} for name in self.branches.get(path.split("/commits/")[1].split("/")[0], [])]
        if "/compare/" in path:
            sha = path.rsplit("...", 1)[1]
            return {"status": self.statuses.get(sha, "behind")}
        raise AssertionError(path)


def test_exact_candidate_rejects_historical_off_trunk_server_pin_with_origin():
    """Regression for the real e3ab87ce off-trunk candidate found by PR #194.

    Stubs the manifest's CURRENT server pin as diverged rather than hardcoding
    e3ab87ce: candidate rebinds advance that pin, and a hardcoded sha stops
    matching any manifest pin — the stub then defaults everything to reachable
    and the test dies of StopIteration instead of testing anything.
    """
    manifest, matrix = _real_files()
    server_sha = manifest["components"]["honua-server"]["sha"]
    client = StubCompareClient(
        statuses={server_sha: "diverged"},
        branches={server_sha: ["fix/2026.1-esri-defects"]},
    )
    f = vp.validate(manifest, matrix, None, exact_candidate=True, reachability_client=client)
    error = next(e for e in f.errors if "components.honua-server.sha" in e)
    assert server_sha in error
    assert "honua-io/honua-server" in error
    assert "fix/2026.1-esri-defects" in error


def test_every_required_manifest_pin_family_is_enumerated():
    manifest, _, _ = _bound_sdk_fixture()
    names = {pin.name for pin in tr.manifest_pins(manifest)}
    assert any(name.startswith("components.") for name in names)
    assert any(name.startswith("clientArtifacts.") for name in names)
    assert any(name.startswith("evidenceSources.") for name in names)
    assert "protocolCertification.serverCertificationProducerSha" in names
    assert "protocolCertification.ledger.requirementsSourceRevision" in names
    assert "protocolCertification.ledger.commit" in names


def test_release_evidence_revisions_are_reachability_pins():
    """release#231: a lock fact backed by an off-trunk commit is an off-trunk release fact."""
    manifest, _ = _real_files()
    names = {pin.name for pin in tr.manifest_pins(manifest)}
    assert "platformLockEvidence.contentDigests.geospatialMcp.revision" in names
    declaration = {"repository": "https://github.com/honua-io/geospatial-mcp",
                   "revision": "e" * 40, "path": "spec/schemas/index.json",
                   "sha256": "sha256:" + "f" * 64}
    synthetic = {"platformLockEvidence": {
        "contentDigests": {"catalog": declaration},
        "fixtures": [{"repository": "https://github.com/honua-io/fixtures", "revision": "e" * 40}],
        "notes": {**declaration, "repository": "https://github.com/honua-io/honua-release"}}}
    pins = {pin.name: pin for pin in tr.manifest_pins(synthetic)}
    assert pins["platformLockEvidence.contentDigests.catalog.revision"] == tr.Pin(
        "platformLockEvidence.contentDigests.catalog.revision", "honua-io/geospatial-mcp", "e" * 40)
    assert pins["platformLockEvidence.fixtures[0].revision"].repository == "honua-io/fixtures"
    assert pins["platformLockEvidence.notes.revision"].repository == "honua-io/honua-release"
    with pytest.raises(tr.ReachabilityError, match=r"platformLockEvidence\.contentDigests\.catalog"):
        tr.verify_manifest_pins(synthetic, StubCompareClient(statuses={"e" * 40: "diverged"}))


def test_bound_ledger_commit_is_pinned_to_the_evidence_repository():
    manifest = {
        "protocolCertification": {
            "ledger": {
                "status": "bound",
                "repository": "honua-io/honua-evidence",
                "commit": "d" * 40,
                "requirementsSourceRevision": "pending",
            }
        }
    }
    pins = tr.manifest_pins(manifest)
    assert [p for p in pins if p.name == "protocolCertification.ledger.commit"] == [
        tr.Pin("protocolCertification.ledger.commit", "honua-io/honua-evidence", "d" * 40)
    ]


def test_pending_ledger_commit_is_not_pinned():
    manifest = {
        "protocolCertification": {
            "ledger": {"status": "pending", "commit": "pending"}
        }
    }
    assert not [
        p for p in tr.manifest_pins(manifest) if p.name == "protocolCertification.ledger.commit"
    ]


@pytest.mark.parametrize("status", ["ahead", "diverged", None])
def test_trunk_compare_fails_closed_for_every_non_ancestor_status(status):
    pin = tr.Pin("components.example.sha", "honua-io/example", "b" * 40)
    with pytest.raises(tr.ReachabilityError, match=r"components\.example\.sha=.*honua-io/example"):
        tr.verify_manifest_pins(
            {"components": {"example": {"sha": pin.sha}}},
            StubCompareClient(statuses={pin.sha: status}),
        )


def test_compare_api_failure_names_pin_and_repository():
    class FailedClient:
        def json(self, path):
            raise tr.ReachabilityError("stubbed API unavailable")

    with pytest.raises(
        tr.ReachabilityError,
        match=r"components\.example\.sha=.*honua-io/example.*stubbed API unavailable",
    ):
        tr.verify_manifest_pins(
            {"components": {"example": {"sha": "c" * 40}}}, FailedClient()
        )


def test_committed_manifest_matches_published_json_schema():
    schema = json.loads((REPO_ROOT / "schemas/platform-manifest.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(_real_files()[0])


def test_exact_candidate_rejects_non_trunk_dispatched_ref_fixture():
    manifest, matrix = _real_files()
    fixture = vp._load_yaml(REPO_ROOT / "tools/fixtures/candidate-manifest-non-trunk.yaml")
    manifest = copy.deepcopy(manifest)
    manifest["candidate"] = fixture["candidate"]

    f = vp.validate(manifest, matrix, None, exact_candidate=True)

    assert not f.ok
    assert (
        "exact-candidate: candidate.refSource must be 'trunk'; dispatched ref was "
        "'release/unsafe-candidate'"
    ) in f.errors


def test_exact_candidate_rejects_trunk_claim_pinning_a_different_server_sha():
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    manifest["candidate"]["ref"] = "2222222222222222222222222222222222222222"

    f = vp.validate(manifest, matrix, None, exact_candidate=True)

    assert not f.ok
    assert (
        "exact-candidate: candidate.ref must equal components.honua-server.sha; "
        "candidate.ref=2222222222222222222222222222222222222222 but the pinned server sha is "
        f"{manifest['components']['honua-server']['sha']}"
    ) in f.errors


def test_exact_candidate_binds_the_committed_manifest_ref_to_the_pinned_sha():
    manifest, _ = _real_files()

    assert manifest["candidate"]["ref"] == manifest["components"]["honua-server"]["sha"]


def test_structure_rejects_untrusted_or_unpinned_certification_ledger():
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    ledger = manifest["protocolCertification"]["ledger"]
    ledger.update(
        status="bound",
        repository="attacker/example",
        commit="main",
        requirementsSourceRevision="main",
        sha256="unknown",
    )
    f = vp.validate(manifest, matrix, None)
    assert not f.ok
    assert any("owned by honua-io/honua-evidence" in e for e in f.errors)
    assert any("commit must be a full SHA" in e for e in f.errors)
    assert any("requirementsSourceRevision must be a full SHA" in e for e in f.errors)
    assert any("sha256 must be an exact digest" in e for e in f.errors)


def test_structure_rejects_actor_replayable_or_released_pending_candidate_state():
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    manifest["protocolCertification"]["candidateCutAt"] = "not-a-cut"
    manifest["protocolCertification"]["ledger"].update(
        {
            "status": "pending",
            "commit": "pending",
            "requirementsSourceRevision": "pending",
            "sha256": "pending",
        }
    )
    manifest["status"] = "released"
    f = vp.validate(manifest, matrix, None)
    assert not f.ok
    assert any("candidateCutAt" in e for e in f.errors)
    assert any("released platform cannot" in e for e in f.errors)


def test_structure_rejects_server_certification_producer_that_is_not_the_candidate():
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    manifest["protocolCertification"]["serverCertificationProducerSha"] = "f" * 40
    f = vp.validate(manifest, matrix, None)
    assert not f.ok
    assert any("serverCertificationProducerSha must match" in e for e in f.errors)


# ---- structure rules can fail ---------------------------------------------------------------------
def test_structure_rejects_unknown_client():
    manifest, matrix = _real_files()
    matrix = copy.deepcopy(matrix)
    matrix["contracts"]["geoservices"]["clients"]["honua-sdk-ruby"] = ">=1.0.0"
    f = vp.validate(manifest, matrix, None)
    assert not f.ok and any("unknown client 'honua-sdk-ruby'" in e for e in f.errors)


def test_structure_rejects_bad_range():
    manifest, matrix = _real_files()
    matrix = copy.deepcopy(matrix)
    matrix["contracts"]["geoservices"]["clients"]["honua-sdk-js"] = ">=not.a.version"
    f = vp.validate(manifest, matrix, None)
    assert not f.ok and any("bad range" in e for e in f.errors)


def test_structure_rejects_component_with_no_valid_pin():
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    manifest["components"]["honua-sdk-js"] = {"version": "not-semver"}  # no sha either
    f = vp.validate(manifest, matrix, None)
    assert not f.ok and any("neither a valid semver" in e for e in f.errors)


def test_structure_rejects_client_without_immutable_source_sha():
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    manifest["clientArtifacts"]["honua-sdk-js"]["sourceSha"] = "trunk"
    f = vp.validate(manifest, matrix, None)
    assert not f.ok and any("clientArtifacts.honua-sdk-js.sourceSha" in e for e in f.errors)


def test_structure_keeps_evidence_sources_out_of_components():
    manifest, matrix = _real_files()
    # One repository can both ship a deployable component and publish installable bytes; the
    # records remain independent even when their logical names match.
    assert manifest["clientArtifacts"] is not manifest["components"]
    assert set(manifest["evidenceSources"]).isdisjoint(manifest["components"])
    f = vp.validate(manifest, matrix, None)
    assert f.ok, f.errors


def test_structure_rejects_floating_evidence_producer_ref():
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    manifest["evidenceSources"]["esri-compat"]["producerSha"] = "trunk"
    f = vp.validate(manifest, matrix, None)
    assert not f.ok and any("evidenceSources.esri-compat.producerSha" in e for e in f.errors)


def test_exact_candidate_rejects_local_or_unpublished_client():
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    artifact = manifest["clientArtifacts"]["honua-sdk-js"]
    artifact.update(source="local", publicationState="unpublished")
    artifact.pop("integrity")
    f = vp.validate(manifest, matrix, None, exact_candidate=True)
    assert not f.ok
    assert any("does not name published/promoted bytes" in e for e in f.errors)
    assert any("lacks an immutable digest/integrity pin" in e for e in f.errors)
    assert any("cannot use source=local" in e for e in f.errors)


@pytest.mark.parametrize("source", ["local", "checkout", "build"])
def test_exact_candidate_rejects_every_source_build_fallback(source):
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    manifest["clientArtifacts"]["honua-sdk-js"]["source"] = source
    f = vp.validate(manifest, matrix, None, exact_candidate=True)
    assert not f.ok and any(f"cannot use source={source}" in e for e in f.errors)


def test_exact_candidate_rejects_null_server_image():
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    manifest["components"]["honua-server"]["image"] = None
    f = vp.validate(manifest, matrix, None, exact_candidate=True)
    assert not f.ok and any("requires an image and immutable digest" in e for e in f.errors)


def test_exact_candidate_rejects_required_producer_without_pin():
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    manifest["evidenceSources"]["cite"]["producerSha"] = "trunk"
    f = vp.validate(manifest, matrix, None, exact_candidate=True)
    assert not f.ok and any("lacks a trusted immutable producer pin" in e for e in f.errors)


def test_exact_candidate_accepts_bound_coherent_pins():
    manifest, _, requirements = _bound_sdk_fixture()
    # Pin validity is separate from pending deploy qualification, tested in
    # test_deploy_qualification.py through the full validate() entry point.
    f = vp.Findings()
    vp.check_exact_candidate(manifest, f)
    vp.check_bound_catalog_pin_coherence(manifest, requirements, f)
    assert f.ok, f.errors


def test_exact_candidate_rejects_a_bound_image_with_no_platform_stamp():
    manifest, _, _ = _bound_sdk_fixture()
    del manifest["components"]["honua-console"]["artifactVersion"]
    f = vp.Findings()
    vp.check_exact_candidate(manifest, f)
    assert any(
        "exact-candidate: honua-console.artifactVersion None must be the platform version '2026.1.0-rc.2'"
        in error for error in f.errors), f.errors


def test_legacy_evidence_pin_cannot_drift_from_manifest():
    manifest, _ = _real_files()
    config = {"esri": {"evidenceRef": "f" * 40}}
    f = vp.Findings()
    vp.check_legacy_evidence_pin_coherence(manifest, config, f)
    assert not f.ok and any("evidenceSources.esri-compat" in e for e in f.errors)


def test_structure_requires_explicit_aws_runtime_architectures():
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    del manifest["components"]["honua-server"]["awsEcsArchitecture"]
    f = vp.validate(manifest, matrix, None)
    assert not f.ok and any("awsEcsArchitecture" in e for e in f.errors)


@pytest.mark.parametrize("apply", ["drop", "drop-amd64", "truncate", "string"])
def test_structure_requires_server_amd64_platform_digest(apply):
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    server = manifest["components"]["honua-server"]
    if apply == "drop":
        del server["platformDigests"]
    elif apply == "drop-amd64":
        del server["platformDigests"]["amd64"]
    elif apply == "truncate":
        server["platformDigests"]["amd64"] = "sha256:abcd"
    else:
        server["platformDigests"] = "sha256:" + "a" * 64
    f = vp.validate(manifest, matrix, None)
    assert not f.ok and any("platformDigests.amd64" in e for e in f.errors)


def test_structure_rejects_lambda_architecture_other_than_x86_64():
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    manifest["components"]["honua-server"]["awsLambdaArchitecture"] = "arm64"
    f = vp.validate(manifest, matrix, None)
    assert not f.ok
    assert any("must be x86_64 for the 2026.1 Lambda GA" in error for error in f.errors)


# ---- awsLambdaEcrDigest: a real digest or ONE documented sentinel, nothing else --------------------
@pytest.mark.parametrize("value", [
    "TBD-at-publish",                       # a hand-wave
    "sha256:deadbeef",                      # well-shaped prefix, wrong length
    "pending",                              # near-miss on the sentinel spelling
    "",                                     # absent
])
def test_structure_rejects_non_digest_non_sentinel_ecr_digest(value):
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    manifest["components"]["honua-server"]["awsLambdaEcrDigest"] = value
    f = vp.validate(manifest, matrix, None)
    assert not f.ok and any("awsLambdaEcrDigest" in e for e in f.errors)


def test_structure_accepts_pending_ecr_mirror_sentinel_and_real_digests():
    manifest, matrix = _real_files()
    for value in (vp.PENDING_ECR_MIRROR, "sha256:" + "a" * 64):
        candidate = copy.deepcopy(manifest)
        candidate["components"]["honua-server"]["awsLambdaEcrDigest"] = value
        f = vp.validate(candidate, matrix, None)
        assert f.ok, f"{value!r} must be accepted, got: {f.errors}"


# ---- coherence rules can fail ---------------------------------------------------------------------
def test_coherence_pin_out_of_range_fails():
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    # Bump python past the geoservices ceiling (<0.2.0) without widening the matrix.
    manifest["components"]["honua-sdk-python"]["version"] = "0.2.0"
    f = vp.validate(manifest, matrix, None)
    assert not f.ok and any("does NOT satisfy" in e and "honua-sdk-python" in e for e in f.errors)


def test_coherence_server_sha_mismatch_fails():
    manifest, matrix = _real_files()
    matrix = copy.deepcopy(matrix)
    matrix["deploy"]["honua-iac"]["deploysServerImage"] = "sha:deadbeef"
    f = vp.validate(manifest, matrix, None)
    assert not f.ok and any("pins server sha deadbeef" in e for e in f.errors)


def test_coherence_db_schema_mismatch_fails():
    manifest, matrix = _real_files()
    matrix = copy.deepcopy(matrix)
    matrix["data"]["honua-server"]["requiresDbSchema"] = "metadata-v2"
    f = vp.validate(manifest, matrix, None)
    assert not f.ok and any("requiresDbSchema" in e for e in f.errors)


# ---- drift rules can fail -------------------------------------------------------------------------
def test_drift_narrowing_without_contract_bump_fails():
    manifest, matrix = _real_files()
    baseline = copy.deepcopy(matrix)
    current = copy.deepcopy(matrix)
    # Raise the js floor (drop support for the previously-supported alpha) without bumping version.
    current["contracts"]["geoservices"]["clients"]["honua-sdk-js"] = ">=0.0.20 <0.1.0"
    f = vp.validate(manifest, current, baseline_matrix=baseline)
    assert not f.ok and any("narrowed its support window" in e for e in f.errors)


def test_drift_narrowing_with_contract_bump_is_allowed():
    manifest, matrix = _real_files()
    baseline = copy.deepcopy(matrix)
    current = copy.deepcopy(matrix)
    current["contracts"]["geoservices"]["version"] = "v1"  # contract bumped -> narrowing allowed
    current["contracts"]["geoservices"]["clients"]["honua-sdk-js"] = ">=0.0.20 <0.1.0"
    # Keep the manifest pin coherent with the new floor so only drift is under test.
    manifest = copy.deepcopy(manifest)
    manifest["components"]["honua-sdk-js"]["version"] = "0.0.20"
    f = vp.validate(manifest, current, baseline_matrix=baseline)
    assert f.ok, f"narrowing with a contract bump should pass, got: {f.errors}"


def test_drift_widening_is_always_allowed():
    manifest, matrix = _real_files()
    baseline = copy.deepcopy(matrix)
    current = copy.deepcopy(matrix)
    current["contracts"]["geoservices"]["clients"]["honua-sdk-dotnet"] = ">=1.0.0 <2.0.0"  # widened floor down
    f = vp.validate(manifest, current, baseline_matrix=baseline)
    assert f.ok, f"widening must always pass, got: {f.errors}"


def test_drift_dropping_a_client_without_bump_fails():
    manifest, matrix = _real_files()
    baseline = copy.deepcopy(matrix)
    current = copy.deepcopy(matrix)
    del current["contracts"]["grpc"]["clients"]["honua-sdk-js"]
    f = vp.validate(manifest, current, baseline_matrix=baseline)
    assert not f.ok and any("was dropped from contract" in e for e in f.errors)


if __name__ == "__main__":
    import sys
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{'OK' if not failures else 'FAILED'}: {failures} failure(s)")
    sys.exit(1 if failures else 0)


@pytest.mark.parametrize("section", ["components", "experimental"])
@pytest.mark.parametrize("value", ["false", "true", 1, None])
@pytest.mark.parametrize("empty", [True, False])
def test_source_pinned_only_requires_a_boolean_in_validator_and_schema(section, value, empty):
    manifest, matrix = _real_files()
    name = next(iter(manifest[section]))
    row = manifest[section][name]
    row["sourcePinnedOnly"] = value
    row["contractVersions"] = {} if empty else {"api": "1"}
    row["schemaVersions"] = {} if empty else {"workspace": "1"}
    findings = vp.validate(manifest, matrix, None)
    assert any(f"{section}.{name}.sourcePinnedOnly must be a boolean" in error for error in findings.errors)
    if empty:
        for group in ("contractVersions", "schemaVersions"):
            assert any(f"{section}.{name}.{group}: must be a non-empty mapping" in error for error in findings.errors)
    schema = json.loads((REPO_ROOT / "schemas/platform-manifest.schema.json").read_text())
    errors = list(Draft202012Validator(schema).iter_errors(manifest))
    assert any(list(error.absolute_path) == [section, name, "sourcePinnedOnly"] for error in errors)


@pytest.mark.parametrize("section", ["components", "experimental"])
def test_false_does_not_allow_empty_manifest_version_sets(section):
    manifest, matrix = _real_files()
    name = next(iter(manifest[section]))
    manifest[section][name].update(sourcePinnedOnly=False, contractVersions={}, schemaVersions={})
    findings = vp.validate(manifest, matrix, None)
    assert not any("sourcePinnedOnly must be a boolean" in error for error in findings.errors)
    for group in ("contractVersions", "schemaVersions"):
        assert any(f"{section}.{name}.{group}: must be a non-empty mapping" in error for error in findings.errors)


def _internal_matrix(**row):
    _, matrix = _real_files()
    matrix = copy.deepcopy(matrix)
    matrix["capabilities"]["multi-tenancy"].update(row)
    return matrix


def test_multi_tenancy_is_internal_under_ruling_r29():
    _, matrix = _real_files()
    assert matrix["capabilities"]["multi-tenancy"]["lifecycle"] == "internal"
    assert "internal" in matrix["lifecycle"]


@pytest.mark.parametrize("claim", ["availability", "performance", "support", "qualification", "releaseScope"])
def test_internal_capability_rejects_customer_claims(claim):
    f = vp.Findings()
    vp.check_capability_lifecycle(_internal_matrix(**{claim: "99.9%"}), f)
    assert any("must not carry" in e for e in f.errors), f.errors


def test_internal_capability_is_not_counted_in_the_ga_denominator():
    f = vp.Findings()
    vp.check_capability_lifecycle(_internal_matrix(), f, {"expectedGa": ["serve.wfs", "admin.multi-tenancy"]})
    assert any("counted in the expected-GA manifest" in e for e in f.errors), f.errors


def test_internal_capability_rejects_missing_or_empty_keys():
    for row in ({"capabilityKeys": []},):
        f = vp.Findings()
        vp.check_capability_lifecycle(_internal_matrix(**row), f)
        assert any("at least one capability-matrix key" in e for e in f.errors), f.errors
    matrix = _internal_matrix()
    del matrix["capabilities"]["multi-tenancy"]["capabilityKeys"]
    f = vp.Findings()
    vp.check_capability_lifecycle(matrix, f)
    assert any("at least one capability-matrix key" in e for e in f.errors), f.errors


def test_capability_lifecycle_rejects_unknown_status_and_vocabulary_drift():
    f = vp.Findings()
    vp.check_capability_lifecycle(_internal_matrix(lifecycle="trial"), f)
    assert any("lifecycle must be one of" in e for e in f.errors), f.errors
    matrix = _internal_matrix()
    del matrix["lifecycle"]["internal"]
    f = vp.Findings()
    vp.check_capability_lifecycle(matrix, f)
    assert any("lifecycle must define exactly" in e for e in f.errors), f.errors


# ---- R22: imaged components carry the label's platform version (#231 WI-2) -----------------------
def _stamped_manifest():
    from platform_version import stamp_platform_version
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    manifest["platformRelease"] = "2026.1-rc.3"
    server = manifest["components"]["honua-server"]
    server.setdefault("artifactSourceRevision", server["sha"])
    stamp_platform_version(manifest, "2026.1-rc.3")
    return manifest, matrix


def _r22_errors(manifest, matrix):
    f = vp.Findings()
    vp.check_structure(manifest, matrix, f)
    return [error for error in f.errors if "(R22)" in error or "artifactVersion cannot be checked" in error]


def test_a_stamped_candidate_agrees_with_its_platform_version():
    manifest, matrix = _stamped_manifest()
    assert manifest["components"]["honua-server"]["artifactVersion"] == "2026.1.0-rc.3"
    assert manifest["components"]["honua-console"]["artifactVersion"] == "2026.1.0-rc.3"
    assert _r22_errors(manifest, matrix) == []


@pytest.mark.parametrize("name", ["honua-server", "honua-console"])
@pytest.mark.parametrize("version", ["2026.1.0-rc.2", "2026.1-rc.3", "pre-release", "1.0.0"])
def test_validator_refuses_an_imaged_version_other_than_the_platform_version(name, version):
    manifest, matrix = _stamped_manifest()
    manifest["components"][name]["artifactVersion"] = version
    if name == "honua-server":
        manifest["components"][name]["releaseVersion"] = version
    assert _r22_errors(manifest, matrix) == [
        f"manifest: {name}.artifactVersion {version!r} must be the platform version '2026.1.0-rc.3' "
        "of platformRelease '2026.1-rc.3' (R22)"]


@pytest.mark.parametrize("field", ["digest", "artifactSourceRevision", "platformDigests"])
def test_validator_refuses_a_platform_version_stamped_without_bound_bytes(field):
    manifest, matrix = _stamped_manifest()
    del manifest["components"]["honua-console"][field]
    assert f"manifest: honua-console.artifactVersion is stamped but {field} is not bound (R22)" in \
        _r22_errors(manifest, matrix)


def test_validator_refuses_a_chart_version_without_its_package_checksum():
    manifest, matrix = _stamped_manifest()
    manifest["components"]["honua-helm"]["artifactVersion"] = "2026.1.0-rc.3"
    assert _r22_errors(manifest, matrix) == [
        "manifest: honua-helm.artifactVersion is stamped but digest, artifactSourceRevision, "
        "artifactSha256 are not bound (R22)"]


@pytest.mark.parametrize("release_version", ["2026.1.0-rc.2", "2026.1.0"])
def test_validator_refuses_a_server_release_version_beside_another_artifact_version(release_version):
    manifest, matrix = _stamped_manifest()
    manifest["components"]["honua-server"]["releaseVersion"] = release_version
    assert _r22_errors(manifest, matrix) == [
        f"manifest: honua-server.releaseVersion {release_version!r} must equal its stamped artifactVersion "
        "'2026.1.0-rc.3' (R22)"]
    del manifest["components"]["honua-server"]["artifactVersion"]
    assert len(_r22_errors(manifest, matrix)) == 1


@pytest.mark.parametrize("name", ["honua-server", "honua-console"])
@pytest.mark.parametrize("version", ["2026.1.0-rc.2", "2026.1.0", "1.0.0"])
def test_validator_checks_a_plain_imaged_version_too(name, version):
    """The coordinator follow-up: a plain version key beside the stamp is checked, not ignored."""
    manifest, matrix = _stamped_manifest()
    manifest["components"][name]["version"] = version
    assert _r22_errors(manifest, matrix) == [
        f"manifest: {name}.version {version!r} must be pre-release or the platform version "
        "'2026.1.0-rc.3' of platformRelease '2026.1-rc.3' (R22)"]


def test_validator_refuses_an_rc_version_in_a_ga_manifest():
    from platform_version import stamp_platform_version
    manifest, matrix = _stamped_manifest()
    manifest["platformRelease"] = "2026.1"
    stamp_platform_version(manifest, "2026.1")
    assert _r22_errors(manifest, matrix) == []
    manifest["components"]["honua-console"]["version"] = "2026.1.0-rc.3"
    assert _r22_errors(manifest, matrix) == [
        "manifest: honua-console.version '2026.1.0-rc.3' must be pre-release or the platform version "
        "'2026.1.0' of platformRelease '2026.1' (R22)"]


def test_validator_refuses_a_stamp_when_the_release_names_no_platform_version():
    manifest, matrix = _stamped_manifest()
    manifest["platformRelease"] = "snapshot"
    errors = _r22_errors(manifest, matrix)
    assert len(errors) == 2 and all("platformRelease 'snapshot' is not a platform label" in e for e in errors)


# ---- expected-GA snapshot pin (honua-release#183) -------------------------------------------------
def _committed_manifest_and_snapshot():
    manifest = vp._load_yaml(vp.MANIFEST_PATH)
    return manifest, vp._load_json(vp.EXPECTED_GA_MANIFEST_PATH)


def test_committed_expected_ga_snapshot_is_bound_to_the_pinned_server():
    manifest, snapshot = _committed_manifest_and_snapshot()
    f = vp.Findings()
    vp.check_expected_ga_snapshot_pin(manifest, snapshot, f)
    assert f.ok, f.errors


def test_expected_ga_snapshot_lagging_the_server_pin_fails_validate():
    manifest, snapshot = _committed_manifest_and_snapshot()
    advanced = copy.deepcopy(manifest)
    advanced["components"]["honua-server"]["sha"] = "f" * 40
    f = vp.Findings()
    vp.check_expected_ga_snapshot_pin(advanced, snapshot, f)
    assert any("lags components.honua-server.sha" in e for e in f.errors), f.errors

    stale = copy.deepcopy(snapshot)
    stale["sourceSnapshot"]["target"] = "local-docker ghcr.io/honua-io/honua-server@sha256:" + "0" * 64
    f = vp.Findings()
    vp.check_expected_ga_snapshot_pin(manifest, stale, f)
    assert any("does not name the pinned honua-server digest" in e for e in f.errors), f.errors

    inferred = copy.deepcopy(snapshot)
    inferred["sourceSnapshot"]["deploymentRevisionSource"] = "manifest"
    f = vp.Findings()
    vp.check_expected_ga_snapshot_pin(manifest, inferred, f)
    assert any("must be 'commit-sha'" in e for e in f.errors), f.errors

    for missing in (None, {"expectedGa": []}):
        f = vp.Findings()
        vp.check_expected_ga_snapshot_pin(manifest, missing, f)
        assert not f.ok


def test_validate_cli_fails_when_the_snapshot_lags(monkeypatch, tmp_path, capsys):
    manifest, snapshot = _committed_manifest_and_snapshot()
    lagging = copy.deepcopy(snapshot)
    lagging["sourceSnapshot"]["deploymentRevision"] = "87966c3f7b6c840ffc4d4da0b451714ab717b18a"
    path = tmp_path / "expected-ga-manifest.json"
    path.write_text(json.dumps(lagging), encoding="utf-8")
    monkeypatch.setattr(vp, "EXPECTED_GA_MANIFEST_PATH", path)
    assert vp.main([]) != 0
    assert "lags components.honua-server.sha" in capsys.readouterr().out
