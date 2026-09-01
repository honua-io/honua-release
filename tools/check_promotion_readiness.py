#!/usr/bin/env python3
"""Fail-closed burn-in decision for promotion of an exact RC bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RUN_ID_RE = re.compile(r"^[1-9][0-9]*$")
PHASES = ("freeze", "during", "after")


class ReadinessError(ValueError):
    pass


def _time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ReadinessError(f"{field} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ReadinessError(f"{field} must be an RFC3339 UTC timestamp") from exc
    if parsed.tzinfo != timezone.utc:
        raise ReadinessError(f"{field} must be UTC")
    return parsed


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _run_id(value: Any, field: str) -> str:
    value = str(value)
    if not RUN_ID_RE.fullmatch(value):
        raise ReadinessError(f"{field} must be a positive Actions run id")
    return value


def _load(path: Path, field: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReadinessError(f"{field} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise ReadinessError(f"{field} must be a JSON object")
    return value


def evaluate(
    record: dict[str, Any], *, lock_path: Path, evidence_dir: Path,
    lock_history: Path, now: datetime,
) -> tuple[dict[str, Any], list[str]]:
    checks: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks[name] = {"status": "pass" if passed else "fail", "detail": detail}
        if not passed:
            failures.append(f"{name}: {detail}")

    check("record-schema", record.get("schemaVersion") == "promotion-evidence.v1",
          f"schemaVersion={record.get('schemaVersion')!r}")
    label = record.get("platformLabel")
    check("platform-label", isinstance(label, str) and bool(re.fullmatch(r"[0-9]+\.[0-9]+-rc\.[1-9][0-9]*", label or "")),
          f"platformLabel={label!r}")

    lock = record.get("lock") if isinstance(record.get("lock"), dict) else {}
    recorded_digest = lock.get("digest")
    actual_digest = _digest(lock_path)
    check("lock-digest", isinstance(recorded_digest, str) and SHA256_RE.fullmatch(recorded_digest) is not None
          and recorded_digest == actual_digest,
          f"recorded={recorded_digest!r}; current={actual_digest}")
    history = [line for line in lock_history.read_text(encoding="utf-8").splitlines() if line.strip()]
    check("lock-unchanged", not history,
          "no platform-lock commits after burn start" if not history else
          f"lock changed after burn start in commits: {', '.join(history)}; reset required")

    burn_start = _time(lock.get("burnStartedAt"), "lock.burnStartedAt")
    age = now - burn_start
    check("burn-window", timedelta(hours=48) <= age <= timedelta(hours=72),
          f"candidate age is {age.total_seconds() / 3600:.2f}h; required 48-72h")

    trains = record.get("strictTrains")
    if not isinstance(trains, list):
        raise ReadinessError("strictTrains must be an array")
    train_ok = len(trains) == 3 and [row.get("phase") for row in trains if isinstance(row, dict)] == list(PHASES)
    train_times: list[datetime] = []
    train_ids: list[str] = []
    for index, row in enumerate(trains):
        if not isinstance(row, dict):
            train_ok = False
            continue
        run_id = _run_id(row.get("runId"), f"strictTrains[{index}].runId")
        completed = _time(row.get("completedAt"), f"strictTrains[{index}].completedAt")
        train_ids.append(run_id)
        train_times.append(completed)
        report_path = evidence_dir / "trains" / run_id / "gate-report.json"
        try:
            report = _load(report_path, f"train {run_id} receipt")
            run_metadata = _load(evidence_dir / "trains" / run_id / "run.json", f"train {run_id} metadata")
            binding = report.get("candidate") if isinstance(report.get("candidate"), dict) else {}
            train = binding.get("train") if isinstance(binding.get("train"), dict) else {}
            candidate_lock = evidence_dir / "trains" / run_id / "platform-lock.json"
            train_ok &= (
                report.get("overallStatus") == "pass"
                and report.get("dry_run") is False
                and str(train.get("runId")) == run_id
                and _digest(candidate_lock) == recorded_digest
                and row.get("lockDigest") == recorded_digest
                and row.get("status") == "pass"
                and run_metadata.get("updated_at") == row.get("completedAt")
            )
        except (ReadinessError, OSError):
            train_ok = False
    if len(set(train_ids)) != len(train_ids):
        train_ok = False
    if len(train_times) == 3:
        train_ok &= train_times[0] <= burn_start < train_times[1] < burn_start + timedelta(hours=48)
        train_ok &= burn_start + timedelta(hours=48) <= train_times[2] <= burn_start + timedelta(hours=72)
        sequence_pages = json.loads((evidence_dir / "train-sequence.json").read_text(encoding="utf-8"))
        sequence = [run for page in sequence_pages for run in page.get("workflow_runs", [])]
        strict_sequence = sorted(
            (run for run in sequence
             if str(run.get("display_title", "")).endswith("(dry_run=false)")
             and train_times[0] <= _time(run.get("updated_at"), "release-train updated_at") <= train_times[2]),
            key=lambda run: _time(run.get("updated_at"), "release-train updated_at"),
        )
        train_ok &= [str(run.get("id")) for run in strict_sequence] == train_ids
        train_ok &= all(run.get("status") == "completed" and run.get("conclusion") == "success"
                        for run in strict_sequence)
    check("strict-trains", train_ok,
          "three consecutive complete strict trains at freeze, during, and after burn-in")

    canaries = record.get("demoCanaries")
    if not isinstance(canaries, list):
        raise ReadinessError("demoCanaries must be an array")
    canary_ok = len(canaries) == 7
    canary_times: list[datetime] = []
    canary_ids: list[str] = []
    for index, row in enumerate(canaries):
        if not isinstance(row, dict):
            canary_ok = False
            continue
        run_id = _run_id(row.get("runId"), f"demoCanaries[{index}].runId")
        completed = _time(row.get("completedAt"), f"demoCanaries[{index}].completedAt")
        canary_ids.append(run_id)
        canary_times.append(completed)
        try:
            receipt = _load(evidence_dir / "canaries" / run_id / "live-canary-evidence.json", f"canary {run_id} receipt")
            run_metadata = _load(evidence_dir / "canaries" / run_id / "run.json", f"canary {run_id} metadata")
            receipt_lock = receipt.get("candidateLock") if isinstance(receipt.get("candidateLock"), dict) else {}
            canary_ok &= (row.get("status") == "pass" and row.get("lockDigest") == recorded_digest
                          and receipt.get("status") == "pass" and str(receipt.get("runId")) == run_id
                          and receipt_lock.get("digest") == recorded_digest
                          and run_metadata.get("updated_at") == row.get("completedAt"))
        except ReadinessError:
            canary_ok = False
    if len(set(canary_ids)) != len(canary_ids):
        canary_ok = False
    if len(canary_times) == 7:
        canary_ok &= canary_times == sorted(canary_times) and canary_times[0] >= burn_start
        canary_ok &= all(timedelta(hours=5, minutes=30) <= b - a <= timedelta(hours=6, minutes=30)
                         for a, b in zip(canary_times, canary_times[1:]))
        canary_ok &= timedelta(0) <= now - canary_times[-1] <= timedelta(hours=6, minutes=30)
    check("demo-canaries", canary_ok,
          "seven distinct consecutive passing 6-hour canaries, latest no more than 6.5h old")

    rc_run_id = _run_id(record.get("rcTrainRunId"), "rcTrainRunId")
    check("exact-rc", rc_run_id in train_ids and bool(train_ids) and rc_run_id == train_ids[0],
          f"promotion source run {rc_run_id} is the recorded freeze RC train")
    decision = {
        "schemaVersion": "promotion-readiness.v1", "platformLabel": label,
        "lockDigest": actual_digest, "rcTrainRunId": rc_run_id,
        "evaluatedAt": now.isoformat().replace("+00:00", "Z"),
        "status": "pass" if not failures else "refused", "checks": checks,
    }
    return decision, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", required=True, type=Path)
    parser.add_argument("--lock", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--lock-history", required=True, type=Path)
    parser.add_argument("--now")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        now = _time(args.now, "now") if args.now else datetime.now(timezone.utc)
        decision, failures = evaluate(_load(args.record, "promotion record"), lock_path=args.lock,
                                      evidence_dir=args.evidence_dir, lock_history=args.lock_history, now=now)
    except (ReadinessError, OSError) as exc:
        decision, failures = {"schemaVersion": "promotion-readiness.v1", "status": "refused",
                              "checks": {"record": {"status": "fail", "detail": str(exc)}}}, [str(exc)]
    args.out.write_text(json.dumps(decision, indent=2) + "\n", encoding="utf-8")
    for name, result in decision["checks"].items():
        print(f"{result['status'].upper():7} {name}: {result['detail']}")
    if failures:
        print("REFUSED: promotion conditions are incomplete or invalid")
        return 1
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"rc_train_run_id={decision['rcTrainRunId']}\n")
            stream.write(f"lock_digest={decision['lockDigest']}\n")
    print("PASS: promotion conditions hold for the exact recorded RC")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
