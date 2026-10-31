"""Independent controller reads of a live FeatureServer layer.

These observations are building blocks, not evidence that the trial fixture,
all ACLs, saved maps, schema or running images were observed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
import urllib.request


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def observe_layer(base_url: str, layer_path: str, controller_key: str, *, timeout: float = 10) -> dict:
    """Read metadata, enumerate IDs, and fetch each feature without trusting pagination."""
    if not controller_key or not layer_path.startswith("/"):
        raise ValueError("controller key and absolute layer path required")
    evidence = []

    def get(path: str, params: dict) -> dict:
        url = base_url.rstrip("/") + layer_path + path + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"X-API-Key": controller_key})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw, status = response.read(), response.status
        document = json.loads(raw)
        if not isinstance(document, dict) or document.get("error"):
            raise ValueError("controller API read failed")
        evidence.append({"method": "GET", "url": url, "status": status,
                         "sha256": hashlib.sha256(raw).hexdigest(),
                         "body_b64": base64.b64encode(raw).decode("ascii")})
        return document

    metadata = get("", {"f": "json"})
    object_id_field = metadata.get("objectIdField")
    fields = metadata.get("fields")
    if not isinstance(object_id_field, str) or not object_id_field or not isinstance(fields, list) or not fields:
        raise ValueError("layer metadata lacks fields or object id")
    ids_response = get("/query", {"f": "json", "where": "1=1", "returnIdsOnly": "true"})
    ids = ids_response.get("objectIds")
    if (not isinstance(ids, list) or not ids or any(type(item) is not int or item < 0 for item in ids)
        or len(set(ids)) != len(ids) or ids_response.get("exceededTransferLimit") is True):
        raise ValueError("incomplete or invalid feature id enumeration")
    features = []
    for object_id in sorted(ids):
        page = get("/query", {"f": "json", "objectIds": object_id, "outFields": "*", "returnGeometry": "true"})
        rows = page.get("features")
        if not isinstance(rows, list) or len(rows) != 1 or page.get("exceededTransferLimit") is True:
            raise ValueError("missing or ambiguous feature")
        row = rows[0]
        if (not isinstance(row, dict) or not isinstance(row.get("attributes"), dict)
            or row["attributes"].get(object_id_field) != object_id or not isinstance(row.get("geometry"), dict)
            or not row["geometry"]):
            raise ValueError("feature identity or geometry missing")
        features.append({"id": object_id, "attributes": row["attributes"],
                         "geometry": row["geometry"],
                         "geometry_sha256": hashlib.sha256(_canonical(row["geometry"])).hexdigest()})
    final_ids = get("/query", {"f": "json", "where": "1=1", "returnIdsOnly": "true"})
    observed_ids = final_ids.get("objectIds")
    if (not isinstance(observed_ids, list) or any(type(item) is not int for item in observed_ids) or
        len(set(observed_ids)) != len(observed_ids) or final_ids.get("exceededTransferLimit") is True or
        set(observed_ids) != set(ids)):
        raise ValueError("feature inventory changed during observation")
    return {"count": len(features), "object_id_field": object_id_field,
            "fields": fields, "features": features, "evidence": evidence}


def observe_database(project: str, compose_file: str, tables: list[tuple[str, str]], *, timeout: float = 30) -> dict:
    """Snapshot every row and column definition of named trial tables via Postgres.

    Table names are controller-supplied and strictly validated. This is not a
    substitute for service ACL or saved-map API observations.
    """
    import re
    import subprocess
    from pathlib import Path

    if not re.fullmatch(r"ops-[0-9a-f]{32}", project) or not Path(compose_file).is_file() or not tables:
        raise ValueError("invalid trial database observation request")
    identifier = re.compile(r"[a-z_][a-z0-9_]*\Z")
    if len(set(tables)) != len(tables) or any(not identifier.fullmatch(schema) or not identifier.fullmatch(table)
                                              for schema, table in tables):
        raise ValueError("invalid table name")
    import time
    compose = str(Path(compose_file).resolve())
    started = time.time()
    ps = subprocess.run(['docker', 'compose', '-p', project, '-f', compose,
                         'ps', '-q', '--status', 'running', 'db'],
                        capture_output=True, text=True, timeout=timeout, check=False)
    containers = ps.stdout.split()
    if ps.returncode != 0 or len(containers) != 1 or not re.fullmatch(r'[a-f0-9]{12,64}', containers[0]):
        raise RuntimeError("database container identity unavailable")
    container = containers[0]
    expressions = []
    for schema, table in tables:
        # All table reads are subqueries of one statement and therefore share
        # the same PostgreSQL MVCC snapshot. Identifiers cannot contain quotes.
        expressions.append(f"""'{schema}.{table}', json_build_object(
          'fields', (SELECT json_agg(json_build_object('name', a.attname,
                    'type', format_type(a.atttypid,a.atttypmod), 'notNull', a.attnotnull)
                    ORDER BY a.attnum) FROM pg_attribute a
                    WHERE a.attrelid = '"{schema}"."{table}"'::regclass
                    AND a.attnum > 0 AND NOT a.attisdropped),
          'rows', (SELECT coalesce(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text), '[]'::jsonb)
                    FROM "{schema}"."{table}" t))""")
    sql = "SELECT json_build_object(" + ",".join(expressions) + ")::text"
    completed = subprocess.run(['docker', 'exec', container, 'psql', '-v', 'ON_ERROR_STOP=1',
                                '-U', 'honua', '-d', 'honua', '-Atc', sql],
                               capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise RuntimeError("database observation failed")
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("database observation invalid") from exc
    expected = {f'{schema}.{table}' for schema, table in tables}
    if not isinstance(document, dict) or set(document) != expected:
        raise ValueError("database observation incomplete")
    raw = completed.stdout.encode('utf-8')
    evidence = {"project": project, "container": container, "started_at": started,
                "finished_at": time.time(), "sha256": hashlib.sha256(raw).hexdigest(),
                "body_b64": base64.b64encode(raw).decode('ascii')}
    for name in expected:
        item = document[name]
        if not isinstance(item, dict) or not isinstance(item.get('fields'), list) or not item['fields'] or not isinstance(item.get('rows'), list):
            raise ValueError(f"database observation incomplete for {name}")
        item['evidence'] = evidence
    return document
