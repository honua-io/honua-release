#!/usr/bin/env python3
"""Validate and bind the D9.3 seven-stage SDK journey to one release candidate.

The journey implementation belongs to honua-sdk-js. This release-side checker
only consumes its checked-in plan and receipt, verifies the cross-component
contract, and emits a candidate-bound report with actionable failure attribution.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RELEASE_ACTIONS_RUN = re.compile(
    r"^/honua-io/honua-release/actions/runs/[1-9][0-9]*/?$"
)
RELEASE_PUBLIC_HTTP_ACTIONS = frozenset(
    {
        "verify-map-public-url",
        "verify-share-url",
        "verify-dashboard-public-url",
    }
)
AWS_PROVISION_CHECKS = (
    "terraform-plan",
    "terraform-apply",
    "readiness",
    "admin-mcp-handoff",
)
AWS_TEARDOWN_CHECKS = ("terraform-destroy", "cleanup-verified")
AWS_ARC_CHECKS = (
    "candidate-image-install",
    "admin-configure-and-publish",
    "esri-gp-mcp",
    "esri-gpserver",
    "native-analysis-artifact",
    "studio-map-app-dashboard-save-reopen",
    "governed-publication-approval",
    "console-audit-recovery",
    "public-share-http-200",
)
AWS_ARC_COMPONENTS = (
    "honua-server",
    "honua-sdk-js",
    "honua-console",
    "honua-studio",
    "honua-devops",
    "honua-iac",
)
AWS_FINAL_ARTIFACTS = (
    "platformManifest",
    "provisionBinding",
    "secretlessHandoff",
    "sdkJourneyReceipt",
    "sdkCheckpoint",
    "consoleReceipt",
    "sdkConsoleReceipt",
    "awsEcsRealModelReceipt",
    "awsEcsRealModelEvidence",
    "teardownEvidence",
)
AWS_FINAL_ARTIFACT_FILES = {
    "provisionBinding": "provision-binding.json",
    "secretlessHandoff": "handoff.json",
    "sdkJourneyReceipt": "sdk-journey.json",
    "consoleReceipt": "console-receipt.json",
    "sdkConsoleReceipt": "sdk-console-receipt.json",
    "awsEcsRealModelReceipt": "real-model-receipt.json",
    "awsEcsRealModelEvidence": "real-model-evidence.json",
    "teardownEvidence": "teardown-evidence.json",
}
AWS_MODEL_LANES = ("admin", "esriGp", "nativeAnalysis", "studioPublication")
# Exact ordered 58-action model roster shared with the Studio producer and
# DevOps cloud verifier. Tuple fields: actionId, lane, role, family, kind, name.
MODEL_ACTION_SPECS = (
    ("admin-status", "admin", "server-status", None, "mcp", "honua_admin_server_status"),
    ("create-connection", "admin", "connection-create", None, "mcp", "honua_admin_connection_create"),
    ("test-connection", "admin", "connection-test", None, "mcp", "honua_admin_connection_test"),
    ("import-parcels", "admin", "import-upload-url", "parcels", "mcp", "honua_admin_import_upload_url"),
    ("import-zoning", "admin", "import-upload-url", "zoning", "mcp", "honua_admin_import_upload_url"),
    ("publish-parcels", "admin", "layer-publish", "parcels", "mcp", "honua_admin_layer_publish"),
    ("publish-zoning", "admin", "layer-publish", "zoning", "mcp", "honua_admin_layer_publish"),
    ("set-public-access", "admin", "service-access", None, "mcp", "honua_admin_service_set_access_policy"),
    ("create-scoped-key", "admin", "scoped-key-create", None, "mcp", "honua_admin_api_key_create"),
    ("list-esri-gp-tasks", "esriGp", "list-tasks", None, "mcp", "honua_esri_gp_list_tasks"),
    ("describe-esri-buffer", "esriGp", "describe-buffer", None, "mcp", "honua_esri_gp_describe_task"),
    ("buffer-esri-mcp", "esriGp", "execute-buffer", None, "mcp", "honua_esri_gp_execute_task"),
    ("wait-esri-mcp-buffer", "esriGp", "wait-buffer", None, "mcp-resource", "honua://jobs/{esriMcpJobId}"),
    ("read-esri-mcp-buffer-results", "esriGp", "read-buffer-results", None, "mcp-resource", "honua://jobs/{esriMcpJobId}/results"),
    ("buffer-esri-gpserver", "nativeAnalysis", "execute-buffer-gpserver", None, "gpserver", "GPServer/analysis/Buffer"),
    ("buffer-parcels", "nativeAnalysis", "execute-buffer", None, "mcp", "honua_buffer_features"),
    ("wait-direct-buffer", "nativeAnalysis", "wait-buffer", None, "mcp-resource", "honua://jobs/{directAnalysisJobId}"),
    ("read-direct-buffer-results", "nativeAnalysis", "read-buffer-results", None, "mcp-resource", "honua://jobs/{directAnalysisJobId}/results"),
    ("create-map-draft", "studioPublication", "create-draft", "map", "mcp", "honua_studio_create_draft"),
    ("add-map-parcels-layer", "studioPublication", "add-layer", "map", "mcp", "honua_studio_add_layer"),
    ("add-map-buffer-layer", "studioPublication", "add-layer", "map", "mcp", "honua_studio_add_layer"),
    ("style-map-buffer-layer", "studioPublication", "set-layer-style", "map", "mcp", "honua_studio_set_layer_style"),
    ("set-map-buffer-visibility", "studioPublication", "set-layer-visibility", "map", "mcp", "honua_studio_set_layer_visibility"),
    ("set-map-view", "studioPublication", "set-view", "map", "mcp", "honua_studio_set_view"),
    ("add-map-widget", "studioPublication", "add-widget", "map", "mcp", "honua_studio_add_widget"),
    ("add-map-control", "studioPublication", "add-control", "map", "mcp", "honua_studio_add_control"),
    ("validate-map-draft", "studioPublication", "validate-draft", "map", "mcp", "honua_studio_validate_draft"),
    ("save-map-version", "studioPublication", "save-version", "map", "mcp", "honua_studio_save_version"),
    ("get-map-version", "studioPublication", "get-version", "map", "mcp", "honua_studio_get_version"),
    ("reopen-map-version", "studioPublication", "reopen-version", "map", "mcp", "honua_studio_reopen_version"),
    ("create-app-draft", "studioPublication", "create-draft", "app", "mcp", "honua_studio_create_draft"),
    ("add-app-parcels-layer", "studioPublication", "add-layer", "app", "mcp", "honua_studio_add_layer"),
    ("add-app-buffer-layer", "studioPublication", "add-layer", "app", "mcp", "honua_studio_add_layer"),
    ("style-app-buffer-layer", "studioPublication", "set-layer-style", "app", "mcp", "honua_studio_set_layer_style"),
    ("set-app-view", "studioPublication", "set-view", "app", "mcp", "honua_studio_set_view"),
    ("add-app-chart", "studioPublication", "add-widget", "app", "mcp", "honua_studio_add_widget"),
    ("add-app-layer-control", "studioPublication", "add-control", "app", "mcp", "honua_studio_add_control"),
    ("bind-app-chart-interaction", "studioPublication", "bind-interaction", "app", "mcp", "honua_studio_bind_interaction"),
    ("validate-app-draft", "studioPublication", "validate-draft", "app", "mcp", "honua_studio_validate_draft"),
    ("save-app-version", "studioPublication", "save-version", "app", "mcp", "honua_studio_save_version"),
    ("get-app-version", "studioPublication", "get-version", "app", "mcp", "honua_studio_get_version"),
    ("reopen-app-version", "studioPublication", "reopen-version", "app", "mcp", "honua_studio_reopen_version"),
    ("create-dashboard-draft", "studioPublication", "create-draft", "dashboard", "mcp", "honua_studio_create_draft"),
    ("add-dashboard-buffer-layer", "studioPublication", "add-layer", "dashboard", "mcp", "honua_studio_add_layer"),
    ("style-dashboard-buffer-layer", "studioPublication", "set-layer-style", "dashboard", "mcp", "honua_studio_set_layer_style"),
    ("set-dashboard-view", "studioPublication", "set-view", "dashboard", "mcp", "honua_studio_set_view"),
    ("add-dashboard-chart", "studioPublication", "add-widget", "dashboard", "mcp", "honua_studio_add_widget"),
    ("add-dashboard-layer-control", "studioPublication", "add-control", "dashboard", "mcp", "honua_studio_add_control"),
    ("validate-dashboard-draft", "studioPublication", "validate-draft", "dashboard", "mcp", "honua_studio_validate_draft"),
    ("save-dashboard-version", "studioPublication", "save-version", "dashboard", "mcp", "honua_studio_save_version"),
    ("get-dashboard-version", "studioPublication", "get-version", "dashboard", "mcp", "honua_studio_get_version"),
    ("reopen-dashboard-version", "studioPublication", "reopen-version", "dashboard", "mcp", "honua_studio_reopen_version"),
    ("propose-map-publication", "studioPublication", "propose-publication", "map", "mcp", "honua_studio_propose_publication"),
    ("save-map-publication-version", "studioPublication", "save-version", "map", "mcp", "honua_studio_save_version"),
    ("propose-app-publication", "studioPublication", "propose-publication", "app", "mcp", "honua_studio_propose_publication"),
    ("save-app-publication-version", "studioPublication", "save-version", "app", "mcp", "honua_studio_save_version"),
    ("propose-dashboard-publication", "studioPublication", "propose-publication", "dashboard", "mcp", "honua_studio_propose_publication"),
    ("save-dashboard-publication-version", "studioPublication", "save-version", "dashboard", "mcp", "honua_studio_save_version"),
)
MODEL_JOIN_NAMES = (
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


@dataclass
class Findings:
    errors: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.errors:
            return "fail"
        if self.blockers:
            return "blocked"
        return "pass"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _contains_forbidden_evidence(value: Any, tokens: tuple[str, ...]) -> bool:
    lowered = tuple(token.lower() for token in tokens)
    safe_control_fields = {"no-secret-serialization"}

    def visit(item: Any, field_name: str | None = None) -> bool:
        if isinstance(item, dict):
            return any(
                (
                    str(name).lower() not in safe_control_fields
                    and any(token in str(name).lower() for token in lowered)
                )
                or visit(child, str(name).lower())
                for name, child in item.items()
            )
        if isinstance(item, list):
            return any(visit(child, field_name) for child in item)
        if isinstance(item, str) and field_name != "name":
            normalized = item.lower()
            return any(token in normalized for token in lowered)
        return False

    return visit(value)


def _load_yaml(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return value


def _load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _action_map(plan: dict) -> dict[str, dict]:
    actions: dict[str, dict] = {}
    for stage in plan.get("stages") or []:
        for action in stage.get("actions") or []:
            action_id = action.get("id")
            if isinstance(action_id, str):
                actions[action_id] = action
    return actions


def _receipt_action_map(receipt: dict) -> dict[str, dict]:
    actions: dict[str, dict] = {}
    for stage in receipt.get("stages") or []:
        for action in stage.get("actions") or []:
            action_id = action.get("id")
            if isinstance(action_id, str):
                actions[action_id] = action
    return actions


def _ordered_stage_action_inventory(
    document: dict,
    *,
    label: str,
    findings: Findings,
) -> tuple[tuple[str, tuple[str, ...]], ...] | None:
    """Return a lossless stage/action roster after rejecting malformed multiplicity."""
    stages = document.get("stages")
    if not isinstance(stages, list):
        findings.errors.append(f"{label} stages must be an array")
        return None

    inventory: list[tuple[str, tuple[str, ...]]] = []
    stage_ids: set[str] = set()
    action_ids: set[str] = set()
    valid = True
    for stage_index, stage in enumerate(stages):
        if not isinstance(stage, dict):
            findings.errors.append(f"{label} stage {stage_index} must be an object")
            valid = False
            continue
        stage_id = stage.get("id")
        if not isinstance(stage_id, str) or not stage_id:
            findings.errors.append(f"{label} stage {stage_index} has no non-empty id")
            valid = False
            continue
        if stage_id in stage_ids:
            findings.errors.append(f"{label} contains duplicate stage id {stage_id}")
            valid = False
        stage_ids.add(stage_id)
        actions = stage.get("actions")
        if not isinstance(actions, list):
            findings.errors.append(f"{label} stage {stage_id} actions must be an array")
            valid = False
            continue
        ordered_actions: list[str] = []
        for action_index, action in enumerate(actions):
            if not isinstance(action, dict):
                findings.errors.append(
                    f"{label} stage {stage_id} action {action_index} must be an object"
                )
                valid = False
                continue
            action_id = action.get("id")
            if not isinstance(action_id, str) or not action_id:
                findings.errors.append(
                    f"{label} stage {stage_id} action {action_index} has no non-empty id"
                )
                valid = False
                continue
            if action_id in action_ids:
                findings.errors.append(
                    f"{label} contains duplicate action id {action_id}"
                )
                valid = False
            action_ids.add(action_id)
            ordered_actions.append(action_id)
        inventory.append((stage_id, tuple(ordered_actions)))
    return tuple(inventory) if valid else None


def _capture_names(action: dict) -> set[str]:
    return {
        capture.get("variable")
        for capture in action.get("captures") or []
        if isinstance(capture, dict) and isinstance(capture.get("variable"), str)
    }


def _contains_template(value: Any, template: str) -> bool:
    if value == template:
        return True
    if isinstance(value, dict):
        return any(_contains_template(item, template) for item in value.values())
    if isinstance(value, list):
        return any(_contains_template(item, template) for item in value)
    if isinstance(value, str):
        return template in value
    return False


def _is_public_https_url(value: Any) -> bool:
    try:
        parsed = urlparse(str(value))
        # Accessing port also rejects malformed authorities such as :not-a-port.
        _ = parsed.port
        try:
            host = (
                (parsed.hostname or "")
                .encode("idna")
                .decode("ascii")
                .lower()
                .rstrip(".")
            )
        except UnicodeError:
            return False
        if (
            parsed.scheme != "https"
            or not host
            or "%" in parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or host in {"localhost", "localhost.localdomain"}
            or host.endswith((".localhost", ".local", ".internal", ".localdomain"))
        ):
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            labels = host.split(".")
            if all(
                re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)", item)
                for item in labels
            ):
                return False
            # A single-label DNS name is an intranet name, not a customer-facing
            # publication identity. Fully qualified test fixtures use a dotted name.
            return "." in host
        return address.is_global and not address.is_multicast
    except ValueError:
        return False


def _is_actions_run_url(value: Any) -> bool:
    """Accept only an immutable Actions run in the governed release repository."""
    try:
        parsed = urlparse(str(value))
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.netloc == "github.com"
        and parsed.hostname == "github.com"
        and parsed.port is None
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and RELEASE_ACTIONS_RUN.fullmatch(parsed.path) is not None
    )


def _git_head(path: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def candidate_identity(manifest: dict, manifest_path: Path) -> dict:
    components = manifest.get("components") or {}
    selected: dict[str, dict] = {}
    for name, component in sorted(components.items()):
        if isinstance(component, dict):
            selected[name] = {
                key: component[key]
                for key in ("version", "sha", "image", "digest", "artifact")
                if component.get(key) not in (None, "")
            }
    digest = _sha256(manifest_path)
    return {
        "platformRelease": manifest.get("platformRelease"),
        "candidateId": f"manifest-sha256:{digest}",
        "manifestSha256": digest,
        "components": selected,
    }


def validate_contract(
    manifest: dict,
    contract: dict,
    plan: dict | None,
    *,
    sdk_head: str | None = None,
    sdk_admin_source: dict | None = None,
) -> Findings:
    findings = Findings()
    if contract.get("schemaVersion") != "honua.ai-delivery-arc-contract/v1":
        findings.errors.append("release contract has an unsupported schemaVersion")
        return findings

    components = manifest.get("components") or {}
    required_components = (contract.get("candidate") or {}).get("requiredComponents") or []
    for name in required_components:
        component = components.get(name)
        if not isinstance(component, dict) or not SHA40.fullmatch(str(component.get("sha", ""))):
            findings.blockers.append(f"candidate component {name} has no exact 40-character SHA pin")
    for name in ("honua-server", "honua-console", "honua-studio"):
        component = components.get(name) or {}
        if not component.get("image") or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(component.get("digest", ""))):
            findings.blockers.append(f"candidate component {name} has no immutable image+digest pin")
    if not (components.get("honua-sdk-js") or {}).get("artifact"):
        findings.blockers.append("candidate component honua-sdk-js has no artifact coordinate")

    target_ids = [target.get("id") for target in contract.get("executionTargets") or []]
    if target_ids != ["local-docker", "aws-ecs"]:
        findings.errors.append(
            f"AI delivery-arc targets are {target_ids!r}; expected ['local-docker', 'aws-ecs']"
        )
    external_by_id = {
        receipt.get("id"): receipt for receipt in contract.get("externalReceipts") or []
    }
    external_ids = set(external_by_id)
    for target in contract.get("executionTargets") or []:
        if not target.get("requiredChecks"):
            findings.errors.append(f"execution target {target.get('id')} has no required full-arc checks")
        for key in ("provisionReceipt", "journeyReceipt"):
            receipt_id = target.get(key)
            if receipt_id and receipt_id != "sdk-zero-to-map" and receipt_id not in external_ids:
                findings.errors.append(
                    f"execution target {target.get('id')} references unknown {key} {receipt_id}"
                )
            if key == "journeyReceipt" and receipt_id in external_by_id:
                claimed_checks = set(
                    ((external_by_id[receipt_id].get("claims") or {}).get("requiredChecks") or [])
                )
                missing_checks = set(target.get("requiredChecks") or []) - claimed_checks
                if missing_checks:
                    findings.errors.append(
                        f"execution target {target.get('id')} journey receipt {receipt_id} "
                        f"omits checks {sorted(missing_checks)}"
                    )
        for receipt_id in target.get("supportingReceipts") or []:
            if receipt_id not in external_ids:
                findings.errors.append(
                    f"execution target {target.get('id')} references unknown supporting receipt {receipt_id}"
                )

    server = components.get("honua-server") or {}
    admin_api = ((server.get("controlPlane") or {}).get("adminApi") or {})
    wanted_rest = (contract.get("inventory") or {}).get("adminRestOperations")
    if admin_api.get("operationCount") != wanted_rest:
        findings.errors.append(
            f"manifest honua-server.controlPlane.adminApi.operationCount={admin_api.get('operationCount')!r}; "
            f"D9.3 requires {wanted_rest}"
        )
    if not SHA256.fullmatch(str(admin_api.get("specSha256", ""))):
        findings.errors.append("manifest admin API pin has no exact specSha256")
    control_plane_status = str((server.get("controlPlane") or {}).get("status", ""))
    if control_plane_status.startswith("blocked"):
        findings.blockers.append(f"manifest control plane reports {control_plane_status}")

    sdk = components.get("honua-sdk-js") or {}
    if sdk_head is not None and sdk_head != sdk.get("sha"):
        findings.errors.append(
            f"SDK journey checkout is {sdk_head}; candidate manifest pins {sdk.get('sha') or '<missing>'}"
        )

    if sdk_admin_source is None:
        findings.blockers.append("manifest-pinned SDK has no config/admin-client.v1.json inventory receipt")
    else:
        expected_tools = (contract.get("inventory") or {}).get("publishedAdminTools")
        if sdk_admin_source.get("operationCount") != wanted_rest:
            findings.errors.append("SDK admin client does not cover the complete 396-operation Admin API")
        if sdk_admin_source.get("publishedAdminOperationCount") != expected_tools:
            findings.errors.append("SDK admin MCP inventory does not declare the 119-tool semantic family")
        if sdk_admin_source.get("releaseManifestServerSha") != server.get("sha"):
            findings.blockers.append(
                "SDK admin inventory is not bound to the manifest-pinned honua-server SHA"
            )
        if sdk_admin_source.get("releaseManifestOperationCount") != wanted_rest:
            findings.blockers.append(
                "SDK admin inventory still records a release candidate with fewer than 396 REST operations"
            )
        status = str(sdk_admin_source.get("releaseManifestStatus", ""))
        if status and status.startswith("blocked"):
            findings.blockers.append(f"SDK admin inventory reports {status}")
        if sdk_admin_source.get("serverSha") != server.get("sha"):
            findings.blockers.append("SDK generated Admin API source is not the manifest-pinned server SHA")
        if sdk_admin_source.get("specSha256") != admin_api.get("specSha256"):
            findings.errors.append("SDK generated Admin API digest does not match the manifest Admin API digest")
        candidate_image = (
            f"{server.get('image')}@{server.get('digest')}"
            if server.get("image") and server.get("digest")
            else None
        )
        if sdk_admin_source.get("serverImage") != candidate_image:
            findings.blockers.append(
                "SDK local installer image is not the immutable manifest-pinned honua-server image"
            )

    if plan is None:
        findings.blockers.append("manifest-pinned SDK does not contain the D9.3 zero-to-map plan")
        return findings

    journey = contract.get("journey") or {}
    if plan.get("schemaVersion") != journey.get("schemaVersion"):
        findings.errors.append("SDK journey plan schemaVersion does not match the release contract")
    if plan.get("journeyId") != journey.get("journeyId"):
        findings.errors.append("SDK journeyId does not match the release contract")
    if plan.get("releaseContract") != contract.get("releaseContract"):
        findings.errors.append("SDK journey is not bound to honua-release#123/D9.3")

    stages = plan.get("stages") or []
    required_stages = journey.get("stages") or []
    actual_stage_ids = [stage.get("id") for stage in stages]
    required_stage_ids = [stage.get("id") for stage in required_stages]
    if len(stages) != 7 or actual_stage_ids != required_stage_ids:
        findings.errors.append(
            f"SDK journey stages are {actual_stage_ids!r}; expected the ordered seven-stage arc {required_stage_ids!r}"
        )

    actions = _action_map(plan)
    if len(actions) != sum(len(stage.get("actions") or []) for stage in stages):
        findings.errors.append("SDK journey action IDs are missing or duplicated")
    for stage in required_stages:
        actual_stage = next((item for item in stages if item.get("id") == stage.get("id")), {})
        actual_action_ids = [action.get("id") for action in actual_stage.get("actions") or []]
        required_action_ids = [action.get("id") for action in stage.get("requiredActions") or []]
        if actual_action_ids != required_action_ids:
            findings.errors.append(
                f"stage {stage.get('id')} actions are {actual_action_ids!r}; "
                f"expected the ordered inventory {required_action_ids!r}"
            )
        for required in stage.get("requiredActions") or []:
            action = actions.get(required.get("id"))
            if action is None:
                findings.errors.append(
                    f"stage {stage.get('id')} is missing required action {required.get('id')}"
                )
                continue
            for key in ("kind", "tool"):
                if key in required and action.get(key) != required.get(key):
                    findings.errors.append(
                        f"action {required.get('id')} has {key}={action.get(key)!r}; expected {required.get(key)!r}"
                    )

    admin_prefix = (contract.get("inventory") or {}).get("adminToolPrefix", "honua_admin_")
    admin_stage = next((stage for stage in stages if stage.get("id") == "admin"), {})
    for action in admin_stage.get("actions") or []:
        if action.get("kind") != "mcp" or not str(action.get("tool", "")).startswith(admin_prefix):
            findings.errors.append(
                f"admin stage action {action.get('id')} bypasses the governed {admin_prefix} MCP family"
            )

    for join_name, join in (contract.get("joins") or {}).items():
        producer = actions.get(join.get("producerAction") or join.get("receiptAction"))
        consumer = actions.get(join.get("consumerAction")) if join.get("consumerAction") else None
        capture = join.get("capture")
        if capture and (producer is None or capture not in _capture_names(producer)):
            findings.errors.append(f"{join_name} producer does not capture {capture}")
        template = join.get("template")
        if template and (consumer is None or not _contains_template(consumer, template)):
            findings.errors.append(f"{join_name} consumer does not use {template}")
        result_action = actions.get(join.get("resultAction")) if join.get("resultAction") else None
        result_capture = join.get("resultCapture")
        if result_capture:
            captures = {
                capture.get("variable"): capture
                for capture in (result_action or {}).get("captures") or []
                if isinstance(capture, dict)
            }
            if captures.get(result_capture, {}).get("equals") != join.get("equals"):
                findings.errors.append(
                    f"{join_name} result does not join {result_capture} to {join.get('equals')}"
                )

    studio_console = (contract.get("joins") or {}).get("studioConsole") or {}
    receipt_action = actions.get(studio_console.get("receiptAction")) or {}
    actual_matches = receipt_action.get("matches") or {}
    for pointer, template in (studio_console.get("matches") or {}).items():
        if actual_matches.get(pointer) != template:
            findings.errors.append(f"Console receipt does not join {pointer} to {template}")
    available_variables = {
        *(plan.get("variables") or {}).keys(),
        "journeyId",
        "releaseContract",
    }
    for action in actions.values():
        available_variables.update(_capture_names(action))
    for pointer, template in (studio_console.get("matches") or {}).items():
        variables = re.findall(r"\$\{([^}]+)\}", str(template))
        for variable in variables:
            if variable not in available_variables:
                findings.errors.append(
                    f"Console receipt join {pointer} references uncaptured variable {variable}"
                )
    required_pointers = set(receipt_action.get("requiredPointers") or [])
    missing_pointers = set(studio_console.get("requiredPointers") or []) - required_pointers
    if missing_pointers:
        findings.errors.append(
            f"Console receipt omits required candidate/checkpoint pointers: {sorted(missing_pointers)}"
        )
    required_equal = {tuple(pair) for pair in studio_console.get("equalPointers") or []}
    actual_equal = {tuple(pair) for pair in receipt_action.get("equalPointers") or []}
    if not required_equal.issubset(actual_equal):
        findings.errors.append(
            f"Console receipt omits identity equality joins: {sorted(required_equal - actual_equal)}"
        )

    return findings


def _validate_exact_passed_checks(
    findings: Findings,
    checks: Any,
    expected: tuple[str, ...] | list[str],
    *,
    label: str,
) -> None:
    expected_set = set(expected)
    if not isinstance(checks, dict):
        findings.errors.append(f"{label} checks must be an object")
        return
    if set(checks) != expected_set:
        findings.errors.append(
            f"{label} check inventory drift "
            f"(missing={sorted(expected_set - set(checks))}, "
            f"extra={sorted(set(checks) - expected_set)})"
        )
    for check in expected:
        if checks.get(check) != "passed":
            findings.errors.append(f"{label} does not prove {check}=passed")


def _load_digest_bound_json(
    findings: Findings,
    evidence_path: Path,
    filename: str,
    digest: Any,
    *,
    label: str,
) -> tuple[Path, dict] | None:
    path = evidence_path.with_name(filename)
    if not path.is_file():
        findings.errors.append(f"{label} supporting evidence is missing: {path}")
        return None
    if not SHA256.fullmatch(str(digest or "")):
        findings.errors.append(f"{label} has no SHA-256 binding")
        return None
    if _sha256(path) != digest:
        findings.errors.append(
            f"{label} supporting evidence bytes do not match its SHA-256"
        )
        return None
    try:
        return path, _load_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        findings.errors.append(f"{label} supporting evidence is invalid JSON: {exc}")
        return None


def _load_integrity_bound_checkpoint(
    findings: Findings,
    evidence_path: Path,
    digest: Any,
) -> tuple[Path, dict] | None:
    """Load the checkpoint whose declared artifact is its canonical integrity hash."""
    path = evidence_path.with_name("checkpoint.json")
    if not path.is_file():
        findings.errors.append(f"AWS SDK checkpoint supporting evidence is missing: {path}")
        return None
    try:
        checkpoint = _load_json(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        findings.errors.append(f"AWS SDK checkpoint supporting evidence is invalid JSON: {exc}")
        return None
    integrity = checkpoint.get("integrity")
    declared = integrity.get("digest") if isinstance(integrity, dict) else None
    unsigned = dict(checkpoint)
    unsigned.pop("integrity", None)
    if (
        not SHA256.fullmatch(str(digest or ""))
        or not isinstance(integrity, dict)
        or integrity.get("algorithm") != "sha256"
        or declared != digest
        or _canonical_sha256(unsigned) != digest
    ):
        findings.errors.append(
            "AWS SDK checkpoint canonical bytes do not match its declared integrity digest"
        )
        return None
    return path, checkpoint


def _target_journey_entry(
    value: dict | tuple[Path, dict] | None,
) -> tuple[Path | None, dict | None]:
    if isinstance(value, tuple) and len(value) == 2:
        path, document = value
        return path, document
    if isinstance(value, dict):
        return None, value
    return None, None


def _validate_generic_aws_evidence_bundle(
    findings: Findings,
    manifest: dict,
    identity: dict,
    evidence_path: Path,
    evidence_document: dict,
    run_url: Any,
    aws_journey_receipt: dict | tuple[Path, dict] | None = None,
) -> dict[str, tuple[Path, dict]]:
    """Validate the final document plus every producer document it cites by digest."""
    components = manifest.get("components") or {}
    expected_arc_components = {
        name: (components.get(name) or {}).get("sha") for name in AWS_ARC_COMPONENTS
    }
    if evidence_document.get("components") != expected_arc_components:
        findings.errors.append(
            "generic AWS final evidence does not bind the exact delivery-arc components"
        )
    _validate_exact_passed_checks(
        findings,
        evidence_document.get("checks"),
        AWS_ARC_CHECKS,
        label="generic AWS final evidence",
    )

    artifacts = evidence_document.get("artifacts")
    if not isinstance(artifacts, dict):
        findings.errors.append("generic AWS final evidence artifacts must be an object")
        return {}
    if set(artifacts) != set(AWS_FINAL_ARTIFACTS):
        findings.errors.append(
            "generic AWS final evidence artifact inventory drift "
            f"(missing={sorted(set(AWS_FINAL_ARTIFACTS) - set(artifacts))}, "
            f"extra={sorted(set(artifacts) - set(AWS_FINAL_ARTIFACTS))})"
        )
    for artifact_name in AWS_FINAL_ARTIFACTS:
        if not SHA256.fullmatch(str(artifacts.get(artifact_name, ""))):
            findings.errors.append(
                f"generic AWS final evidence omits {artifact_name} digest"
            )
    if artifacts.get("platformManifest") != identity.get("manifestSha256"):
        findings.errors.append(
            "generic AWS final evidence platformManifest digest differs from the candidate"
        )

    artifact_records: dict[str, tuple[Path, dict]] = {}
    for artifact_name, filename in AWS_FINAL_ARTIFACT_FILES.items():
        record = _load_digest_bound_json(
            findings,
            evidence_path,
            filename,
            artifacts.get(artifact_name),
            label=f"AWS final artifact {artifact_name}",
        )
        if record is not None:
            artifact_records[artifact_name] = record
    checkpoint_record = _load_integrity_bound_checkpoint(
        findings, evidence_path, artifacts.get("sdkCheckpoint")
    )
    if checkpoint_record is not None:
        artifact_records["sdkCheckpoint"] = checkpoint_record

    journey_path, journey_document = _target_journey_entry(aws_journey_receipt)
    journey_record = artifact_records.get("sdkJourneyReceipt")
    if journey_document is not None and journey_record is not None:
        if journey_document != journey_record[1]:
            findings.errors.append(
                "AWS final artifact sdkJourneyReceipt differs from the supplied AWS SDK receipt"
            )
        if journey_path is not None and (
            not journey_path.is_file()
            or _sha256(journey_path) != artifacts.get("sdkJourneyReceipt")
        ):
            findings.errors.append(
                "AWS final artifact sdkJourneyReceipt does not bind the supplied AWS SDK receipt bytes"
            )

    binding_record = artifact_records.get("provisionBinding")
    teardown_record = artifact_records.get("teardownEvidence")

    expected_provision_components = {
        name: (components.get(name) or {}).get("sha")
        for name in ("honua-server", "honua-devops", "honua-iac")
    }
    server = components.get("honua-server") or {}
    expected_server_image = f"{server.get('image')}@{server.get('digest')}"
    if binding_record is not None:
        _, binding = binding_record
        if (
            binding.get("schemaVersion") != "honua.aws-ecs.provision-binding/v1"
            or binding.get("target") != "aws-ecs"
            or binding.get("status") != "ready"
            or binding.get("candidateId") != identity.get("candidateId")
            or binding.get("releaseId") != manifest.get("platformRelease")
        ):
            findings.errors.append(
                "AWS provision binding has the wrong schema/target/status/candidate/release"
            )
        if binding.get("components") != expected_provision_components:
            findings.errors.append(
                "AWS provision binding has the wrong component identities"
            )
        if binding.get("serverImage") != expected_server_image:
            findings.errors.append(
                "AWS provision binding does not install the manifest image@digest"
            )
        if not _is_public_https_url(binding.get("endpoint")):
            findings.errors.append("AWS provision binding endpoint is not public HTTPS")
        _validate_exact_passed_checks(
            findings,
            binding.get("checks"),
            AWS_PROVISION_CHECKS,
            label="AWS provision binding",
        )
        binding_evidence = binding.get("evidence") or {}
        if binding_evidence.get("url") != run_url or not _is_actions_run_url(
            binding_evidence.get("url")
        ):
            findings.errors.append(
                "AWS provision binding evidence URL differs from its governed Actions run"
            )
        provision_record = _load_digest_bound_json(
            findings,
            evidence_path,
            "provision-evidence.json",
            binding_evidence.get("sha256"),
            label="AWS provision evidence",
        )
        if provision_record is not None:
            _, provision = provision_record
            readiness = provision.get("readiness") or {}
            if (
                provision.get("schemaVersion")
                != "honua.release.aws-ecs-provision-evidence/v1"
                or provision.get("candidateId") != identity.get("candidateId")
                or provision.get("releaseId") != manifest.get("platformRelease")
                or provision.get("components") != expected_provision_components
                or provision.get("endpoint") != binding.get("endpoint")
                or provision.get("serverImage") != expected_server_image
                or provision.get("terraformApply") != "passed"
                or not SHA256.fullmatch(str(provision.get("terraformPlanSha256", "")))
                or not isinstance(readiness, dict)
                or readiness.get("status") != 200
                or not _is_public_https_url(readiness.get("url"))
                or not SHA256.fullmatch(str(provision.get("handoffSha256", "")))
            ):
                findings.errors.append(
                    "AWS provision evidence does not prove the exact candidate plan/apply/readiness/handoff"
                )

        handoff_record = artifact_records.get("secretlessHandoff")
        if handoff_record is not None:
            _, handoff = handoff_record
            handoff_env = handoff.get("env") or {}
            handoff_refs = handoff.get("secretRefs") or {}
            endpoint = binding.get("endpoint")
            if (
                handoff.get("schemaVersion") != "honua.mcp-proxy.handoff/v1"
                or not isinstance(handoff_env, dict)
                or not isinstance(handoff_refs, dict)
                or handoff_env.get("HONUA_BASE_URL") != endpoint
                or handoff_env.get("HONUA_MCP_REMOTE_URL")
                != f"{str(endpoint).rstrip('/')}/mcp"
                or "HONUA_ADMIN_KEY" in handoff_env
                or "HONUA_API_KEY" in handoff_env
                or handoff_refs.get("HONUA_ADMIN_KEY")
                != binding.get("adminKeySecretRef")
            ):
                findings.errors.append(
                    "AWS secretless handoff does not bind the exact provision endpoint/reference"
                )

        if checkpoint_record is not None:
            _, checkpoint = checkpoint_record
            if (
                checkpoint.get("schemaVersion")
                != "honua.zero-to-map.checkpoint/v1"
                or checkpoint.get("candidateId") != identity.get("candidateId")
                or checkpoint.get("releaseId") != manifest.get("platformRelease")
                or checkpoint.get("target") != "aws-ecs"
                or checkpoint.get("state") != "paused"
                or checkpoint.get("sourceRevision")
                != (components.get("honua-sdk-js") or {}).get("sha")
                or checkpoint.get("provisionReceiptSha256")
                != _sha256(binding_record[0])
            ):
                findings.errors.append(
                    "AWS SDK checkpoint does not bind the exact candidate/provision receipt"
                )

    embedded_teardown = evidence_document.get("teardown")
    if teardown_record is not None:
        _, teardown = teardown_record
        if teardown != embedded_teardown:
            findings.errors.append(
                "AWS teardown evidence bytes differ from the embedded final evidence"
            )
        expected_teardown_components = {
            name: (components.get(name) or {}).get("sha")
            for name in ("honua-devops", "honua-iac")
        }
        if (
            teardown.get("schemaVersion") != "honua.aws-ecs.teardown-evidence/v1"
            or teardown.get("target") != "aws-ecs"
            or teardown.get("status") != "passed"
            or teardown.get("candidateId") != identity.get("candidateId")
            or teardown.get("releaseId") != manifest.get("platformRelease")
            or teardown.get("components") != expected_teardown_components
        ):
            findings.errors.append(
                "AWS teardown evidence has the wrong schema/target/status/candidate/release/components"
            )
        _validate_exact_passed_checks(
            findings,
            teardown.get("checks"),
            AWS_TEARDOWN_CHECKS,
            label="AWS teardown evidence",
        )
        teardown_evidence = teardown.get("evidence") or {}
        if teardown_evidence.get("url") != run_url or not _is_actions_run_url(
            teardown_evidence.get("url")
        ):
            findings.errors.append(
                "AWS teardown evidence URL differs from its governed Actions run"
            )
        proof_record = _load_digest_bound_json(
            findings,
            evidence_path,
            "teardown-proof.json",
            teardown_evidence.get("sha256"),
            label="AWS teardown proof",
        )
        if proof_record is not None:
            _, proof = proof_record
            if (
                proof.get("schemaVersion") != "honua.release.aws-ecs-teardown-proof/v1"
                or proof.get("candidateId") != identity.get("candidateId")
                or proof.get("releaseId") != manifest.get("platformRelease")
                or proof.get("checks")
                != {"terraformDestroy": "passed", "cleanupVerified": "passed"}
            ):
                findings.errors.append(
                    "AWS teardown proof does not bind the exact candidate destroy/cleanup"
                )
    return artifact_records


def validate_external_receipts(
    manifest: dict,
    manifest_path: Path,
    contract: dict,
    receipts: dict[str, tuple[Path, dict]],
    evidence_documents: dict[str, tuple[Path, dict]] | None = None,
    target_journey_receipts: dict[
        str, dict | tuple[Path, dict]
    ] | None = None,
) -> tuple[Findings, list[dict]]:
    findings = Findings()
    records: list[dict] = []
    identity = candidate_identity(manifest, manifest_path)
    components = manifest.get("components") or {}
    evidence_documents = evidence_documents or {}
    target_journey_receipts = target_journey_receipts or {}
    # The AWS model runs before teardown, while the outer release receipt is
    # sealed after teardown. Its provisionReceiptSha256 therefore names the
    # exact provision-binding bytes the model consumed. The outer receipt's
    # content-addressed final evidence cites that same digest. Preserve that
    # chain plus the governed run so a model receipt cannot substitute an
    # unrelated 64-hex value from another run or endpoint.
    aws_provision_binding: tuple[Path, dict] | None = None
    aws_provision_run_url: str | None = None
    aws_final_artifacts: dict[str, tuple[Path, dict]] | None = None
    for expected in contract.get("externalReceipts") or []:
        receipt_id = expected.get("id")
        supplied = receipts.get(receipt_id)
        if supplied is None:
            findings.blockers.append(
                f"live journey is missing external receipt {receipt_id} ({expected.get('issue')})"
            )
            continue
        path, receipt = supplied
        model_contracts = {
            "certification/aws-ecs-real-model-ai-arc.schema.json": {
                "target": "aws-ecs",
                "id": "aws-ecs-real-model-ai-arc",
                "receiptVersion": "honua.aws-ecs.real-model-ai-arc/v1",
                "evidenceVersion": "honua.aws-ecs.real-model-ai-arc-evidence/v1",
                "promptVersion": "honua.aws-ecs.ai-arc.prompt/v1",
                "evalVersion": "honua.aws-ecs.ai-arc.eval/v1",
            },
            "certification/local-docker-real-model-ai-arc.schema.json": {
                "target": "local-docker",
                "id": "local-docker-real-model-ai-arc",
                "receiptVersion": "honua.local-docker.real-model-ai-arc/v1",
                "evidenceVersion": "honua.local-docker.real-model-ai-arc-evidence/v1",
                "promptVersion": "honua.local-docker.ai-arc.prompt/v1",
                "evalVersion": "honua.local-docker.ai-arc.eval/v1",
            },
        }
        model_contract = model_contracts.get(expected.get("receiptSchema"))
        if model_contract is not None:
            model_evidence = evidence_documents.get(receipt_id)
            if model_evidence is None:
                findings.blockers.append(
                    f"live journey is missing external evidence for {receipt_id} ({expected.get('evidenceSchema')})"
                )
                continue
            evidence_path, evidence_document = model_evidence
            _, target_journey_receipt = _target_journey_entry(
                target_journey_receipts.get(model_contract["target"])
            )
            if target_journey_receipt is None:
                findings.blockers.append(
                    f"live journey is missing the {model_contract['target']} SDK action receipt"
                )
                continue
            _validate_real_model_receipt(
                findings,
                manifest,
                identity,
                expected,
                model_contract,
                path,
                receipt,
                evidence_path,
                evidence_document,
                target_journey_receipt,
                aws_provision_binding=aws_provision_binding,
                aws_provision_run_url=aws_provision_run_url,
                aws_final_artifacts=aws_final_artifacts,
            )
            records.append(
                {
                    "id": receipt_id,
                    "path": str(path),
                    "receiptSha256": _sha256(path),
                    "evidencePath": str(evidence_path),
                    "evidenceSha256": _sha256(evidence_path),
                    "source": receipt.get("source") or {},
                    "components": receipt.get("components") or {},
                    "evidence": receipt.get("evidence") or {},
                    "claims": {
                        "target": receipt.get("target"),
                        "checks": receipt.get("checks") or {},
                    },
                }
            )
            continue
        generic_evidence = evidence_documents.get(receipt_id)
        if generic_evidence is None:
            findings.blockers.append(
                f"live journey is missing external evidence for {receipt_id}"
            )
            continue
        evidence_path, evidence_document = generic_evidence
        if receipt.get("schemaVersion") != "honua.release.evidence-receipt/v1":
            findings.errors.append(
                f"external receipt {receipt_id} has an unsupported schemaVersion"
            )
        if receipt.get("id") != receipt_id or receipt.get("status") != "passed":
            findings.errors.append(
                f"external receipt {receipt_id} must identify itself and have status=passed"
            )
        if receipt.get("candidateId") != identity["candidateId"]:
            findings.errors.append(
                f"external receipt {receipt_id} is not bound to the exact manifest digest"
            )
        if receipt.get("releaseId") != manifest.get("platformRelease"):
            findings.errors.append(
                f"external receipt {receipt_id} has the wrong platform release id"
            )
        source = receipt.get("source") or {}
        component_name = expected.get("sourceComponent")
        component = components.get(component_name) or {}
        if source.get("repository") != expected.get("sourceRepository") or source.get(
            "sha"
        ) != component.get("sha"):
            findings.errors.append(
                f"external receipt {receipt_id} is not from the manifest-pinned {component_name}"
            )
        receipt_components = receipt.get("components") or {}
        expected_receipt_components = {
            bound_name: (components.get(bound_name) or {}).get("sha")
            for bound_name in expected.get("boundComponents") or []
        }
        if receipt_components != expected_receipt_components:
                findings.errors.append(
                f"external receipt {receipt_id} does not bind its exact manifest components"
                )
        evidence = receipt.get("evidence") or {}
        if not _is_actions_run_url(evidence.get("url")):
            findings.errors.append(
                f"external receipt {receipt_id} evidence URL is not an immutable Actions run"
            )
        if not SHA256.fullmatch(str(evidence.get("sha256", ""))):
            findings.errors.append(
                f"external receipt {receipt_id} has no evidence SHA-256"
            )
        elif _sha256(evidence_path) != evidence.get("sha256"):
            findings.errors.append(
                f"external receipt {receipt_id} evidence bytes do not match its SHA-256"
            )
        expected_evidence_identity = {
            "schemaVersion": "honua.aws-ecs.ai-delivery-arc-evidence/v1",
            "status": "passed",
            "target": "aws-ecs",
            "candidateId": identity["candidateId"],
            "releaseId": manifest.get("platformRelease"),
            "source": source,
        }
        for key, expected_value in expected_evidence_identity.items():
            if evidence_document.get(key) != expected_value:
                findings.errors.append(
                    f"external receipt {receipt_id} evidence disagrees on {key}"
                )
        artifact_records = _validate_generic_aws_evidence_bundle(
            findings,
            manifest,
            identity,
            evidence_path,
            evidence_document,
            evidence.get("url"),
            target_journey_receipts.get("aws-ecs"),
        )
        if receipt_id == "aws-ecs-provision":
            aws_final_artifacts = artifact_records
            aws_provision_binding = artifact_records.get("provisionBinding")
            run_url = evidence.get("url")
            if isinstance(run_url, str):
                aws_provision_run_url = run_url
        expected_claims = expected.get("claims") or {}
        claims = receipt.get("claims") or {}
        for key in ("target", "journeyId", "releaseContract"):
            if key in expected_claims and claims.get(key) != expected_claims.get(key):
                findings.errors.append(
                    f"external receipt {receipt_id} claim {key}={claims.get(key)!r}; "
                    f"expected {expected_claims.get(key)!r}"
                )
        checks = claims.get("checks")
        _validate_exact_passed_checks(
            findings,
            checks,
            expected_claims.get("requiredChecks") or [],
            label=f"external receipt {receipt_id}",
                )
        records.append(
            {
                "id": receipt_id,
                "path": str(path),
                "receiptSha256": _sha256(path),
                "evidencePath": str(evidence_path),
                "evidenceSha256": _sha256(evidence_path),
                "source": source,
                "components": receipt_components,
                "evidence": evidence,
                "claims": claims,
            }
        )
    return findings, records


def _validate_real_model_receipt(
    findings: Findings,
    manifest: dict,
    identity: dict,
    expected: dict,
    model_contract: dict[str, str],
    receipt_path: Path,
    receipt: dict,
    evidence_path: Path,
    evidence_document: dict,
    journey_receipt: dict | None,
    *,
    aws_provision_binding: tuple[Path, dict] | None = None,
    aws_provision_run_url: str | None = None,
    aws_final_artifacts: dict[str, tuple[Path, dict]] | None = None,
) -> None:
    target = model_contract["target"]
    receipt_fields = {
        "schemaVersion", "id", "status", "target", "candidateId", "releaseId",
        "endpointSha256", "source", "components", "model", "promptVersion",
        "evalVersion", "transcriptSha256", "deterministic", "lanes", "joins",
        "checks", "evidence",
    }
    if set(receipt) != receipt_fields:
        findings.errors.append(f"{target} real-model receipt has unexpected or missing fields")
    if (
        receipt.get("schemaVersion") != model_contract["receiptVersion"]
        or receipt.get("id") != model_contract["id"]
        or receipt.get("status") != "passed"
        or receipt.get("target") != target
    ):
        findings.errors.append(f"{target} real-model receipt has the wrong schema/id/status/target")
    if receipt.get("candidateId") != identity.get("candidateId") or receipt.get("releaseId") != manifest.get("platformRelease"):
        findings.errors.append(f"{target} real-model receipt is not bound to the exact candidate/release")
    if not SHA256.fullmatch(str(receipt.get("endpointSha256", ""))):
        findings.errors.append(f"{target} real-model receipt has no endpoint SHA-256")
    source = receipt.get("source") or {}
    studio_sha = ((manifest.get("components") or {}).get("honua-studio") or {}).get("sha")
    if source != {"repository": "honua-io/honua-studio", "sha": studio_sha}:
        findings.errors.append(f"{target} real-model receipt is not from the pinned Studio runner")
    expected_components = {
        name: (manifest.get("components") or {}).get(name, {}).get("sha")
        for name in expected.get("boundComponents") or []
    }
    if receipt.get("components") != expected_components:
        findings.errors.append(f"{target} real-model receipt does not bind all manifest component SHAs")
    model = receipt.get("model") or {}
    if model.get("provider") not in {"anthropic", "bedrock", "openai"} or not model.get("modelId"):
        findings.errors.append(f"{target} real-model receipt has no live provider/model identity")
    if receipt.get("promptVersion") != model_contract["promptVersion"] or receipt.get("evalVersion") != model_contract["evalVersion"]:
        findings.errors.append(f"{target} real-model receipt has the wrong prompt/eval version")
    if not SHA256.fullmatch(str(receipt.get("transcriptSha256", ""))):
        findings.errors.append(f"{target} real-model receipt has no transcript SHA-256")
    deterministic = receipt.get("deterministic") or {}
    expected_deterministic_fields = {
        "target", "checkpointDigest", "consoleAggregateSha256", "consoleEvidenceSha256"
    }
    if target == "aws-ecs":
        expected_deterministic_fields.add("provisionReceiptSha256")
    if (
        set(deterministic) != expected_deterministic_fields
        or deterministic.get("target") != target
        or not SHA256.fullmatch(str(deterministic.get("checkpointDigest", "")))
        or not SHA256.fullmatch(str(deterministic.get("consoleAggregateSha256", "")))
        or not SHA256.fullmatch(str(deterministic.get("consoleEvidenceSha256", "")))
        or (
            target == "aws-ecs"
            and not SHA256.fullmatch(str(deterministic.get("provisionReceiptSha256", "")))
        )
    ):
        findings.errors.append(f"{target} real-model receipt has no exact deterministic checkpoint join")
    if target == "aws-ecs":
        if aws_provision_binding is None or aws_provision_run_url is None:
            findings.errors.append(
                "aws-ecs real-model receipt has no validated provision receipt context"
            )
        else:
            provision_path, provision = aws_provision_binding
            if deterministic.get("provisionReceiptSha256") != _sha256(provision_path):
                findings.errors.append(
                    "aws-ecs real-model provisionReceiptSha256 does not bind the exact "
                    "provision-binding bytes"
                )
            endpoint = provision.get("endpoint")
            expected_endpoint_sha256 = (
                hashlib.sha256(endpoint.rstrip("/").encode("utf-8")).hexdigest()
                if isinstance(endpoint, str) and endpoint
                else None
            )
            if receipt.get("endpointSha256") != expected_endpoint_sha256:
                findings.errors.append(
                    "aws-ecs real-model endpointSha256 does not bind the provisioned endpoint"
                )
        if aws_final_artifacts is None:
            findings.errors.append(
                "aws-ecs real-model receipt has no validated final artifact context"
            )
        else:
            model_receipt_record = aws_final_artifacts.get(
                "awsEcsRealModelReceipt"
            )
            model_evidence_record = aws_final_artifacts.get(
                "awsEcsRealModelEvidence"
            )
            if (
                model_receipt_record is None
                or model_receipt_record[1] != receipt
                or not receipt_path.is_file()
                or _sha256(receipt_path) != _sha256(model_receipt_record[0])
            ):
                findings.errors.append(
                    "aws-ecs real-model receipt differs from the digest-listed final artifact"
                )
            if (
                model_evidence_record is None
                or model_evidence_record[1] != evidence_document
                or not evidence_path.is_file()
                or _sha256(evidence_path) != _sha256(model_evidence_record[0])
            ):
                findings.errors.append(
                    "aws-ecs real-model evidence differs from the digest-listed final artifact"
                )

            checkpoint_record = aws_final_artifacts.get("sdkCheckpoint")
            console_record = aws_final_artifacts.get("consoleReceipt")
            sdk_console_record = aws_final_artifacts.get("sdkConsoleReceipt")
            checkpoint_digest = (
                ((checkpoint_record[1].get("integrity") or {}).get("digest"))
                if checkpoint_record is not None
                else None
            )
            console_digest = (
                _sha256(console_record[0]) if console_record is not None else None
            )
            sdk_console_digest = (
                _sha256(sdk_console_record[0])
                if sdk_console_record is not None
                else None
            )
            if deterministic.get("checkpointDigest") != checkpoint_digest:
                findings.errors.append(
                    "aws-ecs real-model checkpointDigest differs from the digest-listed checkpoint"
                )
            if deterministic.get("consoleAggregateSha256") != console_digest:
                findings.errors.append(
                    "aws-ecs real-model Console aggregate differs from the digest-listed receipt"
                )
            if (
                console_record is None
                or sdk_console_record is None
                or console_record[1] != sdk_console_record[1]
                or console_digest != sdk_console_digest
            ):
                findings.errors.append(
                    "AWS aggregate and SDK Console receipts are not the same content-addressed approval"
                )
            console_action = _receipt_action_map(journey_receipt or {}).get(
                "console-approval"
            ) or {}
            console_action_evidence = console_action.get("evidence") or {}
            if console_action_evidence.get("sha256") != sdk_console_digest:
                findings.errors.append(
                    "AWS SDK Console action does not bind the digest-listed Console receipt"
                )
            console_evidence_record = _load_digest_bound_json(
                findings,
                console_record[0] if console_record is not None else evidence_path,
                "console-evidence.json",
                deterministic.get("consoleEvidenceSha256"),
                label="AWS Console browser evidence",
            )
            if console_evidence_record is not None:
                console_evidence = console_evidence_record[1]
                console_candidate = console_evidence.get("candidate") or {}
                if (
                    console_candidate.get("candidateId")
                    != identity.get("candidateId")
                    or console_candidate.get("releaseId")
                    != manifest.get("platformRelease")
                ):
                    findings.errors.append(
                        "AWS Console browser evidence does not bind the exact candidate/release"
                    )
    checks = receipt.get("checks") or {}
    for check in (expected.get("claims") or {}).get("requiredChecks") or []:
        if checks.get(check) != "passed":
            findings.errors.append(f"{target} real-model receipt does not prove {check}=passed")
    lanes = receipt.get("lanes") or {}
    required_lanes = set(AWS_MODEL_LANES)
    if set(lanes) != required_lanes:
        findings.errors.append(f"{target} real-model receipt does not cover the four required natural-language lanes")
    observed_identity_keys: dict[str, set[str]] = {
        name: set() for name in required_lanes
    }
    observed_action_ids: set[str] = set()
    journey_actions = _receipt_action_map(journey_receipt or {})
    joins = receipt.get("joins") or {}
    expected_by_lane: dict[str, list[tuple[str, str, str | None, str, str]]] = {
        lane: [] for lane in AWS_MODEL_LANES
    }
    for action_id, lane, role, family, kind, name in MODEL_ACTION_SPECS:
        expected_by_lane[lane].append(
            (
                action_id,
                role,
                family,
                kind,
                name.format(
                    esriMcpJobId=joins.get("esriMcpJobId", ""),
                    directAnalysisJobId=joins.get("directAnalysisJobId", ""),
                ),
            )
        )
    for lane_name, lane in (lanes.items() if isinstance(lanes, dict) else []):
        if not isinstance(lane, dict) or not SHA256.fullmatch(str(lane.get("promptSha256", ""))) or not SHA256.fullmatch(str(lane.get("transcriptSha256", ""))):
            findings.errors.append(f"{target} real-model lane {lane_name} lacks prompt/transcript hashes")
            continue
        calls = lane.get("calls") or []
        if not isinstance(calls, list) or not calls:
            findings.errors.append(f"{target} real-model lane {lane_name} has no calls")
            continue
        expected_calls = expected_by_lane.get(lane_name, [])
        if len(calls) != len(expected_calls):
            findings.errors.append(
                f"{target} real-model lane {lane_name} does not have the canonical action multiplicity"
            )
        for index, call in enumerate(calls):
            if not isinstance(call, dict) or call.get("status") != "passed" or not SHA256.fullmatch(str(call.get("responseSha256", ""))):
                findings.errors.append(f"{target} real-model lane {lane_name} contains an unproved call")
                continue
            expected_call_fields = {
                "actionId", "actionReceiptSha256", "role", "kind", "name",
                "status", "responseSha256", "result",
            }
            if call.get("family") is not None:
                expected_call_fields.add("family")
            if set(call) != expected_call_fields:
                findings.errors.append(
                    f"{target} real-model lane {lane_name} call has unexpected or missing fields"
                )
            role = call.get("role")
            action_id = call.get("actionId")
            family = call.get("family")
            kind = call.get("kind")
            name = call.get("name")
            if (
                not isinstance(role, str)
                or not isinstance(action_id, str)
                or not action_id
                or family not in {None, "map", "app", "dashboard", "parcels", "zoning"}
                or kind not in {"mcp", "mcp-resource", "gpserver"}
                or not isinstance(name, str)
                or not name
            ):
                findings.errors.append(f"{target} real-model lane {lane_name} has a malformed call identity")
                continue
            if index >= len(expected_calls):
                findings.errors.append(
                    f"{target} real-model lane {lane_name} has an extra non-canonical action {action_id}"
                )
            elif (action_id, role, family, kind, name) != expected_calls[index]:
                findings.errors.append(
                    f"{target} real-model lane {lane_name} call {index} is not canonical action "
                    f"{expected_calls[index][0]}"
                )
            if action_id in observed_action_ids:
                findings.errors.append(f"{target} real-model evidence duplicates SDK action {action_id}")
            observed_action_ids.add(action_id)
            action_receipt_digest = call.get("actionReceiptSha256")
            if not SHA256.fullmatch(str(action_receipt_digest or "")):
                findings.errors.append(
                    f"{target} real-model action {action_id} has no SDK action-receipt digest"
                )
            elif journey_receipt is not None:
                journey_action = journey_actions.get(action_id)
                if journey_action is None or action_receipt_digest != _canonical_sha256(journey_action):
                    findings.errors.append(
                        f"{target} real-model action {action_id} is not bound to the SDK journey receipt"
                    )
            result = call.get("result") or {}
            identities = result.get("identities") or {}
            if result.get("status") != "reconciled" or not isinstance(identities, dict) or not identities:
                findings.errors.append(f"{target} real-model lane {lane_name} call {name} has no successful joined result")
                continue
            for key, value in identities.items():
                if key not in joins or joins.get(key) != value:
                    findings.errors.append(f"{target} real-model call {name} fabricates or disagrees on identity {key}")
                else:
                    observed_identity_keys.setdefault(lane_name, set()).add(key)
            required_call_identities = {
                "buffer-esri-gpserver": {"gpServerJobId"},
            }.get(action_id, set())
            missing_call_identities = required_call_identities - set(identities)
            if missing_call_identities:
                findings.errors.append(
                    f"{target} real-model action {action_id} omits result joins "
                    f"{sorted(missing_call_identities)}"
                )
    required_lane_joins = {
        "admin": {"connectionId", "parcelsLayerId", "zoningLayerId", "serviceName"},
        "esriGp": {"esriMcpJobId", "esriMcpResultPackageId", "esriMcpArtifactId"},
        "nativeAnalysis": {"gpServerJobId", "directAnalysisJobId", "bufferArtifactId"},
        "studioPublication": {
            *{
                f"{family}{suffix}"
                for family in ("map", "app", "dashboard")
                for suffix in (
                    "ItemId", "VersionId", "ReopenedDraftId", "PublicationVersionId",
                    "ProposalId",
                )
            },
        },
    }
    for lane_name, required in required_lane_joins.items():
        missing = required - observed_identity_keys.get(lane_name, set())
        if missing:
            findings.errors.append(
                f"{target} real-model lane {lane_name} result evidence omits joins {sorted(missing)}"
            )
    required_joins = set(MODEL_JOIN_NAMES)
    if not isinstance(joins, dict) or set(joins) != required_joins:
        findings.errors.append(
            f"{target} real-model receipt has non-canonical deterministic joins "
            f"(missing={sorted(required_joins - set(joins or {}))}, "
            f"extra={sorted(set(joins or {}) - required_joins)})"
        )
    elif joins.get("candidateId") != receipt.get("candidateId") or joins.get("releaseId") != receipt.get("releaseId"):
        findings.errors.append(f"{target} real-model joins do not bind the exact candidate/release")
    for family in ("map", "app", "dashboard"):
        if joins.get(f"{family}PublicationStatus") != "published":
            findings.errors.append(
                f"{target} real-model join {family}PublicationStatus is not published"
            )
        if not _is_public_https_url(joins.get(f"{family}PublicUrl")):
            findings.errors.append(f"{target} real-model join {family}PublicUrl is not public HTTPS")
    if journey_receipt is not None:
        deterministic_captures = {
            key: value
            for action in _receipt_action_map(journey_receipt).values()
            for key, value in (action.get("captures") or {}).items()
        }
        for key in required_joins & set(deterministic_captures):
            if joins.get(key) != deterministic_captures[key]:
                findings.errors.append(
                    f"{target} real-model join {key} differs from deterministic journey evidence"
                )
    evidence = receipt.get("evidence") or {}
    if not _is_public_https_url(evidence.get("url")) or not SHA256.fullmatch(str(evidence.get("sha256", ""))):
        findings.errors.append(f"{target} real-model receipt has no public content-addressed evidence")
    elif _sha256(evidence_path) != evidence.get("sha256"):
        findings.errors.append(f"{target} real-model evidence bytes do not match its receipt SHA-256")
    if target == "aws-ecs" and (
        evidence.get("url") != aws_provision_run_url
        or not _is_actions_run_url(evidence.get("url"))
    ):
        findings.errors.append(
            "aws-ecs real-model evidence URL differs from the exact provision Actions run"
        )
    evidence_bindings = {
        "schemaVersion": model_contract["evidenceVersion"],
        "candidateId": receipt.get("candidateId"), "releaseId": receipt.get("releaseId"),
        "endpointSha256": receipt.get("endpointSha256"), "source": source, "model": model,
        "promptVersion": receipt.get("promptVersion"), "evalVersion": receipt.get("evalVersion"),
        "transcriptSha256": receipt.get("transcriptSha256"), "target": target,
        "checkpointDigest": deterministic.get("checkpointDigest"),
        "consoleAggregateSha256": deterministic.get("consoleAggregateSha256"),
        "consoleEvidenceSha256": deterministic.get("consoleEvidenceSha256"),
        "lanes": lanes, "joins": joins,
    }
    if target == "aws-ecs":
        evidence_bindings["provisionReceiptSha256"] = deterministic.get("provisionReceiptSha256")
    if evidence_document != evidence_bindings:
        findings.errors.append(f"{target} real-model evidence document is not an exact receipt binding")
    if _contains_forbidden_evidence(
        {"receipt": receipt, "evidence": evidence_document},
        (
            "password", "authorization", "credential", "api_key", "apikey",
            "accesskey", "secret", "token", "bearer", "secretstring", "fixture",
        ),
    ):
        findings.errors.append(f"{target} real-model evidence contains forbidden secret/fixture material")


def validate_receipt(
    manifest: dict,
    manifest_path: Path,
    contract: dict,
    plan: dict,
    receipt: dict,
    *,
    expected_mode: str,
    expected_target: str | None,
) -> tuple[Findings, dict]:
    findings = Findings()
    if receipt.get("schemaVersion") != "honua.zero-to-map.receipt/v1":
        findings.errors.append("SDK journey receipt has an unsupported schemaVersion")
    for key in ("journeyId", "releaseContract"):
        if receipt.get(key) != plan.get(key):
            findings.errors.append(f"SDK journey receipt {key} does not match its plan")
    if receipt.get("mode") != expected_mode:
        findings.errors.append(
            f"SDK journey receipt mode={receipt.get('mode')!r}; expected {expected_mode!r}"
        )

    plan_inventory = _ordered_stage_action_inventory(
        plan, label="SDK journey plan", findings=findings
    )
    receipt_inventory = _ordered_stage_action_inventory(
        receipt, label="SDK journey receipt", findings=findings
    )
    if plan_inventory is None or receipt_inventory is None:
        return findings, {"failureAttribution": None}
    if receipt_inventory != plan_inventory:
        findings.errors.append(
            "SDK journey receipt ordered stage/action inventory differs from its plan"
        )

    plan_actions = _action_map(plan)
    receipt_actions = _receipt_action_map(receipt)
    if set(plan_actions) != set(receipt_actions):
        missing = sorted(set(plan_actions) - set(receipt_actions))
        extra = sorted(set(receipt_actions) - set(plan_actions))
        findings.errors.append(
            f"SDK journey receipt action inventory drift (missing={missing}, extra={extra})"
        )

    first_non_pass: dict | None = None
    for stage in receipt.get("stages") or []:
        for action in stage.get("actions") or []:
            if action.get("status") != "passed" and first_non_pass is None:
                first_non_pass = {
                    "stage": stage.get("number"),
                    "stageId": stage.get("id"),
                    "actionId": action.get("id"),
                    "tool": plan_actions.get(action.get("id"), {}).get("tool"),
                    "status": action.get("status"),
                    "code": action.get("code"),
                    "message": action.get("message"),
                }

    if expected_mode == "contract":
        if receipt.get("status") != "blocked":
            findings.errors.append(
                "contract-mode SDK receipt must be explicitly blocked"
            )
        first = (receipt.get("stages") or [{}])[0].get("actions") or [{}]
        if first[0].get("code") != "live-execution-disabled":
            findings.errors.append(
                "contract-mode receipt did not attribute the block to disabled live execution"
            )
        findings.blockers.append(
            "contract mode validated the deterministic plan but did not execute the live candidate"
        )
    else:
        if expected_target not in {"local-docker", "aws-ecs"}:
            findings.errors.append(
                "live SDK journey validation requires an explicit execution target"
            )
        if receipt.get("status") != "passed":
            detail = first_non_pass or {"actionId": "unknown"}
            tool = f" / {detail.get('tool')}" if detail.get("tool") else ""
            findings.blockers.append(
                "live journey did not pass at "
                f"stage {detail.get('stage')} ({detail.get('stageId')}) / {detail.get('actionId')}"
                f"{tool}: "
                f"{detail.get('code') or detail.get('status')} - {detail.get('message') or 'no detail'}"
            )
        not_passed = [
            action_id
            for action_id, action in receipt_actions.items()
            if action.get("status") != "passed"
        ]
        if not_passed:
            findings.blockers.append(
                f"live journey has non-passing actions: {not_passed}"
            )

        # A row of action IDs marked "passed" is not execution evidence. Require
        # the manifest-pinned SDK receipt to retain safe evidence for every live
        # call and every identity that the plan says it captures.
        identity = candidate_identity(manifest, manifest_path)
        manifest_components = manifest.get("components") or {}
        for action_id, plan_action in plan_actions.items():
            action = receipt_actions.get(action_id) or {}
            if action.get("status") != "passed":
                continue
            evidence = action.get("evidence")
            if not isinstance(evidence, dict) or not evidence:
                findings.errors.append(
                    f"live action {action_id} is passed without execution evidence"
                )
                continue
            kind = plan_action.get("kind")
            if kind == "cli":
                if action_id in {"install-local", "install-status"}:
                    if (
                        expected_target == "aws-ecs"
                        and evidence.get("target") != "aws-ecs"
                    ):
                        findings.errors.append(
                            f"live AWS ECS receipt action {action_id} is not target-bound to aws-ecs"
                        )
                    elif expected_target == "local-docker" and "target" in evidence:
                        findings.errors.append(
                            f"live local Docker receipt action {action_id} contains foreign target evidence"
                        )
                if expected_target == "aws-ecs" and action_id == "install-local":
                    server = manifest_components.get("honua-server") or {}
                    expected_image = f"{server.get('image')}@{server.get('digest')}"
                    expected_components = {
                        name: (manifest_components.get(name) or {}).get("sha")
                        for name in ("honua-server", "honua-devops", "honua-iac")
                    }
                    if (
                        evidence.get("candidateId") != identity["candidateId"]
                        or evidence.get("releaseId") != manifest.get("platformRelease")
                        or evidence.get("components") != expected_components
                        or evidence.get("terraformPlan") != "passed"
                        or evidence.get("terraformApply") != "passed"
                        or evidence.get("serverImage") != expected_image
                        or not _is_actions_run_url(evidence.get("producerEvidenceUrl"))
                        or not SHA256.fullmatch(
                            str(evidence.get("producerEvidenceSha256", ""))
                        )
                    ):
                        findings.errors.append(
                            "live AWS ECS install action has no exact candidate/image/Terraform provision evidence"
                        )
                elif expected_target == "aws-ecs" and action_id == "install-status":
                    if (
                        evidence.get("readiness") != "passed"
                        or evidence.get("adminMcpHandoff") != "passed"
                        or evidence.get("credentialReferencePresent") is not True
                        or not _is_public_https_url(evidence.get("endpoint"))
                        or evidence.get("mcpUrl")
                        != f"{str(evidence.get('endpoint', '')).rstrip('/')}/mcp"
                    ):
                        findings.errors.append(
                            "live AWS ECS status action has no readiness/admin MCP handoff evidence"
                        )
                elif expected_target == "local-docker" and (
                    evidence.get("command") != "honua" or evidence.get("exitCode") != 0
                ):
                    findings.errors.append(
                        f"live local Docker CLI action {action_id} has no successful Honua CLI evidence"
                    )
                elif (
                    expected_target not in {"local-docker", "aws-ecs"}
                    and evidence.get("exitCode") != 0
                ):
                    findings.errors.append(
                        f"live CLI action {action_id} has no successful exit-code evidence"
                    )
            elif kind == "mcp" and (
                evidence.get("tool") != plan_action.get("tool")
                or evidence.get("isError") is not False
            ):
                findings.errors.append(
                    f"live MCP action {action_id} has mismatched tool/error evidence"
                )
            elif kind == "mcp-resource" and not str(evidence.get("uri", "")).startswith(
                "honua://"
            ):
                findings.errors.append(
                    f"live MCP resource action {action_id} has no Honua resource URI evidence"
                )
            elif kind == "gpserver":
                if evidence.get("protocol") != "geoservices-gp":
                    findings.errors.append(
                        f"live GPServer action {action_id} has no GeoServices GP evidence"
                    )
                if str(evidence.get("status", "")).lower() not in {
                    "success",
                    "successful",
                    "succeeded",
                }:
                    findings.errors.append(
                        f"live GPServer action {action_id} has no successful terminal evidence"
                    )
            elif kind == "receipt" and (
                evidence.get("source") != "external-receipt"
                or not SHA256.fullmatch(str(evidence.get("sha256", "")))
            ):
                findings.errors.append(
                    f"live receipt action {action_id} has no content-addressed evidence"
                )
            elif kind == "http":
                if (
                    action_id in RELEASE_PUBLIC_HTTP_ACTIONS
                    and evidence.get("status") != 200
                ):
                    findings.errors.append(
                        f"live release HTTP action {action_id} did not return mandatory HTTP 200"
                    )
                if (
                    evidence.get("status") != plan_action.get("expectedStatus")
                    or not _is_public_https_url(evidence.get("url"))
                    or evidence.get("identityMatched") is not True
                ):
                    findings.errors.append(
                        f"live HTTP action {action_id} has no identity-matched public HTTPS URL/status evidence"
                    )

            captures = action.get("captures") or {}
            for variable in _capture_names(plan_action):
                if captures.get(variable) in (None, ""):
                    findings.errors.append(
                        f"live action {action_id} omits planned capture {variable}"
                    )

        console = receipt_actions.get("console-approval") or {}
        captures = console.get("captures") or {}
        if captures.get("candidateId") != identity["candidateId"]:
            findings.errors.append(
                "Console receipt candidateId is not the exact platform-manifest digest"
            )
        if captures.get("releaseId") != manifest.get("platformRelease"):
            findings.errors.append(
                "Console receipt releaseId is not the manifest platformRelease"
            )
        if not _is_public_https_url(captures.get("shareUrl")):
            findings.errors.append("Console receipt shareUrl is not a public HTTPS URL")
        final_urls = {
            "verify-map-public-url": captures.get("mapPublicUrl"),
            "verify-share-url": captures.get("shareUrl"),
            "verify-dashboard-public-url": captures.get("dashboardPublicUrl"),
        }
        for action_id, public_url in final_urls.items():
            if not _is_public_https_url(public_url):
                findings.errors.append(
                    f"Console receipt {action_id} URL is not public HTTPS"
                )
            action_evidence = (receipt_actions.get(action_id) or {}).get(
                "evidence"
            ) or {}
            if action_evidence.get("url") != public_url:
                findings.errors.append(
                    f"live HTTP action {action_id} did not probe its exact Console URL"
                )
        evidence = console.get("evidence") or {}
        if evidence.get("source") != "external-receipt" or not SHA256.fullmatch(
            str(evidence.get("sha256", ""))
        ):
            findings.errors.append(
                "Console checkpoint has no content-addressed external receipt evidence"
            )

    return findings, {"failureAttribution": first_non_pass}


def build_report(
    manifest: dict,
    manifest_path: Path,
    contract: dict,
    contract_findings: Findings,
    *,
    receipt: dict | None = None,
    receipt_findings: Findings | None = None,
    receipt_detail: dict | None = None,
    sdk_head: str | None = None,
    external_findings: Findings | None = None,
    external_receipts: list[dict] | None = None,
) -> dict:
    errors = [
        *contract_findings.errors,
        *((receipt_findings or Findings()).errors),
        *((external_findings or Findings()).errors),
    ]
    blockers = [
        *contract_findings.blockers,
        *((receipt_findings or Findings()).blockers),
        *((external_findings or Findings()).blockers),
    ]
    status = "fail" if errors else "blocked" if blockers else "pass"
    return {
        "schemaVersion": "honua.ai-delivery-arc-release-receipt/v1",
        "releaseContract": contract.get("releaseContract"),
        "status": status,
        "candidate": candidate_identity(manifest, manifest_path),
        "sdkJourneySource": {
            "repository": "honua-io/honua-sdk-js",
            "sha": sdk_head,
        },
        "inventory": contract.get("inventory"),
        "executionTargets": contract.get("executionTargets") or [],
        "errors": errors,
        "blockers": blockers,
        "journey": receipt,
        **(receipt_detail or {}),
        "externalReceiptRequirements": contract.get("externalReceipts") or [],
        "externalReceipts": external_receipts or [],
    }


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=REPO_ROOT / "platform-manifest.yaml")
    parser.add_argument("--contract", type=Path, default=REPO_ROOT / "certification" / "ai-delivery-arc.yaml")
    parser.add_argument("--sdk-root", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--sdk-receipt", type=Path)
    parser.add_argument(
        "--target-sdk-receipt",
        action="append",
        default=[],
        metavar="TARGET=PATH",
        help="live target-specific SDK journey receipt; repeat for local-docker and aws-ecs",
    )
    parser.add_argument(
        "--external-receipt",
        action="append",
        default=[],
        metavar="ID=PATH",
        help="candidate-bound external receipt; repeat for AWS and real-model Studio evidence",
    )
    parser.add_argument(
        "--external-evidence",
        action="append",
        default=[],
        metavar="ID=PATH",
        help="content-addressed evidence document for a dedicated external receipt",
    )
    parser.add_argument("--mode", choices=("contract", "live"), default="contract")
    parser.add_argument("--json-out", type=Path, required=True)
    parser.add_argument("--require-real", action="store_true")
    args = parser.parse_args(argv)

    try:
        manifest = _load_yaml(args.manifest)
        contract = _load_yaml(args.contract)
        sdk_head = _git_head(args.sdk_root)
        plan_path = args.plan or args.sdk_root / "mcp" / "release" / "zero-to-map" / "journey.v1.json"
        plan = _load_json(plan_path) if plan_path.is_file() else None
        source_path = args.sdk_root / "config" / "admin-client.v1.json"
        sdk_admin_source = _load_json(source_path) if source_path.is_file() else None
        contract_findings = validate_contract(
            manifest,
            contract,
            plan,
            sdk_head=sdk_head,
            sdk_admin_source=sdk_admin_source,
        )
        receipt = _load_json(args.sdk_receipt) if args.sdk_receipt and args.sdk_receipt.is_file() else None
        receipt_findings = Findings()
        receipt_detail: dict = {}
        external_findings = Findings()
        external_records: list[dict] = []
        if args.sdk_receipt and receipt is None:
            receipt_findings.blockers.append(f"SDK journey receipt is missing: {args.sdk_receipt}")
        elif receipt is not None and plan is not None:
            receipt_findings, receipt_detail = validate_receipt(
                manifest,
                args.manifest,
                contract,
                plan,
                receipt,
                expected_mode=args.mode,
                expected_target="local-docker" if args.mode == "live" else None,
            )
            if args.mode == "live":
                target_journey_receipts: dict[
                    str, tuple[Path, dict]
                ] = {}
                if receipt is not None:
                    assert args.sdk_receipt is not None
                    target_journey_receipts["local-docker"] = (
                        args.sdk_receipt,
                        receipt,
                    )
                for entry in args.target_sdk_receipt:
                    if "=" not in entry:
                        raise ValueError("--target-sdk-receipt requires TARGET=PATH")
                    target, raw_path = entry.split("=", 1)
                    if target not in {"local-docker", "aws-ecs"}:
                        raise ValueError(f"unsupported target SDK receipt: {target}")
                    target_receipt = _load_json(Path(raw_path))
                    target_findings, _ = validate_receipt(
                        manifest,
                        args.manifest,
                        contract,
                        plan,
                        target_receipt,
                        expected_mode="live",
                        expected_target=target,
                    )
                    receipt_findings.errors.extend(target_findings.errors)
                    receipt_findings.blockers.extend(target_findings.blockers)
                    target_journey_receipts[target] = (
                        Path(raw_path),
                        target_receipt,
                    )
                supplied: dict[str, tuple[Path, dict]] = {}
                for entry in args.external_receipt:
                    if "=" not in entry:
                        raise ValueError("--external-receipt requires ID=PATH")
                    receipt_id, raw_path = entry.split("=", 1)
                    path = Path(raw_path)
                    supplied[receipt_id] = (path, _load_json(path))
                supplied_evidence: dict[str, tuple[Path, dict]] = {}
                for entry in args.external_evidence:
                    if "=" not in entry:
                        raise ValueError("--external-evidence requires ID=PATH")
                    receipt_id, raw_path = entry.split("=", 1)
                    path = Path(raw_path)
                    supplied_evidence[receipt_id] = (path, _load_json(path))
                external_findings, external_records = validate_external_receipts(
                    manifest,
                    args.manifest,
                    contract,
                    supplied,
                    supplied_evidence,
                    target_journey_receipts,
                )
        report = build_report(
            manifest,
            args.manifest,
            contract,
            contract_findings,
            receipt=receipt,
            receipt_findings=receipt_findings,
            receipt_detail=receipt_detail,
            sdk_head=sdk_head,
            external_findings=external_findings,
            external_receipts=external_records,
        )
        _write_json(args.json_out, report)
    except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        report = {
            "schemaVersion": "honua.ai-delivery-arc-release-receipt/v1",
            "status": "fail",
            "errors": [str(exc)],
            "blockers": [],
        }
        _write_json(args.json_out, report)

    print(f"AI delivery arc: {report['status'].upper()}")
    for error in report.get("errors") or []:
        print(f"ERROR {error}")
    for blocker in report.get("blockers") or []:
        print(f"BLOCKED {blocker}")
    if report["status"] == "fail":
        return 1
    if report["status"] == "blocked" and args.require_real:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
