#!/usr/bin/env python3
"""Enumerate manifest commit pins and prove that each is reachable from repository trunk."""
from __future__ import annotations

import json
import subprocess
import urllib.parse
from dataclasses import dataclass
from typing import Protocol


class APIClient(Protocol):
    def json(self, path: str) -> object: ...


class GhClient:
    """Read-only GitHub API client using the authenticated gh CLI."""

    def json(self, path: str) -> object:
        try:
            result = subprocess.run(
                ["gh", "api", path], capture_output=True, text=True, check=True
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            detail = getattr(exc, "stderr", "") or str(exc)
            raise ReachabilityError(f"GitHub API request failed for {path}: {detail.strip()}") from exc
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ReachabilityError(f"GitHub API returned invalid JSON for {path}") from exc


class ReachabilityError(ValueError):
    """A manifest pin cannot be proven reachable from repository trunk."""


@dataclass(frozen=True)
class Pin:
    name: str
    repository: str
    sha: str
    trunk: str = "trunk"


def manifest_pins(manifest: dict) -> list[Pin]:
    """Return every release-significant commit pin, including intentional duplicate bindings."""
    pins: list[Pin] = []
    for name, component in (manifest.get("components") or {}).items():
        sha = str((component or {}).get("sha", ""))
        if sha:
            pins.append(Pin(f"components.{name}.sha", f"honua-io/{name}", sha))
    for name, artifact in (manifest.get("clientArtifacts") or {}).items():
        artifact = artifact or {}
        sha = str(artifact.get("sourceSha", ""))
        if sha:
            pins.append(
                Pin(f"clientArtifacts.{name}.sourceSha", str(artifact.get("repository", "")), sha)
            )
    for name, source in (manifest.get("evidenceSources") or {}).items():
        source = source or {}
        sha = str(source.get("producerSha", ""))
        if sha:
            pins.append(
                Pin(
                    f"evidenceSources.{name}.producerSha",
                    str(source.get("repository", "")),
                    sha,
                    str(source.get("trustedBranch", "trunk")),
                )
            )

    certification = manifest.get("protocolCertification") or {}
    server_sha = str(certification.get("serverCertificationProducerSha", ""))
    if server_sha:
        pins.append(
            Pin(
                "protocolCertification.serverCertificationProducerSha",
                "honua-io/honua-server",
                server_sha,
            )
        )
    ledger = certification.get("ledger") or {}
    requirements_sha = str(ledger.get("requirementsSourceRevision", ""))
    if requirements_sha and requirements_sha != "pending":
        pins.append(
            Pin(
                "protocolCertification.ledger.requirementsSourceRevision",
                "honua-io/honua-release",
                requirements_sha,
            )
        )
    ledger_commit = str(ledger.get("commit", ""))
    if ledger_commit and ledger_commit != "pending":
        pins.append(
            Pin(
                "protocolCertification.ledger.commit",
                str(ledger.get("repository", "honua-io/honua-evidence")),
                ledger_commit,
            )
        )
    return pins


def _branch_heads(client: APIClient, pin: Pin) -> list[str]:
    path = (
        f"repos/{pin.repository}/commits/{urllib.parse.quote(pin.sha, safe='')}"
        "/branches-where-head"
    )
    try:
        response = client.json(path)
    except ReachabilityError:
        return []
    if not isinstance(response, list):
        return []
    return sorted(
        str(branch.get("name"))
        for branch in response
        if isinstance(branch, dict) and branch.get("name")
    )


def verify_pin(pin: Pin, client: APIClient) -> None:
    path = (
        f"repos/{pin.repository}/compare/{urllib.parse.quote(pin.trunk, safe='')}..."
        f"{urllib.parse.quote(pin.sha, safe='')}"
    )
    response = client.json(path)
    if not isinstance(response, dict):
        raise ReachabilityError(
            f"{pin.name}={pin.sha} in {pin.repository}: GitHub compare returned a non-object response"
        )
    status = response.get("status")
    if status in {"identical", "behind"}:
        return
    branches = _branch_heads(client, pin)
    origin = f"; off-trunk branch origin: {', '.join(branches)}" if branches else ""
    raise ReachabilityError(
        f"{pin.name}={pin.sha} in {pin.repository} is not reachable from {pin.trunk!r} "
        f"(compare status {status!r}){origin}"
    )


def verify_manifest_pins(manifest: dict, client: APIClient) -> int:
    pins = manifest_pins(manifest)
    for pin in pins:
        try:
            verify_pin(pin, client)
        except ValueError as exc:
            message = str(exc)
            if not message.startswith(f"{pin.name}="):
                message = f"{pin.name}={pin.sha} in {pin.repository}: {message}"
            raise ReachabilityError(message) from exc
    return len(pins)
