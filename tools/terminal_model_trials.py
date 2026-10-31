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
from decimal import Decimal, InvalidOperation

_spec = importlib.util.spec_from_file_location('canary_stats', Path(__file__).resolve().parent.parent / 'certification/agent-ops/stats.py')
_stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_stats)


def aggregate(root: Path, trials: int, *, cell: str, guidance: str, model: str, lock_digest: str | None) -> dict:
    if not 1 <= trials <= 10:
        raise ValueError('trials must be between 1 and 10')
    seen = {}
    receipt_digests = set()
    for folder in root.iterdir():
        match = re.fullmatch(r'terminal-model-canary-trial-(\d+)-attempt-1', folder.name)
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
                    or binding.get('observationSource') not in ('docker-pull', 'ecs-task-readback', 'lambda-function-readback')):
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



def aggregate_costs(root: Path, trials: int, run_id: str, ceiling: Decimal = Decimal("20")) -> dict:
    """Require one first-attempt, run-bound cost estimate per cloud trial."""
    costs = {}
    for folder in root.iterdir():
        match = re.fullmatch(r"cloud-gate-report-trial-(\d+)", folder.name)
        if not match or not folder.is_dir():
            raise ValueError(f"unexpected cloud cost artifact {folder.name}")
        number = int(match.group(1))
        if number not in range(1, trials + 1) or number in costs:
            raise ValueError(f"duplicate or unexpected cloud cost trial {number}")
        paths = list(folder.rglob("gate-report-cloud.json"))
        if len(paths) != 1:
            raise ValueError(f"trial {number} lacks exactly one cloud cost report")
        cost = json.loads(paths[0].read_bytes()).get("finalCost") or {}
        if (cost.get("scope") != "run" or cost.get("runId") != run_id
                or cost.get("runAttempt") != "1" or cost.get("currency") != "USD"
                or cost.get("status") not in ("pass", "fail")):
            raise ValueError(f"trial {number} has no first-attempt run-bound cost estimate")
        try:
            amount = Decimal(str(cost["amountUsd"]))
        except (KeyError, InvalidOperation) as exc:
            raise ValueError(f"trial {number} has an invalid cost estimate") from exc
        if not amount.is_finite() or amount < 0:
            raise ValueError(f"trial {number} has an invalid cost estimate")
        costs[number] = amount
    if set(costs) != set(range(1, trials + 1)):
        raise ValueError("missing cloud trial cost reports")
    total = sum(costs.values(), Decimal(0))
    if total > ceiling:
        raise ValueError(f"cloud trial estimate ${total} exceeds the ${ceiling} run ceiling")
    return {"estimateUsd": str(total), "ceilingUsd": str(ceiling),
            "trialEstimatesUsd": {str(k): str(v) for k, v in sorted(costs.items())}}

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory', type=Path)
    p.add_argument('--cost-directory', type=Path)
    p.add_argument('--run-id')
    p.add_argument('--trials', type=int, required=True)
    p.add_argument('--cell')
    p.add_argument('--guidance')
    p.add_argument('--model')
    p.add_argument('--lock-digest', default='')
    args = p.parse_args()
    if args.cost_directory:
        if not args.run_id:
            p.error("--cost-directory requires --run-id")
        report = aggregate_costs(args.cost_directory, args.trials, args.run_id)
    else:
        if not args.directory or not args.cell or not args.guidance or not args.model:
            p.error("--directory, --cell, --guidance and --model are required")
        report = aggregate(args.directory, args.trials, cell=args.cell, guidance=args.guidance, model=args.model, lock_digest=args.lock_digest or None)
    print(json.dumps(report, indent=2))

if __name__ == '__main__':
    main()
