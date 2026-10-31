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

from local_http import open_local, reject_embedded_secrets, require_layer_path, require_loopback


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def observe_layer(base_url: str, layer_path: str, controller_key: str, *, timeout: float = 10,
                  sensitive_values: tuple[str, ...] = ()) -> dict:
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
            raw, status = response.read(2_000_001), response.status
        if status != 200 or len(raw) > 2_000_000:
            raise ValueError('layer observation failed or exceeds size limit')
        from client import Redactor
        import os
        sensitive = [controller_key, *sensitive_values, *(os.environ.get(name, '') for name in (
            'E2E_API_KEY', 'OPS_SMOKE_OPERATOR_KEY', 'HONUA_ADMIN_PASSWORD', 'HONUA_MASTER_KEY',
            'TERMINAL_MODEL_API_KEY', 'HONUA_CANARY_AWS_ACCESS_KEY_ID',
            'HONUA_CANARY_AWS_SECRET_ACCESS_KEY', 'HONUA_CANARY_AWS_SESSION_TOKEN',
            'HONUA_CANARY_TRANSCRIPT_SIGNING_SEED'))]
        decoded = raw.decode('utf-8')
        if Redactor(sensitive_values=sensitive).text(decoded) != decoded:
            raise ValueError('layer observation disclosed a credential')
        document = json.loads(raw)
        reject_embedded_secrets(document, sensitive)
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


def observe_database(project: str, compose_file: str, tables: list[tuple[str, str]], *, timeout: float = 30,
                     sensitive_values: tuple[str, ...] = ()) -> dict:
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
    if len(set(tables)) != len(tables) or any(
        not identifier.fullmatch(schema) or not identifier.fullmatch(table) or schema != 'honua_data' or
        (table not in {'e2e_src_fs', 'maui_zoning'} and not re.fullmatch(r'(?:imported_)?ops_[a-z0-9_]+', table))
        for schema, table in tables
    ):
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
    reject_embedded_secrets(document, list(sensitive_values))
    expected = {f'{schema}.{table}' for schema, table in tables}
    if not isinstance(document, dict) or set(document) != expected:
        raise ValueError("database observation incomplete")
    raw = completed.stdout.encode('utf-8')
    from client import Redactor
    if Redactor(sensitive_values=list(sensitive_values)).text(completed.stdout) != completed.stdout:
        raise ValueError('database observation disclosed a credential')
    evidence = {"project": project, "container": container, "started_at": started,
                "finished_at": time.time(), "sha256": hashlib.sha256(raw).hexdigest(),
                "body_b64": base64.b64encode(raw).decode('ascii')}
    for name in expected:
        item = document[name]
        if not isinstance(item, dict) or not isinstance(item.get('fields'), list) or not item['fields'] or not isinstance(item.get('rows'), list):
            raise ValueError(f"database observation incomplete for {name}")
        item['evidence'] = evidence
    return document


def observe_service_settings(base_url: str, service: str, controller_key: str, *, timeout: float = 10,
                             sensitive_values: tuple[str, ...] = ()) -> dict:
    """Read actual service settings and access policy; layer ACLs need separate reads."""
    import re
    if not controller_key or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', service):
        raise ValueError('invalid controller or service')
    path = '/api/v1/admin/services/' + urllib.parse.quote(service, safe='') + '/settings'
    body, evidence = _controller_json(base_url, path, controller_key, timeout=timeout, sensitive_values=sensitive_values)
    data = body.get('data')
    if (body.get('success') is not True or not isinstance(data, dict) or data.get('serviceName') != service or
        not isinstance(data.get('enabledProtocols'), list) or not isinstance(data.get('accessPolicy'), dict)):
        raise ValueError('service settings or ACL missing')
    return {'service': service, 'settings': data, 'evidence': evidence}


def _controller_json(base_url: str, path: str, key: str, *, method: str = 'GET', timeout: float = 10,
                     sensitive_values: tuple[str, ...] = ()) -> tuple[dict, dict]:
    import os
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
        raw, status = response.read(2_000_001), response.status
    if len(raw) > 2_000_000:
        raise ValueError('controller observation exceeds size limit')
    from client import Redactor
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise ValueError('controller observation is not UTF-8') from exc
    secrets = [key, *sensitive_values, *(os.environ.get(name, '') for name in (
        'E2E_API_KEY', 'HONUA_ADMIN_PASSWORD', 'HONUA_MASTER_KEY', 'TERMINAL_MODEL_API_KEY',
        'HONUA_CANARY_AWS_ACCESS_KEY_ID', 'HONUA_CANARY_AWS_SECRET_ACCESS_KEY',
        'HONUA_CANARY_AWS_SESSION_TOKEN', 'HONUA_CANARY_TRANSCRIPT_SIGNING_SEED',
    ))]
    if Redactor(sensitive_values=secrets).text(text) != text:
        raise ValueError('controller observation disclosed its credential')
    body = json.loads(raw)
    reject_embedded_secrets(body, secrets)
    if not isinstance(body, dict) or body.get('error') or body.get('success') is False:
        raise ValueError('controller observation failed')
    return body, {'method': method, 'url': url, 'status': status, 'started_at': started,
                  'finished_at': time.time(), 'sha256': hashlib.sha256(raw).hexdigest(),
                  'body_b64': base64.b64encode(raw).decode('ascii')}


