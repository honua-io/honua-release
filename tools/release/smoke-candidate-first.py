#!/usr/bin/env python3
"""Deterministic local smoke for the candidate-first release contract.

This does not impersonate GitHub Actions. It exercises the same release-side
join with a throwaway label and proves that a matching digest passes while a
wrong digest is rejected.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "collect_candidate_evidence", ROOT / "tools/release/collect-candidate-evidence.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)

SOURCE_SHA = "a" * 40
CANDIDATE_DIGEST = "sha256:" + "b" * 64
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def write_receipt(root: Path, producer: str, digest: str) -> None:
    folder = root / producer
    folder.mkdir()
    receipt = folder / "receipt.json"
    receipt.write_text(json.dumps({
        "candidate": {"source_sha": SOURCE_SHA, "image_digest": digest},
        "generated_at": "2026-09-04T11:00:00Z",
    }) + "\n", encoding="utf-8")
    (folder / "metadata.json").write_text(json.dumps({
        "id": producer, "runId": "269", "runAttempt": "1", "artifactId": "26901",
        "receipt": str(receipt),
    }) + "\n", encoding="utf-8")


def main() -> None:
    with TemporaryDirectory(prefix="release-269-dry-run-") as raw:
        root = Path(raw)
        print("release_id=269-dry-run")
        print(f"candidate digest emitted: {CANDIDATE_DIGEST}")
        for producer in ("realtime", "sdk-python", "protocol-grpc", "protocol-mcp"):
            print(f"post-candidate evidence collected: {producer}")
            write_receipt(root, producer, CANDIDATE_DIGEST)
        passed = MODULE.collect(root, digest=CANDIDATE_DIGEST, source_sha=SOURCE_SHA, now=NOW)
        assert passed["status"] == "pass"
        print("exact-candidate gate: pass")

        wrong = root / "wrong-digest"
        wrong.mkdir()
        write_receipt(wrong, "realtime", "sha256:" + "c" * 64)
        rejected = MODULE.collect(wrong, digest=CANDIDATE_DIGEST, source_sha=SOURCE_SHA, now=NOW)
        assert rejected["status"] == "fail"
        print("wrong-digest regression: rejected")


if __name__ == "__main__":
    main()
