#!/usr/bin/env python3
"""Validate the generated protocol certification requirements catalog."""

from __future__ import annotations

import fnmatch
import importlib.util
import json
import re
from pathlib import Path

import jsonschema
import yaml

ROOT = Path(__file__).parent
SUPPORTED = {"implemented", "partial", "covered"}
FIXTURE = "docker/cng/seed.sql@{source_sha}"


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def validate_geometry_client_addressability(catalog: dict) -> None:
    policy = json.loads((ROOT / "sources/geometry-service-client-addressability.v1.json").read_text(encoding="utf-8"))
    if (policy["schema"], policy["client_key"], policy["client_kind"], policy["surface"], policy["supported_operation_count"], policy["addressable_by_client"]) != ("honua.geometry-service-client-addressability/v1", "pro", "licensed-scripting", "geometry-service", 31, False):
        raise ValueError("GeometryServer policy must remain scoped to the governed licensed scripting client.")
    source = policy["source"]
    if (source["repository"] != "honua-io/honua-esri-compat" or source["commit"] != "7b275a11a7a4995dcd4dbfb90f21d9e57342eacc" or source["blob"] != "68f0fd206ee566682efebeb8d32ff4ac19dba66c" or "git-blob:" + source["blob"] not in policy["reason"]):
        raise ValueError("GeometryServer policy requires an exact private source commit/blob and matching evidence reference.")
    spec = importlib.util.spec_from_file_location("protocol_generator_config", ROOT / "generate-protocol-requirements.py")
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    declaration = config.licensed_scripting_client()
    lane = declaration[2]
    driver = lane.removeprefix("desktop-")
    if declaration[3] != config.DESKTOP["clients"][policy["client_key"]]["deployment_target"] or declaration[4] is not True:
        raise ValueError("Licensed scripting declaration must match the authoritative desktop manifest.")
    matrix = json.loads((ROOT / "sources/esri-compat/matrix/geometry-service.matrix.json").read_text(encoding="utf-8"))
    operations = {case["id"] for case in matrix["cases"] if case["status"] in SUPPORTED}
    rows = [row for row in catalog["requirements"] if row["client_lane"] == lane + "-geometry-service"]
    if len(operations) != policy["supported_operation_count"] or len(rows) != len(operations) or {row["operation"] for row in rows} != operations:
        raise ValueError("All original Licensed scripting GeometryServer operation records must remain explicit.")
    if any(row.get("client_driver") != driver or row["addressable_by_client"] or row["addressability_reason"] != policy["reason"]
           or row["scenario_facets"] != ["not-client-addressable"] for row in rows):
        raise ValueError("Licensed scripting GeometryServer operations must carry the documented non-addressability policy.")
    if any(row["addressability_reason"] == policy["reason"] for row in catalog["requirements"] if row not in rows):
        raise ValueError("Licensed scripting GeometryServer policy cannot apply to another client or surface.")
    for lane in (client[2] for client in config.protocol_clients() if not client[4]):
        other = [row for row in catalog["requirements"] if row["client_lane"] == lane + "-geometry-service"]
        if (len(other) != len(operations) or {row["operation"] for row in other} != operations
                or any(not row["addressable_by_client"] or row["addressability_reason"] is not None for row in other)):
            raise ValueError("Other GeometryServer clients must retain every addressable operation.")


def validate_bounded_roster(catalog: dict, roster: dict) -> None:
    """Every bounded 2026.1 external-client row must join one receipt result (release#346).

    The client-interop-cert-v1 normalizer narrows a receipt to (client_lane,
    client_version, surface) and rejects the whole receipt unless each result's
    test_case_id resolves to exactly one requirement there.
    """
    clients = roster["clients"]
    cells = roster["cells"]
    cell_keys = [(cell["canonical_client"], cell["surface"], cell["operation"]) for cell in cells]
    if len(cell_keys) != len(set(cell_keys)):
        raise ValueError("Bounded client roster contains duplicate cells.")
    for cell, key in zip(cells, cell_keys):
        client = clients.get(cell["canonical_client"])
        if client is None:
            raise ValueError(f"Bounded client roster cell names an unruled client: {key}")
        expected_test_id = f"client-cert/{slug(cell['canonical_client'])}/{cell['surface']}/{cell['operation']}"
        if cell["test_id"] != expected_test_id:
            raise ValueError(f"Bounded client roster cell {key} must use test ID {expected_test_id!r}.")
        allowed_lanes = {client["client_lane"], *client.get("retained_producer_lanes", {})}
        if cell["client_lane"] not in allowed_lanes:
            raise ValueError(
                f"Bounded client roster cell {key} uses lane {cell['client_lane']!r}; "
                f"the lane ruling allows {sorted(allowed_lanes)}."
            )
    preview_capabilities = set(next(
        ruling for ruling in roster["rulings"] if ruling["id"] == "preview-surfaces"
    )["preview_capability_keys"])
    preview_keys = {
        (cell["canonical_client"], cell["surface"], cell["operation"]): cell
        for cell in roster["preview_cells"]
    }
    not_addressable_keys = {
        (cell["canonical_client"], cell["surface"], cell["operation"]): cell
        for cell in roster["not_addressable_cells"]
    }
    dispositions = [*cell_keys, *preview_keys, *not_addressable_keys]
    if len(dispositions) != len(set(dispositions)):
        raise ValueError("Bounded client roster gives a cell more than one disposition.")
    governed_test_ids = {cell["test_id"] for cell in cells}
    for key, cell in preview_keys.items():
        if cell["capability_key"] not in preview_capabilities:
            raise ValueError(
                f"Bounded client roster preview cell {key} names non-Preview capability {cell['capability_key']!r}."
            )
        if not all(cell.get(field) for field in ("rationale", "decision", "target_release")):
            raise ValueError(f"Bounded client roster preview cell {key} needs rationale, decision and target_release.")
    for key, cell in not_addressable_keys.items():
        if not cell.get("addressability_reason") or cell.get("governed_by_test_id") not in governed_test_ids:
            raise ValueError(
                f"Bounded client roster non-addressable cell {key} needs a reason and a governed_by_test_id "
                "that names a governed cell."
            )
    rows: dict[tuple[str, str, str], list[dict]] = {}
    for row in catalog["requirements"]:
        if row["canonical_client"] in clients:
            rows.setdefault((row["canonical_client"], row["surface"], row["operation"]), []).append(row)
    previewed = sorted(set(rows) & set(preview_keys))
    if previewed:
        raise ValueError(f"Bounded client roster Preview cells carry generated requirements: {previewed}")
    for key, cell in not_addressable_keys.items():
        matched = rows.pop(key, [])
        if len(matched) != 1 or matched[0]["addressable_by_client"] is not False \
                or matched[0]["addressability_reason"] != cell["addressability_reason"] \
                or matched[0].get("test_ids") or matched[0]["client_lane"] != cell["client_lane"]:
            raise ValueError(
                f"Bounded client roster non-addressable cell {key} must match one non-addressable requirement "
                "with its reason, its lane and no test_ids."
            )
    if set(rows) != set(cell_keys):
        raise ValueError(
            "Bounded client roster differs from the generated bounded rows "
            f"(missing={sorted(set(rows) - set(cell_keys))}, unexpected={sorted(set(cell_keys) - set(rows))})"
        )
    for cell, key in zip(cells, cell_keys):
        if len(rows[key]) != 1:
            raise ValueError(f"Bounded client roster cell {key} matches {len(rows[key])} requirements.")
        row = rows[key][0]
        if row["capability_key"] in preview_capabilities:
            raise ValueError(f"Bounded client roster cell {key} governs Preview capability {row['capability_key']!r}.")
        if row["client_lane"] != cell["client_lane"] or row.get("test_ids") != [cell["test_id"]]:
            raise ValueError(
                f"Bounded requirement {key} must carry lane {cell['client_lane']!r} and exactly "
                f"test_ids [{cell['test_id']!r}]."
            )
        pinned = clients[cell["canonical_client"]]["client_version"]
        if pinned is not None and row["client_version"] != pinned:
            raise ValueError(f"Bounded requirement {key} must pin client_version {pinned!r}.")
    bounded_groups = {
        (row["client_lane"], row["client_version"], row["surface"])
        for matched in rows.values() for row in matched
    }
    claims: dict[tuple[str, str, str, str], int] = {}
    for row in catalog["requirements"]:
        group = (row["client_lane"], row["client_version"], row["surface"])
        if group in bounded_groups and row["addressable_by_client"]:
            if not row.get("test_ids"):
                raise ValueError(f"Requirement sharing a bounded receipt group declares no test_ids: {group}")
            for test_id in row["test_ids"]:
                claims[(*group, test_id)] = claims.get((*group, test_id), 0) + 1
    ambiguous = sorted(claim for claim, count in claims.items() if count > 1)
    if ambiguous:
        raise ValueError(f"Bounded test IDs resolve to more than one requirement: {ambiguous}")