def same_reopened_envelope(saved: object, reopened: object) -> bool:
    """Compare map content while allowing the server's fresh validation time."""
    if not isinstance(saved, dict) or not isinstance(reopened, dict):
        return False
    source_validation, fresh_validation = saved.get('validation'), reopened.get('validation')
    if not isinstance(source_validation, dict) or not isinstance(fresh_validation, dict):
        return False
    from datetime import datetime
    for validation in (source_validation, fresh_validation):
        if validation.get('status') != 'valid' or validation.get('diagnostics') != []:
            return False
        try:
            timestamp = datetime.fromisoformat(validation['generatedAt'].replace('Z', '+00:00'))
        except (KeyError, AttributeError, TypeError, ValueError):
            return False
        if timestamp.tzinfo is None:
            return False
    def without_time(envelope):
        result = dict(envelope)
        result['validation'] = {key: value for key, value in envelope['validation'].items() if key != 'generatedAt'}
        return result
    return without_time(saved) == without_time(reopened)


def reopen_saved_map(base_url: str, item_id: str, version_id: str, controller_key: str, *, timeout: float = 10,
                     sensitive_values: tuple[str, ...] = ()) -> dict:
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
    saved, first = _controller_json(base_url, root, controller_key, timeout=timeout, sensitive_values=sensitive_values)
    source = saved.get('data')
    if (not isinstance(source, dict) or source.get('itemId') != item or source.get('versionId') != version or
        not isinstance(source.get('envelope'), dict) or source['envelope'].get('family') != 'map'):
        raise ValueError('saved map version observation incomplete')
    reopened, second = _controller_json(base_url, root + '/reopen', controller_key, method='POST', timeout=timeout,
                                        sensitive_values=sensitive_values)
    draft = reopened.get('data')
    if not isinstance(draft, dict):
        raise ValueError('reopen did not create a draft')
    try:
        draft_id = str(UUID(draft.get('draftId')))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError('reopen draft identity invalid') from exc
    observed, third = _controller_json(base_url, f'/api/v1/studio/package-drafts/{draft_id}', controller_key,
                                       timeout=timeout, sensitive_values=sensitive_values)
    fresh = observed.get('data')
    if not isinstance(fresh, dict):
        raise ValueError('reopened map draft read missing')
    checks = {'draft identity': fresh.get('draftId') == draft_id and draft.get('draftId') == draft_id,
              'item identity': fresh.get('itemId') == item,
              'base version': fresh.get('baseVersionId') == version,
              'fresh envelope': same_reopened_envelope(source['envelope'], fresh.get('envelope')),
              'reopen envelope': same_reopened_envelope(source['envelope'], draft.get('envelope'))}
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        observed = fresh.get('envelope')
        expected = source['envelope']
        allowed = {'family', 'schemaVersion', 'format', 'bindings', 'dependencies', 'validation',
                   'publicationIntent', 'provenance', 'body'}
        differences = (sorted({name if name in allowed else 'other' for name in set(observed) | set(expected)
                               if observed.get(name) != expected.get(name)}) if isinstance(observed, dict) else [])
        raise ValueError('reopened map differs from saved version: ' + ', '.join(failed) +
                         '; envelope fields: ' + ', '.join(differences))
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


