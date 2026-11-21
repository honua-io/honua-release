#!/usr/bin/env python3
"""Dispatch the stackless promise producers for the candidate platform-manifest.yaml pins.

Producers emit `release-promise-receipts` only on a workflow_dispatch that carries
`platform_lock_digest`, and that digest must be sha256 of the qualification lock the
producer regenerates from the manifest it checks out (kept_gate_receipt.ensure_lock), not
the canonical lock-draft digest printed in docs/SDK-SERVER-COMPATIBILITY.md. This tool
computes the digest the same way and dispatches every producer that needs no installed
stack, reviewer, private tracker or spend. Every other contract workflow is listed in
EXCLUDED with the reason it is not dispatched here, so a new promise row cannot be skipped
silently. A producer with a trunk run still queued or running is skipped: its concurrency
group would cancel one of the two runs, and a cancelled overlapping run invalidates the row.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / 'certification/release-promise.v1.json'
REPO = 'honua-io/honua-release'
REF = 'trunk'

STRICT = {'enforcement': 'strict'}
# workflow -> inputs beyond platform_lock_digest; '{label}', '{server_sha}' and '{server_digest}'
# are filled from the manifest.
DISPATCHED = {
    'gate-dr.yml': {},
    'gate-contract.yml': {'platform_label': '{label}', **STRICT},
    'gate-contract-live.yml': {'platform_label': '{label}', **STRICT},
    'gate-observability.yml': {'platform_label': '{label}', **STRICT},
    'gate-security.yml': {'platform_label': '{label}', **STRICT},
    'gate-docs.yml': {'platform_label': '{label}', **STRICT},
    'gate-installed-clients.yml': {'platform_label': '{label}'},
    'gate-executable-docs.yml': {},
    'gate-source-facts.yml': {'server_sha': '{server_sha}', 'server_digest': '{server_digest}'},
}
EXCLUDED = {
    'gate-runs-itself.yml': 'fault injection on an installed Launch Stack',
    'gate-launch-stack.yml': 'timed install of the Launch Stack in the test account',
    'gate-agent-safety.yml': 'route walk against an installed candidate',
    'gate-chaos.yml': 'chaos run on an installed stack after the operates rows pass',
    'gate-whitehat.yml': 'two reviewers upload their verdicts',
    'gate-upgrade.yml': 'needs a candidate-bound Launch Stack upgrade proof',
    'gate-sbom.yml': 'needs the frozen platform-lock.json artifact from release-train',
    'gate-security-private-summary.yml': 'private tracker environment',
    'gate-ai-native.yml': 'paid model arms need the founder spending authorization',
    'gate-protocol-certification.yml': 'matrix inputs come from the nightly protocol ledger',
    'capacity-soak.yml': 'long soak on a sized stack (GA only)',
    'gate-release-outcomes.yml': 'outcome evaluation on an installed stack',
}
IN_FLIGHT = {'queued', 'in_progress', 'waiting', 'requested', 'pending'}
# Their release-train calls share the producers' concurrency groups but run under the caller.
TRAINS = ('release-train.yml', 'nightly-certification.yml', 'rc-release.yml')
TRAIN_CALLS = re.compile(r'uses:\s*\./\.github/workflows/([\w.-]+\.yml)')
SHA = re.compile(r'^[0-9a-f]{40}$')
DIGEST = re.compile(r'^sha256:[0-9a-f]{64}$')


def contract_workflows(contract: Path = CONTRACT) -> set[str]:
    document = json.loads(contract.read_text())
    rows = document['rows'] + document['outcomes']
    return {name for row in rows for name in (row['workflow'], *row.get('additionalWorkflows', []))}


def qualification_lock_digest(manifest: Path, generate=None) -> str:
    """sha256 of the exact lock bytes a producer's receipt step regenerates from `manifest`."""
    import kept_gate_receipt

    with tempfile.TemporaryDirectory() as scratch:
        lock = Path(scratch) / 'platform-lock.json'
        kept_gate_receipt.ensure_lock(lock, manifest.resolve(), generate)
        return 'sha256:' + hashlib.sha256(lock.read_bytes()).hexdigest()


def candidate(manifest: Path) -> dict:
    try:
        document = yaml.safe_load(manifest.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f'cannot read {manifest}: {error}') from error
    if not isinstance(document, dict):
        raise ValueError(f'{manifest} is not a manifest mapping')
    server = (document.get('components') or {}).get('honua-server') or {}
    sha, digest, label = server.get('sha', ''), server.get('digest', ''), document.get('platformRelease', '')
    if not SHA.match(str(sha)) or not DIGEST.match(str(digest)) or not label:
        raise ValueError(f'{manifest} must pin honua-server sha, digest and platformRelease; '
                         f'got sha={sha!r} digest={digest!r} platformRelease={label!r}')
    return {'server_sha': sha, 'server_digest': digest, 'label': label}


def resolve(manifest: Path) -> tuple[dict, str, str]:
    """Pins and digest, or empty values and the reason the producers get no receipt today."""
    try:
        pins = candidate(manifest)
        return pins, qualification_lock_digest(manifest), ''
    except ValueError as error:
        return {}, '', str(error)


