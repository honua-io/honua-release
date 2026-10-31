"""Seed typed development-town tables and publish through the controller API.

This supplies the town, projected stations and mixed-schema data. ACLs, saved
maps and distinct principals still need verified setup; this module alone cannot
enable a priced trial.
"""
from __future__ import annotations

import json
import base64
import hashlib
import os
import re
import shutil
import sys
import subprocess
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / 'tools'
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from agent_e2e_pack import canonical_digest  # noqa: E402
from agent_e2e_setup_http import SetupHttp  # noqa: E402
from agent_e2e_scenario_operations import data, publication_request, submit_publication  # noqa: E402
from state import _projected_layer, messy_table  # noqa: E402

CORPUS = ROOT / 'certification' / 'agent-e2e' / 'corpus'


def _verify_runtime(project: str, compose_file: Path, image_ref: str, digest: str, base_url: str) -> dict:
    if not re.fullmatch(r'ops-[0-9a-f]{32}', project) or not compose_file.is_file() or not image_ref.endswith('@' + digest):
        raise ValueError('isolated trial project and immutable image required')
    origin = urllib.parse.urlsplit(base_url)
    if origin.scheme != 'http' or origin.hostname != '127.0.0.1' or origin.path not in ('', '/') or origin.query or origin.fragment or origin.port is None:
        raise ValueError('trial endpoint must be an explicit loopback port')
    def docker(*args):
        result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=15, check=False)
        if result.returncode != 0:
            raise ValueError('trial runtime identity unavailable')
        return result.stdout.strip()
    ids = docker('compose', '-p', project, '-f', str(compose_file.resolve()), 'ps', '-q', '--status', 'running', 'server').split()
    if len(ids) != 1 or not re.fullmatch(r'[a-f0-9]{12,64}', ids[0]):
        raise ValueError('trial server identity unavailable')
    container = ids[0]
    actual = docker('inspect', '--format', '{{.Image}}', container)
    expected = docker('image', 'inspect', '--format', '{{.Id}}', image_ref)
    ports = docker('port', container, '8080/tcp').splitlines()
    if (not re.fullmatch(r'sha256:[a-f0-9]{64}', actual) or actual != expected or
        not any(p in {f'127.0.0.1:{origin.port}', f'0.0.0.0:{origin.port}'} for p in ports)):
        raise ValueError('trial server image or endpoint mismatch')
    return {'project': project, 'container': container, 'image_id': actual,
            'image_ref': image_ref, 'base_url': base_url}


