"""The GP catalog is Preview for 2026.1 but its safety floor stays required (release#949).

Ruling gp-preview-floor-2026-10-26 classifies every row on the six GP capability keys in
certification/sources/gp-preview-floor.v1.json. Preview rows are maturity=preview and
required_tier=release, which the protocol gate requires at no tier. Floor rows stay supported and
required. These tests hold the generated catalog, the gate scope and the expected-GA list to that
ruling, and prove the generator and the validator fail closed on an unclassified or relabeled row.
"""

from __future__ import annotations

import importlib.util
import json
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CERTIFICATION = ROOT / "certification"
CATALOG = "protocol-certification-requirements.v1.json"
RULING = "sources/gp-preview-floor.v1.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _checker():
    spec = importlib.util.spec_from_file_location("gp_floor_checker", ROOT / "tools" / "check_protocol_certification.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _gp_rows() -> tuple[list[dict], list[dict]]:
    ruling = _load(CERTIFICATION / RULING)
    rows = [row for row in _load(CERTIFICATION / CATALOG)["requirements"]
            if row["capability_key"] in ruling["capability_keys"]]
    floor = [row for row in rows if row["maturity"] == "supported"]
    preview = [row for row in rows if row["maturity"] == "preview"]
    assert len(floor) + len(preview) == len(rows)
    return floor, preview


def test_every_gp_row_is_floor_or_preview_and_none_is_dropped():
    floor, preview = _gp_rows()
    assert (len(floor), len(preview)) == (8, 103)
    assert {row["required_tier"] for row in preview} == {"release"}
    assert {row["required_tier"] for row in floor} == {"nightly"}
    assert {row["operation"] for row in floor if row["surface"] == "gp-server"} == {
        "GP-OP-JOBS-LIST", "GP-OP-JOB-STATUS-2", "GP-OP-CANCEL-JOB", "GP-OP-JOB-RESULTS-2",
        "GP-OP-SUBMIT-JOB-2", "GP-OP-EXECUTE-TASK-2",
    }
    assert {row["client_lane"] for row in floor if row["surface"] == "gp-server"} == {
        "raw-geoservices-gp-server",
    }
    assert {row["client_lane"] for row in floor if row["surface"] == "ogc-api-processes"} == {
        "cite-ogc-api-processes", "py-owslib",
    }


def test_preview_is_confined_to_the_ruled_gp_keys():
    ruling = _load(CERTIFICATION / RULING)
    catalog = _load(CERTIFICATION / CATALOG)
    outside = [row for row in catalog["requirements"]
               if row["maturity"] == "preview" and row["capability_key"] not in ruling["capability_keys"]]
    assert outside == []
    assert "gp-preview-floor-2026-10-26" in catalog["scope_notes"]


@pytest.mark.parametrize("tier", ["pr", "nightly", "release"])
def test_gate_requires_floor_rows_and_no_preview_row(tier):
    in_scope = _checker()._in_scope
    floor, preview = _gp_rows()
    assert not any(in_scope(row, tier) for row in preview)
    if tier != "pr":
        assert all(in_scope(row, tier) for row in floor)


def test_expected_ga_lists_only_ga_ids_and_keeps_jobs_runner():
    expected = _load(ROOT / "e2e" / "expected-ga-manifest.json")
    ids = set(expected["expectedGa"])
    assert "jobs.runner" in ids
    assert not {"sync.offline", "realtime.feature-streams"} & ids
    gated = {entry["id"] for entry in expected["availabilityGated"]}
    assert gated <= ids
    assert not any("lifecycle=preview" in entry["reason"] for entry in expected["availabilityGated"])


@pytest.fixture
def certification_copy(tmp_path: Path) -> Path:
    target = tmp_path / "certification"
    shutil.copytree(
        CERTIFICATION, target,
        ignore=shutil.ignore_patterns(
            "__pycache__", "installed-clients", "terminal-journey", "first-publication", "release-controls",
        ),
    )
    shutil.copy2(ROOT / "platform-manifest.yaml", tmp_path / "platform-manifest.yaml")
    return target


def _run(certification: Path, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(certification / script)],
        cwd=certification, capture_output=True, text=True, timeout=600,
    )



