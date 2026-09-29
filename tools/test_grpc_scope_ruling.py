"""The 2026.1 gRPC scope ruling excludes RPCs only with a complete, reasoned record."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "generate_protocol_requirements", ROOT / "certification" / "generate-protocol-requirements.py"
)
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)
INVENTORY = json.loads(
    (ROOT / "certification" / "sources" / "geospatial-grpc" / "operations.v1.json").read_text(encoding="utf-8")
)
REQUIREMENTS = json.loads(
    (ROOT / "certification" / "protocol-certification-requirements.v1.json").read_text(encoding="utf-8")
)
IN_SCOPE = {
    "FeatureService/QueryFeatures", "FeatureService/QueryFeaturesStream", "FeatureService/ApplyEdits",
    "ProcessService/ValidatePlan", "ProcessService/DryRunPlan", "ProcessService/SubmitJob",
    "ProcessService/GetJob", "ProcessService/GetJobResult", "ProcessService/CancelJob",
    "SpecService/PlanSpec", "SpecService/ApplySpec", "SpecService/CancelApply",
    "ElevationService/GetElevation", "ElevationService/GetElevationProfile",
}


def test_ruling_keeps_exactly_the_implemented_rpcs():
    in_scope, excluded = generator.grpc_scope(INVENTORY)
    assert {f"{rpc['service']}/{rpc['operation']}" for rpc in in_scope} == IN_SCOPE
    assert len(in_scope) + len(excluded) == len(INVENTORY["operations"])


def test_every_exclusion_names_its_owner_and_release():
    _, excluded = generator.grpc_scope(INVENTORY)
    by_operation = {f"{entry['service']}/{entry['operation']}": entry for entry in excluded}
    assert by_operation["ProcessService/ExecutePlan"]["owner_issue"].endswith("/honua-server/issues/4632")
    assert by_operation["SceneService/GetScene"]["maturity"] == "experimental"
    assert by_operation["FormService/GetFormDefinition"]["maturity"] == "preview"
    assert {entry["target_release"] for entry in excluded} == {"2026.2"}


def test_generated_denominator_carries_only_in_scope_grpc_cells():
    rows = [
        row for row in REQUIREMENTS["requirements"]
        if row["surface"] == "grpc" and row["client_lane"] in {"grpc-dotnet", "grpc-python", "grpc-typescript"}
    ]
    assert {row["operation"] for row in rows} == IN_SCOPE
    assert len(rows) == 3 * len(IN_SCOPE)
    assert {row["maturity"] for row in rows} == {"supported"}


@pytest.mark.parametrize("mutation, message", [
    (lambda entry: entry.pop("rationale"), "lacks"),
    (lambda entry: entry.update(owner_issue=""), "lacks"),
    (lambda entry: entry.update(owner_issue="https://example.com/1"), "honua-io issue URL"),
    (lambda entry: entry.update(maturity="supported"), "preview or experimental"),
    (lambda entry: entry.update(ruling="other"), "does not name"),
    (lambda entry: entry.update(operation="NoSuchRpc"), "not in the inventory"),
])
def test_an_incomplete_exclusion_fails_generation(mutation, message):
    inventory = copy.deepcopy(INVENTORY)
    mutation(inventory["excluded_operations"][0])
    with pytest.raises(ValueError, match=message):
        generator.grpc_scope(inventory)


def test_a_duplicate_exclusion_fails_generation():
    inventory = copy.deepcopy(INVENTORY)
    inventory["excluded_operations"].append(copy.deepcopy(inventory["excluded_operations"][0]))
    with pytest.raises(ValueError, match="more than once"):
        generator.grpc_scope(inventory)


def test_missing_ruling_fails_generation():
    inventory = copy.deepcopy(INVENTORY)
    inventory["rulings"] = []
    with pytest.raises(ValueError, match="ruling"):
        generator.grpc_scope(inventory)
