#!/usr/bin/env python3
"""Mint a canonical signed nightly lock only for a complete, candidate-bound green train.

Keyless blob signing is separate from publication tag signing: nightly never moves
channels or creates a GA tag. Existing tag_signing validates the release label.

Only the scheduled trunk nightly can mint, and every refusal happens before cosign runs.
Locks live at refs/tags/nightly-lock/<label>; a tag ruleset must forbid deleting or
moving them, or a deleted lock would let the next night reuse its rc number.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

import yaml

from candidate_binding import REQUIRED_RELEASE_GATES, validate_live_report, _sha256
from generate_platform_lock import generate
from tag_signing import publication_tag
from check_promotion_readiness import EVIDENCE_CLASSES, MAX_FRESHNESS, JOURNEYS, _journey

REQUIRED_NIGHTLY_GATES = REQUIRED_RELEASE_GATES | {'capacity-soak', 'one-operation-rollback', 'journey'}
LABEL = re.compile(r'(?:honua-)?2026\.1-rc\.([1-9][0-9]*)\Z')
LOCK_REFS = 'refs/tags/nightly-lock/'
TRUSTED_REPOSITORY = 'honua-io/honua-release'
TRUSTED_BRANCH = 'trunk'
TRUSTED_WORKFLOW = '.github/workflows/nightly-certification.yml'
SHA = re.compile(r'[0-9a-f]{40}\Z')
DELAYS = (0, 10, 30, 60, 120, 60)
TRANSIENT = ('could not resolve host', 'connection reset', 'connection timed out',
             'operation timed out', 'tls', 'early eof', 'unable to access', 'http 5')


# Each receipt derives its verdict from the gate that actually consumes that class.
CLASS_GATES = {
    'build-test': 'build-test', 'contract': 'contract', 'sbom': 'sbom',
    'security': 'security', 'upgrade': 'upgrade', 'capacity-soak': 'capacity-soak',
    'dr': 'dr', 'lambda-certification': 'cloud-parity',
    'protocol-ledger': 'protocol-certification',
    'deterministic-journey': 'journey', 'nightly-model-journey': 'journey',
}


def declare_evidence(report: dict, lock: Path, journeys: list[dict]) -> dict:
    """Declare the R21 tiers and retain workflow verdicts; missing journeys stay red.

    The qualification lock is generated before gates run. Minting later requires these
    receipts to match the newly generated lock bytes, so no class can cross candidates.
    """
    report = copy.deepcopy(report)
    completed = datetime.fromisoformat(report['generatedAt'].replace('Z', '+00:00'))
    if completed.tzinfo != timezone.utc:
        raise ValueError('evidence timestamp must be UTC')
    report['generatedAt'] = completed.isoformat().replace('+00:00', 'Z')
    digest = 'sha256:' + _sha256(lock)
    train = report['candidate']['train']
    gates = {row['gate']: row['status'] for row in report['gates']}
    declarations, receipts = {}, {}
    for name, kind in EVIDENCE_CLASSES.items():
        if kind == 'qualifying':
            declarations[name] = {'kind': kind, 'receipt': None, 'freshUntil': None}
            continue
        expiry = (completed + MAX_FRESHNESS[name]).isoformat().replace('+00:00', 'Z')
        receipt = {'class': name, 'kind': kind, 'runId': str(train['runId']),
                   'runAttempt': train['runAttempt'], 'completedAt': completed.isoformat().replace('+00:00', 'Z'),
                   'status': gates.get(CLASS_GATES[name], 'missing'), 'lockDigest': digest,
                   'freshUntil': expiry, 'gate': CLASS_GATES[name]}
        if name in JOURNEYS:
            mode, required = JOURNEYS[name]
            cells = []
            for journey in journeys:
                if (journey.get('status') != 'pass' or str(journey.get('runId')) != str(train['runId'])
                        or str(journey.get('runAttempt')) != str(train['runAttempt'])
                        or journey.get('candidateDigest') != report['candidate']['artifacts']['platform-manifest.yaml']['sha256']):
                    continue
                for row in journey.get('cells', []):
                    attempts = row.get('attempts', [])
                    if (row.get('cell') not in required or row.get('status') != 'pass' or not attempts
                            or any(attempt.get('driver') != mode for attempt in attempts)):
                        continue
                    cells.append({'cell': row['cell'], 'mode': mode, 'attemptCount': len(attempts),
                                  'attempts': [{'attempt': a['number'], 'status': a['status'],
                                                'failureAttribution': a.get('failureAttribution'),
                                                'completedAt': a.get('completedAt'),
                                                'lockDigest': digest} for a in attempts]})
            receipt['cells'] = cells
            if not _journey(receipt, required, mode, digest, completed - timedelta(hours=24), completed):
                receipt['status'] = 'fail'
        declarations[name] = {'kind': kind, 'receipt': f'promotion-receipts/{name}/receipt.json',
                              'freshUntil': expiry}
        receipts[name] = receipt
    report['evidenceClasses'] = list(receipts)
    report['evidenceDeclarations'] = declarations
    report['evidenceReceipts'] = receipts
    return report


def evidence_failures(report: dict, digest: str | None = None) -> list[str]:
    errors = []
    receipts = report.get('evidenceReceipts') or {}
    declarations = report.get('evidenceDeclarations') or {}
    nightly = {name for name, kind in EVIDENCE_CLASSES.items() if kind == 'nightly'}
    if (set(receipts) != nightly or set(report.get('evidenceClasses') or []) != nightly
            or len(report.get('evidenceClasses') or []) != len(nightly)
            or set(declarations) != set(EVIDENCE_CLASSES)):
        return ['no lock minted: missing R21 evidence declarations or receipts']
    train = report.get('candidate', {}).get('train', {})
    for name, kind in EVIDENCE_CLASSES.items():
        declaration = declarations.get(name) or {}
        if kind == 'qualifying':
            if declaration != {'kind': kind, 'receipt': None, 'freshUntil': None}:
                errors.append(f'{name}: qualifying evidence must be produced during burn')
            continue
        receipt = receipts[name]
        try:
            completed = datetime.fromisoformat(receipt['completedAt'].replace('Z', '+00:00'))
            expiry = datetime.fromisoformat(receipt['freshUntil'].replace('Z', '+00:00'))
            valid = (receipt['class'] == name and receipt['kind'] == kind and receipt['status'] == 'pass'
                     and receipt['gate'] == CLASS_GATES[name]
                     and str(receipt['runId']) == str(train['runId'])
                     and receipt['runAttempt'] == train['runAttempt']
                     and receipt['completedAt'] == report['generatedAt']
                     and completed.tzinfo == expiry.tzinfo == timezone.utc
                     and expiry - completed == MAX_FRESHNESS[name]
                     and declaration == {'kind': kind, 'receipt': f'promotion-receipts/{name}/receipt.json',
                                         'freshUntil': receipt['freshUntil']}
                     and (digest is None or receipt['lockDigest'] == digest))
            if name in JOURNEYS:
                mode, required = JOURNEYS[name]
                valid &= _journey(receipt, required, mode, receipt['lockDigest'],
                                  completed - timedelta(hours=24), completed)
            if not valid:
                errors.append(f'{name}: invalid or missing nightly receipt')
        except (ValueError, KeyError, TypeError):
            errors.append(f'{name}: invalid nightly receipt')
    return errors



def bind(*args, **kwargs):
    # Declaration assembly needs only retained workflow data; schema validation belongs
    # to the real lock-binding/signing path, which has the full minting dependencies.
    from platform_lock_bundle import bind as bind_bundle
    return bind_bundle(*args, **kwargs)


def bundle_files(*args, **kwargs):
    from platform_lock_bundle import bundle_files as files
    return files(*args, **kwargs)


def next_label(history: Path) -> str:
    numbers = [2]  # #383: rc.2 was published; the first real nightly cut is rc.3.
    if history.exists():
        for path in history.rglob('platform-lock.json'):
            lock = json.loads(path.read_text())
            match = LABEL.fullmatch(str(lock.get('platform', {}).get('id', '')))
            if match:
                numbers.append(int(match.group(1)))
    return f'2026.1-rc.{max(numbers) + 1}'


def _git(args, cwd, run=subprocess.run, sleep=time.sleep):
    """Run git, retrying only transient network failures, for up to about five minutes."""
    for index, delay in enumerate(DELAYS):
        if delay:
            sleep(delay)
        result = run(['git', *args], cwd=cwd, capture_output=True)
        if result.returncode == 0:
            return result.stdout
        detail = result.stderr.decode(errors='replace') if isinstance(result.stderr, bytes) else str(result.stderr)
        if index == len(DELAYS) - 1 or not any(term in detail.lower() for term in TRANSIENT):
            raise ValueError(f"git {' '.join(args)} failed: {detail.strip()}")
    raise AssertionError('unreachable')


def sync_history(destination: Path, repository: Path, remote='origin', *, run=subprocess.run,
                 sleep=time.sleep) -> dict[str, str]:
    """Extract every published lock. A failed or partial fetch refuses; it never reads as no history."""
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f'lock history directory is not empty: {destination}')
    destination.mkdir(parents=True, exist_ok=True)
    listing = _git(['ls-remote', '--refs', remote, LOCK_REFS + '*'], repository, run, sleep).decode()
    published = {}
    for line in listing.splitlines():
        sha, _, ref = line.partition('\t')
        label = ref.removeprefix(LOCK_REFS)
        if not SHA.fullmatch(sha) or not ref.startswith(LOCK_REFS) or not LABEL.fullmatch(label):
            raise ValueError(f'unexpected lock ref listing: {line!r}')
        published[label] = sha
    if published:
        _git(['fetch', '--no-tags', remote, *(f'+{LOCK_REFS}{label}:{LOCK_REFS}{label}' for label in published)],
             repository, run, sleep)
    for label, sha in sorted(published.items()):
        local = _git(['rev-parse', '--verify', f'{LOCK_REFS}{label}^{{commit}}'], repository, run, sleep)
        if local.decode().strip() != sha:
            raise ValueError(f'{LOCK_REFS}{label}: fetched {local.decode().strip()} but origin lists {sha}')
        data = _git(['show', f'{sha}:platform-lock.json'], repository, run, sleep)
        lock = json.loads(data)
        if lock.get('platform', {}).get('id') != f'honua-{label}':
            raise ValueError(f'{LOCK_REFS}{label}: lock records {lock.get("platform", {}).get("id")!r}')
        (destination / label).mkdir()
        (destination / label / 'platform-lock.json').write_bytes(data)
    return published


def _ref_pattern(pattern: str) -> re.Pattern:
    if pattern == '~ALL':
        return re.compile(r'refs/tags/.*\Z')
    parts = re.split(r'(\*\*|\*|\?)', pattern)
    body = ''.join({'**': '.*', '*': '[^/]*', '?': '[^/]'}.get(part, re.escape(part)) for part in parts)
    return re.compile(body + r'\Z')


def lock_ref_protected(rulesets: list, ref: str) -> bool:
    """True when an active tag ruleset covering `ref` forbids deleting and moving it."""
    for ruleset in rulesets if isinstance(rulesets, list) else []:
        if not isinstance(ruleset, dict) or ruleset.get('target') != 'tag' or ruleset.get('enforcement') != 'active':
            continue
        names = (ruleset.get('conditions') or {}).get('ref_name') or {}
        if not any(_ref_pattern(str(p)).match(ref) for p in names.get('include') or []):
            continue
        if any(_ref_pattern(str(p)).match(ref) for p in names.get('exclude') or []):
            continue
        rules = {rule.get('type') for rule in ruleset.get('rules') or [] if isinstance(rule, dict)}
        if {'deletion', 'update'} <= rules:
            return True
    return False


CHANNEL_TAG = re.compile(r':(?:latest|stable|nightly|2026\.1)(?:["\s,]|$)')


def stamp_release_label(manifest_path: Path, label: str) -> None:
    """Record the next candidate label on the manifest. This creates no publication tag."""
    publication_tag(label)
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest['platformRelease'] = label
    manifest['status'] = 'rc'
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))


def failures(report: dict, *, repository=TRUSTED_REPOSITORY, source_sha=None, run_id=None) -> list[str]:
    errors = []
    ok, why = validate_live_report(report)
    if not ok:
        errors.append(why)
    candidate = report.get('candidate') if isinstance(report.get('candidate'), dict) else {}
    source = candidate.get('source') if isinstance(candidate.get('source'), dict) else {}
    train = candidate.get('train') if isinstance(candidate.get('train'), dict) else {}
    # A branch can stub its own gates green, so only the reviewed trunk nightly may mint.
    if source.get('repository') != repository:
        errors.append(f"source repository {source.get('repository')!r} is not {repository}")
    if source.get('branch') != TRUSTED_BRANCH:
        errors.append(f"source branch {source.get('branch')!r} is not {TRUSTED_BRANCH}")
    if train.get('workflowPath') != TRUSTED_WORKFLOW:
        errors.append(f"train workflow {train.get('workflowPath')!r} is not {TRUSTED_WORKFLOW}")
    if train.get('certificationMode') != 'live':
        errors.append(f"train certificationMode {train.get('certificationMode')!r} is not live")
    if source_sha is not None and source.get('sha') != source_sha:
        errors.append(f"source sha {source.get('sha')!r} is not this run's {source_sha}")
    if run_id is not None and str(train.get('runId')) != str(run_id):
        errors.append(f"train run {train.get('runId')!r} is not this run {run_id}")
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
         identity: str, issuer='https://token.actions.githubusercontent.com', *, signer=sign_blob,
         rulesets=None, published=None, repository=TRUSTED_REPOSITORY, source_sha=None, run_id=None) -> str:
    from platform_lock_bundle import canonical_bytes

    errors = failures(report, repository=repository, source_sha=source_sha, run_id=run_id)
    errors.extend(evidence_failures(report))
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
    if published is None or label in published:
        raise ValueError(f'no lock minted: {LOCK_REFS}{label} is not known to be unpublished')
    if not lock_ref_protected(rulesets, LOCK_REFS + label):
        raise ValueError(f'no lock minted: no active tag ruleset forbids deleting or moving {LOCK_REFS}{label}')
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
        if CHANNEL_TAG.search((staging / 'platform-lock.json').read_text()):
            raise ValueError('no lock minted: lock contains a channel tag')
        digest = 'sha256:' + hashlib.sha256((staging / 'platform-lock.json').read_bytes()).hexdigest()
        errors = evidence_failures(report, digest)
        if errors:
            raise ValueError('no lock minted:\n' + '\n'.join(errors))
        (staging / 'gate-report.json').write_bytes(canonical_bytes(report))
        for name, receipt in report['evidenceReceipts'].items():
            path = staging / 'promotion-receipts' / name / 'receipt.json'
            path.parent.mkdir(parents=True)
            path.write_bytes(canonical_bytes(receipt))
        signer(staging / 'platform-lock.json', staging / 'platform-lock.sigstore.json', identity, issuer)
        if not (staging / 'platform-lock.sigstore.json').is_file():
            raise ValueError('no lock minted: signer returned no signature bundle')
        staging.rename(output)
    return label


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--declare-evidence', action='store_true')
    parser.add_argument('--lock', type=Path)
    parser.add_argument('--journey-reports', type=Path)
    parser.add_argument('--history', type=Path, default=Path('nightly-history'))
    parser.add_argument('--next-label', action='store_true')
    parser.add_argument('--stamp', type=Path, help='write the next label onto this candidate manifest')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--matrix', type=Path)
    parser.add_argument('--out-dir', type=Path, default=Path('nightly-lock'))
    parser.add_argument('--certificate-identity')
    parser.add_argument('--sync-from', metavar='REMOTE',
                        help='first extract every published lock from this git remote into --history')
    parser.add_argument('--rulesets', type=Path, help='JSON list of the repository rulesets')
    parser.add_argument('--expected-repository', default=TRUSTED_REPOSITORY)
    parser.add_argument('--expected-source-sha')
    parser.add_argument('--expected-run-id')
    args = parser.parse_args(argv)
    try:
        if args.declare_evidence:
            if not args.report or not args.lock or not args.journey_reports:
                raise ValueError('declaration requires report, lock and journey reports')
            journeys = [json.loads(path.read_text()) for path in args.journey_reports.rglob('gate-report-journey.json')]
            report = declare_evidence(json.loads(args.report.read_text()), args.lock, journeys)
            args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            return 0
        published = None
        if args.sync_from:
            published = sync_history(args.history, Path.cwd(), args.sync_from)
        if args.stamp:
            if published is None:
                raise ValueError('stamping requires --sync-from so the published lock history is complete')
            label = next_label(args.history)
            stamp_release_label(args.stamp, label)
            print(label)
        elif args.next_label:
            print(next_label(args.history))
        else:
            if not all((args.report, args.manifest, args.matrix, args.certificate_identity, args.rulesets,
                        args.expected_source_sha, args.expected_run_id)):
                raise ValueError('report, candidate inputs, trusted signing identity, rulesets and '
                                 'the expected source sha and run id are required')
            if published is None:
                raise ValueError('minting requires --sync-from so the published lock history is complete')
            label = mint(json.loads(args.report.read_text()), args.manifest, args.matrix,
                         args.history, args.out_dir, args.certificate_identity,
                         rulesets=json.loads(args.rulesets.read_text()), published=published,
                         repository=args.expected_repository, source_sha=args.expected_source_sha,
                         run_id=args.expected_run_id)
            print(f'MINTED: {label} -> {args.out_dir}')
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
