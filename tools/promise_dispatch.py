#!/usr/bin/env python3
"""Dispatch the stackless promise producers for the candidate platform-manifest.yaml pins.

Producers emit `release-promise-receipts` only on a workflow_dispatch that carries
`platform_lock_digest`, and that digest must be sha256 of the qualification lock the
producer regenerates from the manifest it checks out (kept_gate_receipt.ensure_lock), not
the canonical lock-draft digest printed in docs/SDK-SERVER-COMPATIBILITY.md. This tool
computes the digest the same way and dispatches every producer that needs no installed
stack, reviewer, private tracker or spend. Every other contract workflow is listed in
EXCLUDED with the reason it is not dispatched here, so a new promise row cannot be skipped
silently.
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
}
SHA = re.compile(r'^[0-9a-f]{40}$')
DIGEST = re.compile(r'^sha256:[0-9a-f]{64}$')


def contract_workflows(contract: Path = CONTRACT) -> set[str]:
    rows = json.loads(contract.read_text())['rows']
    return {name for row in rows for name in (row['workflow'], *row.get('additionalWorkflows', []))}


def qualification_lock_digest(manifest: Path, generate=None) -> str:
    """sha256 of the exact lock bytes a producer's receipt step regenerates from `manifest`."""
    import kept_gate_receipt

    with tempfile.TemporaryDirectory() as scratch:
        lock = Path(scratch) / 'platform-lock.json'
        kept_gate_receipt.ensure_lock(lock, manifest.resolve(), generate)
        return 'sha256:' + hashlib.sha256(lock.read_bytes()).hexdigest()


def candidate(manifest: Path) -> dict:
    document = yaml.safe_load(manifest.read_text())
    server = (document.get('components') or {}).get('honua-server') or {}
    sha, digest, label = server.get('sha', ''), server.get('digest', ''), document.get('platformRelease', '')
    if not SHA.match(str(sha)) or not DIGEST.match(str(digest)) or not label:
        raise ValueError(f'{manifest} must pin honua-server sha, digest and platformRelease; '
                         f'got sha={sha!r} digest={digest!r} platformRelease={label!r}')
    return {'server_sha': sha, 'server_digest': digest, 'label': label}


def plan(pins: dict, lock_digest: str) -> list[tuple[str, dict]]:
    return [(workflow, {'platform_lock_digest': lock_digest,
                        **{key: value.format(**pins) for key, value in extra.items()}})
            for workflow, extra in DISPATCHED.items()]


def command(workflow: str, inputs: dict) -> list[str]:
    fields = [arg for key, value in inputs.items() for arg in ('-f', f'{key}={value}')]
    return ['gh', 'workflow', 'run', workflow, '--repo', REPO, '--ref', REF, *fields]


def summary(pins: dict, lock_digest: str, results: list[tuple[str, str]]) -> str:
    lines = [f"## Promise producers for honua-server {pins['server_sha'][:12]}", '',
             f"- server digest `{pins['server_digest']}`", f'- platform_lock_digest `{lock_digest}`',
             f"- label `{pins['label']}`", '', '| Workflow | Result |', '|---|---|']
    lines += [f'| `{workflow}` | {result} |' for workflow, result in results]
    lines += ['', 'Not dispatched here:', '']
    lines += [f'- `{workflow}`: {reason}' for workflow, reason in EXCLUDED.items()]
    return '\n'.join(lines) + '\n'


def main(argv=None, run=subprocess.run) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--manifest', type=Path, default=ROOT / 'platform-manifest.yaml')
    parser.add_argument('--dispatch', action='store_true', help='run gh workflow run (default: print)')
    parser.add_argument('--summary', type=Path, help='append a Markdown summary (GITHUB_STEP_SUMMARY)')
    args = parser.parse_args(argv)

    unclassified = contract_workflows() - DISPATCHED.keys() - EXCLUDED.keys()
    if unclassified:
        parser.error(f'promise workflows neither dispatched nor excluded: {sorted(unclassified)}')
    pins = candidate(args.manifest)
    lock_digest = qualification_lock_digest(args.manifest)
    results, failed = [], 0
    for workflow, inputs in plan(pins, lock_digest):
        argv_ = command(workflow, inputs)
        if not args.dispatch:
            print(' '.join(argv_))
            results.append((workflow, 'planned'))
            continue
        done = run(argv_, capture_output=True, text=True)
        ok = done.returncode == 0
        failed += not ok
        results.append((workflow, 'dispatched' if ok else f'FAILED: {done.stderr.strip()[:200]}'))
        print(f"{workflow}: {results[-1][1]}")
    text = summary(pins, lock_digest, results)
    if args.summary:
        with args.summary.open('a') as handle:
            handle.write(text)
    print(f'platform_lock_digest={lock_digest}')
    return 1 if failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
