#!/usr/bin/env python3
"""Validate the generated protocol certification requirements catalog."""

from __future__ import annotations

import json
import re
from pathlib import Path

import jsonschema

ROOT = Path(__file__).parent
SUPPORTED = {"implemented", "partial", "covered"}
FIXTURE = "docker/cng/seed.sql@{source_sha}"


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


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
    rows: dict[tuple[str, str, str], list[dict]] = {}
    for row in catalog["requirements"]:
        if row["canonical_client"] in clients:
            rows.setdefault((row["canonical_client"], row["surface"], row["operation"]), []).append(row)
    if set(rows) != set(cell_keys):
        raise ValueError(
            "Bounded client roster differs from the generated bounded rows "
            f"(missing={sorted(set(rows) - set(cell_keys))}, unexpected={sorted(set(cell_keys) - set(rows))})"
        )
    for cell, key in zip(cells, cell_keys):
        if len(rows[key]) != 1:
            raise ValueError(f"Bounded client roster cell {key} matches {len(rows[key])} requirements.")
        row = rows[key][0]
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
        if group in bounded_groups:
            if not row.get("test_ids"):
                raise ValueError(f"Requirement sharing a bounded receipt group declares no test_ids: {group}")
            for test_id in row["test_ids"]:
                claims[(*group, test_id)] = claims.get((*group, test_id), 0) + 1
    ambiguous = sorted(claim for claim, count in claims.items() if count > 1)
    if ambiguous:
        raise ValueError(f"Bounded test IDs resolve to more than one requirement: {ambiguous}")


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
    bounded_lanes = {
        (cell["canonical_client"], cell["surface"], cell["operation"]): cell["client_lane"]
        for cell in bounded_roster["cells"]
    }
    jsonschema.validate(catalog, schema)
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
    licensed_policies = {
        "honua-pro-feature-subscriptions-v1": ("licensed-release", "api-key-protected-v1"),
        "esri-arcgis-pro-arcpy-v1": ("windows-licensed", "anonymous-and-protected-v1"),
    }
    for row in catalog["requirements"]:
        policy = row.get("entitlement_policy_revision")
        if row["licensed"]:
            expected = licensed_policies.get(policy)
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
    for assignment in assignments["assignments"]:
        for client_id in assignment["clients"]:
            client = assignments["clients"][client_id]
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
    if len(harness_assignments) != 32:
        raise ValueError("Server protocol harness must govern exactly 32 public operations.")
    allowed_assignment_fields = {
        "capability_key", "catalog_capability_key", "surface", "operation", "test_ids",
        "scenario_facets",
    }
    required_assignment_fields = allowed_assignment_fields - {"catalog_capability_key"}
    for assignment in harness_assignments:
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
        if not sdk_has_entrypoints(capability)
    }
    actual_harness_capabilities = {
        assignment["capability_key"] for assignment in harness_assignments
    }
    if actual_harness_capabilities != expected_harness_capabilities:
        raise ValueError(
            "Server protocol harness assignments differ from the operation-contract gaps "
            f"(missing={sorted(expected_harness_capabilities - actual_harness_capabilities)}, "
            f"unexpected={sorted(actual_harness_capabilities - expected_harness_capabilities)})"
        )
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
    classified_capabilities = declared_protocols | decision_capabilities
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
    sdk_fixtures = {
        "sdk-js": "0.2.0-alpha.1",
        "sdk-python": "geospatial-grpc@0.2.0-alpha.1",
        "sdk-dotnet": "sha256:83eb29ac38a3fb54914c1252b273dbb7f7f4d651a8204aafb4108d14d6d23727",
    }
    expected_decision_rows: list[dict] = []
    for decision in decisions:
        capability_key = decision["capability_key"]
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
                for assignment in harness_assignments:
                    if assignment["capability_key"] != capability_key:
                        continue
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
        if row["capability_key"] in decision_capabilities
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
    print(f"Validated {len(keys)} complete, unique protocol certification cells.")


if __name__ == "__main__":
    main()
