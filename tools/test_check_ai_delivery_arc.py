"""Unit tests for the D9.3 release-contract and candidate-receipt gate."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_ai_delivery_arc as gate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = yaml.safe_load((ROOT / "certification" / "ai-delivery-arc.yaml").read_text(encoding="utf-8"))


def manifest() -> dict:
    return {
        "platformRelease": "2026.1-rc.2",
        "status": "rc",
        "components": {
            "honua-server": {
                "version": "pre-release",
                "sha": "a" * 40,
                "image": "ghcr.io/honua/server:candidate",
                "digest": f"sha256:{'1' * 64}",
                "controlPlane": {
                    "adminApi": {
                        "operationCount": 396,
                        "specSha256": "2" * 64,
                    }
                },
            },
            "honua-sdk-js": {"version": "0.1.8-beta.0", "sha": "b" * 40, "artifact": "npm:@honua/sdk-js"},
            "honua-console": {
                "version": "pre-release",
                "sha": "c" * 40,
                "image": "ghcr.io/honua/console:candidate",
                "digest": f"sha256:{'3' * 64}",
            },
            "honua-studio": {
                "version": "pre-release",
                "sha": "d" * 40,
                "image": "ghcr.io/honua/studio:candidate",
                "digest": f"sha256:{'4' * 64}",
            },
            "honua-iac": {"version": "pre-release", "sha": "e" * 40},
            "honua-devops": {"version": "pre-release", "sha": "f" * 40},
        },
    }


def sdk_source() -> dict:
    return {
        "serverSha": "a" * 40,
        "specSha256": "2" * 64,
        "operationCount": 396,
        "publishedAdminOperationCount": 119,
        "releaseManifestServerSha": "a" * 40,
        "releaseManifestOperationCount": 396,
        "releaseManifestStatus": "candidate-compatible",
    }


def plan() -> dict:
    stages = []
    for number, stage_contract in enumerate(CONTRACT["journey"]["stages"], start=1):
        actions = []
        for required in stage_contract["requiredActions"]:
            action = {
                "id": required["id"],
                "title": required["id"],
                "kind": required["kind"],
            }
            if "tool" in required:
                action["tool"] = required["tool"]
                action["arguments"] = {}
            elif required["kind"] == "cli":
                action["args"] = [required["id"]]
            elif required["kind"] == "gpserver":
                action.update({"serviceId": "gp", "taskName": "Buffer", "processId": "geometry.buffer"})
            elif required["kind"] == "mcp-resource":
                action.update({"uri": "honua://jobs/${jobId}", "waitForTerminal": True})
            elif required["kind"] == "receipt":
                action.update(
                    {
                        "receiptSchema": "honua.zero-to-map.console-receipt/v1",
                        "matches": dict(CONTRACT["joins"]["studioConsole"]["matches"]),
                        "requiredPointers": list(CONTRACT["joins"]["studioConsole"]["requiredPointers"]),
                        "equalPointers": list(CONTRACT["joins"]["studioConsole"]["equalPointers"]),
                    }
                )
            elif required["kind"] == "http":
                action.update({"url": "${shareUrl}", "expectedStatus": 200})
            actions.append(action)
        stages.append({"number": number, "id": stage_contract["id"], "title": stage_contract["id"], "actions": actions})

    by_id = {action["id"]: action for stage in stages for action in stage["actions"]}
    by_id["create-connection"]["captures"] = [{"variable": "connectionId", "pointers": ["/connectionId"]}]
    by_id["publish-parcels"]["captures"] = [{"variable": "parcelsLayerId", "pointers": ["/layerId"]}]
    by_id["publish-zoning"]["captures"] = [{"variable": "zoningLayerId", "pointers": ["/layerId"]}]
    by_id["buffer-esri-mcp"]["captures"] = [{"variable": "esriMcpJobId", "pointers": ["/jobId"]}]
    by_id["wait-esri-mcp-buffer"].update(
        {
            "uri": "honua://jobs/${esriMcpJobId}",
            "captures": [{"variable": "esriMcpResultsUri", "pointers": ["/resultsUri"]}],
        }
    )
    by_id["read-esri-mcp-buffer-results"]["uri"] = "${esriMcpResultsUri}"
    by_id["read-esri-mcp-buffer-results"]["captures"] = [
        {"variable": "esriMcpResultJobId", "pointers": ["/jobId"], "equals": "${esriMcpJobId}"},
        {"variable": "esriMcpResultPackageId", "pointers": ["/resultPackageId"]},
        {"variable": "esriMcpArtifactId", "pointers": ["/artifactId"]},
    ]
    by_id["buffer-esri-gpserver"]["captures"] = [{"variable": "gpServerJobId", "pointers": ["/jobId"]}]
    by_id["buffer-parcels"]["captures"] = [{"variable": "directAnalysisJobId", "pointers": ["/jobId"]}]
    by_id["wait-direct-buffer"].update(
        {
            "uri": "honua://jobs/${directAnalysisJobId}",
            "captures": [{"variable": "directAnalysisResultsUri", "pointers": ["/resultsUri"]}],
        }
    )
    by_id["read-direct-buffer-results"]["uri"] = "${directAnalysisResultsUri}"
    by_id["read-direct-buffer-results"]["captures"] = [
        {"variable": "directAnalysisResultJobId", "pointers": ["/jobId"], "equals": "${directAnalysisJobId}"},
        {"variable": "bufferArtifactId", "pointers": ["/artifactId"]},
    ]
    by_id["add-buffer-layer"]["arguments"] = {"sourceId": "honua://artifacts/${bufferArtifactId}"}
    by_id["create-draft"]["captures"] = [{"variable": "draftId", "pointers": ["/draftId"]}]
    by_id["propose-publication"]["captures"] = [
        {"variable": "proposalGeneration", "pointers": ["/generation"]}
    ]
    by_id["console-approval"]["captures"] = [
        {"variable": "candidateId", "pointers": ["/candidate/candidateId"]},
        {"variable": "releaseId", "pointers": ["/candidate/releaseId"]},
        {"variable": "shareUrl", "pointers": ["/shareUrl"]},
    ]
    return {
        "schemaVersion": CONTRACT["journey"]["schemaVersion"],
        "journeyId": CONTRACT["journey"]["journeyId"],
        "releaseContract": CONTRACT["releaseContract"],
        "variables": {"route": "zero-to-map", "serviceName": "zero-to-map"},
        "stages": stages,
    }


def receipt_for(plan_value: dict, *, mode: str, status: str) -> dict:
    stages = []
    stopped = False
    for stage in plan_value["stages"]:
        action_receipts = []
        for action in stage["actions"]:
            if mode == "contract":
                action_status = "blocked" if not stopped else "skipped"
                code = "live-execution-disabled" if not stopped else "prerequisite-not-passed"
                stopped = True
                action_receipts.append({"id": action["id"], "kind": action["kind"], "status": action_status, "code": code})
            else:
                action_receipts.append({"id": action["id"], "kind": action["kind"], "status": "passed"})
        stages.append({"number": stage["number"], "id": stage["id"], "title": stage["title"], "status": "passed", "actions": action_receipts})
    return {
        "schemaVersion": "honua.zero-to-map.receipt/v1",
        "journeyId": plan_value["journeyId"],
        "releaseContract": plan_value["releaseContract"],
        "mode": mode,
        "status": status,
        "stages": stages,
    }


def test_complete_dual_gp_contract_passes():
    findings = gate.validate_contract(
        manifest(),
        CONTRACT,
        plan(),
        sdk_head="b" * 40,
        sdk_admin_source=sdk_source(),
    )
    assert findings.status == "pass", (findings.errors, findings.blockers)


def test_discovering_esri_gp_without_ai_execution_fails():
    candidate = plan()
    gp = next(stage for stage in candidate["stages"] if stage["id"] == "geoprocessing")
    gp["actions"] = [action for action in gp["actions"] if action["id"] != "buffer-esri-mcp"]

    findings = gate.validate_contract(
        manifest(), CONTRACT, candidate, sdk_head="b" * 40, sdk_admin_source=sdk_source()
    )

    assert findings.status == "fail"
    assert any("buffer-esri-mcp" in error for error in findings.errors)


def test_removing_studio_composition_action_fails_ordered_inventory():
    candidate = plan()
    studio = next(stage for stage in candidate["stages"] if stage["id"] == "studio")
    studio["actions"] = [action for action in studio["actions"] if action["id"] != "add-chart"]

    findings = gate.validate_contract(
        manifest(), CONTRACT, candidate, sdk_head="b" * 40, sdk_admin_source=sdk_source()
    )

    assert findings.status == "fail"
    assert any("stage studio actions" in error and "add-chart" in error for error in findings.errors)


def test_stale_395_operation_release_inventory_blocks_without_lying():
    source = sdk_source()
    source.update(
        {
            "releaseManifestServerSha": "e" * 40,
            "releaseManifestOperationCount": 395,
            "releaseManifestStatus": "blocked-server-pin-regresses-admin-contract",
        }
    )

    findings = gate.validate_contract(
        manifest(), CONTRACT, plan(), sdk_head="b" * 40, sdk_admin_source=source
    )

    assert findings.status == "blocked"
    assert any("fewer than 396" in blocker for blocker in findings.blockers)


def test_contract_receipt_is_explicitly_blocked_not_fake_green(tmp_path: Path):
    plan_value = plan()
    receipt = receipt_for(plan_value, mode="contract", status="blocked")
    manifest_path = tmp_path / "platform-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest()), encoding="utf-8")

    findings, detail = gate.validate_receipt(
        manifest(), manifest_path, CONTRACT, plan_value, receipt, expected_mode="contract"
    )

    assert findings.status == "blocked", findings.errors
    assert any("did not execute the live candidate" in blocker for blocker in findings.blockers)
    assert detail["failureAttribution"]["actionId"] == "install-local"


def test_live_console_receipt_must_join_exact_manifest_identity(tmp_path: Path):
    plan_value = plan()
    receipt = receipt_for(plan_value, mode="live", status="passed")
    console = next(
        action
        for stage in receipt["stages"]
        for action in stage["actions"]
        if action["id"] == "console-approval"
    )
    console["captures"] = {
        "candidateId": "manifest-sha256:wrong",
        "releaseId": "2026.1-rc.2",
        "shareUrl": "https://example.test/apps/zero-to-map",
    }
    console["evidence"] = {"source": "external-receipt", "sha256": "3" * 64}
    manifest_path = tmp_path / "platform-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest()), encoding="utf-8")

    findings, _ = gate.validate_receipt(
        manifest(), manifest_path, CONTRACT, plan_value, receipt, expected_mode="live"
    )

    assert findings.status == "fail"
    assert any("exact platform-manifest digest" in error for error in findings.errors)


def test_live_failure_names_stage_action_and_tool(tmp_path: Path):
    plan_value = plan()
    receipt = receipt_for(plan_value, mode="live", status="blocked")
    action = next(
        action
        for stage in receipt["stages"]
        for action in stage["actions"]
        if action["id"] == "buffer-esri-mcp"
    )
    action.update({"status": "blocked", "code": "mcp-catalog-incomplete", "message": "tool absent"})
    manifest_path = tmp_path / "platform-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest()), encoding="utf-8")

    findings, detail = gate.validate_receipt(
        manifest(), manifest_path, CONTRACT, plan_value, receipt, expected_mode="live"
    )

    assert findings.status in {"blocked", "fail"}
    assert detail["failureAttribution"] == {
        "stage": 3,
        "stageId": "geoprocessing",
        "actionId": "buffer-esri-mcp",
        "tool": "honua_esri_gp_execute_task",
        "status": "blocked",
        "code": "mcp-catalog-incomplete",
        "message": "tool absent",
    }


def test_live_external_receipts_join_component_pins(tmp_path: Path):
    manifest_value = manifest()
    manifest_path = tmp_path / "platform-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest_value), encoding="utf-8")
    identity = gate.candidate_identity(manifest_value, manifest_path)
    supplied = {}
    for receipt_id, component, repository in (
        ("aws-ecs-provision", "honua-devops", "honua-io/honua-devops"),
        ("studio-real-model", "honua-studio", "honua-io/honua-studio"),
    ):
        path = tmp_path / f"{receipt_id}.json"
        value = {
            "schemaVersion": "honua.release.evidence-receipt/v1",
            "id": receipt_id,
            "status": "passed",
            "candidateId": identity["candidateId"],
            "releaseId": manifest_value["platformRelease"],
            "source": {"repository": repository, "sha": manifest_value["components"][component]["sha"]},
            "components": (
                {
                    "honua-devops": manifest_value["components"]["honua-devops"]["sha"],
                    "honua-iac": manifest_value["components"]["honua-iac"]["sha"],
                }
                if receipt_id == "aws-ecs-provision"
                else {"honua-studio": manifest_value["components"]["honua-studio"]["sha"]}
            ),
            "evidence": {"url": f"https://example.test/{receipt_id}", "sha256": "5" * 64},
        }
        path.write_text(json.dumps(value), encoding="utf-8")
        supplied[receipt_id] = (path, value)

    findings, records = gate.validate_external_receipts(
        manifest_value, manifest_path, CONTRACT, supplied
    )

    assert findings.status == "pass", (findings.errors, findings.blockers)
    assert {record["id"] for record in records} == {"aws-ecs-provision", "studio-real-model"}
    identity_components = gate.candidate_identity(manifest_value, manifest_path)["components"]
    assert identity_components["honua-devops"]["sha"] == "f" * 40
    assert identity_components["honua-iac"]["sha"] == "e" * 40
