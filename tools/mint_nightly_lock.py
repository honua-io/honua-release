#!/usr/bin/env python3
"""Mint a canonical signed nightly lock only for a complete, candidate-bound green train.

Keyless blob signing is separate from publication tag signing: nightly never moves
channels or creates a GA tag. Existing tag_signing validates the release label.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from candidate_binding import REQUIRED_RELEASE_GATES, validate_live_report, _sha256
from generate_platform_lock import generate
from platform_lock_bundle import bind, bundle_files, canonical_bytes
from tag_signing import publication_tag

REQUIRED_NIGHTLY_GATES = REQUIRED_RELEASE_GATES | {'capacity-soak', 'one-operation-rollback', 'journey'}
LABEL = re.compile(r'(?:honua-)?2026\.1-rc\.([1-9][0-9]*)\Z')


def next_label(history: Path) -> str:
    numbers = [2]  # #383: rc.2 was published; the first real nightly cut is rc.3.
    if history.exists():
        for path in history.rglob('platform-lock.json'):
            lock = json.loads(path.read_text())
            match = LABEL.fullmatch(str(lock.get('platform', {}).get('id', '')))
            if match:
                numbers.append(int(match.group(1)))
    return f'2026.1-rc.{max(numbers) + 1}'


def failures(report: dict) -> list[str]:
    errors = []
    ok, why = validate_live_report(report)
    if not ok:
        errors.append(why)
    rows = report.get('gates', [])
    names = {row.get('gate') for row in rows if isinstance(row, dict)}
    errors.extend(f'{name}: missing required gate' for name in sorted(REQUIRED_NIGHTLY_GATES - names))
    errors.extend(f"{row.get('gate', '<unnamed>')}: {row.get('status', '<missing>')} ({row.get('why', '')})"
                  for row in rows if isinstance(row, dict) and row.get('status') != 'pass')
    return errors


def sign_blob(lock_path: Path, bundle_path: Path, identity: str, issuer: str) -> None:
    subprocess.run(['cosign', 'sign-blob', '--yes', '--bundle', str(bundle_path), str(lock_path)], check=True)
    subprocess.run(['cosign', 'verify-blob', '--bundle', str(bundle_path),
                    '--certificate-identity', identity, '--certificate-oidc-issuer', issuer,
                    str(lock_path)], check=True)


def mint(report: dict, manifest: Path, matrix: Path, history: Path, output: Path,
         identity: str, issuer='https://token.actions.githubusercontent.com', *, signer=sign_blob) -> str:
    errors = failures(report)
    if errors:
        raise ValueError('no lock minted:\n' + '\n'.join(errors))
    candidate = report.get('candidate') or {}
    pins = candidate.get('artifacts') or {}
    for path in (manifest, matrix):
        record = pins.get(path.name) or {}
        if record.get('sha256') != _sha256(path) or record.get('size') != path.stat().st_size:
            raise ValueError(f'no lock minted: report is not bound to {path.name} bytes')
    label = next_label(history)
    publication_tag(label)  # Reuse the ruled platform label syntax; create no publication tag.
    if report.get('platform_label') != label:
        raise ValueError(f'no lock minted: report label must be next candidate {label}')
    draft = generate(manifest, matrix)
    if draft.unresolved:
        raise ValueError('no lock minted: unresolved lock facts:\n' + '\n'.join(draft.unresolved))
    bind(draft.lock, manifest, matrix, label)
    if output.exists():
        raise ValueError(f'no lock minted: immutable output already exists: {output}')
    output.parent.mkdir(parents=True, exist_ok=True)
    # Failed generation, signing or verification leaves neither a lock nor a partial bundle.
    with tempfile.TemporaryDirectory(prefix='.nightly-', dir=output.parent) as directory:
        staging = Path(directory) / label
        staging.mkdir()
        for name, data in bundle_files(draft.lock).items():
            (staging / name).write_bytes(data)
        (staging / 'gate-report.json').write_bytes(canonical_bytes(report))
        signer(staging / 'platform-lock.json', staging / 'platform-lock.sigstore.json', identity, issuer)
        if not (staging / 'platform-lock.sigstore.json').is_file():
            raise ValueError('no lock minted: signer returned no signature bundle')
        staging.rename(output)
    return label


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', type=Path, default=Path('nightly-history'))
    parser.add_argument('--next-label', action='store_true')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--matrix', type=Path)
    parser.add_argument('--out-dir', type=Path, default=Path('nightly-lock'))
    parser.add_argument('--certificate-identity')
    args = parser.parse_args(argv)
    try:
        if args.next_label:
            print(next_label(args.history))
        else:
            if not all((args.report, args.manifest, args.matrix, args.certificate_identity)):
                raise ValueError('report, candidate inputs and trusted signing identity are required')
            label = mint(json.loads(args.report.read_text()), args.manifest, args.matrix,
                         args.history, args.out_dir, args.certificate_identity)
            print(f'MINTED: {label} -> {args.out_dir}')
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
