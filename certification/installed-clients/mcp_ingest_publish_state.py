"""Owned disposable-candidate DB readback/restart for canonical ingest evidence.

No tool or resource credit is earned here. Product operations use the fixed
installed SDK/Inspector path; this module independently observes persistence.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_ingest_publish_validation import ingest_output, publish_output, scenario, snapshot_output, strict_json
from mcp_render_validation import inspect_candidate


def snapshot(fixture_bytes, bootstrap, observations, *, execute):
    authored = scenario(fixture_bytes, bootstrap, observations["nonce"], observations["canonicalClient"])
    ingested = ingest_output(observations["cases"][0]["result"], authored)
    published = publish_output(observations["cases"][2]["result"], authored)
    # Identifier validation above bounds every SQL identifier. Literals remain
    # quoted even though authored names are strictly ASCII and bounded.
    def literal(value):
        return "'" + value.replace("'", "''") + "'"
    schema, table, geometry, primary = (ingested[key] for key in ("schema", "table", "geometryColumn", "primaryKey"))
    # One SQL statement gives rows, mapping and its revision one MVCC snapshot.
    # Different environments or duplicate primary mappings remain ambiguous.
    observed = strict_json(execute("WITH mappings AS (SELECT active.revision, jsonb_build_object("
        "'serviceId',svc#>>'{metadata,id}','serviceName',svc#>>'{metadata,name}',"
        "'layerId',(pub#>>'{identifier,value}')::integer,'storageLayerId',(binding->>'storageLayerId')::integer,"
        "'locator',binding->>'locator','connectionId',binding->>'connectionId') AS publication "
        "FROM honua.metadata_v2_current active JOIN honua.metadata_v2_snapshots snapshot "
        "ON snapshot.environment=active.environment AND snapshot.revision=active.revision "
        "CROSS JOIN LATERAL jsonb_array_elements(snapshot.document->'services') svc "
        "CROSS JOIN LATERAL jsonb_array_elements(snapshot.document->'publications') pub "
        "CROSS JOIN LATERAL jsonb_array_elements(snapshot.document->'storageBindings') binding "
        "WHERE svc#>>'{metadata,name}'=" + literal(published["serviceName"]) + " AND pub->>'serviceId'=svc#>>'{metadata,id}' "
        "AND pub->>'publicationType'='esri-feature-layer' AND pub->>'isPrimary'='true' "
        "AND pub->>'storageBindingId'=binding#>>'{metadata,id}' "
        "AND pub->>'resourceId'=binding->>'resourceId'), "
        f'rows AS (SELECT "{primary}" AS id, properties, ST_AsGeoJSON("{geometry}")::json AS geometry, '
        f'ST_SRID("{geometry}") AS srid FROM "{schema}"."{table}") '
        "SELECT json_build_object('rows',COALESCE((SELECT json_agg(row_to_json(rows) ORDER BY id) FROM rows),'[]'::json),"
        "'mappings',COALESCE((SELECT json_agg(row_to_json(mappings)) FROM mappings),'[]'::json))::text;"))
    if (not isinstance(observed, dict) or set(observed) != {"rows", "mappings"}
            or not isinstance(observed["mappings"], list) or len(observed["mappings"]) != 1
            or not isinstance(observed["mappings"][0], dict)
            or set(observed["mappings"][0]) != {"publication", "revision"}):
        raise ValueError("published source lacks one independently persisted primary mapping/revision")
    mapping = observed["mappings"][0]
    value = {"schema": "honua.mcp-ingest-persisted/v1", "tuple": {key: ingested[key] for key in
        ("connectionId", "schema", "table", "geometryColumn", "primaryKey")}, "rows": observed["rows"],
        "publication": mapping["publication"], "revision": mapping["revision"]}
    snapshot_output(value, ingested, published, authored)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--owned-disposable-candidate", action="store_true", required=True)
    args = parser.parse_args()
    try:
        observations = strict_json(args.observations.read_bytes())
        bootstrap = strict_json(args.bootstrap.read_bytes()); fixture = args.fixture.read_bytes()
        authored = scenario(fixture, bootstrap, observations["nonce"], observations["canonicalClient"])
        runtime = inspect_candidate(authored["candidateSourceSha"], authored["candidateImage"])
        if runtime != (observations.get("runtimeRestarted") or observations["runtimeBefore"]):
            raise ValueError("owned candidate changed before persistence/restart observation")
        if args.restart:
            # Never restart by name alone: the inspected full container ID,
            # selected image digest and source must match the owned receipt.
            result = subprocess.run(["docker", "restart", "--time", "15", runtime["containerId"]],
                                    capture_output=True, text=True, timeout=90)
            if result.returncode:
                raise ValueError("owned candidate restart failed")
            deadline = time.monotonic() + 90
            while True:
                try:
                    with urlopen("http://127.0.0.1:8080/healthz/ready", timeout=3) as response:
                        if response.status == 200:
                            break
                except (OSError, TimeoutError):
                    pass
                if time.monotonic() >= deadline:
                    raise ValueError("owned restarted candidate did not become ready")
                time.sleep(0.5)
            restarted = inspect_candidate(authored["candidateSourceSha"], authored["candidateImage"])
            compare = dict(restarted); compare["startedAt"] = runtime["startedAt"]
            if compare != runtime or restarted["startedAt"] == runtime["startedAt"]:
                raise ValueError("candidate restart did not preserve exact container/source/image")
            print(json.dumps(restarted)); return 0
        password = os.environ.get("HONUA_CERT_DB_PASSWORD")
        if not isinstance(password, str) or not password:
            raise ValueError("independent database readback requires a private credential")
        def execute(query):
            command = ["psql", "-X", "--no-password", "-h", "localhost", "-p", "5432", "-U", "honua", "-d", "honua_test",
                       "-v", "ON_ERROR_STOP=1", "-Atq", "-1"]
            result = subprocess.run(command, input=query, text=True, capture_output=True,
                                    env={**os.environ, "PGPASSWORD": password}, timeout=30)
            if result.returncode:
                raise ValueError("independent imported-data SQL readback failed")
            return result.stdout
        print(json.dumps(snapshot(fixture, bootstrap, observations, execute=execute), allow_nan=False))
        return 0
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        print("owned ingest persistence/restart observation refused", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