def validate_mcp_authorization(catalog: dict) -> None:
    """Check both canonical clients against the exhaustive operation policy."""
    spec = importlib.util.spec_from_file_location("mcp_auth_generator", ROOT / "generate-protocol-requirements.py")
    config = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(config)
    inventory = json.loads((ROOT / "sources/geospatial-mcp/operations.v1.json").read_text(encoding="utf-8"))
    policies = config.mcp_operation_auth(inventory)
    kinds = {entry["operation"]: entry["kind"] for entry in inventory["operations"]}
    expected = {
        (operation, client, lane, version, "local-docker", f"mcp.{kinds[operation]}", policy)
        for operation, policy in policies.items()
        for client, lane, version in config.mcp_clients()
    }
    rows = [row for row in catalog["requirements"] if row["surface"] == "mcp"]
    actual = {
        (row["operation"], row["canonical_client"], row["client_lane"], row["client_version"], row["deployment_target"],
         row["capability_key"], row["auth_policy_revision"])
        for row in rows
    }
    if (len(rows) != len(expected) or actual != expected
            or any(row["scenario_facets"] != ["positive", "negative", "media-schema", "auth"] for row in rows)):
        raise ValueError("MCP requirements differ from the complete selected-client authorization policy")


def main() -> None:
    catalog = json.loads((ROOT / "protocol-certification-requirements.v1.json").read_text(encoding="utf-8"))
    schema = json.loads((ROOT / "protocol-certification-requirements.v1.schema.json").read_text(encoding="utf-8"))
    revisions = json.loads((ROOT / "sources" / "source-revisions.v1.json").read_text(encoding="utf-8"))["sources"]
    assignments = json.loads(
        (ROOT / "sources" / "canonical-client-assignments.v1.json").read_text(encoding="utf-8")
    )
    sdk_protocols = json.loads(
        (ROOT / "sources" / "official-sdk-protocol-assignments.v1.json").read_text(encoding="utf-8")
    )
    protocol_harness = json.loads(
        (ROOT / "sources" / "server" / "protocol-harness-assignments.v1.json").read_text(encoding="utf-8")
    )
    applicability = json.loads(
        (ROOT / "sources" / "canonical-client-applicability.v1.json").read_text(encoding="utf-8")
    )
    server = json.loads(
        (ROOT / "sources" / "server" / "capability-matrix.v1.json").read_text(encoding="utf-8")
    )
    bounded_roster = json.loads(
        (ROOT / "sources" / "bounded-client-roster.v1.json").read_text(encoding="utf-8")
    )
    desktop_client = json.loads(
        (ROOT / "sources" / "desktop-client-certification.v1.json").read_text(encoding="utf-8")
    )["clients"]["pro"]
    LICENSED_POLICIES[desktop_client["entitlement_policy_revision"]] = (
        desktop_client["deployment_target"], desktop_client["auth_policy_revision"]
    )
    scripting_policy = next(
        row["entitlement_policy_revision"] for row in catalog["requirements"]
        if row["client_lane"].startswith("desktop-arcpy-")
    )
    LICENSED_POLICIES[scripting_policy] = (
        desktop_client["deployment_target"], desktop_client["auth_policy_revision"]
    )
    bounded_lanes = {
        (cell["canonical_client"], cell["surface"], cell["operation"]): cell["client_lane"]
        for cell in bounded_roster["cells"]
    }
    jsonschema.validate(catalog, schema)
    validate_geometry_client_addressability(catalog)
    validate_mcp_authorization(catalog)
    if catalog["receipt_schema_min"] not in {"v1", "v2"}:
        raise ValueError("Catalog receipt_schema_min must be 'v1' or 'v2'.")
    if catalog["source_revisions"] != revisions:
        raise ValueError("Catalog source revisions differ from the pinned source manifest.")
    if catalog["complete"] is not True:
        raise ValueError("Protocol certification denominator is not declared complete.")
    keys = [
        (
            row["surface"],
            row["operation"],
            row["canonical_client"],
            row["client_version"],
            row["deployment_target"],
        )
        for row in catalog["requirements"]
    ]
    if len(keys) != len(set(keys)):
        raise ValueError("Protocol certification requirements contain duplicate cells.")
    for row in catalog["requirements"]:
        policy = row.get("entitlement_policy_revision")
        if row["licensed"]:
            expected = LICENSED_POLICIES.get(policy)
            if expected is None:
                raise ValueError(f"Licensed requirement has unknown entitlement policy: {policy!r}")
            actual = (row["deployment_target"], row["auth_policy_revision"])
            if actual != expected:
                raise ValueError(
                    f"Licensed requirement policy {policy!r} requires target/auth {expected!r}, "
                    f"got {actual!r}"
                )
        elif policy is not None:
            raise ValueError("Unlicensed requirement cannot claim an entitlement policy.")
    expected_licensed_js_policies = {
        ("streaming.feature-subscriptions", "realtime", "subscribe"): (
            "licensed-release", "release", "api-key-protected-v1",
            "honua-pro-feature-subscriptions-v1",
        ),
        ("streaming.feature-subscriptions", "realtime", "resume"): (
            "licensed-release", "release", "api-key-protected-v1",
            "honua-pro-feature-subscriptions-v1",
        ),
    }
    licensed_js_policies = {
        (row["capability_key"], row["surface"], row["operation"]): (
            row["deployment_target"], row["required_tier"], row["auth_policy_revision"],
            row["entitlement_policy_revision"],
        )
        for row in catalog["requirements"]
        if row["client_lane"] == "sdk-js-certification" and row["licensed"]
    }
    if licensed_js_policies != expected_licensed_js_policies:
        raise ValueError(
            "Licensed JavaScript protocol policies differ from the closed release-owned map "
            f"(expected={expected_licensed_js_policies!r}, actual={licensed_js_policies!r})."
        )
    required_surfaces = {"sdk-python", "sdk-js", "feature-server", "ogc", "cog", "hdf5-netcdf", "zarr"}
    surface_names = {row["surface"] for row in catalog["requirements"]}
    missing = {
        surface for surface in required_surfaces
        if surface not in surface_names
        and not any(name.startswith(f"{surface}:") for name in surface_names)
    }
    if missing:
        raise ValueError(f"Protocol certification denominator is missing required surfaces: {sorted(missing)}")
    required_sdk_lanes = {
        "sdk-dotnet-certification",
        "sdk-python-certification",
        "sdk-js-certification",
    }
    missing_sdk_lanes = required_sdk_lanes - {
        row["client_lane"] for row in catalog["requirements"]
    }
    if missing_sdk_lanes:
        raise ValueError(
            "Protocol certification denominator is missing SDK operation lanes: "
            f"{sorted(missing_sdk_lanes)}"
        )
    present_assignments = {
        (
            row["capability_key"],
            row["surface"],
            row["canonical_client"],
            row["client_version"],
        )
        for row in catalog["requirements"]
    }
    expected_assignments = set()
    implemented_keys = {
        capability["key"]
        for capability in server["capabilities"]
        if capability.get("maturity", {}).get("implemented")
    }
    roster_preview_cells = {
        (cell["canonical_client"], cell["surface"], cell["operation"])
        for cell in bounded_roster["preview_cells"]
    }
    for assignment in assignments["assignments"]:
        # The generator only emits assignments for implemented capabilities.
        if assignment["capability_key"] not in implemented_keys:
            continue
        for client_id in assignment["clients"]:
            client = assignments["clients"][client_id]
            if (client["name"], assignment["surface"], assignment["capability_key"]) in roster_preview_cells:
                continue
            expected_assignments.add((
                assignment["capability_key"],
                assignment["surface"],
                client["name"],
                client["version"].replace("{server_sha}", revisions["server"]["commit"]),
            ))
    missing_assignments = expected_assignments - present_assignments
    if missing_assignments:
        raise ValueError(
            "Protocol certification denominator is missing governed canonical-client assignments: "
            f"{sorted(missing_assignments)}"
        )
    harness_source_sha = revisions["server-certification"]["commit"]
    harness_contract = (
        f"server-protocol-harness@{protocol_harness['revision']}+{harness_source_sha}"
    )
    expected_harness_fields = {
        "schema", "revision", "tracking_issue", "canonical_client", "client_lane",
        "deployment_target", "auth_policy_revision", "required_tier", "assignments",
    }
    if set(protocol_harness) != expected_harness_fields:
        raise ValueError("Server protocol harness source has unknown or missing top-level fields.")
    harness_assignments = protocol_harness["assignments"]
    # The nightly vendors the harness at each candidate server (honua-release#386), and the server
    # grows it (42 operations at 87966c3), so 32, the count at revision 2026-08-21.1, is a floor.
    if len(harness_assignments) < 32:
        raise ValueError("Server protocol harness must govern at least the 32 public operations of revision 2026-08-21.1.")
    allowed_assignment_fields = {
        "capability_key", "catalog_capability_key", "surface", "operation", "test_ids",
        "scenario_facets",
    }
    required_assignment_fields = allowed_assignment_fields - {"catalog_capability_key"}
    known_capabilities = {capability["key"] for capability in server["capabilities"]}
    for assignment in harness_assignments:
        if assignment.get("capability_key") not in known_capabilities:
            raise ValueError("Server protocol harness assignment names an unknown capability.")
        if not required_assignment_fields.issubset(assignment) or not set(assignment).issubset(allowed_assignment_fields):
            raise ValueError("Server protocol harness assignment has unknown or missing fields.")
        if not re.fullmatch(r"(?:GET|POST|PUT|PATCH|DELETE) /\S(?:.*\S)?", assignment["operation"]):
            raise ValueError(f"Server protocol harness has invalid operation identity: {assignment['operation']!r}")
        if "catalog_capability_key" in assignment and not assignment["catalog_capability_key"]:
            raise ValueError("Server protocol harness catalog capability crosswalk cannot be empty.")
        facets = assignment["scenario_facets"]
        if not facets or len(facets) != len(set(facets)):
            raise ValueError("Server protocol harness scenario facets must be non-empty and unique.")
    harness_keys = [
        (assignment["capability_key"], assignment["surface"], assignment["operation"])
        for assignment in harness_assignments
    ]
    if len(harness_keys) != len(set(harness_keys)):
        raise ValueError("Server protocol harness contains duplicate operation assignments.")
    if any(
        not assignment.get("test_ids")
        or len(assignment["test_ids"]) != len(set(assignment["test_ids"]))
        for assignment in harness_assignments
    ):
        raise ValueError("Every server protocol harness operation requires unique executable test IDs.")
    def sdk_has_entrypoints(capability_key: str) -> bool:
        for client in sdk_protocols["clients"]:
            source_name = client["source"]
            snapshot = json.loads(
                (ROOT / "sources" / source_name / "sdk-coverage.v1.json").read_text(encoding="utf-8")
            )
            collection = snapshot.get("coverage", []) if source_name == "sdk-dotnet" else snapshot.get("capabilities", [])
            coverage = next(
                (
                    row for row in collection
                    if row.get("key") == capability_key and row.get("status") in SUPPORTED
                ),
                None,
            )
            if coverage and coverage.get("entrypoints"):
                return True
        return False

    governed_official_capabilities = set(sdk_protocols["capabilities"]) | {
        decision["capability_key"]
        for decision in applicability["decisions"]
        if decision["classification"] == "official-sdk-required"
    }
    expected_harness_capabilities = {
        capability
        for capability in governed_official_capabilities
        if capability in implemented_keys and not sdk_has_entrypoints(capability)
    }
    actual_harness_capabilities = {
        assignment["capability_key"] for assignment in harness_assignments
        if assignment["capability_key"] in implemented_keys
    }
    # Governed write operations supplement SDK-covered capabilities; they are not gaps.
    # The exact operation/test/facet denominator below still requires every implemented
    # assignment and refuses extra, omitted or altered generated rows.
    # An explicit unassigned contract with one unproduced owner is a reviewed gap
    # (analytics.h3, serve.wps at the frozen candidate). It is not harness credit and
    # it is not a silent omission. Stripping the row, or letting a producer claim the
    # lane, still fails.
    gap_rows = explicit_unassigned_harness_gaps(catalog)
    overlap = set(gap_rows) & actual_harness_capabilities
    if overlap:
        raise ValueError(
            "Server protocol harness assignments coexist with explicit unassigned contracts "
            f"(capabilities={sorted(overlap)})"
        )
    accounted_harness_capabilities = actual_harness_capabilities | set(gap_rows)
    if not expected_harness_capabilities.issubset(accounted_harness_capabilities):
        raise ValueError(
            "Server protocol harness assignments omit operation-contract gaps "
            f"(missing={sorted(expected_harness_capabilities - accounted_harness_capabilities)})"
        )
    unexpected_gaps = set(gap_rows) - expected_harness_capabilities
    if unexpected_gaps:
        raise ValueError(
            "Unassigned protocol harness contracts name capabilities that are not implemented gaps "
            f"(unexpected={sorted(unexpected_gaps)})"
        )
    require_unproduced_harness_gaps(catalog, gap_rows)
    expected_harness_rows = {
        (
            assignment["capability_key"], assignment["surface"], assignment["operation"],
            protocol_harness["canonical_client"], protocol_harness["client_lane"],
            f"source@{harness_source_sha}", protocol_harness["deployment_target"],
            protocol_harness["required_tier"], tuple(assignment["scenario_facets"]),
            harness_contract, protocol_harness["auth_policy_revision"],
            f"server-test-fixtures@{harness_source_sha}", tuple(assignment["test_ids"]),
        )
        for assignment in harness_assignments
        # The generator only emits harness rows for implemented capabilities.
        if assignment["capability_key"] in implemented_keys
    }
    present_harness_rows = {
        (
            row["capability_key"], row["surface"], row["operation"],
            row["canonical_client"], row["client_lane"], row["client_version"],
            row["deployment_target"], row["required_tier"], tuple(row["scenario_facets"]),
            row["contract_revision"], row["auth_policy_revision"], row["fixture_revision"],
            tuple(row.get("test_ids", [])),
        )
        for row in catalog["requirements"]
        if row["contract_revision"] == harness_contract
    }
    if present_harness_rows != expected_harness_rows:
        raise ValueError(
            "Protocol certification denominator differs from the governed server harness contract "
            f"(missing={sorted(expected_harness_rows - present_harness_rows)}, "
            f"unexpected={sorted(present_harness_rows - expected_harness_rows)})"
        )

    expected_sdk_protocols = set(sdk_protocols["capabilities"])
    sdk_client_names = {client["name"] for client in sdk_protocols["clients"]}
    present_sdk_protocols = {
        row["capability_key"]
        for row in catalog["requirements"]
        if (
            row["canonical_client"] in sdk_client_names
            or row["contract_revision"] == harness_contract
            or str(row["operation"]).startswith("UNASSIGNED PROTOCOL HARNESS CONTRACT:")
        )
    }
    missing_sdk_protocols = expected_sdk_protocols - present_sdk_protocols
    if missing_sdk_protocols:
        raise ValueError(
            "Protocol certification denominator is missing official SDK protocol parity cells: "
            f"{sorted(missing_sdk_protocols)}"
        )
    protocol_prefixes = ("serve.", "process.", "editing.", "routing.", "geocoding.", "styling.")
    implemented_protocols = {
        capability["key"]
        for capability in server["capabilities"]
        if capability["key"].startswith(protocol_prefixes)
        and capability.get("maturity", {}).get("implemented")
    }
    declared_protocols = set(sdk_protocols["capabilities"])
    if implemented_protocols != declared_protocols:
        raise ValueError(
            "Official SDK protocol assignments differ from the implemented public protocol surface "
            f"(missing={sorted(implemented_protocols - declared_protocols)}, "
            f"unexpected={sorted(declared_protocols - implemented_protocols)})"
        )
    implemented_capabilities = {
        capability["key"]
        for capability in server["capabilities"]
        if capability.get("maturity", {}).get("implemented")
    }
    decisions = applicability["decisions"]
    decision_capabilities = {decision["capability_key"] for decision in decisions}
    if len(decision_capabilities) != len(decisions):
        raise ValueError("Canonical-client applicability contains duplicate capability decisions.")
    allowed_classifications = {
        "official-sdk-required",
        "canonical-external-required",
        "not-client-addressable",
    }
    invalid_classifications = {
        decision["classification"]
        for decision in decisions
        if decision["classification"] not in allowed_classifications
    }
    if invalid_classifications:
        raise ValueError(
            "Canonical-client applicability contains invalid classifications: "
            f"{sorted(invalid_classifications)}"
        )
    overlap = declared_protocols & decision_capabilities
    if overlap:
        raise ValueError(
            "Capabilities cannot be both protocol-assigned and separately classified: "
            f"{sorted(overlap)}"
        )
    unknown_decisions = decision_capabilities - known_capabilities
    if unknown_decisions:
        raise ValueError(f"Canonical-client applicability names unknown capabilities: {sorted(unknown_decisions)}")
    implemented_decisions = decision_capabilities & implemented_capabilities
    if any(
        row["contract_revision"] == f"canonical-client-applicability@{applicability['revision']}"
        and row["capability_key"] not in implemented_decisions
        for row in catalog["requirements"]
    ):
        raise ValueError("Canonical-client applicability has a generated row for a nonimplemented decision.")
    classified_capabilities = declared_protocols | implemented_decisions
    if implemented_capabilities != classified_capabilities:
        raise ValueError(
            "Canonical-client applicability differs from the implemented capability surface "
            f"(unclassified={sorted(implemented_capabilities - classified_capabilities)}, "
            f"unexpected={sorted(classified_capabilities - implemented_capabilities)})"
        )
    governed_fields = (
        "capability_key", "surface", "operation", "canonical_client", "client_lane",
        "client_version", "deployment_target", "required_tier", "licensed", "entitlement_policy_revision",
        "addressable_by_client", "addressability_reason", "scenario_facets",
        "contract_revision", "auth_policy_revision", "fixture_revision", "budget_expectations",
    )

    def projection(row: dict) -> tuple:
        return tuple(
            tuple(row[field]) if field == "scenario_facets"
            else json.dumps(row[field], sort_keys=True) if field == "budget_expectations"
            else row[field]
            for field in governed_fields
        )

    sdk_coverage = {
        source_name: json.loads(
            (ROOT / "sources" / source_name / "sdk-coverage.v1.json").read_text(encoding="utf-8")
        )
        for source_name in ("sdk-js", "sdk-python", "sdk-dotnet")
    }
    python_contract = json.loads(
        (ROOT / "sources" / "sdk-python" / "protocol-certification.v1.json").read_text(encoding="utf-8")
    )
    python_fixture = python_contract.get("fixtureRevision")
    if not isinstance(python_fixture, str) or not python_fixture.strip():
        raise ValueError("pinned sdk-python protocol contract is missing fixtureRevision")
    sdk_fixtures = {
        "sdk-js": "0.2.0-alpha.1",
        "sdk-python": python_fixture,
        "sdk-dotnet": "sha256:83eb29ac38a3fb54914c1252b273dbb7f7f4d651a8204aafb4108d14d6d23727",
    }
    expected_decision_rows: list[dict] = []
    for decision in decisions:
        capability_key = decision["capability_key"]
        if capability_key not in implemented_decisions:
            continue
        classification = decision["classification"]
        if classification == "official-sdk-required":
            capability_row_count = 0
            for client in sdk_protocols["clients"]:
                source_name = client["source"]
                collection = (
                    sdk_coverage.get(source_name, {}).get("coverage", [])
                    if source_name == "sdk-dotnet"
                    else sdk_coverage.get(source_name, {}).get("capabilities", [])
                )
                coverage = next(
                    (
                        row for row in collection
                        if row.get("key") == capability_key and row.get("status") in SUPPORTED
                    ),
                    None,
                )
                entrypoints = coverage.get("entrypoints", []) if coverage else []
                if entrypoints:
                    for entrypoint in entrypoints:
                        expected_decision_rows.append({
                            "capability_key": capability_key,
                            "surface": f"{source_name}:{slug(capability_key)}",
                            "operation": entrypoint,
                            "canonical_client": client["name"],
                            "client_lane": source_name,
                            "client_version": client["version"],
                            "deployment_target": "local-docker",
                            "required_tier": "nightly",
                            "licensed": False,
                            "entitlement_policy_revision": None,
                            "addressable_by_client": True,
                            "addressability_reason": None,
                            "scenario_facets": ["positive", "media-schema"],
                            "contract_revision": f"{source_name}-coverage@{revisions[source_name]['commit']}",
                            "auth_policy_revision": client["auth_policy_revision"],
                            "fixture_revision": sdk_fixtures[source_name],
                            "budget_expectations": None,
                        })
                        capability_row_count += 1
            if capability_row_count == 0:
                matched_harness = False
                for assignment in harness_assignments:
                    if assignment["capability_key"] != capability_key:
                        continue
                    matched_harness = True
                    expected_decision_rows.append({
                        "capability_key": capability_key,
                        "surface": assignment["surface"],
                        "operation": assignment["operation"],
                        "canonical_client": protocol_harness["canonical_client"],
                        "client_lane": protocol_harness["client_lane"],
                        "client_version": f"source@{harness_source_sha}",
                        "deployment_target": protocol_harness["deployment_target"],
                        "required_tier": protocol_harness["required_tier"],
                        "licensed": False,
                        "entitlement_policy_revision": None,
                        "addressable_by_client": True,
                        "addressability_reason": None,
                        "scenario_facets": assignment["scenario_facets"],
                        "contract_revision": harness_contract,
                        "auth_policy_revision": protocol_harness["auth_policy_revision"],
                        "fixture_revision": f"server-test-fixtures@{harness_source_sha}",
                        "budget_expectations": None,
                        "test_ids": assignment["test_ids"],
                    })
                # No SDK entrypoint and no harness assignment: the generator records one
                # unassigned contract under this applicability revision. It is a reviewed
                # gap, not a harness pass. The harness accounting above still requires
                # exactly one unproduced owner.
                if not matched_harness:
                    expected_decision_rows.append({
                        "capability_key": capability_key,
                        "surface": slug(capability_key),
                        "operation": f"{UNASSIGNED_PROTOCOL_HARNESS_PREFIX}{capability_key}",
                        "canonical_client": "UNASSIGNED PROTOCOL HARNESS",
                        "client_lane": f"protocol-harness-gap-{slug(capability_key)}",
                        "client_version": "policy-v1",
                        "deployment_target": "local-docker",
                        "required_tier": "nightly",
                        "licensed": False,
                        "entitlement_policy_revision": None,
                        "addressable_by_client": True,
                        "addressability_reason": None,
                        "scenario_facets": ["positive"],
                        "contract_revision": f"canonical-client-applicability@{applicability['revision']}",
                        "auth_policy_revision": "unassigned-protocol-harness-v1",
                        "fixture_revision": FIXTURE,
                        "budget_expectations": None,
                    })
            continue
        elif classification == "canonical-external-required":
            client_ids = decision.get("clients", [])
            if not client_ids:
                raise ValueError(f"Canonical external decision has no clients: {capability_key}")
            unknown_clients = set(client_ids) - set(applicability["clients"])
            if unknown_clients:
                raise ValueError(
                    f"Canonical external decision has unknown clients for {capability_key}: "
                    f"{sorted(unknown_clients)}"
                )
            for client_id in client_ids:
                client = applicability["clients"][client_id]
                expected_decision_rows.append({
                    "capability_key": capability_key,
                    "surface": slug(capability_key),
                    "operation": capability_key,
                    "canonical_client": client["name"],
                    "client_lane": bounded_lanes.get(
                        (client["name"], slug(capability_key), capability_key),
                        f"{client['lane']}-{slug(capability_key)}",
                    ),
                    "client_version": client["version"],
                    "deployment_target": "local-docker",
                    "required_tier": "nightly",
                    "licensed": False,
                    "entitlement_policy_revision": None,
                    "addressable_by_client": True,
                    "addressability_reason": None,
                    "scenario_facets": decision["scenario_facets"],
                    "contract_revision": f"canonical-client-applicability@{applicability['revision']}",
                    "auth_policy_revision": client["auth_policy_revision"],
                    "fixture_revision": FIXTURE,
                    "budget_expectations": None,
                })
            continue
        else:
            reason = decision.get("reason")
            if not reason:
                raise ValueError(f"Non-client-addressable decision has no reason: {capability_key}")
            expected_decision_rows.append({
                "capability_key": capability_key,
                "surface": slug(capability_key),
                "operation": capability_key,
                "canonical_client": "NOT CLIENT ADDRESSABLE",
                "client_lane": f"not-client-addressable-{slug(capability_key)}",
                "client_version": "policy-v1",
                "deployment_target": "local-docker",
                "required_tier": "nightly",
                "licensed": False,
                "entitlement_policy_revision": None,
                "addressable_by_client": False,
                "addressability_reason": reason,
                "scenario_facets": ["not-client-addressable"],
                "contract_revision": f"canonical-client-applicability@{applicability['revision']}",
                "auth_policy_revision": "not-client-addressable-v1",
                "fixture_revision": FIXTURE,
                "budget_expectations": None,
            })
    sdk_client_names = {client["name"] for client in sdk_protocols["clients"]}
    sdk_coverage_contracts = {
        client["name"]: f"{client['source']}-coverage@{revisions[client['source']]['commit']}"
        for client in sdk_protocols["clients"]
        if client["source"] in sdk_coverage
    }
    official_capabilities = {
        decision["capability_key"]
        for decision in decisions
        if decision["classification"] == "official-sdk-required"
    }
    present_decision_rows = [
        row
        for row in catalog["requirements"]
        if row["capability_key"] in implemented_decisions
        and (
            row["contract_revision"]
            == f"canonical-client-applicability@{applicability['revision']}"
            or row["contract_revision"] == harness_contract
            or (
                row["capability_key"] in official_capabilities
                and row["canonical_client"] in sdk_client_names
                and row["contract_revision"]
                == sdk_coverage_contracts.get(row["canonical_client"])
            )
        )
    ]
    expected_decision_cells = {projection(row) for row in expected_decision_rows}
    present_decision_cells = {projection(row) for row in present_decision_rows}
    if expected_decision_cells != present_decision_cells:
        raise ValueError(
            "Protocol certification denominator differs from canonical-client applicability decisions "
            f"(missing={sorted(expected_decision_cells - present_decision_cells)}, "
            f"unexpected={sorted(present_decision_cells - expected_decision_cells)})"
        )
    validate_bounded_roster(catalog, bounded_roster)
    validate_desktop_clients(catalog, bounded_roster)
    validate_grpc_scope(catalog)
    validate_production(catalog)
    print(f"Validated {len(keys)} complete, unique protocol certification cells.")