def plan(pins: dict, lock_digest: str, selected=None, dr_run='') -> list[tuple[str, dict | None]]:
    """Inputs per producer; None when the producer cannot run without the candidate pins."""
    planned = []
    for workflow, extra in DISPATCHED.items():
        if selected is not None and workflow not in selected:
            continue
        if not lock_digest and extra.get('server_sha'):
            planned.append((workflow, None))
            continue
        inputs = ({'platform_lock_digest': lock_digest, **{key: value.format(**pins) for key, value in extra.items()}}
                  if lock_digest else {key: value for key, value in extra.items() if '{' not in value})
        planned.append((workflow, {**inputs, 'producer_run_id': dr_run} if dr_run and workflow == 'gate-dr.yml'
                        else inputs))
    return planned


def select(only: list[str], skip: list[str]) -> set[str]:
    unknown = sorted(set(only + skip) - DISPATCHED.keys())
    if unknown:
        raise ValueError(f'not dispatched by this tool: {unknown}')
    return (set(only) or set(DISPATCHED)) - set(skip)


def in_flight(workflow: str, run) -> str:
    done = run(['gh', 'run', 'list', '--repo', REPO, '--workflow', workflow, '--branch', REF,
                '--limit', '20', '--json', 'databaseId,status'], capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f'cannot list {workflow} runs: {done.stderr.strip()[:200]}')
    busy = [str(item['databaseId']) for item in json.loads(done.stdout) if item['status'] in IN_FLIGHT]
    return f"run {', '.join(busy)} in flight" if busy else ''


def train_called(root: Path = ROOT) -> set[str]:
    return set(TRAIN_CALLS.findall((root / '.github/workflows/release-train.yml').read_text()))


def trains_in_flight(run) -> str:
    return '; '.join(f'{train} {state}' for train in TRAINS if (state := in_flight(train, run)))


def dispatch_one(workflow: str, inputs: dict, run, train_busy='') -> tuple[str, bool]:
    if train_busy and workflow in train_called():
        return f'skipped: a release train is in flight ({train_busy})', True
    try:
        busy = in_flight(workflow, run)
    except RuntimeError as error:
        return f'FAILED: {error}', False
    if busy:
        return f'skipped: {busy}', True
    done = run(command(workflow, inputs), capture_output=True, text=True)
    if done.returncode != 0:
        return f'FAILED: {done.stderr.strip()[:200]}', False
    return 'dispatched', True


def command(workflow: str, inputs: dict) -> list[str]:
    fields = [arg for key, value in inputs.items() for arg in ('-f', f'{key}={value}')]
    return ['gh', 'workflow', 'run', workflow, '--repo', REPO, '--ref', REF, *fields]


def summary(pins: dict, lock_digest: str, results: list[tuple[str, str]]) -> str:
    lines = [f"## Promise producers for honua-server {pins.get('server_sha', 'unpinned')[:12]}", '',
             f"- server digest `{pins.get('server_digest', '')}`",
             f"- platform_lock_digest `{lock_digest or 'none: no promise receipts from these runs'}`",
             f"- label `{pins.get('label', '')}`", '', '| Workflow | Result |', '|---|---|']
    lines += [f'| `{workflow}` | {result} |' for workflow, result in results]
    lines += ['', 'Not dispatched here:', '']
    lines += [f'- `{workflow}`: {reason}' for workflow, reason in EXCLUDED.items()]
    return '\n'.join(lines) + '\n'


def main(argv=None, run=subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--manifest', type=Path, default=ROOT / 'platform-manifest.yaml')
    parser.add_argument('--dispatch', action='store_true', help='run gh workflow run (default: print)')
    parser.add_argument('--summary', type=Path, help='append a Markdown summary (GITHUB_STEP_SUMMARY)')
    parser.add_argument('--only', action='append', default=[], help='dispatch only this workflow')
    parser.add_argument('--skip', action='append', default=[], help='do not dispatch this workflow')
    parser.add_argument('--dr-producer-run', default='', help='dr-drill-local-docker run gate-dr reads')
    args = parser.parse_args(argv)

    unclassified = contract_workflows() - DISPATCHED.keys() - EXCLUDED.keys()
    if unclassified:
        parser.error(f'promise workflows neither dispatched nor excluded: {sorted(unclassified)}')
    try:
        selected = select(args.only, args.skip)
    except ValueError as error:
        parser.error(str(error))
    pins, lock_digest, problem = resolve(args.manifest)
    if problem:
        # The scans still run daily; only their promise receipts wait for a resolvable manifest.
        print(f'::error::no promise digest: {problem}')
    try:
        train_busy = trains_in_flight(run) if args.dispatch else ''
    except RuntimeError as error:
        print(f'::error::{error}')
        return 1
    results, failed = [], bool(problem)
    for workflow, inputs in plan(pins, lock_digest, selected, args.dr_producer_run):
        if inputs is None:
            result, ok = f'FAILED: needs the candidate pins ({problem})', False
        elif not args.dispatch:
            print(' '.join(command(workflow, inputs)))
            result, ok = 'planned', True
        else:
            result, ok = dispatch_one(workflow, inputs, run, train_busy)
            # A drill's gate-dr is never dispatched again later: fail so the skip can be rerun.
            ok = ok and not (args.dr_producer_run and result.startswith('skipped'))
        failed += not ok
        results.append((workflow, result))
        print(f'{workflow}: {result}')
    text = summary(pins, lock_digest, results)
    if args.summary:
        with args.summary.open('a') as handle:
            handle.write(text)
    print(f'platform_lock_digest={lock_digest}')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
