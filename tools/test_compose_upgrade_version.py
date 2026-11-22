"""Admission must reject wrong-version bytes even when the upgrade itself succeeds."""
import json
from pathlib import Path

import pytest
import yaml

import compose_upgrade_prior as cu
from compose_upgrade_version import version_evidence
from test_compose_upgrade_maps import proof as map_proof
from test_compose_upgrade_ga_baseline import resolve_ga
from test_compose_upgrade_prior import harness_identity, rc


def evidence():
    pair = resolve_ga([rc("2026.1-rc.2", "2026-11-20T00:00:00Z", "2")])
    identity = harness_identity(pair["candidate"], pair["candidate"].rsplit("@", 1)[1])
    identity["runtimeIdentity"]["observedVersion"] = "2026.1.0"
    identity["imageVersion"] = "2026.1.0"
    return pair, {"candidate": identity}


def receipt(pair, phases, **overrides):
    inputs = dict(scenario="normal", status="pass", why="upgrade succeeded", observed={"maps": map_proof(), "credentials": {"schema": 1, "status": "pass",
                      "connectionId": "00000000-0000-0000-0000-000000000001",
                      "apiKeyId": "00000000-0000-0000-0000-000000000002",
                      "checks": dict.fromkeys(("connectionUnchanged", "storedCredentialHealthy",
                                               "existingApiKeyAuthenticated", "apiKeyUnchanged"), True)}},
                  phases=phases, harness={}, run="run", commit="commit")
    inputs.update(overrides)
    return cu.build_receipt(pair, **inputs)


def test_ga_requires_normalized_version_from_the_selected_manifest():
    pair, phases = evidence()
    assert pair["candidate_version"] == "2026.1.0"
    result = receipt(pair, phases)
    assert result["status"] == "pass"
    assert result["versionEvidence"] == {
        "status": "pass", "expectedVersion": "2026.1.0", "runtimeVersion": "2026.1.0",
        "imageVersion": "2026.1.0", "why": "installed runtime and OCI versions match the candidate"}


@pytest.mark.parametrize("value", [None, "", "2026.1.0-rc.2", "2026.1.1", "2026.1", " 2026.1.0", 2026])
@pytest.mark.parametrize("surface", ["runtime", "oci"])
def test_missing_stale_or_inexact_versions_fail_admission(surface, value):
    pair, phases = evidence()
    if surface == "runtime":
        phases["candidate"]["runtimeIdentity"]["observedVersion"] = value
    else:
        phases["candidate"]["imageVersion"] = value
    result = receipt(pair, phases)
    assert result["status"] == "fail"
    assert result["why"].startswith("candidate version refused:")
    assert "upgrade succeeded" in result["why"]
    assert result["versionEvidence"]["status"] == "fail"


@pytest.mark.parametrize("field,value", [
    ("imageID", ""), ("imageID", "other-image"), ("expectedImageID", None),
    ("platform", "linux/arm64"), ("runtimeIdentity", None),
])
def test_a_matching_version_from_the_wrong_container_does_not_certify(field, value):
    pair, phases = evidence()
    phases["candidate"][field] = value
    assert receipt(pair, phases)["status"] == "fail"


@pytest.mark.parametrize("field", ["imageReference", "imageDigest"])
def test_another_image_cannot_lend_its_version_to_the_candidate(field):
    pair, phases = evidence()
    phases["candidate"]["runtimeIdentity"][field] = "sha256:" + "f" * 64
    assert receipt(pair, phases)["status"] == "fail"


@pytest.mark.parametrize("reference", [None, "server:latest", "server@sha256:bad"])
def test_candidate_reference_requires_a_complete_immutable_digest(reference):
    pair, phases = evidence()
    pair["candidate"] = reference
    assert receipt(pair, phases)["status"] == "fail"


@pytest.mark.parametrize("phase", [None, [], "invalid"])
def test_malformed_or_absent_candidate_identity_fails_closed(phase):
    pair, _ = evidence()
    assert receipt(pair, {"candidate": phase})["status"] == "fail"


def test_install_and_rollback_observations_cannot_replace_candidate_evidence():
    pair, phases = evidence()
    assert receipt(pair, {"install": phases["candidate"], "rollback": phases["candidate"]})["status"] == "fail"


@pytest.mark.parametrize("expected", ["garbage", "2026.1", "honua-2026.1.0", [], 2026])
def test_malformed_expected_version_is_a_failure_rather_than_an_exception(expected):
    pair, phases = evidence()
    pair["candidate_version"] = expected
    assert receipt(pair, phases)["status"] == "fail"


@pytest.mark.parametrize("status", ["fail", "blocked"])
def test_version_match_cannot_promote_a_failed_or_blocked_upgrade(status):
    pair, phases = evidence()
    assert receipt(pair, phases, status=status)["status"] == status


def test_negative_control_is_still_judged_by_the_original_harness_assertion():
    pair, _ = evidence()
    result = receipt(pair, {}, scenario="negative-control", why="injected seed loss caught")
    assert result["status"] == "pass"
    assert result["why"] == "injected seed loss caught"
    assert result["versionEvidence"]["status"] == "fail"


def test_unversioned_rc_and_nightly_pairs_retain_existing_admission():
    pair, _ = evidence()
    del pair["candidate_version"]
    assert version_evidence(pair, {})["status"] == "not-applicable"
    assert receipt(pair, {})["status"] == "pass"


def test_cli_writes_failed_receipt_for_successful_harness_with_stale_oci_version(tmp_path):
    pair, phases = evidence()
    phases["candidate"]["imageVersion"] = "2026.1.0-rc.2"
    pair_path = tmp_path / "pair.json"
    pair_path.write_text(json.dumps(pair))
    (tmp_path / "compose-candidate-identity.json").write_text(json.dumps(phases["candidate"]))
    output = tmp_path / "receipt.json"
    assert cu.main(["receipt", "--pair", str(pair_path), "--scenario", "normal", "--status", "pass",
                    "--phase-dir", str(tmp_path), "--output", str(output)]) == 0
    result = json.loads(output.read_text())
    assert result["status"] == "fail"
    assert result["versionEvidence"]["imageVersion"] == "2026.1.0-rc.2"


@pytest.mark.parametrize("status", ["fail", "pass", "blocked"])
def test_workflow_propagates_unbound_version_refusal_to_required_fragment(tmp_path, status):
    """Run the receipt step with a failing receipt and no qualification lock supplied."""
    import subprocess

    workflow = yaml.safe_load(Path(".github/workflows/gate-upgrade.yml").read_text())
    steps = workflow["jobs"]["compose-upgrade"]["steps"]
    run = next(step["run"] for step in steps if "compose_upgrade_prior.py receipt" in step.get("run", ""))
    # Exercise the actual status propagation block against a temporary receipt; no Docker or dispatch.
    start = run.index("          # A version refusal") if "          # A version refusal" in run else run.index("# A version refusal")
    script = run[start:run.index("cat /tmp/cu/compose-upgrade-receipt.json")]
    failed = tmp_path / "receipt.json"
    failed.write_text(json.dumps({"status": status, "why": "candidate version refused"}))
    script = script.replace("/tmp/cu/compose-upgrade-receipt.json", str(failed))
    envfile = tmp_path / "env"
    subprocess.run(["bash", "-euc", script], env={"GITHUB_ENV": str(envfile)}, check=True)
    if status == "fail":
        assert "compose_status=fail" in envfile.read_text()
        assert "compose_why=candidate version refused" in envfile.read_text()
    else:
        assert not envfile.exists()
