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


def _pypi_metadata_url():
    pin = json.loads((RECORDED / 'pins.json').read_text())['python']
    return f"https://pypi.org/pypi/{pin['package']}/{pin['version']}/json"


def recorded_responses():
    """Unedited bodies from urls.json, plus the synthetic PyPI metadata document.

    The synthetic document is not a captured response and is not registry evidence.
    """
    urls = json.loads((RECORDED / 'urls.json').read_text())
    responses = {url: (RECORDED / filename).read_bytes() for filename, url in urls.items()}
    metadata_url = _pypi_metadata_url()
    assert metadata_url not in responses
    assert 'pypi.json' not in urls
    responses[metadata_url] = (RECORDED / 'pypi-metadata.synthetic.json').read_bytes()
    return responses


def test_pypi_metadata_fixture_is_synthetic_and_not_recorded_evidence():
    urls = json.loads((RECORDED / 'urls.json').read_text())
    assert 'pypi.json' not in urls
    assert not any(name.startswith('pypi-metadata') for name in urls)
    document = json.loads((RECORDED / 'pypi-metadata.synthetic.json').read_text())
    assert document['synthetic'] is True
    assert 'info' not in document and 'last_serial' not in document
    [row] = document['urls']
    assert set(row) == {'filename', 'digests', 'url'}
    pin = json.loads((RECORDED / 'pins.json').read_text())['python']
    assert row['filename'] == pin['filename']
    assert row['digests'] == {'sha256': pin['digest'].removeprefix('sha256:')}
    assert row['url'] == urls['pypi.whl']


def recorded_registry(monkeypatch):
    """Replay unedited HTTP response bodies; an unexpected URL is a test failure."""
    responses = recorded_responses()
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


def _npm_split_publisher(monkeypatch):
    import mint_nightly_lock as nightly
    package, version, source = '@honua/sdk', '0.1.14', 'a' * 40
    data = _npm_tarball(package, version)
    pin = {'ecosystem': 'npm', 'package': package, 'version': version,
           'repository': 'honua-io/honua-sdk-js', 'sourceSha': source,
           'integrity': vca._sha512_sri(data), 'publicationState': 'published'}
    repository = 'https://github.com/honua-io/honua-sdk-js'
    ref = 'refs/tags/js-sdk-v0.1.14'
    statement = {'_type': 'https://in-toto.io/Statement/v1',
        'predicateType': 'https://slsa.dev/provenance/v1',
        'subject': [{'name': 'pkg:npm/%40honua/sdk@0.1.14',
                     'digest': {'sha512': hashlib.sha512(data).hexdigest()}}],
        'predicate': {'buildDefinition': {
            'buildType': 'https://slsa-framework.github.io/github-actions-buildtypes/workflow/v1',
            'externalParameters': {'workflow': {'ref': ref, 'repository': repository,
                'path': '.github/workflows/publish-js-sdk.yml'}},
            'internalParameters': {'github': {'event_name': 'workflow_dispatch'}},
            'resolvedDependencies': [{'uri': f'git+{repository}@{ref}', 'digest': {'gitCommit': source}}]},
            'runDetails': {'builder': {'id': 'https://github.com/actions/runner/github-hosted'}}}}
    metadata = {'dist': {'integrity': pin['integrity'], 'tarball': 'https://registry.npmjs.org/package.tgz'}}
    bundles = []
    calls = []
    def request_json(url, **kwargs):
        assert not kwargs.get('token')
        if '/-/npm/v1/attestations/' not in url:
            return metadata
        envelope = {'dsseEnvelope': {'payload': base64.b64encode(json.dumps(statement).encode()).decode()}}
        return {'attestations': [{'predicateType': statement['predicateType'], 'bundle': envelope}, *bundles]}
    monkeypatch.setattr(vca, '_request_json', request_json)
    monkeypatch.setattr(vca, '_request', lambda url: data)
    monkeypatch.setattr(nightly, 'verify_publisher_bundle', lambda *args: calls.append(args))
    return pin, metadata, statement, bundles, calls


