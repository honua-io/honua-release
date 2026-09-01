#!/usr/bin/env python3
"""Fail-closed evaluator for the pre-frozen 2026.1 capacity/SLO lock."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path


class ContractError(ValueError):
    pass


def _time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ContractError(f"{field} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{field} is not an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def lock_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate(lock: dict, receipt: dict, digest: str) -> list[str]:
    failures: list[str] = []
    if receipt.get("status") != "completed":
        failures.append("soak status must be completed (skipped/partial signals are failures)")
    if receipt.get("candidateRevision") != receipt.get("observedRevision"):
        failures.append("observed revision does not match candidate revision")
    if receipt.get("lockSha256") != digest:
        failures.append("receipt does not bind the exact committed threshold lock")
    try:
        if _time(receipt.get("startedAt"), "startedAt") <= _time(lock.get("frozenAt"), "frozenAt"):
            failures.append("soak did not start after the threshold freeze")
    except ContractError as exc:
        failures.append(str(exc))
    if not receipt.get("signingIdentity") or not receipt.get("signature"):
        failures.append("signed receipt identity/signature is missing")
    if receipt.get("profile") != lock.get("soak", {}).get("profile"):
        failures.append("soak profile does not match the lock")
    if receipt.get("steadyStateSeconds", 0) < lock.get("soak", {}).get("minimumSteadyStateSeconds", 0):
        failures.append("steady-state duration is below the locked minimum")
    if receipt.get("envelope") != lock.get("supportedEnvelope"):
        failures.append("tested capacity envelope does not exactly match the supported envelope")

    signals = receipt.get("signals")
    if not isinstance(signals, dict):
        failures.append("signals object is missing")
        signals = {}
    required = lock.get("soak", {}).get("requiredSignals", [])
    thresholds = lock.get("thresholds", {})
    for name in required:
        signal = signals.get(name)
        if not isinstance(signal, dict) or signal.get("status") != "observed":
            failures.append(f"{name}: missing, skipped, or unobserved")
            continue
        if signal.get("revision") != receipt.get("candidateRevision"):
            failures.append(f"{name}: signal revision mismatch")
            continue
        value = signal.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            failures.append(f"{name}: value is absent or non-finite")
            continue
        threshold = thresholds.get(name, {})
        operator, limit = threshold.get("operator"), threshold.get("value")
        passed = operator == "<=" and value <= limit or operator == ">=" and value >= limit
        if not passed:
            failures.append(f"{name}: {value} violates frozen requirement {operator} {limit}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    try:
        lock = json.loads(args.lock.read_text(encoding="utf-8"))
        receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
        failures = evaluate(lock, receipt, lock_digest(args.lock))
    except (OSError, json.JSONDecodeError, ContractError) as exc:
        failures = [str(exc)]
    if failures:
        print("capacity-soak: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("capacity-soak: PASS — exact candidate, frozen lock, complete signed signal set")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
