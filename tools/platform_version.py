"""The artifact version every imaged component takes from the lock label (ruling R22, #376).

honua-server, honua-console and the Helm chart carry the lock's platform version: the candidate
label `2026.1-rc.N` names artifact version `2026.1.0-rc.N`, and the GA label `2026.1.0` names
`2026.1.0` (#383). SDKs, gRPC and MCP keep their own semver and are never stamped.

A platform version names an image or chart only together with the bytes it names, so it is
stamped and accepted only when the artifact's digest, source revision and per-architecture
digests (an image) or package checksum (a chart) are bound.
"""
from __future__ import annotations

import re
from typing import Any

# `honua-` is the lock id prefix; a manifest platformRelease carries the bare label.
LABEL = re.compile(r"(?:honua-)?(?P<year>[0-9]{4})\.(?P<minor>[0-9]+)(?:\.(?P<patch>[0-9]+))?"
                   r"(?P<rc>-rc\.[0-9]+)?")
IMAGED_COMPONENTS = ("honua-server", "honua-console", "honua-helm")
PUBLISHER = "honua-server"
PRERELEASE = "pre-release"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
REVISION = re.compile(r"[0-9a-f]{40}")


def artifact_version(label: str) -> str:
    """Map a platform label or lock id to the imaged-artifact version (#383).

    `2026.1-rc.3` -> `2026.1.0-rc.3`; `2026.1` and `2026.1.0` -> `2026.1.0`; a patch label
    `2026.1.2-rc.1` keeps its patch. Anything else is not a platform label and is refused.
    """
    match = LABEL.fullmatch(str(label or ""))
    if not match:
        raise ValueError(f"{label!r} is not a platform label YYYY.N[.P][-rc.N]")
    return f"{match['year']}.{match['minor']}.{match['patch'] or 0}{match['rc'] or ''}"


def unbound_identity(component: dict[str, Any]) -> list[str]:
    """The manifest identity fields a platform version needs and this imaged component lacks."""
    chart = str(component.get("artifact") or "").startswith("oci-chart:") and not component.get("image")
    missing = []
    if not DIGEST.fullmatch(str(component.get("digest") or "")):
        missing.append("digest")
    if not REVISION.fullmatch(str(component.get("artifactSourceRevision") or "")):
        missing.append("artifactSourceRevision")
    if chart:
        if not DIGEST.fullmatch(str(component.get("artifactSha256") or "")):
            missing.append("artifactSha256")
    else:
        platforms = component.get("platformDigests")
        if not isinstance(platforms, dict) or not platforms or not all(
                DIGEST.fullmatch(str(digest)) for digest in platforms.values()):
            missing.append("platformDigests")
    return missing


def stamp_platform_version(manifest: dict[str, Any], label: str) -> dict[str, str]:
    """Stamp the label's platform version on every bound imaged component; return what was stamped.

    An imaged component whose identity is not bound keeps no artifact version (a carried-forward
    one is removed), so the generator still refuses it as an unreleased snapshot. honua-server's
    `releaseVersion` follows its artifact version, so the first-release floor names the image
    that ships. SDK, gRPC and MCP rows are never touched.
    """
    version = artifact_version(label)
    stamped = {}
    components = manifest.get("components") or {}
    for name in IMAGED_COMPONENTS:
        component = components.get(name)
        if not isinstance(component, dict):
            continue
        if unbound_identity(component):
            component.pop("artifactVersion", None)
            if name == PUBLISHER:
                component.pop("releaseVersion", None)
            continue
        component["artifactVersion"] = version
        if name == PUBLISHER:
            component["releaseVersion"] = version
        stamped[name] = version
    return stamped
