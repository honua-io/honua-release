"""Seed the state that certification/agent-safety/operation-fixtures.json names.

Several operations check their target before the policy decision: a folder, a published layer, a
style, a saved Studio version or a staged GP result must exist, or the submission fails validation
and the agent walk cannot show its proposal. The administrator key creates that state. The GP job
and its import preview are created by the agent itself, because the import is bound to the
requester that ran them. Every value returned here fills a ${name} placeholder in the fixtures.
"""
from __future__ import annotations

import base64
from http import HTTPStatus
import json
import os
import shlex
import struct
import subprocess
import time
import urllib.parse
import uuid

TABLES = ('agent_safety_probe', 'agent_safety_probe_pub')
SERVICE = 'agentsafetyseed'
FOLDER = 'agent-safety-seed'
IMPORTED_CONNECTION = 'Imported data'
PSQL = 'docker compose -f e2e/harness/compose.candidate.yml exec -T db psql -U honua -d honua -v ON_ERROR_STOP=1 -Atc'
GP_BUFFER = '/rest/services/geoprocessing/GPServer/Buffer'
GP_POLLS = 60


class SeedFailed(Exception):
    pass


def sql(statement: str) -> str:
    command = shlex.split(os.environ.get('AGENT_SAFETY_PSQL', PSQL))
    return subprocess.run([*command, statement], check=True, capture_output=True, text=True).stdout.strip()


def data(client, route: str, body=None, headers=None):
    """Call '<METHOD> <path>' and return the unwrapped JSON body and the response headers."""
    method, path = route.split(' ', 1)
    status, raw, response_headers = client.call(method, path, body, headers)
    if not HTTPStatus.OK <= status < HTTPStatus.MULTIPLE_CHOICES:
        raise SeedFailed(f'{method} {path} returned HTTP {status}')
    document = json.loads(raw) if raw.strip() else None
    if isinstance(document, dict) and 'success' in document:
        document = document.get('data')
    return document, response_headers


def seed_tables() -> None:
    for table in TABLES:
        sql(f"CREATE TABLE IF NOT EXISTS public.{table} (id serial PRIMARY KEY, name text, geom geometry(Point,4326)); "
            f"INSERT INTO public.{table}(name, geom) SELECT 'a', ST_SetSRID(ST_MakePoint(-157.8, 21.3), 4326) "
            f"WHERE NOT EXISTS (SELECT 1 FROM public.{table})")


def seed_folder(admin) -> str:
    _, _, headers = admin.call('GET', '/api/v1/admin/folders')
    created, _ = data(admin, 'POST /api/v1/admin/folders', {'name': FOLDER, 'title': 'Agent safety seed'},
                      {'If-Match': headers.get('ETag', '')})
    return created['metadata']['id']


def seed_layer(admin) -> dict:
    connections, _ = data(admin, 'GET /api/v1/admin/connections')
    connection = next((c['connectionId'] for c in connections if c.get('name') == IMPORTED_CONNECTION), None)
    if connection is None:
        raise SeedFailed(f'no "{IMPORTED_CONNECTION}" connection on the candidate')
    layer, _ = data(admin, f'POST /api/v1/admin/connections/{connection}/layers',
                    {'schema': 'public', 'table': TABLES[0], 'layerName': TABLES[0] + '_seed', 'serviceName': SERVICE})
    service_id = sql(f"SELECT service_id FROM honua.metadata_v2_services_idx WHERE name = '{SERVICE}'")
    return {'connectionId': connection, 'serviceName': layer['serviceName'], 'serviceLayerIndex': str(layer['layerId']),
            'storageLayerId': str(layer['storageLayerId']),
            'serviceId': service_id, 'table': TABLES[0], 'publishTable': TABLES[1]}


def seed_style(admin) -> str:
    style = {'version': 8, 'name': 'agent-safety-seed', 'sources': {}, 'layers': []}
    created, headers = data(admin, 'POST /ogc/styles', style, {'Content-Type': 'application/vnd.mapbox.style+json'})
    return (created or {}).get('id') or headers.get('Location', '').rstrip('/').rsplit('/', 1)[-1]


def seed_studio(admin) -> dict:
    draft, _ = data(admin, 'POST /api/v1/studio/package-drafts',
                    {'packageKey': 'agent-safety-seed', 'envelope': {'family': 'map', 'schemaVersion': '1'}})
    version, _ = data(admin, f"POST /api/v1/studio/package-drafts/{draft['draftId']}/content-versions",
                      {'changeNote': 'agent safety seed'})
    return {'studioItemId': version['itemId'], 'studioVersionId': version['versionId'],
            'studioContentHash': version['contentHash']}


def seed_console_item(admin) -> dict:
    """A personal item the administrator owns: an agent edit of it must need a person."""
    item, _ = data(admin, 'POST /api/v1/console/content',
                   {'name': 'agent-safety-seed', 'itemType': 'map', 'visibility': 'personal'})
    return {'consoleItemId': item['id'], 'consoleOwnerId': item['ownerId']}


def agent_gp_job(agent) -> str:
    point = base64.b64encode(struct.pack('<BIdd', 1, 1, -157.8, 21.3)).decode()
    form = urllib.parse.urlencode({'wkb': point, 'srid': '4326', 'distance': '0.01', 'f': 'json'}).encode()
    job, _ = data(agent, 'POST ' + GP_BUFFER + '/submitJob', form, {'Content-Type': 'application/x-www-form-urlencoded'})
    for _ in range(GP_POLLS):
        state, _ = data(agent, f"GET {GP_BUFFER}/jobs/{job['jobId']}?f=json")
        if state.get('jobStatus') == 'esriJobSucceeded':
            return job['jobId']
        time.sleep(1)
    raise SeedFailed('the agent GP buffer job did not succeed')


def agent_preview(agent) -> dict:
    job = agent_gp_job(agent)
    version, digest = sql("SELECT version || '|' || encode(sha256(convert_to(record->'artifactReferences'->>0, 'UTF8')), "
                          f"'hex') FROM honua.execution_jobs WHERE operation_id = '{job}'").split('|')
    staged = {'intentId': f'agent-safety-{uuid.uuid4().hex[:12]}', 'gpJobId': job, 'gpJobVersion': version,
              'artifactSha256': digest}
    parameters = {'intentId': staged['intentId'], 'schema': 'public', 'targetId': 'geoprocessing', 'jobId': job,
                  'resultPackageId': f'{job}:v{version}', 'artifactId': f'{job}:artifact:1',
                  'referenceSha256': digest, 'targetSrid': '4326'}
    preview, _ = data(agent, 'POST /api/v1/operations/dataset.stage.preview/submit', {'parameters': parameters})
    staged['previewFingerprint'] = preview['result']['details']['previewFingerprint']
    return staged


def seed(admin, agent) -> dict:
    """Create the fixture state; the metadata ETag is read by the walk after this returns."""
    seed_tables()
    world = {'folderId': seed_folder(admin), **seed_layer(admin), 'styleId': seed_style(admin)}
    return {**world, **seed_studio(admin), **seed_console_item(admin), **agent_preview(agent)}
