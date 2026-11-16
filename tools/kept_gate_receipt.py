#!/usr/bin/env python3
"""Adapt a kept gate's validated verdict; never turn bootstrap/skip into proof."""
from __future__ import annotations

import argparse
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


def emit(gate, status, manifest, lock_digest, out, *, server_sha='', image_digest='', proof=None):
    if status != 'pass':
        raise ValueError('only a validated pass can emit a promise receipt')
    if manifest is not None:
        server = yaml.safe_load(manifest.read_text())['components']['honua-server']
        server_sha, image_digest = server['sha'], server['digest']
    if not re.fullmatch(r'[0-9a-f]{40}', server_sha):
        raise ValueError('missing immutable server source SHA')
    for digest in (image_digest, lock_digest):
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError('missing immutable candidate digest')
    doc = {'schema': 'honua.release-promise-receipt/v1', 'row': ROWS[gate],
           'status': 'PASS', 'serverSha': server_sha, 'imageDigest': image_digest,
           'platformLockDigest': lock_digest,
           'observedAt': dt.datetime.now(dt.timezone.utc).isoformat(),
           'assertions': {'producerPassed': True}}
    documents = [doc]
    if gate == 'gate-upgrade':
        assertions = ('live', 'rollingUpgrade', 'nMinusOneReaderCompatible', 'rollbackPassed', 'launchStack')
        if (not isinstance(proof, dict) or any(proof.get(k) != doc[k]
                for k in ('serverSha', 'imageDigest', 'platformLockDigest'))
                or any(proof.get('assertions', {}).get(k) is not True for k in assertions)):
            raise ValueError('Launch Stack upgrade and rollback proof is missing or belongs to another candidate')
        doc['assertions'].update({k: True for k in assertions})
    if gate == 'gate-dr':
        # The workflow first validates/attests this full-platform receipt against this manifest.
        if not isinstance(proof, dict) or proof.get('status') != 'pass':
            raise ValueError('validated full-platform restore receipt required')
        milliseconds = proof.get('measurements', {}).get('rtoMs')
        if type(milliseconds) not in (float, int) or not math.isfinite(milliseconds) or milliseconds < 0:
            raise ValueError('restore time must be a finite nonnegative number')
        value = milliseconds / 60000
        documents.append({**doc, 'row': 'recovery-operates-restore', 'value': value,
                          'unit': 'minutes', 'assertions': {'live': True, 'releaseNotesReported': True}})
        notes = f'<!-- honua-outcome:recovery-operates-restore --> {value} minutes\n'
    else:
        notes = None
    out.mkdir(parents=True, exist_ok=False)
    for receipt in documents:
        (out / (receipt['row'] + '.json')).write_text(json.dumps(receipt, indent=2) + '\n')
    if notes is not None:
        (out / 'release-notes.md').write_text(notes)
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
    emit(a.gate, a.status, a.manifest, a.lock_digest, a.out,
         server_sha=a.server_sha, image_digest=a.image_digest,
         proof=json.loads(a.proof.read_text()) if a.proof else None)


if __name__ == '__main__':
    main()
