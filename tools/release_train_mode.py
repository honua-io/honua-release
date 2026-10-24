#!/usr/bin/env python3
"""Refuse ambiguous release preparation and record its nonqualifying result."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlparse


def validate(env):
    preparing = env.get('FREEZE_ONLY') == 'true'
    if preparing:
        if (env.get('DRY_RUN') != 'false' or env.get('NIGHTLY') == 'true' or
                env.get('CANDIDATE_REF') or env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or
                env.get('GITHUB_REF') != 'refs/heads/trunk' or
                env.get('GITHUB_REPOSITORY') != 'honua-io/honua-release' or
                env.get('GITHUB_WORKFLOW_REF') != 'honua-io/honua-release/.github/workflows/release-train.yml@refs/heads/trunk'):
            raise ValueError('Preparation requires the actual protected trunk dispatch, current source inputs and dry_run=false')
    else:
        for key in ('CAPACITY_RECEIPT_URL', 'DR_RECEIPT_URL'):
            url = urlparse(env.get(key, ''))
            if url.scheme != 'https' or not url.netloc:
                raise ValueError(key+' is required for the full release train')
    return preparing


def preparation_receipt(lock_path):
    data = Path(lock_path).read_bytes()
    lock = json.loads(data)
    if lock.get('dryRun') is True or lock.get('platform', {}).get('status') != 'rc':
        raise ValueError('Preparation requires an actual RC qualification input lock')
    if Path(lock_path).with_name('platform-lock.sigstore.json').exists():
        raise ValueError('Preparation must not carry a release lock signature')
    return {'schema': 'honua.candidate-preparation/v1', 'qualification': False,
            'qualificationGates': 'not-run', 'releasePublished': False,
            'qualificationLockDigest': 'sha256:'+hashlib.sha256(data).hexdigest(),
            'authentication': 'signed-inputs-and-exact-qualification-regeneration'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preparation-receipt', type=Path)
    args = parser.parse_args()
    if args.preparation_receipt:
        result = preparation_receipt(args.preparation_receipt.with_name('platform-lock.json'))
        args.preparation_receipt.write_text(json.dumps(result, sort_keys=True)+'\n')
    else:
        preparing = validate(os.environ)
        print('Candidate preparation only; qualification remains pending.' if preparing else 'Full release train requested.')


if __name__ == '__main__':
    main()
