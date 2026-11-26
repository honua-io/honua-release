"""Nightly ledger receipt binding tests for Quantized-Mesh Terrain source-test-host rows."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

import check_protocol_certification as cpc
import test_convergence_rebind as base

ROOT = Path(__file__).resolve().parents[1]
CLIENT = "Honua server public protocol integration harness"
LANE = "server-protocol-harness"
PROTOCOL_VERSION = "2026-11-26.1"


def _terrain_requirement(assignment: dict, sha: str) -> dict:
    return {
        "capability_key": assignment["capability_key"],
        "surface": assignment["surface"],
        "operation": assignment["operation"],
        "maturity": "supported",
        "canonical_client": CLIENT,
        "client_lane": LANE,
        "client_version": f"source@{sha}",
        "deployment_target": "source-test-host",
        "required_tier": "nightly",
        "licensed": False,
        "entitlement_policy_revision": None,
        "addressable_by_client": True,
        "addressability_reason": None,
        "scenario_facets": list(assignment["scenario_facets"]),
        "test_ids": list(assignment["test_ids"]),
        "contract_revision": f"server-protocol-harness@{PROTOCOL_VERSION}+{sha}",
        "auth_policy_revision": "server-test-host-v1",
        "fixture_revision": f"server-test-fixtures@{sha}",
        "budget_expectations": None,
    }


def _terrain_receipt(req: dict, execution: dict, recorded: dict, harness_run: dict) -> dict:
    payload = {
        "contract_revision": req["contract_revision"],
        "execution": execution,
        "test_outcomes": {tid: "pass" for tid in req["test_ids"]},
    }
    payload_b64 = base64.b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    sha = recorded["candidate"]["server_sha"]
    return {
        "schema": "honua.certification-evidence-receipt/v2",
        "identity": {
            "capability_key": req["capability_key"],
            "surface": req["surface"],
            "operation": req["operation"],
            "canonical_client": CLIENT,
            "client_version": req["client_version"],
            "deployment_target": "source-test-host",
            "source_sha": sha,
            "producer_source_sha": sha,
            "image_digest": None,
            "fixture_revision": req["fixture_revision"],
            "contract_revision": req["contract_revision"],
            "auth_policy_revision": req["auth_policy_revision"],
            "started_at": "2026-10-03T11:48:12Z",
            "completed_at": "2026-10-03T11:51:12Z",
            "maturity": "supported",
            "required_tier": "nightly",
            "requirements_revision": recorded["catalog"]["revision"],
            "candidate_cut_at": recorded["candidate"]["cut_at"],
            "test_ids": list(req["test_ids"]),
            "producer_run": {
                "repository": harness_run["repository"],
                "workflow": harness_run["workflow"],
                "run_id": harness_run["run_id"],
                "run_attempt": harness_run["run_attempt"],
                "dispatch_id": recorded["dispatch_id"],
            },
        },
        "result": "pass",
        "facets": {facet: "pass" for facet in req["scenario_facets"]},
        "payload_base64": payload_b64,
    }


def _terrain_cell(req: dict, recorded: dict, harness_run: dict) -> dict:
    sha = recorded["candidate"]["server_sha"]
    execution = {
        "client_id": CLIENT,
        "runner_lane": LANE,
        "protocol_version": PROTOCOL_VERSION,
        "protocol_profile": "source-test-host-http-adapter",
        "performed_by": CLIENT,
        "request_url": None,
        "exercised_capabilities": list(req["scenario_facets"]),
    }
    receipt = _terrain_receipt(req, execution, recorded, harness_run)
    cell = {
        **req,
        **execution,
        "result": "pass",
        "skip_reason": None,
        "source_sha": sha,
        "producer_source_sha": sha,
        "image_digest": None,
        "evidence_uri": "",
        "evidence_digest": "",
        "evidence_receipt": receipt,
        "facet_results": {facet: {"result": "pass", "evidence_digest": ""} for facet in req["scenario_facets"]},
        "started_at": "2026-10-03T11:48:12Z",
        "completed_at": "2026-10-03T11:51:12Z",
        "budget_observations": None,
    }
    return base.rehash(cell)


def terrain_source_host_night() -> dict:
    """Nightly protocol ledger carrying governed Quantized-Mesh Terrain source-test-host receipts."""
    recorded = base.night()
    fixture_doc = json.loads((ROOT / "tools/fixtures/terrain-contract-input.json").read_text(encoding="utf-8"))
    sha = recorded["candidate"]["server_sha"]
    harness_entry = next(p for p in recorded["catalog"]["production"]["producers"] if p["producer"] == LANE)
    harness_entry["client_lanes"] = [LANE]
    harness_entry["cells"] = len(fixture_doc["assignments"])
    harness_run = next(r for r in recorded["runs"] if r["producer"] == LANE)
    harness_run["client_lanes"] = [LANE]
    recorded["catalog"]["requirements"] = [
        r for r in recorded["catalog"]["requirements"] if r["client_lane"] != "harness"
    ]
    recorded["ledger"]["cells"] = [c for c in recorded["ledger"]["cells"] if c["client_lane"] != "harness"]
    for assignment in fixture_doc["assignments"]:
        req = _terrain_requirement(assignment, sha)
        recorded["catalog"]["requirements"].append(req)
        recorded["ledger"]["cells"].append(_terrain_cell(req, recorded, harness_run))
    return recorded


def test_nightly_bind_accepts_quantized_mesh_terrain_source_test_host_receipts(tmp_path):
    recorded = terrain_source_host_night()
    assert base.schema_errors(recorded["ledger"]) == []
    terrain_cells = [
        c for c in recorded["ledger"]["cells"] if c["capability_key"] == "serve.terrain-quantized-mesh"
    ]
    assert len(terrain_cells) == 2
    for cell in terrain_cells:
        assert cpc._bound_source_host_execution(
            cell, recorded["candidate"]["cut_at"], recorded["catalog"]["revision"]
        )

    root = base.candidate_root(tmp_path)
    (root / base.MODULE.CATALOG).write_text(json.dumps(recorded["catalog"], indent=2) + "\n", encoding="utf-8")
    evidence = base.RecordedEvidence({recorded["evidence_commit"]: recorded["ledger"]})
    result = base.MODULE.nightly_bind(
        root, evidence, recorded["requirements_revision"], recorded["runs"],
        "2026-10-03T12:50:00Z", recorded["dispatch_id"],
    )
    assert result["results"] == {"pass": 3, "skip": 1, "not-addressable": 1}
    assert result["ledger"]["status"] == "bound"


@pytest.mark.parametrize("mutation,expected", [
    ("cell-image-digest", "source-test-host evidence must not claim candidate image execution"),
    ("receipt-image-digest", "with null image_digest"),
    ("wrong-cut-at", "source-test-host receipt candidate_cut_at"),
    ("wrong-source-sha", "not about candidate"),
    ("missing-test-id", "test_ids do not match the governed requirement"),
    ("wrong-lane", "source-test-host is only permitted for server-protocol-harness"),
])
def test_nightly_bind_refuses_tampered_source_test_host_terrain_receipts(mutation, expected):
    recorded = terrain_source_host_night()
    cell = next(c for c in recorded["ledger"]["cells"] if c["capability_key"] == "serve.terrain-quantized-mesh")
    if mutation == "cell-image-digest":
        cell["image_digest"] = recorded["candidate"]["image_digest"]
    elif mutation == "receipt-image-digest":
        cell["evidence_receipt"]["identity"]["image_digest"] = recorded["candidate"]["image_digest"]
        base.rehash(cell)
    elif mutation == "wrong-cut-at":
        cell["evidence_receipt"]["identity"]["candidate_cut_at"] = "2026-10-02T00:00:00Z"
        base.rehash(cell)
    elif mutation == "wrong-source-sha":
        cell["source_sha"] = "a" * 40
    elif mutation == "missing-test-id":
        cell["test_ids"] = cell["test_ids"][:1]
        cell["evidence_receipt"]["identity"]["test_ids"] = list(cell["test_ids"])
        base.rehash(cell)
    else:
        req = next(r for r in recorded["catalog"]["requirements"] if r["operation"] == cell["operation"])
        req["client_lane"] = cell["client_lane"] = "js-cesium"
        entry = next(p for p in recorded["catalog"]["production"]["producers"] if p["producer"] == LANE)
        entry["client_lanes"] = [LANE, "js-cesium"]
    assert expected in base.refusal(ledger=recorded["ledger"], catalog=recorded["catalog"], runs=recorded["runs"])
