"""The retained raw GeometryServer lane keeps every supported matrix operation addressable."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CERT = ROOT / "certification"
spec = importlib.util.spec_from_file_location(
    "raw_geometry_validator", CERT / "validate-protocol-requirements.py"
)
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)

LANE = "raw-geoservices-geometry-service"


def catalog():
    return json.loads((CERT / "protocol-certification-requirements.v1.json").read_text(encoding="utf-8"))


def test_raw_geometry_lane_keeps_every_supported_operation():
    rows = [row for row in catalog()["requirements"] if row["client_lane"] == LANE]
    operations = validator._supported_geometry_operations()
    assert rows
    assert {row["operation"] for row in rows} == operations
    assert len(rows) == len(operations)
    assert all(row["addressable_by_client"] and row["addressability_reason"] is None for row in rows)
    validator.validate_raw_geometry_lane(catalog())


def test_a_missing_raw_geometry_lane_is_rejected():
    value = copy.deepcopy(catalog())
    value["requirements"] = [row for row in value["requirements"] if row["client_lane"] != LANE]
    with pytest.raises(ValueError, match="every supported GeometryServer operation"):
        validator.validate_raw_geometry_lane(value)


def test_a_missing_raw_geometry_operation_is_rejected():
    value = copy.deepcopy(catalog())
    row = next(row for row in value["requirements"] if row["client_lane"] == LANE)
    value["requirements"].remove(row)
    with pytest.raises(ValueError, match="every supported GeometryServer operation"):
        validator.validate_raw_geometry_lane(value)


def test_a_non_addressable_raw_geometry_operation_is_rejected():
    value = copy.deepcopy(catalog())
    row = next(row for row in value["requirements"] if row["client_lane"] == LANE)
    row["addressable_by_client"] = False
    with pytest.raises(ValueError, match="must stay addressable"):
        validator.validate_raw_geometry_lane(value)
