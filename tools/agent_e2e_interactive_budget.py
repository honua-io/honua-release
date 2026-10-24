"""Bind ordinary workspace accounting to original authenticated funding source.

This runs in the checked installer/freeze workflow after verify_import. The
private runtime consumes the retained result; it does not invent a new expiry or
need Git/AWS authority merely to quote an ordinary human turn.
"""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import time

from agent_e2e_bootstrap import validate_import
from agent_e2e_pack import PackError, digest_bytes

SCHEMA = 'honua.workspace-execution-accounting/v1'
SOURCE_PATH = 'certification/agent-e2e/bootstrap.json'
ROOT = Path(__file__).resolve().parents[1]


def source_file(source_sha, path):
    # SHA and fixed path are validated against the authenticated producer before
    # this call. A missing original Git object refuses; current source is no fallback.
    result = subprocess.run(['git', 'show', source_sha+':'+path], cwd=ROOT, capture_output=True)
    if result.returncode:
        raise PackError('original funding source configuration is unavailable')
    return result.stdout


def funding_binding(authenticated, *, source_reader=source_file, now_epoch=None, require_live=True):
    artifact = authenticated['artifact']
    validate_import(authenticated, artifact['candidateKey'])
    source = artifact['source']
    raw = source_reader(source['sourceSha'], SOURCE_PATH)
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 8*1024*1024:
        raise PackError('original funding source configuration missing or oversized')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value: raise PackError('duplicate original funding configuration field')
            value[key] = item
        return value
    try:
        configuration = json.loads(raw, object_pairs_hook=unique)
        if (not isinstance(configuration, dict) or set(configuration) !=
                {'schemaVersion', 'candidateKey', 'history', 'ledger', 'expiresAt', 'liveLiabilityReferences'} or
                type(configuration['schemaVersion']) is not int or configuration['schemaVersion'] != 1 or
                configuration['candidateKey'] != artifact['candidateKey'] or configuration['ledger'] != artifact['ledger']):
            raise PackError('original funding source does not bind the authenticated candidate ledger')
        expiry = datetime.fromisoformat(configuration['expiresAt'].replace('Z', '+00:00'))
        now = int(time.time()) if now_epoch is None else now_epoch
        if (type(require_live) is not bool or expiry.tzinfo is None or type(now) is not int or now <= 0 or
                expiry.timestamp() != int(expiry.timestamp()) or expiry.timestamp() <= 0 or
                require_live and int(expiry.timestamp()) <= now):
            raise PackError('original funding expired; activation cannot extend it')
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        if isinstance(error, PackError): raise
        raise PackError('original funding source configuration is invalid') from error
    return {'schema': SCHEMA, 'bootstrapArtifactDigest': authenticated['artifactDigest'],
            'source': source.copy(), 'configurationSha256': digest_bytes(raw),
            'originalExpiresEpoch': int(expiry.timestamp())}, raw


def validate_retained_funding(state):
    """Replay the already captured original; historical observation grants no work."""
    marker = state.get('ledger', {}).get('interactiveAccounting')
    if marker is None:
        if 'interactiveFunding' in state: raise PackError('interactive funding has no ledger capability binding')
        return
    proof = state.get('interactiveFunding')
    if (not isinstance(proof, dict) or set(proof) != {'binding', 'configuration'} or
            proof['binding'] != marker or not isinstance(proof['configuration'], str) or 'bootstrapImport' not in state):
        raise PackError('interactive accounting lacks retained original funding source')
    raw = proof['configuration'].encode('utf-8')
    replay, _ = funding_binding(state['bootstrapImport'], source_reader=lambda sha, path: raw, require_live=False)
    if replay != marker:
        raise PackError('retained interactive funding source/expiry changed')
