"""Unit tests for the D9.3 release-contract and candidate-receipt gate."""
from __future__ import annotations

import json
import hashlib
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
        "serverImage": f"ghcr.io/honua/server:candidate@sha256:{'1' * 64}",
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
    by_id["add-map-buffer-layer"]["arguments"] = {"sourceId": "honua://artifacts/${bufferArtifactId}"}
    for family in ("map", "app", "dashboard"):
        by_id[f"create-{family}-draft"]["captures"] = [
            {"variable": f"{family}DraftId", "pointers": ["/draftId"]},
            {"variable": f"{family}ItemId", "pointers": ["/itemId"]},
            {"variable": f"{family}Generation", "pointers": ["/generation"]},
        ]
        by_id[f"save-{family}-version"]["captures"] = [
            {"variable": f"{family}VersionId", "pointers": ["/versionId"]},
            {"variable": f"{family}ContentHash", "pointers": ["/contentHash"]},
        ]
        by_id[f"reopen-{family}-version"]["captures"] = [
            {"variable": f"{family}ReopenedDraftId", "pointers": ["/draftId"]},
            {"variable": f"{family}ReopenedBaseVersionId", "pointers": ["/baseVersionId"]},
        ]
        by_id[f"propose-{family}-publication"]["captures"] = [
            {"variable": f"{family}ProposalGeneration", "pointers": ["/generation"]}
        ]
        by_id[f"save-{family}-publication-version"]["captures"] = [
            {"variable": f"{family}PublicationVersionId", "pointers": ["/versionId"]},
            {"variable": f"{family}PublicationContentHash", "pointers": ["/contentHash"]},
        ]
    by_id["console-approval"]["captures"] = [
        {"variable": "candidateId", "pointers": ["/candidate/candidateId"]},
        {"variable": "releaseId", "pointers": ["/candidate/releaseId"]},
        *[
            {"variable": f"{family}{suffix}", "pointers": [f"/{section}/{family}/{pointer}"]}
            for family in ("map", "app", "dashboard")
            for suffix, section, pointer in (
                ("ProposalId", "proposals", "proposalId"),
                ("PublicationId", "publications", "publicationId"),
                ("PublicationStatus", "publications", "status"),
                ("PublicUrl", "publications", "publicUrl"),
            )
        ],
        {"variable": "shareUrl", "pointers": ["/shareUrl"]},
    ]
    by_id["verify-map-public-url"]["url"] = "${mapPublicUrl}"
    by_id["verify-share-url"]["url"] = "${shareUrl}"
    by_id["verify-dashboard-public-url"]["url"] = "${dashboardPublicUrl}"
    return {
        "schemaVersion": CONTRACT["journey"]["schemaVersion"],
        "journeyId": CONTRACT["journey"]["journeyId"],
        "releaseContract": CONTRACT["releaseContract"],
        "variables": {
            "route": "zero-to-map",
            "mapRoute": "zero-to-map-map",
            "dashboardRoute": "zero-to-map-dashboard",
            "serviceName": "zero-to-map",
        },
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
                kind = action["kind"]
                evidence = {
                    "cli": {"command": "honua", "exitCode": 0},
                    "mcp": {"tool": action.get("tool"), "isError": False},
                    "mcp-resource": {"uri": "honua://jobs/job-1", "status": "Succeeded"},
                    "gpserver": {
                        "protocol": "geoservices-gp",
                        "status": "successful",
                    },
                    "receipt": {"source": "external-receipt", "sha256": "3" * 64},
                    "http": {
                        "url": "https://example.test/apps/zero-to-map",
                        "status": 200,
                        "identityMatched": True,
                    },
                }[kind]
                captures = {
                    capture["variable"]: f"fixture-{capture['variable']}"
                    for capture in action.get("captures") or []
                }
                action_receipts.append(
                    {
                        "id": action["id"],
                        "kind": kind,
                        "status": "passed",
                        "evidence": evidence,
                        **({"captures": captures} if captures else {}),
                    }
                )
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


def test_aws_target_cannot_claim_checks_its_journey_receipt_omits():
    contract = yaml.safe_load(yaml.safe_dump(CONTRACT))
    aws_receipt = next(
        receipt for receipt in contract["externalReceipts"]
        if receipt["id"] == "aws-ecs-ai-delivery-arc"
    )
    aws_receipt["claims"]["requiredChecks"].remove("public-share-http-200")

    findings = gate.validate_contract(
        manifest(), contract, plan(), sdk_head="b" * 40, sdk_admin_source=sdk_source()
    )

    assert findings.status == "fail"
    assert any("aws-ecs-ai-delivery-arc" in error and "public-share-http-200" in error for error in findings.errors)


def test_removing_studio_composition_action_fails_ordered_inventory():
    candidate = plan()
    studio = next(stage for stage in candidate["stages"] if stage["id"] == "studio")
    studio["actions"] = [action for action in studio["actions"] if action["id"] != "add-app-chart"]

    findings = gate.validate_contract(
        manifest(), CONTRACT, candidate, sdk_head="b" * 40, sdk_admin_source=sdk_source()
    )

    assert findings.status == "fail"
    assert any("stage studio actions" in error and "add-app-chart" in error for error in findings.errors)


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


def aws_real_model_documents(manifest_value: dict, candidate_id: str) -> tuple[dict, dict]:
    joins = {
        name: f"joined-{name}"
        for name in (
            "candidateId", "releaseId", "serviceName", "connectionId", "parcelsLayerId",
            "zoningLayerId", "esriMcpJobId", "esriMcpResultPackageId", "esriMcpArtifactId",
            "gpServerJobId", "directAnalysisJobId", "bufferArtifactId",
            *(
                f"{family}{suffix}"
                for family in ("map", "app", "dashboard")
                for suffix in (
                    "ItemId", "VersionId", "ContentHash", "ReopenedDraftId",
                    "PublicationVersionId", "PublicationContentHash", "ProposalId",
                    "PublicationId", "PublicationStatus", "PublicUrl", "AuditCorrelationId",
                )
            ),
        )
    }
    joins.update(
        {
            "candidateId": candidate_id,
            "releaseId": manifest_value["platformRelease"],
            **{
                f"{family}PublicationStatus": "published"
                for family in ("map", "app", "dashboard")
            },
            **{
                f"{family}PublicUrl": f"https://example.test/{family}/published"
                for family in ("map", "app", "dashboard")
            },
        }
    )

    def call(
        action_id: str,
        lane: str,
        role: str,
        name: str,
        identities: tuple[str, ...],
        *,
        family: str | None = None,
        kind: str = "mcp",
    ) -> dict:
        return {
            "actionId": action_id,
            "actionReceiptSha256": gate._canonical_sha256({"id": action_id}),
            "role": role,
            **({"family": family} if family else {}),
            "kind": kind,
            "name": name,
            "status": "passed",
            "responseSha256": "7" * 64,
            "result": {
                "status": "reconciled",
                "identities": {key: joins[key] for key in identities},
            },
        }

    lane_identity_keys = {
        "admin": (
            "candidateId", "connectionId", "parcelsLayerId", "zoningLayerId", "serviceName",
        ),
        "esriGp": (
            "candidateId", "esriMcpJobId", "esriMcpResultPackageId", "esriMcpArtifactId",
        ),
        "nativeAnalysis": (
            "candidateId", "gpServerJobId", "directAnalysisJobId", "bufferArtifactId",
        ),
        "studioPublication": (
            "candidateId",
            *(
                f"{family}{suffix}"
                for family in ("map", "app", "dashboard")
                for suffix in (
                    "ItemId", "VersionId", "ReopenedDraftId", "PublicationVersionId",
                    "ProposalId",
                )
            ),
        ),
    }
    lanes = {
        lane: {"promptSha256": "1" * 64, "transcriptSha256": "2" * 64, "calls": []}
        for lane in gate.AWS_MODEL_LANES
    }
    for action_id, lane, role, family, kind, name in gate.MODEL_ACTION_SPECS:
        lanes[lane]["calls"].append(
            call(
                action_id,
                lane,
                role,
                name.format(
                    esriMcpJobId=joins["esriMcpJobId"],
                    directAnalysisJobId=joins["directAnalysisJobId"],
                ),
                lane_identity_keys[lane],
                family=family,
                kind=kind,
            )
        )
    source = {"repository": "honua-io/honua-studio", "sha": manifest_value["components"]["honua-studio"]["sha"]}
    model = {"provider": "openai", "modelId": "gpt-live-model"}
    common = {
        "candidateId": candidate_id,
        "releaseId": manifest_value["platformRelease"],
        "endpointSha256": "3" * 64,
        "source": source,
        "model": model,
        "promptVersion": "honua.aws-ecs.ai-arc.prompt/v1",
        "evalVersion": "honua.aws-ecs.ai-arc.eval/v1",
        "transcriptSha256": "4" * 64,
    }
    evidence = {
        "schemaVersion": "honua.aws-ecs.real-model-ai-arc-evidence/v1",
        **common,
        "target": "aws-ecs",
        "provisionReceiptSha256": "5" * 64,
        "checkpointDigest": "6" * 64,
        "consoleAggregateSha256": "8" * 64,
        "consoleEvidenceSha256": "9" * 64,
        "lanes": lanes,
        "joins": joins,
    }
    receipt = {
        "schemaVersion": "honua.aws-ecs.real-model-ai-arc/v1",
        "id": "aws-ecs-real-model-ai-arc",
        "status": "passed",
        "target": "aws-ecs",
        **common,
        "components": {
            name: manifest_value["components"][name]["sha"]
            for name in ("honua-server", "honua-sdk-js", "honua-console", "honua-studio", "honua-devops", "honua-iac")
        },
        "deterministic": {
            "target": "aws-ecs",
            "provisionReceiptSha256": "5" * 64,
            "checkpointDigest": "6" * 64,
            "consoleAggregateSha256": "8" * 64,
            "consoleEvidenceSha256": "9" * 64,
        },
        "lanes": lanes,
        "joins": joins,
        "checks": {
            check: "passed"
            for check in next(
                receipt for receipt in CONTRACT["externalReceipts"]
                if receipt["id"] == "aws-ecs-real-model-ai-arc"
            )["claims"]["requiredChecks"]
        },
    }
    return receipt, evidence


def model_journey_receipt() -> dict:
    return {
        "stages": [
            {
                "actions": [
                    {"id": action_id}
                    for action_id, *_ in gate.MODEL_ACTION_SPECS
                ]
            }
        ]
    }


def local_real_model_documents(manifest_value: dict, candidate_id: str) -> tuple[dict, dict]:
    receipt, evidence = aws_real_model_documents(manifest_value, candidate_id)
    receipt.update(
        {
            "schemaVersion": "honua.local-docker.real-model-ai-arc/v1",
            "id": "local-docker-real-model-ai-arc",
            "target": "local-docker",
            "promptVersion": "honua.local-docker.ai-arc.prompt/v1",
            "evalVersion": "honua.local-docker.ai-arc.eval/v1",
            "deterministic": {
                "target": "local-docker",
                "checkpointDigest": receipt["deterministic"]["checkpointDigest"],
                "consoleAggregateSha256": receipt["deterministic"]["consoleAggregateSha256"],
                "consoleEvidenceSha256": receipt["deterministic"]["consoleEvidenceSha256"],
            },
        }
    )
    evidence.update(
        {
            "schemaVersion": "honua.local-docker.real-model-ai-arc-evidence/v1",
            "target": "local-docker",
            "promptVersion": receipt["promptVersion"],
            "evalVersion": receipt["evalVersion"],
        }
    )
    evidence.pop("provisionReceiptSha256")
    return receipt, evidence


def validate_aws_model_documents(
    tmp_path: Path, receipt: dict, evidence: dict
) -> gate.Findings:
    manifest_value = manifest()
    manifest_path = tmp_path / "platform-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest_value), encoding="utf-8")
    evidence_path = tmp_path / "model-evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    receipt["evidence"] = {
        "url": "https://example.test/model-evidence.json",
        "sha256": gate._sha256(evidence_path),
    }
    receipt_path = tmp_path / "model-receipt.json"
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    contract = {
        "externalReceipts": [
            next(
                expected
                for expected in CONTRACT["externalReceipts"]
                if expected["id"] == "aws-ecs-real-model-ai-arc"
            )
        ]
    }
    findings, _ = gate.validate_external_receipts(
        manifest_value,
        manifest_path,
        contract,
        {"aws-ecs-real-model-ai-arc": (receipt_path, receipt)},
        {"aws-ecs-real-model-ai-arc": (evidence_path, evidence)},
        {"aws-ecs": model_journey_receipt()},
    )
    return findings


