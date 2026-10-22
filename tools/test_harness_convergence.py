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
import yaml

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


def align_manifest_versions(candidate, versions):
    path = candidate.parent / "platform-manifest.yaml"
    manifest = yaml.safe_load(path.read_text())
    for source, component, artifact in REBIND.SDK_PRODUCERS:
        manifest["components"][component]["version"] = versions[source]
        manifest["clientArtifacts"][artifact]["version"] = versions[source]
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))


@pytest.fixture
def candidate(tmp_path):
    target = tmp_path / "certification"
    shutil.copytree(ROOT / "certification", target, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "platform-manifest.yaml", tmp_path / "platform-manifest.yaml")
    # Synthetic admission fixture retains historical source declarations, so its
    # selected versions must match them; no real publication or pin update.
    historical = {row["source"]: row["version"] for row in read(target / "sources/official-sdk-protocol-assignments.v1.json")["clients"]}
    align_manifest_versions(target, historical)
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


def implemented_keys(candidate):
    matrix = read(candidate / "sources/server/capability-matrix.v1.json")
    return {row["key"] for row in matrix["capabilities"] if row.get("maturity", {}).get("implemented")}


def test_all_42_actual_governed_operations_include_ten_added_writes(candidate):
    """Forty-two fixture operations remain the contract. Preview demotions are not harness rows."""
    generate(candidate)
    result = validate(candidate)
    assert result.returncode == 0, result.stderr
    expected = read(candidate / HARNESS)["assignments"]
    assert len(expected) == 42
    implemented = implemented_keys(candidate)
    demoted = {assignment["capability_key"] for assignment in expected if assignment["capability_key"] not in implemented}
    assert demoted == {"alerts.evaluation", "serve.ogc-api-edr", "serve.sensorthings"}
    governed = [assignment for assignment in expected if assignment["capability_key"] in implemented]
    rows = [row for row in read(candidate / CATALOG)["requirements"] if row["client_lane"] == "server-protocol-harness"]
    assert len(rows) == len(governed)
    assert {(r["capability_key"], r["surface"], r["operation"], tuple(r["test_ids"]), tuple(r["scenario_facets"])) for r in rows} == {
        (a["capability_key"], a["surface"], a["operation"], tuple(a["test_ids"]), tuple(a["scenario_facets"])) for a in governed
    }
    writes = [a for a in governed if a["capability_key"] in {"editing.featureserver-edits", "serve.ogc-api-features", "serve.wfs"}]
    assert len(writes) == 10
    assert not any(row["capability_key"] in demoted for row in rows)


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
    versions = {"sdk-js": "0.2.0", "sdk-python": "0.1.18", "sdk-dotnet": "1.11.0"}
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
    align_manifest_versions(candidate, versions)
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


@pytest.mark.parametrize("source", ["sdk-js", "sdk-python", "sdk-dotnet"])
@pytest.mark.parametrize("mismatch", ["component", "artifact", "both"])
def test_source_versions_must_equal_both_manifest_selections(candidate, source, mismatch):
    policy_path = candidate / "sources/official-sdk-protocol-assignments.v1.json"
    original = policy_path.read_bytes()
    manifest_path = candidate.parent / "platform-manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text())
    _, component, artifact = next(row for row in REBIND.SDK_PRODUCERS if row[0] == source)
    if mismatch in {"component", "both"}:
        manifest["components"][component]["version"] = "9.9.9"
    if mismatch in {"artifact", "both"}:
        manifest["clientArtifacts"][artifact]["version"] = "9.9.9"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    with pytest.raises(REBIND.Finding, match=f"pinned {source} declaration version .* differs from"):
        REBIND.project_official_protocol_assignments(candidate.parent)
    assert policy_path.read_bytes() == original


def drop_matrix_capability(candidate, key):
    """Pinned candidate snapshots may already contain the capability under test."""
    matrix_path = candidate / "sources/server/capability-matrix.v1.json"
    matrix = read(matrix_path)
    matrix["capabilities"] = [row for row in matrix["capabilities"] if row["key"] != key]
    write(matrix_path, matrix)
    harness_path = candidate / HARNESS
    harness = read(harness_path)
    harness["assignments"] = [row for row in harness["assignments"] if row["capability_key"] != key]
    write(harness_path, harness)


def drop_h3_applicability(candidate):
    path = candidate / "sources/canonical-client-applicability.v1.json"
    policy = read(path)
    expected = {"capability_key": "analytics.h3", "classification": "official-sdk-required"}
    policy["decisions"] = [row for row in policy["decisions"] if row != expected]
    write(path, policy)


