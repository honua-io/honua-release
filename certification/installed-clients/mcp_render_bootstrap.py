"""Provision the authored render fixture on an owned disposable CI candidate.

Uses the existing installed-client SQL generator and admin publication contract.
No operation exercised here receives canonical MCP client credit.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import regression


def inspect_candidate(candidate_source_sha, candidate_image, *, run=subprocess.run):
    """Read only selected safe Docker fields; never retain container env/secrets."""
    container_format = ('{"running":{{json .State.Running}},"imageId":{{json .Image}},'
                        '"imageReference":{{json .Config.Image}},"startedAt":{{json .State.StartedAt}},'
                        '"containerId":{{json .Id}}}')
    image_format = ('{"imageId":{{json .Id}},"repoDigests":{{json .RepoDigests}},'
                    '"sourceSha":{{json (index .Config.Labels "org.opencontainers.image.revision")}}}')

    def inspect(arguments):
        result = run(arguments, text=True, capture_output=True, timeout=30)
        if result.returncode:
            raise ValueError("owned candidate Docker identity inspection failed")
        return json.loads(result.stdout)

    container = inspect(["docker", "inspect", "--format", container_format, "honua-server"])
    if (not isinstance(container, dict) or container.get("running") is not True
            or container.get("imageReference") != candidate_image
            or not isinstance(container.get("imageId"), str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", container["imageId"])):
        raise ValueError("running owned container differs from the intended exact image")
    image = inspect(["docker", "image", "inspect", "--format", image_format, container["imageId"]])
    if (not isinstance(image, dict) or image.get("imageId") != container["imageId"]
            or image.get("sourceSha") != candidate_source_sha
            or not isinstance(image.get("repoDigests"), list) or candidate_image not in image["repoDigests"]):
        raise ValueError("started image lacks the exact digest and OCI source binding")
    return {"schema": "honua.mcp-render-runtime/v1", "candidateSourceSha": candidate_source_sha,
            "candidateImage": candidate_image, **container}


def provision(fixture_bytes, *, candidate_source_sha, candidate_image, database_password,
              api_key, base_url, runtime_binding, psql_run=subprocess.run, api_factory=regression.AdminApi, receipt=None):
    receipt = receipt if receipt is not None else {}
    if (not isinstance(candidate_source_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", candidate_source_sha)
            or not isinstance(candidate_image, str) or not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", candidate_image)):
        raise ValueError("bootstrap requires the exact candidate image and source")
    if (not isinstance(runtime_binding, dict) or runtime_binding.get("schema") != "honua.mcp-render-runtime/v1"
            or runtime_binding.get("running") is not True
            or runtime_binding.get("candidateSourceSha") != candidate_source_sha
            or runtime_binding.get("candidateImage") != candidate_image
            or runtime_binding.get("imageReference") != candidate_image
            or not isinstance(runtime_binding.get("imageId"), str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", runtime_binding["imageId"])
            or not isinstance(runtime_binding.get("containerId"), str) or not re.fullmatch(r"[a-f0-9]{64}", runtime_binding["containerId"])
            or not isinstance(runtime_binding.get("startedAt"), str) or not runtime_binding["startedAt"]):
        raise ValueError("bootstrap lacks the running owned container identity readback")
    url = urlsplit(base_url)
    if (url.scheme != "http" or url.hostname not in ("localhost", "127.0.0.1", "::1")
            or url.username or url.password or url.query or url.fragment or url.path not in ("", "/")):
        raise ValueError("bootstrap is restricted to the owned loopback CI candidate")
    if not isinstance(database_password, str) or not database_password or not isinstance(api_key, str) or not api_key:
        raise ValueError("bootstrap requires credential environment variables")
    fixture = json.loads(fixture_bytes)
    # Connection coordinates are CI topology, not authored geometry. Keep the
    # original fixture bytes/hash and change only the publish connection copy.
    publication_fixture = copy.deepcopy(fixture)
    database = {"host": "localhost", "port": 5432, "databaseName": "honua_test", "username": "honua", "provider": "PostGIS"}
    publication_fixture["lifecycle"]["database"] = database
    sql = regression.seed_sql(fixture, [], [])
    receipt.update({"schema": "honua.mcp-render-bootstrap/v1", "status": "preparing",
                    "candidateSourceSha": candidate_source_sha, "candidateImage": candidate_image,
                    "fixtureSha256": "sha256:" + hashlib.sha256(fixture_bytes).hexdigest(),
                    "seedSqlSha256": "sha256:" + hashlib.sha256(sql.encode()).hexdigest(),
                    "database": database, "baseUrl": base_url.rstrip("/"), "runtimeBinding": runtime_binding})
    command = ["psql", "-X", "--no-password", "-h", "localhost", "-p", "5432", "-U", "honua", "-d", "honua_test",
               "-v", "ON_ERROR_STOP=1", "-Atq"]
    environment = {**os.environ, "PGPASSWORD": database_password}

    def execute(query):
        result = psql_run(command, input=query, text=True, capture_output=True, env=environment, timeout=60)
        if result.returncode:
            # Database/server messages can include credentials or SQL values;
            # report the phase, retaining no secret-bearing stderr.
            raise ValueError("candidate fixture SQL failed")
        return result.stdout

    receipt["phase"] = "seed"
    execute(sql)
    table = fixture["area"]["table"]
    if table != "sdkreg_area":
        raise ValueError("bootstrap refuses an unauthored area table")
    receipt["phase"] = "independent-database-readback"
    rows = json.loads(execute("SELECT COALESCE(json_agg(row_to_json(r) ORDER BY gid),'[]'::json)::text FROM ("
                              "SELECT gid, name, ARRAY[ST_XMin(Box2D(geom)),ST_YMin(Box2D(geom)),"
                              "ST_XMax(Box2D(geom)),ST_YMax(Box2D(geom))] AS envelope, ST_SRID(geom) AS srid,"
                              "ST_GeometryType(geom) AS \"geometryType\" FROM honua_data.sdkreg_area) r;"))
    expected = sorted([{"gid": row["gid"], "name": row["name"], "envelope": row["envelope"],
                        "srid": 4326, "geometryType": "ST_Polygon"} for row in fixture["area"]["features"]], key=lambda row: row["gid"])
    if rows != expected:
        raise ValueError("candidate fixture readback differs from authored geometry")
    receipt["areaReadback"] = rows
    receipt["phase"] = "publish"
    api = api_factory(base_url, api_key)

    class RetainConnection:
        def request(self, method, path, body):
            result = api.request(method, path, body)
            if method == "POST" and path == "/api/v1/admin/connections/":
                receipt["connectionId"] = result.get("connectionId") or result.get("id")
            return result

    published = regression.publish_harness(RetainConnection(), publication_fixture, [], database_password)
    area = published.get("area", {})
    if (area.get("service") != fixture["area"]["service"] or type(area.get("layerId")) is not int
            or not 0 <= area["layerId"] < 2147483647):
        raise ValueError("candidate publication lacks the allocated area layer")
    receipt["published"] = published
    receipt["phase"] = "independent-publication-readback"
    # A public service name is an alias; render output uses the allocated
    # canonical service ID. Bind that ID to this publication's storage layer and
    # owned table/connection instead of guessing an ID or selecting any layer.
    rows = json.loads(execute("SELECT COALESCE(jsonb_agg(DISTINCT jsonb_build_object("
        "'serviceId',svc#>>'{metadata,id}','serviceName',svc#>>'{metadata,name}',"
        "'layerId',(pub#>>'{identifier,value}')::integer,'storageLayerId',(binding->>'storageLayerId')::integer,"
        "'locator',binding->>'locator','connectionId',binding->>'connectionId')),'[]'::jsonb)::text "
        "FROM honua.metadata_v2_current active JOIN honua.metadata_v2_snapshots snapshot "
        "ON snapshot.environment=active.environment AND snapshot.revision=active.revision "
        "CROSS JOIN LATERAL jsonb_array_elements(snapshot.document->'services') svc "
        "CROSS JOIN LATERAL jsonb_array_elements(snapshot.document->'publications') pub "
        "CROSS JOIN LATERAL jsonb_array_elements(snapshot.document->'storageBindings') binding "
        "WHERE svc#>>'{metadata,name}'='sdkreg-area' AND pub->>'serviceId'=svc#>>'{metadata,id}' "
        "AND pub->>'isPrimary'='true' AND pub->>'storageBindingId'=binding#>>'{metadata,id}' "
        f"AND (binding->>'storageLayerId')::integer={area['layerId']};"))
    if (not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict)
            or rows[0].get("serviceName") != fixture["area"]["service"]
            or not isinstance(rows[0].get("serviceId"), str) or not rows[0]["serviceId"]
            or type(rows[0].get("layerId")) is not int or not 0 <= rows[0]["layerId"] < 2147483647
            or rows[0].get("storageLayerId") != area["layerId"]
            or rows[0].get("locator") != "honua_data.sdkreg_area"
            or not isinstance(receipt.get("connectionId"), str) or not receipt["connectionId"]
            or rows[0].get("connectionId") != receipt["connectionId"]):
        raise ValueError("published area is not bound to the owned authored table and connection")
    receipt.update({"ownedArea": rows[0], "phase": "complete", "status": "provisioned"})
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--candidate-source-sha", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--owned-disposable-candidate", action="store_true", required=True)
    args = parser.parse_args()
    receipt = {"schema": "honua.mcp-render-bootstrap/v1", "status": "failed", "phase": "preflight"}
    try:
        provision(args.fixture.read_bytes(), candidate_source_sha=args.candidate_source_sha,
                  candidate_image=args.candidate_image, database_password=os.environ.get("HONUA_CERT_DB_PASSWORD"),
                  api_key=os.environ.get("HONUA_MCP_API_KEY"), base_url=args.base_url, receipt=receipt,
                  runtime_binding=inspect_candidate(args.candidate_source_sha, args.candidate_image))
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError, regression.RegressionError) as exc:
        receipt.update({"status": "failed", "errorType": type(exc).__name__})
        parser.exit(1, f"render fixture bootstrap refused: {type(exc).__name__}\n")
    finally:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
