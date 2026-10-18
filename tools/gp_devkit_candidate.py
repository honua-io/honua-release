#!/usr/bin/env python3
"""Qualify a packaged devkit through its installed bootstrap on a source-free runner."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET

import gp_devkit_artifacts as gate
import gp_fetch_sdk


def prepare(options):
    packages = list(options.package_directory.glob("*.nupkg"))
    gate.require(len(packages) == 1, "Exactly one downloaded CLI package is required")
    package = packages[0].resolve(strict=True)
    gate.require(gate.sha256(package) == options.package_sha256, "Downloaded package differs from the producer hash")
    version, _ = gate.package_identity(package, options.source_sha)
    gate.require(re.fullmatch(r"[0-9a-f]{40}", options.source_sha), "Full source SHA required")
    gate.require(re.fullmatch(r"(?:docker\.io/library/)?python@sha256:[0-9a-f]{64}", options.python_image), "Immutable Python base required")
    sdk_input = options.sdk_directory.resolve(strict=True)
    sdk_receipt = gate.load(sdk_input / "official-receipt.json")
    metadata = sdk_input / "official-metadata.json"
    gate.require(gate.sha256(metadata) == sdk_receipt["metadataSha256"], "Official SDK metadata changed")
    url, sha512 = gp_fetch_sdk.select(gate.load(metadata), sdk_receipt["version"])
    gate.require(url == sdk_receipt["url"] and sha512 == sdk_receipt["sha512"], "SDK receipt differs from official metadata")
    sdk = sdk_input / f'dotnet-sdk-{sdk_receipt["version"]}-linux-x64.tar.gz'
    with sdk.open("rb") as stream:
        actual_sha512 = hashlib.file_digest(stream, "sha512").hexdigest()
    gate.require(actual_sha512 == sha512 and gate.sha256(sdk) == sdk_receipt["sha256"], "Complete SDK archive bytes changed")
    root = options.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    initial = root / "initial-install"
    initial.mkdir()
    dotnet_root = initial / "dotnet"
    dotnet_root.mkdir()
    with tarfile.open(sdk) as archive:
        archive.extractall(dotnet_root, filter="data")
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
    env |= {"DOTNET_ROOT": str(dotnet_root), "PATH": str(dotnet_root) + os.pathsep + env.get("PATH", ""),
            "DOTNET_CLI_HOME": str(initial / "cli-home"),
            "NUGET_PACKAGES": str(initial / "packages"), "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1"}
    Path(env["DOTNET_CLI_HOME"]).mkdir()
    feed = initial / "feed"
    feed.mkdir()
    shutil.copy2(package, feed / package.name)
    config = initial / "NuGet.Config"
    configuration = ET.Element("configuration")
    sources = ET.SubElement(configuration, "packageSources")
    ET.SubElement(sources, "clear")
    ET.SubElement(sources, "add", key="qualified-package", value=str(feed))
    ET.ElementTree(configuration).write(config)
    tool_path = initial / "tools"
    subprocess.run([str(dotnet_root / "dotnet"), "tool", "install", "Honua.Geoprocessing.Cli",
                    "--version", version, "--tool-path", str(tool_path), "--configfile", str(config)],
                   cwd=initial, env=env, check=True, timeout=180)
    bootstrap = initial / "bootstrap"
    subprocess.run([str(tool_path / "honua-gp-devkit"), "workspace", "bootstrap", "--output", str(bootstrap)],
                   cwd=initial, env=env, check=True, timeout=60)
    provenance = gate.load(bootstrap / "workspace-provenance.json")
    for name, expected in provenance["files"].items():
        path = bootstrap / name
        gate.require(path.resolve().is_relative_to(bootstrap) and gate.sha256(path) == expected, "Installed bootstrap bytes changed")
    assembly = root / "assembly"
    # The one canonical assembler is installed from the package; no script is copied from server source.
    subprocess.run([sys.executable, str(bootstrap / "assemble_workspace_image.py"),
                    "--nupkg", str(package), "--nupkg-sha256", options.package_sha256,
                    "--sdk-archive", str(sdk), "--sdk-sha512", sdk_receipt["sha512"], "--sdk-version", sdk_receipt["version"],
                    "--python-image", options.python_image, "--root", str(assembly)],
                   cwd=initial, env=env, check=True, timeout=3000)
    candidate = gate.qualify(assembly, package, options.source_sha)
    (root / "devkit-candidate.json").write_text(json.dumps(candidate, indent=2) + "\n")
    export = root / "export"
    export.mkdir()
    shutil.copy2(package, export / package.name)
    shutil.copy2(root / "devkit-candidate.json", export / "devkit-candidate.json")
    shutil.copy2(metadata, export / metadata.name)
    shutil.copy2(sdk_input / "official-receipt.json", export / "official-receipt.json")
    proof = export / "qualification"
    proof.mkdir()
    shutil.copy2(assembly / "assembly.json", proof / "assembly.json")
    for folder in ("installed-qualification", "fixture-qualification"):
        (proof / folder).mkdir()
        shutil.copy2(assembly / folder / "qualification.json", proof / folder / "qualification.json")
    # Retain only the declared successful GeoJSON outputs, not arbitrary daemon state or credentials.
    fixtures = gate.load(assembly / "fixture-qualification/qualification.json")
    for run in fixtures["runs"]:
        relative = Path("fixture-qualification/runs") / run["corrected"]["runId"] / "output" / run["geoJsonArtifact"]["path"]
        destination = proof / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(assembly / relative, destination)
    gate.require(gate.qualify(proof, export / package.name, options.source_sha) == candidate,
                 "Exported evidence no longer qualifies the candidate")
    return candidate


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-directory", type=Path, required=True)
    parser.add_argument("--sdk-directory", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--package-sha256", required=True)
    parser.add_argument("--python-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(prepare(parser.parse_args())))
