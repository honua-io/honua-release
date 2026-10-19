"""Owned style setup and independent committed state/restart observations."""
from __future__ import annotations
import argparse
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_style_validation import scenario, snapshot_output, setup_output, strict_json
from mcp_render_validation import inspect_candidate


def snapshot(fixture, authored, *, execute):
    """One statement preserves multiplicity and binds graph revision to its mapping."""
    area = authored["ownedArea"]
    def literal(value):
        return "'" + value.replace("'", "''") + "'"
    # All numeric/address values derive from the independently validated fixture.
    sql = ("WITH mappings AS (SELECT active.revision,svc#>>'{metadata,id}' AS \"serviceId\","
        "(pub#>>'{identifier,value}')::integer AS \"layerId\",(binding->>'storageLayerId')::integer AS \"storageLayerId\","
        "binding->>'locator' AS locator,binding->>'connectionId' AS \"connectionId\","
        "COALESCE(resource->'styleResourceIds','[]'::jsonb) AS \"styleResourceIds\","
        "COALESCE((SELECT jsonb_agg(jsonb_build_object('id',s#>>'{metadata,id}','name',s#>>'{metadata,name}','style',s->'style') "
        "ORDER BY s#>>'{metadata,id}') FROM jsonb_array_elements(snap.document->'resources') s "
        "WHERE (resource->'styleResourceIds') ? (s#>>'{metadata,id}')),'[]'::jsonb) AS \"styleResources\" "
        "FROM honua.metadata_v2_current active JOIN honua.metadata_v2_snapshots snap "
        "ON snap.environment=active.environment AND snap.revision=active.revision "
        "CROSS JOIN LATERAL jsonb_array_elements(snap.document->'services') svc "
        "CROSS JOIN LATERAL jsonb_array_elements(snap.document->'publications') pub "
        "CROSS JOIN LATERAL jsonb_array_elements(snap.document->'storageBindings') binding "
        "CROSS JOIN LATERAL jsonb_array_elements(snap.document->'resources') resource "
        "WHERE svc#>>'{metadata,id}'=" + literal(area["serviceId"]) + " AND pub->>'serviceId'=svc#>>'{metadata,id}' "
        "AND pub->>'publicationType'='esri-feature-layer' AND pub->>'isPrimary'='true' "
        "AND (pub#>>'{identifier,value}')::integer=" + str(area["layerId"]) + " "
        "AND pub->>'storageBindingId'=binding#>>'{metadata,id}' AND pub->>'resourceId'=binding->>'resourceId' "
        "AND pub->>'resourceId'=resource#>>'{metadata,id}'), "
        "styles AS (SELECT style_id AS \"styleId\",title,description,maplibre_style AS document,drawing_info AS \"drawingInfo\","
        "style_version AS version,created_at AS \"createdAt\",updated_at AS \"updatedAt\",revised_by AS \"revisedBy\",change_summary AS \"changeSummary\" FROM honua.styles), "
        "refs AS (SELECT style_id AS \"styleId\",ordinal FROM honua.layer_style_refs WHERE layer_id=" + str(area["storageLayerId"]) + "), "
        "rows AS (SELECT gid,name,json_build_array(ST_XMin(Box2D(geom)),ST_YMin(Box2D(geom)),ST_XMax(Box2D(geom)),ST_YMax(Box2D(geom))) "
        "AS envelope,ST_SRID(geom) AS srid,ST_AsGeoJSON(geom,17)::json AS geometry FROM honua_data.sdkreg_area) "
        "SELECT json_build_object('schema','honua.mcp-style-persisted/v1',"
        "'styles',COALESCE((SELECT json_agg(row_to_json(styles) ORDER BY \"styleId\") FROM styles),'[]'::json),"
        "'refs',COALESCE((SELECT json_agg(row_to_json(refs) ORDER BY ordinal,\"styleId\") FROM refs),'[]'::json),"
        "'mappings',COALESCE((SELECT json_agg(row_to_json(mappings)) FROM mappings),'[]'::json),"
        "'rows',COALESCE((SELECT json_agg(row_to_json(rows) ORDER BY gid) FROM rows),'[]'::json))::text;")
    value = strict_json(execute(sql))
    snapshot_output(value, authored, fixture, applied=False)
    return value