VERSION_LINE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.x$")
LICENSED_POLICIES = {
    "honua-pro-feature-subscriptions-v1": ("licensed-release", "api-key-protected-v1"),
    "licensed-desktop-client-v1": ("windows-licensed", "anonymous-and-protected-v1"),
}


def _validate_pyqgis_grid(
    rows: list[dict], driver: dict, must_fix: str, preview: set[str],
    implemented: set[str], matrix_keys: set[str],
) -> None:
    """R40: the pyqgis lane is the full surface × function grid. A cell the client cannot perform
    stays in the grid as not-applicable, with that reason declared on the row."""
    grid = json.loads((ROOT / "sources" / "pyqgis-function-grid.v1.json").read_text(encoding="utf-8"))
    if grid.get("schema") != "honua.pyqgis-function-grid/v1" or grid.get("ruling") != "R40":
        raise ValueError("pyqgis function grid must be schema honua.pyqgis-function-grid/v1 under ruling R40.")
    operations = [item["operation"] for item in grid["functions"]]
    if len(operations) != len(set(operations)) or not operations:
        raise ValueError("pyqgis function grid needs a unique operation per function.")
    expected: dict[tuple[str, str], str | None] = {}
    for surface in grid["surfaces"]:
        declared = surface["not_applicable"]
        unknown = set(declared) - set(operations)
        if unknown:
            raise ValueError(f"pyqgis surface {surface['surface']} names unknown functions {sorted(unknown)}.")
        if surface["capability_key"] in preview and set(declared) != set(operations):
            raise ValueError(f"preview surface {surface['surface']} must declare every function not-applicable.")
        # The generator emits no grid row for a capability the pinned matrix has not implemented.
        if surface["capability_key"] in matrix_keys and surface["capability_key"] not in implemented:
            continue
        for operation in operations:
            reason = declared.get(operation)
            if reason is not None and not (isinstance(reason, str) and reason.startswith("not-applicable: ")):
                raise ValueError(f"pyqgis {surface['surface']}/{operation} needs a not-applicable reason.")
            expected[(surface["surface"], operation)] = reason
    for dropped in grid["dropped_functions"]:
        reason = dropped["addressability_reason"]
        if not (isinstance(reason, str) and reason.startswith("not-applicable: ")):
            raise ValueError(f"dropped PyQGIS function {dropped['operation']} needs a not-applicable reason.")
        key = (dropped["surface"], dropped["operation"])
        if key in expected:
            raise ValueError(f"dropped PyQGIS function {key} collides with a grid cell.")
        if dropped["surface"] in matrix_keys and dropped["surface"] not in implemented:
            continue
        expected[key] = reason
    present = {
        (row["surface"], row["operation"]): row
        for row in rows
        if row["client_lane"] == driver["lane"] and row["surface"] != "ogc"
    }
    if set(present) != set(expected):
        raise ValueError(
            "pyqgis function grid rows differ from sources/pyqgis-function-grid.v1.json "
            f"(missing={sorted(set(expected) - set(present))[:8]}, "
            f"unexpected={sorted(set(present) - set(expected))[:8]})."
        )
    for key, reason in expected.items():
        row = present[key]
        if row["release_bucket"] != must_fix or row["canonical_client"] != driver["canonical_client"]:
            raise ValueError(f"pyqgis {key} is not a must-fix scripting row.")
        if reason is None:
            if not row["addressable_by_client"] or row["addressability_reason"] is not None:
                raise ValueError(f"pyqgis {key} is applicable and must stay addressable.")
        elif row["addressable_by_client"] or row["addressability_reason"] != reason:
            raise ValueError(f"pyqgis {key} must carry its not-applicable reason.")


