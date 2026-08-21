#!/usr/bin/env python3
"""Generate the complete protocol/client certification denominator."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
OUTPUT = ROOT / "protocol-certification-requirements.v1.json"
SOURCES = ROOT / "sources"
SUPPORTED = {"implemented", "partial", "covered"}
FIXTURE = "docker/cng/seed.sql@{source_sha}"
IDENTITY_FIELDS = ("surface", "operation", "canonical_client", "client_version", "deployment_target")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def main() -> None:
    revisions = load(SOURCES / "source-revisions.v1.json")["sources"]
    existing = load(OUTPUT)
    requirements = [row for row in existing["requirements"] if row["capability_key"].startswith("format.")]
    seen = {tuple(row[field] for field in IDENTITY_FIELDS) for row in requirements}

    def add(*, capability: str, surface: str, operation: str, client: str, lane: str,
            version: str, contract: str, target: str = "local-docker", licensed: bool = False,
            facets: list[str] | None = None, fixture: str = FIXTURE,
            required_tier: str = "nightly") -> None:
        key = (surface, operation, client, version, target)
        if key in seen:
            return
        seen.add(key)
        requirements.append({
            "capability_key": capability,
            "surface": surface,
            "operation": operation,
            "maturity": "supported",
            "canonical_client": client,
            "client_lane": lane,
            "client_version": version,
            "deployment_target": target,
            "required_tier": required_tier,
            "licensed": licensed,
            "addressable_by_client": True,
            "addressability_reason": None,
            "scenario_facets": facets or ["positive", "metadata", "media-schema"],
            "contract_revision": contract,
            "auth_policy_revision": "anonymous-and-protected-v1",
            "fixture_revision": fixture,
        })

    sdk_sources = [
        ("sdk-dotnet", "coverage", "Honua SDK .NET", "1.6.0", "sdk-dotnet", "sha256:83eb29ac38a3fb54914c1252b273dbb7f7f4d651a8204aafb4108d14d6d23727"),
        ("sdk-python", "capabilities", "Honua SDK Python", "0.1.11", "sdk-python", "geospatial-grpc@0.2.0-alpha.1"),
        ("sdk-js", "capabilities", "@honua/sdk-js", "0.1.7-beta.0", "sdk-js", "0.1.0-alpha.3"),
    ]
    for source_name, collection, client, version, surface, fixture in sdk_sources:
        snapshot = load(SOURCES / source_name / "sdk-coverage.v1.json")
        for capability in snapshot[collection]:
            if capability.get("status") not in SUPPORTED:
                continue
            for entrypoint in capability.get("entrypoints", []):
                add(
                    capability=capability["key"], surface=surface, operation=entrypoint,
                    client=client, lane=surface, version=version,
                    contract=f"{source_name}-coverage@{revisions[source_name]['commit']}",
                    facets=["positive", "media-schema"], fixture=fixture,
                )

    for source_name in ("sdk-python", "sdk-js"):
        contract = load(SOURCES / source_name / "protocol-certification.v1.json")
        for operation in contract["operations"]:
            add(
                capability=operation["capability_key"], surface=operation["surface"],
                operation=operation["operation"], client=contract["canonicalClient"],
                lane=f"{source_name}-certification", version=contract["clientVersion"],
                contract=f"{source_name}-certification@{revisions[source_name]['commit']}",
                fixture=contract["fixtureRevision"], facets=operation["scenario_facets"],
            )

    dotnet = load(SOURCES / "sdk-dotnet" / "sdk-certification.v1.json")
    tier_order = ("pr", "nightly", "release")
    for operation in dotnet["operations"]:
        required_tier = next(
            (tier for tier in tier_order if tier in operation["requiredTiers"]), None
        )
        if operation["status"] == "non-addressable" or required_tier is None:
            continue
        facets = list(dict.fromkeys(
            "positive" if facet in {"read-only", "mutation"} else facet
            for facet in operation["scenarioFacets"]
        ))
        add(
            capability=f"sdk-dotnet.{operation['surface']}", surface=operation["surface"],
            operation=operation["id"], client="Honua SDK .NET", lane="sdk-dotnet-certification",
            version="1.6.0", contract=f"sdk-dotnet-certification@{revisions['sdk-dotnet']['commit']}",
            fixture="sha256:83eb29ac38a3fb54914c1252b273dbb7f7f4d651a8204aafb4108d14d6d23727",
            facets=facets, required_tier=required_tier,
        )

    grpc = load(SOURCES / "geospatial-grpc" / "operations.v1.json")
    grpc_fixture = f"geospatial-grpc-conformance@{grpc['fixture_version']}+{grpc['source_sha']}"
    grpc_clients = (
        ("Generated gRPC .NET client", "grpc-dotnet"),
        ("Generated gRPC Python client", "grpc-python"),
        ("Generated gRPC TypeScript client", "grpc-typescript"),
    )
    for rpc in grpc["operations"]:
        operation = f"{rpc['service']}/{rpc['operation']}"
        for client, lane in grpc_clients:
            add(
                capability=f"grpc.{slug(rpc['service'])}", surface="grpc", operation=operation,
                client=client, lane=lane, version=f"source@{grpc['source_sha']}",
                contract=f"geospatial-grpc@{grpc['source_sha']}", fixture=grpc_fixture,
                facets=["positive", "negative", "media-schema"],
            )

    mcp = load(SOURCES / "geospatial-mcp" / "operations.v1.json")
    mcp_clients = (
        ("Official MCP TypeScript SDK", "mcp-typescript-sdk", "1.30.0"),
        ("MCP Inspector", "mcp-inspector", "2.3.0"),
    )
    for entry in mcp["operations"]:
        for client, lane, version in mcp_clients:
            add(
                capability=f"mcp.{entry['kind']}", surface="mcp", operation=entry["operation"],
                client=client, lane=lane, version=version,
                contract=f"geospatial-mcp@{mcp['source_sha']}",
                fixture=mcp["fixture_version"], facets=["positive", "negative", "media-schema"],
            )

    server = load(SOURCES / "server" / "capability-matrix.v1.json")
    lane_clients = {
        "desktop-qgis": ("QGIS", "3.40"),
        "desktop-arcgis": ("ArcGIS Pro", "3.5"),
        "ci-desktop": ("QGIS", "3.40"),
        "js": ("Honua SDK JavaScript", "0.1.7-beta.0"),
        "js-cesium": ("CesiumJS", "1.132.0"),
        "cli": ("Honua CLI", f"source@{revisions['server']['commit'][:12]}"),
        "arcgis-stub": ("ArcGIS REST contract client", "11.3"),
        "bi-excel": ("Microsoft Excel", "Microsoft 365"),
        "bi-powerbi": ("Microsoft Power BI", "2026.08"),
        "ci-bi": ("Microsoft.OData.Client", "8.3"),
    }
    for capability in server["capabilities"]:
        for cite in capability.get("cite", []):
            suite = cite["suite"]
            add(
                capability=capability["key"], surface=slug(suite), operation=capability["key"],
                client="OGC CITE", lane=f"cite-{slug(suite)}", version=suite,
                contract=f"server-capability-matrix@{revisions['server']['commit']}",
                facets=["positive", "negative", "crs-axis", "media-schema"],
            )
        for interop in capability.get("interop", []):
            lane = interop["clientLane"]
            client, version = lane_clients.get(lane, (lane, f"pin@{revisions['server']['commit'][:12]}"))
            add(
                capability=capability["key"], surface=interop["protocol"], operation=capability["key"],
                client=client, lane=lane, version=version,
                contract=f"server-capability-matrix@{revisions['server']['commit']}",
            )

    esri_index = load(SOURCES / "esri-compat" / "matrix" / "index.json")
    esri_clients = [
        ("ArcGIS REST protocol client", "11.3", "raw-geoservices", "local-docker", False),
        ("ArcGIS API for Python", "2.4", "arcgis-python", "local-docker", False),
        ("ArcGIS Maps SDK for .NET", "200.8", "esri-dotnet", "windows", False),
        ("ArcGIS Pro/arcpy", "3.5", "desktop-arcpy", "windows-licensed", True),
    ]
    for service in esri_index["services"]:
        matrix = load(SOURCES / "esri-compat" / "matrix" / service["manifest"])
        for case in matrix["cases"]:
            if case.get("status") not in SUPPORTED:
                continue
            if service["service"] == "ogc":
                continue
            facets = ["positive", "auth", "media-schema"]
            if "query" in case["name"].lower():
                facets += ["pagination", "limit", "crs-axis"]
            for client, version, lane, target, licensed in esri_clients:
                add(
                    capability=f"esri.{service['service']}", surface=service["service"], operation=case["id"],
                    client=client, lane=f"{lane}-{service['service']}", version=version,
                    contract=f"esri-matrix@{revisions['esri-compat']['commit']}", target=target,
                    licensed=licensed, facets=facets,
                )

    ogc = load(SOURCES / "esri-compat" / "matrix" / "ogc.matrix.json")
    for case in ogc["cases"]:
        if case.get("status") not in SUPPORTED:
            continue
        name = case["name"].lower()
        if "features" in name or "wfs" in name:
            clients = [("OGC CITE", f"ets-selection@{revisions['server']['commit']}", "cite"), ("GDAL/OGR", "3.8.4", "gdal"), ("QGIS", "3.40", "qgis")]
        elif "tiles" in name or "wmts" in name or "wms" in name:
            clients = [("OGC CITE", f"ets-selection@{revisions['server']['commit']}", "cite"), ("QGIS", "3.40", "qgis"), ("MapLibre GL JS", "5.7", "maplibre")]
        elif "wcs" in name or "coverage" in name:
            clients = [("OGC CITE", f"ets-selection@{revisions['server']['commit']}", "cite"), ("GDAL", "3.8.4", "gdal"), ("OWSLib", "0.34", "owslib")]
        else:
            clients = [("OGC CITE", f"ets-selection@{revisions['server']['commit']}", "cite"), ("Honua SDK Python", f"source-preview@{revisions['sdk-python']['commit']}", "sdk-python")]
        for client, version, lane in clients:
            add(
                capability="serve.ogc", surface="ogc", operation=case["id"], client=client,
                lane=f"{lane}-ogc", version=version,
                contract=f"esri-ogc-matrix@{revisions['esri-compat']['commit']}",
                facets=["positive", "negative", "auth", "crs-axis", "media-schema"],
            )

    requirements.sort(key=lambda row: (
        row["capability_key"], row["surface"], row["operation"], row["canonical_client"], row["client_lane"]
    ))
    output = {
        "schema": "honua.protocol-certification-requirements/v1",
        "revision": "2026-08-21-complete.3",
        "complete": True,
        "scope_notes": (
            "Complete supported denominator generated from pinned server capability/CITE/interop assignments, "
            "Esri operation matrices, SDK entrypoints, cloud-native canonical clients, generated gRPC "
            "clients, official MCP SDK/Inspector operations, and executable operation contracts for all three Honua SDKs. "
            "The .NET contract contributes 272 addressable operations; 18 explicitly non-addressable public abstractions "
            "remain documented in its pinned source contract and excluded from client certification. "
            "Roadmap Kerchunk and COPC capabilities remain excluded until promoted to supported."
        ),
        "source_revisions": revisions,
        "requirements": requirements,
    }
    OUTPUT.write_text(json.dumps(output, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT} with {len(requirements)} required certification cells.")


if __name__ == "__main__":
    main()