def create_styles(authored, credential, receipt, *, connect=HTTPConnection):
    """Use only the fixed owned candidate; never follow a credential redirect."""
    if not isinstance(credential, str) or not credential or len(credential) > 8192 or any(char in credential for char in "\r\n"):
        raise ValueError("style setup requires a private bounded credential")
    for style in authored["styles"]:
        entry = {"id": style["id"], "method": "POST", "path": "/ogc/styles", "document": style["document"],
                 "status": None, "location": None, "body": None}
        receipt.append(entry)
        connection = connect("127.0.0.1", 8080, timeout=30)
        try:
            connection.request("POST", "/ogc/styles", json.dumps(style["document"], allow_nan=False).encode(), headers={
                "X-API-Key": credential, "X-Style-Id": style["id"], "Content-Type": "application/vnd.mapbox.style+json", "Accept": "application/json"})
            response = connection.getresponse()
            entry["status"], entry["location"] = response.status, response.getheader("Location")
            raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024 or credential.encode() in raw:
                raise ValueError("style setup response unsafe or oversized")
            entry["body"] = strict_json(raw)
        finally:
            connection.close()
        if entry["status"] != 201:
            raise ValueError("style creation was not accepted by the actual catalog")
    setup_output(receipt, authored)


def database_query(query, password, *, run=subprocess.run):
    if not isinstance(password, str) or not password:
        raise ValueError("style readback requires private fixture database credential")
    result = run(["psql", "-X", "--no-password", "-h", "127.0.0.1", "-p", "5432", "-U", "honua", "-d", "honua_test",
                  "-v", "ON_ERROR_STOP=1", "-Atq", "-1"], input=query, text=True, capture_output=True, timeout=30,
                 env={"PATH": os.environ.get("PATH", ""), "PGPASSWORD": password, "PGSSLMODE": "disable", "PGCONNECT_TIMEOUT": "5"})
    if result.returncode:
        raise ValueError("independent style SQL observation failed")
    return result.stdout


def restart(authored, runtime):
    result = subprocess.run(["docker", "restart", "--time", "15", runtime["containerId"]], capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise ValueError("owned style candidate restart failed")
    deadline = time.monotonic() + 90
    while True:
        connection = HTTPConnection("127.0.0.1", 8080, timeout=3)
        try:
            connection.request("GET", "/healthz/ready")
            response = connection.getresponse(); response.read(65536)
            if response.status == 200:
                break
        except (OSError, TimeoutError):
            pass
        finally:
            connection.close()
        if time.monotonic() >= deadline:
            raise ValueError("owned style candidate failed readiness after restart")
        time.sleep(0.5)
    current = inspect_candidate(authored["candidateSourceSha"], authored["candidateImage"])
    if {**current, "startedAt": runtime["startedAt"]} != runtime or current["startedAt"] == runtime["startedAt"]:
        raise ValueError("style restart changed actual candidate identity")
    return current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--setup-receipt", type=Path)
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--owned-disposable-candidate", action="store_true", required=True)
    args = parser.parse_args()
    receipt = []
    try:
        if args.setup and (args.restart or args.setup_receipt is None):
            raise ValueError("style setup requires its own retained receipt")
        observed = strict_json(args.observations.read_bytes())
        fixture = args.fixture.read_bytes()
        authored = scenario(fixture, strict_json(args.bootstrap.read_bytes()), observed["nonce"], observed["canonicalClient"])
        runtime = inspect_candidate(authored["candidateSourceSha"], authored["candidateImage"])
        if runtime != (observed.get("runtimeRestarted") or observed["runtimeBefore"]):
            raise ValueError("style candidate changed before state observation")
        if args.setup:
            create_styles(authored, os.environ.get("HONUA_MCP_API_KEY"), receipt)
            value = receipt
        elif args.restart:
            value = restart(authored, runtime)
        else:
            value = snapshot(fixture, authored, execute=lambda sql: database_query(sql, os.environ.get("HONUA_CERT_DB_PASSWORD")))
        print(json.dumps(value, allow_nan=False)); return 0
    except (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError):
        print("owned canonical style observation refused", file=sys.stderr); return 1
    finally:
        if args.setup and args.setup_receipt is not None:
            args.setup_receipt.write_text(json.dumps(receipt, allow_nan=False) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
