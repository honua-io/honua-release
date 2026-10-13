"""Measure the owned PostGIS fixture before executing the datasource publication playbook."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from uuid import uuid4

DOCUMENT = "honua-site-docs-playbooks-publish-a-service-index"
SQL = """SELECT json_build_object(
 'count', count(*),
 'validGeometryCount', count(*) FILTER (WHERE geom IS NOT NULL AND ST_SRID(geom)=3857 AND GeometryType(geom)='POLYGON'),
 'integerPrimaryKey', EXISTS (SELECT 1 FROM pg_constraint c JOIN pg_attribute a
 ON a.attrelid=c.conrelid AND c.conkey=ARRAY[a.attnum]
 WHERE c.conrelid='public.e2e_layer_src'::regclass AND c.contype='p'
 AND a.attname='id' AND a.atttypid='integer'::regtype)) FROM public.e2e_layer_src;"""


def publication_inputs(root: Path) -> tuple[dict[str, str], dict]:
    """No publication endpoint supplies the expected count; missing/changed fixtures refuse."""
    compose = ["docker", "compose", "-f", str(root / "e2e/harness/compose.candidate.yml")]
    def execute(args):
        result = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            raise ValueError("publication fixture input probe failed")
        try:
            return json.loads(result.stdout)
        except (TypeError, ValueError) as exc:
            raise ValueError("publication fixture input probe returned invalid JSON") from exc

    config = execute([*compose, "config", "--format", "json"])
    services = config.get("services", {})
    db = services.get("db", {}).get("environment", {})
    values = [db.get(key) for key in ("POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")]
    if not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in values):
        raise ValueError("publication fixture requires the owned compose database credentials")
    database, user, password = values
    connection = services.get("server", {}).get("environment", {}).get("ConnectionStrings__DefaultConnection", "")
    expected = f"Host=db;Port=5432;Database={database};Username={user};Password={password}"
    if connection != expected:
        raise ValueError("publication fixture database does not match the candidate server connection")
    observation = execute([*compose, "exec", "-T", "db", "psql", "-X", "-v", "ON_ERROR_STOP=1",
                           "-U", user, "-d", database, "-At", "-c", SQL])
    count = observation.get("count") if isinstance(observation, dict) else None
    if (type(count) is not int or not 1 <= count <= 1000
            or type(observation.get("validGeometryCount")) is not int
            or observation.get("validGeometryCount") != count
            or observation.get("integerPrimaryKey") is not True):
        raise ValueError("publication fixture requires 1–1000 Polygon/3857 rows and integer id primary key")
    suffix = uuid4().hex[:12]
    context = {"publish.databaseHost": "db", "publish.databasePort": "5432", "publish.databaseName": database,
               "publish.databaseUser": user, "publish.databasePassword": password,
               "publish.databaseSslMode": "Disable", "publish.databaseSslRequired": "false",
               "publish.connectionName": f"execdocs-{suffix}", "publish.serviceName": f"execdocs-{suffix}",
               "publish.layerName": f"execdocs-{suffix}", "publish.sourceSchema": "public",
               "publish.sourceTable": "e2e_layer_src", "publish.geometryColumn": "geom",
               "publish.primaryKey": "id", "publish.sourceSrid": "3857", "publish.expectedSourceCount": str(count)}
    evidence = {"scope": "owned compose PostGIS source measured before publication",
                "schema": "public", "table": "e2e_layer_src", "primaryKey": "id",
                "geometryColumn": "geom", "srid": 3857, "observation": observation,
                "connectionName": context["publish.connectionName"], "serviceName": context["publish.serviceName"],
                "layerName": context["publish.layerName"]}
    return context, evidence