def test_missing_npm_githead_requires_publisher_verification_of_exact_bytes(monkeypatch):
    pin, _, _, _, calls = _npm_split_publisher(monkeypatch)
    identity, digest = vca._verify_npm('split-sdk', pin)
    assert identity == 'npm:@honua/sdk@0.1.14'
    assert digest.startswith('sha256:')
    [call] = calls
    assert vca._sha256_pin(call[0]) == digest
    assert vca._sha512_sri(call[0]) == pin['integrity']
    assert call[2] == {'coordinate': '@honua/sdk', 'sourceRevision': 'a' * 40,
        'signerWorkflow': 'honua-io/honua-sdk-js/.github/workflows/publish-js-sdk.yml',
        'sourceRef': 'refs/tags/js-sdk-v0.1.14', 'digestAlgorithm': 'sha512', 'denySelfHosted': True}
    assert call[3:] == ('honua-io/honua-sdk-js', 'https://slsa.dev/provenance/v1')


def test_present_conflicting_npm_githead_cannot_use_provenance_fallback(monkeypatch):
    pin, metadata, _, _, calls = _npm_split_publisher(monkeypatch)
    metadata['gitHead'] = 'b' * 40
    with pytest.raises(vca.VerificationError, match='gitHead does not match'):
        vca._verify_npm('split-sdk', pin)
    assert calls == []


def test_matching_npm_githead_preserves_existing_verification(monkeypatch):
    pin, metadata, _, _, calls = _npm_split_publisher(monkeypatch)
    metadata['gitHead'] = pin['sourceSha']
    vca._verify_npm('split-sdk', pin)
    assert calls == []


@pytest.mark.parametrize('drift', ['subject-name', 'subject-digest', 'workflow', 'ref', 'source', 'event', 'runner', 'repository', 'duplicate'])
def test_missing_githead_provenance_rejects_unbound_claims_before_crypto(monkeypatch, drift):
    pin, _, statement, bundles, calls = _npm_split_publisher(monkeypatch)
    build = statement['predicate']['buildDefinition']
    if drift == 'subject-name': statement['subject'][0]['name'] = 'pkg:npm/other@0.1.14'
    elif drift == 'subject-digest': statement['subject'][0]['digest']['sha512'] = '0' * 128
    elif drift == 'workflow': build['externalParameters']['workflow']['path'] = '.github/workflows/unrelated.yml'
    elif drift == 'ref': build['externalParameters']['workflow']['ref'] = 'refs/heads/trunk'
    elif drift == 'source': build['resolvedDependencies'][0]['digest']['gitCommit'] = 'b' * 40
    elif drift == 'event': build['internalParameters']['github']['event_name'] = 'pull_request'
    elif drift == 'runner': statement['predicate']['runDetails']['builder']['id'] = 'self-hosted'
    elif drift == 'repository': pin['repository'] = 'honua-io/other'
    elif drift == 'duplicate': bundles.append({'predicateType': statement['predicateType'], 'bundle': {}})
    with pytest.raises(vca.VerificationError):
        vca._verify_npm('split-sdk', pin)
    assert calls == []


def test_missing_githead_rejects_unsigned_bundle_even_when_claims_match(monkeypatch):
    import mint_nightly_lock as nightly
    import subprocess
    pin, _, _, _, _ = _npm_split_publisher(monkeypatch)
    def refuse(*args):
        raise subprocess.CalledProcessError(1, ['gh', 'attestation', 'verify'], stderr='signature invalid')
    monkeypatch.setattr(nightly, 'verify_publisher_bundle', refuse)
    with pytest.raises(vca.VerificationError, match='provenance verification failed'):
        vca._verify_npm('split-sdk', pin)


def test_ecosystem_scope_selects_only_requested_required_artifacts(monkeypatch):
    common = {'version': '1.0.0', 'sourceSha': 'a' * 40, 'publicationState': 'published'}
    manifest = {'clientArtifacts': {
        'npm': {**common, 'ecosystem': 'npm', 'package': '@honua/sdk', 'integrity': 'unused'},
        'nuget': {**common, 'ecosystem': 'nuget', 'package': 'Honua.Sdk', 'digest': 'unused'},
    }}
    calls = []
    monkeypatch.setattr(vca, '_verify_npm', lambda name, artifact: calls.append(name) or ('npm:@honua/sdk@1.0.0', 'sha256:one'))
    monkeypatch.setattr(vca, '_verify_nuget_package', lambda name, artifact, token: calls.append(name) or 'nuget:Honua.Sdk@1.0.0')
    assert vca.verify_manifest(manifest, ecosystem='npm') == ['npm:@honua/sdk@1.0.0']
    assert calls == ['npm']
    calls.clear()
    assert len(vca.verify_manifest(manifest)) == 2
    assert calls == ['npm', 'nuget']
    with pytest.raises(vca.VerificationError, match='no client artifacts selected'):
        vca.verify_manifest(manifest, ecosystem='pypi')
    with pytest.raises(vca.VerificationError, match='unsupported ecosystem scope'):
        vca.verify_manifest(manifest, ecosystem='unknown')
    manifest['clientArtifacts']['npm']['required'] = False
    with pytest.raises(vca.VerificationError, match='no client artifacts selected'):
        vca.verify_manifest(manifest, ecosystem='npm')


