#!/usr/bin/env python3
"""Fold one cell's genuine-model canary attempts into the journey report the nightly mint reads.

tools/mint_nightly_lock.py --declare-evidence reads every `gate-report-journey.json` the train
retained and builds the `nightly-model-journey` promotion receipt from rows whose attempts all ran
the `genuine-model` driver. This tool writes that row for the ECS Redis-off cell (or the local Docker
venue) from the canary receipts of one run, in attempt order.

It fails closed: an attempt bound to another lock digest, another cell, a different mode, out of
order, or a failed attempt without a model/infrastructure attribution makes the row `fail`. Each
attempt keeps its own lockDigest so promotion compares the lock the canary actually ran against.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

LOCK_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
CELLS = ("local-docker", "aws-ecs/redis-off")
DRIVER = "genuine-model"


def _attempt(path: Path, number: int, cell: str, lock_digest: str) -> dict:
    data = path.read_bytes()
    receipt = json.loads(data)
    if not isinstance(receipt, dict):
        raise ValueError(f"attempt {number}: receipt is not an object")
    if receipt.get("mode") != DRIVER:
        raise ValueError(f"attempt {number}: receipt is not a genuine-model journey")
    if receipt.get("cell") != cell:
        raise ValueError(f"attempt {number}: receipt ran on another cell")
    if receipt.get("attempt") != number:
        raise ValueError(f"attempt {number}: receipt records attempt {receipt.get('attempt')!r}")
    if receipt.get("lockDigest") != lock_digest:
        raise ValueError(f"attempt {number}: receipt is bound to another lock digest")
    status = receipt.get("status")
    attribution = receipt.get("failureAttribution")
    if status == "pass":
        if attribution is not None:
            raise ValueError(f"attempt {number}: a pass carries a failure attribution")
    elif attribution not in ("model", "infrastructure"):
        raise ValueError(f"attempt {number}: failed attempt is unattributed")
    completed = receipt.get("completedAt")
    if not isinstance(completed, str) or not completed.endswith("Z"):
        raise ValueError(f"attempt {number}: completedAt must be an RFC3339 UTC timestamp")
    datetime.fromisoformat(completed[:-1] + "+00:00")
    return {"number": number, "status": "pass" if status == "pass" else "fail", "driver": DRIVER,
            "failureAttribution": attribution, "completedAt": completed, "lockDigest": receipt["lockDigest"],
            "receipt": path.name, "receiptSha256": hashlib.sha256(data).hexdigest()}


def build(receipts: list[Path], *, cell: str, lock_digest: str, candidate_digest: str,
          run_id: str, run_attempt: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    row = {"cell": cell, "evidenceTier": "GA", "counted": True, "status": "fail",
           "drivers": [DRIVER], "attempts": [], "why": ""}
    try:
        if cell not in CELLS:
            raise ValueError(f"unknown genuine-model cell {cell!r}")
        if not LOCK_DIGEST.fullmatch(lock_digest):
            raise ValueError("lock digest must be sha256:<64 lowercase hex>")
        if not re.fullmatch(r"[0-9a-f]{64}", candidate_digest):
            raise ValueError("candidate digest must be the manifest's bare SHA-256")
        if not all(re.fullmatch(r"[1-9][0-9]*", value) for value in (run_id, run_attempt)):
            raise ValueError("run id and attempt must be positive integers")
        if not 1 <= len(receipts) <= 2:
            raise ValueError("a cell records one or two attempts")
        attempts = [_attempt(path, number, cell, lock_digest) for number, path in enumerate(receipts, 1)]
        row["attempts"] = attempts
        if any(attempt["status"] == "pass" for attempt in attempts[:-1]):
            raise ValueError("an attempt was recorded after a passing attempt")
        times = [attempt["completedAt"] for attempt in attempts]
        if times != sorted(times):
            raise ValueError("attempts are not in completion order")
        if attempts[-1]["status"] != "pass":
            raise ValueError("genuine-model journey did not pass within the recorded attempts")
        row.update(status="pass", why="genuine-model journey passed for the exact lock")
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        row["why"] = str(error) if isinstance(error, ValueError) else "missing or malformed canary receipt"
    status = row["status"]
    return {"gate": "nightly-model-journey", "status": status, "overallStatus": status,
            "source": "terminal-model-canary", "why": row["why"], "candidateDigest": candidate_digest,
            "lockDigest": lock_digest, "runId": run_id, "runAttempt": run_attempt,
            "generatedAt": now.isoformat().replace("+00:00", "Z"), "cells": [row]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, action="append", required=True,
                        help="canary receipt, once per attempt in attempt order")
    parser.add_argument("--cell", required=True, choices=CELLS)
    parser.add_argument("--lock-digest", required=True)
    parser.add_argument("--candidate-digest", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = build(args.receipt, cell=args.cell, lock_digest=args.lock_digest,
                   candidate_digest=args.candidate_digest, run_id=args.run_id, run_attempt=args.run_attempt)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"nightly-model-journey {args.cell}: {report['status']} — {report['why']}")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())
