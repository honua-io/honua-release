"""Keep remote GeometryServer obligations explicit and scoped to their real clients."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CERT = ROOT / "certification"
spec = importlib.util.spec_from_file_location("geometry_catalog_validator", CERT / "validate-protocol-requirements.py")
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


def catalog():
    return json.loads((CERT / "protocol-certification-requirements.v1.json").read_text())


def test_only_the_31_licensed_scripting_geometry_operations_are_non_addressable():
    rows = catalog()["requirements"]
    matrix = json.loads((CERT / "sources/esri-compat/matrix/geometry-service.matrix.json").read_text())
    operations = {c["id"] for c in matrix["cases"] if c["status"] in validator.SUPPORTED}
    assert len(operations) == 31
    spec = importlib.util.spec_from_file_location("generator_config_test", CERT / "generate-protocol-requirements.py")
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    scripting_lane = config.licensed_scripting_client()[2]
    for lane in {r["client_lane"].removesuffix("-geometry-service") for r in rows if r["surface"] == "geometry-service" and r["operation"] in operations}:
        selected = [r for r in rows if r["client_lane"] == lane + "-geometry-service"]
        assert len(selected) == 31
        assert {r["operation"] for r in selected} == operations
        assert all(r["addressable_by_client"] is (lane != scripting_lane) for r in selected)
        if lane != scripting_lane:
            assert all(r["addressability_reason"] is None and "positive" in r["scenario_facets"] for r in selected)
    scripting = [r for r in rows if r.get("client_driver") == scripting_lane.removeprefix("desktop-")]
    assert sum(not r["addressable_by_client"] for r in scripting) == 31
    assert all(r["addressable_by_client"] for r in scripting if r["surface"] != "geometry-service")
    validator.validate_geometry_client_addressability({"requirements":rows})


@pytest.mark.parametrize("mutation", ["addressable", "reason", "drop", "duplicate", "other-client", "foreign-reason"])
def test_validator_refuses_false_native_claim_or_widened_policy(mutation):
    value = copy.deepcopy(catalog())
    row = next(r for r in value["requirements"] if r["client_lane"] == next(r["client_lane"] for r in value["requirements"] if r["surface"] == "geometry-service" and not r["addressable_by_client"]))
    if mutation == "addressable":
        row["addressable_by_client"] = True
    elif mutation == "reason":
        row["addressability_reason"] = "not-applicable: unsubstantiated"
    elif mutation == "drop":
        value["requirements"].remove(row)
    elif mutation == "duplicate":
        value["requirements"].append(copy.deepcopy(row))
    else:
        foreign = next(r for r in value["requirements"] if r["client_lane"] == "raw-geoservices-geometry-service")
        foreign["addressability_reason"] = row["addressability_reason"] if mutation == "other-client" else "not-applicable: wrong client"
        foreign["addressable_by_client"] = False
    with pytest.raises(ValueError):
        validator.validate_geometry_client_addressability(value)


@pytest.mark.parametrize("field,value", [("schema", "wrong"), ("surface", "feature-server"), ("supported_operation_count", 30), ("client_key", "qgis"), ("client_kind", "ui"), ("addressable_by_client", True)])
def test_public_projection_scope_is_fixed(monkeypatch, field, value):
    original = json.loads
    def altered(text, *args, **kwargs):
        data = original(text, *args, **kwargs)
        if isinstance(data, dict) and data.get("schema") == "honua.geometry-service-client-addressability/v1":
            data[field] = value
        return data
    monkeypatch.setattr(validator.json, "loads", altered)
    with pytest.raises(ValueError):
        validator.validate_geometry_client_addressability(catalog())


@pytest.mark.parametrize("field", ["commit", "blob"])
def test_private_evidence_pin_cannot_silently_change(monkeypatch, field):
    original = json.loads
    def altered(text, *args, **kwargs):
        data = original(text, *args, **kwargs)
        if isinstance(data, dict) and data.get("schema") == "honua.geometry-service-client-addressability/v1":
            data["source"][field] = "0" * 40
        return data
    monkeypatch.setattr(validator.json, "loads", altered)
    with pytest.raises(ValueError):
        validator.validate_geometry_client_addressability(catalog())


def test_an_entire_other_client_lane_cannot_disappear():
    value = copy.deepcopy(catalog())
    spec = importlib.util.spec_from_file_location("generator_config_missing_lane", CERT / "generate-protocol-requirements.py")
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    lane = next(client[2] for client in config.protocol_clients() if not client[4])
    value["requirements"] = [r for r in value["requirements"] if r["client_lane"] != lane + "-geometry-service"]
    with pytest.raises(ValueError):
        validator.validate_geometry_client_addressability(value)


def test_target_lane_cannot_be_mislabeled_as_another_driver():
    value = copy.deepcopy(catalog())
    row = next(r for r in value["requirements"] if r["surface"] == "geometry-service" and not r["addressable_by_client"])
    row["client_driver"] = "pro-ui"
    with pytest.raises(ValueError):
        validator.validate_geometry_client_addressability(value)
