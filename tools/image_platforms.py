"""Exact Linux image identities, checked against immutable registry manifests."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from typing import Any

DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
PLATFORM_DIGEST_ARCHITECTURES = {"amd64", "arm64"}


def image_platform_digests(declared: Any, *, index_digest: Any = None, architectures: Any = None) -> dict[str, str]:
    """Exact amd64/arm64 image digests the rollback certifier can dereference.

    ``platformDigests/amd64`` is the serving identity. A missing, malformed, or
    non-architecture entry must not be copied into the lock, and neither may a
    multi-arch index digest repeated as one of its own children.
    """
    if not isinstance(declared, dict) or not declared:
        raise ValueError("platform-specific image digests are not declared")
    accepted: dict[str, str] = {}
    errors: list[str] = []
    for architecture, digest in declared.items():
        exact = isinstance(digest, str) and DIGEST_RE.fullmatch(digest) is not None
        if architecture not in PLATFORM_DIGEST_ARCHITECTURES or not exact:
            errors.append(f"{architecture}: image requires an exact platform-specific digest")
            continue
        accepted[architecture] = digest
    if "amd64" not in accepted and not any(item.startswith("amd64:") for item in errors):
        errors.append("amd64: image requires an exact platform-specific digest")
    if len(accepted) > 1 and isinstance(index_digest, str) and index_digest in accepted.values():
        errors.append("platform digest repeats the multi-arch index digest")
    if architectures not in (None, []) and (
        not isinstance(architectures, list) or set(architectures) != set(accepted)
    ):
        errors.append("architectures do not match platformDigests")
    if errors:
        raise ValueError("; ".join(errors))
    return dict(accepted)


def registry_image_platform_digests(coordinate: str, digest: str) -> dict[str, str]:
    """Resolve Linux children from the immutable registry index, never from a tag."""
    if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
        raise ValueError("image requires an exact registry digest")
    if not isinstance(coordinate, str) or not coordinate or "@" in coordinate:
        raise ValueError("image requires a registry coordinate")
    command = ["docker", "buildx", "imagetools", "inspect", f"{coordinate}@{digest}", "--raw"]
    for delay in (0, 10, 30, 60, 120):
        if delay:
            time.sleep(delay)
        try:
            result = subprocess.run(command, capture_output=True, timeout=15, check=False)
        except subprocess.TimeoutExpired:
            continue
        if result.returncode == 0:
            break
        error = result.stderr.decode(errors="replace")
        if not any(message in error.lower() for message in (
            "could not resolve host", "no such host", "connection reset by peer",
            "tls handshake timeout", "i/o timeout", "error connecting", "context deadline exceeded",
        )):
            raise ValueError(f"registry image inspection failed: {error.strip()}")
    else:
        raise ValueError("registry image inspection failed after network retries")
    raw = result.stdout
    if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("registry index bytes do not match the pinned digest")
    index = json.loads(raw)
    if not isinstance(index, dict) or not isinstance(index.get("manifests"), list):
        raise ValueError("registry image must declare a multi-architecture index")
    platforms: dict[str, str] = {}
    for child in index.get("manifests", []):
        platform = child.get("platform") or {}
        architecture = platform.get("architecture")
        if platform.get("os") != "linux" or architecture not in PLATFORM_DIGEST_ARCHITECTURES:
            continue  # BuildKit attestation manifests are unknown/unknown, not runnable images.
        if architecture in platforms:
            raise ValueError(f"ambiguous registry linux/{architecture} image")
        platforms[architecture] = child.get("digest")
    return image_platform_digests(platforms, index_digest=digest)


def verify_image_platform_digests(artifact: dict, inspector=registry_image_platform_digests) -> None:
    declared = image_platform_digests(artifact.get("platformDigests"),
                                      index_digest=artifact.get("digest"),
                                      architectures=artifact.get("architectures"))
    observed = inspector(artifact.get("coordinate"), artifact.get("digest"))
    if declared != observed:
        raise ValueError("platformDigests do not match registry Linux architecture identities")
