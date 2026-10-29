#!/usr/bin/env python3
"""Fold one cell's genuine-model canary attempts into the journey report the nightly mint reads.

tools/mint_nightly_lock.py --declare-evidence reads every `gate-report-journey.json` the train
retained and builds the `nightly-model-journey` promotion receipt from rows whose attempts all ran
the `genuine-model` driver. This tool writes a row for one approved AWS GA cell (or the local Docker venue) from
the canary receipts of one run, in attempt order. Nightly selects ECS Redis-off; GA uses all four.

It fails closed: an attempt bound to another lock digest, another cell, a different mode, out of
order, or a failed attempt without a model/infrastructure attribution makes the row `fail`. Each
attempt keeps its own lockDigest so promotion compares the lock the canary actually ran against.

Without a lock digest (a manual dispatch before any nightly lock exists) the harness runs live only
when bound to the manifest-pinned server image its green deterministic receipt verified (2026-10-22
ruling: RC gates bind any build; honua-release#1047). Such a row passes or fails on its attempts like
a lock-bound row, records that image binding, and keeps a null lockDigest, so it counts for no lock
(the nightly mint and promotion compare every attempt's lockDigest with the lock). A harness that
could not bind writes its own `blocked` receipt and the row is `blocked`. An unbound attempt that
names no pinned image, or a mix of blocked and executed attempts, is a `fail`.
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
CELLS = ("local-docker", "aws-ecs/redis-off", "aws-ecs/redis-on",
         "aws-serverless/redis-off", "aws-serverless/redis-on")
DRIVER = "genuine-model"
UNBOUND_WHY = ("no platform lock to bind to (manual dispatch without a lock digest): the canary ran unbound "
               "and its receipt is blocked; it counts for no lock")
IMAGE_BOUND_WHY = ("no platform lock to bind to (manual dispatch without a lock digest): bound to the "
                   "manifest-pinned server image {reference}; it counts for no lock")


def _image_binding(receipts: list[dict], *, passed: bool) -> dict:
    """The one pinned server image every executed unbound attempt names (honua-release#1047)."""
    images = []
    for receipt in receipts:
        image = receipt.get("serverImage") or {}
        digest, reference = str(image.get("digest") or ""), str(image.get("reference") or "")
        if not LOCK_DIGEST.fullmatch(digest) or not reference.endswith("@" + digest):
            raise ValueError("an unbound attempt executed without the manifest-pinned server image binding")
        images.append({"reference": reference, "digest": digest})
    if any(image != images[0] for image in images):
        raise ValueError("unbound attempts name different server images")
    deterministic = ((receipts[-1].get("linkedEvidence") or {}).get("deterministicReceipt") or {})
    if passed and deterministic.get("candidateVerified") is not True:
        raise ValueError("an image-bound pass lacks the deterministic receipt that verified the candidate image")
    return {"kind": "server-image", **images[0]}


def _attempt(path: Path, number: int, cell: str, lock_digest: str | None) -> dict:
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
    profile = receipt.get("profile")
    outcome, executed = receipt.get("executionOutcome"), receipt.get("functionalGpExecuted")
    expected = ("redis-off-serving-refusal", "refused", False) if cell.endswith("/redis-off") else ("full-workflow", "completed", True)
    if profile != expected[0]:
        raise ValueError(f"attempt {number}: model profile does not match the declared topology")
    if status == "pass":
        if (profile, outcome, executed) != expected or type(executed) is not bool:
            raise ValueError(f"attempt {number}: model outcome does not qualify the declared profile")
        if attribution is not None:
            raise ValueError(f"attempt {number}: a pass carries a failure attribution")
    elif attribution not in ("model", "infrastructure"):
        raise ValueError(f"attempt {number}: failed attempt is unattributed")
    completed = receipt.get("completedAt")
    if not isinstance(completed, str) or not completed.endswith("Z"):
        raise ValueError(f"attempt {number}: completedAt must be an RFC3339 UTC timestamp")
    datetime.fromisoformat(completed[:-1] + "+00:00")
    return {"number": number, "status": "pass" if status == "pass" else "fail", "driver": DRIVER,
            "failureAttribution": attribution, "profile": profile, "executionOutcome": outcome,
            "functionalGpExecuted": executed, "completedAt": completed, "lockDigest": receipt["lockDigest"],
            "receipt": path.name, "receiptSha256": hashlib.sha256(data).hexdigest()}


def build(receipts: list[Path], *, cell: str, lock_digest: str | None, candidate_digest: str,
          run_id: str, run_attempt: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    row = {"cell": cell, "evidenceTier": "GA", "counted": True, "status": "fail",
           "drivers": [DRIVER], "attempts": [], "why": ""}
    try:
        if cell not in CELLS:
            raise ValueError(f"unknown genuine-model cell {cell!r}")
        if lock_digest is not None and not LOCK_DIGEST.fullmatch(lock_digest):
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
        raw = [json.loads(path.read_bytes()) for path in receipts] if lock_digest is None else []
        if lock_digest is None and all(receipt.get("status") == "blocked" for receipt in raw):
            # The harness's own refusal to run unbound: no binding, no execution, no verdict.
            notices = [str(notice) for notice in raw[-1].get("notices") or []]
            row.update(status="blocked", why=UNBOUND_WHY + (f"; harness: {notices[-1]}" if notices else ""))
        elif lock_digest is None:
            if any(receipt.get("status") == "blocked" for receipt in raw):
                raise ValueError("an unbound run mixes a blocked refusal with executed attempts")
            binding = _image_binding(raw, passed=attempts[-1]["status"] == "pass")
            row["binding"] = binding
            if attempts[-1]["status"] != "pass":
                raise ValueError("genuine-model journey did not pass within the recorded attempts "
                                 f"(bound to the pinned server image {binding['reference']}; no lock)")
            row.update(status="pass", why=IMAGE_BOUND_WHY.format(reference=binding["reference"]))
        elif attempts[-1]["status"] != "pass":
            raise ValueError("genuine-model journey did not pass within the recorded attempts")
        else:
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
    parser.add_argument("--lock-digest", help="omit only for an unbound run, which records `blocked`")
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