def _ui_functions(surface: dict, ui: dict) -> list[str]:
    functions = list(ui["functions"])
    if surface.get("edit"):
        functions.append(ui["feature_edit"])
    if surface.get("wfs_edit"):
        functions.append(ui["wfs_edit"])
    return functions


def validate_desktop_clients(catalog: dict, roster: dict) -> None:
    """R38 keeps desktop clients on a release line. R40 (honua-release#376) splits each client by
    driver. Both scripting drivers are must-fix-before-cut on every applicable GeoServices and OGC
    cell. pro-ui and qgis-ui are must-fix-before-cut only on the core set, and every other UI cell
    is prove-against-candidate. A release_bucket travels only with a client_driver."""
    source = json.loads((ROOT / "sources" / "desktop-client-certification.v1.json").read_text(encoding="utf-8"))
    qgis, pro = source["clients"]["qgis"], source["clients"]["pro"]
    drivers = source["drivers"]
    must_fix, prove = source["buckets"]["must_fix"], source["buckets"]["prove"]
    if (must_fix, prove) != ("must-fix-before-cut", "prove-against-candidate"):
        raise ValueError("Desktop release buckets must be must-fix-before-cut and prove-against-candidate.")
    if set(drivers) != {"pyqgis", "pro-ui", "qgis-ui"}:
        raise ValueError(f"Desktop source drivers must be pyqgis, pro-ui and qgis-ui, not {tuple(drivers)}.")
    for client in (qgis, pro):
        if not VERSION_LINE.fullmatch(client["version"]):
            raise ValueError(f"Desktop client {client['name']} must name a release line <major>.<minor>.x, "
                             f"not {client['version']!r}.")
    if catalog.get("trademarkNotice") != source["trademarkNotice"]:
        raise ValueError("Catalog trademarkNotice differs from sources/desktop-client-certification.v1.json.")
    if roster["clients"]["QGIS"]["client_version"] != qgis["version"]:
        raise ValueError(f"Bounded roster QGIS version differs from the desktop release line {qgis['version']!r}.")
    preview = set(next(
        ruling["preview_capability_keys"] for ruling in roster["rulings"] if ruling["id"] == "preview-surfaces"
    ))
    matrix = json.loads((ROOT / "sources" / "server" / "capability-matrix.v1.json").read_text(encoding="utf-8"))
    matrix_keys = {capability["key"] for capability in matrix["capabilities"]}
    implemented = {
        capability["key"] for capability in matrix["capabilities"]
        if capability.get("maturity", {}).get("implemented")
    }
    rows = catalog["requirements"]
    # The licensed scripting policy is the windows-licensed entitlement that is not the UI policy.
    # Its driver id is that policy's own suffix, so this file does not spell the driver.
    scripting_policy = next(
        policy for policy, (target, _auth) in LICENSED_POLICIES.items()
        if target == pro["deployment_target"] and policy != pro["entitlement_policy_revision"]
    )
    scripting_driver = scripting_policy.split("-")[-2]
    client_drivers = (scripting_driver, "pyqgis", "pro-ui", "qgis-ui")
    ogc_cases = {
        row["operation"] for row in rows
        if row["surface"] == "ogc" and row["canonical_client"] == "OGC CITE"
    }
    by_driver: dict[str, list[dict]] = {driver: [] for driver in client_drivers}
    for row in rows:
        has_driver, has_bucket = "client_driver" in row, "release_bucket" in row
        if has_driver != has_bucket:
            raise ValueError(
                f"{row['canonical_client']} {row['surface']}/{row['operation']} must carry client_driver "
                "and release_bucket together."
            )
        if not has_driver:
            continue
        driver, bucket = row["client_driver"], row["release_bucket"]
        if driver not in by_driver or bucket not in (must_fix, prove):
            raise ValueError(f"Ungoverned desktop driver or bucket on {row['surface']}/{row['operation']}.")
        if row["capability_key"] in preview and row["addressable_by_client"]:
            raise ValueError(
                f"{driver} row {row['surface']}/{row['operation']} carries a supported requirement on "
                f"Preview capability {row['capability_key']!r} (preview-surfaces ruling)."
            )
        by_driver[driver].append(row)
        version = pro["version"] if driver in (scripting_driver, "pro-ui") else qgis["version"]
        if row["client_version"] != version:
            raise ValueError(f"{driver} row {row['surface']}/{row['operation']} left its release line.")
    withheld = [
        row for row in rows
        if row["capability_key"] in matrix_keys and row["capability_key"] not in implemented
    ]
    if withheld:
        row = withheld[0]
        raise ValueError(
            f"{row['client_lane']} row {row['surface']}/{row['operation']} keeps maturity "
            f"{row.get('maturity')!r} for {row['capability_key']!r}, which the pinned matrix has not implemented."
        )
    summary = catalog.get("desktop_driver_summary")
    expected_summary = {
        "ruling": "R40",
        "drivers": {
            driver: {
                must_fix: sum(row["release_bucket"] == must_fix for row in driver_rows),
                prove: sum(row["release_bucket"] == prove for row in driver_rows),
            }
            for driver, driver_rows in by_driver.items()
        },
    }
    if summary != expected_summary:
        raise ValueError(f"desktop_driver_summary {summary} differs from the generated rows {expected_summary}.")
    geoservices_surfaces = {"feature-server", "map-server", "image-server", "featureserver", "mapserver"}
    for driver in (scripting_driver, "pyqgis"):
        if any(row["release_bucket"] != must_fix for row in by_driver[driver]):
            raise ValueError(f"Scripting driver {driver} has a row that is not {must_fix}.")
        if not any(
            row["surface"] in geoservices_surfaces or "geoservices" in row["capability_key"]
            for row in by_driver[driver]
        ):
            raise ValueError(f"Scripting driver {driver} has no GeoServices cell.")
        if not any(
            row["surface"] == "ogc" or row["capability_key"].startswith("serve.w")
            or row["capability_key"].startswith("serve.ogc")
            for row in by_driver[driver]
        ):
            raise ValueError(f"Scripting driver {driver} has no OGC cell.")
    scripting_rows = by_driver[scripting_driver]
    if {row["canonical_client"] for row in scripting_rows} != {pro["name"]}:
        raise ValueError("Licensed scripting rows must use the licensed desktop canonical client.")
    if any(
        not row["licensed"] or row["deployment_target"] != pro["deployment_target"]
        or row["entitlement_policy_revision"] != scripting_policy
        or row["auth_policy_revision"] != pro["auth_policy_revision"]
        or scripting_driver not in row["client_lane"]
        for row in scripting_rows
    ):
        raise ValueError("Every licensed scripting row must be licensed on the governed desktop target.")
    scripting_operations = {row["operation"] for row in scripting_rows}
    scripting_capabilities = {row["capability_key"] for row in scripting_rows}
    if set(ogc_cases) - scripting_operations or any(
        assignment["capability_key"] not in scripting_capabilities
        for assignment in source["ogc_assignments"]
        if assignment["capability_key"] in implemented and assignment["capability_key"] not in preview
    ):
        raise ValueError("The licensed scripting driver must cover every supported OGC matrix case and implemented OGC surface.")
    pyqgis_rows = by_driver["pyqgis"]
    if {row["canonical_client"] for row in pyqgis_rows} != {qgis["name"], drivers["pyqgis"]["canonical_client"]}:
        raise ValueError("pyqgis rows must be the roster QGIS client plus the pyqgis canonical client.")
    if any(row["licensed"] or row["deployment_target"] != qgis["deployment_target"] for row in pyqgis_rows):
        raise ValueError("pyqgis rows stay on the unlicensed QGIS deployment target.")
    pyqgis_operations = {row["operation"] for row in pyqgis_rows}
    pyqgis_capabilities = {row["capability_key"] for row in pyqgis_rows}
    if set(ogc_cases) - pyqgis_operations:
        raise ValueError(f"pyqgis is missing OGC matrix cases {sorted(set(ogc_cases) - pyqgis_operations)}.")
    missing_surfaces = sorted(
        assignment["capability_key"] for assignment in source["ogc_assignments"]
        if assignment["capability_key"] in implemented
        and assignment["capability_key"] not in preview
        and assignment["capability_key"] not in pyqgis_capabilities
    )
    if missing_surfaces:
        raise ValueError(f"pyqgis is missing OGC surfaces {missing_surfaces}.")
    _validate_pyqgis_grid(pyqgis_rows, drivers["pyqgis"], must_fix, preview, implemented, matrix_keys)
    ui = source["ui"]
    expected_ui: dict[str, dict[tuple[str, str], str]] = {"pro-ui": {}, "qgis-ui": {}}
    for surface in ui["surfaces"]:
        functions = _ui_functions(surface, ui)
        unknown = (set(surface["pro_core"]) | set(surface["qgis_core"])) - set(functions)
        if unknown:
            raise ValueError(f"UI surface {surface['surface']} cores name unknown functions {sorted(unknown)}.")
        if surface["capability_key"] in preview:
            continue
        if surface["capability_key"] in matrix_keys and surface["capability_key"] not in implemented:
            continue
        for function in functions:
            expected_ui["pro-ui"][(surface["surface"], function)] = (
                must_fix if function in surface["pro_core"] else prove
            )
            expected_ui["qgis-ui"][(surface["surface"], function)] = (
                must_fix if function in surface["qgis_core"] else prove
            )
    if ui["auth_capability"] not in matrix_keys or ui["auth_capability"] in implemented:
        for mode in ui["auth_modes"]:
            expected_ui["pro-ui"][(ui["auth_surface"], mode)] = must_fix
            expected_ui["qgis-ui"][(ui["auth_surface"], mode)] = must_fix
    for driver, expected in expected_ui.items():
        present = {(row["surface"], row["operation"]): row for row in by_driver[driver]}
        if set(present) != set(expected):
            raise ValueError(
                f"{driver} rows differ from the UI grid "
                f"(missing={sorted(set(expected) - set(present))}, "
                f"unexpected={sorted(set(present) - set(expected))})."
            )
        for key, bucket in expected.items():
            row = present[key]
            if row["release_bucket"] != bucket or row["client_lane"] != drivers[driver]["lane"] \
                    or row["canonical_client"] != drivers[driver]["canonical_client"]:
                raise ValueError(f"{driver} {key} is not the governed core/prove row.")
        licensed = driver == "pro-ui"
        if any(
            row["licensed"] != licensed
            or row["deployment_target"] != (pro if licensed else qgis)["deployment_target"]
            or (licensed and row["entitlement_policy_revision"] != pro["entitlement_policy_revision"])
            for row in by_driver[driver]
        ):
            raise ValueError(f"{driver} rows do not use that client's governed target.")


