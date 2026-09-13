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


def evaluate(lock: dict, receipt: dict, digest: str, expected_revision: str) -> list[str]:
    failures: list[str] = []
    if not expected_revision:
        failures.append("expected manifest-pinned honua-server SHA is missing")
    if receipt.get("status") != "completed":
        failures.append("soak status must be completed (skipped/partial signals are failures)")
    if receipt.get("candidateRevision") != expected_revision:
        failures.append("candidate revision does not match the manifest-pinned honua-server SHA")
    if receipt.get("observedRevision") != expected_revision:
        failures.append("observed revision does not match the manifest-pinned honua-server SHA")
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
    declared = lock.get("supportedEnvelope", {})
    observed = receipt.get("envelope")
    if not isinstance(observed, dict) or any(
        name not in observed or observed[name] != value for name, value in declared.items()
    ):
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


def informational_dimensions(lock: dict, receipt: dict) -> dict:
    """Echo excluded Preview observations without adding them to the GA denominator."""
    result = {}
    for name in ("activeSubscriptions", "alertEvaluationsPerSecond"):
        if name in lock.get("supportedEnvelope", {}) or name in lock.get("soak", {}).get("requiredSignals", []):
            continue
        records = {
            section: receipt[section][name]
            for section in ("envelope", "envelopeVerification", "signals")
            if isinstance(receipt.get(section), dict) and name in receipt[section]
        }
        if records:
            result[name] = records
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    args = parser.parse_args()
    try:
        lock = json.loads(args.lock.read_text(encoding="utf-8"))
        receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
        failures = evaluate(lock, receipt, lock_digest(args.lock), args.expected_revision)
        for name, records in informational_dimensions(lock, receipt).items():
            print(f"Preview informational (not gated): {name} = {json.dumps(records, sort_keys=True)}")
    except (OSError, json.JSONDecodeError, ContractError) as exc:
        failures = [str(exc)]
    if failures:
        print("capacity-soak: FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(f"capacity-soak: PASS — {len(lock['supportedEnvelope'])}/{len(lock['supportedEnvelope'])} GA dimensions; "
          f"{len(lock['soak']['requiredSignals'])}/{len(lock['soak']['requiredSignals'])} frozen SLO signals; "
          "exact candidate, frozen lock, complete signed signal set")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