def compare_source_geometry(project: str, compose_file: str, table: str, features: list[dict], *, srid: int,
                            timeout: float = 30) -> dict:
    """Compare each source geometry's exact EWKB with the actual published source table."""
    import re
    import subprocess
    from pathlib import Path
    if (not re.fullmatch(r'ops-[a-f0-9]{32}', project) or not Path(compose_file).is_file() or
        not re.fullmatch(r'ops_[a-z0-9_]+', table) or type(srid) is not int or srid <= 0 or
        not isinstance(features, list) or not features):
        raise ValueError('invalid source geometry comparison')
    ids = []
    for feature in features:
        if (not isinstance(feature, dict) or not isinstance(feature.get('properties'), dict) or
            not isinstance(feature['properties'].get('source_id'), str) or not feature['properties']['source_id'] or
            not isinstance(feature.get('geometry'), dict)):
            raise ValueError('invalid expected source feature')
        ids.append(feature['properties']['source_id'])
    if len(ids) != len(set(ids)):
        raise ValueError('duplicate expected source identity')
    encoded = base64.b64encode(_canonical(features)).decode('ascii')
    sql = f'''WITH expected AS (
      SELECT f->'properties'->>'source_id' AS source_id,
             ST_SetSRID(ST_GeomFromGeoJSON((f->'geometry')::text), {srid}) AS geom
      FROM jsonb_array_elements(convert_from(decode('{encoded}', 'base64'),'UTF8')::jsonb) f
    ) SELECT json_build_object(
       'expected', (SELECT count(*) FROM expected),
       'actual', (SELECT count(*) FROM honua_data."{table}"),
       'matching', (SELECT count(DISTINCT e.source_id) FROM expected e JOIN honua_data."{table}" t
         ON t.source_id=e.source_id WHERE ST_AsEWKB(t.geom)=ST_AsEWKB(e.geom)))::text'''
    completed = subprocess.run(['docker', 'compose', '-p', project, '-f', str(Path(compose_file).resolve()),
                                'exec', '-T', 'db', 'psql', '-v', 'ON_ERROR_STOP=1',
                                '-U', 'honua', '-d', 'honua', '-Atc', sql],
                               capture_output=True, text=True, timeout=timeout, check=False)
    if completed.returncode:
        raise RuntimeError('source geometry comparison failed')
    try:
        body = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError('invalid source geometry observation') from exc
    if not isinstance(body, dict) or any(type(body.get(k)) is not int for k in ('expected', 'actual', 'matching')):
        raise ValueError('incomplete source geometry observation')
    if body['expected'] != len(features) or body['actual'] != len(features) or body['matching'] != len(features):
        raise ValueError('source geometries do not match')
    raw = completed.stdout.encode()
    return {'counts': body, 'project': project, 'table': table, 'srid': srid,
            'source_sha256': hashlib.sha256(_canonical(features)).hexdigest(),
            'response_sha256': hashlib.sha256(raw).hexdigest(),
            'response_body_b64': base64.b64encode(raw).decode('ascii')}


