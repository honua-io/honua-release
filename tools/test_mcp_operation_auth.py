"""MCP policy changes require real authority evidence without losing client rows."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "certification" / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


GENERATOR = module("mcp_auth_generation", "generate-protocol-requirements.py")
VALIDATOR = module("mcp_auth_validation", "validate-protocol-requirements.py")


@pytest.fixture
def policy(tmp_path, monkeypatch):
    original = GENERATOR.SOURCES
    data = json.loads((original / "mcp-operation-auth-policy.v1.json").read_text())
    inventory = json.loads((original / "geospatial-mcp/operations.v1.json").read_text())
    monkeypatch.setattr(GENERATOR, "SOURCES", tmp_path)
    return tmp_path / "mcp-operation-auth-policy.v1.json", data, inventory


def evaluate(policy):
    path, data, inventory = policy
    path.write_text(json.dumps(data))
    return GENERATOR.mcp_operation_auth(inventory)


def test_every_operation_has_explicit_policy(policy):
    selected = evaluate(policy)
    assert len(selected) == 41
    assert {op for op, value in selected.items() if value == "anonymous-public-v1"} == {
        "initialize", "ping", "tools/list", "resources/templates/list",
    }
    assert sum(value == "mcp-protected-operation-v1" for value in selected.values()) == 37


@pytest.mark.parametrize("mutation", [
    "missing", "unknown", "duplicate", "kind", "anonymous-tool", "anonymous-resource",
    "authenticated-public", "empty-rationale", "unknown-field", "missing-observation",
    "unknown-profile", "unknown-revision", "new-inventory-operation", "conflicting-inventory-kind",
])
def test_incomplete_or_misleading_policy_is_refused(policy, mutation):
    _, data, inventory = policy
    protected = next(row for row in data["operations"] if row["kind"] == "tool")
    if mutation == "missing": data["operations"].pop()
    elif mutation == "unknown": protected["operation"] = "tools/call:not-governed"
    elif mutation == "duplicate": data["operations"].append(copy.deepcopy(protected))
    elif mutation == "kind": protected["kind"] = "transport"
    elif mutation == "anonymous-tool": protected["auth_policy_revision"] = "anonymous-public-v1"
    elif mutation == "anonymous-resource":
        next(row for row in data["operations"] if row["kind"] == "resource")["auth_policy_revision"] = "anonymous-public-v1"
    elif mutation == "authenticated-public": data["operations"][0]["auth_policy_revision"] = "mcp-protected-operation-v1"
    elif mutation == "empty-rationale": protected["rationale"] = " "
    elif mutation == "unknown-field": protected["optional"] = True
    elif mutation == "missing-observation": data["required_observations"]["mcp-protected-operation-v1"].remove("no-authenticated-session-reuse")
    elif mutation == "unknown-profile": data["required_observations"]["trust-producer"] = []
    elif mutation == "unknown-revision": data["revision"] = "future-unreviewed"
    elif mutation == "new-inventory-operation": inventory["operations"].append({"kind": "tool", "operation": "tools/call:future"})
    elif mutation == "conflicting-inventory-kind": inventory["operations"].append({"kind": "resource", "operation": protected["operation"]})
    with pytest.raises(ValueError, match="MCP authorization"):
        evaluate(policy)


@pytest.mark.parametrize("mutation", ["anonymous-label", "missing-auth", "missing-client-row", "wrong-client-version", "wrong-target", "duplicate-row"])
def test_catalog_cannot_suppress_or_relabel_auth_obligation(mutation):
    catalog = json.loads((ROOT / "certification/protocol-certification-requirements.v1.json").read_text())
    row = next(row for row in catalog["requirements"] if row["operation"] == "tools/call:create_map_package")
    if mutation == "anonymous-label": row["auth_policy_revision"] = "anonymous-public-v1"
    elif mutation == "missing-auth": row["scenario_facets"].remove("auth")
    elif mutation == "missing-client-row": catalog["requirements"].remove(row)
    elif mutation == "wrong-client-version": row["client_version"] = "999.0.0"
    elif mutation == "wrong-target": row["deployment_target"] = "different-server"
    elif mutation == "duplicate-row": catalog["requirements"].append(copy.deepcopy(row))
    with pytest.raises(ValueError, match="MCP requirements"):
        VALIDATOR.validate_mcp_authorization(catalog)


def test_complete_catalog_keeps_all_governed_rows():
    catalog = json.loads((ROOT / "certification/protocol-certification-requirements.v1.json").read_text())
    VALIDATOR.validate_mcp_authorization(catalog)
    assert len(catalog["requirements"]) == 2716  # +3 sdk-python 0.1.18 rows (rc hand rebind)
    assert sum(row["licensed"] for row in catalog["requirements"]) == 335
    rows = [row for row in catalog["requirements"] if row["surface"] == "mcp"]
    assert len(rows) == 82
    assert all(row["scenario_facets"] == ["positive", "negative", "media-schema", "auth"] for row in rows)
    assert {(row["canonical_client"], row["client_lane"], row["client_version"]) for row in rows} == set(GENERATOR.mcp_clients())
