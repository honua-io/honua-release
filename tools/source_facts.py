#!/usr/bin/env python3
"""Count retired-cache package declarations and source references in the candidate server.

The predicate and exemptions come from the candidate's own retired-cache check, so the
receipt applies the policy that the server repository enforces on every pull request.
Every tracked regular file is a source except documentation, tests and migrations whose
content matches the hash the candidate pins: applied migrations are history that cannot
change. Manifest lines are references too, so a declaration form the package readers do
not parse still counts.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tomllib

import rc_gate_window as gate

RECEIPT = Path('cloud-no-redis-source.json')
CHECKER = 'scripts/ci/check-retired-cache.py'
NOT_SHIPPED_ROOTS = ('docs/', 'tests/')
PINNED_HISTORY = 'eng/schema/migration-hashes/'
REGULAR_FILE_MODES = ('100644', '100755')
SHOWN_FINDINGS = 25
MSBUILD_SUFFIXES = ('.csproj', '.fsproj', '.vbproj', '.props', '.targets')
MSBUILD_PACKAGE = re.compile(
    r'<(?:Global)?Package(?:Reference|Version)\b[^>]*?\b(?:Include|Update)\s*=\s*(["\'])(.+?)\1')
REQUIREMENT_NAME = re.compile(r'\s*([A-Za-z0-9][A-Za-z0-9._-]*)')
NODE_DEPENDENCY_KEYS = ('dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies')


class FactsUnavailable(Exception):
    pass


def candidate_from_environment():
    candidate = gate.Candidate(
        gate.normalize_sha(os.environ.get('SERVER_SHA')),
        gate.normalize_digest(os.environ.get('SERVER_DIGEST')),
        gate.normalize_digest(os.environ.get('LOCK_DIGEST')),
    )
    if not all((candidate.server_sha, candidate.server_digest, candidate.platform_lock)):
        raise FactsUnavailable('candidate identity is incomplete')
    return candidate


def git(checkout: Path, *arguments: str) -> bytes:
    return subprocess.check_output(['git', '-C', str(checkout), *arguments])


def require_candidate_checkout(checkout: Path, candidate) -> None:
    if git(checkout, 'rev-parse', 'HEAD').decode().strip() != candidate.server_sha:
        raise FactsUnavailable('server checkout is not the candidate SHA')
    if git(checkout, 'status', '--porcelain', '--untracked-files=no'):
        raise FactsUnavailable('server checkout differs from the candidate commit')


def load_policy(checkout: Path):
    path = checkout / CHECKER
    if not path.is_file():
        raise FactsUnavailable(f'{CHECKER} is missing at the candidate')
    spec = importlib.util.spec_from_file_location('candidate_retired_cache', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not all(callable(getattr(module, name, None)) for name in ('mentions_retired_cache', 'exempt')):
        raise FactsUnavailable(f'{CHECKER} no longer exposes mentions_retired_cache and exempt')
    return module


def regular_files(checkout: Path) -> list[str]:
    entries = git(checkout, 'ls-files', '--stage', '-z').decode('utf-8').split('\0')
    return [entry.split('\t', 1)[1] for entry in entries
            if entry and entry.split(' ', 1)[0] in REGULAR_FILE_MODES]


def read_text(checkout: Path, path: str) -> str:
    return (checkout / path).read_bytes().decode('utf-8', errors='replace')


def node_packages(text: str) -> list[str]:
    document = json.loads(text)
    return [name for key in NODE_DEPENDENCY_KEYS for name in (document.get(key) or {})]


def requirement_names(lines) -> list[str]:
    names = (REQUIREMENT_NAME.match(line) for line in lines if not line.lstrip().startswith(('#', '-')))
    return [match.group(1) for match in names if match]


def pyproject_packages(text: str) -> list[str]:
    project = tomllib.loads(text).get('project', {})
    optional = project.get('optional-dependencies', {})
    declared = [*project.get('dependencies', []), *(item for group in optional.values() for item in group)]
    return requirement_names(declared)


def msbuild_packages(text: str) -> list[str]:
    return [match.group(2) for match in MSBUILD_PACKAGE.finditer(text)]


def package_reader(path: str):
    name = PurePosixPath(path).name
    if name.endswith(MSBUILD_SUFFIXES):
        return msbuild_packages
    if name == 'package.json':
        return node_packages
    if name == 'pyproject.toml':
        return pyproject_packages
    if name.startswith('requirements') and name.endswith('.txt'):
        return lambda text: requirement_names(text.splitlines())
    return None


def package_findings(checkout: Path, policy, paths: list[str]) -> list[str]:
    readers = {path: package_reader(path) for path in paths if not policy.exempt(path)}
    return [f'{path}: package {package}'
            for path, reader in readers.items() if reader
            for package in reader(read_text(checkout, path))
            if policy.mentions_retired_cache(package)]


def pinned_history(checkout: Path, path: str, tracked: set[str]) -> bool:
    pin = f'{PINNED_HISTORY}{path}.sha256'
    if pin not in tracked:
        return False
    expected = read_text(checkout, pin).split()
    actual = hashlib.sha256((checkout / path).read_bytes()).hexdigest()
    return bool(expected) and expected[0].lower() == actual


def source_paths(checkout: Path, policy, paths: list[str]) -> list[str]:
    tracked = set(paths)
    return [path for path in paths
            if not path.startswith(NOT_SHIPPED_ROOTS) and not policy.exempt(path)
            and not pinned_history(checkout, path, tracked)]


def source_findings(checkout: Path, policy, paths: list[str]) -> list[str]:
    return [f'{path}:{number}'
            for path in source_paths(checkout, policy, paths)
            for number, line in enumerate(read_text(checkout, path).splitlines(), start=1)
            if policy.mentions_retired_cache(line)]


def receipt(candidate, packages: list[str], sources: list[str]) -> dict:
    passed = not packages and not sources
    return {
        'schema': 'honua.release-promise-receipt/v1',
        'row': 'cloud-no-redis-source',
        'status': 'PASS' if passed else 'FAIL',
        'serverSha': candidate.server_sha,
        'imageDigest': candidate.server_digest,
        'platformLockDigest': candidate.platform_lock,
        'observedAt': dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z'),
        'assertions': {'redisPackages': len(packages), 'redisSourceReferences': len(sources)},
        'findings': (packages + sources)[:SHOWN_FINDINGS],
    }


def collect(checkout: Path, candidate) -> dict:
    require_candidate_checkout(checkout, candidate)
    policy = load_policy(checkout)
    paths = regular_files(checkout)
    packages = package_findings(checkout, policy, paths)
    sources = source_findings(checkout, policy, paths)
    for finding in packages + sources:
        print(finding)
    return receipt(candidate, packages, sources)


def main() -> int:
    RECEIPT.unlink(missing_ok=True)
    try:
        candidate = candidate_from_environment()
        document = collect(Path(os.environ.get('SERVER_CHECKOUT', 'server')), candidate)
    except (FactsUnavailable, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f'Source facts unavailable: {error}', file=sys.stderr)
        return 1
    RECEIPT.write_text(json.dumps(document, indent=2) + '\n')
    counts = document['assertions']
    print(f"redisPackages={counts['redisPackages']} redisSourceReferences={counts['redisSourceReferences']}")
    return 0 if document['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
