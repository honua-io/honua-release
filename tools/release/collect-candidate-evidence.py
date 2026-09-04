#!/usr/bin/env python3
"""Join post-candidate evidence and fail closed on identity drift."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


SHA = re.compile(r"^[0-9a-f]{40}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
REQUIRED = {"realtime", "sdk-python", "protocol-grpc", "protocol-mcp"}
IDENTITY_KEYS = {"image_digest", "imageDigest", "serverImage", "server_image", "image"}
SOURCE_KEYS = {"source_sha", "sourceSha", "serverRevision", "server_revision"}
TIME_KEYS = {"generatedAt", "generated_at", "generated_at_utc"}


def values_at(value: Any, names: set[str], path: str = "") -> Iterator[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            if key in names:
                yield child_path, child
            yield from values_at(child, names, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from values_at(child, names, f"{path}[{index}]")


def parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not RFC3339") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} has no timezone")
    return parsed.astimezone(timezone.utc)


def validate_receipt(meta: dict[str, Any], receipt: Any, *, digest: str, source_sha: str, now: datetime) -> dict[str, Any]:
    errors: list[str] = []
    if not meta.get("runId") or not meta.get("runAttempt") or not meta.get("artifactId"):
        errors.append("dispatcher metadata is missing immutable run/artifact identity")

    identities = list(values_at(receipt, IDENTITY_KEYS))
    matching = [path for path, value in identities if value == digest]
    conflicting = [path for path, value in identities if isinstance(value, str) and DIGEST.fullmatch(value) and value != digest]
    if not matching:
        errors.append(f"receipt does not name candidate digest {digest}")
    if conflicting:
        errors.append(f"receipt contains a conflicting digest at {', '.join(conflicting)}")

    source_values = [value for _, value in values_at(receipt, SOURCE_KEYS)]
    if not source_values:
        errors.append("receipt has no candidate source revision")
    elif source_sha not in source_values:
        errors.append(f"receipt source revision does not name candidate {source_sha}")
    timestamps = [value for _, value in values_at(receipt, TIME_KEYS)]
    if not timestamps:
        errors.append("receipt has no generated timestamp")
    else:
        try:
            generated = parse_time(timestamps[0], "receipt generated timestamp")
            if generated > now + timedelta(minutes=5):
                errors.append("receipt timestamp is in the future")
            if now - generated > timedelta(hours=24):
                errors.append("receipt is stale")
        except ValueError as exc:
            errors.append(str(exc))

    return {
        "id": meta.get("id"),
        "runId": meta.get("runId"),
        "runAttempt": meta.get("runAttempt"),
        "artifactId": meta.get("artifactId"),
        "status": "pass" if not errors else "fail",
        "errors": errors,
        "digestPaths": matching,
    }


def collect(directory: Path, *, digest: str, source_sha: str, now: datetime) -> dict[str, Any]:
    if not DIGEST.fullmatch(digest):
        raise ValueError("candidate image must be an exact sha256 digest")
    if not SHA.fullmatch(source_sha):
        raise ValueError("candidate source revision must be a full lowercase SHA")

    rows: list[dict[str, Any]] = []
    for metadata_path in sorted(directory.glob("*/metadata.json")):
        meta = json.loads(metadata_path.read_text(encoding="utf-8"))
        receipt_path = Path(str(meta.get("receipt", "")))
        if not receipt_path.is_file():
            rows.append({"id": meta.get("id"), "status": "fail", "errors": ["receipt file is missing"]})
            continue
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        rows.append(validate_receipt(meta, receipt, digest=digest, source_sha=source_sha, now=now))

    ids = {row.get("id") for row in rows}
    for required in sorted(REQUIRED - ids):
        rows.append({"id": required, "status": "fail", "errors": ["required post-candidate receipt is missing"]})
    result = {
        "schemaVersion": 1,
        "status": "pass" if ids >= REQUIRED and rows and all(row["status"] == "pass" for row in rows) else "fail",
        "candidate": {"sourceSha": source_sha, "imageDigest": digest},
        "evidence": rows,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--candidate-digest", required=True)
    parser.add_argument("--candidate-source-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = collect(args.evidence_dir, digest=args.candidate_digest, source_sha=args.candidate_source_sha, now=datetime.now(timezone.utc))
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[ERROR] candidate evidence collection: {exc}", file=sys.stderr)
        return 1
    print(f"candidate evidence: {result['status']} ({args.output})")
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
