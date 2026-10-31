#!/usr/bin/env python3
"""Aggregate the first-attempt canary receipts, rejecting incomplete trial sets."""
from __future__ import annotations
import argparse
from collections import Counter
import importlib.util
import hashlib
import json
from pathlib import Path
import re

_spec = importlib.util.spec_from_file_location('canary_stats', Path(__file__).resolve().parent.parent / 'certification/agent-ops/stats.py')
_stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_stats)


def aggregate(root: Path, trials: int, *, cell: str, guidance: str, model: str, lock_digest: str | None) -> dict:
    if not 1 <= trials <= 10:
        raise ValueError('trials must be between 1 and 10')
    seen = {}
    receipt_digests = set()
    for folder in root.iterdir():
        match = re.fullmatch(r'terminal-model-trial-(\d+)-attempt-1', folder.name)
        if not match or not folder.is_dir():
            raise ValueError(f'unexpected trial artifact {folder.name}')
        trial = int(match.group(1))
        if trial not in range(1, trials + 1) or trial in seen:
            raise ValueError(f'duplicate or unexpected trial {trial}')
        paths = list(folder.rglob('terminal-model-canary-receipt.json'))
        if len(paths) != 1:
            raise ValueError(f'trial {trial} has {len(paths)} receipts')
        data = paths[0].read_bytes()
        receipt_hash = hashlib.sha256(data).hexdigest()
        if receipt_hash in receipt_digests:
            raise ValueError(f"trial {trial} duplicates another receipt")
        receipt_digests.add(receipt_hash)
        receipt = json.loads(data)
        if (receipt.get('attempt') != 1 or receipt.get('mode') != 'genuine-model'
            or receipt.get('cell') != cell or receipt.get('guidance') != guidance
            or (receipt.get('endpoint') or {}).get('model') != model
            or receipt.get('lockDigest') != lock_digest):
            raise ValueError(f'trial {trial} identity does not match the requested arm')
        if lock_digest and receipt.get('status') == 'pass':
            binding, image = receipt.get('lockVerification') or {}, receipt.get('serverImage') or {}
            if (binding.get('lockDigest') != lock_digest or binding.get('serverDigest') != image.get('digest')
                    or binding.get('observedReference') != image.get('reference')
                    or binding.get('observationSource') not in ('docker-pull', 'ecs-task-readback')):
                raise ValueError(f'trial {trial} lacks the verified lock/image binding')
        status, attribution = receipt.get('status'), receipt.get('failureAttribution')
        if status not in ('pass', 'fail', 'blocked', 'skipped') or (status == 'pass' and attribution is not None) or (status != 'pass' and attribution not in ('model', 'infrastructure', 'undetermined')):
            raise ValueError(f'trial {trial} has invalid outcome or attribution')
        seen[trial] = {'trial': trial, 'status': status, 'failureAttribution': attribution, 'receiptSha256': receipt_hash}
    if set(seen) != set(range(1, trials + 1)):
        raise ValueError(f'missing trials: {sorted(set(range(1, trials + 1)) - set(seen))}')
    passed = sum(row['status'] == 'pass' for row in seen.values())
    counts = Counter(row['failureAttribution'] for row in seen.values() if row['status'] != 'pass')
    return {'guidance': guidance, 'model': model, 'cell': cell, 'passed': passed, 'trials': trials,
            'failureAttribution': {key: counts[key] for key in ('model', 'infrastructure', 'undetermined')},
            'clopperPearson95': [_stats.clopper_pearson_lower(passed, trials, .025), _stats.clopper_pearson_upper(passed, trials, .025)],
            'results': [seen[i] for i in sorted(seen)]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--trials', type=int, required=True)
    p.add_argument('--cell', required=True)
    p.add_argument('--guidance', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--lock-digest', default='')
    args = p.parse_args()
    report = aggregate(args.directory, args.trials, cell=args.cell, guidance=args.guidance, model=args.model, lock_digest=args.lock_digest or None)
    print(json.dumps(report, indent=2))

if __name__ == '__main__':
    main()
