#!/usr/bin/env python3
"""Fixture repository revisions: what each gate checked out, and the lock declaration built from them.

A gate that checks out a fixture repository runs `emit` after its checkouts. The record names the
revision git reports for each checkout directory, not the ref the workflow asked for, so a failed
or substituted checkout is recorded as a missing revision instead of a plausible sha.

The nightly mint reads every record from its own run with `declare`, which refuses when a fixture
gate emitted nothing, when any revision is missing, or when two records disagree about the same
repository. The result is the lock's `platformLockEvidence.fixtures` declaration.

Standard library only: gate jobs that install no Python packages run `emit`.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

SCHEMA = 'honua.fixture-revisions.v1'
RECORD = 'fixture-revisions.json'
SHA = re.compile(r'[0-9a-f]{40}\Z')
REPOSITORY = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z')

# Every gate job of the nightly train that checks out a fixture repository. A job missing here
# would let its fixture revision escape the lock; a record from a job not listed here is refused.
FIXTURE_GATES = {
    'certification': ('conformance-mcp', 'conformance-esri-geoservices'),
    'e2e-local-docker': ('seam', 'slice1'),
    'gate-dr': ('contract', 'receipt'),
    'terminal-journey-contract': ('terminal-contract',),
}


def _git(directory: Path, *args: str) -> str:
    result = subprocess.run(['git', '-C', str(directory), *args], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ''


def observe(repository: str, directory: Path, path: str | None = None) -> dict:
    """The revision checked out at `directory`, or an empty revision when it is not that repository."""
    if not REPOSITORY.fullmatch(repository):
        raise ValueError(f'repository must be owner/name: {repository!r}')
    fixture = {'repository': f'https://github.com/{repository}', 'revision': ''}
    if path:
        fixture['path'] = path
    toplevel = _git(directory, 'rev-parse', '--show-toplevel')
    origin = _git(directory, 'remote', 'get-url', 'origin').removesuffix('.git')
    # A directory inside some other checkout (or one whose origin names another repository)
    # would otherwise report that checkout's HEAD.
    if (toplevel and Path(toplevel).resolve() == directory.resolve()
            and origin.lower().endswith('github.com/' + repository.lower())):
        revision = _git(directory, 'rev-parse', '--verify', 'HEAD^{commit}')
        fixture['revision'] = revision if SHA.fullmatch(revision) else ''
    return fixture


def emit(gate: str, job: str, run_id: str, run_attempt: str, checkouts: list[str]) -> dict:
    """checkouts: `owner/name=directory` or `owner/name=directory=path-inside-the-repository`."""
    fixtures = []
    for spec in checkouts:
        repository, separator, rest = spec.partition('=')
        directory, _, path = rest.partition('=')
        if not separator or not directory:
            raise ValueError(f'checkout must be owner/name=directory[=path]: {spec!r}')
        fixtures.append(observe(repository, Path(directory), path or None))
    return {'schema': SCHEMA, 'gate': gate, 'job': job, 'runId': str(run_id),
            'runAttempt': str(run_attempt), 'fixtures': fixtures}


def load(directory: Path) -> list[dict]:
    return [json.loads(path.read_text(encoding='utf-8')) for path in sorted(directory.rglob(RECORD))]


def declare(records: list, *, run_id=None, gates=FIXTURE_GATES) -> list[dict]:
    """Build the lock's fixture declaration, or refuse with every reason at once."""
    errors, seen, observed = [], set(), {}
    expected = {(gate, job) for gate, jobs in gates.items() for job in jobs}
    for record in records:
        if not isinstance(record, dict) or record.get('schema') != SCHEMA:
            errors.append(f'unrecognised fixture revision record: {record!r:.120}')
            continue
        source = (record.get('gate'), record.get('job'))
        name = f'{source[0]}/{source[1]}'
        if source not in expected:
            errors.append(f'{name}: not a fixture gate of the nightly train')
            continue
        if source in seen:
            errors.append(f'{name}: emitted more than one fixture revision record')
            continue
        seen.add(source)
        if run_id is not None and str(record.get('runId')) != str(run_id):
            errors.append(f"{name}: record is from run {record.get('runId')!r}, not this run {run_id}")
            continue
        fixtures = record.get('fixtures')
        if not isinstance(fixtures, list) or not fixtures:
            errors.append(f'{name}: no fixture revisions emitted')
            continue
        for fixture in fixtures:
            fixture = fixture if isinstance(fixture, dict) else {}
            repository, revision = str(fixture.get('repository', '')), str(fixture.get('revision', ''))
            if not repository.startswith('https://github.com/') or not SHA.fullmatch(revision):
                errors.append(f'{name}: fixture revision of {repository or "<unnamed>"} is missing')
                continue
            observed.setdefault(repository, []).append((name, revision, fixture.get('path')))
    errors.extend(f'{gate}/{job}: fixture revisions not emitted' for gate, job in sorted(expected - seen))
    declaration = []
    for repository, uses in sorted(observed.items()):
        revisions = {revision for _, revision, _ in uses}
        if len(revisions) > 1:
            errors.append(f'{repository}: gates disagree about the fixture revision: '
                          + ', '.join(f'{name}@{revision}' for name, revision, _ in sorted(uses)))
            continue
        fixture = {'repository': repository, 'revision': revisions.pop()}
        paths = {path for _, _, path in uses}
        # One narrowed path is declared as such; a repository read whole anywhere is declared whole.
        if len(paths) == 1 and None not in paths:
            fixture['path'] = paths.pop()
        declaration.append(fixture)
    if errors:
        raise ValueError('fixture revisions refused:\n' + '\n'.join(errors))
    return declaration


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    record = commands.add_parser('emit', help='record the revisions this gate job checked out')
    record.add_argument('--gate', required=True, choices=sorted(FIXTURE_GATES))
    record.add_argument('--job', required=True)
    record.add_argument('--run-id', required=True)
    record.add_argument('--run-attempt', required=True)
    record.add_argument('--out', type=Path, required=True)
    record.add_argument('checkouts', nargs='+', metavar='owner/name=directory[=path]')
    combine = commands.add_parser('declare', help='print the lock fixture declaration for one run')
    combine.add_argument('records', type=Path)
    combine.add_argument('--run-id')
    args = parser.parse_args(argv)
    try:
        if args.command == 'emit':
            if args.job not in FIXTURE_GATES[args.gate]:
                raise ValueError(f'{args.gate} has no fixture job {args.job!r}')
            result = emit(args.gate, args.job, args.run_id, args.run_attempt, args.checkouts)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
            for fixture in result['fixtures']:
                print(f"{args.gate}/{args.job}: {fixture['repository']}@{fixture['revision'] or '<missing>'}")
        else:
            print(json.dumps(declare(load(args.records), run_id=args.run_id), indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