def drop_unproduced_lane(candidate, lane):
    path = candidate / "sources/protocol-certification-production.v1.json"
    policy = read(path)
    policy["unproduced"] = [row for row in policy["unproduced"] if row.get("client_lanes") != [lane]]
    write(path, policy)


def add_reviewed_wps_contract(candidate):
    """Extracted source inputs, not a conformance receipt or a support promotion."""
    fixture = read(ROOT / "tools/fixtures/wps-contract-source-2026-10-13.json")
    drop_matrix_capability(candidate, "serve.wps")
    matrix_path = candidate / "sources/server/capability-matrix.v1.json"
    matrix = read(matrix_path)
    matrix["capabilities"].append(fixture["capability"])
    write(matrix_path, matrix)
    harness = read(candidate / HARNESS)
    harness["assignments"].extend(fixture["assignments"])
    write(candidate / HARNESS, harness)
    path, data = REBIND.project_official_protocol_assignments(candidate.parent)
    (candidate.parent / path).write_bytes(data)


def test_wps_operations_admit_without_inventing_cite_production(candidate):
    add_reviewed_wps_contract(candidate)
    generate(candidate)
    result = validate(candidate)
    assert result.returncode == 0, result.stderr
    catalog = read(candidate / CATALOG)
    assignments = read(candidate / HARNESS)["assignments"]
    assert len(assignments) == 47
    implemented = implemented_keys(candidate)
    governed = [assignment for assignment in assignments if assignment["capability_key"] in implemented]
    harness = [r for r in catalog["requirements"] if r["client_lane"] == "server-protocol-harness"]
    assert len(harness) == len(governed)
    wps = [r for r in harness if r["capability_key"] == "serve.wps"]
    assert len(wps) == 5
    assert sum(len(r["test_ids"]) for r in wps) == 10
    cite = [r for r in catalog["requirements"] if r["client_lane"] == "cite-wps-2-0"]
    assert len(cite) == 1
    assert cite[0]["addressable_by_client"] is True
    assert cite[0]["required_tier"] == "nightly"
    assert cite[0]["scenario_facets"] == ["positive", "negative", "crs-axis", "media-schema"]
    assert not any("cite-wps-2-0" in p["client_lanes"] for p in catalog["production"]["producers"])
    owner = next(p for p in catalog["production"]["unproduced"] if p["client_lanes"] == ["cite-wps-2-0"])
    assert owner["cells"] == 1
    assert owner["issue"] == "https://github.com/honua-io/honua-release/issues/360"
    # A claimed produced row cannot be substituted into the generated catalog.
    catalog["production"]["unproduced"].remove(owner)
    producer = next(p for p in catalog["production"]["producers"] if p["producer"] == "honua-server-cite")
    producer["client_lanes"].append("cite-wps-2-0")
    producer["cells"] += owner["cells"]
    catalog["production"]["cells"]["produced"] += owner["cells"]
    catalog["production"]["cells"]["unproduced"] -= owner["cells"]
    write(candidate / CATALOG, catalog)
    rejected = validate(candidate)
    assert rejected.returncode != 0
    assert "production dispositions differ" in rejected.stderr


@pytest.mark.parametrize("mutation, count", [("missing", 0), ("duplicate", 2)])
def test_wps_cite_disposition_must_be_exactly_once(candidate, mutation, count):
    add_reviewed_wps_contract(candidate)
    path = candidate / "sources/protocol-certification-production.v1.json"
    policy = read(path)
    if mutation == "missing":
        policy["unproduced"] = [p for p in policy["unproduced"] if p["client_lanes"] != ["cite-wps-2-0"]]
    else:
        next(p for p in policy["producers"] if p["producer"] == "honua-server-cite")["client_lanes"].append("cite-wps-2-0")
    write(path, policy)
    refused = run(candidate, "generate-protocol-requirements.py")
    assert refused.returncode != 0
    assert f"client lane 'cite-wps-2-0' (local-docker) has {count} production dispositions" in refused.stderr


def add_reviewed_h3_contract(candidate, *, implemented=True, assignments=True):
    """Reviewed adapter source data; never an executed or image-qualified receipt."""
    fixture = read(ROOT / "tools/fixtures/h3-contract-source-2026-10-13.json")
    drop_matrix_capability(candidate, "analytics.h3")
    matrix_path = candidate / "sources/server/capability-matrix.v1.json"
    matrix = read(matrix_path)
    capability = copy.deepcopy(fixture["capability"])
    if not implemented:
        capability["maturity"] = {"preview": 1}
    matrix["capabilities"].append(capability)
    write(matrix_path, matrix)
    if assignments:
        harness = read(candidate / HARNESS)
        harness["assignments"].extend(fixture["assignments"])
        write(candidate / HARNESS, harness)


