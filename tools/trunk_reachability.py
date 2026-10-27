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
    # release#231: every revision behind a release-level lock fact is release-significant too.
    evidence = manifest.get("platformLockEvidence") or {}
    declarations: list[tuple[str, object]] = [
        (f"platformLockEvidence.contentDigests.{name}", declaration)
        for name, declaration in sorted((evidence.get("contentDigests") or {}).items())
    ]
    declarations += [
        (f"platformLockEvidence.fixtures[{index}]", declaration)
        for index, declaration in enumerate(evidence.get("fixtures") or [])
    ]
    if evidence.get("notes") is not None:
        declarations.append(("platformLockEvidence.notes", evidence["notes"]))
    for name, declaration in declarations:
        if not isinstance(declaration, dict):
            continue
        sha = str(declaration.get("revision", ""))
        repository = str(declaration.get("repository", "")).removeprefix("https://github.com/")
        if sha and repository:
            pins.append(Pin(f"{name}.revision", repository, sha))

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


REQUIREMENTS_PIN = "protocolCertification.ledger.requirementsSourceRevision"
RELEASE_REPOSITORY = "honua-io/honua-release"
# The files nightly-certification's stage step commits over reviewed trunk (convergence_rebind
# --nightly-stage); anything else would let a staged commit carry unreviewed release code.
STAGED_REQUIREMENTS_FILES = ("platform-manifest.yaml", "compatibility-matrix.yaml",
                             "certification/protocol-certification-requirements.v1.json")
STAGED_REQUIREMENTS_PREFIX = "certification/sources/"


# GitHub pages a commit's file listing (at most 100 per page) and lists no more than 3000 files.
COMMIT_FILES_PAGE = 100
COMMIT_FILES_LIMIT = 3000


def _commit_files(client: APIClient, pin: Pin, first: object, refuse) -> list[str]:
    """Every file the commit changes, read page by page. A listing that reaches GitHub's 3000-file
    cap cannot be proven complete, so it refuses rather than judging a partial list."""
    files: list[str] = []
    page, response = 1, first
    while True:
        rows = (response or {}).get("files") if isinstance(response, dict) else None
        if not isinstance(rows, list):
            raise refuse(f"its file listing page {page} is not a list")
        files.extend(str((row or {}).get("filename", "")) for row in rows)
        if len(files) >= COMMIT_FILES_LIMIT:
            raise refuse(f"its file listing reaches GitHub's {COMMIT_FILES_LIMIT}-file cap and cannot be "
                         "proven complete")
        if len(rows) < COMMIT_FILES_PAGE:
            return files
        page += 1
        response = client.json(f"repos/{pin.repository}/commits/{pin.sha}"
                               f"?per_page={COMMIT_FILES_PAGE}&page={page}")


def _verify_staged_requirements(pin: Pin, client: APIClient, status: object) -> None:
    """The nightly binds its ledger to the catalog it staged: one data commit over reviewed trunk,
    published at refs/nightly-requirements/<sha> and never merged. It is reachable through that ref
    when its single parent is trunk history and it changes only the staged catalog data."""
    def refuse(reason: str) -> ReachabilityError:
        return ReachabilityError(f"{pin.name}={pin.sha} in {pin.repository} is not reachable from "
                                 f"{pin.trunk!r} (compare status {status!r}) and {reason}")
    try:
        ref = client.json(f"repos/{pin.repository}/git/ref/nightly-requirements/{pin.sha}")
    except ReachabilityError:
        raise refuse(f"has no refs/nightly-requirements/{pin.sha}") from None
    target = (ref or {}).get("object") if isinstance(ref, dict) else None
    if not isinstance(target, dict) or target.get("type") != "commit" or target.get("sha") != pin.sha:
        raise refuse(f"refs/nightly-requirements/{pin.sha} does not name that commit")
    commit = client.json(f"repos/{pin.repository}/commits/{pin.sha}?per_page={COMMIT_FILES_PAGE}&page=1")
    parents = [str((row or {}).get("sha", "")) for row in (commit or {}).get("parents") or []] \
        if isinstance(commit, dict) else []
    if len(parents) != 1:
        raise refuse("the staged requirements commit does not have exactly one parent")
    try:
        verify_pin(Pin(f"{pin.name} parent", pin.repository, parents[0], pin.trunk), client)
    except ReachabilityError as exc:
        raise refuse(f"its parent is not reviewed trunk history: {exc}") from None
    files = _commit_files(client, pin, commit, refuse)
    if not files:
        raise refuse("the staged requirements commit changes nothing")
    unexpected = [name for name in files
                  if name not in STAGED_REQUIREMENTS_FILES and not name.startswith(STAGED_REQUIREMENTS_PREFIX)]
    if unexpected:
        raise refuse("the staged requirements commit changes reviewed release code: " + ", ".join(unexpected))


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
    if pin.name == REQUIREMENTS_PIN and pin.repository == RELEASE_REPOSITORY:
        _verify_staged_requirements(pin, client, status)
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
