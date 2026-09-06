#!/usr/bin/env python3
"""Validate compatibility-ledger.v1 cross-record and digest invariants."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import release_inspect
from validate_platform_lock import validate as validate_lock


def validate(ledger: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if ledger.get("ledgerVersion") != "compatibility-ledger.v1":
        errors.append("$.ledgerVersion: must equal compatibility-ledger.v1")
    records = ledger.get("platformLocks")
    if not isinstance(records, dict):
        return errors + ["$.platformLocks: must be a mapping"]
    for digest, record in records.items():
        lock = record.get("platformLock") if isinstance(record, dict) else None
        if not isinstance(lock, dict):
            errors.append(f"$.platformLocks.{digest}.platformLock: must be a mapping")
            continue
        errors.extend(f"$.platformLocks.{digest}.platformLock{error[1:]}"
                      for error in validate_lock(lock).errors)
        actual = release_inspect.canonical_digest(lock)
        if digest != actual:
            errors.append(f"$.platformLocks.{digest}: key does not match canonical platform-lock digest {actual}")
        for index, edge in enumerate(record.get("releaseArtifacts") or []):
            component = (lock.get("components") or {}).get(edge.get("component"))
            artifact_index = edge.get("artifactIndex")
            if not component or not isinstance(artifact_index, int) or artifact_index >= len(component.get("artifacts") or []):
                errors.append(f"$.platformLocks.{digest}.releaseArtifacts[{index}]: does not resolve to a lock artifact")

    reverse = ledger.get("componentReleases")
    if not isinstance(reverse, dict):
        errors.append("$.componentReleases: must be a mapping")
    else:
        expected: dict[str, set[str]] = {}
        for digest, record in records.items():
            for component in (record.get("platformLock") or {}).get("components", {}):
                expected.setdefault(component, set()).add(digest)
        for component in sorted(set(expected) | set(reverse)):
            actual_digests = set(reverse.get(component) or [])
            if actual_digests != expected.get(component, set()):
                errors.append(f"$.componentReleases.{component}: must exactly reverse the platformLocks component edges")

    for collection, fields in (("upgradeEdges", ("fromLockDigest", "toLockDigest")), ("experimentalExclusions", ("lockDigest",))):
        for index, edge in enumerate(ledger.get(collection) or []):
            for field in fields:
                if edge.get(field) not in records:
                    errors.append(f"$.{collection}[{index}].{field}: references an unknown platform-lock digest")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ledger", type=Path)
    args = parser.parse_args(argv)
    try:
        ledger = release_inspect.load_ledger(args.ledger)
    except release_inspect.InspectError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    errors = validate(ledger)
    if errors:
        print(f"REFUSED: {len(errors)} compatibility ledger violation(s)", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"PASS: {args.ledger} is a coherent compatibility-ledger.v1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
