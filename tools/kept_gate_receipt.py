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


def candidate_document(verdict):
    if verdict.status != 'pass':
        raise ValueError('only a validated pass can emit a promise receipt')
    server_sha, image_digest = verdict.server_sha, verdict.image_digest
    if verdict.manifest is not None:
        server = yaml.safe_load(verdict.manifest.read_text())['components']['honua-server']
        server_sha, image_digest = server['sha'], server['digest']
    if not re.fullmatch(r'[0-9a-f]{40}', server_sha):
        raise ValueError('missing immutable server source SHA')
    for digest in (image_digest, verdict.lock_digest):
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError('missing immutable candidate digest')
    return {'schema': 'honua.release-promise-receipt/v1', 'row': ROWS[verdict.gate],
            'status': 'PASS', 'serverSha': server_sha, 'imageDigest': image_digest,
            'platformLockDigest': verdict.lock_digest,
            'observedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
            'assertions': {'producerPassed': True}}


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


def emit(verdict, out):
    doc = candidate_document(verdict)
    documents = [doc]
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
    p.add_argument('--server-sha', default='')
    p.add_argument('--image-digest', default='')
    p.add_argument('--proof', type=Path)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    emit(Verdict(gate=a.gate, status=a.status, manifest=a.manifest, lock_digest=a.lock_digest,
                 server_sha=a.server_sha, image_digest=a.image_digest,
                 proof=json.loads(a.proof.read_text()) if a.proof else None), a.out)


if __name__ == '__main__':
    main()
