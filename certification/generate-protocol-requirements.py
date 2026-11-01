#!/usr/bin/env python3
"""Generate the complete protocol/client certification denominator."""

from __future__ import annotations

import fnmatch
import base64
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
OUTPUT = ROOT / "protocol-certification-requirements.v1.json"
SOURCES = ROOT / "sources"
SUPPORTED = {"implemented", "partial", "covered"}
FIXTURE = "docker/cng/seed.sql@{source_sha}"
# These are the actual fixture paths exercised by the pinned server CITE
# normalizer. Unknown suites retain the existing default and refuse admission.
CITE_FIXTURES = {
    "OGC API Features 1.0": "docker/cite/ogc-api-features/seed.sql",
    "OGC API Tiles 1.0": "docker/cite/ogc-api-tiles/seed.sql",
    "WFS 1.0": "docker/cite/shared/test-data",
    "WFS 1.1": "docker/cite/shared/test-data",
    "WFS 2.0": "docker/cite/shared/test-data",
    "WCS 2.0": "docker/cite/wcs20/seed.sql",
    "WMS 1.3": "docker/cite/shared/seed/mapserver.sql",
    "WMTS 1.0": "docker/cite/shared/seed/mapserver.sql",
    "WPS 2.0": "docker/cite/wps20/crs84-echo.xml",
}

IDENTITY_FIELDS = ("surface", "operation", "canonical_client", "client_version", "deployment_target")
# QGIS desktop checks use the declared release line, <major>.<minor>.x.
DESKTOP = json.loads((SOURCES / "desktop-client-certification.v1.json").read_text(encoding="utf-8"))
QGIS = DESKTOP["clients"]["qgis"]
QGIS_VERSION = QGIS["version"]
DESKTOP_DRIVERS = DESKTOP["drivers"]
MUST_FIX = DESKTOP["buckets"]["must_fix"]
PROVE = DESKTOP["buckets"]["prove"]


