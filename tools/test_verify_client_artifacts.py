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
