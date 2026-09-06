"""Qualification policy tests; receipt references here are synthetic, never execution evidence."""
import copy

import pytest

import validate_platform as vp


@pytest.fixture
def candidate():
    manifest = vp._load_yaml(vp.MANIFEST_PATH)
    matrix = vp._load_yaml(vp.MATRIX_PATH)
    row = matrix["deploy"]["honua-server"]["awsLambda"]["architectures"]["x86_64"]
    return manifest, matrix, row


def qualify(manifest, row):
    row.update(status="supported", qualification="passed", qualificationReceipt={
        "url": "https://example.invalid/test-only/lambda-receipt.json",
        "candidateManifestDigest": vp.qualification_candidate_digest(manifest),
    })


@pytest.mark.parametrize("qualification", ["pending", "failed", "fabricated", None])
def test_supported_without_passed_qualification_rejected(candidate, qualification):
    manifest, matrix, row = candidate
    qualify(manifest, row)
    row["qualification"] = qualification
    result = vp.validate(manifest, matrix, None)
    assert not result.ok
    assert any("supported requires qualification: passed" in error for error in result.errors)


def test_ga_target_pending_accepted(candidate):
    manifest, matrix, row = candidate
    assert row["status"] == "ga-target" and row["qualification"] == "pending"
    assert vp.validate(manifest, matrix, None).ok


def test_supported_passed_receipt_accepted(candidate):
    manifest, matrix, row = candidate
    qualify(manifest, row)
    result = vp.validate(manifest, matrix, None)
    assert result.ok, result.errors


@pytest.mark.parametrize("receipt", [None, "https://example.invalid/receipt.json", {}])
def test_supported_without_receipt_rejected(candidate, receipt):
    manifest, matrix, row = candidate
    row.update(status="supported", qualification="passed", qualificationReceipt=receipt)
    result = vp.validate(manifest, matrix, None)
    assert not result.ok
    assert any("qualificationReceipt" in error for error in result.errors)


@pytest.mark.parametrize("field", ["sha", "digest", "awsLambdaDigest", "awsLambdaEcrDigest"])
def test_supported_wrong_candidate_receipt_rejected(candidate, field):
    manifest, matrix, row = candidate
    qualify(manifest, row)
    # Deliberate mutation after reference binding, not a fabricated release pin.
    manifest["components"]["honua-server"][field] += "-changed"
    result = vp.validate(manifest, matrix, None)
    assert not result.ok
    assert any("missing or wrong-candidate receipt" in error for error in result.errors)


@pytest.mark.parametrize("url", [None, "", "pending", "http://example.invalid/receipt", "https://example.invalid"])
def test_supported_invalid_receipt_reference_rejected(candidate, url):
    manifest, matrix, row = candidate
    qualify(manifest, row)
    row["qualificationReceipt"]["url"] = url
    result = vp.validate(manifest, matrix, None)
    assert not result.ok
    assert any("must reference an HTTPS receipt" in error for error in result.errors)


def test_exact_candidate_pending_ga_target_rejected(candidate):
    manifest, matrix, _ = candidate
    result = vp.validate(manifest, matrix, None, exact_candidate=True)
    assert any("GA target, qualification pending" in error for error in result.errors)


def test_exact_candidate_passed_ga_target_clears_qualification_gate(candidate):
    manifest, matrix, row = candidate
    qualify(manifest, row)
    result = vp.Findings()
    vp.check_deploy_qualification(manifest, matrix, result, exact_candidate=True)
    assert result.ok, result.errors


def test_receipt_binding_covers_other_candidate_components(candidate):
    manifest, matrix, row = candidate
    qualify(manifest, row)
    changed = copy.deepcopy(manifest)
    changed["components"]["honua-iac"]["sha"] += "-changed"
    result = vp.validate(changed, matrix, None)
    assert any("missing or wrong-candidate receipt" in error for error in result.errors)