@pytest.mark.parametrize("capability,expected", [
    ({"key": "serve.wms", "maturity": {"implemented": 1}}, True),
    ({"key": "process.ogc-api-processes", "maturity": {"preview": 10}}, True),
    ({"key": "serve.wms", "maturity": {"preview": 1}}, False),
    ({"key": "process.ogc-api-processes", "maturity": {"planned": 10}}, False),
    ({"key": "process.ogc-api-processes", "maturity": {}}, False),
])
def test_governed_rows_do_not_admit_other_preview_or_unimplemented_sources(capability, expected):
    generator = runpy.run_path(str(CERTIFICATION / "generate-protocol-requirements.py"))
    before = json.dumps(capability, sort_keys=True)
    assert generator["capability_has_governed_rows"](capability) is expected
    assert json.dumps(capability, sort_keys=True) == before

def test_source_preview_retains_the_classified_gp_rows_and_safety_floor(certification_copy: Path):
    before = _load(certification_copy / CATALOG)
    keys = set(_load(certification_copy / RULING)["capability_keys"])
    matrix_path = certification_copy / "sources/server/capability-matrix.v1.json"
    matrix = _load(matrix_path)
    capability = next(row for row in matrix["capabilities"] if row["key"] == "process.ogc-api-processes")
    capability["maturity"] = {"preview": 10}
    matrix_path.write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")

    result = _run(certification_copy, "generate-protocol-requirements.py")
    assert result.returncode == 0, result.stderr
    after = _load(certification_copy / CATALOG)
    before_gp = [row for row in before["requirements"] if row["capability_key"] in keys]
    after_gp = [row for row in after["requirements"] if row["capability_key"] in keys]
    assert after_gp == before_gp
    floor = [row for row in after_gp if row["maturity"] == "supported"]
    assert len(floor) == 8
    assert {row["client_lane"] for row in floor if row["surface"] == "ogc-api-processes"} == {
        "cite-ogc-api-processes", "py-owslib",
    }
    assert _load(matrix_path) == matrix
    validation = _run(certification_copy, "validate-protocol-requirements.py")
    assert validation.returncode == 0, validation.stderr

def test_generator_refuses_an_unclassified_gp_row(certification_copy: Path):
    path = certification_copy / RULING
    ruling = _load(path)
    ruling["floor"] = [entry for entry in ruling["floor"] if entry["operations"] != ["GP-OP-JOBS-LIST"]]
    path.write_text(json.dumps(ruling, indent=2) + "\n", encoding="utf-8")
    result = _run(certification_copy, "generate-protocol-requirements.py")
    assert result.returncode != 0
    assert "GP-OP-JOBS-LIST" in result.stderr and "classify it exactly once" in result.stderr


def test_generator_refuses_a_ruling_with_no_floor(certification_copy: Path):
    path = certification_copy / RULING
    ruling = _load(path)
    ruling["preview"].extend({**entry, "reason": "relabeled"} for entry in ruling["floor"])
    ruling["floor"] = []
    path.write_text(json.dumps(ruling, indent=2) + "\n", encoding="utf-8")
    result = _run(certification_copy, "generate-protocol-requirements.py")
    assert result.returncode != 0
    assert "must keep a required GP safety floor" in result.stderr


@pytest.mark.parametrize(("operation", "relabel", "message"), [
    ("GP-OP-JOBS-LIST", {"maturity": "preview", "required_tier": "release"}, "must stay supported and required"),
    ("GP-OP-TEMPLATE-LISTING", {"maturity": "supported", "required_tier": "nightly"}, "must be maturity=preview"),
])
def test_validator_refuses_a_relabeled_gp_row(certification_copy: Path, operation, relabel, message):
    path = certification_copy / CATALOG
    catalog = _load(path)
    row = next(row for row in catalog["requirements"]
               if row["surface"] == "gp-server" and row["operation"] == operation)
    row.update(relabel)
    path.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
    result = _run(certification_copy, "validate-protocol-requirements.py")
    assert result.returncode != 0
    assert message in result.stderr
