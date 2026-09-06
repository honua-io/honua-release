#!/usr/bin/env python3
"""Bind a complete candidate lock to train inputs and derive its customer records.

This does not manufacture missing registry facts or assert certification. The train
attests the canonical lock only after this command succeeds. Promotion verifies that
attestation and checks these deterministic derivatives before publishing them.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

import yaml

from generate_compatibility_table import render
from generate_platform_lock import generate
from release_inspect import canonical_digest
from validate_platform_lock import load_lock, validate


def canonical_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _declared(expected, actual, path: str) -> None:
    """Match every declared input fact; absent facts must be supplied by the lock."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise ValueError(f"{path}: expected a mapping")
        for key, value in expected.items():
            _declared(value, actual.get(key), f"{path}.{key}")
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            raise ValueError(f"{path}: artifact denominator differs from manifest")
        for index, value in enumerate(expected):
            _declared(value, actual[index], f"{path}[{index}]")
    elif expected is not None and actual != expected:
        raise ValueError(f"{path}: lock differs from frozen input ({actual!r} != {expected!r})")


def bind(lock: dict, manifest: Path, matrix: Path, label: str) -> None:
    errors = validate(lock).errors
    if errors:
        raise ValueError("; ".join(errors))
    if lock["platform"]["status"] != "rc":
        raise ValueError("candidate lock must have rc status")
    if lock["platform"]["id"] != f"honua-{label}":
        raise ValueError("platform label differs from atomic candidate identity")
    draft = generate(manifest, matrix)
    for refusal in draft.unresolved:
        if "published package coordinate is pending" in refusal:
            raise ValueError(refusal)
    _declared(draft.lock["sourceInputs"], lock["sourceInputs"], "sourceInputs")
    _declared(draft.lock["platform"], lock["platform"], "platform")
    if set(lock["components"]) != set(draft.lock["components"]):
        raise ValueError("component denominator differs from frozen manifest")
    _declared(draft.lock["components"], lock["components"], "components")
    # The operator allows source-only identities for these two experimental apps.
    # A missing SDK/image cannot silently become a source-only release component.
    for name, component in lock["components"].items():
        if component["artifactIdentityModel"] == "source-pinned" and (
            name not in {"honua-mobile", "honua-collect"}
            or component["lifecycleStatus"] != "Experimental"
        ):
            raise ValueError(f"{name}: no operator-approved source-only identity")


def build_bom(lock: dict) -> dict:
    """One BOM entry per locked artifact, preserving artifact and source-head identities."""
    digest = canonical_digest(lock)
    entries = []
    for name, component in sorted(lock["components"].items()):
        for index, artifact in enumerate(component["artifacts"]):
            props = {
                "honua:coordinate": artifact["coordinate"],
                "honua:artifactSourceRevision": artifact["sourceRevision"],
                "honua:componentSourceRevision": component["source"]["revision"],
                "honua:repository": component["source"]["repository"],
                "honua:lifecycleStatus": component["lifecycleStatus"],
                "honua:supportTier": component["supportTier"],
            }
            for key in ("digest", "integrity", "architectures", "platformDigests"):
                if key in artifact:
                    value = artifact[key]
                    props[f"honua:{key}"] = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
            for group in ("contractVersions", "schemaVersions"):
                for key, value in component[group].items():
                    props[f"honua:{group}:{key}"] = value
            entry = {"type": "container" if artifact["kind"] == "image" else "library",
                     "name": name, "version": artifact["version"],
                     "bom-ref": f"{digest}/{name}/{index}",
                     "properties": [{"name": key, "value": value} for key, value in sorted(props.items())]}
            hashes = []
            if "sha256" in artifact or "digest" in artifact:
                hashes.append({"alg": "SHA-256", "content": artifact.get("sha256", artifact.get("digest")).split(":", 1)[1]})
            if "integrity" in artifact:
                raw = base64.b64decode(artifact["integrity"].removeprefix("sha512-"), validate=True)
                if len(raw) != 64:
                    raise ValueError(f"{name}: SHA-512 integrity must contain exactly 64 bytes")
                hashes.append({"alg": "SHA-512", "content": raw.hex()})
            if hashes:
                entry["hashes"] = hashes
            entries.append(entry)
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "metadata": {"component": {"type": "application", "name": "honua-platform",
                                    "version": lock["platform"]["id"], "bom-ref": digest},
                     "properties": [{"name": "honua:platformLockDigest", "value": digest}]},
        "components": entries,
    }


def build_ledger(lock: dict) -> dict:
    digest = canonical_digest(lock)
    return {
        "ledgerVersion": "compatibility-ledger.v1",
        "platformLocks": {digest: {"platformLock": lock, "releaseArtifacts": [
            {"component": name, "artifactIndex": index}
            for name, component in sorted(lock["components"].items())
            for index, _ in enumerate(component["artifacts"])
        ], "certifications": []}},
        "componentReleases": {name: [digest] for name in sorted(lock["components"])},
        "artifactReceipts": [], "clientServerCertifications": [], "upgradeEdges": [],
        "experimentalExclusions": [
            {"lockDigest": digest, "component": name,
             "reason": f"Locked lifecycle: {component['lifecycleStatus']}"}
            for name, component in sorted(lock["components"].items())
            if component["lifecycleStatus"] in {"Experimental", "Excluded"}
        ],
    }


def bundle_files(lock: dict) -> dict[str, bytes]:
    return {
        "platform-lock.json": canonical_bytes(lock),
        "bom.cdx.json": canonical_bytes(build_bom(lock)),
        "compatibility-ledger.v1.json": canonical_bytes(build_ledger(lock)),
        "SDK-SERVER-COMPATIBILITY.md": render(lock).encode("utf-8"),
        # Site publication can copy this record; no parallel manifest interpretation.
        "platform-release.v1.json": canonical_bytes({
            "platform": lock["platform"], "lockDigest": canonical_digest(lock),
            "components": lock["components"], "notes": lock["notes"],
        }),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lock", type=Path)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--check", action="store_true", help="verify frozen bytes and all derivatives without rewriting")
    args = parser.parse_args(argv)
    try:
        lock = load_lock(args.lock)
        bind(lock, args.manifest, args.matrix, args.label)
        files = bundle_files(lock)
        if args.check:
            for name, expected in files.items():
                if (args.out_dir / name).read_bytes() != expected:
                    raise ValueError(f"{name}: bytes differ from the atomic lock")
        else:
            args.out_dir.mkdir(parents=True, exist_ok=True)
            # Refuse replacement: reruns may reproduce an identity, never move it.
            for name, expected in files.items():
                path = args.out_dir / name
                if path.exists() and path.read_bytes() != expected:
                    raise ValueError(f"{path}: refusing to overwrite a different candidate")
            for name, expected in files.items():
                (args.out_dir / name).write_bytes(expected)
        print(f"PASS: bound candidate {lock['platform']['id']} {canonical_digest(lock)}")
        return 0
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