def test_live_external_receipts_join_component_pins(tmp_path: Path):
    manifest_value = manifest()
    manifest_path = tmp_path / "platform-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest_value), encoding="utf-8")
    identity = gate.candidate_identity(manifest_value, manifest_path)
    supplied = {}
    supplied_evidence = {}
    expected_receipts = {
        expected["id"]: expected for expected in CONTRACT["externalReceipts"]
    }
    for receipt_id, expected in expected_receipts.items():
        component = expected["sourceComponent"]
        repository = expected["sourceRepository"]
        path = tmp_path / f"{receipt_id}.json"
        expected_claims = expected.get("claims") or {}
        if receipt_id in {
            "aws-ecs-real-model-ai-arc",
            "local-docker-real-model-ai-arc",
        }:
            documents = (
                aws_real_model_documents
                if receipt_id == "aws-ecs-real-model-ai-arc"
                else local_real_model_documents
            )
            value, evidence_value = documents(manifest_value, identity["candidateId"])
            evidence_path = tmp_path / f"{receipt_id}-evidence.json"
            evidence_path.write_text(json.dumps(evidence_value), encoding="utf-8")
            value["evidence"] = {
                "url": f"https://example.test/{receipt_id}-evidence.json",
                "sha256": gate._sha256(evidence_path),
            }
            path.write_text(json.dumps(value), encoding="utf-8")
            supplied[receipt_id] = (path, value)
            supplied_evidence[receipt_id] = (evidence_path, evidence_value)
            continue
        value = {
            "schemaVersion": "honua.release.evidence-receipt/v1",
            "id": receipt_id,
            "status": "passed",
            "candidateId": identity["candidateId"],
            "releaseId": manifest_value["platformRelease"],
            "source": {"repository": repository, "sha": manifest_value["components"][component]["sha"]},
            "components": {
                name: manifest_value["components"][name]["sha"]
                for name in expected["boundComponents"]
            },
            "evidence": {"url": f"https://example.test/{receipt_id}", "sha256": "5" * 64},
            "claims": {
                "target": expected_claims["target"],
                **(
                    {"journeyId": expected_claims["journeyId"]}
                    if "journeyId" in expected_claims
                    else {}
                ),
                **(
                    {"releaseContract": expected_claims["releaseContract"]}
                    if "releaseContract" in expected_claims
                    else {}
                ),
                "checks": {
                    check: "passed" for check in expected_claims["requiredChecks"]
                },
            },
        }
        path.write_text(json.dumps(value), encoding="utf-8")
        supplied[receipt_id] = (path, value)

    findings, records = gate.validate_external_receipts(
        manifest_value,
        manifest_path,
        CONTRACT,
        supplied,
        evidence_documents=supplied_evidence,
        target_journey_receipts={
            "aws-ecs": model_journey_receipt(),
            "local-docker": model_journey_receipt(),
        },
    )

    assert findings.status == "pass", (findings.errors, findings.blockers)
    assert {record["id"] for record in records} == {
        "aws-ecs-provision",
        "aws-ecs-ai-delivery-arc",
        "aws-ecs-real-model-ai-arc",
        "local-docker-real-model-ai-arc",
    }
    identity_components = gate.candidate_identity(manifest_value, manifest_path)["components"]
    assert identity_components["honua-devops"]["sha"] == "f" * 40
    assert identity_components["honua-iac"]["sha"] == "e" * 40


