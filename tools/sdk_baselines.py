"""Derive SDK server floors from immutable, consumed capability manifests."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from semver import parse

SDK_COMPONENTS = ("honua-sdk-js", "honua-sdk-dotnet", "honua-sdk-python", "geospatial-mcp")
PUBLISHER = "honua-server"
REVISION = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
# A capability that exists in the publisher's first release has no earlier server to name.
# It resolves to that release only against an immutable receipt proving no prior publication.
FIRST_RELEASE = "first-release"


def content_digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(data.encode()).hexdigest()


def _released(version: Any) -> str | None:
    return str(version) if version and version != "pre-release" else None


def publisher_artifact_version(publisher: dict[str, Any]) -> str | None:
    """The released server version actually pinned, read from a lock entry or a manifest row.

    A floor is only worth anything if the server it names is the server that ships, so the
    first-release version has to be checked against the artifact the release pins, not taken
    on the manifest's word. `pre-release` is a source snapshot, not a released version.
    """
    for artifact in publisher.get("artifacts") or []:
        if isinstance(artifact, dict) and artifact.get("version"):
            return _released(artifact["version"])
    return _released(publisher.get("artifactVersion") or publisher.get("version"))


def release_context(lock: dict[str, Any]) -> dict[str, Any]:
    """First-release facts, read only from the lock; never inferred from an SDK or a label."""
    publisher = (lock.get("components") or {}).get(PUBLISHER) or {}
    return {
        "firstReleaseVersion": publisher.get("releaseVersion"),
        "publisherArtifactVersion": publisher_artifact_version(publisher),
        "publicationHistory": publisher.get("publicationHistory"),
    }


def first_release_floor(capability: str, entry: dict[str, Any], context: dict[str, Any]) -> str:
    """Resolve a first-release capability, or say exactly which immutable fact is missing.

    The publisher has never released a server, so no capability can name an earlier one.
    The floor is the first release itself, and only a receipt enumerating the publisher's
    complete (empty) tag/release history can establish that. A missing receipt, a receipt
    the manifest does not cite, or an unnamed first release all stay unqualified.
    """
    receipt = context.get("publicationHistory")
    if not isinstance(receipt, dict):
        raise ValueError(f"unqualified: {capability} claims the first-release model but the lock "
                         f"pins no {PUBLISHER} publication-history receipt")
    if (not receipt.get("path") or not DIGEST.fullmatch(str(receipt.get("sha256", "")))
            or not str(receipt.get("uri", "")).startswith("https://")):
        raise ValueError("publication-history pin requires a repository path, HTTPS URI and SHA-256")
    evidence = entry["evidence"]
    if (evidence.get("uri"), evidence.get("sha256")) != (receipt["uri"], receipt["sha256"]):
        raise ValueError(f"unqualified: {capability} introduction evidence does not cite the locked "
                         f"{PUBLISHER} publication-history receipt")
    version = context.get("firstReleaseVersion")
    if not version:
        raise ValueError(f"unqualified: {capability} resolves to the first {PUBLISHER} release, "
                         "which this lock does not name")
    # The publisher's release version is a claim; the locked artifact is the thing that ships.
    # A floor derived from the first is only real if it is the version of the second.
    shipped = context.get("publisherArtifactVersion")
    if not shipped:
        raise ValueError(f"unqualified: {capability} resolves to the first {PUBLISHER} release, but "
                         f"the lock pins no released {PUBLISHER} artifact version to bind it to")
    if shipped != str(version):
        raise ValueError(f"unqualified: the lock names first {PUBLISHER} release {version}, but the "
                         f"locked {PUBLISHER} artifact is {shipped}")
    declared = entry.get("minimumServerVersion")
    if declared is not None and declared != version:
        raise ValueError(f"unqualified: {capability} declares {declared!r}; the first "
                         f"{PUBLISHER} release is {version}")
    return str(version)


def derive(baseline: dict[str, Any], context: dict[str, Any] | None = None) -> str:
    """Maximum introduction floor over every required capability; never infer a floor."""
    manifests = baseline.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        raise ValueError("unqualified: no consumed protocol/capability manifest is pinned")
    floors = []
    for manifest in manifests:
        source = manifest.get("source", {})
        if not REVISION.fullmatch(str(source.get("revision", ""))):
            raise ValueError("manifest source must pin an immutable git revision")
        if not re.fullmatch(r"https://github\.com/[^/]+/[^/]+", str(source.get("repository", ""))):
            raise ValueError("manifest source must identify its repository")
        if not source.get("path"):
            raise ValueError("manifest source path is required")
        content = manifest.get("content")
        if not isinstance(content, dict) or manifest.get("sha256") != content_digest(content):
            raise ValueError("manifest canonical content digest disagrees with its lock pin")
        required = manifest.get("requiredCapabilities")
        if not isinstance(required, list) or not required or len(set(required)) != len(required):
            raise ValueError("requiredCapabilities must be a nonempty unique list")
        capabilities = content.get("capabilities", {})
        for capability in required:
            entry = capabilities.get(capability, {})
            floor = entry.get("minimumServerVersion")
            first_release = entry.get("introductionModel") == FIRST_RELEASE
            if not floor and not first_release:
                raise ValueError(f"unqualified: {capability} has no server introduction floor")
            # CalVer aliases need an explicit publisher mapping, never numeric inference.
            if entry.get("versionModel") != "semver":
                raise ValueError(f"unqualified: {capability} has no SemVer server identity mapping")
            if not entry.get("evidence"):
                raise ValueError(f"unqualified: {capability} has no introduction evidence")
            evidence = entry["evidence"]
            if not DIGEST.fullmatch(str(evidence.get("sha256", ""))) or not str(evidence.get("uri", "")).startswith("https://"):
                raise ValueError("introduction evidence requires an HTTPS URI and SHA-256")
            if first_release:
                floor = first_release_floor(capability, entry, context or {})
            floors.append(parse(floor))
    return str(max(floors))


def check_component(component: dict[str, Any], context: dict[str, Any] | None = None) -> str:
    baseline = component.get("serverCompatibility", {})
    floor = derive(baseline, context)
    if baseline.get("minimumServerVersion") != floor:
        raise ValueError(f"lock minimumServerVersion must equal derived floor {floor}")
    declarations = baseline.get("declarations")
    if not isinstance(declarations, list) or not declarations:
        raise ValueError("unqualified: no SDK baseline declaration is pinned")
    artifacts = component.get("artifacts", [])
    revisions = ({item.get("sourceRevision") for item in artifacts} if artifacts
                 else {component.get("source", {}).get("revision")})
    declared_revisions = set()
    for declaration in declarations:
        if declaration.get("revision") not in revisions or not REVISION.fullmatch(str(declaration.get("revision", ""))):
            raise ValueError("SDK declaration revision is not bound to component/artifact source")
        if not declaration.get("path") or not DIGEST.fullmatch(str(declaration.get("sha256", ""))):
            raise ValueError("SDK declaration needs a path and byte SHA-256")
        if declaration.get("minimumServerVersion") != floor:
            raise ValueError(f"declared baseline {declaration.get('minimumServerVersion')!r} disagrees with lock floor {floor}")
        declared_revisions.add(declaration["revision"])
    if revisions - declared_revisions:
        raise ValueError("SDK declarations must cover every artifact source revision")
    return floor


def findings(lock: dict[str, Any]) -> list[str]:
    errors = []
    context = release_context(lock)
    for name in SDK_COMPONENTS:
        try:
            check_component(lock.get("components", {}).get(name, {}), context)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            errors.append(f"{name}: {exc}")
    return errors
