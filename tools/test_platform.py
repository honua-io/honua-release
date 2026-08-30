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


def test_bound_ledger_rejects_sdk_catalog_manifest_pin_mismatch():
    manifest, matrix = _real_files()
    requirements = vp._load_json(REPO_ROOT / "certification/protocol-certification-requirements.v1.json")
    requirements = copy.deepcopy(requirements)
    requirements["source_revisions"]["sdk-python"]["commit"] = "f" * 40

    f = vp.validate(manifest, matrix, baseline_matrix=None, requirements=requirements)

    assert not f.ok
    assert any(
        "source_revisions.sdk-python.commit" in error
        and "components.honua-sdk-python.sha" in error
        for error in f.errors
    )


def test_pending_ledger_allows_catalog_manifest_pin_transition():
    manifest, matrix = _real_files()
    manifest = copy.deepcopy(manifest)
    manifest["protocolCertification"]["ledger"].update(
        status="pending", commit="pending", requirementsSourceRevision="pending", sha256="pending"
    )
    requirements = vp._load_json(REPO_ROOT / "certification/protocol-certification-requirements.v1.json")
    requirements = copy.deepcopy(requirements)
    requirements["source_revisions"]["sdk-python"]["commit"] = "f" * 40

    f = vp.validate(manifest, matrix, baseline_matrix=None, requirements=requirements)

    assert not any("source_revisions.sdk-python.commit" in error for error in f.errors)


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
    manifest, _ = _real_files()
    names = {pin.name for pin in tr.manifest_pins(manifest)}
    assert any(name.startswith("components.") for name in names)
    assert any(name.startswith("clientArtifacts.") for name in names)
    assert any(name.startswith("evidenceSources.") for name in names)
    assert "protocolCertification.serverCertificationProducerSha" in names
    assert "protocolCertification.ledger.requirementsSourceRevision" in names
    assert "protocolCertification.ledger.commit" in names


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


def test_exact_candidate_accepts_committed_pins():
    manifest, matrix = _real_files()
    f = vp.validate(manifest, matrix, None, exact_candidate=True)
    assert f.ok, f.errors


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