CANDIDATE_INPUTS = ("{server_image}", "{server_sha}", "{cut_at}")
UNASSIGNED_PROTOCOL_HARNESS_PREFIX = "UNASSIGNED PROTOCOL HARNESS CONTRACT:"


def _generator_module():
    spec = importlib.util.spec_from_file_location(
        "protocol_generator_digest", ROOT / "generate-protocol-requirements.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _disposition_selects(entry: dict, row: dict) -> bool:
    lane = row["client_lane"]
    if any(fnmatch.fnmatchcase(lane, pattern) for pattern in entry.get("except_client_lanes") or []):
        return False
    return any(
        fnmatch.fnmatchcase(lane, pattern) for pattern in entry.get("client_lanes") or []
    ) or row["deployment_target"] in (entry.get("deployment_targets") or [])


def _unassigned_gap_contract(capability: str) -> str:
    """Official assignments win: the generator emits that contract before applicability."""
    sdk_protocols = json.loads(
        (ROOT / "sources" / "official-sdk-protocol-assignments.v1.json").read_text(encoding="utf-8")
    )
    applicability = json.loads(
        (ROOT / "sources" / "canonical-client-applicability.v1.json").read_text(encoding="utf-8")
    )
    if capability in set(sdk_protocols["capabilities"]):
        return f"official-sdk-protocol-assignments@{sdk_protocols['revision']}"
    required = {
        decision["capability_key"]
        for decision in applicability["decisions"]
        if decision["classification"] == "official-sdk-required"
    }
    if capability in required:
        return f"canonical-client-applicability@{applicability['revision']}"
    raise ValueError(
        f"Unassigned protocol harness contract for {capability} is not a governed gap."
    )


def _unassigned_gap_row(capability: str, contract: str) -> dict:
    return {
        "capability_key": capability,
        "surface": slug(capability),
        "operation": f"{UNASSIGNED_PROTOCOL_HARNESS_PREFIX}{capability}",
        "maturity": "supported",
        "canonical_client": "UNASSIGNED PROTOCOL HARNESS",
        "client_lane": f"protocol-harness-gap-{slug(capability)}",
        "client_version": "policy-v1",
        "deployment_target": "local-docker",
        "required_tier": "nightly",
        "licensed": False,
        "entitlement_policy_revision": None,
        "addressable_by_client": True,
        "addressability_reason": None,
        "scenario_facets": ["positive"],
        "contract_revision": contract,
        "auth_policy_revision": "unassigned-protocol-harness-v1",
        "fixture_revision": FIXTURE,
        "budget_expectations": None,
    }


def explicit_unassigned_harness_gaps(catalog: dict) -> dict[str, dict]:
    """One reviewed unassigned contract per capability. Not a harness assignment.

    Every field the generator writes is governed. A nightly gap that becomes a
    release row, or any other drift, is not an accounted gap.
    """
    found: dict[str, dict] = {}
    for row in catalog["requirements"]:
        operation = row.get("operation")
        if not isinstance(operation, str) or not operation.startswith(UNASSIGNED_PROTOCOL_HARNESS_PREFIX):
            continue
        capability = row.get("capability_key")
        if (
            not isinstance(capability, str)
            or operation != f"{UNASSIGNED_PROTOCOL_HARNESS_PREFIX}{capability}"
        ):
            raise ValueError(
                "Unassigned protocol harness contract does not name its capability: "
                f"{operation!r}"
            )
        if capability in found:
            raise ValueError(f"Duplicate unassigned protocol harness contract for {capability}.")
        expected = _unassigned_gap_row(capability, _unassigned_gap_contract(capability))
        drifted = sorted(field for field in expected if row.get(field) != expected[field])
        extra = sorted(set(row) - set(expected))
        if drifted or extra:
            raise ValueError(
                f"Unassigned protocol harness contract for {capability} drifted "
                f"(fields={drifted}, extra={extra})."
            )
        found[capability] = row
    return found


def require_unproduced_harness_gaps(catalog: dict, gaps: dict[str, dict]) -> None:
    production = catalog.get("production") or {}
    producers = production.get("producers") or []
    unproduced = production.get("unproduced") or []
    for capability, row in gaps.items():
        produced = [entry for entry in producers if _disposition_selects(entry, row)]
        owned = [entry for entry in unproduced if _disposition_selects(entry, row)]
        if produced or len(owned) != 1:
            raise ValueError(
                "Unassigned protocol harness contract "
                f"{capability} is not an owned unproduced gap "
                f"(producers={len(produced)}, unproduced={len(owned)}). "
                "It is not a passing harness receipt."
            )


def validate_production(catalog: dict) -> None:
    """Every client-addressable cell is produced by a named workflow the nightly dispatches at the
    resolved candidate, or recorded as unproduced with an owner (honua-release#386, #360).

    The ledger's denominator stays honest: a skip is either a producer's own result or a declared
    gap with the producer its owner must cut, never an unexplained absence.
    """
    source = json.loads(
        (ROOT / "sources" / "protocol-certification-production.v1.json").read_text(encoding="utf-8")
    )
    production = catalog["production"]
    strip = lambda entry: {key: value for key, value in entry.items() if key != "cells"}  # noqa: E731
    if production["revision"] != source["revision"] \
            or [strip(entry) for entry in production["producers"]] != source["producers"] \
            or [strip(entry) for entry in production["unproduced"]] != source["unproduced"]:
        raise ValueError("Catalog production dispositions differ from sources/protocol-certification-production.v1.json.")
    dispositions = [*production["producers"], *production["unproduced"]]
    producers = [entry["producer"] for entry in production["producers"]]
    if len(producers) != len(set(producers)):
        raise ValueError("Catalog production names a producer more than once.")
    for entry in production["producers"]:
        if entry["source_revision_key"] not in catalog["source_revisions"]:
            raise ValueError(f"Producer {entry['producer']} pins an unknown source_revision_key.")
        values = set(entry["inputs"].values())
        ungoverned = sorted(value for value in values if "{" in value and value not in CANDIDATE_INPUTS)
        if ungoverned:
            raise ValueError(f"Producer {entry['producer']} uses ungoverned input placeholders {ungoverned}.")
        if not set(CANDIDATE_INPUTS) <= values:
            raise ValueError(
                f"Producer {entry['producer']} is not bound to the candidate: its inputs must carry "
                f"{', '.join(CANDIDATE_INPUTS)}."
            )
    counts = [0] * len(dispositions)
    for row in catalog["requirements"]:
        if not row["addressable_by_client"]:
            continue
        owners = {
            index for index, entry in enumerate(dispositions)
            if not any(fnmatch.fnmatchcase(row["client_lane"], pattern) for pattern in entry.get("except_client_lanes", []))
            and (any(fnmatch.fnmatchcase(row["client_lane"], pattern) for pattern in entry.get("client_lanes", []))
                 or row["deployment_target"] in entry.get("deployment_targets", []))
        }
        if len(owners) != 1:
            raise ValueError(
                f"Client lane {row['client_lane']!r} has {len(owners)} production dispositions; exactly one is required."
            )
        counts[owners.pop()] += 1
    if [entry["cells"] for entry in dispositions] != counts:
        raise ValueError("Catalog production cell counts differ from the requirements they classify.")
    expected = {
        "produced": sum(entry["cells"] for entry in production["producers"]),
        "unproduced": sum(entry["cells"] for entry in production["unproduced"]),
        "not_addressable": sum(not row["addressable_by_client"] for row in catalog["requirements"]),
    }
    if production["cells"] != expected:
        raise ValueError(f"Catalog production totals {production['cells']} differ from {expected}.")


def validate_grpc_scope(catalog: dict) -> None:
    """Every inventoried gRPC RPC is either a generated requirement or a ruled exclusion."""
    grpc = json.loads(
        (ROOT / "sources" / "geospatial-grpc" / "operations.v1.json").read_text(encoding="utf-8")
    )
    excluded = {f"{entry['service']}/{entry['operation']}" for entry in grpc.get("excluded_operations", [])}
    inventory = {f"{rpc['service']}/{rpc['operation']}" for rpc in grpc["operations"]}
    lanes = {"grpc-dotnet", "grpc-python", "grpc-typescript"}
    generated: dict[str, set[str]] = {}
    for row in catalog["requirements"]:
        if row["surface"] == "grpc" and row["client_lane"] in lanes:
            generated.setdefault(row["operation"], set()).add(row["client_lane"])
    leaked = sorted(set(generated) & excluded)
    if leaked:
        raise ValueError(f"Excluded gRPC operations carry generated requirements: {leaked}")
    expected = inventory - excluded
    if set(generated) != expected:
        raise ValueError(
            "Generated gRPC requirements differ from the in-scope inventory "
            f"(missing={sorted(expected - set(generated))}, unexpected={sorted(set(generated) - expected)})"
        )
    incomplete = sorted(operation for operation, found in generated.items() if found != lanes)
    if incomplete:
        raise ValueError(f"In-scope gRPC operations lack a client lane: {incomplete}")
    # The certified gRPC clients must be the ones the platform manifest ships.
    manifest = yaml.safe_load((ROOT.parent / "platform-manifest.yaml").read_text(encoding="utf-8"))
    shipped = manifest["components"]["geospatial-grpc"]
    published = grpc.get("published_clients") or {}
    for lane in sorted(lanes):
        version = (published.get(lane) or {}).get("version")
        if version != shipped["version"]:
            raise ValueError(
                f"gRPC lane {lane} certifies {version!r}, but platform-manifest ships "
                f"geospatial-grpc {shipped['version']!r}"
            )
    if (published.get("grpc-dotnet") or {}).get("digest") != shipped.get("artifactSha256"):
        raise ValueError("gRPC .NET lane digest differs from components.geospatial-grpc.artifactSha256")
    if grpc.get("source_sha") != shipped.get("artifactSourceRevision"):
        raise ValueError("gRPC contract revision differs from components.geospatial-grpc.artifactSourceRevision")
    lane_versions = {row["client_version"] for row in catalog["requirements"]
                     if row["surface"] == "grpc" and row["client_lane"] in lanes}
    if lane_versions != {shipped["version"]}:
        raise ValueError(f"Generated gRPC client versions {sorted(lane_versions)} differ from the shipped version")
    # The release gate compares a receipt to these rows. The rows themselves must carry the
    # published archive digest, not a well-formed substitute. This does not qualify a candidate.
    generator = _generator_module()
    for row in catalog["requirements"]:
        lane = row.get("client_lane")
        if row.get("surface") != "grpc" or lane not in lanes:
            continue
        expected_digest = generator.grpc_client_digest(published, lane)
        if row.get("client_package_digest") != expected_digest:
            raise ValueError(
                f"gRPC requirement {lane} {row.get('operation')} digest differs from the published package"
            )


if __name__ == "__main__":
    main()
