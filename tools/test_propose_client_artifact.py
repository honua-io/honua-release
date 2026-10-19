import copy
import pytest
import propose_client_artifact as proposal
import verify_client_artifacts as verifier


def fixture():
    return {'clientArtifacts': {'sdk': {'ecosystem': 'nuget', 'registry': 'nuget.org', 'package': 'Honua.Sdk',
            'version': '1.0.0', 'sourceSha': 'a' * 40, 'repository': 'honua-io/honua-sdk-dotnet',
            'targets': ['dotnet'], 'publicationState': 'published'}}}


def test_proposal_derives_registry_identity_and_preserves_manifest(monkeypatch):
    manifest = fixture()
    before = copy.deepcopy(manifest)
    monkeypatch.setattr(verifier, '_request_json', lambda url: {'catalogEntry': 'https://api.nuget.org/v3/catalog0/x'}
                        if 'registration' in url else {'repository': {'commit': 'b' * 40}})
    monkeypatch.setattr(verifier, '_request', lambda url: b'actual package bytes')
    def verify(value, **kwargs):
        artifact = value['clientArtifacts']['sdk']
        assert artifact['digest'] == verifier._sha256_pin(b'actual package bytes')
        assert artifact['sourceSha'] == 'b' * 40
        assert artifact['targets'] == ['dotnet']
        return {'sdk': {'version': '1.1.1', 'sourceRevision': 'b' * 40, 'sha256': artifact['digest']}}
    monkeypatch.setattr(verifier, 'verify_manifest', verify)
    result = proposal.propose(manifest, 'sdk', '1.1.1')
    assert manifest == before
    assert result['candidateQualification'] == 'pending'
    assert result['state'] == 'review-required'


@pytest.mark.parametrize('version', ['latest', '*', '1.1', '../1.1.1', '1.1.1/other'])
def test_refuses_mutable_or_ambiguous_versions(version):
    with pytest.raises(verifier.VerificationError, match='exact package version'):
        proposal.propose(fixture(), 'sdk', version)


def test_refuses_untrusted_catalog_before_download(monkeypatch):
    monkeypatch.setattr(verifier, '_request_json', lambda url: {'catalogEntry': 'https://evil.example/x'})
    with pytest.raises(verifier.VerificationError, match='untrusted catalog'):
        proposal.propose(fixture(), 'sdk', '1.1.1')


def test_verification_failure_produces_no_proposal(monkeypatch):
    monkeypatch.setattr(verifier, '_request_json', lambda url: {'catalogEntry': 'https://api.nuget.org/v3/catalog0/x'}
                        if 'registration' in url else {'repository': {'commit': 'b' * 40}})
    monkeypatch.setattr(verifier, '_request', lambda url: b'bytes')
    def reject(*args, **kwargs):
        raise verifier.VerificationError('archive repository mismatch')
    monkeypatch.setattr(verifier, 'verify_manifest', reject)
    with pytest.raises(verifier.VerificationError, match='archive repository mismatch'):
        proposal.propose(fixture(), 'sdk', '1.1.1')


def test_optional_artifact_cannot_skip_package_verification():
    manifest = fixture()
    manifest['clientArtifacts']['sdk']['required'] = False
    with pytest.raises(verifier.VerificationError, match='existing required client'):
        proposal.propose(manifest, 'sdk', '1.1.1')


@pytest.mark.parametrize('source', [None, 'main', 'b' * 39, 'B' * 40])
def test_registry_source_must_be_immutable(monkeypatch, source):
    monkeypatch.setattr(verifier, '_request_json', lambda url: {'catalogEntry': 'https://api.nuget.org/v3/catalog0/x'}
                        if 'registration' in url else {'repository': {'commit': source}})
    with pytest.raises(verifier.VerificationError, match='immutable repository commit'):
        proposal.propose(fixture(), 'sdk', '1.1.1')


