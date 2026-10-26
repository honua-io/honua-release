"""Read-only, exact-candidate Metadata V2 source observation for grounding."""
from __future__ import annotations

import argparse
import hashlib
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_map_package_validation import strict_json
from mcp_render_validation import inspect_candidate, scenario


def admin_release_projection(credential, credential_salt, *, connect=HTTPConnection):
    """Observe only the safe declared release projection of the same candidate."""
    if (not isinstance(credential, str) or not credential or len(credential) > 8192
            or any(char in credential for char in "\r\n")
            or not isinstance(credential_salt, str)
            or not re.fullmatch(r"[a-f0-9]{32}", credential_salt)):
        raise ValueError("admin preflight requires a bounded private credential")
    fingerprint = "scrypt:" + credential_salt + ":" + hashlib.scrypt(
        credential.encode(), salt=bytes.fromhex(credential_salt), n=16384, r=8, p=1, dklen=32).hex()
    connection = connect("127.0.0.1", 8080, timeout=30)
    try:
        connection.request("GET", "/api/v1/admin/deploy/preflight?includeDiagnostics=true",
                           headers={"X-API-Key": credential, "Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200 or response.getheader("Location") is not None:
            raise ValueError("owned candidate admin preflight refused")
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024 or credential.encode() in raw:
            raise ValueError("owned candidate admin preflight is unsafe or oversized")
        body = strict_json(raw)
        if not isinstance(body, dict) or not isinstance(body.get("platformRelease"), dict):
            raise ValueError("admin preflight lacks declared release projection")
        projection = body["platformRelease"]
        pending = [projection]
        while pending:
            value = pending.pop()
            if isinstance(value, str) and credential in value:
                raise ValueError("admin preflight projection echoed a private credential")
            if isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
        # The rest of preflight can include diagnostics unrelated to this
        # source oracle; retain only the reviewed configuration projection.
        return {"platformRelease": projection, "credentialFingerprint": fingerprint,
                "responseSha256": "sha256:" + hashlib.sha256(raw).hexdigest()}
    finally:
        connection.close()


def graph_resource(owned_area, *, execute):
    """Retain exactly one current graph resource joined to the owned publication."""
    def literal(value):
        return "'" + value.replace("'", "''") + "'"

    service = literal(owned_area["serviceId"])
    layer = owned_area["storageLayerId"]
    if type(layer) is not int or layer < 0 or layer >= 2147483647:
        raise ValueError("owned storage layer is not bounded")
    sql = (
        "SELECT COALESCE(json_agg(row_to_json(bound)),'[]'::json)::text FROM ("
        "SELECT active.revision,svc#>>'{metadata,id}' AS \"serviceId\","
        "svc#>>'{metadata,name}' AS \"serviceName\","
        "(pub#>>'{identifier,value}')::integer AS \"layerId\","
        "resource#>>'{metadata,id}' AS \"resourceId\","
        "resource#>>'{metadata,name}' AS \"resourceName\","
        "resource->>'type' AS \"resourceType\","
        "(binding->>'storageLayerId')::integer AS \"storageLayerId\","
        "binding->>'locator' AS locator,binding->>'connectionId' AS \"connectionId\" "
        "FROM honua.metadata_v2_current active JOIN honua.metadata_v2_snapshots snap "
        "ON snap.environment=active.environment AND snap.revision=active.revision "
        "CROSS JOIN LATERAL jsonb_array_elements(snap.document->'services') svc "
        "CROSS JOIN LATERAL jsonb_array_elements(snap.document->'publications') pub "
        "CROSS JOIN LATERAL jsonb_array_elements(snap.document->'storageBindings') binding "
        "CROSS JOIN LATERAL jsonb_array_elements(snap.document->'resources') resource "
        "WHERE svc#>>'{metadata,id}'=" + service + " "
        "AND pub->>'serviceId'=svc#>>'{metadata,id}' AND pub->>'isPrimary'='true' "
        "AND pub->>'storageBindingId'=binding#>>'{metadata,id}' "
        "AND pub->>'resourceId'=resource#>>'{metadata,id}' "
        "AND binding->>'resourceId'=resource#>>'{metadata,id}' "
        "AND (binding->>'storageLayerId')::integer=" + str(layer) + ") bound;")
    rows = strict_json(execute(sql))
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("current graph lacks one unique owned dataset resource")
    return rows[0]


DATASET_CATALOG_SQL = """WITH snap AS (SELECT s.document AS d FROM honua.metadata_v2_current a JOIN honua.metadata_v2_snapshots s ON s.environment=a.environment AND s.revision=a.revision),
pubs AS (SELECT p FROM snap CROSS JOIN LATERAL jsonb_array_elements(snap.d->'publications') p),
binds AS (SELECT b FROM snap CROSS JOIN LATERAL jsonb_array_elements(snap.d->'storageBindings') b),
res AS (SELECT r FROM snap CROSS JOIN LATERAL jsonb_array_elements(snap.d->'resources') r
        WHERE r->>'type' IN ('feature-dataset','table','raster-dataset','tile-dataset')),
layers AS (SELECT DISTINCT COALESCE(pb.b->>'storageLayerId', rb.b->>'storageLayerId') AS id,
                  COALESCE(NULLIF(res.r#>>'{metadata,name}',''), res.r#>>'{metadata,id}') AS name
           FROM res JOIN pubs ON pubs.p->>'resourceId'=res.r#>>'{metadata,id}'
           LEFT JOIN binds pb ON pb.b#>>'{metadata,id}'=pubs.p->>'storageBindingId'
           LEFT JOIN binds rb ON rb.b->>'resourceId'=res.r#>>'{metadata,id}'),
services AS (SELECT DISTINCT svc#>>'{metadata,name}' AS name FROM snap CROSS JOIN LATERAL jsonb_array_elements(snap.d->'services') svc
             WHERE EXISTS (SELECT 1 FROM pubs WHERE pubs.p->>'serviceId'=svc#>>'{metadata,id}'))
SELECT json_build_object(
  'layers', COALESCE((SELECT json_agg(json_build_object('id', id, 'name', name) ORDER BY id, name) FROM layers WHERE id IS NOT NULL AND name IS NOT NULL AND name <> ''), '[]'::json),
  'services', COALESCE((SELECT json_agg(name ORDER BY name) FROM services WHERE name IS NOT NULL AND name <> ''), '[]'::json))::text;"""
DATASET_TYPES = ("feature-dataset", "table", "raster-dataset", "tile-dataset")


def dataset_catalog(*, execute):
    """Every readable dataset the candidate can rank, read independently from Metadata V2.

    honua-server ranks one Dataset candidate per published dataset resource (ID = its
    storage layer ID, name = its resource name) and one per published service (ID = name).
    Several resources can share a storage layer, so an ID can carry more than one name.
    """
    value = strict_json(execute(DATASET_CATALOG_SQL))
    layers, services = (value.get("layers"), value.get("services")) if isinstance(value, dict) else (None, None)
    if (not isinstance(value, dict) or set(value) != {"layers", "services"}
            or not isinstance(layers, list) or not isinstance(services, list)
            or len(layers) > 4096 or len(services) > 4096
            or any(not isinstance(item, dict) or set(item) != {"id", "name"}
                   or not isinstance(item["id"], str) or not re.fullmatch(r"[0-9]{1,10}", item["id"])
                   or not isinstance(item["name"], str) or not item["name"].strip() for item in layers)
            or any(not isinstance(name, str) or not name.strip() for name in services)
            or len({(item["id"], item["name"]) for item in layers}) != len(layers) or len(set(services)) != len(services)):
        raise ValueError("readable dataset catalog readback is malformed")
    return {"layers": layers, "services": services}


def database_query(sql, password, *, run=subprocess.run):
    if not isinstance(password, str) or not password:
        raise ValueError("graph readback requires the owned private database credential")
    result = run(["psql", "-X", "--no-password", "-h", "127.0.0.1", "-p", "5432", "-U", "honua", "-d", "honua_test",
                  "-v", "ON_ERROR_STOP=1", "-Atq", "-1"], input=sql, text=True,
                 capture_output=True, timeout=30,
                 env={"PATH": os.environ.get("PATH", ""), "PGPASSWORD": password,
                      "PGSSLMODE": "disable", "PGCONNECT_TIMEOUT": "5"})
    if result.returncode:
        raise ValueError("independent Metadata V2 graph readback failed")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--owned-disposable-candidate", action="store_true", required=True)
    parser.add_argument("--admin-preflight", action="store_true")
    parser.add_argument("--credential-salt")
    args = parser.parse_args()
    try:
        fixture = args.fixture.read_bytes()
        bootstrap = strict_json(args.bootstrap.read_bytes())
        authored = scenario(fixture, bootstrap)
        before = inspect_candidate(authored["candidateSourceSha"], authored["candidateImage"])
        if before != authored["runtimeBinding"]:
            raise ValueError("grounding graph belongs to another candidate runtime")
        execute = lambda sql: database_query(sql, os.environ.get("HONUA_CERT_DB_PASSWORD"))
        observation = (admin_release_projection(os.environ.get("HONUA_MCP_API_KEY"), args.credential_salt)
                       if args.admin_preflight else graph_resource(bootstrap["ownedArea"], execute=execute))
        datasets = None if args.admin_preflight else dataset_catalog(execute=execute)
        after = inspect_candidate(authored["candidateSourceSha"], authored["candidateImage"])
        if after != before:
            raise ValueError("grounding candidate changed during graph readback")
        # The catalog is read through the installed MCP client separately.
        # Validate the graph half here and retain only safe IDs/digests.
        if not args.admin_preflight:
            area = bootstrap["ownedArea"]
            for name in ("serviceId", "connectionId", "locator", "layerId", "storageLayerId"):
                if observation.get(name) != area[name]:
                    raise ValueError("graph readback differs from owned publication")
            if observation.get("resourceType") != "feature-dataset":
                raise ValueError("owned publication is not a feature dataset")
        print(json.dumps({"schema": "honua.mcp-grounding-admin-preflight/v1" if args.admin_preflight else "honua.mcp-grounding-graph/v1",
                          "projection" if args.admin_preflight else "metadata": (
                              {"platformRelease": observation["platformRelease"]} if args.admin_preflight else observation),
                          **({"credentialFingerprint": observation["credentialFingerprint"],
                              "responseSha256": observation["responseSha256"]} if args.admin_preflight else {}),
                          **({} if args.admin_preflight else {"datasets": datasets}),
                          "candidateSourceSha": authored["candidateSourceSha"],
                          "candidateImage": authored["candidateImage"],
                          "fixtureSha256": authored["fixtureSha256"],
                          "bootstrapSha256": authored["bootstrapSha256"]}, sort_keys=True, allow_nan=False))
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        print("owned grounding graph observation refused", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
