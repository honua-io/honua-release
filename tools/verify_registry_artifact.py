#!/usr/bin/env python3
"""Verify stable registry bytes and their build source against the platform manifest."""
from __future__ import annotations

import argparse
import base64
import email.parser
import hashlib
import json
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

import yaml

SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
GITHUB_SOURCE_SHA_OID = "1.3.6.1.4.1.57264.1.3"


class VerificationError(ValueError):
    """The registry artifact cannot be attributed to the manifest component."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _component(manifest_path: Path, name: str) -> dict[str, Any]:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    component = (manifest.get("components") or {}).get(name)
    if not isinstance(component, dict):
        raise VerificationError(f"manifest has no component {name}")
    return component


def _published_provenance(component: dict[str, Any]) -> dict[str, Any]:
    provenance = component.get("artifactProvenance")
    if not isinstance(provenance, dict):
        raise VerificationError("component has no artifactProvenance contract")
    if provenance.get("status") != "published":
        raise VerificationError(
            "manifest marks this stable registry artifact unpublished; refusing an unpinned package"
        )
    required = {"status", "filename", "sha256", "repository", "sourceSha"}
    if not required.issubset(provenance):
        raise VerificationError("published artifactProvenance omits immutable identity fields")
    if not SHA256.fullmatch(str(provenance.get("sha256", ""))):
        raise VerificationError("artifactProvenance sha256 is not a digest")
    if not SHA40.fullmatch(str(provenance.get("sourceSha", ""))):
        raise VerificationError("artifactProvenance sourceSha is not a full Git SHA")
    return provenance


def _verify_common(
    component: dict[str, Any],
    provenance: dict[str, Any],
    artifact: Path,
) -> None:
    if artifact.name != provenance["filename"]:
        raise VerificationError(
            f"registry filename {artifact.name!r} does not match manifest {provenance['filename']!r}"
        )
    if _sha256(artifact) != provenance["sha256"]:
        raise VerificationError("registry artifact bytes do not match the manifest SHA-256")
    component_sha = str(component.get("sha", ""))
    if provenance["sourceSha"] != component_sha:
        raise VerificationError(
            "registry artifact was built from a different source SHA than the manifest component"
        )


def _normalized_repository(value: str) -> str:
    return value.removesuffix(".git").rstrip("/")


def verify_nuget(manifest_path: Path, artifact: Path) -> None:
    component = _component(manifest_path, "honua-sdk-dotnet")
    provenance = _published_provenance(component)
    _verify_common(component, provenance, artifact)
    with zipfile.ZipFile(artifact) as package:
        nuspecs = [name for name in package.namelist() if name.lower().endswith(".nuspec")]
        if len(nuspecs) != 1:
            raise VerificationError("NuGet package does not contain exactly one nuspec")
        root = ET.fromstring(package.read(nuspecs[0]))
    metadata = next(
        (item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "metadata"),
        None,
    )
    if metadata is None:
        raise VerificationError("NuGet nuspec has no metadata")
    values = {
        item.tag.rsplit("}", 1)[-1]: (item.text or "").strip()
        for item in metadata
    }
    repository = next(
        (item for item in metadata if item.tag.rsplit("}", 1)[-1] == "repository"),
        None,
    )
    expected_repository = f"https://github.com/{provenance['repository']}"
    if (
        values.get("id") != "Honua.Sdk"
        or values.get("version") != str(component.get("version"))
        or repository is None
        or _normalized_repository(repository.attrib.get("url", ""))
        != expected_repository
        or repository.attrib.get("commit") != provenance["sourceSha"]
    ):
        raise VerificationError(
            "NuGet nuspec does not bind package id/version/repository commit to the manifest"
        )


def _certificate_source_sha(certificate: str) -> str:
    try:
        der = base64.b64decode(certificate, validate=True)
    except (ValueError, TypeError) as exc:
        raise VerificationError("PyPI provenance certificate is not valid base64 DER") from exc
    try:
        result = subprocess.run(
            ["openssl", "x509", "-inform", "DER", "-noout", "-text"],
            input=der,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise VerificationError("could not inspect the verified PyPI certificate") from exc
    lines = result.stdout.decode("utf-8", errors="replace").splitlines()
    for index, line in enumerate(lines[:-1]):
        if GITHUB_SOURCE_SHA_OID in line:
            candidate = lines[index + 1].strip().lstrip(".(")
            if SHA40.fullmatch(candidate):
                return candidate
    raise VerificationError("PyPI certificate has no GitHub source SHA extension")


def verify_pypi(manifest_path: Path, artifact: Path, provenance_path: Path) -> None:
    component = _component(manifest_path, "honua-sdk-python")
    provenance = _published_provenance(component)
    _verify_common(component, provenance, artifact)
    with zipfile.ZipFile(artifact) as wheel:
        metadata_files = [
            name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_files) != 1:
            raise VerificationError("wheel does not contain exactly one dist-info METADATA")
        metadata = email.parser.BytesParser().parsebytes(wheel.read(metadata_files[0]))
    if (
        str(metadata.get("Name", "")).lower().replace("_", "-") != "honua-sdk"
        or metadata.get("Version") != str(component.get("version"))
    ):
        raise VerificationError(
            "wheel distribution/version does not match the manifest component"
        )
    repository_url = f"https://github.com/{provenance['repository']}"
    try:
        subprocess.run(
            [
                "pypi-attestations",
                "verify",
                "pypi",
                "--repository",
                repository_url,
                "--provenance-file",
                str(provenance_path),
                str(artifact),
            ],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise VerificationError("PyPI Sigstore attestation verification failed") from exc

    document = json.loads(provenance_path.read_text(encoding="utf-8"))
    bundles = document.get("attestation_bundles")
    if not isinstance(bundles, list) or not bundles:
        raise VerificationError("PyPI provenance has no attestation bundle")
    matching_certificates: list[str] = []
    for bundle in bundles:
        publisher = bundle.get("publisher") if isinstance(bundle, dict) else None
        if not isinstance(publisher, dict):
            continue
        if (
            publisher.get("kind") != "GitHub"
            or publisher.get("repository") != provenance["repository"]
            or publisher.get("workflow") != provenance.get("workflow")
        ):
            continue
        for attestation in bundle.get("attestations") or []:
            certificate = (attestation.get("verification_material") or {}).get(
                "certificate"
            )
            if isinstance(certificate, str):
                matching_certificates.append(certificate)
    if not matching_certificates:
        raise VerificationError("PyPI publisher repository/workflow does not match the manifest")
    if provenance["sourceSha"] not in {
        _certificate_source_sha(certificate) for certificate in matching_certificates
    }:
        raise VerificationError("PyPI certificate source SHA does not match the manifest")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("nuget", "pypi"))
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--provenance", type=Path)
    args = parser.parse_args()
    try:
        if args.kind == "nuget":
            verify_nuget(args.manifest, args.artifact)
        else:
            if args.provenance is None:
                raise VerificationError("--provenance is required for PyPI")
            verify_pypi(args.manifest, args.artifact, args.provenance)
    except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise SystemExit(f"registry provenance verification failed: {exc}") from exc
    print(f"verified {args.kind} artifact {args.artifact.name} against manifest source")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
