#!/usr/bin/env python3
"""Authenticate an exact candidate freeze before campaign execution.

A completed train may have later red gates: the freeze's signed subjects and
exact run/attempt authenticate its inputs without requiring the agent gate to
have already passed. Dry-run locks never qualify. Nightly locks stay unsigned;
their two signed source inputs must regenerate the exact qualification lock.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

from agent_e2e_pack import PackError, digest_bytes, load_json

REPOSITORY = 'honua-io/honua-release'
SIGNER = '.github/workflows/release-train.yml'
PRODUCERS = {SIGNER, '.github/workflows/nightly-certification.yml'}


def require(value, reason):
    if not value:
        raise PackError(reason)


def invoke(argv):
    return json.loads(subprocess.check_output(argv, text=True))


def git_source(sha, path):
    return subprocess.check_output(['git', 'show', sha+':'+path])


def authenticate_subject(path, source_sha, run_id, run_attempt, *, call=invoke):
    identity = f'https://github.com/{REPOSITORY}/{SIGNER}@refs/heads/trunk'
    rows = call(['gh', 'attestation', 'verify', str(path), '--repo', REPOSITORY,
        '--cert-identity', identity, '--source-digest', source_sha, '--source-ref', 'refs/heads/trunk',
        '--cert-oidc-issuer', 'https://token.actions.githubusercontent.com', '--deny-self-hosted-runners',
        '--predicate-type', 'https://slsa.dev/provenance/v1', '--format', 'json'])
    require(isinstance(rows, list) and rows, 'candidate lacks cryptographically verified provenance')
    expected_run = f'https://github.com/{REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}'
    expected_digest = digest_bytes(Path(path).read_bytes())[7:]
    for row in rows:
        verified = row['verificationResult']
        statement, certificate = verified['statement'], verified['signature']['certificate']
        if ([subject['digest'].get('sha256') for subject in statement['subject']] == [expected_digest]
                and certificate.get('runInvocationURI') == expected_run
                and certificate.get('sourceRepositoryDigest') == source_sha
                and certificate.get('sourceRepositoryURI') == 'https://github.com/'+REPOSITORY
                and certificate.get('runnerEnvironment') == 'github-hosted'
                and certificate.get('buildSignerURI') == identity):
            return
    raise PackError('candidate provenance differs from exact trusted freeze run/attempt/source')


def bind(lock, manifest, matrix, *, unsigned):
    from platform_lock_bundle import bind, bind_qualification, canonical_bytes
    label = lock['platform']['id'].removeprefix('honua-')
    (bind_qualification if unsigned else bind)(lock, manifest, matrix, label)
    return canonical_bytes(lock)


def authenticate(root, run_id, run_attempt, *, call=invoke, source=git_source, binder=bind):
    root = Path(root)
    require(type(run_id) is int and run_id > 0 and type(run_attempt) is int and run_attempt > 0,
            'candidate requires exact positive run and attempt')
    metadata = call(['gh', 'api', f'repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}'])
    repository = call(['gh', 'api', 'repos/'+REPOSITORY])
    branch = call(['gh', 'api', 'repos/'+REPOSITORY+'/branches/trunk'])
    require(repository.get('full_name') == REPOSITORY and repository.get('default_branch') == 'trunk' and
            branch.get('name') == 'trunk' and branch.get('protected') is True,
            'candidate producer must belong to the protected default trunk branch')
    require(metadata.get('id') == run_id and metadata.get('run_attempt') == run_attempt and
            metadata.get('head_branch') == 'trunk' and metadata.get('path') in PRODUCERS and
            metadata.get('event') in ('schedule', 'workflow_dispatch') and metadata.get('status') == 'completed' and
            metadata.get('repository', {}).get('full_name') == REPOSITORY and
            metadata.get('head_repository', {}).get('full_name') == REPOSITORY,
            'candidate is not exact completed trusted trunk train/nightly attempt')
    sha = metadata.get('head_sha', '')
    require(isinstance(sha, str) and re.fullmatch('[0-9a-f]{40}', sha), 'candidate source requires exact commit')
    manifest, matrix, lock_path = root/'platform-manifest.yaml', root/'compatibility-matrix.yaml', root/'frozen-lock/platform-lock.json'
    artifacts = {}
    for path in (manifest, matrix):
        require(path.is_file() and not path.is_symlink() and path.read_bytes() == source(sha, path.name),
                'candidate source artifact differs from immutable producer commit: '+path.name)
        artifacts[path.name] = digest_bytes(path.read_bytes())
    require(lock_path.is_file() and not lock_path.is_symlink(), 'missing exact frozen candidate lock')
    lock = load_json(lock_path)
    require(lock.get('dryRun') is not True and lock.get('platform', {}).get('status') == 'rc' and
            isinstance(lock['platform'].get('id'), str) and lock['platform']['id'].startswith('honua-'),
            'dry-run or unbound candidate lock cannot authorize a live campaign')
    for name, path in [('platformManifest', manifest), ('compatibilityMatrix', matrix)]:
        require(lock.get('sourceInputs', {}).get(name) == {'path': path.name, 'sha256': artifacts[path.name]},
                'candidate lock differs from authenticated source input: '+name)
    signed = (root/'frozen-lock/platform-lock.sigstore.json').is_file()
    subjects = [lock_path] if signed else [manifest, matrix]
    for path in subjects:
        authenticate_subject(path, sha, run_id, run_attempt, call=call)
    require(lock_path.read_bytes() == binder(lock, manifest, matrix, unsigned=not signed),
            'candidate lock bytes differ from canonical binding to authenticated inputs')
    return {'schemaVersion': 1, 'producer': {'repository': REPOSITORY, 'workflow': metadata['path'],
            'sourceSha': sha, 'runId': run_id, 'runAttempt': run_attempt},
            'qualificationLockDigest': digest_bytes(lock_path.read_bytes()), 'artifacts': artifacts,
            'authentication': 'signed-lock' if signed else 'signed-inputs-and-exact-qualification-regeneration'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-root', type=Path, required=True)
    parser.add_argument('--run-id', type=int, required=True)
    parser.add_argument('--run-attempt', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = authenticate(args.candidate_root, args.run_id, args.run_attempt)
        args.output.write_text(json.dumps(result, sort_keys=True)+'\n', encoding='utf-8')
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'blocked', 'reason': str(exc) if isinstance(exc, PackError) else type(exc).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
