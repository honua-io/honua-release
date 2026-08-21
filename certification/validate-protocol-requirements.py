#!/usr/bin/env python3
"""Validate the generated protocol certification requirements catalog."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema

ROOT = Path(__file__).parent


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
    applicability = json.loads(
        (ROOT / "sources" / "canonical-client-applicability.v1.json").read_text(encoding="utf-8")
    )
    server = json.loads(
        (ROOT / "sources" / "server" / "capability-matrix.v1.json").read_text(encoding="utf-8")
    )
    jsonschema.validate(catalog, schema)
    if catalog["source_revisions"] != revisions:
        raise ValueError("Catalog source revisions differ from the pinned source manifest.")
    if catalog["complete"] is not True:
        raise ValueError("Protocol certification denominator is not declared complete.")
    keys = [
        (row["capability_key"], row["surface"], row["operation"], row["canonical_client"], row["client_lane"])
        for row in catalog["requirements"]
    ]
    if len(keys) != len(set(keys)):
        raise ValueError("Protocol certification requirements contain duplicate cells.")
    required_surfaces = {"sdk-python", "sdk-js", "feature-server", "ogc", "cog", "hdf5-netcdf", "zarr"}
    missing = required_surfaces - {row["surface"] for row in catalog["requirements"]}
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
    expected_sdk_protocols = {
        (
            capability_key,
            client["name"],
            client["version"],
        )
        for capability_key in sdk_protocols["capabilities"]
        for client in sdk_protocols["clients"]
    }
    present_sdk_protocols = {
        (
            row["capability_key"],
            row["canonical_client"],
            row["client_version"],
        )
        for row in catalog["requirements"]
        if row["contract_revision"]
        == f"official-sdk-protocol-assignments@{sdk_protocols['revision']}"
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
    unassigned_capabilities = set(applicability["unassigned_capabilities"])
    if len(unassigned_capabilities) != len(applicability["unassigned_capabilities"]):
        raise ValueError("Canonical-client applicability contains duplicate unassigned capabilities.")
    overlap = declared_protocols & unassigned_capabilities
    if overlap:
        raise ValueError(
            "Capabilities cannot be both SDK-assigned and canonical-client-unassigned: "
            f"{sorted(overlap)}"
        )
    classified_capabilities = declared_protocols | unassigned_capabilities
    if implemented_capabilities != classified_capabilities:
        raise ValueError(
            "Canonical-client applicability differs from the implemented capability surface "
            f"(unclassified={sorted(implemented_capabilities - classified_capabilities)}, "
            f"unexpected={sorted(classified_capabilities - implemented_capabilities)})"
        )
    present_unassigned = {
        row["capability_key"]
        for row in catalog["requirements"]
        if row["canonical_client"] == "UNASSIGNED CANONICAL CLIENT"
        and row["client_version"] == "pending-3387"
        and row["contract_revision"]
        == f"canonical-client-applicability@{applicability['revision']}"
    }
    if unassigned_capabilities != present_unassigned:
        raise ValueError(
            "Protocol certification denominator is missing canonical-client assignment blockers "
            f"(missing={sorted(unassigned_capabilities - present_unassigned)}, "
            f"unexpected={sorted(present_unassigned - unassigned_capabilities)})"
        )
    print(f"Validated {len(keys)} complete, unique protocol certification cells.")


if __name__ == "__main__":
    main()