def observe_metadata_security(project: str, compose_file: str, services: list[str], *, timeout: float = 30,
                              sensitive_values: tuple[str, ...] = ()) -> dict:
    """Read scoped metadata security values and links in one SQL snapshot.

    A null policy is retained as null, never interpreted as a protective ACL.
    This diagnostic does not prove effective access or enable a trial without
    fixture and principal provenance.
    """
    import re
    import subprocess
    from pathlib import Path
    if (not re.fullmatch(r'ops-[a-f0-9]{32}', project) or not Path(compose_file).is_file() or
        not services or len(set(services)) != len(services) or
        any(not re.fullmatch(r'[a-z0-9_-]{1,100}', name) for name in services)):
        raise ValueError('invalid metadata observation scope')
    compose = str(Path(compose_file).resolve())
    ps = subprocess.run(['docker', 'compose', '-p', project, '-f', compose,
                         'ps', '-q', '--status', 'running', 'db'], capture_output=True, text=True,
                        timeout=timeout, check=False)
    containers = ps.stdout.split()
    if ps.returncode or len(containers) != 1 or not re.fullmatch(r'[a-f0-9]{12,64}', containers[0]):
        raise ValueError('metadata database identity unavailable')
    # Names are controller-owned, strictly alphanumeric and cannot escape SQL literals.
    names = ','.join("'" + name + "'" for name in sorted(services))
    sql = f"""WITH current_graph AS (
      SELECT s.document FROM honua.metadata_v2_current c JOIN honua.metadata_v2_snapshots s
        ON s.environment=c.environment AND s.revision=c.revision
    ), selected_services AS (
      SELECT value AS s FROM current_graph, jsonb_array_elements(document->'services')
        WHERE value->'metadata'->>'name' IN ({names})
    ), selected_publications AS (
      SELECT value AS p FROM current_graph, jsonb_array_elements(document->'publications')
        WHERE value->>'serviceId' IN (SELECT s->'metadata'->>'id' FROM selected_services)
    ), selected_resources AS (
      SELECT value AS r FROM current_graph, jsonb_array_elements(document->'resources')
        WHERE value->'metadata'->>'id' IN (SELECT p->>'resourceId' FROM selected_publications)
    ) SELECT json_build_object(
       'graphCount', (SELECT count(*) FROM current_graph),
       'services', (SELECT coalesce(jsonb_agg(jsonb_build_object('id',s->'metadata'->'id',
         'name',s->'metadata'->'name','policy',s->'accessPolicy','policyPresent',s ? 'accessPolicy')
         ORDER BY s->'metadata'->>'id'),'[]'::jsonb) FROM selected_services),
       'publications', (SELECT coalesce(jsonb_agg(jsonb_build_object('id',p->'metadata'->'id',
         'serviceId',p->'serviceId','resourceId',p->'resourceId','layerIndex',p->'layerIndex',
         'identifier',p->'identifier') ORDER BY p->'metadata'->>'id'),'[]'::jsonb) FROM selected_publications),
       'resources', (SELECT coalesce(jsonb_agg(jsonb_build_object('id',r->'metadata'->'id',
         'name',r->'metadata'->'name','policy',r->'accessPolicy','policyPresent',r ? 'accessPolicy',
         'policyIds',r->'policyIds') ORDER BY r->'metadata'->>'id'),'[]'::jsonb) FROM selected_resources),
       'policies', (SELECT coalesce(jsonb_agg(value ORDER BY value->'metadata'->>'id'),'[]'::jsonb)
         FROM current_graph, jsonb_array_elements(document->'policies')
         WHERE value->'metadata'->>'id' IN (
           SELECT jsonb_array_elements_text(coalesce(r->'policyIds','[]'::jsonb)) FROM selected_resources)))::text"""
    completed = subprocess.run(['docker', 'exec', containers[0], 'psql', '-v', 'ON_ERROR_STOP=1',
                                '-U', 'honua', '-d', 'honua', '-Atc', sql], capture_output=True, text=True,
                               timeout=timeout, check=False)
    if completed.returncode:
        raise ValueError('metadata security read failed')
    raw = completed.stdout.encode()
    if len(raw) > 2_000_000:
        raise ValueError('metadata security read exceeds limit')
    from client import Redactor
    import os
    secrets = [*sensitive_values, *(os.environ.get(name, '') for name in ('E2E_API_KEY', 'OPS_SMOKE_OPERATOR_KEY',
               'HONUA_ADMIN_PASSWORD', 'HONUA_MASTER_KEY', 'TERMINAL_MODEL_API_KEY'))]
    if Redactor(sensitive_values=secrets).text(completed.stdout) != completed.stdout:
        raise ValueError('metadata observation disclosed credential')
    doc = json.loads(raw)
    reject_embedded_secrets(doc, secrets)
    def contains_sensitive_metadata(value):
        if isinstance(value, dict):
            return any(re.search(r'(?i)password|secret|token|authorization|api.?key', str(key)) or
                       contains_sensitive_metadata(item) for key, item in value.items())
        if isinstance(value, list):
            return any(contains_sensitive_metadata(item) for item in value)
        if isinstance(value, str):
            return bool(re.search(r'(?i)(?:password|secret|token|api.?key)\s*[:=]\s*\S+', value) or
                        re.search(r'(?i)https?://[^\s/@:]+:[^\s/@]+@', value))
        return False
    if contains_sensitive_metadata(doc):
        raise ValueError('metadata observation contains sensitive fields')
    if (not isinstance(doc, dict) or doc.get('graphCount') != 1 or
        any(not isinstance(doc.get(key), list) for key in ('services', 'publications', 'resources', 'policies')) or
        any(not isinstance(row, dict) for key in ('services', 'publications', 'resources', 'policies') for row in doc[key]) or
        len(doc['services']) != len(services) or
        {row.get('name') for row in doc['services']} != set(services) or
        not doc['publications'] or not doc['resources']):
        raise ValueError('metadata security inventory incomplete')
    service_ids = [row.get('id') for row in doc['services']]
    resource_ids = [row.get('id') for row in doc['resources']]
    publication_ids = [row.get('id') for row in doc['publications']]
    policy_ids = [row.get('metadata', {}).get('id') if isinstance(row.get('metadata'), dict) else None
                  for row in doc['policies']]
    if (any(type(row.get('policyPresent')) is not bool or
            (row.get('policy') is not None and not isinstance(row.get('policy'), dict))
            for row in doc['services'] + doc['resources']) or
        any(not isinstance(row.get('policyIds'), list) or any(not isinstance(value, str) or not value
            for value in row['policyIds']) for row in doc['resources']) or
        any(not isinstance(value, str) or not value for value in service_ids + resource_ids + publication_ids + policy_ids) or
        len(set(publication_ids)) != len(publication_ids) or
        len(set(policy_ids)) != len(policy_ids) or
        any(policy_id not in policy_ids for row in doc['resources'] for policy_id in row['policyIds']) or
        len(set(service_ids)) != len(service_ids) or len(set(resource_ids)) != len(resource_ids) or
        any(row.get('serviceId') not in service_ids or row.get('resourceId') not in resource_ids
            for row in doc['publications']) or
        set(service_ids) != {row.get('serviceId') for row in doc['publications']} or
        set(resource_ids) != {row.get('resourceId') for row in doc['publications']}):
        raise ValueError('metadata security links incomplete')
    return {'project': project, 'database_container': containers[0], 'security': doc,
            'sha256': hashlib.sha256(raw).hexdigest(), 'body_b64': base64.b64encode(raw).decode('ascii')}