def test_npm_proposal_verifies_recorded_package_bytes(monkeypatch):
    from test_verify_client_artifacts import RECORDED_PINS, recorded_registry
    recorded_registry(monkeypatch)
    current = copy.deepcopy(RECORDED_PINS['js'])
    current['version'] = '0.0.0'
    current['sourceSha'] = 'a' * 40
    result = proposal.propose({'clientArtifacts': {'js': current}}, 'js', '0.1.12')
    assert result['verifiedIdentity']['sourceRevision'] == '1102d2d55916340edca13cb28411df8da8206f92'
    assert result['verifiedIdentity']['sha256'] == 'sha256:679e0873ae1347be0f7de33ae9876ac82ebd4cf1af6a160e8bcf1e8dc70a7b62'
    assert result['candidateQualification'] == 'pending'


def test_pypi_proposal_derives_signed_source_and_checks_real_wheel_bytes(monkeypatch):
    from test_verify_client_artifacts import RECORDED_PINS, recorded_registry
    recorded_registry(monkeypatch)
    request_json = verifier._request_json
    def metadata(url, **kwargs):
        value = request_json(url, **kwargs)
        # The existing PyPI metadata fixture is explicitly synthetic. Add its
        # wheel type for discovery; the recorded wheel/provenance stay unedited.
        if url.endswith('/json') and 'pypi.org/pypi/' in url:
            value['urls'][0]['packagetype'] = 'bdist_wheel'
        return value
    monkeypatch.setattr(verifier, '_request_json', metadata)
    current = copy.deepcopy(RECORDED_PINS['python'])
    current.update(version='0.0.0', sourceSha='a' * 40)
    result = proposal.propose({'clientArtifacts': {'python': current}}, 'python', '0.1.12')
    assert result['verifiedIdentity']['sourceRevision'] == '12670676a1e8acb835e911c358adbf46a731120a'
    assert result['verifiedIdentity']['sha256'] == 'sha256:4ca00c6d585a7325ccb39e15c5e3e4e036e3d91b9bed3471cd5037e1e36efcf2'


def test_npm_unsigned_source_hint_never_skips_publisher_verification(monkeypatch):
    from test_verify_client_artifacts import _npm_split_publisher
    pin, _, _, _, calls = _npm_split_publisher(monkeypatch)
    result = proposal.propose({'clientArtifacts': {'js': pin}}, 'js', '0.1.14')
    assert result['verifiedIdentity']['sourceRevision'] == 'a' * 40
    assert len(calls) == 1
    assert calls[0][2]['sourceRevision'] == 'a' * 40


def test_npm_bad_publisher_signature_cannot_produce_proposal(monkeypatch):
    import subprocess
    import mint_nightly_lock
    from test_verify_client_artifacts import _npm_split_publisher
    pin, _, _, _, _ = _npm_split_publisher(monkeypatch)
    def reject(*args):
        raise subprocess.CalledProcessError(1, ['gh', 'attestation', 'verify'])
    monkeypatch.setattr(mint_nightly_lock, 'verify_publisher_bundle', reject)
    with pytest.raises(verifier.VerificationError, match='publisher provenance verification failed'):
        proposal.propose({'clientArtifacts': {'js': pin}}, 'js', '0.1.14')


@pytest.mark.parametrize('count', [0, 2])
def test_pypi_ambiguous_wheel_is_refused(monkeypatch, count):
    manifest = {'clientArtifacts': {'python': {'ecosystem': 'pypi', 'package': 'honua-sdk'}}}
    monkeypatch.setattr(verifier, '_request_json', lambda url: {'urls': [{'packagetype': 'bdist_wheel'}] * count})
    with pytest.raises(verifier.VerificationError, match='exactly one published wheel'):
        proposal.propose(manifest, 'python', '0.1.17')


def test_npm_discovery_refuses_external_download_origin(monkeypatch):
    manifest = {'clientArtifacts': {'js': {'ecosystem': 'npm', 'package': '@honua/sdk-js'}}}
    monkeypatch.setattr(verifier, '_request_json', lambda url: {'dist': {'tarball': 'https://evil.example/package.tgz'}})
    with pytest.raises(verifier.VerificationError, match='untrusted tarball'):
        proposal.propose(manifest, 'js', '0.2.3')
