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
