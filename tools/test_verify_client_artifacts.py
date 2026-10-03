from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import zipfile

import pytest

import verify_client_artifacts as vca


def _npm_tarball(name: str, version: str) -> bytes:
    output = io.BytesIO()
    package_json = json.dumps({"name": name, "version": version, "bin": {"honua": "dist/cli.js"}}).encode()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path, data in (("package/package.json", package_json), ("package/dist/cli.js", b"#!/usr/bin/env node\n")):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def _wheel(name: str, version: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(f"{name}-{version}.dist-info/METADATA", f"Name: {name}\nVersion: {version}\n")
    return output.getvalue()


def _nupkg(name: str, version: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(f"{name}.nuspec", f"<package><metadata><id>{name}</id><version>{version}</version></metadata></package>")
    return output.getvalue()


def test_archive_identity_checks_accept_real_package_shapes():
    vca._verify_npm_archive(_npm_tarball("@honua/mcp-server", "1.2.3"), "@honua/mcp-server", "1.2.3")
    vca._verify_wheel(_wheel("honua_sdk", "1.2.3"), "honua-sdk", "1.2.3")
    vca._verify_nuget(_nupkg("Honua.Sdk", "1.2.3"), "Honua.Sdk", "1.2.3")


def test_archive_identity_checks_reject_wrong_package_metadata():
    with pytest.raises(vca.VerificationError, match="metadata does not match"):
        vca._verify_npm_archive(_npm_tarball("wrong", "1.2.3"), "@honua/mcp-server", "1.2.3")
    with pytest.raises(vca.VerificationError, match="version does not match"):
        vca._verify_wheel(_wheel("honua_sdk", "9.9.9"), "honua-sdk", "1.2.3")


def test_digest_helpers_are_exact():
    data = b"published bytes"
    expected_sri = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    assert vca._sha512_sri(data) == expected_sri
    assert vca._sha256_pin(data) == "sha256:" + hashlib.sha256(data).hexdigest()


def _public_nuget(monkeypatch):
    output = io.BytesIO()
    repository = 'https://github.com/honua-io/honua-sdk-dotnet'
    commit = 'a' * 40
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('Honua.Sdk.nuspec', f'<package><metadata><id>Honua.Sdk</id><version>1.10.1</version><repository url="{repository}" commit="{commit}" /></metadata></package>')
    data = output.getvalue()
    pin = {'registry': 'nuget.org', 'package': 'Honua.Sdk', 'version': '1.10.1',
           'filename': 'honua.sdk.1.10.1.nupkg', 'repository': 'honua-io/honua-sdk-dotnet',
           'sourceSha': commit, 'digest': vca._sha256_pin(data)}
    catalog_url = 'https://api.nuget.org/v3/catalog0/package.json'
    package_url = 'https://api.nuget.org/v3-flatcontainer/honua.sdk/1.10.1/honua.sdk.1.10.1.nupkg'
    catalog = {'listed': True, 'id': 'Honua.Sdk', 'version': '1.10.1',
               'packageHashAlgorithm': 'SHA512', 'packageHash': vca._sha512_sri(data).removeprefix('sha512-'),
               'repository': {'url': repository, 'commit': commit}}
    def request_json(url, **kwargs):
        assert not kwargs.get('token')
        return catalog if url == catalog_url else {'listed': True, 'catalogEntry': catalog_url, 'packageContent': package_url}
    def request(url, **kwargs):
        assert url == package_url and not kwargs.get('token')
        return data
    monkeypatch.setattr(vca, '_request_json', request_json)
    monkeypatch.setattr(vca, '_request', request)
    return pin, catalog


def test_public_nuget_checks_bytes_and_source_without_credentials(monkeypatch):
    pin, _ = _public_nuget(monkeypatch)
    assert vca._verify_nuget_package('sdk', pin, None) == 'nuget:Honua.Sdk@1.10.1'


@pytest.mark.parametrize('field,value,message', [
    ('digest', 'sha256:' + '0' * 64, 'manifest digest'),
    ('sourceSha', 'b' * 40, 'repository commit'),
    ('filename', 'other.nupkg', 'filename'),
])
def test_public_nuget_rejects_manifest_identity_drift(monkeypatch, field, value, message):
    pin, _ = _public_nuget(monkeypatch)
    pin[field] = value
    with pytest.raises(vca.VerificationError, match=message):
        vca._verify_nuget_package('sdk', pin, None)


@pytest.mark.parametrize('field,value,message', [
    ('packageHash', 'wrong', 'registry package hash'),
    ('listed', False, 'not listed'),
    ('version', '9.0.0', 'catalog identity'),
    ('repository', {'url': 'https://github.com/other/repo', 'commit': 'a' * 40}, 'repository commit'),
])
def test_public_nuget_rejects_registry_identity_drift(monkeypatch, field, value, message):
    pin, catalog = _public_nuget(monkeypatch)
    catalog[field] = value
    with pytest.raises(vca.VerificationError, match=message):
        vca._verify_nuget_package('sdk', pin, None)


def test_public_nuget_rejects_archive_source_drift(monkeypatch):
    pin, catalog = _public_nuget(monkeypatch)
    pin['sourceSha'] = 'b' * 40
    catalog['repository']['commit'] = pin['sourceSha']
    with pytest.raises(vca.VerificationError, match='archive repository commit'):
        vca._verify_nuget_package('sdk', pin, None)


RECORDED = vca.REPO_ROOT / 'tools' / 'fixtures' / 'client-artifacts'


def recorded_registry(monkeypatch):
    """Replay unedited HTTP response bodies; an unexpected URL is a test failure."""
    urls = json.loads((RECORDED / 'urls.json').read_text())
    responses = {url: (RECORDED / filename).read_bytes() for filename, url in urls.items()}
    reads = []

    def request(url, **kwargs):
        assert not kwargs.get('token')
        reads.append(url)
        return responses[url]

    monkeypatch.setattr(vca, '_request', request)
    return reads


# These expectations are transcribed from the captured registry responses and
# independent sha256sum output, never computed by the verifier under test.
RECORDED_PINS = json.loads((RECORDED / 'pins.json').read_text())


def test_recorded_registries_return_verified_artifact_identities(monkeypatch):
    reads = recorded_registry(monkeypatch)
    assert vca.verify_manifest({'clientArtifacts': RECORDED_PINS}, include_identities=True) == {
        'js': {'version': '0.1.12', 'sourceRevision': '1102d2d55916340edca13cb28411df8da8206f92',
               'sha256': 'sha256:679e0873ae1347be0f7de33ae9876ac82ebd4cf1af6a160e8bcf1e8dc70a7b62'},
        'python': {'version': '0.1.12', 'sourceRevision': '12670676a1e8acb835e911c358adbf46a731120a',
                   'sha256': 'sha256:4ca00c6d585a7325ccb39e15c5e3e4e036e3d91b9bed3471cd5037e1e36efcf2'},
        'dotnet': {'version': '1.10.1', 'sourceRevision': '8a0a06c815baefd49e7398d38a9f22642a8c80c5',
                   'sha256': 'sha256:65e096cdea4d6f2e35226ae3ed3d769fea5f19fc1c4d3612a6339e75a42a8bbd'},
    }
    assert len(reads) == 8  # metadata, bytes, NuGet catalog, and PyPI provenance
    assert vca.verify_manifest({'clientArtifacts': RECORDED_PINS}) == [
        'nuget:Honua.Sdk@1.10.1', 'npm:@honua/sdk-js@0.1.12',
        'pypi:honua-sdk==0.1.12:honua_sdk-0.1.12-py3-none-any.whl',
    ]


@pytest.mark.parametrize('name,field,value,message', [
    ('js', 'sourceSha', 'b' * 40, 'gitHead'),
    ('js', 'integrity', 'sha512-wrong', 'registry integrity'),
    ('python', 'digest', 'sha256:' + '0' * 64, 'PyPI digest'),
    ('dotnet', 'digest', 'sha256:' + '0' * 64, 'manifest digest'),
    ('dotnet', 'sourceSha', 'b' * 40, 'repository commit'),
    ('python', 'sourceSha', 'trunk', 'immutable revision'),
    ('python', 'sourceSha', 'b' * 40, 'manifest sourceSha'),
    ('js', 'publicationState', 'pending', 'not published/promoted'),
])
def test_recorded_identity_drift_never_returns_an_identity(monkeypatch, name, field, value, message):
    import copy
    recorded_registry(monkeypatch)
    pins = copy.deepcopy(RECORDED_PINS)
    pins[name][field] = value
    with pytest.raises(vca.VerificationError, match=message):
        vca.verify_manifest({'clientArtifacts': pins}, include_identities=True)


@pytest.mark.parametrize('name,filename,message', [
    ('js', 'npm.tgz', 'npm bytes'),
    ('python', 'pypi.whl', 'wheel bytes'),
    ('dotnet', 'nuget.nupkg', 'NuGet bytes'),
])
def test_recorded_package_corruption_refuses_identity(monkeypatch, name, filename, message):
    recorded_registry(monkeypatch)
    request = vca._request
    url = json.loads((RECORDED / 'urls.json').read_text())[filename]
    monkeypatch.setattr(vca, '_request', lambda target, **kw:
                        request(target, **kw) + (b'corrupted' if target == url else b''))
    with pytest.raises(vca.VerificationError, match=message):
        vca.verify_manifest({'clientArtifacts': {name: RECORDED_PINS[name]}}, include_identities=True)


def test_recorded_nuget_index_does_not_publish_1_6_2():
    assert json.loads((RECORDED / 'nuget-index.json').read_text())['versions'] == [
        '1.6.4', '1.7.0', '1.8.0', '1.9.0', '1.10.0', '1.10.1',
    ]


@pytest.mark.parametrize('mutation', ['absent', 'subject', 'publisher', 'signature', 'certificate'])
def test_pypi_refuses_missing_or_invalid_provenance(monkeypatch, mutation):
    recorded_registry(monkeypatch)
    request = vca._request
    provenance = json.loads((RECORDED / 'pypi-provenance.json').read_text())
    bundle = provenance['attestation_bundles'][0]
    envelope = bundle['attestations'][0]['envelope']
    if mutation == 'absent':
        provenance['attestation_bundles'] = []
    elif mutation == 'subject':
        statement = json.loads(base64.b64decode(envelope['statement']))
        statement['subject'][0]['digest']['sha256'] = '0' * 64
        envelope['statement'] = base64.b64encode(json.dumps(statement).encode()).decode()
    elif mutation == 'publisher':
        bundle['publisher']['repository'] = 'other/repo'
    elif mutation == 'certificate':
        bundle['attestations'][0]['verification_material']['certificate'] = ''
    else:
        envelope['signature'] = base64.b64encode(b'wrong').decode()
    monkeypatch.setattr(vca, '_request', lambda url, **kw:
                        json.dumps(provenance).encode() if url.endswith('/provenance') else request(url, **kw))
    with pytest.raises(vca.VerificationError, match='PyPI provenance'):
        vca.verify_manifest({'clientArtifacts': {'python': RECORDED_PINS['python']}}, include_identities=True)
