#!/usr/bin/env python3
"""Publish the exact successful GP candidate. Never pack, rebuild or run user code."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile

import gp_devkit_artifacts as gate

RELEASE_REPOSITORY = "honua-io/honua-release"
SERVER_REPOSITORY = "honua-io/honua-server"
WORKFLOW = ".github/workflows/gp-devkit-candidate.yml"
IMAGE_REPOSITORY = "ghcr.io/honua-io/honua-gp-workspace"


def command(args, **kwargs):
    return subprocess.run(args, check=True, timeout=900, **kwargs)


def api(path):
    result = command(["gh", "api", path], capture_output=True, text=True)
    return json.loads(result.stdout)


def validate_run(run, run_id, attempt, release_sha):
    gate.require(run.get("id") == run_id and run.get("run_attempt") == attempt,
                 "Candidate run/attempt differs from the immutable selection")
    gate.require(run.get("conclusion") == "success" and run.get("status") == "completed"
                 and run.get("event") == "workflow_dispatch" and run.get("head_branch") == "trunk"
                 and run.get("head_sha") == release_sha
                 and run.get("head_repository", {}).get("full_name") == RELEASE_REPOSITORY
                 and run.get("path") in {WORKFLOW, WORKFLOW + "@refs/heads/trunk"},
                 "Only a successful trusted-trunk candidate workflow may publish")
    for field in ("run_started_at", "updated_at"):
        gate.require(isinstance(run.get(field), str), "Candidate execution timestamps are missing")


def validate_artifact(artifact, run, artifact_id, name):
    gate.require(artifact.get("id") == artifact_id and artifact.get("name") == name
                 and artifact.get("expired") is False
                 and artifact.get("workflow_run", {}).get("id") == run["id"]
                 and artifact.get("workflow_run", {}).get("head_sha") == run["head_sha"],
                 "Artifact does not belong to the selected successful candidate")
    gate.require(re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.get("digest", "")),
                 "An immutable Actions archive digest is required")
    gate.require(type(artifact.get("size_in_bytes")) is int and 0 < artifact["size_in_bytes"] <= 12 * 1024**3,
                 "Candidate archive size is outside the publication budget")
    stamp = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
    gate.require(stamp(run["run_started_at"]) <= stamp(artifact["created_at"]) <= stamp(run["updated_at"]),
                 "Artifact was not created during the selected attempt")


def extract_archive(path, destination):
    """Reject links, special files, traversal, duplicates and excessive expansion."""
    destination.mkdir()
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        gate.require(len(entries) <= 512 and sum(entry.file_size for entry in entries) <= 16 * 1024**3,
                     "Candidate archive exceeds extraction limits")
        seen = set()
        for entry in entries:
            relative = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            gate.require(entry.filename and not relative.is_absolute() and ".." not in relative.parts
                         and "\\" not in entry.filename and not any(ord(char) < 32 for char in entry.filename)
                         and relative not in seen and stat.S_IFMT(mode) in {0, stat.S_IFREG, stat.S_IFDIR},
                         "Unsafe candidate archive member")
            seen.add(relative)
            output = destination.joinpath(*relative.parts)
            if entry.is_dir():
                output.mkdir(parents=True, exist_ok=True)
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, output.open("xb") as target:
                copied = 0
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    copied += len(chunk)
                    gate.require(copied <= entry.file_size, "Candidate member grew during extraction")
                    target.write(chunk)
                gate.require(copied == entry.file_size, "Candidate archive member is truncated")


def download_artifact(artifact_id, name, run, root):
    metadata = api(f"repos/{RELEASE_REPOSITORY}/actions/artifacts/{artifact_id}")
    validate_artifact(metadata, run, artifact_id, name)
    archive = root / (name + ".zip")
    with archive.open("xb") as output:
        command(["gh", "api", f"repos/{RELEASE_REPOSITORY}/actions/artifacts/{artifact_id}/zip"], stdout=output)
    gate.require("sha256:" + gate.sha256(archive) == metadata["digest"], "Downloaded Actions archive digest changed")
    destination = root / name
    extract_archive(archive, destination)
    return destination, metadata


def verify_attestation(path, release_sha, run_id, attempt):
    result = command(["gh", "attestation", "verify", str(path), "--repo", RELEASE_REPOSITORY,
                      "--signer-workflow", RELEASE_REPOSITORY + "/" + WORKFLOW,
                      "--source-ref", "refs/heads/trunk", "--source-digest", release_sha,
                      "--deny-self-hosted-runners", "--format", "json"], capture_output=True, text=True)
    invocation = f"https://github.com/{RELEASE_REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}"
    statements = json.loads(result.stdout)
    gate.require(any(item.get("verificationResult", {}).get("statement", {}).get("predicate", {})
                     .get("runDetails", {}).get("metadata", {}).get("invocationId") == invocation for item in statements),
                 "Attestation does not bind the selected candidate invocation")


def require_merged(repository, sha):
    comparison = api(f"repos/{repository}/compare/{sha}...trunk")
    gate.require(comparison.get("status") in {"ahead", "identical"}, "Candidate source is not merged to trusted trunk")


def prepare(options):
    for value in (options.release_sha, options.server_sha):
        gate.require(re.fullmatch(r"[0-9a-f]{40}", value), "Full immutable source commits required")
    gate.require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", options.version), "Exact version required")
    root = options.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    run = api(f"repos/{RELEASE_REPOSITORY}/actions/runs/{options.run_id}/attempts/{options.attempt}")
    validate_run(run, options.run_id, options.attempt, options.release_sha)
    require_merged(RELEASE_REPOSITORY, options.release_sha)
    require_merged(SERVER_REPOSITORY, options.server_sha)
    candidate_root, candidate_metadata = download_artifact(options.candidate_artifact_id,
        "gp-devkit-qualified-candidate", run, root)
    image_root, image_metadata = download_artifact(options.image_artifact_id,
        "gp-devkit-qualified-image", run, root)
    candidate = gate.load(candidate_root / "devkit-candidate.json")
    filename = candidate["package"]["file"]
    gate.require(Path(filename).name == filename and filename.endswith(".nupkg"), "Invalid package filename")
    package = candidate_root / filename
    gate.require(candidate["sourceSha"] == options.server_sha and candidate["version"] == options.version,
                 "Candidate package/source differs from the publication selection")
    gate.require(gate.qualify(candidate_root / "qualification", package, options.server_sha) == candidate,
                 "Retained installed and language fixtures no longer qualify these exact bytes")
    image = image_root / "qualified-image.tar.gz"
    checksum = (image_root / "qualified-image.tar.gz.sha256").read_text().split()
    gate.require(len(checksum) == 2 and re.fullmatch(r"[0-9a-f]{64}", checksum[0])
                 and Path(checksum[1]).name == image.name and gate.sha256(image) == checksum[0],
                 "Qualified image archive bytes changed")
    for path in (package, candidate_root / "devkit-candidate.json", image):
        verify_attestation(path, options.release_sha, options.run_id, options.attempt)
    receipt = {"schema": "honua.gp.devkit-publication-input/v1", "runId": options.run_id,
               "attempt": options.attempt, "releaseSourceSha": options.release_sha,
               "serverSourceSha": options.server_sha, "version": options.version,
               "candidateArtifact": candidate_metadata, "imageArtifact": image_metadata,
               "candidateDirectory": str(candidate_root), "imageArchive": str(image),
               "imageArchiveSha256": checksum[0]}
    (root / "publication-input.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def release_status(tag, source_sha):
    result = subprocess.run(["gh", "api", f"repos/{SERVER_REPOSITORY}/releases/tags/{tag}"],
                            capture_output=True, text=True, timeout=60)
    if result.returncode:
        gate.require("HTTP 404" in result.stderr, "Cannot verify existing release identity")
        return None
    release = json.loads(result.stdout)
    gate.require(release.get("tag_name") == tag and release.get("target_commitish") == source_sha
                 and release.get("draft") is False and release.get("prerelease") is False,
                 "Existing release does not match the exact qualified source")
    return release


def verify_release_asset(asset, path):
    gate.require(type(asset.get("id")) is int and asset["id"] > 0
                 and asset.get("state") == "uploaded" and asset.get("size") == path.stat().st_size,
                 "Existing release asset identity or size differs")
    expected = gate.sha256(path)
    gate.require(asset.get("digest") in {None, "sha256:" + expected}, "Existing release asset digest differs")
    with tempfile.TemporaryDirectory(prefix="honua-release-asset-") as temporary:
        downloaded = Path(temporary) / "asset"
        with downloaded.open("xb") as output:
            command(["gh", "api", f"repos/{SERVER_REPOSITORY}/releases/assets/{asset['id']}",
                     "-H", "Accept: application/octet-stream"], stdout=output)
        gate.require(downloaded.stat().st_size == path.stat().st_size and gate.sha256(downloaded) == expected,
                     "Existing release asset bytes differ")


def ensure_release_assets(tag, source_sha, version, paths):
    expected = {path.name: path for path in paths}
    gate.require(len(expected) == len(paths), "Duplicate expected release asset names")
    release = release_status(tag, source_sha)
    if release is None:
        try:
            command(["gh", "release", "create", tag, "--repo", SERVER_REPOSITORY, "--target", source_sha,
                     "--latest=false", "--title", "GP devkit " + version,
                     "--notes", "Exact qualified installed devkit and sandbox. Anonymous consumption is independently checked; platform pins are unchanged."])
        except subprocess.CalledProcessError:
            # A lost success response cannot authorize a second, different release.
            if release_status(tag, source_sha) is None:
                raise
        release = release_status(tag, source_sha)
        gate.require(release is not None, "Created release cannot be reconciled")
    def assets_by_name(current):
        assets = current.get("assets", [])
        gate.require(isinstance(assets, list) and len(assets) <= len(expected), "Unexpected existing release assets")
        names = [asset.get("name") for asset in assets]
        gate.require(len(set(names)) == len(names) and all(name in expected for name in names),
                     "Duplicate or foreign release asset names")
        return dict(zip(names, assets))
    assets = assets_by_name(release)
    # Verify every retained asset before publishing any missing one.
    for name, asset in assets.items():
        verify_release_asset(asset, expected[name])
    for name, path in expected.items():
        if name in assets:
            continue
        upload_error = None
        try:
            command(["gh", "release", "upload", tag, "--repo", SERVER_REPOSITORY, str(path)])
        except subprocess.CalledProcessError as error:
            upload_error = error
        current = release_status(tag, source_sha)
        gate.require(current is not None, "Release disappeared during upload")
        assets = assets_by_name(current)
        if name not in assets and upload_error:
            raise upload_error
        gate.require(name in assets, "Uploaded release asset cannot be reconciled")
        verify_release_asset(assets[name], path)
    final = release_status(tag, source_sha)
    gate.require(final is not None, "Release disappeared after publication")
    retained = assets_by_name(final)
    gate.require(set(retained) == set(expected), "Published release is incomplete")
    for name, asset in retained.items():
        verify_release_asset(asset, expected[name])


def registry_identity(candidate, image_tag):
    result = subprocess.run(["docker", "buildx", "imagetools", "inspect", image_tag, "--raw"],
                            capture_output=True, timeout=90)
    if result.returncode:
        error = result.stderr.decode("utf-8", errors="replace").lower()
        missing = any(value in error for value in ("manifest unknown", "no such manifest")) or re.fullmatch(
            r"(?:error:\s*)?" + re.escape(image_tag.lower()) + r": not found\s*", error.strip())
        gate.require(missing and not any(value in error for value in ("unauthorized", "denied", "forbidden", "credential")),
                     "Registry identity cannot be checked")
        return None
    descriptor = json.loads(command(["docker", "buildx", "imagetools", "inspect", image_tag,
                                     "--format", "{{json .Manifest}}"], capture_output=True, text=True).stdout)
    gate.require(re.fullmatch(r"sha256:[0-9a-f]{64}", descriptor.get("digest", "")),
                 "Registry returned no immutable manifest digest")
    reference = IMAGE_REPOSITORY + "@" + descriptor["digest"]
    return gate.published(candidate, reference, result.stdout), result.stdout


def publication_preflight(candidate, tag, image_tag):
    release = release_status(tag, candidate["sourceSha"])
    identity = registry_identity(candidate, image_tag)
    # Our own interrupted flow cannot create a release before its image exists.
    # A deleted/unreadable image beside an existing release needs investigation;
    # it must not authorize a fresh registry mutation from an uncertain state.
    gate.require(release is None or identity is not None,
                 "Existing release has no verifiable matching image; investigate before resuming")
    return identity


def publish(options):
    root = options.input.resolve(strict=True)
    receipt = gate.load(root / "publication-input.json")
    # Never use absolute paths from mutable receipt input to widen the selected bundle.
    candidate_root = root / "gp-devkit-qualified-candidate"
    image = root / "gp-devkit-qualified-image/qualified-image.tar.gz"
    candidate = gate.load(candidate_root / "devkit-candidate.json")
    package = candidate_root / Path(candidate["package"]["file"]).name
    gate.require(gate.qualify(candidate_root / "qualification", package, receipt["serverSourceSha"]) == candidate,
                 "Candidate changed between verification and publication")
    gate.require(gate.sha256(image) == receipt["imageArchiveSha256"], "Qualified image archive changed")
    tag = "gp-devkit-v" + candidate["version"]
    image_tag = IMAGE_REPOSITORY + ":" + tag
    identity = publication_preflight(candidate, tag, image_tag)
    if identity is None:
        command(["docker", "load", "--input", str(image)])
        inspected = json.loads(command(["docker", "image", "inspect", candidate["imageConfigDigest"]], capture_output=True, text=True).stdout)
        gate.require(len(inspected) == 1 and inspected[0].get("Id") == candidate["imageConfigDigest"]
                     and inspected[0].get("Os") == "linux" and inspected[0].get("Architecture") == "amd64",
                     "Loaded image is not the qualified Linux x64 config")
        command(["docker", "tag", candidate["imageConfigDigest"], image_tag])
        push_error = None
        try:
            command(["docker", "push", image_tag])
        except subprocess.CalledProcessError as error:
            push_error = error
        identity = registry_identity(candidate, image_tag)
        if identity is None and push_error:
            raise push_error
        gate.require(identity is not None, "Pushed image cannot be reconciled")
    published, raw = identity
    output = root / "published"
    output.mkdir(exist_ok=True)
    copy = output / "candidate"
    def payload(directory):
        entries = list(directory.rglob("*"))
        gate.require(not any(path.is_symlink() or not (path.is_dir() or path.is_file()) for path in entries), "Candidate output contains special files")
        return {str(path.relative_to(directory)): gate.sha256(path) for path in entries if path.is_file()}
    if copy.exists():
        gate.require(not copy.is_symlink() and payload(copy) == payload(candidate_root), "Retained candidate output differs")
    else:
        shutil.copytree(candidate_root, copy)
    def write_exact(path, data):
        if path.exists() or path.is_symlink():
            gate.require(path.is_file() and not path.is_symlink() and path.read_bytes() == data, "Retained publication bytes differ")
        else:
            path.write_bytes(data)
    write_exact(output / "published.json", (json.dumps(published, indent=2) + "\n").encode())
    write_exact(output / "registry-manifest.json", raw)
    write_exact(output / "publication-input.json", (json.dumps(receipt, indent=2) + "\n").encode())
    for path in (package, output / "published.json", output / "registry-manifest.json"):
        write_exact(output / (path.name + ".sha256"), (gate.sha256(path) + "  " + path.name + "\n").encode())
    ensure_release_assets(tag, candidate["sourceSha"], candidate["version"], [
        package, output / (package.name + ".sha256"), output / "published.json",
        output / "published.json.sha256", output / "registry-manifest.json", output / "registry-manifest.json.sha256"])
    return published


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    prepare_args = actions.add_parser("prepare")
    for name in ("run-id", "attempt", "candidate-artifact-id", "image-artifact-id"):
        prepare_args.add_argument("--" + name, type=int, required=True)
    for name in ("release-sha", "server-sha", "version"):
        prepare_args.add_argument("--" + name, required=True)
    prepare_args.add_argument("--output", type=Path, required=True)
    publish_args = actions.add_parser("publish")
    publish_args.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(prepare(args) if args.action == "prepare" else publish(args)))