def prepare_town(*, variant: str, namespace: str, base_url: str, credential_env: str,
                 candidate_digest: str, connection_id: str, work: Path, project: str,
                 compose_file: Path, image_ref: str, authorize, explicit_service_acl: bool = False) -> dict:
    """Seed and publish only the selected development town, retaining every intent.

    No held-out pack is opened. Only the three selected A/B layer files are
    copied into a new private directory and verified against the manifest.
    Typed PostGIS tables are published through the existing operation helper.
    """
    if variant not in ('A', 'B') or not re.fullmatch(r'[a-z][a-z0-9_]{0,35}', namespace):
        raise ValueError('only A/B development towns and an isolated namespace are allowed')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', candidate_digest):
        raise ValueError('candidate digest required')
    if work.exists():
        raise ValueError('town setup directory already exists; never replay a setup')
    if not callable(authorize):
        raise ValueError('controller authorization callback required')
    authorize()
    runtime = _verify_runtime(project, compose_file, image_ref, candidate_digest, base_url)
    work.mkdir(parents=True, mode=0o700)
    selected = {f'town-{variant}/{name}.4326.geojson' for name in ('parcels', 'flood_zones', 'shelters')}
    manifest = json.loads((CORPUS / 'manifest.v1.json').read_text(encoding='utf-8'))
    entries = [row for row in manifest['files'] if row.get('path') in selected]
    if len(entries) != len(selected) or {row['path'] for row in entries} != selected:
        raise ValueError('selected town manifest incomplete')
    for row in entries:
        relative = Path(row['path'])
        dest = work / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CORPUS / relative, dest)
        content = dest.read_bytes()
        if len(content) != row['bytes'] or hashlib.sha256(content).hexdigest() != row['sha256']:
            raise ValueError('selected source does not match frozen manifest')
    manifest['files'] = entries
    manifest_path = work / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding='utf-8')
    audit = work / 'audit'
    audit.mkdir(mode=0o700)
    def sync_directory(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    sync_directory(work.parent)
    sync_directory(work)

    def retain(kind: str, step: str, record: dict) -> dict:
        if kind not in ('intent', 'response') or not re.fullmatch(r'[a-z0-9-]{1,80}', step):
            raise ValueError('invalid retained setup event')
        target = audit / f'{kind}-{step}.json'
        encoded = json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        sync_directory(audit)
        return {'key': target.name, 'sha256': canonical_digest(record)}

    def authorize_bound() -> None:
        authorize()
        if _verify_runtime(project, compose_file, image_ref, candidate_digest, base_url) != runtime:
            raise ValueError('trial runtime changed during town setup')

    http = SetupHttp(base_url, credential_env,
                     identity={'candidateKey': candidate_digest, 'attemptId': namespace.replace('_', '-')},
                     authorize=authorize_bound, retain=retain, timeout=300)
    # The generic import currently stores GeoJSON properties in one JSON text
    # column. Seed typed PostGIS source tables instead so the published layers
    # actually expose the source fields and values required by the prereg.
    definitions = {
        'parcels': ('Polygon', 4326, [('source_id', 'text'), ('name', 'text'), ('owner', 'text'), ('value', 'integer')]),
        'flood_zones': ('Polygon', 4326, [('source_id', 'text'), ('name', 'text')]),
        'shelters': ('Point', 4326, [('source_id', 'text'), ('name', 'text'), ('capacity', 'integer'), ('is_open', 'boolean')]),
        'stations': ('Point', 3338, [('source_id', 'text'), ('name', 'text'), ('latitude', 'double precision'), ('elevation_m', 'integer')]),
    }
    town_name = {'A': 'Kestrel', 'B': 'Willow'}[variant]
    sources = {name: json.loads((work / f'town-{variant}' / f'{name}.4326.geojson').read_text())['features']
               for name in ('parcels', 'flood_zones', 'shelters')}
    sources['stations'] = _projected_layer(town_name)['features']
    sql_parts = []
    for name, (geometry_type, srid, fields) in definitions.items():
        features = sources[name]
        if not isinstance(features, list) or not features or any(
            not isinstance(f, dict) or not isinstance(f.get('properties'), dict) or
            set(f['properties']) != {field for field, _ in fields} or
            not isinstance(f.get('geometry'), dict) or f['geometry'].get('type') != geometry_type
            for f in features
        ):
            raise ValueError('source feature fields or geometry do not match the fixture schema')
        raw = json.dumps(features, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        encoded = base64.b64encode(raw).decode('ascii')
        table = namespace + '_' + name
        columns = ', '.join(f'"{field}" {kind}' for field, kind in fields)
        selected_fields = ', '.join(f"(f->'properties'->>'{field}')::{kind}" for field, kind in fields)
        field_names = ', '.join(f'"{field}"' for field, _ in fields)
        sql_parts.append(f"""CREATE TABLE honua_data."{table}" (gid integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            {columns}, geom geometry({geometry_type},{srid}) NOT NULL);
            INSERT INTO honua_data."{table}" ({field_names}, geom)
            SELECT {selected_fields}, ST_SetSRID(ST_GeomFromGeoJSON((f->'geometry')::text), {srid})
            FROM jsonb_array_elements(convert_from(decode('{encoded}', 'base64'), 'UTF8')::jsonb) f;""")
    sql = '\n'.join(sql_parts)
    authorize_bound()
    retain('intent', 'typed-town-seed', {'kind': 'sql-seed', 'project': project,
           'request_sha256': hashlib.sha256(sql.encode()).hexdigest()})
    authorize_bound()
    proc = subprocess.run(['docker', 'compose', '-p', project, '-f', str(compose_file.resolve()),
                           'exec', '-T', 'db', 'psql', '-1', '-v', 'ON_ERROR_STOP=1', '-U', 'honua', '-d', 'honua', '-f', '-'],
                          input=sql, capture_output=True, text=True, timeout=30, check=False)
    retain('response', 'typed-town-seed', {'kind': 'sql-seed', 'project': project,
           'exit_code': proc.returncode, 'response_sha256': hashlib.sha256(proc.stdout.encode()).hexdigest()})
    if proc.returncode != 0:
        raise RuntimeError('typed town source creation failed')
    result = {'namespace': namespace, 'variant': variant, 'startingState': 'published-town', 'layers': {}}
    for name, (geometry_type, srid, _) in definitions.items():
        table, service = namespace + '_' + name, namespace + '-' + name.replace('_', '-')
        published = submit_publication(http, publication_request(connection=connection_id,
            schema='honua_data', table=table, service=service, geometry_type=geometry_type, srid=srid),
            step='publish-' + name.replace('_', '-'))
        if published['status'] != 'completed':
            raise RuntimeError('typed fixture publication requires approval')
        binding = {'schema': 'honua_data', 'table': table, 'service': service,
                   'layerId': published['layerId'], 'source_srid': srid,
                   'source_sha256': hashlib.sha256(json.dumps(sources[name], sort_keys=True,
                       separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
                   'publication': published['reference']}
        if explicit_service_acl:
            policy = {'allowAnonymous': True, 'allowAnonymousWrite': False,
                      'allowedRoles': [], 'allowedWriteRoles': []}
            answer = http.json('policy-' + name.replace('_', '-'), 'PUT',
                               '/api/v1/admin/services/' + service + '/access-policy', policy)
            if data(answer).get('accessPolicy') != policy:
                raise RuntimeError('service access policy mutation was not observed')
            read = http.json('policy-read-' + name.replace('_', '-'), 'GET',
                             '/api/v1/admin/services/' + service + '/settings')
            settings = data(read)
            if settings.get('serviceName') != service or settings.get('accessPolicy') != policy:
                raise RuntimeError('independent service access policy readback mismatch')
            binding['service_policy'] = {'expected': policy, 'write': answer['reference'],
                                         'read': read['reference']}
        if name == 'stations':
            result['stations'] = binding
        else:
            result['layers'][name] = binding
    # A separate mixed-schema source table is seeded through the trial DB. The
    # exact names are generated and quoted; no model text is interpolated.
    authorize_bound()
    rows = json.dumps(messy_table(town_name), sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    encoded = base64.b64encode(rows).decode('ascii')
    mixed_table = namespace + '_legacy_permits'
    sql = f"""CREATE TABLE honua_data."{mixed_table}" (
        permit_id text PRIMARY KEY, year integer, fee numeric, note text, active boolean, owner text);
        INSERT INTO honua_data."{mixed_table}" SELECT * FROM jsonb_to_recordset(
        convert_from(decode('{encoded}', 'base64'), 'UTF8')::jsonb)
        AS r(permit_id text, year integer, fee numeric, note text, active boolean, owner text);"""
    intent = {'kind': 'sql-seed', 'project': project, 'table': mixed_table,
              'request_sha256': hashlib.sha256(sql.encode()).hexdigest()}
    retain('intent', 'mixed-schema-seed', intent)
    authorize_bound()
    proc = subprocess.run(['docker', 'compose', '-p', project, '-f', str(compose_file.resolve()),
                           'exec', '-T', 'db', 'psql', '-1', '-v', 'ON_ERROR_STOP=1', '-U', 'honua', '-d', 'honua', '-f', '-'],
                          input=sql, capture_output=True, text=True, timeout=30, check=False)
    outcome = {'kind': 'sql-seed', 'project': project, 'table': mixed_table,
               'exit_code': proc.returncode, 'response_sha256': hashlib.sha256(proc.stdout.encode()).hexdigest()}
    retain('response', 'mixed-schema-seed', outcome)
    if proc.returncode != 0:
        raise RuntimeError('mixed-schema fixture creation failed')
    result['mixed_schema'] = {'schema': 'honua_data', 'table': mixed_table,
                              'source_sha256': hashlib.sha256(rows).hexdigest()}
    authorize_bound()
    result['runtime_identity'] = runtime
    return result
