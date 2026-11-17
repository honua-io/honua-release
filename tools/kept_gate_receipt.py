#!/usr/bin/env python3
"""Adapt a kept gate's validated verdict; never turn bootstrap/skip into proof."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import json
import math
from pathlib import Path
import re

import yaml

from candidate_lock import PinnedCandidate, resolve_pins, validate_lock

ROWS = {
    'gate-sbom': 'supply-sbom', 'gate-security': 'supply-scan',
    'gate-contract': 'contract-compat', 'gate-contract-live': 'contract-compat',
    'gate-docs': 'docs-executable', 'gate-executable-docs': 'docs-executable',
    'gate-installed-clients': 'clients-installed', 'gate-observability': 'operates-telemetry',
    'gate-dr': 'operates-restore', 'gate-protocol-certification': 'protocol-compat',
    'capacity-soak': 'operates-capacity', 'gate-upgrade': 'operates-8-upgrade',
}


@dataclass(frozen=True)
class Verdict:
    gate: str
    status: str
    manifest: Path | None
    lock_digest: str
    server_sha: str = ''
    image_digest: str = ''
    proof: dict | None = None
    lock: Path | None = None


def candidate_document(verdict):
    if verdict.status != 'pass':
        raise ValueError('only a validated pass can emit a promise receipt')
    if verdict.manifest is None or verdict.lock is None:
        raise ValueError('candidate manifest and lock bytes are required')
    raw = verdict.manifest.read_bytes()
    manifest = yaml.safe_load(raw)
    pins = resolve_pins(manifest, ('server',))
    validate_lock(verdict.lock.read_bytes(), verdict.lock_digest, PinnedCandidate(raw, pins))
    server_sha, image_digest = pins['server']['sha'], pins['server']['digest']
    if verdict.gate == 'gate-protocol-certification' and (
            verdict.server_sha != server_sha or verdict.image_digest != image_digest):
        raise ValueError('protocol evaluated another candidate')
    if verdict.gate == 'gate-protocol-certification':
        validate_protocol(manifest, verdict.proof)
    if not re.fullmatch(r'[0-9a-f]{40}', server_sha):
        raise ValueError('missing immutable server source SHA')
    for digest in (image_digest, verdict.lock_digest):
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError('missing immutable candidate digest')
    return {'schema': 'honua.release-promise-receipt/v1', 'row': ROWS[verdict.gate],
            'status': 'PASS', 'serverSha': server_sha, 'imageDigest': image_digest,
            'platformLockDigest': verdict.lock_digest,
            'observedAt': dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z'),
            'assertions': {'producerPassed': True}}


def validate_protocol(manifest, proof):
    try:
        certification = manifest['protocolCertification']
        expected = {
            'EXPECTED_REQUIREMENTS_REVISION': certification['ledger']['requirementsSourceRevision'],
            'EXPECTED_CUT_AT': certification['candidateCutAt'],
            'EXPECTED_SERVER_CERTIFICATION_SHA': certification['serverCertificationProducerSha'],
        }
        for short, component in (('SDK_JS', 'honua-sdk-js'), ('SDK_PYTHON', 'honua-sdk-python'),
                ('SDK_DOTNET', 'honua-sdk-dotnet'), ('GEOSPATIAL_GRPC', 'geospatial-grpc'),
                ('GEOSPATIAL_MCP', 'geospatial-mcp')):
            expected['EXPECTED_' + short + '_SHA'] = manifest['components'][component]['sha']
        for short, artifact in (('SDK_JS', 'honua-sdk-js'), ('SDK_PYTHON', 'honua-sdk-python-wheel'),
                ('SDK_DOTNET', 'honua-sdk-dotnet')):
            expected['EXPECTED_' + short + '_VERSION'] = manifest['clientArtifacts'][artifact]['version']
        if not isinstance(proof, dict) or any(
                not isinstance(value, str) or not value or proof.get(key) != value
                for key, value in expected.items()):
            raise ValueError('protocol evaluated another component set')
    except (KeyError, TypeError) as exc:
        raise ValueError('protocol candidate component pins are missing') from exc


def validate_upgrade(doc, proof):
    assertions = ('live', 'rollingUpgrade', 'nMinusOneReaderCompatible', 'rollbackPassed', 'launchStack')
    if (not isinstance(proof, dict) or any(proof.get(k) != doc[k]
            for k in ('serverSha', 'imageDigest', 'platformLockDigest'))
            or any(proof.get('assertions', {}).get(k) is not True for k in assertions)):
        raise ValueError('Launch Stack upgrade and rollback proof is missing or belongs to another candidate')
    doc['assertions'].update({k: True for k in assertions})


def recovery_outcome(doc, proof):
    # The workflow validates/attests the full-platform receipt against the manifest first.
    if not isinstance(proof, dict) or proof.get('status') != 'pass':
        raise ValueError('validated full-platform restore receipt required')
    milliseconds = proof.get('measurements', {}).get('rtoMs')
    if type(milliseconds) not in (float, int) or not math.isfinite(milliseconds) or milliseconds < 0:
        raise ValueError('restore time must be a finite nonnegative number')
    return {**doc, 'row': 'recovery-operates-restore', 'value': milliseconds / 60000,
            'unit': 'minutes', 'assertions': {'live': True, 'releaseNotesReported': True}}


def validate_capacity(proof):
    try:
        ended = dt.datetime.fromisoformat(proof['window']['endedAt'].replace('Z', '+00:00'))
        age = dt.datetime.now(dt.timezone.utc) - ended
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError('capacity source observation time is required') from exc
    if not dt.timedelta(0) <= age <= dt.timedelta(days=7):
        raise ValueError('capacity source evidence is stale or from the future')
    return proof['window']['endedAt']


def ensure_lock(lock, manifest, generate=None):
    """Regenerate absent qualification-lock bytes from the exact manifest and matrix the gate read.

    Producers receive only the lock digest; release-train freezes the same canonical bytes from
    these inputs. candidate_document still requires sha256(lock) == the supplied digest and that
    the lock binds the manifest pins, so regeneration cannot admit another candidate.
    """
    if lock.exists() or manifest is None:
        return
    beside = manifest.parent / 'compatibility-matrix.yaml'
    matrix = beside if beside.exists() else Path('compatibility-matrix.yaml')
    if generate is None:
        import generate_platform_lock as generator
        from platform_lock_bundle import canonical_bytes

        def generate(manifest_path, matrix_path):
            draft = generator.generate(manifest_path, matrix_path, qualification=True,
                                       image_inspector=generator.registry_image_platform_digests)
            return canonical_bytes(draft.lock)
    lock.write_bytes(generate(manifest, matrix))


def emit(verdict, out):
    doc = candidate_document(verdict)
    documents = [doc]
    if verdict.gate == 'capacity-soak':
        doc['sourceObservedAt'] = validate_capacity(verdict.proof)
    if verdict.gate == 'gate-upgrade':
        validate_upgrade(doc, verdict.proof)
    if verdict.gate == 'gate-dr':
        documents.append(recovery_outcome(doc, verdict.proof))
    out.mkdir(parents=True, exist_ok=False)
    for receipt in documents:
        (out / (receipt['row'] + '.json')).write_text(json.dumps(receipt, indent=2) + '\n')
    if verdict.gate == 'gate-dr':
        value = documents[-1]['value']
        (out / 'release-notes.md').write_text(
            f'<!-- honua-outcome:recovery-operates-restore --> {value} minutes\n')
    return documents


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--gate', choices=ROWS, required=True)
    p.add_argument('--status', required=True)
    p.add_argument('--manifest', type=Path)
    p.add_argument('--lock-digest', required=True)
    p.add_argument('--lock', type=Path, required=True)
    p.add_argument('--server-sha', default='')
    p.add_argument('--image-digest', default='')
    p.add_argument('--proof', type=Path)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    if a.status == 'pass':
        ensure_lock(a.lock, a.manifest)
    emit(Verdict(gate=a.gate, status=a.status, manifest=a.manifest, lock_digest=a.lock_digest,
                 server_sha=a.server_sha, image_digest=a.image_digest, lock=a.lock,
                 proof=json.loads(a.proof.read_text()) if a.proof else None), a.out)


if __name__ == '__main__':
    main()
