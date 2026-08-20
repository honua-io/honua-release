#!/usr/bin/env python3
"""Validate and bind the D9.3 seven-stage SDK journey to one release candidate.

The journey implementation belongs to honua-sdk-js. This release-side checker
only consumes its checked-in plan and receipt, verifies the cross-component
contract, and emits a candidate-bound report with actionable failure attribution.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

REPO_ROOT = Path(__file__).resolve().parents[1]
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


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


def validate_external_receipts(
    manifest: dict,
    manifest_path: Path,
    contract: dict,
    receipts: dict[str, tuple[Path, dict]],
) -> tuple[Findings, list[dict]]:
    findings = Findings()
    records: list[dict] = []
    identity = candidate_identity(manifest, manifest_path)
    components = manifest.get("components") or {}
    for expected in contract.get("externalReceipts") or []:
        receipt_id = expected.get("id")
        supplied = receipts.get(receipt_id)
        if supplied is None:
            findings.blockers.append(f"live journey is missing external receipt {receipt_id} ({expected.get('issue')})")
            continue
        path, receipt = supplied
        if receipt.get("schemaVersion") != "honua.release.evidence-receipt/v1":
            findings.errors.append(f"external receipt {receipt_id} has an unsupported schemaVersion")
        if receipt.get("id") != receipt_id or receipt.get("status") != "passed":
            findings.errors.append(f"external receipt {receipt_id} must identify itself and have status=passed")
        if receipt.get("candidateId") != identity["candidateId"]:
            findings.errors.append(f"external receipt {receipt_id} is not bound to the exact manifest digest")
        if receipt.get("releaseId") != manifest.get("platformRelease"):
            findings.errors.append(f"external receipt {receipt_id} has the wrong platform release id")
        source = receipt.get("source") or {}
        component_name = expected.get("sourceComponent")
        component = components.get(component_name) or {}
        if source.get("repository") != expected.get("sourceRepository") or source.get("sha") != component.get("sha"):
            findings.errors.append(f"external receipt {receipt_id} is not from the manifest-pinned {component_name}")
        receipt_components = receipt.get("components") or {}
        for bound_name in expected.get("boundComponents") or []:
            pinned_sha = (components.get(bound_name) or {}).get("sha")
            if receipt_components.get(bound_name) != pinned_sha:
                findings.errors.append(
                    f"external receipt {receipt_id} does not bind manifest component {bound_name}"
                )
        evidence = receipt.get("evidence") or {}
        if not str(evidence.get("url", "")).startswith(("https://", "http://")):
            findings.errors.append(f"external receipt {receipt_id} has no evidence URL")
        if not SHA256.fullmatch(str(evidence.get("sha256", ""))):
            findings.errors.append(f"external receipt {receipt_id} has no evidence SHA-256")
        records.append(
            {
                "id": receipt_id,
                "path": str(path),
                "receiptSha256": _sha256(path),
                "source": source,
                "components": receipt_components,
                "evidence": evidence,
            }
        )
    return findings, records


def validate_receipt(
    manifest: dict,
    manifest_path: Path,
    contract: dict,
    plan: dict,
    receipt: dict,
    *,
    expected_mode: str,
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

    plan_actions = _action_map(plan)
    receipt_actions = _receipt_action_map(receipt)
    if set(plan_actions) != set(receipt_actions):
        missing = sorted(set(plan_actions) - set(receipt_actions))
        extra = sorted(set(receipt_actions) - set(plan_actions))
        findings.errors.append(f"SDK journey receipt action inventory drift (missing={missing}, extra={extra})")

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
            findings.errors.append("contract-mode SDK receipt must be explicitly blocked")
        first = (receipt.get("stages") or [{}])[0].get("actions") or [{}]
        if first[0].get("code") != "live-execution-disabled":
            findings.errors.append("contract-mode receipt did not attribute the block to disabled live execution")
        findings.blockers.append(
            "contract mode validated the deterministic plan but did not execute the live candidate"
        )
    else:
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
            findings.blockers.append(f"live journey has non-passing actions: {not_passed}")

        identity = candidate_identity(manifest, manifest_path)
        console = receipt_actions.get("console-approval") or {}
        captures = console.get("captures") or {}
        if captures.get("candidateId") != identity["candidateId"]:
            findings.errors.append("Console receipt candidateId is not the exact platform-manifest digest")
        if captures.get("releaseId") != manifest.get("platformRelease"):
            findings.errors.append("Console receipt releaseId is not the manifest platformRelease")
        evidence = console.get("evidence") or {}
        if evidence.get("source") != "external-receipt" or not SHA256.fullmatch(str(evidence.get("sha256", ""))):
            findings.errors.append("Console checkpoint has no content-addressed external receipt evidence")

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
        "--external-receipt",
        action="append",
        default=[],
        metavar="ID=PATH",
        help="candidate-bound external receipt; repeat for AWS and real-model Studio evidence",
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
            )
            if args.mode == "live":
                supplied: dict[str, tuple[Path, dict]] = {}
                for entry in args.external_receipt:
                    if "=" not in entry:
                        raise ValueError("--external-receipt requires ID=PATH")
                    receipt_id, raw_path = entry.split("=", 1)
                    path = Path(raw_path)
                    supplied[receipt_id] = (path, _load_json(path))
                external_findings, external_records = validate_external_receipts(
                    manifest, args.manifest, contract, supplied
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
