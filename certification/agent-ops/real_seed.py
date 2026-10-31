"""Seed a development town through the existing controller import/publication API.

This supplies only the three town layers. Projected data, messy schemas, ACLs,
saved maps and distinct principals still need their own verified setup; this
module alone cannot enable a priced trial.
"""
from __future__ import annotations

import json
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
from agent_e2e_scenario_backend import TownSetup  # noqa: E402
from agent_e2e_setup_http import SetupHttp  # noqa: E402

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
        not any(p.rsplit(':', 1)[-1] == str(origin.port) for p in ports)):
        raise ValueError('trial server image or endpoint mismatch')
    return {'project': project, 'container': container, 'image_id': actual,
            'image_ref': image_ref, 'base_url': base_url}


def prepare_town(*, variant: str, namespace: str, base_url: str, credential_env: str,
                 candidate_digest: str, connection_id: str, work: Path, project: str,
                 compose_file: Path, image_ref: str, authorize) -> dict:
    """Import and publish only the selected development town, retaining every intent.

    No held-out pack is opened. Only the three selected A/B layer files are
    copied into a new private directory, and TownSetup verifies their bytes
    against the checked-in manifest before upload.
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
    selected = {f'town-{variant}/{name}.3857.geojson' for name in ('parcels', 'flood_zones', 'shelters')}
    manifest = json.loads((CORPUS / 'manifest.v1.json').read_text(encoding='utf-8'))
    entries = [row for row in manifest['files'] if row.get('path') in selected]
    if len(entries) != len(selected) or {row['path'] for row in entries} != selected:
        raise ValueError('selected town manifest incomplete')
    for row in entries:
        relative = Path(row['path'])
        dest = work / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CORPUS / relative, dest)
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

    http = SetupHttp(base_url, credential_env,
                     identity={'candidateKey': candidate_digest, 'attemptId': namespace.replace('_', '-')},
                     authorize=authorize, retain=retain, timeout=300)
    result = TownSetup(http, manifest_path).prepare(variant=variant, namespace=namespace,
                                                   starting_state='published-town', connection_id=connection_id)
    result['runtime_identity'] = runtime
    return result
