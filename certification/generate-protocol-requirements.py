#!/usr/bin/env python3
"""Generate the complete protocol/client certification denominator."""

from __future__ import annotations

import fnmatch
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
# R38 (honua-release#376): desktop clients are certified against a release line, <major>.<minor>.x;
# a receipt from any patch of the line satisfies it and records the exact version it observed.
# R40 splits each client by driver. Scripting rows are must-fix-before-cut on every applicable
# GeoServices and OGC cell. UI rows (pro-ui, qgis-ui) are must-fix-before-cut only on the core
# set named in the source; every other UI cell is prove-against-candidate.
DESKTOP = json.loads((SOURCES / "desktop-client-certification.v1.json").read_text(encoding="utf-8"))
QGIS = DESKTOP["clients"]["qgis"]
QGIS_VERSION = QGIS["version"]
PRO = DESKTOP["clients"]["pro"]
DESKTOP_DRIVERS = DESKTOP["drivers"]
MUST_FIX = DESKTOP["buckets"]["must_fix"]
PROVE = DESKTOP["buckets"]["prove"]


def licensed_desktop(release_bucket: str, client_driver: str, *, entitlement: str | None = None) -> dict[str, Any]:
    """Every row of the licensed desktop client runs on its governed licensed target under its
    entitlement policy, so the gate applies licensed-evidence entitlement and freshness rules."""
    return {
        "target": PRO["deployment_target"], "auth_policy": PRO["auth_policy_revision"], "licensed": True,
        "entitlement_policy": entitlement or PRO["entitlement_policy_revision"],
        "release_bucket": release_bucket, "client_driver": client_driver,
    }


def ui_functions(surface: dict[str, Any]) -> list[str]:
    """The UI operations a surface contributes: the shared set, FeatureServer edit, and WFS-T edit."""
    ui = DESKTOP["ui"]
    functions = list(ui["functions"])
    if surface.get("edit"):
        functions.append(ui["feature_edit"])
    if surface.get("wfs_edit"):
        functions.append(ui["wfs_edit"])
    return functions


def desktop_driver_summary(requirements: list[dict[str, Any]], drivers: tuple[str, ...]) -> dict[str, Any]:
    """Counts of desktop rows per driver per release bucket (R40)."""
    counts = {driver: {MUST_FIX: 0, PROVE: 0} for driver in drivers}
    for row in requirements:
        driver = row.get("client_driver")
        if driver is None:
            continue
        if driver not in counts:
            raise ValueError(f"desktop row names an unknown client_driver {driver!r}")
        bucket = row.get("release_bucket")
        if bucket not in counts[driver]:
            raise ValueError(f"desktop row {row['surface']}/{row['operation']} has no governed release_bucket")
        counts[driver][bucket] += 1
    return {"ruling": "R40", "drivers": counts}


def emit_pyqgis_grid(add, *, preview_capabilities: set[str]) -> None:
    """R40: PyQGIS is every server-declared surface crossed with every function that client can
    perform, not one row per surface. A function the client cannot perform is not-applicable,
    with the reason carried on the row."""
    grid = load(SOURCES / "pyqgis-function-grid.v1.json")
    if grid.get("schema") != "honua.pyqgis-function-grid/v1" or grid.get("ruling") != "R40":
        raise ValueError("pyqgis function grid must be schema honua.pyqgis-function-grid/v1 under ruling R40")
    functions = grid["functions"]
    operations = [item["operation"] for item in functions]
    if len(operations) != len(set(operations)) or not operations:
        raise ValueError("pyqgis function grid needs a unique operation per function")
    families: dict[str, str] = {}
    allowed_families = {
        "connect/discover", "add", "render", "identify", "query/filter", "edit",
        "raster read", "save/reopen", "auth",
    }
    for item in functions:
        family = item["family"]
        if family not in allowed_families:
            raise ValueError(f"pyqgis function {item['operation']} names an unknown family {family!r}")
        families[item["operation"]] = family
    surfaces = grid["surfaces"]
    if not surfaces or len({item["surface"] for item in surfaces}) != len(surfaces):
        raise ValueError("pyqgis function grid repeats a surface or declares none")
    contract = f"pyqgis-function-grid@{grid['revision']}"
    driver = DESKTOP_DRIVERS["pyqgis"]
    for surface in surfaces:
        declared = surface["not_applicable"]
        unknown = set(declared) - set(operations)
        if unknown:
            raise ValueError(f"pyqgis surface {surface['surface']} names unknown functions {sorted(unknown)}")
        if surface["capability_key"] in preview_capabilities and set(declared) != set(operations):
            raise ValueError(
                f"preview surface {surface['surface']} must declare every PyQGIS function not-applicable"
            )
        for operation in operations:
            reason = declared.get(operation)
            addressable = reason is None
            if not addressable and not (isinstance(reason, str) and reason.startswith("not-applicable: ")):
                raise ValueError(
                    f"pyqgis {surface['surface']}/{operation} needs a not-applicable reason, not {reason!r}"
                )
            facets = (
                ["not-client-addressable"] if not addressable
                else ["auth"] if families[operation] == "auth"
                else ["positive", "auth", "media-schema"]
            )
            add(
                capability=surface["capability_key"], surface=surface["surface"], operation=operation,
                client=driver["canonical_client"], lane=driver["lane"], version=QGIS_VERSION,
                contract=contract, auth_policy=QGIS["auth_policy_revision"],
                target=QGIS["deployment_target"], facets=facets, addressable=addressable,
                addressability_reason=reason, release_bucket=MUST_FIX, client_driver="pyqgis",
            )
    for dropped in grid["dropped_functions"]:
        reason = dropped["addressability_reason"]
        if not (isinstance(reason, str) and reason.startswith("not-applicable: ")):
            raise ValueError(f"dropped PyQGIS function {dropped['operation']} needs a not-applicable reason")
        add(
            capability=dropped["surface"], surface=dropped["surface"], operation=dropped["operation"],
            client=driver["canonical_client"], lane=driver["lane"], version=QGIS_VERSION,
            contract=contract, auth_policy=QGIS["auth_policy_revision"], target=QGIS["deployment_target"],
            facets=["not-client-addressable"], addressable=False, addressability_reason=reason,
            release_bucket=MUST_FIX, client_driver="pyqgis",
        )