def python_wheel_identity(manifest: dict, version: str, source_sha: str) -> dict:
    """Only independently selected published bytes can govern a Python receipt."""
    if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
        raise ValueError("selected manifest requires an exact Python source SHA")
    component = manifest.get("components", {}).get("honua-sdk-python", {})
    artifact = manifest.get("clientArtifacts", {}).get("honua-sdk-python-wheel", {})
    if (component.get("sha") != source_sha or component.get("version") != version
            or artifact.get("sourceSha") != source_sha or artifact.get("version") != version
            or artifact.get("repository") != "honua-io/honua-sdk-python"
            or artifact.get("package") != "honua-sdk" or artifact.get("ecosystem") != "pypi"
            or artifact.get("publicationState") != "published"
            or artifact.get("filename") != f"honua_sdk-{version}-py3-none-any.whl"
            or not isinstance(artifact.get("digest"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact["digest"])):
        raise ValueError("selected manifest does not bind a coherent published Python SDK wheel")
    return {"filename": artifact["filename"], "sha256": artifact["digest"][7:], "source": "pypi"}


def selected_python_wheel(revisions: dict) -> dict | None:
    # Historical mixed cohorts remain unchanged and cannot qualify executed
    # Python rows. The existing rebind refuses their component/artifact split.
    # Only a coherent future selection adds governed bytes during regeneration.
    import yaml
    manifest = yaml.safe_load((ROOT.parent / "platform-manifest.yaml").read_text(encoding="utf-8"))
    source = revisions["sdk-python"]["commit"]
    component = manifest.get("components", {}).get("honua-sdk-python", {})
    artifact = manifest.get("clientArtifacts", {}).get("honua-sdk-python-wheel", {})
    if component.get("sha") != source or artifact.get("sourceSha") != source:
        return None
    return python_wheel_identity(manifest, source_sdk_version("sdk-python"), source)


def source_sdk_version(source: str) -> str:
    """Read the declared version at the catalog's pinned source, never registry latest.

    .NET's coverage snapshot has no version; its release-please manifest is
    vendored from the same commit by convergence_rebind. Publication/source
    equality remains the rebind's separate registry-artifact prerequisite.
    """
    if source == "sdk-python":
        declaration = load(SOURCES / source / "protocol-certification.v1.json")
        if (declaration.get("canonicalClient") != "Honua SDK Python"
                or declaration.get("sourceRepository") != "honua-io/honua-sdk-python"):
            raise ValueError("pinned sdk-python contract has an unexpected client or repository")
        version = declaration.get("clientVersion")
    elif source == "sdk-dotnet":
        version = load(SOURCES / source / "release-please-manifest.json").get(".")
    else:
        raise ValueError(f"no source version declaration for {source}")
    numeric = r"(?:0|[1-9][0-9]*)"
    identifier = rf"(?:{numeric}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    semver = rf"{numeric}\.{numeric}\.{numeric}(?:-{identifier}(?:\.{identifier})*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    if not isinstance(version, str) or not re.fullmatch(semver, version):
        raise ValueError(f"pinned {source} must declare an exact SemVer version")
    return version


def sdk_js_version() -> str:
    """Bind every JS cell to the version in the two source-pinned declarations."""
    coverage = load(SOURCES / "sdk-js" / "sdk-coverage.v1.json")
    contract = load(SOURCES / "sdk-js" / "protocol-certification.v1.json")
    sdk = coverage.get("sdk") or {}
    version = sdk.get("version")
    # npm SemVer, including historical prereleases; reject coerced or partial versions.
    numeric = r"(?:0|[1-9][0-9]*)"
    identifier = rf"(?:{numeric}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    semver = rf"{numeric}\.{numeric}\.{numeric}(?:-{identifier}(?:\.{identifier})*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    if sdk.get("package") != "@honua/sdk-js" or not isinstance(version, str) or not re.fullmatch(semver, version):
        raise ValueError("pinned sdk-js coverage must declare @honua/sdk-js and an exact SemVer version")
    if contract.get("canonicalClient") != "@honua/sdk-js" or contract.get("clientVersion") != version:
        raise ValueError("pinned sdk-js coverage and protocol contract must declare the same client and version")
    return version
def mcp_clients() -> list[tuple[str, str, str]]:
    """Read literal manifests fetched from the selected producer; never execute it.

    Names/lanes remain release policy. Versions must agree across the producer's
    exact direct dependency declaration, lock root, and installed package entry.
    Nightly staging refreshes both snapshots at the same selected source SHA.
    """
    package = load(SOURCES / "geospatial-mcp" / "package.json")
    lock = load(SOURCES / "geospatial-mcp" / "package-lock.json")
    name = "geospatial-mcp-protocol-certification"
    if (package.get("name") != name or lock.get("name") != name
            or lock.get("lockfileVersion") != 3):
        raise ValueError("pinned MCP certification package/lock identity is invalid")
    packages = lock.get("packages") or {}
    root = packages.get("") or {}
    if root.get("name") != name:
        raise ValueError("pinned MCP lock root identity is invalid")
    clients = []
    for client, lane, dependency in (
        ("Official MCP TypeScript SDK", "mcp-typescript-sdk", "@modelcontextprotocol/sdk"),
        ("MCP Inspector", "mcp-inspector", "@modelcontextprotocol/inspector"),
    ):
        version = (package.get("dependencies") or {}).get(dependency)
        numeric = r"(?:0|[1-9][0-9]*)"
        identifier = rf"(?:{numeric}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
        semver = rf"{numeric}\.{numeric}\.{numeric}(?:-{identifier}(?:\.{identifier})*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
        entry = packages.get("node_modules/" + dependency) or {}
        if (not isinstance(version, str) or not re.fullmatch(semver, version)
                or (root.get("dependencies") or {}).get(dependency) != version
                or entry.get("version") != version
                or entry.get("resolved") != f"https://registry.npmjs.org/{dependency}/-/{dependency.rsplit('/', 1)[1]}-{version}.tgz"):
            raise ValueError(f"pinned MCP {dependency} must have matching exact package/lock versions and registry identity")
        integrity = entry.get("integrity")
        try:
            digest = (base64.b64decode(integrity.removeprefix("sha512-"), validate=True)
                      if isinstance(integrity, str) and integrity.startswith("sha512-") else b"")
        except ValueError:
            digest = b""
        if len(digest) != 64:
            raise ValueError(f"pinned MCP {dependency} requires SHA512 package integrity")
        clients.append((client, lane, version))
    return clients


def mcp_operation_auth(inventory: dict) -> dict[str, str]:
    """Require an explicit authorization decision for every selected MCP operation.

    This is release-owned policy, not a change to the producer snapshots. The
    selected producer must actually implement the new auth facet before its
    receipts can satisfy the regenerated requirements.
    """
    policy = load(SOURCES / "mcp-operation-auth-policy.v1.json")
    if (policy.get("schema") != "honua.mcp-operation-auth-policy/v1"
            or policy.get("revision") != "mcp-operation-auth-v1"):
        raise ValueError("MCP authorization policy schema/revision is invalid")
    expected_profiles = {
        "anonymous-public-v1": ["anonymous-success", "fresh-anonymous-session", "no-credentials"],
        "mcp-protected-operation-v1": [
            "authenticated-success", "fresh-anonymous-refusal", "no-credential-forwarding",
            "no-authenticated-session-reuse", "caller-session-binding",
        ],
    }
    if policy.get("required_observations") != expected_profiles:
        raise ValueError("MCP authorization policy must preserve all required observations")
    operations = {}
    for entry in inventory["operations"]:
        operation, kind = entry["operation"], entry["kind"]
        valid_kind = (
            kind == "transport" and operation in {"initialize", "ping", "tools/list", "resources/templates/list"}
            or kind == "tool" and isinstance(operation, str) and operation.startswith("tools/call:")
            or kind == "resource" and isinstance(operation, str) and operation.startswith("resources/read:")
        )
        if not valid_kind:
            raise ValueError(f"MCP authorization policy has unknown operation kind or method: {kind}/{operation}")
        if operation in operations and operations[operation] != kind:
            raise ValueError(f"MCP authorization inventory has conflicting kinds: {operation}")
        operations[operation] = kind
    decisions = policy.get("operations")
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("MCP authorization policy requires explicit operation decisions")
    selected = {}
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) != {"operation", "kind", "auth_policy_revision", "rationale"}:
            raise ValueError("MCP authorization decision has unknown or missing fields")
        operation = decision["operation"]
        if not isinstance(operation, str) or operation not in operations or operation in selected:
            raise ValueError(f"MCP authorization decision is unknown or duplicated: {operation}")
        kind = operations[operation]
        expected = "anonymous-public-v1" if kind == "transport" else "mcp-protected-operation-v1"
        if (decision["kind"] != kind or decision["auth_policy_revision"] != expected
                or not isinstance(decision["rationale"], str) or not decision["rationale"].strip()):
            raise ValueError(f"MCP authorization decision contradicts the server contract: {operation}")
        selected[operation] = expected
    if set(selected) != set(operations):
        raise ValueError(f"MCP authorization decisions are missing: {sorted(set(operations) - set(selected))}")
    return selected


def protocol_clients() -> list[tuple]:
    """Authoritative protocol client declarations, shared by generation and validation."""
    return [
        ("GeoServices REST protocol client", "11.3", "raw-geoservices", "local-docker", False, None, None),
        ("Python GIS client", "2.4", "python-gis", "local-docker", False, None, None),
        (".NET GIS client", "200.8", "dotnet-gis", "windows", False, None, None),
    ]


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
    grpc_client_digest(published, lane)
    return version


def grpc_client_digest(published: dict[str, Any], lane: str) -> str:
    """Exact public archive identity, preserving npm's canonical SHA-512 SRI."""
    client = published.get(lane, {})
    value = client.get("integrity") if lane == "grpc-typescript" else client.get("digest")
    if lane == "grpc-typescript":
        import base64
        try:
            raw = base64.b64decode(value.removeprefix("sha512-"), validate=True) if isinstance(value, str) and value.startswith("sha512-") else b""
        except ValueError:
            raw = b""
        valid = len(raw) == 64 and value == "sha512-" + base64.b64encode(raw).decode()
    else:
        valid = isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None
    if not valid:
        raise ValueError(f"geospatial-grpc published_clients[{lane}] needs the exact published artifact digest")
    return value


PRODUCTION_PLACEHOLDERS = (
    "{server_image}", "{server_sha}", "{cut_at}", "{requirements_ref}", "{requirements_revision}",
    # sdk-harness-trunk-pinned-package-2026-10-27: only a producer with a harness_revision_key.
    "{sdk_package}", "{sdk_package_version}", "{sdk_package_digest}", "{sdk_source_sha}",
)
SDK_PACKAGE_PLACEHOLDERS = ("{sdk_package}", "{sdk_package_version}", "{sdk_package_digest}", "{sdk_source_sha}")


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
            harness = entry.get("harness_revision_key")
            if harness is not None and (harness not in revisions
                                        or revisions[harness]["repository"] != entry["repository"]):
                raise ValueError(f"producer {name} harness_revision_key {harness!r} is not a revision of its repository")
            if (harness is None) != (not set(SDK_PACKAGE_PLACEHOLDERS) & set(entry["inputs"].values())):
                raise ValueError(f"producer {name} must pair a harness_revision_key with the pinned package inputs")
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


def pinned_python_fixture(contract: dict) -> str:
    """Coverage and certification rows for this client share one pinned fixture."""
    fixture = contract.get("fixtureRevision")
    if not isinstance(fixture, str) or not fixture.strip():
        raise ValueError("pinned sdk-python protocol contract is missing fixtureRevision")
    return fixture


def server_seed_fixture(fixtures: dict, revisions: dict) -> str:
    """The applied server seed is pinned with the server-certification source, never apart from it."""
    seed = fixtures.get("server_seed")
    if not isinstance(seed, dict):
        raise ValueError("canonical-client-fixtures is missing server_seed")
    source = seed.get("source")
    if source != "server-certification" or seed.get("path") != "tests/seed/base-schema.sql":
        raise ValueError("server_seed must name server-certification tests/seed/base-schema.sql")
    if seed.get("repository") != revisions[source]["repository"]:
        raise ValueError("server_seed repository differs from the server-certification source")
    if seed.get("commit") != revisions[source]["commit"]:
        raise ValueError(
            f"server_seed is pinned at {seed.get('commit')!r}, but server-certification is "
            f"{revisions[source]['commit']!r}; re-pin the seed digest in the same rebind"
        )
    digest = seed.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("server_seed sha256 must be sha256:<64 lowercase hex>")
    return digest


def dotnet_release_profile(contract: dict) -> dict[str, int]:
    """releaseProfile.excluded and each operation's releaseDenominator are one decision."""
    profile = contract.get("releaseProfile")
    if not isinstance(profile, dict):
        raise ValueError("sdk-dotnet certification is missing releaseProfile")
    denominator = profile.get("denominator")
    excluded = profile.get("excluded")
    if isinstance(denominator, bool) or not isinstance(denominator, int) or denominator < 0:
        raise ValueError("sdk-dotnet releaseProfile.denominator must be a non-negative integer")
    if not isinstance(excluded, list):
        raise ValueError("sdk-dotnet releaseProfile.excluded must be a list")
    operations = contract.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("sdk-dotnet certification has no operations")
    by_id: dict[str, dict] = {}
    for operation in operations:
        operation_id = operation.get("id")
        if not isinstance(operation_id, str) or not operation_id or operation_id in by_id:
            raise ValueError(f"sdk-dotnet certification repeats or omits operation id {operation_id!r}")
        by_id[operation_id] = operation
    excluded_ids: list[str] = []
    for entry in excluded:
        if not isinstance(entry, dict):
            raise ValueError("sdk-dotnet releaseProfile exclusion must name an operation")
        operation_id = entry.get("operationId")
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("sdk-dotnet releaseProfile exclusion is missing operationId")
        if not isinstance(entry.get("ownerIssue"), str) or not entry["ownerIssue"].strip():
            raise ValueError(f"sdk-dotnet release exclusion {operation_id} is missing ownerIssue")
        if not isinstance(entry.get("reason"), str) or not entry["reason"].strip():
            raise ValueError(f"sdk-dotnet release exclusion {operation_id} is missing reason")
        excluded_ids.append(operation_id)
    if len(excluded_ids) != len(set(excluded_ids)):
        raise ValueError("sdk-dotnet releaseProfile.excluded repeats an operationId")
    unknown = sorted(set(excluded_ids) - set(by_id))
    if unknown:
        raise ValueError(f"sdk-dotnet releaseProfile excludes unknown operations: {unknown}")
    excluded_set = set(excluded_ids)
    included = non_addressable = 0
    for operation in operations:
        operation_id = operation["id"]
        denom = operation.get("releaseDenominator")
        if denom not in {"included", "excluded", "non-addressable"}:
            raise ValueError(f"sdk-dotnet operation {operation_id} has releaseDenominator {denom!r}")
        if (operation_id in excluded_set) != (denom == "excluded"):
            raise ValueError(
                f"sdk-dotnet operation {operation_id} releaseDenominator {denom!r} "
                "disagrees with releaseProfile.excluded"
            )
        status = operation.get("status")
        if (status == "non-addressable") != (denom == "non-addressable"):
            raise ValueError(
                f"sdk-dotnet operation {operation_id} status {status!r} "
                f"disagrees with releaseDenominator {denom!r}"
            )
        if denom == "included":
            included += 1
        elif denom == "non-addressable":
            non_addressable += 1
    if included != denominator:
        raise ValueError(
            f"sdk-dotnet releaseProfile.denominator is {denominator}, but {included} operations are included"
        )
    return {"denominator": denominator, "excluded": len(excluded_ids), "non_addressable": non_addressable}


GP_PREVIEW_FLOOR_RULING = "gp-preview-floor-2026-10-26"


def gp_entry_selects(entry: dict, row: dict) -> bool:
    if row["capability_key"] != entry["capability_key"] or row["surface"] != entry["surface"]:
        return False
    if entry["operations"] != "*" and row["operation"] not in entry["operations"]:
        return False
    return "lanes" not in entry or row["client_lane"] in entry["lanes"]


def apply_gp_preview_floor(requirements: list[dict[str, Any]]) -> dict[str, int]:
    """Label every GP catalog row Preview except its explicitly classified safety floor.

    Founder decision D8 (honua-release#949): the GP catalog is Preview for 2026.1, but rows whose
    failure is a security or correctness bug stay supported and required. Every GP row must match
    exactly one floor or preview entry, and every entry must match a row, so a new GP row fails
    generation until it is classified; nothing on another surface is touched.
    """
    ruling = load(SOURCES / "gp-preview-floor.v1.json")
    if ruling.get("schema") != "honua.gp-preview-floor-ruling/v1" or ruling.get("id") != GP_PREVIEW_FLOOR_RULING:
        raise ValueError(f"GP preview ruling must be schema honua.gp-preview-floor-ruling/v1 id {GP_PREVIEW_FLOOR_RULING}")
    keys = set(ruling["capability_keys"])
    preview_row = ruling["preview_row"]
    if (preview_row.get("maturity"), preview_row.get("required_tier")) != ("preview", "release"):
        raise ValueError("GP preview rows must be maturity=preview, required_tier=release (required at no tier)")
    entries = [("floor", entry) for entry in ruling["floor"]] + [("preview", entry) for entry in ruling["preview"]]
    for _, entry in entries:
        if entry["capability_key"] not in keys or not entry.get("reason"):
            raise ValueError(f"GP preview ruling entry is outside the ruling or has no reason: {entry}")
    used: set[int] = set()
    counts = {"floor": 0, "preview": 0}
    for row in requirements:
        if row["capability_key"] not in keys:
            continue
        matches = [(index, kind) for index, (kind, entry) in enumerate(entries) if gp_entry_selects(entry, row)]
        if len(matches) != 1:
            raise ValueError(
                f"GP row {row['capability_key']}/{row['surface']}/{row['operation']}/{row['client_lane']} "
                f"matches {len(matches)} {GP_PREVIEW_FLOOR_RULING} entries; classify it exactly once"
            )
        index, kind = matches[0]
        used.add(index)
        counts[kind] += 1
        if row["maturity"] != "supported":
            raise ValueError(f"GP row {row['surface']}/{row['operation']} is not supported before the ruling")
        if kind == "preview":
            row["maturity"] = preview_row["maturity"]
            row["required_tier"] = preview_row["required_tier"]
    unused = [entries[index][1] for index in range(len(entries)) if index not in used]
    if unused:
        raise ValueError(f"{GP_PREVIEW_FLOOR_RULING} entries match no generated row: {unused}")
    if not counts["floor"]:
        raise ValueError(f"{GP_PREVIEW_FLOOR_RULING} must keep a required GP safety floor")
    return counts


def main() -> None:
    revisions = load(SOURCES / "source-revisions.v1.json")["sources"]
    python_wheel = selected_python_wheel(revisions)
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
    fixture_doc = json.loads(
        (ROOT / "sources" / "canonical-client-fixtures.v1.json").read_text(encoding="utf-8")
    )
    fixture_pins = fixture_doc["fixtures"]
    # The .NET certification producer binds the seed it applied to the candidate
    # (honua-sdk-dotnet sdk-certification.yml), so the row carries that digest.
    dotnet_certification_fixture = server_seed_fixture(fixture_doc, revisions)
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
            client_driver: str | None = None,
            client_package_digest: str | None = None) -> None:
        # Every row is stamped maturity "supported". A capability the pinned matrix has not
        # implemented must not enter the release denominator, on any lane.
        recorded = server_by_key.get(capability)
        if recorded is not None and not recorded.get("maturity", {}).get("implemented"):
            return
        # The preview-surfaces ruling (release#351) covers every client driver (R40), not only
        # the bounded roster: an addressable driver row on a Preview capability would be a
        # supported requirement the release gate demands. PyQGIS keeps its not-applicable grid row.
        if client_driver is not None and addressable and capability in preview_capabilities:
            return
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
        if client_package_digest is not None:
            row["client_package_digest"] = client_package_digest
        if lane == "sdk-python-certification" and python_wheel is not None:
            row["client_package"] = dict(python_wheel)
        if test_ids is not None:
            row["test_ids"] = test_ids
        if release_bucket is not None:
            row["release_bucket"] = release_bucket
        if client_driver is not None:
            row["client_driver"] = client_driver
        bound = bind_bounded_cell(row)
        if bound is not None:
            requirements.append(bound)

    server = load(SOURCES / "server" / "capability-matrix.v1.json")
    server_by_key = {capability["key"]: capability for capability in server["capabilities"]}
    python_fixture = pinned_python_fixture(load(SOURCES / "sdk-python" / "protocol-certification.v1.json"))
    dotnet_version = source_sdk_version("sdk-dotnet")
    sdk_sources = [
        ("sdk-python", "capabilities", "Honua SDK Python", source_sdk_version("sdk-python"), "sdk-python", python_fixture),
        ("sdk-js", "capabilities", "@honua/sdk-js", sdk_js_version(), "sdk-js", fixture_pins["sdk-js"]),
        ("sdk-dotnet", "coverage", "Honua SDK .NET", dotnet_version, "sdk-dotnet", "sha256:83eb29ac38a3fb54914c1252b273dbb7f7f4d651a8204aafb4108d14d6d23727"),
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
                    else pinned_python_fixture(contract)
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
    dotnet_profile = dotnet_release_profile(dotnet)
    tier_order = ("pr", "nightly", "release")
    dotnet_rows_before = len(requirements)
    for operation in dotnet["operations"]:
        if operation["releaseDenominator"] != "included":
            continue
        required_tier = next(
            (tier for tier in tier_order if tier in operation["requiredTiers"]), None
        )
        if required_tier is None:
            raise ValueError(
                f"sdk-dotnet release operation {operation['id']} has no pr, nightly, or release tier"
            )
        facets = list(dict.fromkeys(
            "positive" if facet == "read-only" else facet
            for facet in operation["scenarioFacets"]
        ))
        add(
            capability=f"sdk-dotnet.{operation['surface']}", surface=operation["surface"],
            operation=operation["id"], client="Honua SDK .NET", lane="sdk-dotnet-certification",
            version=dotnet_version, contract=f"sdk-dotnet-certification@{revisions['sdk-dotnet']['commit']}",
            auth_policy="api-key-protected-v1",
            fixture=dotnet_certification_fixture,
            facets=facets, required_tier=required_tier,
        )
    dotnet_emitted = len(requirements) - dotnet_rows_before
    if dotnet_emitted != dotnet_profile["denominator"]:
        raise ValueError(
            f"sdk-dotnet release profile denominator is {dotnet_profile['denominator']}, "
            f"but {dotnet_emitted} certification rows were emitted"
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
                client_package_digest=grpc_client_digest(grpc_published, lane),
                contract=f"geospatial-grpc@{grpc['source_sha']}", auth_policy="anonymous-public-v1",
                fixture=grpc_fixture,
                facets=["positive", "negative", "media-schema"],
            )

    mcp = load(SOURCES / "geospatial-mcp" / "operations.v1.json")
    selected_mcp_clients = mcp_clients()
    selected_mcp_auth = mcp_operation_auth(mcp)
    for entry in mcp["operations"]:
        for client, lane, version in selected_mcp_clients:
            add(
                capability=f"mcp.{entry['kind']}", surface="mcp", operation=entry["operation"],
                client=client, lane=lane, version=version,
                contract=f"geospatial-mcp@{mcp['source_sha']}", auth_policy=selected_mcp_auth[entry["operation"]],
                fixture=mcp["fixture_version"], facets=["positive", "negative", "media-schema", "auth"],
            )

    lane_clients = {
        "desktop-qgis": ("QGIS", QGIS_VERSION),
        "ci-desktop": ("QGIS", QGIS_VERSION),
        "js": ("Honua SDK JavaScript", "0.1.9-beta.0"),
        "js-cesium": ("CesiumJS", "1.132.0"),
        "cli": ("Honua CLI", f"source@{revisions['server']['commit'][:12]}"),
        "geoservices-rest-stub": ("GeoServices REST contract client", "protocol-v1"),
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
                fixture=f"{CITE_FIXTURES[suite]}@{{source_sha}}" if suite in CITE_FIXTURES else FIXTURE,
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

    geoservices_index = load(SOURCES / "geoservices-rest" / "matrix" / "index.json")
    geoservices_clients = protocol_clients()
    # The public catalog records neutral protocol capability and client identifiers.
    for service in geoservices_index["services"]:
        matrix = load(SOURCES / "geoservices-rest" / "matrix" / service["manifest"])
        for case in matrix["cases"]:
            if case.get("status") not in SUPPORTED:
                continue
            if service["service"] == "ogc":
                continue
            facets = ["positive", "auth", "media-schema"]
            if "query" in case["name"].lower():
                facets += ["pagination", "limit", "crs-axis"]
            for client, version, lane, target, licensed, entitlement_policy, bucket in geoservices_clients:
                add(
                    capability=f"geoservices-rest.{service['service']}", surface=service["service"], operation=case["id"],
                    client=client,
                    lane=f"{lane}-{service['service']}", version=version,
                    contract=f"geoservices-rest-matrix@{revisions['geoservices-rest-matrix']['commit']}",
                    auth_policy="anonymous-and-protected-v1", target=target,
                    licensed=licensed, entitlement_policy=entitlement_policy,
                    facets=facets,
                    release_bucket=bucket,
                )
    driver_order = ("pyqgis", "qgis-ui")

    ogc = load(SOURCES / "geoservices-rest" / "matrix" / "ogc.matrix.json")
    ogc_contract = f"ogc-matrix@{revisions['geoservices-rest-matrix']['commit']}"
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

    desktop_contract = f"desktop-client-certification@{DESKTOP['revision']}"
    ogc_facets = ["positive", "negative", "auth", "crs-axis", "media-schema"]
    qgis_operations = {
        row["operation"] for row in requirements if row["canonical_client"] == QGIS["name"]
    }
    for case in ogc["cases"]:
        if case.get("status") not in SUPPORTED:
            continue
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
    emit_pyqgis_grid(add, preview_capabilities=preview_capabilities)

    ui = DESKTOP["ui"]
    for surface in ui["surfaces"]:
        functions = ui_functions(surface)
        unknown = set(surface["qgis_core"]) - set(functions)
        if unknown:
            raise ValueError(f"UI surface {surface['surface']} cores name functions it does not generate: {sorted(unknown)}")
        facets = ui["facets"][surface["family"]]
        for function in functions:
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
    pyqgis_clients = {QGIS["name"], DESKTOP_DRIVERS["pyqgis"]["canonical_client"]}
    for row in requirements:
        if row["canonical_client"] not in pyqgis_clients:
            continue
        row["client_driver"] = "pyqgis"
        row["release_bucket"] = MUST_FIX
    summary = desktop_driver_summary(requirements, driver_order)
    gp_counts = apply_gp_preview_floor(requirements)

    requirements.sort(key=lambda row: (
        row["capability_key"], row["surface"], row["operation"], row["canonical_client"], row["client_lane"]
    ))
    output = {
        "schema": "honua.protocol-certification-requirements/v1",
        "revision": "2026-10-26-complete.26",
        "receipt_schema_min": "v2",
        "complete": True,
        "scope_notes": (
            "Complete supported denominator generated from pinned server capability/CITE/interop assignments, "
            "GeoServices REST operation matrices, SDK entrypoints, cloud-native canonical clients, generated gRPC "
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
            f"The .NET release profile contributes {dotnet_profile['denominator']} operations "
            f"and excludes {dotnet_profile['excluded']} candidate-fixture operations; "
            f"{dotnet_profile['non_addressable']} explicitly non-addressable public abstractions "
            "remain documented in its pinned source contract and excluded from client certification. "
            f"QGIS desktop checks use release line {QGIS_VERSION}; a receipt records the exact patch it observed. "
            "PyQGIS is every implemented server-declared surface crossed with "
            "every function that client can perform (connect/discover, add, render, identify, "
            "query/filter, edit, raster read, save/reopen, and the auth modes). A function PyQGIS "
            "has no client for is not-applicable, and the reason is declared on the row. "
            "QGIS UI core functions are must-fix-before-cut. Every other UI cell is "
            "prove-against-candidate. Counts per bucket per driver are in desktop_driver_summary. "
            "Roadmap Kerchunk and COPC capabilities remain excluded until promoted to supported. "
            f"The GP Preview ruling ({GP_PREVIEW_FLOOR_RULING}, honua-release#949) labels "
            f"{gp_counts['preview']} GP catalog rows maturity=preview and required_tier=release, so they are "
            "reported at every tier and required at none. "
            f"{gp_counts['floor']} GP safety-floor rows (owner isolation, route-binding refusals, input "
            "validation, resource bounds and typed refusals) stay supported and required. Each row's "
            "classification and reason is in sources/gp-preview-floor.v1.json."
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
