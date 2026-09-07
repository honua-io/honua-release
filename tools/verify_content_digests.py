#!/usr/bin/env python3
"""Verify the platform manifest's declared lock content digests against their pinned bytes.

The lock carries each content digest as a bare `sha256:...` string, so nothing in the lock can
prove where those bytes came from. The manifest declares the repository, immutable revision and
path behind every digest; this reads that file at that revision and recomputes the digest.

Online it uses the pinned GitHub contents API through the existing `gh` authentication; offline
`--source-root ROOT` reads git objects from `ROOT/OWNER/REPO`, so a dirty working tree or a newer
branch head cannot stand in for the pinned commit.
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import yaml

from release_facts import CONTENT_DIGEST_FACTS, content_digest, content_digest_conflicts
from verify_sdk_baseline_sources import SourceReader


def declared_digests(manifest: dict) -> dict[str, tuple[str, dict[str, str]]]:
    evidence = manifest.get("platformLockEvidence")
    if evidence is not None and not isinstance(evidence, dict):
        raise ValueError("platformLockEvidence must be a mapping")
    declarations = (evidence or {}).get("contentDigests") or {}
    if not isinstance(declarations, dict):
        raise ValueError("platformLockEvidence.contentDigests must be a mapping")
    known = {name for name, _ in CONTENT_DIGEST_FACTS}
    unknown = sorted(set(declarations) - known)
    if unknown:
        raise ValueError(f"the lock declares no such content digest(s): {', '.join(unknown)}")
    return {name: content_digest(value) for name, value in sorted(declarations.items())}


def verify(manifest: dict, reader: SourceReader) -> list[str]:
    conflicts = content_digest_conflicts(manifest)
    if conflicts:
        # Matching its own source bytes is not enough: the same standard must not also be
        # identified, differently, by a component artifact in the same manifest.
        raise ValueError("; ".join(message for _, message in conflicts))
    verified = []
    for name, (digest, source) in declared_digests(manifest).items():
        raw = reader(source["repository"], source["revision"], source["path"])
        actual = "sha256:" + hashlib.sha256(raw).hexdigest()
        if actual != digest:
            raise ValueError(
                f"contentDigests.{name}: {source['path']} at {source['revision']} hashes to {actual}, "
                f"not the declared {digest}"
            )
        verified.append(f"{name} ({source['path']}@{source['revision'][:12]})")
    return verified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--source-root", type=Path,
                        help="offline git repositories at ROOT/OWNER/REPO; commits must exist locally")
    args = parser.parse_args(argv)
    try:
        manifest = yaml.safe_load(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("expected a platform manifest mapping")
        verified = verify(manifest, SourceReader(args.source_root))
    except (OSError, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError) as exc:
        print(f"BLOCKED: {exc}")
        return 1
    if not verified:
        # Undeclared digests are the lock generator's refusal to report, not a verification pass.
        print("PASS: the manifest declares no lock content digest yet")
        return 0
    print("PASS: pinned content digest bytes verified for " + ", ".join(verified))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
