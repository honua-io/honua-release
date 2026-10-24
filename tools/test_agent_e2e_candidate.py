"""Cryptographic verifier boundary replay; fixtures never authenticate a real run."""
import copy
import json

import pytest

import agent_e2e_candidate as candidate
from agent_e2e_pack import PackError, digest_bytes


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


@pytest.fixture
def inputs(tmp_path):
    source = {'platform-manifest.yaml': b'platformRelease: 2026.1-rc.1\n', 'compatibility-matrix.yaml': b'contracts: {}\n'}
    for name, raw in source.items():
        (tmp_path/name).write_bytes(raw)
    lock = {'platform': {'status': 'rc', 'id': 'honua-2026.1-rc.1'},
        'sourceInputs': {key: {'path': name, 'sha256': digest_bytes(source[name])} for key, name in
            [('platformManifest', 'platform-manifest.yaml'), ('compatibilityMatrix', 'compatibility-matrix.yaml')]}}
    (tmp_path/'frozen-lock').mkdir()
    (tmp_path/'frozen-lock/platform-lock.json').write_bytes(canonical(lock))
    metadata = {'id': 42, 'run_attempt': 2, 'head_branch': 'trunk', 'head_sha': 'a'*40,
        'path': '.github/workflows/nightly-certification.yml', 'event': 'schedule', 'status': 'completed',
        'conclusion': 'failure', 'repository': {'full_name': candidate.REPOSITORY},
        'head_repository': {'full_name': candidate.REPOSITORY}}
    return tmp_path, source, lock, metadata


def verifier(metadata, *, certificate_change=None, branch_protected=True):
    calls = []
    def call(argv):
        calls.append(argv)
        if argv[1] == 'api':
            if '/actions/runs/' in argv[-1]: return metadata
            if '/branches/' in argv[-1]: return {'name': 'trunk', 'protected': branch_protected}
            return {'full_name': candidate.REPOSITORY, 'default_branch': 'trunk'}
        assert argv[1:3] == ['attestation', 'verify']
        identity = f'https://github.com/{candidate.REPOSITORY}/{candidate.SIGNER}@refs/heads/trunk'
        cert = {'runInvocationURI': f'https://github.com/{candidate.REPOSITORY}/actions/runs/42/attempts/2',
                'sourceRepositoryDigest': 'a'*40, 'sourceRepositoryURI': 'https://github.com/'+candidate.REPOSITORY,
                'runnerEnvironment': 'github-hosted', 'buildSignerURI': identity}
        if certificate_change: certificate_change(cert)
        from pathlib import Path
        return [{'verificationResult': {'statement': {'subject': [{'digest': {'sha256': digest_bytes(Path(argv[3]).read_bytes())[7:]}}]},
                'signature': {'certificate': cert}}}]
    return call, calls


@pytest.mark.parametrize('signed', [False, True])
def test_exact_freeze_is_accepted_even_when_later_campaign_gate_is_red(inputs, signed):
    root, source, lock, metadata = inputs
    if signed:
        (root/'frozen-lock/platform-lock.sigstore.json').write_text('{}')
    call, calls = verifier(metadata)
    bound = []
    def binder(value, manifest, matrix, *, unsigned):
        bound.append(unsigned)
        assert manifest.read_bytes() == source[manifest.name]
        assert matrix.read_bytes() == source[matrix.name]
        return canonical(value)
    result = candidate.authenticate(root, 42, 2, call=call, source=lambda sha, name: source[name], binder=binder)
    assert result['producer']['runAttempt'] == 2
    assert bound == [not signed]
    subjects = [argv[3] for argv in calls if argv[1:3] == ['attestation', 'verify']]
    assert len(subjects) == (1 if signed else 2)
    assert all('--deny-self-hosted-runners' in argv for argv in calls if argv[1] == 'attestation')


@pytest.mark.parametrize('change', [lambda cert: cert.update(runInvocationURI='https://github.com/honua-io/honua-release/actions/runs/42/attempts/1'),
    lambda cert: cert.update(sourceRepositoryDigest='b'*40), lambda cert: cert.update(runnerEnvironment='self-hosted'),
    lambda cert: cert.update(buildSignerURI='https://github.com/fork/repo/forged.yml@refs/heads/trunk')])
def test_wrong_attested_attempt_source_runner_or_workflow_is_refused(inputs, change):
    root, source, _, metadata = inputs
    call, _ = verifier(metadata, certificate_change=change)
    with pytest.raises(PackError, match='exact trusted freeze'):
        candidate.authenticate(root, 42, 2, call=call, source=lambda sha, name: source[name], binder=lambda *args, **kwargs: b'')


@pytest.mark.parametrize('key,value', [('run_attempt', 1), ('head_branch', 'feature'), ('status', 'in_progress'),
    ('path', '.github/workflows/other.yml'), ('event', 'pull_request'), ('head_repository', {'full_name': 'fork/repo'})])
def test_untrusted_candidate_metadata_fails_before_attestation(inputs, key, value):
    root, source, _, metadata = inputs
    metadata[key] = value
    call, calls = verifier(metadata)
    with pytest.raises(PackError):
        candidate.authenticate(root, 42, 2, call=call, source=lambda sha, name: source[name], binder=lambda *args, **kwargs: b'')
    assert all(argv[1] != 'attestation' for argv in calls)


def test_lock_cannot_bind_a_different_source_manifest(inputs):
    root, source, lock, metadata = inputs
    lock['sourceInputs']['platformManifest']['sha256'] = 'sha256:'+'0'*64
    (root/'frozen-lock/platform-lock.json').write_bytes(canonical(lock))
    call, _ = verifier(metadata)
    with pytest.raises(PackError, match='authenticated source input'):
        candidate.authenticate(root, 42, 2, call=call, source=lambda sha, name: source[name], binder=lambda *args, **kwargs: b'')


def test_dry_run_cannot_authorize_a_real_campaign(inputs):
    root, source, lock, metadata = inputs
    lock['dryRun'] = True
    (root/'frozen-lock/platform-lock.json').write_bytes(canonical(lock))
    call, _ = verifier(metadata)
    with pytest.raises(PackError, match='dry-run'):
        candidate.authenticate(root, 42, 2, call=call, source=lambda sha, name: source[name], binder=lambda *args, **kwargs: b'')


def test_unprotected_trunk_is_refused(inputs):
    root, source, _, metadata = inputs
    call, _ = verifier(metadata, branch_protected=False)
    with pytest.raises(PackError, match='protected default'):
        candidate.authenticate(root, 42, 2, call=call, source=lambda sha, name: source[name], binder=lambda *args, **kwargs: b'')
