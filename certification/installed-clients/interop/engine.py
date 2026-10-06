"""Candidate preparation and the driver plan for the cross-client interop scenarios.

Harness setup is fixture preparation, not a client under test: it seeds the handoff table and
creates the datasource the publishing clients publish through, using SQL and the admin REST API
directly. Everything a scenario does goes through the client named on each step.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
SCHEMA = "honua_data"


def seed_sql(rx: Any, fixture: dict[str, Any], tag: str) -> str:
    """The handoff table the .NET SDK publishes (the area table is created by the CLI's import)."""
    handoff = fixture["handoff"]
    return f"CREATE SCHEMA IF NOT EXISTS {SCHEMA};\n" + rx._point_table(
        handoff["tablePrefix"] + tag, handoff["fields"], handoff["features"])


def prepare(rx: Any, api: Any, fixture: dict[str, Any], tag: str, db_password: str, psql: list[str] | None = None) -> dict[str, Any]:
    """Seed the handoff table and create the harness datasource; returns the context the plan needs."""
    proc = subprocess.run([*(psql or rx.default_psql()), "-v", "ON_ERROR_STOP=1", "-q"], input=seed_sql(rx, fixture, tag),
                          text=True, capture_output=True, check=False)
    if proc.returncode:
        raise rx.RegressionError(f"interop fixture SQL failed: {rx.scrub(proc.stderr[-1000:])}")
    connection = fixture["connection"]
    created = api.request("POST", "/api/v1/admin/connections/", {
        "name": connection["namePrefix"] + tag, **connection["database"], "password": db_password,
        "sslRequired": False, "sslMode": "Disable"})
    connection_id = created.get("connectionId") or created.get("id") if isinstance(created, dict) else None
    if not connection_id:
        raise rx.RegressionError("interop harness datasource has no id")
    return {"connectionId": connection_id, "tag": tag}


def build_plan(fixture: dict[str, Any], suite_fixture: dict[str, Any], context: dict[str, Any], published: dict[str, Any],
               scenario_ids: list[str], scenarios: dict[str, dict[str, Any]], base_url: str,
               principals: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    """Everything the orchestrator needs except credentials, which travel in the environment."""
    import judge
    tag = context["tag"]
    handoff, area, proposal, identity = (fixture[key] for key in ("handoff", "area", "proposal", "identity"))
    sites_layer = published["sites"]["layerId"]
    return {
        "schemaVersion": 1,
        "baseUrl": base_url.rstrip("/"),
        "tag": tag,
        "connectionId": context["connectionId"],
        "scenarios": [{"id": sid, "steps": [step["id"] for step in scenarios[sid]["steps"]]} for sid in scenario_ids],
        "sites": {"service": published["sites"]["service"], "layerId": sites_layer, "collectionId": str(sites_layer)},
        "handoff": {"schema": SCHEMA, "table": handoff["tablePrefix"] + tag, "service": handoff["servicePrefix"] + tag,
                    "layerName": handoff["layerName"], "geometryType": handoff["geometryType"],
                    "fields": list(handoff["fields"]), "edit": handoff["edit"]},
        "area": {"tableName": area["tableNamePrefix"] + tag, "service": area["servicePrefix"] + tag,
                 "layerName": area["layerName"], "geometryType": area["geometryType"], "fileName": area["fileName"],
                 "features": area["features"], "render": area["render"], "buffer": area["buffer"]},
        "proposal": {"packageKey": proposal["packageKeyPrefix"] + tag, "route": proposal["routePrefix"] + tag,
                     "visibility": proposal["visibility"],
                     "envelope": {**proposal["envelope"], "body": judge.bound_map_body(fixture, str(sites_layer))},
                     "proposalPollSeconds": proposal["proposalPollSeconds"],
                     "publicationPollSeconds": proposal["publicationPollSeconds"]},
        "identity": {"keyName": identity["keyNamePrefix"] + tag, "permissions": identity["permissions"],
                     "siteCount": len(suite_fixture["sites"]["features"]), "revocation": identity["revocation"]},
        "principals": {f"{name}Id": value["id"] for name, value in (principals or {}).items()},
    }

