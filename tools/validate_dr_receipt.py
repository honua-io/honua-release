#!/usr/bin/env python3
"""Fail closed on incomplete, candidate-bound disaster recovery evidence.

The deployment inventory is a release input, never a receipt-supplied denominator.
This verifies observations; it does not turn fixture receipts into live certification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

SUBSTRATES = ("postgresql", "redis", "object-storage", "job-queue",
              "transactional-outbox", "workflow-cursors")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
RECEIPT_MAX_AGE = timedelta(hours=24)
# A reported recovery time must agree with the outage the receipt itself records. The
# allowance covers producer clock granularity and instrumentation overhead, never a
# different measurement: it is the larger of one second and 5% of the observed window.
RTO_TOLERANCE_MS = 1000.0
RTO_TOLERANCE_RATIO = 0.05


class ReceiptError(ValueError):
    pass


def mapping(value, path):
    if not isinstance(value, dict):
        raise ReceiptError(f"{path}: missing or invalid object")
    return value


def nonempty(value, path):
    if not isinstance(value, str) or not value.strip():
        raise ReceiptError(f"{path}: missing or empty value")
    return value


def timestamp(value, path):
    try:
        result = datetime.fromisoformat(nonempty(value, path).replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result
    except ValueError as exc:
        raise ReceiptError(f"{path}: missing or invalid timezone-aware timestamp") from exc


def digest(value, path):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ReceiptError(f"{path}: missing or invalid SHA-256")
    return value


def expected_substrates(candidate):
    config = mapping(candidate.get("disasterRecovery"), "candidate.disasterRecovery")
    topology = nonempty(config.get("topology"), "candidate.disasterRecovery.topology")
    configured = mapping(config.get("substrates"), "candidate.disasterRecovery.substrates")
    # Explicit false is required: an omitted configuration entry is not evidence of disablement.
    for name in SUBSTRATES:
        if name not in configured:
            raise ReceiptError(f"candidate.disasterRecovery.substrates.{name}: missing enablement")
    for name, enabled in configured.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", name):
            raise ReceiptError("candidate.disasterRecovery.substrates: invalid substrate name")
        if type(enabled) is not bool:
            raise ReceiptError(f"candidate.disasterRecovery.substrates.{name}: enablement must be boolean")
    required = {name for name, enabled in configured.items() if enabled}
    if not required:
        raise ReceiptError("candidate.disasterRecovery.substrates: no enabled durable substrate")
    return topology, required


def objective(value, path, minimum):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ReceiptError(f"{path}: require a finite objective of at least {minimum} millisecond(s)")
    return float(value)


def expected_objectives(candidate):
    """Deployment-owned recovery limits. Absent limits fail closed: without them a
    producer-reported measurement has nothing it can fail against."""
    config = mapping(candidate.get("disasterRecovery"), "candidate.disasterRecovery")
    objectives = mapping(config.get("objectives"), "candidate.disasterRecovery.objectives")
    # Zero data loss is a legitimate objective; zero recovery time is not observable.
    return (objective(objectives.get("rpoMs"), "candidate.disasterRecovery.objectives.rpoMs", 0),
            objective(objectives.get("rtoMs"), "candidate.disasterRecovery.objectives.rtoMs", 1))


def observation(value, path):
    value = mapping(value, path)
    identity = nonempty(value.get("stateId"), f"{path}.stateId")
    checksum = digest(value.get("sha256"), f"{path}.sha256")
    count = value.get("count")
    if type(count) is not int or count < 1:
        raise ReceiptError(f"{path}.count: require nonempty observed state")
    nonempty(value.get("runtimeSurface"), f"{path}.runtimeSurface")
    at = timestamp(value.get("observedAt"), f"{path}.observedAt")
    return (identity, checksum, count), at


def validate(candidate_path: Path, receipt, *, now: datetime | None = None):
    candidate_bytes = candidate_path.read_bytes()
    candidate = mapping(yaml.safe_load(candidate_bytes), "candidate")
    topology, required = expected_substrates(candidate)
    rpo_limit, rto_limit = expected_objectives(candidate)
    receipt = mapping(receipt, "receipt")
    if receipt.get("schema") != "honua.dr-drill-receipt/v2":
        raise ReceiptError("receipt.schema: require honua.dr-drill-receipt/v2; PostgreSQL restore evidence is not full-platform DR")
    if receipt.get("scope") != "full-platform" or receipt.get("status") != "pass":
        raise ReceiptError("receipt: require scope=full-platform and status=pass")
    if receipt.get("candidateLockDigest") != "sha256:" + hashlib.sha256(candidate_bytes).hexdigest():
        raise ReceiptError("receipt.candidateLockDigest: does not match candidate configuration/lock bytes")
    if receipt.get("topology") != topology:
        raise ReceiptError("receipt.topology: does not match candidate.disasterRecovery.topology")
    entries = mapping(receipt.get("substrates"), "receipt.substrates")
    missing = required - entries.keys()
    if missing:
        raise ReceiptError("receipt.substrates: missing enabled substrate(s): " + ", ".join(sorted(missing)))
    extra = entries.keys() - required
    if extra:
        raise ReceiptError("receipt.substrates: unconfigured substrate(s): " + ", ".join(sorted(extra)))
    start = timestamp(receipt.get("startedAt"), "receipt.startedAt")
    end = timestamp(receipt.get("completedAt"), "receipt.completedAt")
    now = now or datetime.now(timezone.utc)
    if start > end or end > now:
        raise ReceiptError("receipt: invalid drill interval or future completion timestamp")
    # Bound the oldest observation, so neither a long drill nor a new gate report
    # can refresh old telemetry. The CLI always uses the actual verification time.
    if now - start > RECEIPT_MAX_AGE:
        raise ReceiptError("receipt: stale DR evidence; drill must start within the last 24 hours")
    measurements = mapping(receipt.get("measurements"), "receipt.measurements")
    reported = {}
    for name in ("rpoMs", "rtoMs"):
        value = measurements.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ReceiptError(f"receipt.measurements.{name}: require finite nonnegative measurement")
        reported[name] = float(value)
    for name, allowed in (("rpoMs", rpo_limit), ("rtoMs", rto_limit)):
        if reported[name] > allowed:
            raise ReceiptError(
                f"receipt.measurements.{name}: {reported[name]:.0f} ms exceeds the candidate "
                f"objective of {allowed:.0f} ms")
    outage_started: list[datetime] = []
    recovered: list[datetime] = []
    for name in sorted(required):
        path = f"receipt.substrates.{name}"
        entry = mapping(entries[name], path)
        backup = mapping(entry.get("backup"), f"{path}.backup")
        nonempty(backup.get("id"), f"{path}.backup.id")
        digest(backup.get("sha256"), f"{path}.backup.sha256")
        if backup.get("primaryStateDestroyed") is not True or backup.get("restoredIntoCleanStore") is not True:
            raise ReceiptError(f"{path}.backup: require destroyed primary state and restore into clean store")
        restart = mapping(entry.get("restartRecovery"), f"{path}.restartRecovery")
        before, written_at = observation(restart.get("writtenBeforeRestart"), f"{path}.restartRecovery.writtenBeforeRestart")
        after, read_at = observation(restart.get("readAfterRestart"), f"{path}.restartRecovery.readAfterRestart")
        stopped = timestamp(restart.get("stoppedAt"), f"{path}.restartRecovery.stoppedAt")
        ready = timestamp(restart.get("readyAt"), f"{path}.restartRecovery.readyAt")
        prior = nonempty(restart.get("instanceBefore"), f"{path}.restartRecovery.instanceBefore")
        current = nonempty(restart.get("instanceAfter"), f"{path}.restartRecovery.instanceAfter")
        if prior == current:
            raise ReceiptError(f"{path}.restartRecovery: instance identity did not change")
        if not start <= written_at < stopped < ready < read_at <= end:
            raise ReceiptError(f"{path}.restartRecovery: observations must bracket restart within the drill interval")
        if before != after:
            raise ReceiptError(f"{path}.restartRecovery: readAfterRestart stateId/sha256/count differs from writtenBeforeRestart")
        outage_started.append(stopped)
        recovered.append(read_at)
    # Recovery ends when the restored state is readable again through the recorded runtime
    # surface, not when a process reports ready. Ordering above guarantees a positive window,
    # so a reported zero can never agree with it.
    observed = (max(recovered) - min(outage_started)).total_seconds() * 1000.0
    if abs(reported["rtoMs"] - observed) > max(RTO_TOLERANCE_MS, observed * RTO_TOLERANCE_RATIO):
        raise ReceiptError(
            f"receipt.measurements.rtoMs: {reported['rtoMs']:.0f} ms disagrees with the "
            f"{observed:.0f} ms recovery window observed between the earliest stoppedAt and the "
            "latest readAfterRestart.observedAt")
    return sorted(required)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True, help="pipeline-selected candidate manifest or lock")
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        required = validate(args.candidate, json.loads(args.receipt.read_text(encoding="utf-8")))
    except (ReceiptError, OSError, ValueError, yaml.YAMLError) as exc:
        print(f"DR receipt FAIL: {exc}", file=sys.stderr)
        return 1
    print("DR receipt PASS: restart recovery observed for " + ", ".join(required))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
