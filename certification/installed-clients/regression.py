"""Published-client regression suite: published SDKs, CLIs and the MCP proxy drive the candidate.

The suite has three parts.

* ``scenarios/*.json`` is the scenario contract. It lists each scenario's steps and oracle kinds,
  and names, per client, the SDK API, command or MCP request each step must go through. The id's
  prefix is the scenario family (``sdk-``, ``cli-``, ``mcp-``), which fixes the clients it names.
* ``drivers/<client>/`` holds one program per client. A driver runs only against the installed
  published package. It calls the SDK's own client classes, the installed command or the
  installed MCP proxy, never raw HTTP, and prints one JSON observation per step.
* This module seeds ``fixture.v1.json`` into the candidate's database and publishes the harness
  layers. It writes each driver's plan, runs the driver, and judges every observation with
  ``oracles.py``.

Harness setup (seeding tables, publishing the shared read-only layers and the managed edit layer,
minting the proposer and approver principals) uses the admin REST API directly. It is fixture
preparation, not the client under test.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

import oracles

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / "fixture.v1.json"
SCENARIOS = HERE / "scenarios"
# Driver -> the slug that names its per-client fixture tables and services.
SUITE_DRIVERS = {"pypi-sdk": "python", "npm-sdk": "js", "nuget-sdk": "dotnet",
                 "npm-cli": "clijs", "pypi-cli": "clipy", "npm-mcp-workflow": "mcp"}
# Scenario family (the scenario id's prefix) -> the client artifacts that each name one API per step.
FAMILIES = {
    "sdk": ("honua-sdk-python-wheel", "honua-sdk-js", "honua-sdk-dotnet"),
    "cli": ("honua-sdk-js", "honua-sdk-python-wheel"),
    "mcp": ("honua-mcp-server",),
}
# Driver -> (family, artifact): which scenarios a driver runs and for which published client.
DRIVER_CLIENTS = {
    "pypi-sdk": ("sdk", "honua-sdk-python-wheel"), "npm-sdk": ("sdk", "honua-sdk-js"), "nuget-sdk": ("sdk", "honua-sdk-dotnet"),
    "npm-cli": ("cli", "honua-sdk-js"), "pypi-cli": ("cli", "honua-sdk-python-wheel"),
    "npm-mcp-workflow": ("mcp", "honua-mcp-server"),
}
# The workflow principals the harness mints for the proposal steps: the proposer may publish, the
# approver may only approve. Their keys travel in the driver's environment, never in the plan.
PRINCIPAL_GRANTS = {"proposer": ["admin:write"], "approver": ["admin:approve"]}
BEARER_ISSUER = "https://sdk-regression.invalid"
BEARER_AUDIENCE = "sdk-regression"
BEARER_TENANT = "default"
# The only fields a driver observation may carry into evaluation. Anything else is dropped.
OBSERVATION_KEYS = {"scenario", "step", "api", "observed", "error", "unsupported", "skipped"}
# Oracles that judge the whole observation, because the expected outcome is an error.
ERROR_ORACLES = {"refused", "not-found", "self-approval-refused", "mcp-permission-denied"}
SECRET_SHAPES = re.compile(
    r"(?i)(authorization|x-api-key|password|secret|token|apikey|api_key)(\s*[=:]\s*)\S+|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_.-]+"
)


class RegressionError(RuntimeError):
    pass


# ── contract ─────────────────────────────────────────────────────────────────────────────────


def load_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    return json.loads(path.read_text())


def load_scenarios(directory: Path = SCENARIOS) -> dict[str, dict[str, Any]]:
    scenarios: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("*.json")):
        scenario = json.loads(path.read_text())
        validate_scenario(scenario, path.name)
        if scenario["id"] in scenarios:
            raise RegressionError(f"duplicate scenario id {scenario['id']!r}")
        scenarios[scenario["id"]] = scenario
    if not scenarios:
        raise RegressionError("no scenario contracts found")
    return scenarios


def scenario_family(scenario_id: str) -> str | None:
    family = str(scenario_id).split("-", 1)[0]
    return family if family in FAMILIES and re.fullmatch(r"[a-z]+-[a-z0-9-]+", str(scenario_id)) else None


def validate_scenario(scenario: dict[str, Any], name: str) -> None:
    family = scenario_family(scenario.get("id", ""))
    if scenario.get("schemaVersion") != 1 or family is None:
        raise RegressionError(f"{name}: scenario needs schemaVersion 1 and an sdk-*, cli-* or mcp-* id")
    steps = scenario.get("steps")
    if not isinstance(steps, list) or not steps:
        raise RegressionError(f"{name}: scenario has no steps")
    ids = [step.get("id") for step in steps]
    if len(set(ids)) != len(ids) or any(not isinstance(step.get("oracle"), str) or step["oracle"] not in ORACLES for step in steps):
        raise RegressionError(f"{name}: steps need unique ids and a known oracle")
    if scenario.get("receiptFields") != ["client", "version", "integrity", "scenario", "step", "api", "status", "oracle"]:
        raise RegressionError(f"{name}: receiptFields must be the allowlist")
    clients = scenario.get("clients")
    if not isinstance(clients, dict) or set(clients) != set(FAMILIES[family]):
        raise RegressionError(f"{name}: every {family} client must name its API for each step")
    for client, apis in clients.items():
        if not isinstance(apis, dict) or set(apis) != set(ids) or not all(isinstance(v, str) and v for v in apis.values()):
            raise RegressionError(f"{name}: client {client} must name one API per step")


def validate_suite_cell(cell: dict[str, Any], scenarios: dict[str, dict[str, Any]], blocker: re.Pattern[str]) -> None:
    """A suite cell names its scenario; a blocked one maps each blocked step to its issue and signature."""
    cell_id = cell["id"]
    scenario = scenarios.get(cell.get("scenario"))
    if scenario is None:
        raise RegressionError(f"{cell_id}: unknown scenario {cell.get('scenario')!r}")
    if DRIVER_CLIENTS.get(cell.get("driver")) != (scenario_family(scenario["id"]), cell.get("artifact")):
        raise RegressionError(f"{cell_id}: driver {cell.get('driver')!r} does not drive artifact {cell.get('artifact')!r} "
                              f"through {scenario['id']}")
    blocked = cell.get("blockedSteps")
    if cell["status"] != "blocked":
        if blocked is not None:
            raise RegressionError(f"{cell_id}: an active cell cannot carry blockedSteps")
        return
    step_ids = {step["id"] for step in scenario["steps"]}
    if not isinstance(blocked, dict) or not blocked or not set(blocked) <= step_ids:
        raise RegressionError(f"{cell_id}: a blocked suite cell must name blocked steps of its scenario")
    for step_id, entry in blocked.items():
        if (not isinstance(entry, dict) or set(entry) != {"blockedBy", "signature"}
                or not blocker.fullmatch(str(entry["blockedBy"])) or not isinstance(entry["signature"], str) or not entry["signature"]):
            raise RegressionError(f"{cell_id}: blocked step {step_id} needs a blockedBy issue URL and an observed signature")
        re.compile(entry["signature"])
    if cell.get("blockedBy") != sorted({entry["blockedBy"] for entry in blocked.values()}):
        raise RegressionError(f"{cell_id}: blockedBy must list exactly the blocked steps' issues, sorted")


# ── candidate preparation ────────────────────────────────────────────────────────────────────


def _sql_literal(value: Any) -> str:
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    return repr(value)


def _point_table(table: str, fields: dict[str, str], rows: list[dict[str, Any]]) -> str:
    columns = ", ".join(f"{name} {kind}" + (" PRIMARY KEY" if name == "gid" else " NOT NULL") for name, kind in fields.items())
    values = ",\n ".join(
        "(" + ", ".join(_sql_literal(row[name]) for name in fields) + f", ST_SetSRID(ST_MakePoint({row['x']!r}, {row['y']!r}), 4326))"
        for row in rows
    )
    return (f"DROP TABLE IF EXISTS honua_data.{table};\n"
            f"CREATE TABLE honua_data.{table} ({columns}, geom geometry(Point,4326));\n"
            f"INSERT INTO honua_data.{table} ({', '.join(fields)}, geom) VALUES\n {values};\n")


def seed_sql(fixture: dict[str, Any], slugs: list[str], workflow_slugs: list[str] = ()) -> str:
    """Deterministic SQL for the shared tables, one edits + lifecycle table per SDK client and one
    lifecycle + proposal table per command-line client."""
    area = fixture["area"]
    polygons = ",\n ".join(
        f"({row['gid']}, {_sql_literal(row['name'])}, ST_SetSRID(ST_MakeEnvelope({', '.join(map(repr, row['envelope']))}), 4326))"
        for row in area["features"]
    )
    parts = [
        "CREATE SCHEMA IF NOT EXISTS honua_data;\n",
        _point_table(fixture["sites"]["table"], fixture["sites"]["fields"], fixture["sites"]["features"]),
        f"DROP TABLE IF EXISTS honua_data.{area['table']};\n"
        f"CREATE TABLE honua_data.{area['table']} (gid integer PRIMARY KEY, name text NOT NULL, geom geometry(Polygon,4326));\n"
        f"INSERT INTO honua_data.{area['table']} (gid, name, geom) VALUES\n {polygons};\n",
    ]
    for slug in slugs:
        parts.append(_point_table(fixture["edits"]["tablePrefix"] + slug, fixture["edits"]["fields"], fixture["edits"]["features"]))
        parts.append(_point_table(fixture["lifecycle"]["tablePrefix"] + slug, fixture["lifecycle"]["fields"], fixture["lifecycle"]["features"]))
    for slug in workflow_slugs:
        for key in ("lifecycle", "proposal"):
            parts.append(_point_table(fixture[key]["tablePrefix"] + slug, fixture[key]["fields"], fixture[key]["features"]))
    return "".join(parts)


class AdminApi:
    """Minimal admin REST access for harness setup only (never the client under test)."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url, self.api_key = base_url.rstrip("/"), api_key

    def request(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base_url + path, data=data, method=method, headers={
            "X-API-Key": self.api_key, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            raise RegressionError(f"harness {method} {path} returned HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise RegressionError(f"harness {method} {path} failed: {type(exc).__name__}") from None
        if isinstance(payload, dict) and payload.get("success") is False:
            raise RegressionError(f"harness {method} {path} was refused by the candidate")
        return payload.get("data") if isinstance(payload, dict) and "data" in payload else payload


def publish_harness(api: AdminApi, fixture: dict[str, Any], slugs: list[str], db_password: str) -> dict[str, Any]:
    """Create the harness datasource and publish the shared and per-client layers."""
    database = fixture["lifecycle"]["database"]
    connection = api.request("POST", "/api/v1/admin/connections/", {
        "name": "sdkreg-harness", **database, "password": db_password, "sslRequired": False, "sslMode": "Disable"})
    connection_id = connection.get("connectionId") or connection.get("id")
    if not connection_id:
        raise RegressionError("harness datasource has no id")

    def publish(table: str, service: str, layer: str, geometry: str, **extra: Any) -> int:
        published = api.request("POST", f"/api/v1/admin/connections/{connection_id}/layers", {
            "schema": "honua_data", "table": table, "layerName": layer, "serviceName": service,
            "geometryColumn": "geom", "geometryType": geometry, "primaryKey": "gid", "srid": 4326,
            "enabled": True, **extra})
        layer_id = published.get("layerId") if isinstance(published, dict) else None
        if type(layer_id) is not int:
            raise RegressionError(f"harness publish of {service} returned no layer id")
        return layer_id

    sites, area, edits = fixture["sites"], fixture["area"], fixture["edits"]
    published = {
        "sites": {"service": sites["service"], "layerId": publish(sites["table"], sites["service"], sites["layerName"], sites["geometryType"])},
        "area": {"service": area["service"], "layerId": publish(area["table"], area["service"], area["layerName"], area["geometryType"])},
        "edits": {},
    }
    for slug in slugs:
        service = edits["servicePrefix"] + slug
        # Editing needs a managed publication that declares its edit capabilities. None of the
        # published SDKs' publish requests can declare them yet, so the harness publishes it.
        published["edits"][slug] = {"service": service, "layerId": publish(
            edits["tablePrefix"] + slug, service, edits["layerName"], edits["geometryType"],
            storageMode="managed", capabilities=["Query", "Create", "Update", "Delete"])}
    return published


def mint_principals(api: AdminApi, tag: str) -> dict[str, dict[str, str]]:
    """Short-lived proposer and approver API keys. Returns {name: {"id", "key"}}; only ids enter the plan."""
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
    principals = {}
    for name, grants in PRINCIPAL_GRANTS.items():
        created = api.request("POST", "/api/v1/admin/api-keys/", {"name": f"sdkreg-{tag}-{name}", "permissions": grants,
                                                                 "expiresAt": expires})
        key_id = (created.get("apiKey") or {}).get("id") if isinstance(created, dict) else None
        key = created.get("key") if isinstance(created, dict) else None
        if not isinstance(key_id, str) or not key_id or not isinstance(key, str) or not key:
            raise RegressionError(f"harness could not mint the {name} principal")
        principals[name] = {"id": key_id, "key": key}
    return principals


def mint_bearer(signing_key: str, *, lifetime: int = 3600) -> str:
    """An HS256 operator token for the candidate's generic OIDC issuer (per-run key)."""
    def encode(value: dict[str, Any]) -> bytes:
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")
    now = int(time.time())
    body = encode({"alg": "HS256", "typ": "JWT"}) + b"." + encode({
        "iss": BEARER_ISSUER, "aud": BEARER_AUDIENCE, "sub": "sdk-regression-operator", "tenant_id": BEARER_TENANT,
        "role": "admin", "roles": ["admin"], "iat": now, "nbf": now, "exp": now + lifetime, "jti": secrets.token_hex(16)})
    signature = base64.urlsafe_b64encode(hmac.new(signing_key.encode(), body, hashlib.sha256).digest()).rstrip(b"=")
    return (body + b"." + signature).decode()


# ── plans ────────────────────────────────────────────────────────────────────────────────────


def build_plan(fixture: dict[str, Any], published: dict[str, Any], slug: str, scenario_ids: list[str],
               scenarios: dict[str, dict[str, Any]], base_url: str,
               principals: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    """Everything a driver needs except credentials, which travel in the environment."""
    sites, area, lifecycle, processes, proposal = (fixture[key] for key in ("sites", "area", "lifecycle", "processes", "proposal"))
    edits = fixture["edits"]
    edit_layer = published["edits"].get(slug) or {}
    in_bbox = oracles.sites_in_bbox(fixture)
    return {
        "schemaVersion": 1,
        "baseUrl": base_url.rstrip("/"),
        "client": slug,
        "scenarios": [{"id": sid, "steps": [step["id"] for step in scenarios[sid]["steps"]]} for sid in scenario_ids],
        "sites": {"service": published["sites"]["service"], "layerId": published["sites"]["layerId"],
                  "collectionId": str(published["sites"]["layerId"]), "where": oracles.where_clause(sites["filter"]),
                  "bbox": sites["bbox"], "itemId": str(in_bbox[0]["gid"]) if in_bbox else "1",
                  "fields": list(sites["fields"])},
        "area": {"collectionId": str(published["area"]["layerId"]), "service": published["area"]["service"],
                 "layerId": published["area"]["layerId"], **area["tiles"]},
        "edits": {"service": edit_layer.get("service"), "layerId": edit_layer.get("layerId"),
                  "fields": list(edits["fields"]), "add": edits["add"], "update": edits["update"],
                  "delete": edits["delete"], "attachment": edits["attachment"]},
        "lifecycle": {"connectionName": lifecycle["connectionPrefix"] + slug, "database": lifecycle["database"],
                      "table": lifecycle["tablePrefix"] + slug, "service": lifecycle["servicePrefix"] + slug,
                      "layerName": lifecycle["layerName"], "geometryType": lifecycle["geometryType"]},
        "processes": {key: processes[key] for key in ("processId", "point", "srid", "distance", "pollTimeoutSeconds")},
        "proposal": {"schema": "honua_data", "table": proposal["tablePrefix"] + slug, "service": proposal["servicePrefix"] + slug,
                     "layerName": proposal["layerName"], "geometryType": proposal["geometryType"]},
        "principals": {f"{name}Id": value["id"] for name, value in (principals or {}).items()},
        "mcp": {"render": fixture["mcp"]["render"]},
    }


# ── evaluation ───────────────────────────────────────────────────────────────────────────────


def scrub(text: str) -> str:
    return SECRET_SHAPES.sub(lambda m: (m.group(1) + m.group(2) + "***") if m.group(1) else "***", text)


def parse_observations(stdout: str) -> dict[tuple[str, str], dict[str, Any]]:
    observations: dict[tuple[str, str], dict[str, Any]] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("scenario"), str) or not isinstance(payload.get("step"), str):
            continue
        key = (payload["scenario"], payload["step"])
        if key in observations:
            # A step reported twice is ambiguous; never let a later line overwrite a failure.
            observations[key] = {"scenario": key[0], "step": key[1], "error": {"type": "DuplicateObservation"}}
            continue
        observations[key] = {name: value for name, value in payload.items() if name in OBSERVATION_KEYS}
    return observations


def _context_layer_id(observations: dict[tuple[str, str], dict[str, Any]], scenario: str) -> int | None:
    published = observations.get((scenario, "publish"), {}).get("observed") or {}
    return published.get("layerId") if type(published.get("layerId")) is int else None


def judge_step(scenario_id: str, step: dict[str, Any], observation: dict[str, Any] | None, fixture: dict[str, Any],
               plan: dict[str, Any], observations: dict[tuple[str, str], dict[str, Any]]) -> tuple[bool, str]:
    if observation is None:
        return False, "the driver produced no observation for this step (missing scenario step)"
    if "unsupported" in observation:
        return False, f"unsupported: the published client has no API for this step ({observation['unsupported']})"
    if "skipped" in observation:
        return False, f"not run: {observation['skipped']}"
    kind = step["oracle"]
    if kind in ERROR_ORACLES:
        return ORACLES[kind](observation, fixture, plan, observations, scenario_id)
    if "error" in observation:
        return False, oracles._error_summary(observation)
    observed = observation.get("observed")
    if not isinstance(observed, dict):
        return False, "the driver reported no observed value"
    return ORACLES[kind](observed, fixture, plan, observations, scenario_id)


def _layer_published(observed, fixture, plan, observations, scenario):
    lifecycle = plan["lifecycle"]
    got = (observed.get("serviceName"), observed.get("layerName"), observed.get("enabled"), type(observed.get("layerId")) is int)
    want = (lifecycle["service"], lifecycle["layerName"], True, True)
    return got == want, f"published service/layer/enabled/id {got[:3]} (integer id: {got[3]}), fixture expects {want[:3]}"


def _layer_listed(observed, fixture, plan, observations, scenario):
    layer_id = _context_layer_id(observations, scenario)
    rows = observed.get("layers") if isinstance(observed.get("layers"), list) else []
    match = [row for row in rows if isinstance(row, dict) and row.get("layerId") == layer_id]
    ok = layer_id is not None and len(match) == 1 and match[0].get("enabled") is True
    return ok, f"published layer {layer_id} listed {len(match)} time(s), enabled={match[0].get('enabled') if match else None}"


def _layer_disabled(observed, fixture, plan, observations, scenario):
    layer_id = _context_layer_id(observations, scenario)
    ok = layer_id is not None and observed.get("layerId") == layer_id and observed.get("enabled") is False
    return ok, f"layer {observed.get('layerId')} enabled={observed.get('enabled')!r} after unpublish"


def _observed_names(observations, scenario, step):
    names = (observations.get((scenario, step), {}).get("observed") or {}).get("names")
    return names if isinstance(names, list) else None


def _count(observed, fixture, plan, observations, scenario):
    step_expect = {
        ("sdk-auth", "api-key-query"): len(fixture["sites"]["features"]),
        ("sdk-auth", "bearer-query"): len(fixture["sites"]["features"]),
        ("sdk-geoservices", "count"): len(oracles.filtered_sites(fixture)),
        ("sdk-admin-lifecycle", "served"): len(fixture["lifecycle"]["features"]),
        ("cli-workflow", "served"): len(fixture["lifecycle"]["features"]),
    }
    return oracles.oracle_count(observed, step_expect[(scenario, observed.get("_step"))])


ORACLES: dict[str, Callable[..., tuple[bool, str]]] = {
    "count": _count,
    "contains-service": lambda o, f, p, obs, s: oracles.oracle_contains_service(o, f["sites"]["service"]),
    "refused": lambda o, f, p, obs, s: oracles.oracle_refused(o),
    "not-found": lambda o, f, p, obs, s: oracles.oracle_not_found(o),
    "datasource-created": lambda o, f, p, obs, s: (
        isinstance(o.get("connectionId"), str) and bool(o["connectionId"]),
        "datasource created with an id" if o.get("connectionId") else "datasource has no id"),
    "datasource-healthy": lambda o, f, p, obs, s: (o.get("success") is True, f"datasource test success={o.get('success')!r}"),
    "layer-published": _layer_published,
    "layer-listed": _layer_listed,
    "layer-disabled": _layer_disabled,
    "features": lambda o, f, p, obs, s: oracles._compare_features(o, oracles.filtered_sites(f), ["gid", "name", "rank"], "filtered query"),
    "object-ids": lambda o, f, p, obs, s: oracles.oracle_object_ids(o, [row["gid"] for row in oracles.filtered_sites(f)]),
    "edit-ids": lambda o, f, p, obs, s: oracles.oracle_edit_ids(o, f),
    "edit-success": lambda o, f, p, obs, s: oracles.oracle_edit_success(o),
    "edited-features": lambda o, f, p, obs, s: oracles._compare_features(o, oracles.edited_features(f), ["gid", "name", "rank"], "edits layer"),
    "attachments": lambda o, f, p, obs, s: oracles.oracle_attachments(o, f),
    "items": lambda o, f, p, obs, s: oracles.oracle_items(o, oracles.sites_in_bbox(f), "bbox"),
    "item": lambda o, f, p, obs, s: oracles.oracle_item(o, next(r for r in f["sites"]["features"] if str(r["gid"]) == p["sites"]["itemId"])),
    "vector-tile": lambda o, f, p, obs, s: oracles.oracle_vector_tile(o, f),
    "raster-tile": lambda o, f, p, obs, s: oracles.oracle_raster_tile(o, f),
    "empty-tile": lambda o, f, p, obs, s: oracles.oracle_empty_tile(o),
    "job-accepted": lambda o, f, p, obs, s: oracles.oracle_job_accepted(o),
    "job-succeeded": lambda o, f, p, obs, s: oracles.oracle_job_succeeded(o),
    "buffer-result": lambda o, f, p, obs, s: oracles.oracle_buffer(o, f),
    "proposal-pending": lambda o, f, p, obs, s: oracles.oracle_proposal_pending(o),
    "self-approval-refused": lambda o, f, p, obs, s: oracles.oracle_self_approval_refused(o),
    "proposal-approved": lambda o, f, p, obs, s: oracles.oracle_proposal_approved(o),
    "proposal-resolved": lambda o, f, p, obs, s: oracles.oracle_proposal_resolved(o, p.get("principals") or {}),
    "approved-features": lambda o, f, p, obs, s: oracles._compare_features(o, f["proposal"]["features"], ["gid", "name"], "approved publication"),
    "mcp-initialized": lambda o, f, p, obs, s: oracles.oracle_mcp_initialized(o, f),
    "mcp-setup-view": lambda o, f, p, obs, s: oracles.oracle_mcp_view(o, f, "setup"),
    "mcp-default-view": lambda o, f, p, obs, s: oracles.oracle_mcp_view(o, f, "default"),
    "mcp-permission-denied": lambda o, f, p, obs, s: oracles.oracle_mcp_permission_denied(o),
    "mcp-full-catalog": lambda o, f, p, obs, s: oracles.oracle_mcp_full_catalog(o, f, _observed_names(obs, s, "default-tools-list")),
    "map-render": lambda o, f, p, obs, s: oracles.oracle_map_render(o, f),
    "mcp-job-accepted": lambda o, f, p, obs, s: oracles.oracle_mcp_job_accepted(o),
    "mcp-job-succeeded": lambda o, f, p, obs, s: oracles.oracle_mcp_job_succeeded(o),
}


def evaluate_cell(cell: dict[str, Any], scenario: dict[str, Any], client_artifact: str,
                  observations: dict[tuple[str, str], dict[str, Any]], fixture: dict[str, Any],
                  plan: dict[str, Any], client: str) -> tuple[str, str, list[dict[str, Any]]]:
    """Judge every contract step against the matrix; returns (status, detail, step rows).

    ``pass``: every step passes and the matrix expects that. ``blocked``: every unblocked step
    passes and every matrix-blocked step fails with its declared signature. Anything else is
    ``fail``, including a blocked step that starts passing (the matrix must be flipped).
    """
    expected_api = scenario["clients"][client_artifact]
    blocked = cell.get("blockedSteps") or {}
    rows: list[dict[str, Any]] = []
    for step in scenario["steps"]:
        observation = observations.get((scenario["id"], step["id"]))
        if observation is not None and isinstance(observation.get("observed"), dict):
            observation = {**observation, "observed": {**observation["observed"], "_step": step["id"]}}
        passed, summary = judge_step(scenario["id"], step, observation, fixture, plan, observations)
        api = expected_api[step["id"]]
        if observation is not None and observation.get("api") != api:
            passed, summary = False, f"driver used {observation.get('api')!r}, the contract names {api!r}"
        status = "pass" if passed else "fail"
        row = {"step": step["id"], "api": api, "status": status}
        entry = blocked.get(step["id"])
        if entry:
            row["blockedBy"] = entry["blockedBy"]
            if passed:
                status = "fail"
                summary = f"{summary}; the matrix marks this step blocked by {entry['blockedBy']}: set it active"
            elif re.search(entry["signature"], summary):
                status = "blocked"
            else:
                summary = f"{summary}; the declared blocker signature /{entry['signature']}/ was not observed"
        row.update(status=status, oracle=scrub(summary))
        rows.append(row)
    failed = [row for row in rows if row["status"] == "fail"]
    if failed:
        return "fail", "; ".join(f"{client} `{row['api']}` {row['step']}: {row['oracle']}" for row in failed), rows
    blocked_rows = [row for row in rows if row["status"] == "blocked"]
    if blocked_rows:
        return "blocked", (f"{len(rows) - len(blocked_rows)} steps pass; blocked as declared: "
                           + ", ".join(f"{row['step']} ({row['blockedBy']})" for row in blocked_rows)), rows
    return "pass", f"{client}: {len(rows)} steps pass against the fixture oracles", rows


def run_driver(command: list[str], cwd: Path, env: dict[str, str], timeout: int = 1800) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return 124, exc.stdout or "", f"driver timed out after {timeout}s"
    except OSError as exc:
        return 127, "", f"driver could not start: {type(exc).__name__}"
    return proc.returncode, proc.stdout, proc.stderr


def log(message: str) -> None:
    print(scrub(message), file=sys.stderr)


def driver_env(base: dict[str, str], *, api_key: str, bearer: str, db_password: str, plan_path: Path,
               principals: dict[str, dict[str, str]] | None = None) -> dict[str, str]:
    env = {key: value for key, value in base.items() if not key.startswith("SDKREG_")}
    env.update({"SDKREG_PLAN": str(plan_path), "SDKREG_API_KEY": api_key, "SDKREG_BEARER": bearer,
                "SDKREG_DB_PASSWORD": db_password})
    for name, value in (principals or {}).items():
        env[f"SDKREG_{name.upper()}_KEY"] = value["key"]
    return env


def default_psql() -> list[str]:
    compose = os.environ.get("SDKREG_COMPOSE")
    if compose:
        return [*compose.split(), "exec", "-T", "db", "psql", "-U", "honua", "-d", "honua"]
    return ["docker", "compose", "-f", str(HERE.parents[1] / "e2e/harness/compose.candidate.yml"),
            "-f", str(HERE / "compose.sdk-regression.yml"), "exec", "-T", "db", "psql", "-U", "honua", "-d", "honua"]


def seed(fixture: dict[str, Any], slugs: list[str], psql: list[str] | None = None, workflow_slugs: list[str] = ()) -> None:
    proc = subprocess.run([*(psql or default_psql()), "-v", "ON_ERROR_STOP=1", "-q"], input=seed_sql(fixture, slugs, workflow_slugs),
                          text=True, capture_output=True, check=False)
    if proc.returncode:
        raise RegressionError(f"fixture SQL failed: {scrub(proc.stderr[-1000:])}")
