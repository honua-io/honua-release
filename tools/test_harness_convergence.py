"""Governed server operations survive catalog growth and candidate demotion."""
from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("harness_rebind", ROOT / "tools/convergence_rebind.py")
REBIND = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REBIND)
HARNESS = "sources/server/protocol-harness-assignments.v1.json"
CATALOG = "protocol-certification-requirements.v1.json"


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


@pytest.fixture
def candidate(tmp_path):
    target = tmp_path / "certification"
    shutil.copytree(ROOT / "certification", target, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "platform-manifest.yaml", tmp_path / "platform-manifest.yaml")
    # Byte-identical actual server 00a23a8c62f2e0ba5dd03244cdcf9f205790cbff contract.
    data = (ROOT / "tools/fixtures/protocol-harness-2026-08-26.1.json").read_bytes()
    assert len(read(ROOT / "tools/fixtures/protocol-harness-2026-08-26.1.json")["assignments"]) == 42
    (target / HARNESS).write_bytes(data)
    return target


def run(candidate, name):
    return subprocess.run([sys.executable, str(candidate / name)], capture_output=True, text=True, timeout=60)


def generate(candidate):
    result = run(candidate, "generate-protocol-requirements.py")
    assert result.returncode == 0, result.stderr


def validate(candidate):
    return run(candidate, "validate-protocol-requirements.py")


def test_all_42_actual_governed_operations_include_ten_added_writes(candidate):
    generate(candidate)
    result = validate(candidate)
    assert result.returncode == 0, result.stderr
    expected = read(candidate / HARNESS)["assignments"]
    rows = [row for row in read(candidate / CATALOG)["requirements"] if row["client_lane"] == "server-protocol-harness"]
    assert len(rows) == 42
    assert {(r["capability_key"], r["surface"], r["operation"], tuple(r["test_ids"]), tuple(r["scenario_facets"])) for r in rows} == {
        (a["capability_key"], a["surface"], a["operation"], tuple(a["test_ids"]), tuple(a["scenario_facets"])) for a in expected
    }
    writes = [a for a in expected if a["capability_key"] in {"editing.featureserver-edits", "serve.ogc-api-features", "serve.wfs"}]
    assert len(writes) == 10


@pytest.mark.parametrize("mutation", ["omit", "extra", "test-id", "facet", "preview-row"])
def test_exact_catalog_denominator_remains_required(candidate, mutation):
    generate(candidate)
    catalog = read(candidate / CATALOG)
    row = next(r for r in catalog["requirements"] if r["client_lane"] == "server-protocol-harness" and r["operation"].endswith("/addFeatures"))
    if mutation == "omit":
        catalog["requirements"].remove(row)
    elif mutation == "extra":
        extra = copy.deepcopy(row)
        extra["operation"] += "Unassigned"
        catalog["requirements"].append(extra)
    elif mutation == "test-id":
        row["test_ids"] = ["Different.Test"]
    elif mutation == "facet":
        row["scenario_facets"] = ["positive"]
    else:
        matrix = read(candidate / "sources/server/capability-matrix.v1.json")
        capability = next(r for r in matrix["capabilities"] if r["key"] == "editing.featureserver-edits")
        capability["maturity"] = {"preview": 1}
        write(candidate / "sources/server/capability-matrix.v1.json", matrix)
    write(candidate / CATALOG, catalog)
    result = validate(candidate)
    assert result.returncode != 0
    assert "denominator differs from the governed server harness contract" in result.stderr


@pytest.mark.parametrize("mutation", ["unknown", "duplicate", "gap"])
def test_bad_contract_still_refused(candidate, mutation):
    harness = read(candidate / HARNESS)
    if mutation == "unknown":
        harness["assignments"][0]["capability_key"] = "serve.unknown-synthetic"
    elif mutation == "duplicate":
        harness["assignments"].append(copy.deepcopy(harness["assignments"][0]))
    else:
        harness["assignments"] = [a for a in harness["assignments"] if a["capability_key"] != "ai.grounding"]
    write(candidate / HARNESS, harness)
    generated = run(candidate, "generate-protocol-requirements.py")
    if generated.returncode == 0:
        assert validate(candidate).returncode != 0


def test_projection_tracks_implemented_surface_without_altering_other_policy(candidate):
    policy_path = candidate / "sources/official-sdk-protocol-assignments.v1.json"
    old = read(policy_path)
    old_bytes = policy_path.read_bytes()
    applicability_path = candidate / "sources/canonical-client-applicability.v1.json"
    original_applicability = applicability_path.read_bytes()
    path, data = REBIND.project_official_protocol_assignments(candidate.parent)
    assert data == old_bytes
    matrix_path = candidate / "sources/server/capability-matrix.v1.json"
    matrix = read(matrix_path)
    demoted = {"serve.ogc-api-coverages", "serve.ogc-api-edr", "serve.wmts", "serve.sensorthings", "alerts.evaluation"}
    for row in matrix["capabilities"]:
        if row["key"] in demoted:
            row["maturity"] = {"preview": 1}
            row["status"] = "preview"
    write(matrix_path, matrix)
    path, data = REBIND.project_official_protocol_assignments(candidate.parent)
    new = json.loads(data)
    assert set(new["capabilities"]) == set(old["capabilities"]) - demoted
    assert {k: v for k, v in old.items() if k != "capabilities"} == {k: v for k, v in new.items() if k != "capabilities"}
    assert policy_path.read_bytes() == old_bytes
    (candidate.parent / path).write_bytes(data)
    assert applicability_path.read_bytes() == original_applicability
    generate(candidate)
    result = validate(candidate)
    assert result.returncode == 0, result.stderr
    catalog = read(candidate / CATALOG)
    stale = copy.deepcopy(catalog["requirements"][0])
    stale.update(capability_key="alerts.evaluation", surface="stale-preview",
                 operation="stale-preview", contract_revision="canonical-client-applicability@" + read(applicability_path)["revision"])
    catalog["requirements"].append(stale)
    write(candidate / CATALOG, catalog)
    result = validate(candidate)
    assert result.returncode != 0
    assert "generated row for a nonimplemented decision" in result.stderr


