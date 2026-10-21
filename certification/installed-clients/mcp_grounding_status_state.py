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
        observation = (admin_release_projection(os.environ.get("HONUA_MCP_API_KEY"), args.credential_salt)
                       if args.admin_preflight else
                       graph_resource(bootstrap["ownedArea"], execute=lambda sql: database_query(
                           sql, os.environ.get("HONUA_CERT_DB_PASSWORD"))))
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
                          "candidateSourceSha": authored["candidateSourceSha"],
                          "candidateImage": authored["candidateImage"],
                          "fixtureSha256": authored["fixtureSha256"],
                          "bootstrapSha256": authored["bootstrapSha256"]}, sort_keys=True, allow_nan=False))
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        print("owned grounding graph observation refused", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
