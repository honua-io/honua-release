"""Derive SDK server floors from immutable, consumed capability manifests."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from semver import parse

SDK_COMPONENTS = ("honua-sdk-js", "honua-sdk-dotnet", "honua-sdk-python", "geospatial-mcp")
REVISION = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def content_digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return "sha256:" + hashlib.sha256(data.encode()).hexdigest()


def derive(baseline: dict[str, Any]) -> str:
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
            if not floor:
                raise ValueError(f"unqualified: {capability} has no server introduction floor")
            # CalVer aliases need an explicit publisher mapping, never numeric inference.
            if entry.get("versionModel") != "semver":
                raise ValueError(f"unqualified: {capability} has no SemVer server identity mapping")
            if not entry.get("evidence"):
                raise ValueError(f"unqualified: {capability} has no introduction evidence")
            evidence = entry["evidence"]
            if not DIGEST.fullmatch(str(evidence.get("sha256", ""))) or not str(evidence.get("uri", "")).startswith("https://"):
                raise ValueError("introduction evidence requires an HTTPS URI and SHA-256")
            floors.append(parse(floor))
    return str(max(floors))


def check_component(component: dict[str, Any]) -> str:
    baseline = component.get("serverCompatibility", {})
    floor = derive(baseline)
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
    for name in SDK_COMPONENTS:
        try:
            check_component(lock.get("components", {}).get(name, {}))
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            errors.append(f"{name}: {exc}")
    return errors
