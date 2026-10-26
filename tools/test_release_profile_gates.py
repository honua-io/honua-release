"""Release-profile exclusions, unassigned gap rows, matrix maturity, and the Python fixture."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CERTIFICATION = ROOT / "certification"
CATALOG = "protocol-certification-requirements.v1.json"
DEMOTED = ("alerts.evaluation", "fieldops.offline-sync", "serve.sensorthings")
PYTHON_CLIENT = "Honua SDK Python"
PYTHON_VERSION = "0.1.18"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def run(candidate: Path, name: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(candidate / name)],
        capture_output=True, text=True, timeout=120,
    )


@pytest.fixture
def copied(tmp_path: Path) -> Path:
    target = tmp_path / "certification"
    shutil.copytree(CERTIFICATION, target, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "platform-manifest.yaml", tmp_path / "platform-manifest.yaml")
    return target


def test_dotnet_release_profile_is_the_catalog_denominator() -> None:
    contract = load(CERTIFICATION / "sources/sdk-dotnet/sdk-certification.v1.json")
    catalog = load(CERTIFICATION / CATALOG)
    excluded = {entry["operationId"] for entry in contract["releaseProfile"]["excluded"]}
    included = {
        operation["id"] for operation in contract["operations"]
        if operation["releaseDenominator"] == "included"
    }
    rows = [
        row for row in catalog["requirements"]
        if row["client_lane"] == "sdk-dotnet-certification"
    ]
    # Honua.Sdk 1.12.0 (aada6d89) includes the 14 operations 1.11.x excluded as candidate fixtures.
    assert contract["releaseProfile"]["denominator"] == 306
    assert len(excluded) == 0
    assert len(rows) == len(included) == 306
    assert {row["operation"] for row in rows} == included
    assert excluded.isdisjoint(included)
    assert excluded.isdisjoint({row["operation"] for row in rows})


def test_dotnet_profile_disagreement_refuses_generation(copied: Path) -> None:
    path = copied / "sources/sdk-dotnet/sdk-certification.v1.json"
    contract = load(path)
    # Name an included operation as excluded without changing its own denominator.
    operation = next(row for row in contract["operations"] if row["releaseDenominator"] == "included")
    operation_id = operation["id"]
    contract["releaseProfile"]["excluded"].append({
        "operationId": operation_id,
        "ownerIssue": "https://github.com/honua-io/honua-sdk-dotnet/issues/308",
        "reason": "Synthetic profile disagreement for the regression.",
    })
    write(path, contract)

    result = run(copied, "generate-protocol-requirements.py")

    assert result.returncode != 0
    assert "disagrees with releaseProfile.excluded" in result.stderr
    assert operation_id in result.stderr


def test_moving_an_included_dotnet_operation_out_of_the_profile_omits_it(copied: Path) -> None:
    path = copied / "sources/sdk-dotnet/sdk-certification.v1.json"
    contract = load(path)
    operation = next(
        row for row in contract["operations"] if row["releaseDenominator"] == "included"
    )
    operation["releaseDenominator"] = "excluded"
    contract["releaseProfile"]["excluded"].append({
        "operationId": operation["id"],
        "ownerIssue": "https://github.com/honua-io/honua-sdk-dotnet/issues/308",
        "reason": "Synthetic release-profile exclusion for the denominator regression.",
    })
    contract["releaseProfile"]["denominator"] -= 1
    write(path, contract)

    generated = run(copied, "generate-protocol-requirements.py")
    assert generated.returncode == 0, generated.stderr
    catalog = load(copied / CATALOG)
    rows = [
        row for row in catalog["requirements"]
        if row["client_lane"] == "sdk-dotnet-certification"
    ]
    assert operation["id"] not in {row["operation"] for row in rows}
    assert len(rows) == contract["releaseProfile"]["denominator"] == 305


@pytest.mark.parametrize("field,value", [
    ("required_tier", "release"),
    ("fixture_revision", "docker/cng/seed.sql@drifted"),
    ("auth_policy_revision", "anonymous-public-v1"),
    ("scenario_facets", ["positive", "metadata"]),
    ("maturity", "preview"),
])
def test_unassigned_gap_drift_fails_validation(copied: Path, field: str, value: object) -> None:
    # Server 8cb074f7 assigns harness rows for serve.wps and analytics.h3, so the
    # catalog carries no real gap row; inject the governed one, then drift it.
    path = copied / CATALOG
    catalog = load(path)
    row = governed_gap_row(copied, "serve.wps")
    row[field] = value
    catalog["requirements"].append(row)
    write(path, catalog)

    result = run(copied, "validate-protocol-requirements.py")

    assert result.returncode != 0
    assert "serve.wps" in result.stderr
    assert field in result.stderr


def governed_gap_row(copied: Path, capability: str) -> dict:
    spec = importlib.util.spec_from_file_location("copied_validator", copied / "validate-protocol-requirements.py")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    return validator._unassigned_gap_row(capability, validator._unassigned_gap_contract(capability))


def test_assigned_harness_capability_cannot_also_carry_a_gap_row(copied: Path) -> None:
    path = copied / CATALOG
    catalog = load(path)
    assert not [row for row in catalog["requirements"]
                if row["operation"].startswith("UNASSIGNED PROTOCOL HARNESS CONTRACT:")]
    catalog["requirements"].append(governed_gap_row(copied, "serve.wps"))
    write(path, catalog)

    result = run(copied, "validate-protocol-requirements.py")

    assert result.returncode != 0
    assert "coexist with explicit unassigned contracts" in result.stderr
    assert "serve.wps" in result.stderr


def test_demoted_matrix_capabilities_have_no_supported_rows() -> None:
    catalog = load(CERTIFICATION / CATALOG)
    present = {row["capability_key"] for row in catalog["requirements"]}
    assert not present.intersection(DEMOTED)


def test_generator_drops_rows_when_matrix_maturity_is_not_implemented(copied: Path) -> None:
    matrix_path = copied / "sources/server/capability-matrix.v1.json"
    matrix = load(matrix_path)
    capability = next(row for row in matrix["capabilities"] if row["key"] == "alerts.evaluation")
    original = json.loads(json.dumps(capability["maturity"]))
    count = sum(original.values()) or 1
    capability["maturity"] = {"implemented": count}
    write(matrix_path, matrix)
    promoted = run(copied, "generate-protocol-requirements.py")
    assert promoted.returncode == 0, promoted.stderr
    promoted_rows = [
        row for row in load(copied / CATALOG)["requirements"]
        if row["capability_key"] == "alerts.evaluation"
    ]
    assert promoted_rows
    assert {row["maturity"] for row in promoted_rows} == {"supported"}

    capability["maturity"] = original
    write(matrix_path, matrix)
    demoted = run(copied, "generate-protocol-requirements.py")
    assert demoted.returncode == 0, demoted.stderr
    catalog = load(copied / CATALOG)
    assert not [
        row for row in catalog["requirements"] if row["capability_key"] in DEMOTED
    ]
    validated = run(copied, "validate-protocol-requirements.py")
    assert validated.returncode == 0, validated.stderr


def test_a_supported_row_on_an_unpromoted_capability_fails_validation(copied: Path) -> None:
    generated = run(copied, "generate-protocol-requirements.py")
    assert generated.returncode == 0, generated.stderr
    path = copied / CATALOG
    catalog = load(path)
    donor = next(row for row in catalog["requirements"] if row["client_lane"] == "sdk-js")
    injected = dict(donor)
    injected.update(
        capability_key="alerts.evaluation",
        surface="alerts-evaluation",
        operation="synthetic-preview-row",
        maturity="supported",
    )
    catalog["requirements"].append(injected)
    write(path, catalog)

    result = run(copied, "validate-protocol-requirements.py")

    assert result.returncode != 0
    assert "alerts.evaluation" in result.stderr
    assert "has not implemented" in result.stderr


def test_python_coverage_uses_the_pinned_contract_fixture() -> None:
    contract = load(CERTIFICATION / "sources/sdk-python/protocol-certification.v1.json")
    catalog = load(CERTIFICATION / CATALOG)
    fixture = contract["fixtureRevision"]
    assert fixture == "geospatial-grpc@1.0.3"
    rows = [
        row for row in catalog["requirements"]
        if row["canonical_client"] == PYTHON_CLIENT and row["client_version"] == PYTHON_VERSION
    ]
    assert {row["client_lane"] for row in rows} >= {"sdk-python", "sdk-python-certification"}
    assert {row["fixture_revision"] for row in rows} == {fixture}
    assert "geospatial-grpc@0.2.0-alpha.1" not in {row["fixture_revision"] for row in rows}


def test_python_fixture_revision_moves_coverage_and_certification_together(copied: Path) -> None:
    path = copied / "sources/sdk-python/protocol-certification.v1.json"
    contract = load(path)
    contract["fixtureRevision"] = "geospatial-grpc@9.9.9"
    write(path, contract)

    generated = run(copied, "generate-protocol-requirements.py")
    assert generated.returncode == 0, generated.stderr
    rows = [
        row for row in load(copied / CATALOG)["requirements"]
        if row["canonical_client"] == PYTHON_CLIENT and row["client_version"] == PYTHON_VERSION
    ]
    by_lane = {row["client_lane"] for row in rows}
    assert {"sdk-python", "sdk-python-certification"} <= by_lane
    assert {row["fixture_revision"] for row in rows} == {"geospatial-grpc@9.9.9"}


def test_blank_python_fixture_revision_refuses_generation(copied: Path) -> None:
    path = copied / "sources/sdk-python/protocol-certification.v1.json"
    contract = load(path)
    contract["fixtureRevision"] = "  "
    write(path, contract)

    result = run(copied, "generate-protocol-requirements.py")

    assert result.returncode != 0
    assert "missing fixtureRevision" in result.stderr


SEED = "sources/canonical-client-fixtures.v1.json"
APPLIED_SEED = "sha256:522454f3de0247fa5f8044a7bbc906ba57e44c34f2cece602c91688ee0e1cd63"


def test_dotnet_certification_rows_carry_the_applied_server_seed():
    """The .NET producer binds the seed it applied at the candidate; the rows must carry the same digest."""
    catalog = load(CERTIFICATION / CATALOG)
    seed = load(CERTIFICATION / SEED)["server_seed"]
    server = load(CERTIFICATION / "sources/source-revisions.v1.json")["sources"]["server-certification"]
    assert seed["commit"] == server["commit"]
    assert seed["sha256"] == APPLIED_SEED  # tests/seed/base-schema.sql at 8cb074f7
    fixtures = {
        row["fixture_revision"] for row in catalog["requirements"]
        if row["client_lane"] == "sdk-dotnet-certification"
    }
    assert fixtures == {APPLIED_SEED}


def test_server_seed_cannot_lag_the_server_certification_pin(copied: Path):
    path = copied / SEED
    doc = load(path)
    doc["server_seed"]["commit"] = "a" * 40
    write(path, doc)
    result = run(copied, "generate-protocol-requirements.py")
    assert result.returncode != 0
    assert "re-pin the seed digest in the same rebind" in result.stderr
