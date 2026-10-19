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
    with pytest.raises(verifier.VerificationError, match='existing public NuGet'):
        proposal.propose(manifest, 'sdk', '1.1.1')


@pytest.mark.parametrize('source', [None, 'main', 'b' * 39, 'B' * 40])
def test_registry_source_must_be_immutable(monkeypatch, source):
    monkeypatch.setattr(verifier, '_request_json', lambda url: {'catalogEntry': 'https://api.nuget.org/v3/catalog0/x'}
                        if 'registration' in url else {'repository': {'commit': source}})
    with pytest.raises(verifier.VerificationError, match='immutable repository commit'):
        proposal.propose(fixture(), 'sdk', '1.1.1')