def test_missing_full_aws_arc_receipt_blocks_even_when_provisioning_exists(tmp_path: Path):
    manifest_value = manifest()
    manifest_path = tmp_path / "platform-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest_value), encoding="utf-8")

    findings, _ = gate.validate_external_receipts(
        manifest_value, manifest_path, CONTRACT, {}
    )

    assert findings.status == "blocked"
    assert any("aws-ecs-ai-delivery-arc" in blocker for blocker in findings.blockers)


def test_aws_real_model_receipt_rejects_missing_family_and_fabricated_id(tmp_path: Path):
    manifest_value = manifest()
    manifest_path = tmp_path / "identity-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest_value), encoding="utf-8")
    identity = gate.candidate_identity(manifest_value, manifest_path)
    receipt, evidence = aws_real_model_documents(manifest_value, identity["candidateId"])
    calls = receipt["lanes"]["studioPublication"]["calls"]
    receipt["lanes"]["studioPublication"]["calls"] = [
        call for call in calls if call.get("family") != "dashboard"
    ]
    receipt["lanes"]["admin"]["calls"][0]["result"]["identities"] = {
        "fabricatedConnectionId": "not-a-deterministic-resource"
    }
    evidence["lanes"] = receipt["lanes"]

    findings = validate_aws_model_documents(tmp_path, receipt, evidence)

    assert findings.status == "fail"
    assert any("fabricates or disagrees" in error for error in findings.errors)
    assert any("studioPublication" in error and "dashboard" in error for error in findings.errors)


