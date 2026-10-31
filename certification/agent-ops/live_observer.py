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

from local_http import open_local, require_layer_path, require_loopback


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def observe_layer(base_url: str, layer_path: str, controller_key: str, *, timeout: float = 10) -> dict:
    """Read metadata, enumerate IDs, and fetch each feature without trusting pagination."""
    require_loopback(base_url)
    require_layer_path(layer_path)
    if not controller_key:
        raise ValueError("controller key and absolute layer path required")
    evidence = []

    def get(path: str, params: dict) -> dict:
        url = base_url.rstrip("/") + layer_path + path + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"X-API-Key": controller_key})
        with open_local(request, timeout=timeout) as response:
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


def observe_service_settings(base_url: str, service: str, controller_key: str, *, timeout: float = 10) -> dict:
    """Read actual service settings and access policy; layer ACLs need separate reads."""
    import re
    if not controller_key or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', service):
        raise ValueError('invalid controller or service')
    path = '/api/v1/admin/services/' + urllib.parse.quote(service, safe='') + '/settings'
    body, evidence = _controller_json(base_url, path, controller_key, timeout=timeout)
    data = body.get('data')
    if (body.get('success') is not True or not isinstance(data, dict) or data.get('serviceName') != service or
        not isinstance(data.get('enabledProtocols'), list) or not isinstance(data.get('accessPolicy'), dict)):
        raise ValueError('service settings or ACL missing')
    return {'service': service, 'settings': data, 'evidence': evidence}


def _controller_json(base_url: str, path: str, key: str, *, method: str = 'GET', timeout: float = 10) -> tuple[dict, dict]:
    import time
    require_loopback(base_url)
    if not key or not path.startswith('/') or path.startswith('//'):
        raise ValueError('controller origin or path invalid')
    url = base_url.rstrip('/') + path
    request = urllib.request.Request(url, data=b'{}' if method == 'POST' else None, method=method,
                                     headers={'X-API-Key': key, 'Content-Type': 'application/json'})
    started = time.time()
    # Refuse redirects: an observer must not send its credential to another origin.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=timeout) as response:
        raw, status = response.read(), response.status
    body = json.loads(raw)
    if not isinstance(body, dict) or body.get('error') or body.get('success') is False:
        raise ValueError('controller observation failed')
    return body, {'method': method, 'url': url, 'status': status, 'started_at': started,
                  'finished_at': time.time(), 'sha256': hashlib.sha256(raw).hexdigest(),
                  'body_b64': base64.b64encode(raw).decode('ascii')}


def reopen_saved_map(base_url: str, item_id: str, version_id: str, controller_key: str, *, timeout: float = 10) -> dict:
    """Reopen an existing version and corroborate the resulting draft by fresh reads.

    This creates a draft on the disposable trial stack. Call only after the
    model session and after recording the pre-observation state.
    """
    from uuid import UUID
    try:
        item = str(UUID(item_id))
        version = str(UUID(version_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError('map version identity invalid') from exc
    root = f'/api/v1/studio/content-items/{item}/versions/{version}'
    saved, first = _controller_json(base_url, root, controller_key, timeout=timeout)
    source = saved.get('data')
    if (not isinstance(source, dict) or source.get('itemId') != item or source.get('versionId') != version or
        not isinstance(source.get('envelope'), dict) or source['envelope'].get('family') != 'map'):
        raise ValueError('saved map version observation incomplete')
    reopened, second = _controller_json(base_url, root + '/reopen', controller_key, method='POST', timeout=timeout)
    draft = reopened.get('data')
    if not isinstance(draft, dict):
        raise ValueError('reopen did not create a draft')
    try:
        draft_id = str(UUID(draft.get('draftId')))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError('reopen draft identity invalid') from exc
    observed, third = _controller_json(base_url, f'/api/v1/studio/package-drafts/{draft_id}', controller_key, timeout=timeout)
    fresh = observed.get('data')
    if (not isinstance(fresh, dict) or fresh.get('draftId') != draft_id or fresh.get('itemId') != item or
        fresh.get('baseVersionId') != version or fresh.get('envelope') != source['envelope'] or
        draft.get('draftId') != draft_id or draft.get('envelope') != source['envelope']):
        raise ValueError('reopened map differs from saved version')
    return {'item_id': item, 'version_id': version, 'draft_id': draft_id,
            'envelope': fresh['envelope'], 'evidence': [first, second, third]}


def observe_runtime(project: str, compose_file: str, image_ref: str, expected_replicas: int, *, timeout: float = 15) -> dict:
    """Inspect every expected running replica against an immutable image ID."""
    import re
    import subprocess
    import time
    from pathlib import Path
    if (not re.fullmatch(r'ops-[a-f0-9]{32}', project) or not Path(compose_file).is_file() or
        not re.fullmatch(r'[^\s@]+@sha256:[a-f0-9]{64}', image_ref) or
        type(expected_replicas) is not int or expected_replicas < 1):
        raise ValueError('invalid trial runtime observation')
    started = time.time()
    def docker(*args):
        result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout, check=False)
        if result.returncode:
            raise RuntimeError('runtime observation failed')
        return result.stdout.strip()
    ids = docker('compose', '-p', project, '-f', str(Path(compose_file).resolve()),
                 'ps', '-q', '--status', 'running', 'server').split()
    if len(ids) != expected_replicas or len(set(ids)) != len(ids) or any(not re.fullmatch(r'[a-f0-9]{12,64}', cid) for cid in ids):
        raise ValueError('missing or duplicate running replica')
    expected_id = docker('image', 'inspect', '--format', '{{.Id}}', image_ref)
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', expected_id):
        raise ValueError('expected image identity missing')
    replicas = []
    for cid in ids:
        raw = docker('inspect', '--format', '{{.Image}}|{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}', cid)
        image_id, separator, status = raw.partition('|')
        state, separator2, health = status.partition('|')
        if not separator or not separator2 or image_id != expected_id or state != 'running' or health != 'healthy':
            raise ValueError('replica image or health mismatch')
        replicas.append({'container': cid, 'image_id': image_id, 'state': state, 'health': health})
    return {'project': project, 'image_ref': image_ref, 'expected_image_id': expected_id,
            'replicas': replicas, 'started_at': started, 'finished_at': time.time()}
