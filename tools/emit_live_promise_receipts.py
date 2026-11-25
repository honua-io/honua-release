#!/usr/bin/env python3
"""Fail-closed receipt emitter for Launch Stack, Runs-Itself, and white-hat gates."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'certification'))

import rc2_rigor as rigor
import release_adversarial as adversarial
import release_outcomes as outcomes

DIGEST_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
SHA_RE = re.compile(r'^[0-9a-f]{40}$')
STACK_RE = re.compile(r'^honua-gate-[a-zA-Z0-9-]+$')
REPORT_RE = re.compile(r'^[a-z0-9-]+\.md$')
RC_IMAGE_SIZE_BUDGET = 370_000_000
REQUIRED_WHITEHAT_REVIEWS = 2
OPERATES_IDS = tuple(
    [*(f'operates-{i}' for i in (1, 2, 3, 4, 5, 6, 9, 10, 11)), 'operates-12-notify']
)


@dataclass(frozen=True)
class EmitContext:
    proof_dir: Path
    server_sha: str
    server_digest: str
    lock_digest: str
    now: dt.datetime


def _confined_dir(raw: str) -> Path:
    path = Path(raw)
    if not raw or path.is_absolute() or '..' in path.parts or not path.is_dir():
        raise ValueError('proof directory must be an existing relative directory')
    return path


def _validate_identity(server_sha: str, server_digest: str, lock_digest: str) -> None:
    if not SHA_RE.fullmatch(server_sha):
        raise ValueError('server_sha must be 40 lowercase hex characters')
    if not DIGEST_RE.fullmatch(server_digest) or not DIGEST_RE.fullmatch(lock_digest):
        raise ValueError('server_digest and lock_digest must be sha256 digests')


def _verify_proof_binding(proof: dict, context: EmitContext) -> str:
    req = rigor.ReceiptContext('live', context.server_sha, context.server_digest, context.lock_digest)
    return rigor.verify_proof_identity_and_age(proof, req, context.now)


def _emitted_at(context: EmitContext) -> str:
    return context.now.isoformat().replace('+00:00', 'Z')


def _base_receipt(context: EmitContext, row_id: str, stack_name: str, source_observed: str) -> dict:
    return {
        'schema': 'honua.release-promise-receipt/v1',
        'row': row_id,
        'status': 'PASS',
        'serverSha': context.server_sha,
        'imageDigest': context.server_digest,
        'platformLockDigest': context.lock_digest,
        'observedAt': _emitted_at(context),
        'sourceObservedAt': source_observed,
        'stackName': stack_name,
    }


def _outcome_receipt(
    context: EmitContext, row_id: str, value: float | int, source_observed: str
) -> dict:
    if not outcomes.number(value):
        raise ValueError(f'invalid measurement for {row_id}')
    return {
        'schema': 'honua.release-promise-receipt/v1',
        'row': row_id,
        'status': 'PASS',
        'serverSha': context.server_sha,
        'imageDigest': context.server_digest,
        'platformLockDigest': context.lock_digest,
        'observedAt': _emitted_at(context),
        'sourceObservedAt': source_observed,
        'value': value,
        'unit': outcomes.unit_for(row_id),
        'assertions': {'live': True, 'releaseNotesReported': True},
    }


def _write_bundle(out: Path, receipts: list[dict], notes_docs: list[dict]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    for receipt in receipts:
        target = out / f"{receipt['row']}.json"
        target.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    if notes_docs:
        lines = [outcomes.notes_line(doc) for doc in sorted(notes_docs, key=lambda d: d['row'])]
        (out / 'release-notes.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def emit_launch_stack(context: EmitContext, out: Path) -> list[dict]:
    proof = json.loads((context.proof_dir / 'launch-stack-proof.json').read_text(encoding='utf-8'))
    source_observed = _verify_proof_binding(proof, context)
    stack = str(proof.get('stackName', ''))
    if not STACK_RE.fullmatch(stack):
        raise ValueError('Launch Stack proof requires a honua-gate-* stackName')
    size = proof.get('imageSizeBytes')
    if type(size) is not int or not 0 < size <= RC_IMAGE_SIZE_BUDGET:
        raise ValueError('Launch Stack AOT image size exceeds RC budget or is invalid')
    if (proof.get('orchestrator') != 'ecs' or proof.get('redis') is not False
            or proof.get('defaultImage') != 'postgis-only'
            or proof.get('protectionProfile') != 'Hardened'
            or proof.get('publishedAotImage') is not True):
        raise ValueError('Launch Stack proof failed ECS/Hardened/AOT assertions')
    cloud = {**_base_receipt(context, 'cloud-launch-stack', stack, source_observed),
             'assertions': {'live': True, 'orchestrator': 'ecs', 'redis': False,
                            'defaultImage': 'postgis-only'}}
    aot = {**_base_receipt(context, 'cloud-image-aot', stack, source_observed),
           'imageSizeBytes': size,
           'assertions': {'live': True, 'publishedAotImage': True, 'sizeBudgetMet': True}}
    hardened = {**_base_receipt(context, 'security-hardened', stack, source_observed),
                'assertions': {'live': True, 'protectionProfile': 'Hardened'}}
    install_min = _outcome_receipt(
        context, 'install-console', proof.get('installConsoleMinutes'), source_observed
    )
    receipts = [cloud, aot, hardened, install_min]
    _write_bundle(out, receipts, [install_min])
    return receipts


def emit_runs_itself(context: EmitContext, out: Path) -> list[dict]:
    proof = json.loads((context.proof_dir / 'runs-itself-proof.json').read_text(encoding='utf-8'))
    source_observed = _verify_proof_binding(proof, context)
    stack = str(proof.get('stackName', ''))
    if not STACK_RE.fullmatch(stack):
        raise ValueError('Runs-Itself proof requires a honua-gate-* stackName')
    rows_data = proof.get('rows')
    if not isinstance(rows_data, dict):
        raise ValueError('Runs-Itself proof requires a rows mapping')
    receipts, note_docs = [], []
    for row_id in OPERATES_IDS:
        entry = rows_data.get(row_id)
        if not isinstance(entry, dict) or entry.get('status') != 'PASS':
            raise ValueError(f'Runs-Itself row {row_id} did not pass')
        receipts.append(
            {**_base_receipt(context, row_id, stack, source_observed), 'assertions': {'live': True}}
        )
        rec = _outcome_receipt(
            context, f'recovery-{row_id}', entry.get('recoveryMinutes'), source_observed
        )
        receipts.append(rec)
        note_docs.append(rec)
    unattended = _outcome_receipt(
        context, 'unattended-person', proof.get('unattendedPersonMinutes'), source_observed
    )
    receipts.append(unattended)
    note_docs.append(unattended)
    _write_bundle(out, receipts, note_docs)
    return receipts


def emit_whitehat(context: EmitContext, out: Path) -> list[dict]:
    proof = json.loads((context.proof_dir / 'whitehat-proof.json').read_text(encoding='utf-8'))
    source_observed = _verify_proof_binding(proof, context)
    reviews = proof.get('reviews')
    if not isinstance(reviews, list) or len(reviews) != REQUIRED_WHITEHAT_REVIEWS:
        raise ValueError('white-hat proof requires exactly two independent reviews')
    archived: list[tuple[str, bytes]] = []
    for review in reviews:
        name = review.get('report')
        if not isinstance(name, str) or not REPORT_RE.fullmatch(name):
            raise ValueError(f'invalid white-hat report filename: {name}')
        report_path = context.proof_dir / name
        if not report_path.is_file():
            raise ValueError(f'missing white-hat report file: {name}')
        raw = report_path.read_bytes()
        if review.get('reportSha256') != hashlib.sha256(raw).hexdigest():
            raise ValueError(f'white-hat report hash mismatch: {name}')
        archived.append((name, raw))
    receipt = {
        **proof,
        'schema': 'honua.release-promise-receipt/v1',
        'row': 'security-whitehat',
        'status': 'PASS',
        'serverSha': context.server_sha,
        'imageDigest': context.server_digest,
        'platformLockDigest': context.lock_digest,
        'observedAt': _emitted_at(context),
        'sourceObservedAt': source_observed,
        'assertions': {'live': True},
    }
    check_doc = {**receipt, '_archivedReports': [name for name, _ in archived]}
    if not adversarial.valid(check_doc):
        raise ValueError('white-hat receipt failed adversarial verification')
    _write_bundle(out, [receipt], [])
    for name, raw in archived:
        (out / name).write_bytes(raw)
    return [receipt]


EMITTERS = {
    'launch-stack': emit_launch_stack,
    'runs-itself': emit_runs_itself,
    'whitehat': emit_whitehat,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=sorted(EMITTERS))
    parser.add_argument('--proof-dir', required=True)
    parser.add_argument('--server-sha', required=True)
    parser.add_argument('--server-digest', required=True)
    parser.add_argument('--lock-digest', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        proof_dir = _confined_dir(args.proof_dir)
        _validate_identity(args.server_sha, args.server_digest, args.lock_digest)
        now = dt.datetime.now(dt.timezone.utc)
        context = EmitContext(proof_dir, args.server_sha, args.server_digest, args.lock_digest, now)
        EMITTERS[args.mode](context, args.out)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f'Live promise receipt rejected: {error}\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