def test_aws_real_model_receipt_rejects_generic_studio_only_blob(tmp_path: Path):
    manifest_value = manifest()
    manifest_path = tmp_path / "identity-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest_value), encoding="utf-8")
    identity = gate.candidate_identity(manifest_value, manifest_path)
    receipt, evidence = aws_real_model_documents(manifest_value, identity["candidateId"])
    receipt["lanes"] = {"studioPublication": receipt["lanes"]["studioPublication"]}
    evidence["lanes"] = receipt["lanes"]

    findings = validate_aws_model_documents(tmp_path, receipt, evidence)

    assert findings.status == "fail"
    assert any("four required natural-language lanes" in error for error in findings.errors)


def test_aws_real_model_receipt_requires_exact_ordered_action_roster(tmp_path: Path):
    manifest_value = manifest()
    manifest_path = tmp_path / "identity-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest_value), encoding="utf-8")
    candidate_id = gate.candidate_identity(manifest_value, manifest_path)["candidateId"]

    receipt, evidence = aws_real_model_documents(manifest_value, candidate_id)
    receipt["lanes"]["admin"]["calls"].pop()
    findings = validate_aws_model_documents(tmp_path, receipt, evidence)
    assert findings.status == "fail"
    assert any("canonical action multiplicity" in error for error in findings.errors)

    receipt, evidence = aws_real_model_documents(manifest_value, candidate_id)
    receipt["lanes"]["admin"]["calls"].append(
        json.loads(json.dumps(receipt["lanes"]["admin"]["calls"][-1]))
    )
    findings = validate_aws_model_documents(tmp_path, receipt, evidence)
    assert findings.status == "fail"
    assert any("canonical action multiplicity" in error for error in findings.errors)
    assert any("extra non-canonical action" in error for error in findings.errors)

    receipt, evidence = aws_real_model_documents(manifest_value, candidate_id)
    calls = receipt["lanes"]["admin"]["calls"]
    calls[0], calls[1] = calls[1], calls[0]
    findings = validate_aws_model_documents(tmp_path, receipt, evidence)
    assert findings.status == "fail"
    assert any("call 0 is not canonical action admin-status" in error for error in findings.errors)

    receipt, evidence = aws_real_model_documents(manifest_value, candidate_id)
    calls = receipt["lanes"]["admin"]["calls"]
    for field in ("actionId", "actionReceiptSha256"):
        calls[0][field], calls[1][field] = calls[1][field], calls[0][field]
    findings = validate_aws_model_documents(tmp_path, receipt, evidence)
    assert findings.status == "fail"
    assert any("call 0 is not canonical action admin-status" in error for error in findings.errors)


