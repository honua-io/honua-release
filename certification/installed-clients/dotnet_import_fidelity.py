#!/usr/bin/env python3
"""Fail-closed gate for a clean published-.NET-SDK service/layer import receipt.

The frozen scorecard denominator (honua-server import-fidelity-scorecard-baseline)
stays intact. This module never writes a receipt and never treats a missing,
skipped, waived, released, source-built, or not-applicable cell as a pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REQUIREMENTS = Path(__file__).with_name("dotnet-import-fidelity.requirements.v1.json")
DEFAULT_MANIFEST = ROOT / "platform-manifest.yaml"
REQUIREMENTS_SCHEMA = "honua.dotnet-import-fidelity-requirements/v1"
RECEIPT_SCHEMA = "honua.dotnet-import-fidelity-receipt/v1"
PUBLIC_REGISTRIES = ("https://api.nuget.org/v3/index.json",)
PIN_ARTIFACT = "honua-sdk-dotnet"
ISSUE = "https://github.com/honua-io/honua-release/issues/317"
DEPENDENCIES = (
    "https://github.com/honua-io/honua-release/issues/7",
    "https://github.com/honua-io/honua-release/issues/157",
    "https://github.com/honua-io/honua-server/issues/4420",
)
# Order matches tests/dotnet/Honua.Server.Tests/Import/import-fidelity-scorecard-baseline.json.
FROZEN_SERVICE_CASES = (
    "esri_census_states",
    "esri_military_ops_area_z",
    "esri_military_ops_line_zm",
    "esri_usa_cities",
    "esri_usa_highways",
    "esri_wildfire_lines",
    "hawaii_historiccultural_moku",
    "hawaii_infra_dams",
    "hawaii_infra_marine_sewerlines",
    "kauai_bridges",
)
FROZEN_CHECKS = (
    "all_fields_diff",
    "core_query_parity",
    "distinct_parity",
    "edge_case_query_parity",
    "error_shape_parity",
    "geojson_query_parity",
    "geometry_parity",
    "grouped_statistics_parity",
    "mapserver_featureserver_parity",
    "spatial_envelope_parity",
    "statistics_parity",
    "time_query_parity",
)
NOT_APPLICABLE_CHECKS = frozenset({"time_query_parity"})
JOURNEY_REQUIREMENTS = (
    ("journey.published-package-install", "package"),
    ("journey.inventory-discovery", "lifecycle"),
    ("journey.service-selection", "lifecycle"),
    ("journey.apply", "lifecycle"),
    ("journey.wait", "lifecycle"),
    ("journey.cancellation-recovery", "lifecycle"),
    ("journey.reconciliation", "lifecycle"),
    ("fixture.points", "fixture"),
    ("fixture.lines", "fixture"),
    ("fixture.polygons", "fixture"),
    ("fixture.tables", "fixture"),
    ("fixture.domains-subtypes", "fixture"),
    ("fixture.related-records", "fixture"),
    ("fixture.attachments", "fixture"),
    ("fixture.zm-curves", "fixture"),
    ("fixture.nulls-int64-time", "fixture"),
    ("fixture.renderer-time-metadata", "fixture"),
    ("fixture.filters-crs", "fixture"),
    ("fixture.auth", "fixture"),
    ("fixture.pagination-limits", "fixture"),
    ("fixture.bounded-large-data", "fixture"),
    ("fixture.injected-failure", "fixture"),
    ("crosscheck.counts-values", "crosscheck"),
    ("crosscheck.geometry-tolerances", "crosscheck"),
    ("crosscheck.schema-metadata", "crosscheck"),
    ("crosscheck.attachment-hashes", "crosscheck"),
    ("crosscheck.relationships", "crosscheck"),
    ("crosscheck.honua-and-esri-clients", "crosscheck"),
    ("evidence.immutable-identities", "evidence"),
    ("evidence.rollback-repoint", "evidence"),
    ("evidence.rejects-data-loss", "evidence"),
    ("dependency.server-4420", "dependency"),
)
_EVIDENCE = re.compile(
    r"^https://github\.com/honua-io/[A-Za-z0-9_.-]+/actions/runs/[0-9]+(?:/attempts/[0-9]+)?$"
    r"|^https://github\.com/honua-io/honua-evidence/.+"
)
_LOSS_TOKENS = ("loss", "drift", "mismatch", "incomplete")
_NON_PASS_VERDICTS = frozenset({
    "fail", "skipped", "missing", "released", "waived", "unknown", "unsupported", "blocked",
})


def expected_requirement_cells() -> list[dict[str, str]]:
    cells: list[dict[str, str]] = []
    for service_case in FROZEN_SERVICE_CASES:
        for check in FROZEN_CHECKS:
            kind = "not-applicable" if check in NOT_APPLICABLE_CHECKS else "supported"
            requirement_id = f"{service_case}/{check}"
            cells.append({
                "id": requirement_id,
                "requirementId": requirement_id,
                "kind": kind,
                "group": "baseline",
                "serviceCase": service_case,
                "check": check,
            })
    for requirement_id, group in JOURNEY_REQUIREMENTS:
        cell = {
            "id": requirement_id,
            "requirementId": requirement_id,
            "kind": "supported",
            "group": group,
        }
        if requirement_id == "dependency.server-4420":
            cell["dependsOn"] = DEPENDENCIES[2]
        cells.append(cell)
    cells.sort(key=lambda cell: cell["id"])
    return cells


def canonical_revision(cells: list[dict[str, Any]]) -> str:
    payload = "\n".join(f"{cell['id']} {cell['kind']}" for cell in sorted(cells, key=lambda item: item["id"]))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


def requirements_document() -> dict[str, Any]:
    cells = expected_requirement_cells()
    supported = sum(1 for cell in cells if cell["kind"] == "supported")
    not_applicable = sum(1 for cell in cells if cell["kind"] == "not-applicable")
    return {
        "schema": REQUIREMENTS_SCHEMA,
        "revision": canonical_revision(cells),
        "issue": ISSUE,
        "dependencies": list(DEPENDENCIES),
        "publicRegistries": list(PUBLIC_REGISTRIES),
        "frozenBaseline": {
            "source": "honua-server tests/dotnet/Honua.Server.Tests/Import/import-fidelity-scorecard-baseline.json",
            "serviceCases": len(FROZEN_SERVICE_CASES),
            "checksPerCase": len(FROZEN_CHECKS),
            "supportedBaselineCells": supported - len(JOURNEY_REQUIREMENTS),
            "notApplicableCells": not_applicable,
            "note": (
                "time_query_parity is not applicable on the frozen baseline and is outside the pass "
                "denominator. Journey cells add the published-SDK consumer requirements; they do not "
                "replace or shrink the baseline."
            ),
        },
        "cells": cells,
    }


def load_requirements(path: Path | None = None) -> dict[str, Any]:
    return json.loads((path or DEFAULT_REQUIREMENTS).read_text())


def server_image_ref(manifest: dict[str, Any]) -> tuple[str, str]:
    server = manifest.get("components", {}).get("honua-server", {})
    image = str(server.get("image", "")).split("@")[0]
    digest = str(server.get("digest", ""))
    return str(server.get("sha", "")), f"{image}@{digest}" if image and digest else ""


def evaluate(
    manifest: dict[str, Any],
    receipt: dict[str, Any] | None,
    requirements: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requirements = requirements if requirements is not None else load_requirements()
    findings: list[dict[str, str]] = []

    def fail(check: str, why: str) -> None:
        findings.append({"check": check, "why": why})

    governed = _governed_cells(requirements, fail)
    supported_ids = [cell["id"] for cell in governed if cell["kind"] == "supported"]
    not_applicable_ids = [cell["id"] for cell in governed if cell["kind"] == "not-applicable"]
    denominator = len(supported_ids)
    if receipt is None:
        fail("receipt", "no published-.NET-SDK consumer receipt; missing evidence is not a pass")
        return _verdict(findings, denominator, 0, len(not_applicable_ids))
    if not isinstance(receipt, dict):
        fail("receipt", "receipt is not a JSON object")
        return _verdict(findings, denominator, 0, len(not_applicable_ids))
    if findings:
        return _verdict(findings, denominator, 0, len(not_applicable_ids))

    _check_envelope(manifest, receipt, requirements, fail)
    observed = _index_cells(receipt, fail)
    passed = _score_cells(supported_ids, not_applicable_ids, observed, fail)
    _check_summary(receipt, denominator, passed, len(not_applicable_ids), observed, supported_ids, fail)
    return _verdict(findings, denominator, passed, len(not_applicable_ids))


def _governed_cells(requirements: dict[str, Any], fail) -> list[dict[str, Any]]:
    if requirements.get("schema") != REQUIREMENTS_SCHEMA:
        fail("requirements", f"schema must be {REQUIREMENTS_SCHEMA}")
        return []
    if requirements.get("issue") != ISSUE:
        fail("requirements", "requirements are not bound to honua-release#317")
    if list(requirements.get("dependencies") or []) != list(DEPENDENCIES):
        fail("requirements", "installed-client and server#4420 dependencies are missing")
    if tuple(requirements.get("publicRegistries") or ()) != PUBLIC_REGISTRIES:
        fail("requirements", "public NuGet allowlist must be exactly nuget.org")
    expected = expected_requirement_cells()
    if requirements.get("cells") != expected:
        fail("requirements", "governed denominator does not match the frozen baseline plus journey cells")
    if requirements.get("revision") != canonical_revision(expected):
        fail("requirements", "requirements revision does not match the frozen cell denominator")
    return expected


def _check_envelope(manifest: dict[str, Any], receipt: dict[str, Any], requirements: dict[str, Any], fail) -> None:
    if receipt.get("schema") != RECEIPT_SCHEMA:
        fail("schema", f"schema must be {RECEIPT_SCHEMA}")
    if receipt.get("requirementsRevision") != requirements.get("revision"):
        fail("stale", "requirements revision does not match the governed denominator")
    if not _timestamp(receipt.get("generatedAt")):
        fail("generatedAt", "generatedAt is missing or not a UTC timestamp")
    if not _valid_evidence(receipt.get("evidenceUri")):
        fail("evidence", "receipt evidence URI is missing or not a durable honua-io actions/evidence link")
    consumer = receipt.get("consumer")
    if not isinstance(consumer, dict):
        fail("consumer", "consumer identity is missing")
    else:
        _check_consumer(manifest, consumer, fail)
    sha, image = server_image_ref(manifest)
    server = receipt.get("server")
    if not isinstance(server, dict) or server.get("sourceSha") != sha or server.get("image") != image:
        fail("server", "server source SHA or image digest does not match the manifest pin")
    if receipt.get("postgis") is not True:
        fail("postgis", "receipt does not record real PostGIS")
    if receipt.get("sdkSeamMocks") is not False:
        fail("sdk-seam-mocks", "SDK seam mocks are not a pass")
    if receipt.get("rollbackRepoint") != "pass":
        fail("rollback-repoint", "rollback/repoint did not pass")
    differences = receipt.get("differences")
    if not isinstance(differences, list):
        fail("differences", "data-loss differences were not recorded")
    else:
        for index, item in enumerate(differences):
            if not isinstance(item, dict) or _difference_blocks(item):
                code = item.get("code") if isinstance(item, dict) else None
                fail("data-loss", f"difference {index} records data loss or metadata drift ({code})")


def _check_consumer(manifest: dict[str, Any], consumer: dict[str, Any], fail) -> None:
    pin = manifest.get("clientArtifacts", {}).get(PIN_ARTIFACT)
    if not isinstance(pin, dict):
        fail("consumer", "manifest has no honua-sdk-dotnet pin")
        return
    if consumer.get("kind") != "published-nuget" or consumer.get("sourceBuilt") is not False:
        fail("source-built", "consumer is not a published NuGet package")
    if consumer.get("clean") is not True or consumer.get("projectReference") is not False:
        fail("consumer", "consumer is not a clean package install")
    if consumer.get("assemblyMatchesPackage") is not True:
        fail("consumer", "loaded assembly does not match the restored package bytes")
    if consumer.get("localPackageDir") not in (None, ""):
        fail("source-built", "local package directory is not a published NuGet install")
    if consumer.get("registry") not in PUBLIC_REGISTRIES:
        fail("registry", "registry is not the public NuGet registry")
    if consumer.get("packageId") != pin.get("package") or consumer.get("version") != pin.get("version"):
        fail("wrong-version", "package id or version does not match the manifest pin")
    if consumer.get("digest") != pin.get("digest"):
        fail("wrong-version", "package digest does not match the manifest pin")


def _index_cells(receipt: dict[str, Any], fail) -> dict[str, dict[str, Any]]:
    observed: dict[str, dict[str, Any]] = {}
    cells = receipt.get("cells")
    if not isinstance(cells, list):
        fail("cells", "per-cell results are missing")
        return observed
    for cell in cells:
        if not isinstance(cell, dict) or not isinstance(cell.get("id"), str):
            fail("cells", "cell is missing an id")
            continue
        requirement_id = cell["id"]
        if requirement_id in observed:
            fail(requirement_id, "duplicate cell")
            continue
        observed[requirement_id] = cell
    return observed


def _score_cells(
    supported_ids: list[str],
    not_applicable_ids: list[str],
    observed: dict[str, dict[str, Any]],
    fail,
) -> int:
    passed = 0
    for requirement_id in supported_ids:
        cell = observed.get(requirement_id)
        if cell is None:
            fail(requirement_id, "missing")
            continue
        verdict = cell.get("verdict")
        if verdict == "not-applicable":
            fail(requirement_id, "supported cell dropped from the denominator")
            continue
        if verdict in _NON_PASS_VERDICTS:
            fail(requirement_id, f"{verdict} is not a pass")
            continue
        if verdict != "pass":
            fail(requirement_id, f"verdict {verdict!r} is not a pass")
            continue
        reason = _pass_defect(cell)
        if reason:
            fail(requirement_id, reason)
            continue
        passed += 1
    for requirement_id in not_applicable_ids:
        cell = observed.get(requirement_id)
        if cell is None:
            fail(requirement_id, "missing not-applicable cell")
            continue
        if cell.get("verdict") == "pass":
            fail(requirement_id, "not-applicable cell counted as pass")
            continue
        if cell.get("verdict") != "not-applicable" or cell.get("executed") is not False:
            fail(requirement_id, "not-applicable cell must be recorded and excluded from the denominator")
    return passed


def _pass_defect(cell: dict[str, Any]) -> str | None:
    if cell.get("executed") is not True:
        return "not executed"
    if cell.get("stale") is not False:
        return "stale"
    if cell.get("sourceBuilt") is not False:
        return "source-built"
    if not _valid_evidence(cell.get("evidenceUri")):
        return "missing or non-durable evidence"
    return None


def _check_summary(
    receipt: dict[str, Any],
    denominator: int,
    passed: int,
    not_applicable: int,
    observed: dict[str, dict[str, Any]],
    supported_ids: list[str],
    fail,
) -> None:
    skipped = sum(1 for requirement_id in supported_ids if observed.get(requirement_id, {}).get("verdict") == "skipped")
    expected = {
        "denominator": denominator,
        "passed": passed,
        "failed": denominator - passed,
        "skipped": skipped,
        "notApplicable": not_applicable,
    }
    summary = receipt.get("summary")
    if summary != expected:
        fail("summary", f"summary does not match the governed denominator {expected}")
    claimed = receipt.get("denominator")
    if claimed is not None and claimed != denominator:
        fail("denominator", "receipt denominator shrinks or inflates the governed supported set")


def _difference_blocks(item: dict[str, Any]) -> bool:
    severity = str(item.get("severity", "")).lower()
    code = str(item.get("code", "")).lower()
    if severity in {"blocking", "fail", "error"} or severity == "":
        return True
    return any(token in code for token in _LOSS_TOKENS)


def _valid_evidence(value: object) -> bool:
    return isinstance(value, str) and _EVIDENCE.fullmatch(value) is not None


def _timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _verdict(findings: list[dict[str, str]], denominator: int, passed: int, not_applicable: int) -> dict[str, Any]:
    status = "pass" if not findings and passed == denominator else "fail"
    if status == "pass":
        reason = (
            f"{passed}/{denominator} supported cells passed; "
            f"{not_applicable} not-applicable cells excluded from the denominator"
        )
    elif findings:
        shown = "; ".join(f"{item['check']}: {item['why']}" for item in findings[:8])
        reason = f"{passed}/{denominator} supported cells passed; {shown}"
    else:
        reason = f"{passed}/{denominator} supported cells passed"
    return {
        "status": status,
        "reason": reason,
        "denominator": denominator,
        "passed": passed,
        "notApplicable": not_applicable,
        "findings": findings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate a published .NET SDK import-fidelity receipt")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = yaml.safe_load(args.manifest.read_text())
        requirements = load_requirements(args.requirements)
        receipt = json.loads(args.receipt.read_text())
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as exc:
        print(f"certification input error: {exc}", file=sys.stderr)
        return 2
    verdict = evaluate(manifest, receipt, requirements)
    print(json.dumps(verdict, indent=2))
    return 0 if verdict["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
