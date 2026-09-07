"""Verify locked SDK baseline inputs against files at their immutable source revisions."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
from urllib.parse import quote

import yaml

import server_publication_history
from sdk_baselines import PUBLISHER, REVISION, SDK_COMPONENTS, content_digest, findings

ROOT = Path(__file__).resolve().parents[1]

REPOSITORY = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")


def source_identity(repository: str, revision: str, path: str) -> tuple[str, str]:
    match = REPOSITORY.fullmatch(repository)
    if not match or any(part in {".", ".."} for part in match.groups()):
        raise ValueError("source repository must identify a GitHub owner/repository")
    if not REVISION.fullmatch(revision):
        raise ValueError("source revision must be an immutable git SHA")
    if (not path or PurePosixPath(path).is_absolute()
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or "\\" in path or any(ord(char) < 32 for char in path)):
        raise ValueError("source path must be a relative repository file path")
    return match.group(1), match.group(2)


class SourceReader:
    """Read commit objects, never working-tree files; or use GitHub's pinned contents API."""

    def __init__(self, source_root: Path | None = None):
        self.source_root = source_root
        self.cache: dict[tuple[str, str, str], bytes] = {}

    def __call__(self, repository: str, revision: str, path: str) -> bytes:
        owner, repo = source_identity(repository, revision, path)
        key = repository, revision, path
        if key in self.cache:
            return self.cache[key]
        if self.source_root is not None:
            command = ["git", "-C", str(self.source_root / owner / repo), "show", f"{revision}:{path}"]
        else:
            command = ["gh", "api", "--hostname", "github.com", "-H", "Accept: application/vnd.github.raw+json",
                       f"repos/{owner}/{repo}/contents/{quote(path, safe='/')}?ref={revision}"]
        # Network faults are retried without authentication changes. A 403 also waits;
        # it never triggers an authentication/device flow. Diagnostics omit response bodies.
        for attempt, delay in enumerate((10, 30, 60, 120, 0)):
            try:
                result = subprocess.run(command, capture_output=True, timeout=15)
            except subprocess.TimeoutExpired:
                retry = True
            else:
                if result.returncode == 0:
                    self.cache[key] = result.stdout
                    return result.stdout
                error = result.stderr.decode("utf-8", errors="replace").lower()
                retry = any(marker in error for marker in (
                    "error connecting to api.github.com", "could not resolve host", "connection reset by peer",
                    "timeout", "timed out", "temporary failure", "http 403", "http 429", "http 50",
                ))
            if self.source_root is not None or not retry or attempt == 4:
                raise ValueError(f"cannot read pinned source {repository}@{revision}:{path}")
            time.sleep(delay)
        raise AssertionError("unreachable")


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key in source manifest: {key}")
        result[key] = value
    return result


def verify_publication_history(lock: dict, root: Path) -> None:
    """A locked first-release pin must match the committed receipt's bytes and content."""
    history = ((lock.get("components") or {}).get(PUBLISHER) or {}).get("publicationHistory")
    if not isinstance(history, dict):
        return
    relative = str(history.get("path", ""))
    if (not relative or PurePosixPath(relative).is_absolute()
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise ValueError(f"{PUBLISHER}: publication-history path must be a relative repository path")
    path = root / relative
    if not path.is_file():
        raise ValueError(f"{PUBLISHER}: publication-history receipt is missing at {relative}")
    if "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != history.get("sha256"):
        raise ValueError(f"{PUBLISHER}: publication-history receipt bytes disagree with the lock pin")
    server_publication_history.verify(json.loads(path.read_text(encoding="utf-8")))


def verify_sources(lock: dict, reader: SourceReader, root: Path = ROOT) -> list[str]:
    errors = findings(lock)
    if errors:
        raise ValueError("; ".join(errors))
    verify_publication_history(lock, root)
    verified = []
    for name in SDK_COMPONENTS:
        component = lock["components"][name]
        baseline = component["serverCompatibility"]
        for manifest in baseline["manifests"]:
            source = manifest["source"]
            raw = reader(source["repository"], source["revision"], source["path"])
            content = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
            if content_digest(content) != manifest["sha256"] or content != manifest["content"]:
                raise ValueError(f"{name}: pinned manifest source disagrees with lock: {source['path']}")
        for declaration in baseline["declarations"]:
            raw = reader(component["source"]["repository"], declaration["revision"], declaration["path"])
            if "sha256:" + hashlib.sha256(raw).hexdigest() != declaration["sha256"]:
                raise ValueError(f"{name}: pinned declaration byte digest disagrees with lock: {declaration['path']}")
        verified.append(name)
    return verified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lock", type=Path)
    parser.add_argument("--source-root", type=Path, help="offline git repositories at ROOT/OWNER/REPO; commits must exist locally")
    args = parser.parse_args(argv)
    try:
        lock = yaml.safe_load(args.lock.read_text(encoding="utf-8"))
        if not isinstance(lock, dict) or lock.get("lockVersion") != "platform-lock.v1":
            raise ValueError("expected a platform-lock.v1 mapping")
        verified = verify_sources(lock, SourceReader(args.source_root))
        print("PASS: pinned baseline source bytes verified for " + ", ".join(verified))
        return 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError) as exc:
        print(f"BLOCKED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