@pytest.mark.parametrize("state", ["absent", "preview", "implemented"])
def test_h3_applicability_is_explicit_conditional_and_idempotent(candidate, state):
    # Reconstruct the pre-decision input. The pinned candidate already carries the decision.
    drop_matrix_capability(candidate, "analytics.h3")
    drop_h3_applicability(candidate)
    policy_path = candidate / "sources/canonical-client-applicability.v1.json"
    original = policy_path.read_bytes()
    if state != "absent":
        add_reviewed_h3_contract(candidate, implemented=state == "implemented")
    path, data = REBIND.project_canonical_client_applicability(candidate.parent)
    assert candidate.parent / path == policy_path
    assert policy_path.read_bytes() == original
    if state == "implemented":
        old, new = json.loads(original), json.loads(data)
        h3 = {"capability_key": "analytics.h3", "classification": "official-sdk-required"}
        assert new["decisions"].count(h3) == 1
        new["decisions"].remove(h3)
        assert new == old
    else:
        assert data == original
    policy_path.write_bytes(data)
    assert REBIND.project_canonical_client_applicability(candidate.parent) == (path, data)
    generate(candidate)
    result = validate(candidate)
    assert result.returncode == 0, result.stderr
    rows = [r for r in read(candidate / CATALOG)["requirements"] if r["capability_key"] == "analytics.h3"]
    assert len(rows) == (4 if state == "implemented" else 0)
    for row in rows:
        assert row["client_lane"] == "server-protocol-harness"
        assert row["scenario_facets"] == ["positive", "negative"]
        assert len(row["test_ids"]) == 2


@pytest.mark.parametrize("state", ["absent", "implemented"])
@pytest.mark.parametrize("mutation", ["classification", "duplicate", "extra-fields"])
def test_h3_projection_refuses_conflicting_existing_decisions(candidate, mutation, state):
    if state == "implemented":
        add_reviewed_h3_contract(candidate)
    path = candidate / "sources/canonical-client-applicability.v1.json"
    policy = read(path)
    decision = {"capability_key": "analytics.h3", "classification": "official-sdk-required"}
    if mutation == "classification":
        decision["classification"] = "not-addressable"
    elif mutation == "extra-fields":
        decision["reason"] = "An unreviewed exception"
    policy["decisions"].append(decision)
    if mutation == "duplicate":
        policy["decisions"].append(copy.deepcopy(decision))
    write(path, policy)
    original = path.read_bytes()
    with pytest.raises(REBIND.Finding, match="analytics.h3 applicability conflicts"):
        REBIND.project_canonical_client_applicability(candidate.parent)
    assert path.read_bytes() == original


def stage_reviewed_unassigned_gap(candidate, capability):
    """Implemented source surface, no harness assignment, one reviewed unproduced owner."""
    if capability == "analytics.h3":
        add_reviewed_h3_contract(candidate, assignments=False)
        path, data = REBIND.project_canonical_client_applicability(candidate.parent)
        (candidate.parent / path).write_bytes(data)
        path, data = REBIND.project_h3_production_gap(candidate.parent)
    elif capability == "serve.wps":
        fixture = read(ROOT / "tools/fixtures/wps-contract-source-2026-10-13.json")
        drop_matrix_capability(candidate, "serve.wps")
        matrix_path = candidate / "sources/server/capability-matrix.v1.json"
        matrix = read(matrix_path)
        matrix["capabilities"].append(fixture["capability"])
        write(matrix_path, matrix)
        path, data = REBIND.project_official_protocol_assignments(candidate.parent)
        (candidate.parent / path).write_bytes(data)
        path, data = REBIND.project_wps_production_gap(candidate.parent)
    else:
        raise AssertionError(capability)
    (candidate.parent / path).write_bytes(data)
    return path


@pytest.mark.parametrize("capability, lane", [
    ("analytics.h3", "protocol-harness-gap-analytics-h3"),
    ("serve.wps", "protocol-harness-gap-serve-wps"),
])
def test_explicit_unproduced_gap_is_accounted_and_not_harness_credit(candidate, capability, lane):
    stage_reviewed_unassigned_gap(candidate, capability)
    generate(candidate)
    result = validate(candidate)
    assert result.returncode == 0, result.stderr
    catalog = read(candidate / CATALOG)
    gaps = [row for row in catalog["requirements"] if row["client_lane"] == lane]
    assert len(gaps) == 1
    assert gaps[0]["operation"] == f"UNASSIGNED PROTOCOL HARNESS CONTRACT:{capability}"
    assert gaps[0]["canonical_client"] == "UNASSIGNED PROTOCOL HARNESS"
    assert not any(
        row["client_lane"] == "server-protocol-harness" and row["capability_key"] == capability
        for row in catalog["requirements"]
    )
    owners = [
        entry for entry in catalog["production"]["unproduced"]
        if entry.get("client_lanes") == [lane]
    ]
    assert len(owners) == 1 and owners[0]["cells"] == 1
    assert owners[0]["owner"] == "honua-io/honua-release"
    assert not any(lane in (entry.get("client_lanes") or []) for entry in catalog["production"]["producers"])
    catalog["requirements"] = [row for row in catalog["requirements"] if row["client_lane"] != lane]
    write(candidate / CATALOG, catalog)
    refused = validate(candidate)
    assert refused.returncode != 0
    assert "omit operation-contract gaps" in refused.stderr
    assert capability in refused.stderr


