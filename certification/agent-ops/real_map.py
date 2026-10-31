"""Create one real, isolated saved-map fixture for an unpriced Compose smoke.

This is not a complete trial fixture: layer-level sharing and a task principal's
actual model session still need independent evidence before priced use.
"""
from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import subprocess
import urllib.request
import uuid
from pathlib import Path

from real_seed import _verify_runtime
from agent_e2e_setup_http import SetupHttp
from agent_e2e_scenario_operations import data
from agent_e2e_studio_fixture import exact_envelope, validation
from local_http import open_local, require_loopback


def observe_public_source(base_url: str, service: str, layer_id: int) -> dict:
    require_loopback(base_url)
    if not re.fullmatch(r'[a-z0-9_-]{1,100}', service) or type(layer_id) is not int or layer_id < 0:
        raise ValueError('invalid public map source')
    path = f'/rest/services/{service}/FeatureServer/{layer_id}/query?where=1%3D1&outFields=*&f=geojson'
    with open_local(urllib.request.Request(base_url.rstrip('/') + path), timeout=10) as response:
        raw, status = response.read(2_000_001), response.status
    collection = json.loads(raw)
    if (status != 200 or len(raw) > 2_000_000 or not isinstance(collection, dict) or
        collection.get('type') != 'FeatureCollection' or not isinstance(collection.get('features'), list) or
        not collection['features']):
        raise ValueError('saved map public source did not serve features')
    return {'path': path, 'status': status, 'sha256': hashlib.sha256(raw).hexdigest(),
            'body_b64': base64.b64encode(raw).decode(), 'feature_count': len(collection['features'])}


def same_public_source(before: dict, after: dict) -> bool:
    """Compare all source data, disregarding only feature enumeration order."""
    def decode(receipt):
        if not isinstance(receipt, dict) or receipt.get('status') != 200:
            raise ValueError('source receipt missing')
        raw = base64.b64decode(receipt['body_b64'], validate=True)
        if hashlib.sha256(raw).hexdigest() != receipt.get('sha256'):
            raise ValueError('source receipt digest mismatch')
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get('type') != 'FeatureCollection' or
            not isinstance(value.get('features'), list) or not value['features'] or
            any(not isinstance(feature, dict) for feature in value['features']) or
            len(value['features']) != receipt.get('feature_count')):
            raise ValueError('source receipt incomplete')
        value['features'] = sorted(value['features'], key=lambda feature:
            json.dumps(feature, sort_keys=True, separators=(',', ':'), allow_nan=False))
        return value
    try:
        return before.get('path') == after.get('path') and decode(before) == decode(after)
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


