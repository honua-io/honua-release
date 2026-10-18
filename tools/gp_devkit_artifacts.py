#!/usr/bin/env python3
"""Bind installed GP qualification to the exact public package and sandbox bytes.

This gate does not build, execute, publish, or certify a server/map/cloud journey.
The normal producer must run the installed bootstrap and both language fixtures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile


class ArtifactError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ArtifactError(message)


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
    require(len(raw) <= 8 * 1024 * 1024, "Evidence exceeds the bounded JSON budget")
    return json.loads(raw)


def package_identity(path, source_sha):
    with zipfile.ZipFile(path) as package:
        nuspecs = [name for name in package.namelist() if name.endswith(".nuspec")]
        require(len(nuspecs) == 1, "Exactly one package specification is required")
        root = ET.fromstring(package.read(nuspecs[0]))
        metadata = next(node for node in root if node.tag.rsplit("}", 1)[-1] == "metadata")
        fields = {node.tag.rsplit("}", 1)[-1]: node for node in metadata}
        require(fields["id"].text == "Honua.Geoprocessing.Cli", "Unexpected devkit package identity")
        version = fields["version"].text
        require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version or ""), "Exact package version required")
        repository = fields.get("repository")
        require(repository is not None and repository.get("commit") == source_sha and
                repository.get("url", "").removesuffix(".git").rstrip("/") == "https://github.com/honua-io/honua-server",
                "Package repository/source binding differs from the reviewed server source")
        assembly = package.read("tools/net10.0/any/honua-gp.dll")
        return version, hashlib.sha256(assembly).hexdigest()


def qualify(root, package, source_sha, *, expected_python_image=None):
    root, package = Path(root), Path(package)
    require(re.fullmatch(r"[0-9a-f]{40}", source_sha), "Full server source SHA required")
    version, assembly_hash = package_identity(package, source_sha)
    prepared = load(root / "assembly.json")
    require(prepared.get("schema") == "honua.gp.workspace-assembly/v1" and prepared.get("qualified") is True,
            "Installed assembly has not completed qualification")
    image = prepared.get("image", "")
    require(re.fullmatch(r"sha256:[0-9a-f]{64}", image), "Exact locally qualified image config digest required")
    require(prepared.get("nupkg") == {"version": version, "sha256": sha256(package)}, "Qualified package bytes/version differ")
    require(prepared.get("nuget") and prepared.get("wheels", {}).get("wheels"), "Locked dependency provenance is missing")
    sdk = prepared.get("sdk", {})
    require(re.fullmatch(r"10\.0\.[0-9]+", sdk.get("version", "")) and
            re.fullmatch(r"[0-9a-f]{64}", sdk.get("archiveSha256", "")), "Complete pinned .NET SDK archive identity required")
    require(re.fullmatch(r"(?:docker\.io/library/)?python@sha256:[0-9a-f]{64}", prepared.get("pythonBase", "")),
            "Reviewed immutable Python base required")
    if expected_python_image is not None:
        require(prepared["pythonBase"] == expected_python_image,
                "Assembled Python base differs from the reviewed workflow input")
    installed = load(root / "installed-qualification/qualification.json")
    required_checks = {"installed-bootstrap", "payload-hashes", "installed-serve", "human-binding", "delegated-bind-denied",
                       "workspace-grant", "immutable-source", "conditional-source", "run-discovery", "sigterm-shutdown"}
    require(installed.get("image") == image and required_checks <= set(installed.get("checks", [])),
            "Installed transport qualification is missing or used another image")
    bootstrap = prepared.get("bootstrap", {})
    require(isinstance(bootstrap.get("files"), dict) and bootstrap["files"] and
            all(re.fullmatch(r"[0-9a-f]{64}", value) for value in bootstrap["files"].values()) and
            bootstrap.get("devkitVersion") in {version, version + "+" + source_sha},
            "Installed payload/version provenance is missing")
    require(installed.get("payload") == bootstrap.get("files") and installed.get("devkitVersion") == bootstrap.get("devkitVersion"),
            "Installed bootstrap payload differs from the assembled payload")
    fixtures = load(root / "fixture-qualification/qualification.json")
    require(fixtures.get("image") == image and fixtures.get("devkitSha256") == assembly_hash,
            "Execution qualification used another image or devkit assembly")
    runs = fixtures.get("runs", [])
    require(len(runs) == 2 and {run.get("language") for run in runs} == {"python", "dotnet"},
            "Both Python and .NET execution qualifications are required")
    artifacts = []
    execution_ids = set()
    for run in runs:
        receipts = [run.get(name, {}) for name in ("failed", "corrected", "cancelled")]
        require([receipt.get("status") for receipt in receipts] == ["failed", "succeeded", "cancelled"],
                "Failure, corrected success and cancellation must all be observed")
        require(len({receipt.get("runId") for receipt in receipts}) == 3 and
                all(re.fullmatch(r"[0-9a-f]{32}", receipt.get("runId", "")) for receipt in receipts),
                "Distinct actual execution identities required")
        current_ids = {receipt["runId"] for receipt in receipts}
        require(execution_ids.isdisjoint(current_ids),
                "Execution identities must be distinct across both languages")
        execution_ids.update(current_ids)
        require(all(receipt.get("schema") == "honua.gp.workspace-run/v1" and receipt.get("network") == "none" and
                    receipt.get("image") == image and re.fullmatch(r"[0-9a-f]{64}", receipt.get("snapshotDigest", "")) for receipt in receipts),
                "Execution receipts must bind immutable snapshots and the exact image")
        require(receipts[0]["snapshotDigest"] != receipts[1]["snapshotDigest"] and
                not receipts[0].get("artifacts") and not receipts[2].get("artifacts"),
                "Correction must change the snapshot; failure and cancellation cannot publish output")
        artifact = run.get("geoJsonArtifact", {})
        require(artifact.get("sourceSnapshotDigest") == receipts[1]["snapshotDigest"], "Artifact is not bound to corrected execution")
        relative = artifact.get("path", "")
        path = Path(relative)
        require(relative and not path.is_absolute() and ".." not in path.parts, "Artifact path must remain inside its actual run")
        matches = [item for item in receipts[1].get("artifacts", []) if item.get("path") == relative]
        require(len(matches) == 1 and all(matches[0].get(key) == artifact.get(key) for key in ("bytes", "sha256")),
                "Artifact is not the declared successful harness output")
        artifact_root = root / "fixture-qualification/runs" / receipts[1]["runId"] / "output"
        output = artifact_root / path
        require(output.resolve().is_relative_to(root.resolve()) and
                not any(part.is_symlink() for part in (output, *output.parents) if part.is_relative_to(root)),
                "Artifact escaped its retained run")
        require(type(artifact.get("bytes")) is int and 0 < artifact["bytes"] <= 4 * 1024 * 1024 and
                output.stat().st_size == artifact["bytes"] and sha256(output) == artifact.get("sha256"), "Qualified GeoJSON bytes changed")
        geojson = load(output)
        require(geojson.get("type") == "FeatureCollection" and geojson.get("features"), "Nonempty geospatial output required")
        artifacts.append({"language": run["language"], "runId": receipts[1]["runId"], "sha256": artifact["sha256"], "bytes": artifact["bytes"]})
    return {"schema": "honua.gp.devkit-candidate/v1", "sourceRepository": "honua-io/honua-server", "sourceSha": source_sha,
            "version": version, "package": {"file": package.name, "sha256": sha256(package)}, "imageConfigDigest": image,
            "assemblySha256": sha256(root / "assembly.json"), "sdk": sdk, "pythonBase": prepared["pythonBase"],
            "qualification": {"installedTransport": True, "python": True, "dotnet": True, "geoJsonArtifacts": artifacts},
            "scope": "linux-x64 installed local development; server import/map/cloud journeys require separate evidence"}


def published(candidate, reference, manifest):
    require(candidate.get("schema") == "honua.gp.devkit-candidate/v1", "Candidate identity required")
    require(re.fullmatch(r"ghcr\.io/honua-io/honua-gp-workspace@sha256:[0-9a-f]{64}", reference), "Immutable governed registry image required")
    require("sha256:" + hashlib.sha256(manifest).hexdigest() == reference.split("@", 1)[1], "Registry manifest digest changed")
    document = json.loads(manifest)
    require(document.get("config", {}).get("digest") == candidate["imageConfigDigest"], "Registry image is not the locally qualified image")
    # Single-platform Linux x64 image; an index needs its own per-platform qualification.
    require(document.get("mediaType") in {"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"},
            "Exact qualified platform manifest required")
    tag = "gp-devkit-v" + candidate["version"]
    return candidate | {"schema": "honua.gp.devkit-release/v1", "sandboxImage": reference,
                        "package": candidate["package"] | {"url": f"https://github.com/honua-io/honua-server/releases/download/{tag}/{candidate['package']['file']}"}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    check = sub.add_parser("qualify")
    check.add_argument("--root", type=Path, required=True)
    check.add_argument("--package", type=Path, required=True)
    check.add_argument("--source-sha", required=True)
    check.add_argument("--output", type=Path, required=True)
    publish = sub.add_parser("published")
    publish.add_argument("--candidate", type=Path, required=True)
    publish.add_argument("--image", required=True)
    publish.add_argument("--manifest", type=Path, required=True)
    publish.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = qualify(args.root, args.package, args.source_sha) if args.action == "qualify" else published(load(args.candidate), args.image, args.manifest.read_bytes())
    args.output.write_text(json.dumps(result, indent=2) + "\n")
