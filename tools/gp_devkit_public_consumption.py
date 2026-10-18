#!/usr/bin/env python3
"""Prove anonymous consumption of the exact qualified public GP devkit bytes."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener
import xml.etree.ElementTree as ET

import gp_devkit_artifacts as gate
import gp_fetch_sdk


def validate_asset_url(url, *, redirect=False):
    parsed = urlsplit(url)
    hosts = {"github.com"}
    if redirect:
        hosts |= {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
    gate.require(parsed.scheme == "https" and parsed.netloc in hosts and not parsed.fragment,
                 "Public package URL must remain on the GitHub release asset hosts")
    if parsed.netloc == "github.com":
        gate.require(not parsed.query and re.fullmatch(
            r"/honua-io/honua-server/releases/download/gp-devkit-v[0-9A-Za-z.-]+/[^/]+\.nupkg", parsed.path),
            "Public package must be an exact versioned Honua release asset")
    return url


class AssetRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        validate_asset_url(new_url, redirect=True)
        return super().redirect_request(request, response, code, message, headers, new_url)


def download_package(url, destination, expected_sha256):
    validate_asset_url(url)
    gate.require(re.fullmatch(r"[0-9a-f]{64}", expected_sha256), "Package SHA-256 required")
    # No environment proxy, credential store, cookies, or Authorization headers.
    opener = build_opener(ProxyHandler({}), AssetRedirects())
    partial = destination.with_suffix(".partial")
    digest, size = hashlib.sha256(), 0
    try:
        with opener.open(url, timeout=90) as response, partial.open("xb") as output:
            gate.require(response.status == 200, "Anonymous package download failed")
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                gate.require(size <= 512 * 1024 * 1024, "Public package exceeds its size budget")
                digest.update(chunk)
                output.write(chunk)
        gate.require(size > 0 and digest.hexdigest() == expected_sha256,
                     "Anonymous download differs from the qualified package")
        partial.rename(destination)
    finally:
        partial.unlink(missing_ok=True)
    return size


def require_image_identity(document, release):
    gate.require(document.get("Id") == release["imageConfigDigest"]
                 and document.get("Os") == "linux" and document.get("Architecture") == "amd64"
                 and release["sandboxImage"] in document.get("RepoDigests", []),
                 "Anonymous registry pull differs from the qualified Linux x64 image")


def verify(options):
    candidate_root = options.candidate_directory.resolve(strict=True)
    candidate = gate.load(candidate_root / "devkit-candidate.json")
    name = candidate["package"]["file"]
    gate.require(Path(name).name == name and name.endswith(".nupkg"), "Invalid package filename")
    package = candidate_root / name
    gate.require(gate.qualify(candidate_root / "qualification", package, candidate["sourceSha"]) == candidate,
                 "Retained candidate evidence no longer qualifies")
    release = gate.load(options.release_identity)
    gate.require(release == gate.published(candidate, release["sandboxImage"], options.registry_manifest.read_bytes()),
                 "Publication identity differs from the qualified candidate")

    root = options.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    feed = root / "feed"
    feed.mkdir()
    downloaded = feed / name
    size = download_package(release["package"]["url"], downloaded, candidate["package"]["sha256"])
    gate.require(gate.package_identity(downloaded, candidate["sourceSha"])
                 == gate.package_identity(package, candidate["sourceSha"]), "Public package assembly changed")

    # Acquire Microsoft's complete SDK independently, never a source-build SDK cache.
    sdk_input = root / "sdk-input"
    sdk = gp_fetch_sdk.acquire(candidate["sdk"]["version"], sdk_input)
    gate.require(sdk["sha256"] == candidate["sdk"]["archiveSha256"], "Public install SDK differs from qualification")
    dotnet = root / "dotnet"
    dotnet.mkdir()
    with tarfile.open(sdk_input / sdk["file"]) as archive:
        archive.extractall(dotnet, filter="data")
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
    cli_home, docker_config = root / "cli-home", root / "docker-config"
    cli_home.mkdir()
    docker_config.mkdir()
    (docker_config / "config.json").write_text('{"auths":{}}\n')
    env |= {"DOTNET_ROOT": str(dotnet), "PATH": str(dotnet) + os.pathsep + env.get("PATH", ""),
            "DOTNET_CLI_HOME": str(cli_home), "NUGET_PACKAGES": str(root / "packages"),
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1", "DOCKER_CONFIG": str(docker_config)}
    config = root / "NuGet.Config"
    configuration = ET.Element("configuration")
    sources = ET.SubElement(configuration, "packageSources")
    ET.SubElement(sources, "clear")
    ET.SubElement(sources, "add", key="anonymous-public-package", value=str(feed))
    ET.ElementTree(configuration).write(config)
    tools = root / "tools"
    subprocess.run([str(dotnet / "dotnet"), "tool", "install", "Honua.Geoprocessing.Cli", "--version", candidate["version"],
                    "--tool-path", str(tools), "--configfile", str(config)], cwd=root, env=env, check=True, timeout=180)
    bootstrap = root / "bootstrap"
    subprocess.run([str(tools / "honua-gp-devkit"), "workspace", "bootstrap", "--output", str(bootstrap)],
                   cwd=root, env=env, check=True, timeout=60)
    payload = gate.load(bootstrap / "workspace-provenance.json")
    expected = gate.load(candidate_root / "qualification/assembly.json")["bootstrap"]
    gate.require(payload["files"] == expected["files"] and payload["devkitVersion"] == expected["devkitVersion"],
                 "Public installed bootstrap differs from the qualified payload")
    for relative, digest in payload["files"].items():
        path = bootstrap / relative
        gate.require(path.resolve().is_relative_to(bootstrap) and gate.sha256(path) == digest,
                     "Public installed bootstrap bytes changed")
    subprocess.run(["docker", "pull", "--platform", "linux/amd64", release["sandboxImage"]],
                   cwd=root, env=env, check=True, timeout=900)
    inspected = subprocess.run(["docker", "image", "inspect", "--format", "{{json .}}", release["sandboxImage"]],
                               cwd=root, env=env, check=True, timeout=30, capture_output=True, text=True)
    require_image_identity(json.loads(inspected.stdout), release)
    receipt = {"schema": "honua.gp.devkit-public-consumption/v1", "sourceSha": candidate["sourceSha"],
               "version": candidate["version"], "package": release["package"] | {"bytes": size},
               "sandboxImage": release["sandboxImage"], "imageConfigDigest": release["imageConfigDigest"],
               "checks": ["anonymous-package-download", "exact-package-bytes", "isolated-installed-bootstrap",
                          "anonymous-registry-pull", "qualified-image-identity"],
               "scope": "Public artifact consumption only; integrated GIS journeys require separate evidence"}
    (root / "public-consumption.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-directory", type=Path, required=True)
    parser.add_argument("--release-identity", type=Path, required=True)
    parser.add_argument("--registry-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(verify(parser.parse_args())))
