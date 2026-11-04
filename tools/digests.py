"""SHA-256 digests and canonical JSON bytes that release receipts are bound by."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SHA256_PREFIX = "sha256:"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_digest(data: bytes) -> str:
    return SHA256_PREFIX + sha256_hex(data)


def file_sha256_hex(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def file_sha256_digest(path: Path) -> str:
    return SHA256_PREFIX + file_sha256_hex(path)


def ascii_canonical_bytes(value) -> bytes:
    # Non-ASCII stays \u-escaped: persisted receipt digests were taken over these exact bytes.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def canonical_bytes(value) -> bytes:
    # Non-ASCII is preserved: persisted receipt digests were taken over these exact bytes.
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _canonical_receipt(value) -> bytes | None:
    try:
        return canonical_bytes(value)
    except ValueError:
        return None


def receipt_digest(value) -> str | None:
    if not isinstance(value, dict):
        return None
    body = _canonical_receipt(value)
    if body is None:
        return None
    return sha256_digest(body)