def test_scoped_cli_reports_exact_selected_artifacts_without_full_platform_claim(monkeypatch, tmp_path, capsys):
    manifest = tmp_path / 'manifest.yaml'
    manifest.write_text('clientArtifacts: {}\n')
    calls = []
    monkeypatch.setattr(vca, 'verify_manifest', lambda value, **kwargs:
        calls.append(kwargs) or ['npm:@honua/sdk@0.1.14', 'npm:create-honua-app@0.1.7'])
    assert vca.main(['--manifest', str(manifest), '--ecosystem', 'npm']) == 0
    output = capsys.readouterr().out
    assert 'OK    npm:@honua/sdk@0.1.14' in output
    assert 'OK    npm:create-honua-app@0.1.7' in output
    assert 'SCOPE npm-only; full platform package verification was not requested' in output
    assert 'verified 2 required published npm client artifact(s)' in output
    assert calls[0]['ecosystem'] == 'npm'


def _http_error(url, code):
    import urllib.error
    return urllib.error.HTTPError(url, code, 'Backend is unhealthy' if code == 503 else 'Not Found', {}, None)


PYPI_PROVENANCE = ('https://pypi.org/integrity/honua-admin/0.1.12/'
                   'honua_admin-0.1.12-py3-none-any.whl/provenance')


def test_registry_5xx_is_retried_with_backoff_then_succeeds(monkeypatch):
    calls, sleeps = [], []
    answers = [_http_error(PYPI_PROVENANCE, 503), _http_error(PYPI_PROVENANCE, 502), b'{"ok": true}']

    def open_(url, headers):
        calls.append(url)
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(vca, '_open', open_)
    monkeypatch.setattr(vca, '_sleep', sleeps.append)
    assert vca._request(PYPI_PROVENANCE) == b'{"ok": true}'
    assert len(calls) == 3
    assert sleeps == list(vca.REQUEST_DELAYS[1:3])


def test_registry_5xx_that_outlasts_the_retries_is_transient_not_a_refusal(monkeypatch):
    calls = []

    def open_(url, headers):
        calls.append(url)
        raise _http_error(url, 503)

    monkeypatch.setattr(vca, '_open', open_)
    monkeypatch.setattr(vca, '_sleep', lambda seconds: None)
    with pytest.raises(vca.RegistryUnavailable, match='transient-registry-unavailable') as caught:
        vca._request(PYPI_PROVENANCE)
    assert len(calls) == len(vca.REQUEST_DELAYS)
    assert caught.value.outcome == 'transient-registry-unavailable'
    # Still a VerificationError subtype so no caller can mistake it for a verified pin.
    assert isinstance(caught.value, vca.VerificationError)


def test_registry_404_is_refused_immediately_and_is_not_transient(monkeypatch):
    calls = []

    def open_(url, headers):
        calls.append(url)
        raise _http_error(url, 404)

    monkeypatch.setattr(vca, '_open', open_)
    monkeypatch.setattr(vca, '_sleep', lambda seconds: pytest.fail('a 404 must not be retried'))
    with pytest.raises(vca.VerificationError, match='unreachable') as caught:
        vca._request(PYPI_PROVENANCE)
    assert not isinstance(caught.value, vca.RegistryUnavailable)
    assert len(calls) == 1


def test_verifier_cli_reports_a_registry_outage_with_its_own_exit_status(monkeypatch, tmp_path, capsys):
    manifest = tmp_path / 'm.yaml'
    manifest.write_text('clientArtifacts: {}\n')

    def outage(*args, **kwargs):
        raise vca.RegistryUnavailable('transient-registry-unavailable: registry did not answer')

    monkeypatch.setattr(vca, 'verify_manifest', outage)
    assert vca.main(['--manifest', str(manifest)]) == 75
    assert 'TRANSIENT' in capsys.readouterr().out
