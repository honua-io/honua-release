"""Project governed rows into bounded data-only producer dispatch context."""
from __future__ import annotations

import json
import re
import runpy
from pathlib import Path

SCHEMA = 'honua.protocol-producer-context/v1'
MAX_BYTES = 60000
KEYS = ('capability_key', 'surface', 'operation', 'canonical_client', 'client_version',
        'client_lane', 'deployment_target', 'fixture_revision', 'contract_revision',
        'auth_policy_revision', 'scenario_facets', 'maturity', 'required_tier')
PRODUCERS = {
    'honua-server-cite': ('honua-io/honua-server', 'cite-certification.yml'),
    'server-protocol-harness': ('honua-io/honua-server', 'protocol-harness-certification.yml'),
    'honua-sdk-js': ('honua-io/honua-sdk-js', 'integration.yml'),
}


def context(catalog: dict, producer: dict, candidate: dict, pin: str) -> str:
    identity = PRODUCERS.get(producer.get('producer'))
    if identity != (producer.get('repository'), producer.get('workflow')):
        raise ValueError('producer context requires the exact reviewed repository/workflow')
    revision = catalog.get('revision')
    if not isinstance(revision, str) or not revision.strip() or len(revision) > 200:
        raise ValueError('producer context requires a bounded catalog revision')
    if not re.fullmatch('[0-9a-f]{40}', pin):
        raise ValueError('producer context requires a full source pin')
    selects = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'certification/generate-protocol-requirements.py'), run_name='producer_context_policy')['selects']
    rows = []
    seen = set()
    for row in catalog.get('requirements', []):
        if not selects(producer, row) or row.get('addressable_by_client') is not True:
            continue
        selected = {key: row[key] for key in KEYS}
        if 'test_ids' in row:
            selected['test_ids'] = row['test_ids']
        key = tuple(row[field] for field in ('surface', 'operation', 'canonical_client', 'client_version', 'deployment_target'))
        if key in seen:
            raise ValueError('producer context contains duplicate requirement identities')
        seen.add(key)
        if selected['maturity'] not in {'supported', 'preview', 'experimental', 'roadmap', 'deprecated', 'internal'} or selected['required_tier'] not in {'pr', 'nightly', 'release'}:
            raise ValueError('producer context has unknown maturity or tier')
        rows.append(selected)
    if not rows or len(rows) != producer.get('cells'):
        raise ValueError('producer context does not match its declared cell count')
    value = {'schema': SCHEMA, 'requirements_revision': revision,
             'producer': producer['producer'], 'producer_source_sha': pin,
             'candidate': {'source_sha': candidate['server_sha'], 'image_digest': candidate['image_digest'], 'cut_at': candidate['cut_at']},
             'requirements': rows}
    encoded = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    if len(encoded.encode()) > MAX_BYTES:
        raise ValueError('producer context exceeds the 60000-byte dispatch bound')
    return encoded
