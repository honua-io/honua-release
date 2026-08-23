from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_registry_artifact as verify  # noqa: E402


def test_published_registry_bytes_must_join_the_component_source(tmp_path: Path):
    artifact = tmp_path / "package.whl"
    artifact.write_bytes(b"stable registry bytes")
    component = {"sha": "a" * 40}
    provenance = {
        "status": "published",
        "filename": artifact.name,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "repository": "honua-io/example",
        "sourceSha": "b" * 40,
    }

    with pytest.raises(verify.VerificationError, match="different source SHA"):
        verify._verify_common(component, provenance, artifact)


def test_unpublished_version_cannot_accept_a_surprise_registry_package():
    with pytest.raises(verify.VerificationError, match="unpublished"):
        verify._published_provenance(
            {
                "artifactProvenance": {
                    "status": "unpublished",
                    "repository": "honua-io/honua-sdk-dotnet",
                }
            }
        )


def test_nuget_requires_embedded_repository_commit(tmp_path: Path):
    component_sha = "a" * 40
    package = tmp_path / "Honua.Sdk.1.6.0.nupkg"
    nuspec = """<?xml version="1.0"?>
<package xmlns="http://schemas.microsoft.com/packaging/2013/05/nuspec.xsd">
  <metadata>
    <id>Honua.Sdk</id>
    <version>1.6.0</version>
    <repository type="git" url="https://github.com/honua-io/honua-sdk-dotnet" commit="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" />
  </metadata>
</package>
"""
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("Honua.Sdk.nuspec", nuspec)
    manifest = {
        "components": {
            "honua-sdk-dotnet": {
                "version": "1.6.0",
                "sha": component_sha,
                "artifactProvenance": {
                    "status": "published",
                    "filename": package.name,
                    "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
                    "repository": "honua-io/honua-sdk-dotnet",
                    "sourceSha": component_sha,
                },
            }
        }
    }
    manifest_path = tmp_path / "platform-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")

    with pytest.raises(verify.VerificationError, match="repository commit"):
        verify.verify_nuget(manifest_path, package)


def test_pypi_verifies_the_exact_provenance_document(
    tmp_path: Path,
    monkeypatch,
):
    component_sha = "a" * 40
    wheel = tmp_path / "honua_sdk-0.1.10-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "honua_sdk-0.1.10.dist-info/METADATA",
            "Name: honua-sdk\nVersion: 0.1.10\n\n",
        )
    manifest = {
        "components": {
            "honua-sdk-python": {
                "version": "0.1.10",
                "sha": component_sha,
                "artifactProvenance": {
                    "status": "published",
                    "filename": wheel.name,
                    "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    "repository": "honua-io/honua-sdk-python",
                    "workflow": "publish-python-sdk.yml",
                    "sourceSha": component_sha,
                },
            }
        }
    }
    manifest_path = tmp_path / "platform-manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    provenance_path = tmp_path / "provenance.json"
    provenance_path.write_text(
        json.dumps(
            {
                "attestation_bundles": [
                    {
                        "publisher": {
                            "kind": "GitHub",
                            "repository": "honua-io/honua-sdk-python",
                            "workflow": "publish-python-sdk.yml",
                        },
                        "attestations": [
                            {"verification_material": {"certificate": "certificate"}}
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return verify.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(verify.subprocess, "run", fake_run)
    monkeypatch.setattr(verify, "_certificate_source_sha", lambda _value: component_sha)

    verify.verify_pypi(manifest_path, wheel, provenance_path)

    command = commands[0]
    assert command[:3] == ["pypi-attestations", "verify", "pypi"]
    assert command[command.index("--provenance-file") + 1] == str(provenance_path)