def test_producer_cannot_claim_an_unassigned_harness_gap(candidate):
    stage_reviewed_unassigned_gap(candidate, "analytics.h3")
    generate(candidate)
    catalog = read(candidate / CATALOG)
    lane = "protocol-harness-gap-analytics-h3"
    owner = next(entry for entry in catalog["production"]["unproduced"] if entry["client_lanes"] == [lane])
    catalog["production"]["unproduced"].remove(owner)
    producer = next(entry for entry in catalog["production"]["producers"] if entry["producer"] == "server-protocol-harness")
    producer["client_lanes"] = [*producer["client_lanes"], lane]
    producer["cells"] += owner["cells"]
    catalog["production"]["cells"]["produced"] += owner["cells"]
    catalog["production"]["cells"]["unproduced"] -= owner["cells"]
    write(candidate / CATALOG, catalog)
    refused = validate(candidate)
    assert refused.returncode != 0
    assert "analytics.h3 is not an owned unproduced gap" in refused.stderr
    assert "not a passing harness receipt" in refused.stderr


def test_h3_classification_cannot_substitute_for_missing_operation_contract(candidate):
    # The pinned production file already owns this gap. Removing that owner is what
    # makes a bare applicability decision fail closed.
    drop_unproduced_lane(candidate, "protocol-harness-gap-analytics-h3")
    add_reviewed_h3_contract(candidate, assignments=False)
    path, data = REBIND.project_canonical_client_applicability(candidate.parent)
    (candidate.parent / path).write_bytes(data)
    original_catalog = (candidate / CATALOG).read_bytes()
    result = run(candidate, "generate-protocol-requirements.py")
    assert result.returncode != 0
    assert "client lane 'protocol-harness-gap-analytics-h3' (local-docker) has 0 production dispositions" in result.stderr
    assert (candidate / CATALOG).read_bytes() == original_catalog


def test_h3_projection_does_not_classify_unreviewed_capabilities(candidate):
    add_reviewed_h3_contract(candidate)
    path = candidate / "sources/server/capability-matrix.v1.json"
    matrix = read(path)
    other = copy.deepcopy(next(r for r in matrix["capabilities"] if r["key"] == "analytics.h3"))
    other["key"] = "analytics.unreviewed"
    matrix["capabilities"].append(other)
    write(path, matrix)
    path, data = REBIND.project_canonical_client_applicability(candidate.parent)
    (candidate.parent / path).write_bytes(data)
    assert not any(r["capability_key"] == "analytics.unreviewed" for r in json.loads(data)["decisions"])
    generate(candidate)
    result = validate(candidate)
    assert result.returncode != 0
    assert "unclassified=['analytics.unreviewed']" in result.stderr


def test_h3_projection_can_roll_back_to_pre_h3_source(candidate):
    drop_matrix_capability(candidate, "analytics.h3")
    drop_h3_applicability(candidate)
    policy = candidate / "sources/canonical-client-applicability.v1.json"
    original = read(policy)
    matrix_path = candidate / "sources/server/capability-matrix.v1.json"
    historical_matrix = matrix_path.read_bytes()
    harness_path = candidate / HARNESS
    historical_harness = harness_path.read_bytes()
    add_reviewed_h3_contract(candidate)
    path, data = REBIND.project_canonical_client_applicability(candidate.parent)
    policy.write_bytes(data)
    assert any(row["capability_key"] == "analytics.h3" for row in read(policy)["decisions"])
    matrix_path.write_bytes(historical_matrix)
    harness_path.write_bytes(historical_harness)
    path, data = REBIND.project_canonical_client_applicability(candidate.parent)
    assert json.loads(data) == original
    policy.write_bytes(data)
    assert REBIND.project_canonical_client_applicability(candidate.parent) == (path, data)
    generate(candidate)
    result = validate(candidate)
    assert result.returncode == 0, result.stderr
