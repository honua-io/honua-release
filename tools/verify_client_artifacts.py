#!/usr/bin/env python3
"""Verify that manifest client pins identify the exact published package bytes."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from email.parser import Parser
from pathlib import Path
from xml.etree import ElementTree

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


class VerificationError(ValueError):
    """A published package does not match its manifest identity or integrity pin."""


class _CrossOriginAuthStripRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # noqa: ANN001
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlparse(request.full_url).hostname != urllib.parse.urlparse(newurl).hostname:
            redirected.remove_header("Authorization")
        return redirected


def _request(url: str, *, token: str | None = None) -> bytes:
    headers = {"Accept": "*/*", "User-Agent": "honua-release-pin-verifier"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        opener = urllib.request.build_opener(_CrossOriginAuthStripRedirect())
        with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as response:
            return response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise VerificationError(f"published artifact is unreachable at {url}: {exc}") from exc


def _request_json(url: str, *, token: str | None = None) -> dict:
    try:
        value = json.loads(_request(url, token=token))
    except json.JSONDecodeError as exc:
        raise VerificationError(f"registry returned invalid JSON for {url}") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"registry returned a non-object response for {url}")
    return value


def _sha256_pin(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _sha512_sri(data: bytes) -> str:
    encoded = base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    return f"sha512-{encoded}"


def _normalise_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _verify_npm_archive(data: bytes, package: str, version: str) -> None:
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            names = set(archive.getnames())
            package_json = archive.extractfile("package/package.json")
            if package_json is None:
                raise VerificationError(f"npm package {package}@{version} has no package/package.json")
            metadata = json.load(package_json)
            if metadata.get("name") != package or str(metadata.get("version")) != version:
                raise VerificationError(f"npm package metadata does not match {package}@{version}")
            bins = metadata.get("bin") or {}
            if isinstance(bins, str):
                bins = {package.rsplit("/", 1)[-1]: bins}
            for command, target in bins.items():
                member = f"package/{str(target).lstrip('./')}"
                if member not in names:
                    raise VerificationError(f"npm package {package}@{version} bin {command!r} targets missing {target!r}")
    except (tarfile.TarError, KeyError, json.JSONDecodeError) as exc:
        raise VerificationError(f"npm package {package}@{version} is not a valid package tarball") from exc


def _verify_wheel(data: bytes, package: str, version: str) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            metadata_files = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(metadata_files) != 1:
                raise VerificationError(f"wheel {package}=={version} must contain exactly one METADATA file")
            metadata = Parser().parsestr(archive.read(metadata_files[0]).decode("utf-8"))
    except (zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise VerificationError(f"wheel {package}=={version} is not a valid wheel") from exc
    if _normalise_package_name(metadata.get("Name", "")) != _normalise_package_name(package):
        raise VerificationError(f"wheel project name does not match {package!r}")
    if metadata.get("Version") != version:
        raise VerificationError(f"wheel version does not match {version!r}")


def _verify_nuget(data: bytes, package: str, version: str) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            nuspecs = [name for name in archive.namelist() if name.lower().endswith(".nuspec")]
            if len(nuspecs) != 1:
                raise VerificationError(f"NuGet package {package}@{version} must contain exactly one .nuspec")
            root = ElementTree.fromstring(archive.read(nuspecs[0]))
    except (zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise VerificationError(f"NuGet package {package}@{version} is not a valid .nupkg") from exc
    fields = {element.tag.rsplit("}", 1)[-1]: (element.text or "") for element in root.iter()}
    if fields.get("id") != package or fields.get("version") != version:
        raise VerificationError(f"NuGet package metadata does not match {package}@{version}")


def _verify_npm(name: str, artifact: dict) -> str:
    package = str(artifact["package"])
    version = str(artifact["version"])
    expected = str(artifact.get("integrity", ""))
    encoded = urllib.parse.quote(package, safe="")
    metadata = _request_json(f"https://registry.npmjs.org/{encoded}/{urllib.parse.quote(version, safe='')}")
    dist = metadata.get("dist") or {}
    if dist.get("integrity") != expected:
        raise VerificationError(f"{name}: npm registry integrity does not match the manifest")
    tarball_url = str(dist.get("tarball", ""))
    if not tarball_url.startswith("https://registry.npmjs.org/"):
        raise VerificationError(f"{name}: npm registry returned an untrusted tarball URL")
    data = _request(tarball_url)
    if _sha512_sri(data) != expected:
        raise VerificationError(f"{name}: downloaded npm bytes do not match manifest integrity")
    _verify_npm_archive(data, package, version)
    return f"npm:{package}@{version}"


def _verify_pypi(name: str, artifact: dict) -> str:
    package = str(artifact["package"])
    version = str(artifact["version"])
    filename = str(artifact.get("filename", ""))
    expected = str(artifact.get("digest", ""))
    metadata = _request_json(
        f"https://pypi.org/pypi/{urllib.parse.quote(package, safe='')}/{urllib.parse.quote(version, safe='')}/json"
    )
    matches = [row for row in metadata.get("urls", []) if row.get("filename") == filename]
    if len(matches) != 1:
        raise VerificationError(f"{name}: PyPI does not publish exactly one {filename!r}")
    row = matches[0]
    if f"sha256:{(row.get('digests') or {}).get('sha256', '')}" != expected:
        raise VerificationError(f"{name}: PyPI digest does not match the manifest")
    url = str(row.get("url", ""))
    if urllib.parse.urlparse(url).hostname not in {"files.pythonhosted.org", "pypi.org"}:
        raise VerificationError(f"{name}: PyPI returned an untrusted download URL")
    data = _request(url)
    if _sha256_pin(data) != expected:
        raise VerificationError(f"{name}: downloaded wheel bytes do not match manifest digest")
    _verify_wheel(data, package, version)
    return f"pypi:{package}=={version}:{filename}"


def _verify_nuget_package(name: str, artifact: dict, github_token: str | None) -> str:
    if artifact.get("registry") != "github-packages":
        raise VerificationError(f"{name}: unsupported NuGet registry {artifact.get('registry')!r}")
    if not github_token:
        raise VerificationError(f"{name}: GITHUB_TOKEN is required to download GitHub Packages bytes")
    package = str(artifact["package"])
    version = str(artifact["version"])
    expected = str(artifact.get("digest", ""))
    repository = str(artifact.get("repository", ""))
    owner = repository.split("/", 1)[0]
    owner_part = urllib.parse.quote(owner, safe="")
    package_part = urllib.parse.quote(package, safe="")
    version_part = urllib.parse.quote(version, safe="")
    filename_part = urllib.parse.quote(f"{package}.{version}.nupkg", safe="")
    url = f"https://nuget.pkg.github.com/{owner_part}/download/{package_part}/{version_part}/{filename_part}"
    data = _request(url, token=github_token)
    if _sha256_pin(data) != expected:
        raise VerificationError(f"{name}: downloaded NuGet bytes do not match manifest digest")
    _verify_nuget(data, package, version)
    return f"nuget:{package}@{version}"


def verify_manifest(manifest: dict, *, github_token: str | None = None) -> list[str]:
    artifacts = manifest.get("clientArtifacts") or {}
    if not isinstance(artifacts, dict) or not artifacts:
        raise VerificationError("clientArtifacts must be a non-empty mapping")
    verified = []
    for name, artifact in sorted(artifacts.items()):
        if not isinstance(artifact, dict):
            raise VerificationError(f"{name}: client artifact must be a mapping")
        if artifact.get("required", True) is False:
            continue
        if artifact.get("publicationState") not in {"published", "promoted"}:
            raise VerificationError(f"{name}: required client artifact is not published/promoted")
        ecosystem = artifact.get("ecosystem")
        if ecosystem == "npm":
            verified.append(_verify_npm(name, artifact))
        elif ecosystem == "pypi":
            verified.append(_verify_pypi(name, artifact))
        elif ecosystem == "nuget":
            verified.append(_verify_nuget_package(name, artifact, github_token))
        else:
            raise VerificationError(f"{name}: unsupported ecosystem {ecosystem!r}")
    return verified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(REPO_ROOT / "platform-manifest.yaml"))
    parser.add_argument("--github-token-env", default="GITHUB_TOKEN")
    args = parser.parse_args(argv)
    manifest = yaml.safe_load(Path(args.manifest).read_text(encoding="utf-8")) or {}
    try:
        verified = verify_manifest(manifest, github_token=os.environ.get(args.github_token_env))
    except VerificationError as exc:
        print(f"ERROR {exc}")
        return 1
    for identity in verified:
        print(f"OK    {identity}")
    print(f"OK    verified {len(verified)} required published client artifact(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