PR_SDK_SMOKE_OPERATIONS = {
    "sdk-js": {
        ("featureserver", "metadata"),
        ("featureserver", "query"),
        ("grpc-web", "query"),
        ("imageserver", "export-image"),
        ("ogc-features", "items"),
        ("stac", "search"),
        ("wmts", "get-tile"),
    },
    "sdk-python": {
        ("geoservices-root", "list-services"),
        ("geoservices-featureserver", "layer-metadata"),
        ("geoservices-featureserver", "query"),
        ("ogc-api-features", "items"),
        ("ogc-api-processes", "list-processes"),
    },
}


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


GRPC_SCOPE_RULING = "grpc-2026.1-implemented-rpcs"
GRPC_EXCLUDED_MATURITIES = {"preview", "experimental"}


def grpc_scope(
    grpc: dict[str, Any], server_commit: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split the gRPC inventory into in-scope RPCs and ruled exclusions.

    The ruling must have been checked against the pinned server commit. An
    excluded RPC must be in the inventory, name the scope ruling, and carry its
    maturity, rationale, decision, owner issue and target release. It gets no
    generated requirement, so it can neither pass nor block as GA.
    """
    rulings = {ruling["id"]: ruling for ruling in grpc.get("rulings", [])}
    if GRPC_SCOPE_RULING not in rulings:
        raise ValueError(f"geospatial-grpc inventory has no {GRPC_SCOPE_RULING!r} ruling")
    if server_commit not in rulings[GRPC_SCOPE_RULING].get("verified_server_commits", []):
        raise ValueError(
            f"the {GRPC_SCOPE_RULING!r} ruling was not verified against the pinned server commit "
            f"{server_commit}; re-check which RPCs that revision implements and record it"
        )
    inventory = {(rpc["service"], rpc["operation"]) for rpc in grpc["operations"]}
    if len(inventory) != len(grpc["operations"]):
        raise ValueError("geospatial-grpc inventory lists an RPC more than once")
    excluded: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in grpc.get("excluded_operations", []):
        key = (entry["service"], entry["operation"])
        if key not in inventory:
            raise ValueError(f"excluded gRPC operation {key} is not in the inventory")
        if key in excluded:
            raise ValueError(f"excluded gRPC operation {key} is listed more than once")
        if entry.get("ruling") != GRPC_SCOPE_RULING:
            raise ValueError(f"excluded gRPC operation {key} does not name the {GRPC_SCOPE_RULING!r} ruling")
        if entry.get("maturity") not in GRPC_EXCLUDED_MATURITIES:
            raise ValueError(f"excluded gRPC operation {key} must be preview or experimental")
        missing = [
            field for field in ("rationale", "decision", "owner_issue", "target_release")
            if not isinstance(entry.get(field), str) or not entry[field].strip()
        ]
        if missing:
            raise ValueError(f"excluded gRPC operation {key} lacks {missing}")
        if not entry["owner_issue"].startswith("https://github.com/honua-io/"):
            raise ValueError(f"excluded gRPC operation {key} owner_issue must be a honua-io issue URL")
        excluded[key] = entry
    in_scope = [rpc for rpc in grpc["operations"] if (rpc["service"], rpc["operation"]) not in excluded]
    if not in_scope:
        raise ValueError("the gRPC scope ruling excludes every RPC")
    return in_scope, list(excluded.values())


def grpc_client_version(published: dict[str, Any], lane: str) -> str:
    """The installed, published package version a gRPC client lane is certified against."""
    client = published.get(lane)
    if not isinstance(client, dict):
        raise ValueError(f"geospatial-grpc published_clients has no entry for {lane}")
    version = client.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError(f"geospatial-grpc published_clients[{lane}] needs a released version, not {version!r}")
    if not any(isinstance(client.get(field), str) and client[field].startswith(("sha256:", "sha512-"))
               for field in ("digest", "integrity")):
        raise ValueError(f"geospatial-grpc published_clients[{lane}] needs the published artifact digest")
    return version


PRODUCTION_PLACEHOLDERS = ("{server_image}", "{server_sha}", "{cut_at}")


def selects(entry: dict[str, Any], row: dict[str, Any]) -> bool:
    """A disposition owns a requirement by its client lane (glob) or its deployment target, less
    the lanes it hands to another disposition (except_client_lanes, glob)."""
    if any(fnmatch.fnmatchcase(row["client_lane"], pattern) for pattern in entry.get("except_client_lanes", [])):
        return False
    return any(fnmatch.fnmatchcase(row["client_lane"], pattern) for pattern in entry.get("client_lanes", [])) \
        or row["deployment_target"] in entry.get("deployment_targets", [])


def production(requirements: list[dict[str, Any]], revisions: dict[str, Any]) -> dict[str, Any]:
    """Give every client-addressable lane exactly one disposition: a producer the nightly
    dispatches at the resolved candidate, or `unproduced` with an owner (honua-release#386, #360)."""
    source = load(SOURCES / "protocol-certification-production.v1.json")
    dispositions = [
        ("producer", entry["producer"], entry) for entry in source["producers"]
    ] + [("unproduced", entry["owner"], entry) for entry in source["unproduced"]]
    for kind, name, entry in dispositions:
        if kind == "producer":
            if entry["source_revision_key"] not in revisions:
                raise ValueError(f"producer {name} pins unknown source_revision_key {entry['source_revision_key']!r}")
            for key, value in entry["inputs"].items():
                if "{" in value and value not in PRODUCTION_PLACEHOLDERS:
                    raise ValueError(f"producer {name} input {key} uses an ungoverned placeholder {value!r}")
        elif not (entry.get("issue", "").startswith("https://github.com/honua-io/") and entry.get("missing_producer")):
            raise ValueError(f"unproduced disposition owned by {name} needs an owner issue and missing_producer")
        if not entry.get("client_lanes") and not entry.get("deployment_targets"):
            raise ValueError(f"production disposition {name} selects no requirement")
    # A lane the denominator gains needs a disposition before the catalog regenerates; a lane it
    # loses (a demoted capability) only leaves its disposition with fewer cells.
    cells: dict[int, int] = {}
    for row in requirements:
        if not row["addressable_by_client"]:
            continue
        owners = {index for index, (_kind, _name, entry) in enumerate(dispositions) if selects(entry, row)}
        if len(owners) != 1:
            raise ValueError(
                f"client lane {row['client_lane']!r} ({row['deployment_target']}) has {len(owners)} production "
                "dispositions; exactly one is required"
            )
        owner = owners.pop()
        cells[owner] = cells.get(owner, 0) + 1
    producers = [{**entry, "cells": cells.get(index, 0)} for index, (kind, _name, entry) in enumerate(dispositions) if kind == "producer"]
    unproduced = [{**entry, "cells": cells.get(index, 0)} for index, (kind, _name, entry) in enumerate(dispositions) if kind == "unproduced"]
    return {
        "revision": source["revision"],
        "cells": {
            "produced": sum(entry["cells"] for entry in producers),
            "unproduced": sum(entry["cells"] for entry in unproduced),
            "not_addressable": sum(1 for row in requirements if not row["addressable_by_client"]),
        },
        "producers": producers,
        "unproduced": unproduced,
    }


def main() -> None:
    revisions = load(SOURCES / "source-revisions.v1.json")["sources"]
    bounded_roster = load(SOURCES / "bounded-client-roster.v1.json")
    bounded_cells = {
        (cell["canonical_client"], cell["surface"], cell["operation"]): cell
        for cell in bounded_roster["cells"]
    }
    preview_ruling = next(
        ruling for ruling in bounded_roster["rulings"] if ruling["id"] == "preview-surfaces"
    )
    preview_capabilities = set(preview_ruling["preview_capability_keys"])
    preview_cells = {
        (cell["canonical_client"], cell["surface"], cell["operation"]): cell
        for cell in bounded_roster["preview_cells"]
    }
    not_addressable_cells = {
        (cell["canonical_client"], cell["surface"], cell["operation"]): cell
        for cell in bounded_roster["not_addressable_cells"]
    }
    for key, cell in preview_cells.items():
        if cell["capability_key"] not in preview_capabilities:
            raise ValueError(
                f"bounded-roster preview cell {key} names {cell['capability_key']!r}, "
                "which the preview-surfaces ruling does not make Preview"
            )
    for key, cell in not_addressable_cells.items():
        if not any(governed["test_id"] == cell["governed_by_test_id"] for governed in bounded_roster["cells"]):
            raise ValueError(
                f"bounded-roster non-addressable cell {key} names ungoverned "
                f"{cell['governed_by_test_id']!r}"
            )
    overlap = (set(bounded_cells) & set(preview_cells)) | (set(bounded_cells) & set(not_addressable_cells)) \
        | (set(preview_cells) & set(not_addressable_cells))
    if overlap:
        raise ValueError(f"bounded-roster cell has more than one disposition: {sorted(overlap)}")
    bound_cells: set[tuple[str, str, str]] = set()

    def bind_bounded_cell(row: dict[str, Any]) -> dict[str, Any] | None:
        """Give a bounded-roster row its ruled lane and single governed test ID.

        A Preview cell (release#351) yields no supported requirement; a cell no
        released client can exercise (release#359) stays as a non-addressable row.
        """
        client = bounded_roster["clients"].get(row["canonical_client"])
        if client is None:
            return row
        key = (row["canonical_client"], row["surface"], row["operation"])
        if key in bound_cells:
            raise ValueError(f"bounded-roster cell is generated more than once: {key}")
        if key in preview_cells:
            bound_cells.add(key)
            return None
        if key in not_addressable_cells:
            cell = not_addressable_cells[key]
            bound_cells.add(key)
            row = {
                **row,
                "client_lane": cell["client_lane"],
                "addressable_by_client": False,
                "addressability_reason": cell["addressability_reason"],
            }
            row.pop("test_ids", None)
            return row
        cell = bounded_cells.get(key)
        if cell is None:
            raise ValueError(f"bounded-roster requirement has no governed test ID: {key}")
        if row["capability_key"] in preview_capabilities:
            raise ValueError(
                f"bounded-roster cell {key} governs Preview capability {row['capability_key']!r}; "
                "move it to preview_cells"
            )
        if client["client_version"] is not None and row["client_version"] != client["client_version"]:
            raise ValueError(
                f"bounded-roster requirement {key} pins {row['client_version']!r}, "
                f"but the roster rules {client['client_version']!r}"
            )
        bound_cells.add(key)
        return {**row, "client_lane": cell["client_lane"], "test_ids": [cell["test_id"]]}

    format_source = json.loads(
        (ROOT / "sources" / "cloud-native-format-requirements.v1.json").read_text(encoding="utf-8")
    )
    fixture_pins = json.loads(
        (ROOT / "sources" / "canonical-client-fixtures.v1.json").read_text(encoding="utf-8")
    )["fixtures"]
    requirements = [
        bound
        for row in format_source["requirements"]
        if (bound := bind_bounded_cell({
            **row,
            "budget_expectations": row.get("budget_expectations"),
            "entitlement_policy_revision": row.get("entitlement_policy_revision"),
        })) is not None
    ]
    seen = {tuple(row[field] for field in IDENTITY_FIELDS) for row in requirements}

    def add(*, capability: str, surface: str, operation: str, client: str, lane: str,
            version: str, contract: str, auth_policy: str,
            target: str = "local-docker", licensed: bool = False,
            entitlement_policy: str | None = None,
            facets: list[str] | None = None, fixture: str = FIXTURE,
            required_tier: str = "nightly", addressable: bool = True,
            addressability_reason: str | None = None,
            test_ids: list[str] | None = None,
            release_bucket: str | None = None,
            client_driver: str | None = None) -> None:
        key = (surface, operation, client, version, target)
        if key in seen:
            return
        seen.add(key)
        row = {
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
            "entitlement_policy_revision": entitlement_policy,
            "addressable_by_client": addressable,
            "addressability_reason": addressability_reason,
            "scenario_facets": facets or ["positive", "metadata", "media-schema"],
            "contract_revision": contract,
            "auth_policy_revision": auth_policy,
            "fixture_revision": fixture,
            "budget_expectations": None,
        }
        if test_ids is not None:
            row["test_ids"] = test_ids
        if release_bucket is not None:
            row["release_bucket"] = release_bucket
        if client_driver is not None:
            row["client_driver"] = client_driver
        bound = bind_bounded_cell(row)
        if bound is not None:
            requirements.append(bound)

    sdk_sources = [
        ("sdk-python", "capabilities", "Honua SDK Python", "0.1.11", "sdk-python", "geospatial-grpc@0.2.0-alpha.1"),
        ("sdk-js", "capabilities", "@honua/sdk-js", "0.1.9-beta.0", "sdk-js", fixture_pins["sdk-js"]),
        ("sdk-dotnet", "coverage", "Honua SDK .NET", "1.6.0", "sdk-dotnet", "sha256:83eb29ac38a3fb54914c1252b273dbb7f7f4d651a8204aafb4108d14d6d23727"),
    ]
    sdk_capability_operations: dict[tuple[str, str], list[str]] = {}
    for source_name, collection, client, version, surface, fixture in sdk_sources:
        snapshot = load(SOURCES / source_name / "sdk-coverage.v1.json")
        for capability in snapshot[collection]:
            if capability.get("status") not in SUPPORTED:
                continue
            sdk_capability_operations[(source_name, capability["key"])] = list(
                capability.get("entrypoints", [])
            )
            for entrypoint in capability.get("entrypoints", []):
                add(
                    capability=capability["key"],
                    surface=f"{surface}:{slug(capability['key'])}",
                    operation=entrypoint,
                    client=client, lane=surface, version=version,
                    contract=f"{source_name}-coverage@{revisions[source_name]['commit']}",
                    auth_policy="anonymous-public-v1",
                    facets=["positive", "media-schema"], fixture=fixture,
                )

    sdk_operation_policies = {
        ("sdk-js", "streaming.feature-subscriptions", "realtime", "subscribe"): {
            "auth_policy_revision": "api-key-protected-v1",
            "deployment_target": "licensed-release",
            "required_tier": "release",
            "licensed": True,
            "entitlement_policy_revision": "honua-pro-feature-subscriptions-v1",
        },
        ("sdk-js", "streaming.feature-subscriptions", "realtime", "resume"): {
            "auth_policy_revision": "api-key-protected-v1",
            "deployment_target": "licensed-release",
            "required_tier": "release",
            "licensed": True,
            "entitlement_policy_revision": "honua-pro-feature-subscriptions-v1",
        },
    }
    applied_sdk_operation_policies = set()
    for source_name in ("sdk-python", "sdk-js"):
        contract = load(SOURCES / source_name / "protocol-certification.v1.json")
        for operation in contract["operations"]:
            policy_key = (
                source_name, operation["capability_key"],
                operation["surface"], operation["operation"],
            )
            policy = sdk_operation_policies.get(policy_key, {})
            if policy:
                applied_sdk_operation_policies.add(policy_key)
            add(
                capability=operation["capability_key"], surface=operation["surface"],
                operation=operation["operation"], client=contract["canonicalClient"],
                lane=f"{source_name}-certification", version=contract["clientVersion"],
                contract=f"{source_name}-certification@{revisions[source_name]['commit']}",
                auth_policy=policy.get(
                    "auth_policy_revision",
                    operation.get("authPolicyRevision", "anonymous-public-v1"),
                ),
                target=policy.get(
                    "deployment_target",
                    operation.get("deploymentTarget", "local-docker"),
                ),
                fixture=(
                    fixture_pins["sdk-js"]
                    if source_name == "sdk-js"
                    else contract["fixtureRevision"]
                ),
                facets=operation["scenario_facets"],
                licensed=policy.get("licensed", operation.get("licensed", False)),
                entitlement_policy=policy.get(
                    "entitlement_policy_revision",
                    operation.get("entitlementPolicyRevision"),
                ),
                required_tier=(
                    policy.get("required_tier")
                    or operation.get("requiredTier")
                    or (
                        "pr"
                        if (operation["surface"], operation["operation"])
                        in PR_SDK_SMOKE_OPERATIONS[source_name]
                        else "nightly"
                    )
                ),
            )
    if applied_sdk_operation_policies != set(sdk_operation_policies):
        raise ValueError(
            "SDK operation policies do not exactly match the pinned protocol contracts: "
            f"missing={sorted(set(sdk_operation_policies) - applied_sdk_operation_policies)}"
        )

    dotnet = load(SOURCES / "sdk-dotnet" / "sdk-certification.v1.json")
    dotnet_addressable_operations = sum(
        operation["status"] != "non-addressable" for operation in dotnet["operations"]
    )
    tier_order = ("pr", "nightly", "release")
    for operation in dotnet["operations"]:
        required_tier = next(
            (tier for tier in tier_order if tier in operation["requiredTiers"]), None
        )
        if operation["status"] == "non-addressable" or required_tier is None:
            continue
        facets = list(dict.fromkeys(
            "positive" if facet == "read-only" else facet
            for facet in operation["scenarioFacets"]
        ))
        add(
            capability=f"sdk-dotnet.{operation['surface']}", surface=operation["surface"],
            operation=operation["id"], client="Honua SDK .NET", lane="sdk-dotnet-certification",
            version="1.6.0", contract=f"sdk-dotnet-certification@{revisions['sdk-dotnet']['commit']}",
            auth_policy="api-key-protected-v1",
            fixture="sha256:1165029a4c750c38a9b180f79f560dea41b84d8f0725618c9d96d3864be2d419",
            facets=facets, required_tier=required_tier,
        )

    grpc = load(SOURCES / "geospatial-grpc" / "operations.v1.json")
    grpc_fixture = (
        f"geospatial-grpc-conformance@{grpc['fixture_version']}+{grpc.get('fixture_source_sha', grpc['source_sha'])}"
    )
    grpc_published = grpc.get("published_clients") or {}
    grpc_clients = (
        ("Generated gRPC .NET client", "grpc-dotnet"),
        ("Generated gRPC Python client", "grpc-python"),
        ("Generated gRPC TypeScript client", "grpc-typescript"),
    )
    grpc_in_scope, grpc_excluded = grpc_scope(grpc, revisions["server"]["commit"])
    for rpc in grpc_in_scope:
        operation = f"{rpc['service']}/{rpc['operation']}"
        for client, lane in grpc_clients:
            add(
                capability=f"grpc.{slug(rpc['service'])}", surface="grpc", operation=operation,
                client=client, lane=lane, version=grpc_client_version(grpc_published, lane),
                contract=f"geospatial-grpc@{grpc['source_sha']}", auth_policy="anonymous-public-v1",
                fixture=grpc_fixture,
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
                contract=f"geospatial-mcp@{mcp['source_sha']}", auth_policy="anonymous-public-v1",
                fixture=mcp["fixture_version"], facets=["positive", "negative", "media-schema"],
            )

    server = load(SOURCES / "server" / "capability-matrix.v1.json")
    lane_clients = {
        "desktop-qgis": ("QGIS", QGIS_VERSION),
        "desktop-arcgis": (PRO["name"], PRO["version"]),
        "ci-desktop": ("QGIS", QGIS_VERSION),
        "js": ("Honua SDK JavaScript", "0.1.9-beta.0"),
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
                auth_policy="anonymous-public-v1",
                facets=["positive", "negative", "crs-axis", "media-schema"],
            )
        for interop in capability.get("interop", []):
            lane = interop["clientLane"]
            # R40: the coarse interop lane for the licensed desktop client is the scripting
            # matrix plus the UI grid, so it is not emitted again here.
            if lane.startswith("desktop-") and lane.endswith("gis") and "qgis" not in lane:
                continue
            client, version = lane_clients.get(lane, (lane, f"pin@{revisions['server']['commit'][:12]}"))
            add(
                capability=capability["key"], surface=interop["protocol"], operation=capability["key"],
                client=client, lane=lane, version=version,
                contract=f"server-capability-matrix@{revisions['server']['commit']}",
                auth_policy="anonymous-public-v1",
            )

    assignments = load(SOURCES / "canonical-client-assignments.v1.json")
    server_by_key = {capability["key"]: capability for capability in server["capabilities"]}
    for assignment in assignments["assignments"]:
        capability = server_by_key.get(assignment["capability_key"])
        if not capability or not capability.get("maturity", {}).get("implemented"):
            continue
        for client_id in assignment["clients"]:
            client = assignments["clients"][client_id]
            version = client["version"].replace("{server_sha}", revisions["server"]["commit"])
            add(
                capability=assignment["capability_key"], surface=assignment["surface"],
                operation=assignment["capability_key"], client=client["name"],
                lane=f"{client['lane']}-{slug(assignment['surface'])}", version=version,
                contract=f"canonical-client-assignments@{assignments['revision']}",
                auth_policy="anonymous-and-protected-v1",
                facets=assignment["scenario_facets"],
            )

    protocol_harness = load(SOURCES / "server" / "protocol-harness-assignments.v1.json")
    harness_source_sha = revisions["server-certification"]["commit"]
    harness_contract = (
        f"server-protocol-harness@{protocol_harness['revision']}+{harness_source_sha}"
    )
    harness_capabilities = {
        assignment["capability_key"] for assignment in protocol_harness["assignments"]
    }
    for assignment in protocol_harness["assignments"]:
        capability = server_by_key.get(assignment["capability_key"])
        if not capability or not capability.get("maturity", {}).get("implemented"):
            continue
        add(
            capability=assignment["capability_key"], surface=assignment["surface"],
            operation=assignment["operation"], client=protocol_harness["canonical_client"],
            lane=protocol_harness["client_lane"], version=f"source@{harness_source_sha}",
            contract=harness_contract,
            auth_policy=protocol_harness["auth_policy_revision"],
            target=protocol_harness["deployment_target"],
            fixture=f"server-test-fixtures@{harness_source_sha}",
            facets=assignment["scenario_facets"],
            required_tier=protocol_harness["required_tier"],
            test_ids=assignment["test_ids"],
        )

    sdk_protocols = load(SOURCES / "official-sdk-protocol-assignments.v1.json")
    for capability_key in sdk_protocols["capabilities"]:
        capability = server_by_key.get(capability_key)
        if not capability or not capability.get("maturity", {}).get("implemented"):
            continue
        if capability_key not in harness_capabilities and not any(
            sdk_capability_operations.get((client["source"], capability_key))
            for client in sdk_protocols["clients"]
        ):
            add(
                capability=capability_key, surface=slug(capability_key),
                operation=f"UNASSIGNED PROTOCOL HARNESS CONTRACT:{capability_key}",
                client="UNASSIGNED PROTOCOL HARNESS",
                lane=f"protocol-harness-gap-{slug(capability_key)}", version="policy-v1",
                contract=f"official-sdk-protocol-assignments@{sdk_protocols['revision']}",
                auth_policy="unassigned-protocol-harness-v1", facets=["positive"],
            )

    applicability = load(SOURCES / "canonical-client-applicability.v1.json")
    allowed_classifications = {
        "official-sdk-required",
        "canonical-external-required",
        "not-client-addressable",
    }
    decision_keys: set[str] = set()
    for decision in applicability["decisions"]:
        capability_key = decision["capability_key"]
        classification = decision["classification"]
        if capability_key in decision_keys:
            raise ValueError(f"duplicate canonical-client applicability decision: {capability_key}")
        decision_keys.add(capability_key)
        if classification not in allowed_classifications:
            raise ValueError(
                f"unknown canonical-client applicability classification for {capability_key}: "
                f"{classification}"
            )
        capability = server_by_key.get(capability_key)
        if not capability or not capability.get("maturity", {}).get("implemented"):
            continue
        contract = f"canonical-client-applicability@{applicability['revision']}"
        if classification == "official-sdk-required":
            if capability_key not in harness_capabilities and not any(
                sdk_capability_operations.get((client["source"], capability_key))
                for client in sdk_protocols["clients"]
            ):
                add(
                    capability=capability_key, surface=slug(capability_key),
                    operation=f"UNASSIGNED PROTOCOL HARNESS CONTRACT:{capability_key}",
                    client="UNASSIGNED PROTOCOL HARNESS",
                    lane=f"protocol-harness-gap-{slug(capability_key)}",
                    version="policy-v1", contract=contract,
                    auth_policy="unassigned-protocol-harness-v1", facets=["positive"],
                )
            continue
        elif classification == "canonical-external-required":
            client_ids = decision.get("clients", [])
            if not client_ids:
                raise ValueError(f"canonical external decision has no clients: {capability_key}")
            unknown_clients = set(client_ids) - set(applicability["clients"])
            if unknown_clients:
                raise ValueError(
                    f"canonical external decision has unknown clients for {capability_key}: "
                    f"{sorted(unknown_clients)}"
                )
            clients = [applicability["clients"][client_id] for client_id in client_ids]
            facets = decision["scenario_facets"]
        else:
            reason = decision.get("reason")
            if not reason:
                raise ValueError(f"non-client-addressable decision has no reason: {capability_key}")
            add(
                capability=capability_key, surface=slug(capability_key),
                operation=capability_key, client="NOT CLIENT ADDRESSABLE",
                lane=f"not-client-addressable-{slug(capability_key)}", version="policy-v1",
                contract=contract, auth_policy="not-client-addressable-v1",
                facets=["not-client-addressable"], addressable=False,
                addressability_reason=reason,
            )
            continue
        for client in clients:
            add(
                capability=capability_key, surface=slug(capability_key),
                operation=capability_key, client=client["name"],
                lane=f"{client['lane']}-{slug(capability_key)}", version=client["version"],
                contract=contract, auth_policy=client["auth_policy_revision"], facets=facets,
            )

    esri_index = load(SOURCES / "esri-compat" / "matrix" / "index.json")
    esri_clients = [
        ("ArcGIS REST protocol client", "11.3", "raw-geoservices", "local-docker", False, None, None),
        ("ArcGIS API for Python", "2.4", "arcgis-python", "local-docker", False, None, None),
        ("ArcGIS Maps SDK for .NET", "200.8", "esri-dotnet", "windows", False, None, None),
        ("ArcGIS Pro/arcpy", PRO["version"], "desktop-arcpy", "windows-licensed", True, "esri-arcgis-pro-arcpy-v1", MUST_FIX),
    ]
    # The licensed scripting client is the tuple whose label carries a slash. Its public canonical
    # name is the product label; the driver id is that label's suffix.
    scripting_canonical = PRO["name"]
    scripting_driver = scripting_lane = scripting_entitlement = None
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
            for client, version, lane, target, licensed, entitlement_policy, bucket in esri_clients:
                scripting = "/" in client
                if scripting and scripting_driver is None:
                    scripting_lane = lane
                    scripting_entitlement = entitlement_policy
                    scripting_driver = client.rsplit("/", 1)[-1]
                add(
                    capability=f"esri.{service['service']}", surface=service["service"], operation=case["id"],
                    client=scripting_canonical if scripting else client,
                    lane=f"{lane}-{service['service']}", version=version,
                    contract=f"esri-matrix@{revisions['esri-compat']['commit']}",
                    auth_policy="anonymous-and-protected-v1", target=target,
                    licensed=licensed, entitlement_policy=entitlement_policy,
                    # The licensed scripting driver replaces the coarse desktop interop rows, which
                    # were the licensed client's only metadata coverage; its metadata cases keep it.
                    facets=facets + ["metadata"] if scripting and "metadata" in case["name"].lower() else facets,
                    release_bucket=bucket,
                    client_driver=scripting_driver if scripting else None,
                )
    if scripting_driver is None or scripting_lane is None or scripting_entitlement is None:
        raise ValueError("Licensed scripting client tuple was not generated.")
    driver_order = (scripting_driver, "pyqgis", "pro-ui", "qgis-ui")

    ogc = load(SOURCES / "esri-compat" / "matrix" / "ogc.matrix.json")
    ogc_contract = f"esri-ogc-matrix@{revisions['esri-compat']['commit']}"
    for case in ogc["cases"]:
        if case.get("status") not in SUPPORTED:
            continue
        name = case["name"].lower()
        if "features" in name or "wfs" in name:
            clients = [("OGC CITE", f"ets-selection@{revisions['server']['commit']}", "cite"), ("GDAL/OGR", "3.8.4", "gdal"), ("QGIS", QGIS_VERSION, "qgis")]
        elif "tiles" in name or "wmts" in name or "wms" in name:
            clients = [("OGC CITE", f"ets-selection@{revisions['server']['commit']}", "cite"), ("QGIS", QGIS_VERSION, "qgis"), ("MapLibre GL JS", "5.7", "maplibre")]
        elif "wcs" in name or "coverage" in name:
            clients = [("OGC CITE", f"ets-selection@{revisions['server']['commit']}", "cite"), ("GDAL", "3.8.4", "gdal"), ("OWSLib", "0.36.0", "owslib")]
        else:
            clients = [("OGC CITE", f"ets-selection@{revisions['server']['commit']}", "cite"), ("Honua SDK Python", f"source-preview@{revisions['sdk-python']['commit']}", "sdk-python")]
        for client, version, lane in clients:
            add(
                capability="serve.ogc", surface="ogc", operation=case["id"], client=client,
                lane=f"{lane}-ogc", version=version,
                contract=ogc_contract,
                auth_policy="anonymous-and-protected-v1",
                facets=["positive", "negative", "auth", "crs-axis", "media-schema"],
            )

    # R40: scripting drivers take every applicable OGC function, GeoServices and OGC alike.
    # The licensed scripting driver gets each supported OGC matrix case plus each implemented
    # OGC surface. pyqgis keeps the roster-bound QGIS rows (stamped below) and the matrix cases
    # that roster does not already cover. Its surfaces are the function grid emitted below, not
    # one more row per surface. Preview capabilities stay off the QGIS canonical client.
    desktop_contract = f"desktop-client-certification@{DESKTOP['revision']}"
    ogc_facets = ["positive", "negative", "auth", "crs-axis", "media-schema"]
    qgis_operations = {
        row["operation"] for row in requirements if row["canonical_client"] == QGIS["name"]
    }
    for case in ogc["cases"]:
        if case.get("status") not in SUPPORTED:
            continue
        add(
            capability="serve.ogc", surface="ogc", operation=case["id"],
            client=scripting_canonical, lane=f"{scripting_lane}-ogc",
            version=PRO["version"], contract=ogc_contract,
            auth_policy=PRO["auth_policy_revision"], facets=ogc_facets,
            **{key: value for key, value in licensed_desktop(
                MUST_FIX, scripting_driver, entitlement=scripting_entitlement,
            ).items() if key != "auth_policy"},
        )
        if case["id"] in qgis_operations:
            continue
        add(
            capability="serve.ogc", surface="ogc", operation=case["id"],
            client=DESKTOP_DRIVERS["pyqgis"]["canonical_client"],
            lane=DESKTOP_DRIVERS["pyqgis"]["lane"], version=QGIS_VERSION,
            contract=ogc_contract,
            auth_policy=QGIS["auth_policy_revision"], target=QGIS["deployment_target"],
            facets=ogc_facets, release_bucket=MUST_FIX, client_driver="pyqgis",
        )
    for assignment in DESKTOP["ogc_assignments"]:
        capability = server_by_key.get(assignment["capability_key"])
        if not capability or not capability.get("maturity", {}).get("implemented"):
            continue
        add(
            capability=assignment["capability_key"], surface=assignment["surface"],
            operation=assignment["capability_key"],
            client=scripting_canonical, lane=f"{scripting_lane}-ogc",
            version=PRO["version"], contract=desktop_contract, facets=assignment["scenario_facets"],
            **licensed_desktop(MUST_FIX, scripting_driver, entitlement=scripting_entitlement),
        )
    emit_pyqgis_grid(add, preview_capabilities=preview_capabilities)

    ui = DESKTOP["ui"]
    for surface in ui["surfaces"]:
        functions = ui_functions(surface)
        unknown = (set(surface["pro_core"]) | set(surface["qgis_core"])) - set(functions)
        if unknown:
            raise ValueError(f"UI surface {surface['surface']} cores name functions it does not generate: {sorted(unknown)}")
        facets = ui["facets"][surface["family"]]
        for function in functions:
            add(
                capability=surface["capability_key"], surface=surface["surface"], operation=function,
                client=DESKTOP_DRIVERS["pro-ui"]["canonical_client"],
                lane=DESKTOP_DRIVERS["pro-ui"]["lane"], version=PRO["version"],
                contract=desktop_contract, facets=facets,
                **licensed_desktop(
                    MUST_FIX if function in surface["pro_core"] else PROVE, "pro-ui",
                ),
            )
            add(
                capability=surface["capability_key"], surface=surface["surface"], operation=function,
                client=DESKTOP_DRIVERS["qgis-ui"]["canonical_client"],
                lane=DESKTOP_DRIVERS["qgis-ui"]["lane"], version=QGIS_VERSION,
                contract=desktop_contract, auth_policy=QGIS["auth_policy_revision"],
                target=QGIS["deployment_target"], facets=facets,
                release_bucket=MUST_FIX if function in surface["qgis_core"] else PROVE,
                client_driver="qgis-ui",
            )
    for mode in ui["auth_modes"]:
        add(
            capability=ui["auth_capability"], surface=ui["auth_surface"], operation=mode,
            client=DESKTOP_DRIVERS["pro-ui"]["canonical_client"],
            lane=DESKTOP_DRIVERS["pro-ui"]["lane"], version=PRO["version"],
            contract=desktop_contract, facets=ui["facets"]["auth"],
            **licensed_desktop(MUST_FIX, "pro-ui"),
        )
        add(
            capability=ui["auth_capability"], surface=ui["auth_surface"], operation=mode,
            client=DESKTOP_DRIVERS["qgis-ui"]["canonical_client"],
            lane=DESKTOP_DRIVERS["qgis-ui"]["lane"], version=QGIS_VERSION,
            contract=desktop_contract, auth_policy=QGIS["auth_policy_revision"],
            target=QGIS["deployment_target"], facets=ui["facets"]["auth"],
            release_bucket=MUST_FIX, client_driver="qgis-ui",
        )

    unbound_cells = sorted((set(bounded_cells) | set(not_addressable_cells)) - bound_cells)
    if unbound_cells:
        raise ValueError(f"bounded-roster cells match no generated requirement: {unbound_cells}")

    # Roster binding rewrites QGIS lanes after add(), so the pyqgis stamp runs once the rows exist.
    # The licensed scripting rows already carry their driver; stamping the shared product label
    # here would overwrite the UI rows that use the same canonical client.
    pyqgis_clients = {QGIS["name"], DESKTOP_DRIVERS["pyqgis"]["canonical_client"]}
    for row in requirements:
        if row["canonical_client"] not in pyqgis_clients:
            continue
        row["client_driver"] = "pyqgis"
        row["release_bucket"] = MUST_FIX
    summary = desktop_driver_summary(requirements, driver_order)

    requirements.sort(key=lambda row: (
        row["capability_key"], row["surface"], row["operation"], row["canonical_client"], row["client_lane"]
    ))
    output = {
        "schema": "honua.protocol-certification-requirements/v1",
        "revision": "2026-10-04-complete.21",
        "trademarkNotice": DESKTOP["trademarkNotice"],
        "receipt_schema_min": "v2",
        "complete": True,
        "scope_notes": (
            "Complete supported denominator generated from pinned server capability/CITE/interop assignments, "
            "Esri operation matrices, SDK entrypoints, cloud-native canonical clients, generated gRPC "
            "clients, governed external-client assignments for every supported OGC/STAC/SensorThings surface, "
            "official MCP SDK/Inspector operations, explicit three-SDK parity cells for every supported protocol "
            "and every Honua-specific application capability, executable operation contracts for all three Honua "
            "SDKs, explicit fail-closed SDK operation-contract blockers where those contracts do not yet exist, "
            "pinned external harnesses for identity, operations, raster, BIM, and point-cloud capabilities, "
            "exact operation-to-test contracts for the server protocol integration harness, "
            "and one governed test ID per bounded 2026.1 external-client cell. "
            f"The gRPC scope ruling ({GRPC_SCOPE_RULING}, geospatial-grpc#88, honua-release#376) keeps "
            f"{len(grpc_in_scope)} RPCs the default honua-server image implements, for "
            f"{len(grpc_in_scope) * len(grpc_clients)} generated-client cells. It excludes "
            f"{len(grpc_excluded)} RPCs ({len(grpc_excluded) * len(grpc_clients)} cells) as Preview or "
            "Experimental, each listed with its rationale and owner issue in "
            "sources/geospatial-grpc/operations.v1.json. "
            f"{len(preview_cells)} bounded cells on Preview surfaces are excluded by the preview-surfaces "
            f"roster ruling and {len(not_addressable_cells)} cells no released client can exercise remain "
            "non-addressable rows (release#351, release#359). "
            f"The .NET contract contributes {dotnet_addressable_operations} addressable operations; "
            "18 explicitly non-addressable public abstractions "
            "remain documented in its pinned source contract and excluded from client certification. "
            f"Desktop clients are certified against release lines (R38): QGIS {QGIS_VERSION} and "
            f"{PRO['name']} {PRO['version']}; a receipt records the exact patch it observed. "
            "R40 splits those clients by driver. Scripting rows are must-fix-before-cut on every "
            "applicable GeoServices and OGC cell. PyQGIS is every server-declared surface crossed with "
            "every function that client can perform (connect/discover, add, render, identify, "
            "query/filter, edit, raster read, save/reopen, and the auth modes). A function PyQGIS "
            "has no client for is not-applicable, and the reason is declared on the row. "
            "pro-ui and qgis-ui are must-fix-before-cut only on "
            "the core set recorded in the desktop source (FeatureServer, MapServer, ImageServer render, "
            "connect, add layer, render, identify, query, FeatureServer edit, and the auth modes; "
            "QGIS UI also WMS, WFS, WCS and WFS-T edit). Every other UI cell is "
            "prove-against-candidate. Counts per bucket per driver are in desktop_driver_summary. "
            "Roadmap Kerchunk and COPC capabilities remain excluded until promoted to supported."
        ),
        "source_revisions": revisions,
        "desktop_driver_summary": summary,
        "production": production(requirements, revisions),
        "requirements": requirements,
    }
    OUTPUT.write_text(json.dumps(output, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT} with {len(requirements)} required certification cells.")


if __name__ == "__main__":
    main()