def test_real_model_schemas_admit_every_canonical_action_transport():
    canonical_kinds = {spec[4] for spec in gate.MODEL_ACTION_SPECS}
    manifest_value = manifest()
    receipt, _ = aws_real_model_documents(manifest_value, "manifest-sha256:" + "a" * 64)
    required_join_names = set(receipt["joins"])
    assert required_join_names == set(gate.MODEL_JOIN_NAMES)
    for schema_name in (
        "aws-ecs-real-model-ai-arc.schema.json",
        "aws-ecs-real-model-ai-arc-evidence.schema.json",
        "local-docker-real-model-ai-arc.schema.json",
    ):
        schema = json.loads((ROOT / "certification" / schema_name).read_text(encoding="utf-8"))
        assert set(schema["$defs"]["call"]["properties"]["kind"]["enum"]) == canonical_kinds
        joins_schema = schema["properties"]["joins"]
        assert set(joins_schema["required"]) == required_join_names
        assert joins_schema["additionalProperties"] is False
        assert len(joins_schema["patternProperties"]) == 1
    local_evidence_schema = json.loads(
        (ROOT / "certification" / "local-docker-real-model-ai-arc-evidence.schema.json").read_text(
            encoding="utf-8"
        )
    )
    joins_schema = local_evidence_schema["properties"]["joins"]
    assert set(joins_schema["required"]) == required_join_names
    assert joins_schema["additionalProperties"] is False
    assert len(joins_schema["patternProperties"]) == 1


