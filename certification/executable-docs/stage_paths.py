"""Stage declared repository subdirectories into the executable-docs reader workspace."""
from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class StageError(RuntimeError):
    """Raised when a declared stagePaths tree is unsafe, truncated, or empty."""


@dataclass(frozen=True)
class StagedFile:
    path: str
    content: bytes
    mode: str


def safe_stage_path(raw: str) -> str:
    stripped = raw.strip()
    if not stripped or stripped.startswith("/") or "\\" in stripped or "\x00" in stripped:
        raise StageError(f"unsafe stage path: {raw!r}")
    clean = stripped.rstrip("/")
    parts = clean.split("/")
    if not clean or any(part in {"", ".", ".."} for part in parts):
        raise StageError(f"unsafe stage path: {raw!r}")
    return clean


def validated_tree_blobs(entries: list[dict[str, Any]], label: str) -> list[tuple[str, str]]:
    blobs: list[tuple[str, str]] = []
    for entry in entries:
        kind, mode, path = entry.get("type"), entry.get("mode"), str(entry.get("path", ""))
        if kind == "tree" and mode == "040000":
            continue
        if kind == "blob" and mode in {"100644", "100755"}:
            blobs.append((safe_stage_path(path), "0755" if mode == "100755" else "0644"))
            continue
        raise StageError(f"unsupported staged entry in {label}: {path!r} (type={kind!r}, mode={mode!r})")
    return blobs


def stage_tree(doc: dict[str, Any], revision: str, resolver: Any) -> list[StagedFile]:
    raw_paths = doc.get("stagePaths")
    if raw_paths is None:
        return []
    if not isinstance(raw_paths, list):
        raise StageError(f"stagePaths must be a list, got {type(raw_paths).__name__}")
    repo = doc["repo"]
    staged: list[StagedFile] = []
    for raw in raw_paths:
        prefix = safe_stage_path(str(raw))
        label = f"{repo}@{revision[:12]}:{prefix}"
        ref = urllib.parse.quote(f"{revision}:{prefix}", safe="")
        tree = json.loads(
            resolver.fetch(
                f"https://api.github.com/repos/{repo}/git/trees/{ref}?recursive=1",
                token=resolver.token,
            )
        )
        if tree.get("truncated"):
            raise StageError(f"staged tree {label} was truncated")
        blobs = validated_tree_blobs(tree.get("tree") or [], label)
        if not blobs:
            raise StageError(f"staged path {label} matched no files")
        for sub_path, file_mode in blobs:
            rel = f"{prefix}/{sub_path}"
            encoded = urllib.parse.quote(rel, safe="/")
            url = f"https://raw.githubusercontent.com/{repo}/{revision}/{encoded}"
            staged.append(StagedFile(rel, resolver.fetch(url, token=resolver.token), file_mode))
    return staged


def write_staged_files(session: Any, runtime: str, files: list[StagedFile]) -> None:
    base = Path(session.cwd)
    for item in files:
        target = base / item.path
        session.put_bytes(runtime, target, item.content)
        if item.mode == "0755":
            session.chmod(runtime, target, item.mode)