def prepare_map(*, project: str, compose_file: Path, image_ref: str, base_url: str,
                work: Path, service: str, layer_id: int, controller_env: str,
                operator_env: str, operator_id: str) -> dict:
    """Use actual Studio draft, validation and save APIs with retained receipts."""
    if (not image_ref.count('@sha256:') == 1 or not re.fullmatch(r'[a-z0-9_-]{1,100}', service) or type(layer_id) is not int or
        layer_id < 0 or not os.environ.get(controller_env) or not os.environ.get(operator_env) or
        os.environ[controller_env] == os.environ[operator_env] or work.exists()):
        raise ValueError('invalid map setup inputs')
    owner = str(uuid.UUID(operator_id))
    digest = image_ref.split('@', 1)[1]
    runtime = _verify_runtime(project, compose_file, image_ref, digest, base_url, excluded_env=(operator_env,))
    work.mkdir(mode=0o700)
    directory = os.open(work.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    item = str(uuid.uuid4())
    namespace = 'ops_map_' + uuid.uuid4().hex[:16]
    def authorize():
        if _verify_runtime(project, compose_file, image_ref, digest, base_url, excluded_env=(operator_env,)) != runtime:
            raise ValueError('map setup runtime changed')
    def retain(kind, step, record):
        from agent_e2e_pack import canonical_digest
        target = work / (kind + '-' + step + '.json')
        encoded = json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        directory = os.open(work, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {'key': target.name, 'sha256': canonical_digest(record)}
    authorize()
    sql = "SELECT count(*) FROM honua.studio_content_items WHERE item_id='" + item + "'::uuid"
    child_env = os.environ.copy()
    child_env.pop(operator_env, None)
    observed = subprocess.run(['docker', 'compose', '-p', project, '-f', str(compose_file.resolve()),
                               'exec', '-T', 'db', 'psql', '-v', 'ON_ERROR_STOP=1', '-U', 'honua',
                               '-d', 'honua', '-Atc', sql], capture_output=True, text=True, timeout=15,
                              check=False, env=child_env)
    if observed.returncode != 0 or observed.stdout.strip() != '0':
        raise ValueError('map identity absence not observed')
    baseline = {'project': project, 'item_id': item, 'count': 0,
                'response_sha256': hashlib.sha256(observed.stdout.encode()).hexdigest()}
    retain('response', 'map-baseline', baseline)
    authorize()
    source = observe_public_source(base_url, service, layer_id)
    retain('response', 'map-public-source', source)
    source_path = source['path']
    identity = {'candidateKey': digest, 'attemptId': namespace.replace('_', '-')}
    setup = SetupHttp(base_url, controller_env, identity=identity, authorize=authorize, retain=retain,
                      private_values=(os.environ[operator_env],), timeout=120)
    requester = SetupHttp(base_url, operator_env, identity=identity, authorize=authorize, retain=retain,
                          private_values=(os.environ[controller_env],), timeout=120)
    body = {'mapPackageId': namespace, 'format': 'honua_map_package.v1',
            'sourceBindings': [{'sourceId': 'parcels', 'protocol': 'geoservices_feature_service',
                                'locator': {'serviceId': service, 'layerId': layer_id}}],
            'mapSpec': {'version': 8,
                        'sources': {'parcels': {'type': 'geojson',
                            'data': source_path}},
                        'layers': [{'id': 'parcels', 'type': 'fill', 'source': 'parcels'}]},
            'view': {'center': [-157.8583, 21.3069], 'zoom': 5}, 'layers': [], 'widgets': [],
            'controls': [], 'interactions': []}
    envelope = {'family': 'map', 'schemaVersion': '1.0', 'format': 'honua_map_package.v1',
                'bindings': [], 'dependencies': [], 'provenance': [], 'body': body}
    create = setup.json('map-create', 'POST', '/api/v1/studio/package-drafts',
                        {'itemId': item, 'packageKey': namespace, 'workspaceId': namespace,
                         'ownerId': owner, 'envelope': envelope})
    draft = data(create, (201,))
    draft_id = str(uuid.UUID(draft.get('draftId')))
    if draft.get('itemId') != item or draft.get('ownerId') != owner:
        raise ValueError('map draft identity mismatch')
    exact_envelope(draft.get('envelope'), envelope)
    checked = setup.json('map-validate', 'POST', '/api/v1/studio/package-drafts/' + draft_id + '/validate', {})
    validation(data(checked))
    saved = setup.json('map-save', 'POST', '/api/v1/studio/package-drafts/' + draft_id + '/content-versions',
                       {'changeNote': 'Initial isolated smoke fixture'})
    version = data(saved, (201,))
    version_id = str(uuid.UUID(version.get('versionId')))
    if version.get('itemId') != item or version.get('sourceDraftId') != draft_id:
        raise ValueError('saved map identity mismatch')
    exact_envelope(version.get('envelope'), envelope)
    read = requester.json('map-operator-read', 'GET', f'/api/v1/studio/content-items/{item}/versions/{version_id}')
    if data(read) != version:
        raise ValueError('operator read differs from saved map')
    return {'item_id': item, 'draft_id': draft_id, 'version_id': version_id,
            'envelope': version['envelope'], 'baseline': baseline, 'public_source': source,
            'references': {'create': create['reference'], 'validate': checked['reference'],
                           'save': saved['reference'], 'operator_read': read['reference']}}