def test_real_model_receipt_rejects_extra_secret_shaped_join(tmp_path: Path):
    manifest_value = manifest()
    manifest_path = tmp_path / "identity-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest_value), encoding="utf-8")
    candidate_id = gate.candidate_identity(manifest_value, manifest_path)["candidateId"]
    receipt, evidence = aws_real_model_documents(manifest_value, candidate_id)
    receipt["joins"]["accessToken"] = "forwardable-credential"
    evidence["joins"] = receipt["joins"]

    findings = validate_aws_model_documents(tmp_path, receipt, evidence)

    assert findings.status == "fail"
    assert any("non-canonical deterministic joins" in error for error in findings.errors)
    assert any("forbidden secret" in error for error in findings.errors)


def test_aws_model_receipt_requires_sdk_action_digest_and_gpserver_join(tmp_path: Path):
    manifest_value = manifest()
    manifest_path = tmp_path / "identity-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest_value), encoding="utf-8")
    candidate_id = gate.candidate_identity(manifest_value, manifest_path)["candidateId"]
    receipt, evidence = aws_real_model_documents(manifest_value, candidate_id)
    gp_call = receipt["lanes"]["nativeAnalysis"]["calls"][0]
    gp_call["actionReceiptSha256"] = "f" * 64
    gp_call["result"]["identities"].pop("gpServerJobId")
    evidence["lanes"] = receipt["lanes"]

    findings = validate_aws_model_documents(tmp_path, receipt, evidence)

    assert findings.status == "fail"
    assert any("buffer-esri-gpserver" in error and "SDK journey receipt" in error for error in findings.errors)
    assert any("buffer-esri-gpserver" in error and "gpServerJobId" in error for error in findings.errors)


