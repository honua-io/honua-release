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


def test_only_the_31_arcpy_remote_geometry_operations_are_non_addressable():
    rows = catalog()["requirements"]
    matrix = json.loads((CERT / "sources/esri-compat/matrix/geometry-service.matrix.json").read_text())
    operations = {c["id"] for c in matrix["cases"] if c["status"] in validator.SUPPORTED}
    assert len(operations) == 31
    for lane in ("desktop-arcpy", "raw-geoservices", "arcgis-python", "esri-dotnet"):
        selected = [r for r in rows if r["client_lane"] == lane + "-geometry-service"]
        assert len(selected) == 31
        assert {r["operation"] for r in selected} == operations
        assert all(r["addressable_by_client"] is (lane != "desktop-arcpy") for r in selected)
        if lane != "desktop-arcpy":
            assert all(r["addressability_reason"] is None and "positive" in r["scenario_facets"] for r in selected)
    scripting = [r for r in rows if r.get("client_driver") == "arcpy"]
    assert sum(not r["addressable_by_client"] for r in scripting) == 31
    assert all(r["addressable_by_client"] for r in scripting if r["surface"] != "geometry-service")
    validator.validate_arcpy_geometry_addressability({"requirements":rows})


@pytest.mark.parametrize("mutation", ["addressable", "reason", "drop", "duplicate", "other-client", "foreign-reason"])
def test_validator_refuses_false_native_claim_or_widened_policy(mutation):
    value = copy.deepcopy(catalog())
    row = next(r for r in value["requirements"] if r["client_lane"] == "desktop-arcpy-geometry-service")
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
        validator.validate_arcpy_geometry_addressability(value)
