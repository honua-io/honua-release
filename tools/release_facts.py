"""Declaration and immutability rules for the release-level facts of one atomic candidate.

Release #231 requires that a signed platform lock be one atomic candidate identity: every fact
it carries must come from the reviewed frozen inputs and must name immutable bytes. The
component half of that identity is already bound to `platform-manifest.yaml`. The release-level
half - content digests, fixture revisions, SBOM/provenance references and the release notes -
had no declaration path at all: the generator could never emit those fields, and candidate
binding never compared them, so a manufactured lock could introduce release facts that no
frozen input declares and that no reviewer ever saw.

This module is the single definition of both rules. The generator consumes declarations through
it, the semantic validator enforces immutability with it, and candidate binding compares the
lock against the declarations it produces, so the three cannot drift.
"""
from __future__ import annotations

import re
from typing import Any

REVISION = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
REPOSITORY = re.compile(r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# A reference whose last path element is one of these names moves under the release; it can
# never identify the bytes a customer verifies.
FLOATING_TAGS = frozenset({
    "latest", "nightly", "nightly-aot", "edge", "dev", "main", "master", "trunk", "stable",
})
# The three content digests the lock schema requires, with the refusal wording the release
# worklist has always used for them.
CONTENT_DIGEST_FACTS = (
    ("geospatialMcp", "certified content digest"),
    ("catalog", "catalog digest"),
    ("okf", "OKF digest"),
)
# repository@revision:path#sha256:digest - one immutable file, named by its bytes.
SOURCE_REFERENCE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}:[^\s#]+#sha256:[0-9a-f]{64}$"
)
_MUTABLE_GIT_PATH = re.compile(r"/(?:blob|tree|raw|archive)/(?!(?:[0-9a-f]{40})(?:/|\.|$))")


def _mapping(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be a mapping")
    return value


def source_reference(value: Any, *, path_required: bool = True) -> dict[str, str]:
    """Validate a repository/revision[/path] declaration and return exactly those keys."""
    declaration = _mapping(value, "source reference")
    unknown = sorted(set(declaration) - {"repository", "revision", "path", "sha256"})
    if unknown:
        raise ValueError(f"unknown source reference field(s): {', '.join(unknown)}")
    repository = str(declaration.get("repository", ""))
    if not REPOSITORY.fullmatch(repository):
        raise ValueError("source repository must be an https://github.com/OWNER/REPO URL")
    revision = str(declaration.get("revision", ""))
    if not REVISION.fullmatch(revision):
        raise ValueError("source revision must be an immutable 40-character git revision")
    reference = {"repository": repository, "revision": revision}
    path = declaration.get("path")
    if path is None:
        if path_required:
            raise ValueError("source path is required")
        return reference
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        raise ValueError("source path must be a relative repository file path")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("source path must be a relative repository file path")
    reference["path"] = path
    return reference


def immutable_uri(uri: Any) -> str:
    """Refuse any evidence URI that can point at different bytes tomorrow."""
    if not isinstance(uri, str) or not uri.startswith(("https://", "oci://")):
        raise ValueError("uri must be an https:// or oci:// reference")
    if uri.startswith("oci://") and not re.search(r"@sha256:[0-9a-f]{64}$", uri):
        raise ValueError("oci reference must be pinned to an immutable @sha256 digest")
    tail = uri.rsplit("/", 1)[-1]
    if tail.rsplit(":", 1)[-1].lower() in FLOATING_TAGS or tail.lower() in FLOATING_TAGS:
        raise ValueError("floating tag references are forbidden")
    if _MUTABLE_GIT_PATH.search(uri) or "/refs/heads/" in uri:
        raise ValueError("git references must name a 40-character revision, not a branch")
    return uri


def content_digest(value: Any) -> tuple[str, dict[str, str]]:
    """A content digest is the byte SHA-256 of one file at an immutable source revision."""
    declaration = _mapping(value, "content digest declaration")
    reference = source_reference(declaration)
    digest = str(declaration.get("sha256", ""))
    if not DIGEST.fullmatch(digest):
        raise ValueError("content digest must be a sha256:<64 hex> byte digest of the pinned file")
    return digest, reference


def fixture_reference(value: Any) -> dict[str, str]:
    """Fixture repositories are pinned by revision; a path may narrow them to one directory."""
    return source_reference(value, path_required=False)


def evidence_reference(value: Any) -> dict[str, str]:
    """SBOM/provenance rows name one component, one immutable URI and the bytes' digest."""
    declaration = _mapping(value, "evidence reference")
    unknown = sorted(set(declaration) - {"component", "uri", "sha256"})
    if unknown:
        raise ValueError(f"unknown evidence reference field(s): {', '.join(unknown)}")
    component = declaration.get("component")
    if not isinstance(component, str) or not component:
        raise ValueError("evidence reference must name its component")
    digest = str(declaration.get("sha256", ""))
    if not DIGEST.fullmatch(digest):
        raise ValueError("evidence reference must carry a sha256:<64 hex> digest")
    return {"component": component, "uri": immutable_uri(declaration.get("uri")), "sha256": digest}


def notes_reference(value: Any) -> str:
    """Release notes enter the lock as one immutable reference, never as prose or a live page."""
    if isinstance(value, dict):
        reference = source_reference(value)
        digest = str(value.get("sha256", ""))
        if not DIGEST.fullmatch(digest):
            raise ValueError("release notes must be pinned by the byte sha256 of the referenced file")
        return f"{reference['repository']}@{reference['revision']}:{reference['path']}#{digest}"
    if isinstance(value, str) and value:
        if SOURCE_REFERENCE.fullmatch(value):
            return value
        uri, _, digest = value.partition("#")
        if DIGEST.fullmatch(digest):
            return f"{immutable_uri(uri)}#{digest}"
        raise ValueError(
            "release notes reference must be repository@revision:path#sha256:<digest> or uri#sha256:<digest>"
        )
    raise ValueError("release notes declaration must be a mapping or an immutable reference string")