def test_real_model_action_roster_is_the_release_contract_inventory():
    required_actions = [
        action
        for stage in CONTRACT["journey"]["stages"]
        for action in stage["requiredActions"]
        if action["kind"] in {"mcp", "mcp-resource", "gpserver"}
    ]
    assert [spec[0] for spec in gate.MODEL_ACTION_SPECS] == [
        action["id"] for action in required_actions
    ]
    for spec, required in zip(gate.MODEL_ACTION_SPECS, required_actions, strict=True):
        assert spec[4] == required["kind"]
        if "tool" in required:
            assert spec[5] == required["tool"]


def test_live_pass_without_per_action_evidence_fails(tmp_path: Path):
    plan_value = plan()
    receipt = receipt_for(plan_value, mode="live", status="passed")
    action = next(
        action
        for stage in receipt["stages"]
        for action in stage["actions"]
        if action["id"] == "buffer-esri-gpserver"
    )
    action.pop("evidence")
    manifest_path = tmp_path / "platform-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest()), encoding="utf-8")

    findings, _ = gate.validate_receipt(
        manifest(), manifest_path, CONTRACT, plan_value, receipt, expected_mode="live"
    )

    assert findings.status == "fail"
    assert any("buffer-esri-gpserver" in error and "without execution evidence" in error for error in findings.errors)


def test_live_final_share_url_must_be_public_https(tmp_path: Path):
    plan_value = plan()
    receipt = receipt_for(plan_value, mode="live", status="passed")
    console = next(
        action
        for stage in receipt["stages"]
        for action in stage["actions"]
        if action["id"] == "console-approval"
    )
    console["captures"].update(
        {
            "candidateId": "manifest-sha256:wrong-until-identity-check",
            "releaseId": "2026.1-rc.2",
            "shareUrl": "http://127.0.0.1:8080/apps/zero-to-map",
        }
    )
    final = next(
        action
        for stage in receipt["stages"]
        for action in stage["actions"]
        if action["id"] == "verify-share-url"
    )
    final["evidence"] = {"url": "http://127.0.0.1:8080/apps/zero-to-map", "status": 200}
    manifest_path = tmp_path / "platform-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest()), encoding="utf-8")

    findings, _ = gate.validate_receipt(
        manifest(), manifest_path, CONTRACT, plan_value, receipt, expected_mode="live"
    )

    assert findings.status == "fail"
    assert any("public HTTPS" in error for error in findings.errors)


def test_live_map_and_dashboard_probes_must_match_exact_console_urls(tmp_path: Path):
    plan_value = plan()
    receipt = receipt_for(plan_value, mode="live", status="passed")
    manifest_value = manifest()
    manifest_path = tmp_path / "platform-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest_value), encoding="utf-8")
    console = next(
        action
        for stage in receipt["stages"]
        for action in stage["actions"]
        if action["id"] == "console-approval"
    )
    public_urls = {
        "mapPublicUrl": "https://example.test/maps/published-map",
        "shareUrl": "https://example.test/apps/published-app",
        "appPublicUrl": "https://example.test/apps/published-app",
        "dashboardPublicUrl": "https://example.test/dashboards/published-dashboard",
    }
    console["captures"].update(
        {
            "candidateId": gate.candidate_identity(manifest_value, manifest_path)["candidateId"],
            "releaseId": manifest_value["platformRelease"],
            **public_urls,
        }
    )
    actions = {
        action["id"]: action
        for stage in receipt["stages"]
        for action in stage["actions"]
    }
    actions["verify-map-public-url"]["evidence"]["url"] = public_urls["mapPublicUrl"]
    actions["verify-share-url"]["evidence"]["url"] = public_urls["shareUrl"]
    actions["verify-dashboard-public-url"]["evidence"]["url"] = (
        "https://example.test/dashboards/a-different-dashboard"
    )

    findings, _ = gate.validate_receipt(
        manifest_value,
        manifest_path,
        CONTRACT,
        plan_value,
        receipt,
        expected_mode="live",
    )

    assert findings.status == "fail"
    assert any(
        "verify-dashboard-public-url did not probe its exact Console URL" in error
        for error in findings.errors
    )