@pytest.mark.parametrize("mutation", ["unknown", "missing"])
def test_applicability_cannot_invent_or_omit_implemented_decisions(candidate, mutation):
    path = candidate / "sources/canonical-client-applicability.v1.json"
    policy = read(path)
    if mutation == "unknown":
        policy["decisions"].append({"capability_key": "synthetic.unknown", "classification": "official-sdk-required"})
    else:
        policy["decisions"] = [row for row in policy["decisions"] if row["capability_key"] != "ai.grounding"]
    write(path, policy)
    generate(candidate)
    result = validate(candidate)
    assert result.returncode != 0
    expected = "names unknown capabilities" if mutation == "unknown" else "unclassified=['ai.grounding']"
    assert expected in result.stderr


def change_client_versions(candidate):
    versions = {"sdk-js": "0.2.0", "sdk-python": "0.1.16", "sdk-dotnet": "1.11.0"}
    for source in ("sdk-js", "sdk-python"):
        path = candidate / "sources" / source / "protocol-certification.v1.json"
        contract = read(path)
        contract["clientVersion"] = versions[source]
        write(path, contract)
    path = candidate / "sources/sdk-js/sdk-coverage.v1.json"
    coverage = read(path)
    coverage["sdk"]["version"] = versions["sdk-js"]
    write(path, coverage)
    write(candidate / "sources/sdk-dotnet/release-please-manifest.json", {".": versions["sdk-dotnet"]})
    return versions


def test_projected_client_versions_validate_real_catalog_and_preserve_roster(candidate):
    policy_path = candidate / "sources/official-sdk-protocol-assignments.v1.json"
    old = read(policy_path)
    old_bytes = policy_path.read_bytes()
    versions = change_client_versions(candidate)
    # Real generation exposes the stale official-applicability identity before projection.
    generate(candidate)
    stale = validate(candidate)
    assert stale.returncode != 0
    assert "denominator differs from canonical-client applicability decisions" in stale.stderr
    path, data = REBIND.project_official_protocol_assignments(candidate.parent)
    new = json.loads(data)
    assert {k: v for k, v in old.items() if k != "clients"} == {k: v for k, v in new.items() if k != "clients"}
    for before, after in zip(old["clients"], new["clients"]):
        assert after["version"] == versions[before["source"]]
        assert {k: v for k, v in before.items() if k != "version"} == {k: v for k, v in after.items() if k != "version"}
    assert policy_path.read_bytes() == old_bytes
    (candidate.parent / path).write_bytes(data)
    generate(candidate)
    result = validate(candidate)
    assert result.returncode == 0, result.stderr
    decisions = {r["capability_key"] for r in read(candidate / "sources/canonical-client-applicability.v1.json")["decisions"]
                 if r["classification"] == "official-sdk-required"}
    rows = [r for r in read(candidate / CATALOG)["requirements"]
            if r["capability_key"] in decisions and r["client_lane"] in versions]
    assert rows
    assert {r["client_lane"] for r in rows} == set(versions)
    assert all(r["client_version"] == versions[r["client_lane"]] for r in rows)
    assert REBIND.project_official_protocol_assignments(candidate.parent)[1] == data


@pytest.mark.parametrize("mutation", ["missing-source", "mismatched-js", "wrong-python", "unknown-roster", "missing-roster", "duplicate-roster"])
def test_client_version_projection_refuses_incoherent_sources_without_writes(candidate, mutation):
    path = candidate / "sources/official-sdk-protocol-assignments.v1.json"
    policy = read(path)
    if mutation == "missing-source":
        (candidate / "sources/sdk-dotnet/release-please-manifest.json").unlink()
    elif mutation == "mismatched-js":
        source = candidate / "sources/sdk-js/protocol-certification.v1.json"
        contract = read(source)
        contract["clientVersion"] = "99.0.0"
        write(source, contract)
    elif mutation == "wrong-python":
        source = candidate / "sources/sdk-python/protocol-certification.v1.json"
        contract = read(source)
        contract["canonicalClient"] = "another-client"
        write(source, contract)
    elif mutation == "unknown-roster":
        policy["clients"][0]["source"] = "another-source"
    elif mutation == "missing-roster":
        policy["clients"].pop()
    else:
        policy["clients"].append(copy.deepcopy(policy["clients"][0]))
    write(path, policy)
    before = path.read_bytes()
    with pytest.raises(REBIND.Finding):
        REBIND.project_official_protocol_assignments(candidate.parent)
    assert path.read_bytes() == before
