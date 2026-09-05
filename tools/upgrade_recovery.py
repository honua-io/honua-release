#!/usr/bin/env python3
"""Build and validate candidate-bound failed-upgrade recovery receipts."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCENARIOS = {
    "post-migration-readiness",
    "bad-config",
    "migration-boundary",
    "rollback-failure",
}
TERMINAL = {"recovered", "manual-restore-required", "rollback-failed"}
REQUIRED_PHASES = ("prior-ready", "candidate-failed", "recovery-started", "recovery-terminal")
REQUIRED_EVIDENCE = (
    "candidate.log",
    "helm-history.log",
    "migration-journal-before.json",
    "migration-journal-after.json",
    "seed-checksums-before.json",
    "seed-checksums-after.json",
    "rollback.log",
    "prior-readiness.json",
    "rollback-read-write.json",
    "imageids.json",
)


class RecoveryError(ValueError):
    pass


def sha256_bytes(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise RecoveryError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise RecoveryError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def classify(observation: dict[str, Any]) -> str:
    if not observation.get("rollbackAttempted"):
        raise RecoveryError("failed upgrade did not attempt rollback")
    if observation.get("schemaCompatible") is False:
        if observation.get("rollbackSucceeded") or observation.get("priorReady"):
            raise RecoveryError("schema-incompatible rollback must not claim the prior image ready")
        return "manual-restore-required"
    required = (
        "rollbackSucceeded",
        "priorReady",
        "priorImageMatched",
        "seedChecksumsMatched",
        "boundedReadPassed",
        "boundedWritePassed",
    )
    return "recovered" if all(observation.get(key) is True for key in required) else "rollback-failed"


def validate(receipt: dict[str, Any], evidence_dir: Path | None = None) -> dict[str, Any]:
    if receipt.get("schema") != "honua.upgrade-recovery-receipt/v1":
        raise RecoveryError("unsupported recovery receipt schema")
    if receipt.get("scenario") not in SCENARIOS:
        raise RecoveryError("unknown deterministic failure scenario")
    bindings = receipt.get("bindings") or {}
    for key in ("candidateLockDigest", "priorLockDigest", "candidateImageDigest", "priorImageDigest"):
        value = bindings.get(key)
        if not isinstance(value, str) or not value.startswith("sha256:") or len(value) != 71:
            raise RecoveryError(f"bindings.{key} must be an exact sha256 digest")
    phases = receipt.get("phases") or []
    names = [phase.get("name") for phase in phases]
    if names != list(REQUIRED_PHASES):
        raise RecoveryError(f"phases must be ordered exactly as {REQUIRED_PHASES}")
    times = [_timestamp(phase.get("at", ""), f"phases[{index}].at") for index, phase in enumerate(phases)]
    if times != sorted(times):
        raise RecoveryError("phase timestamps are not monotonic")
    observation = receipt.get("observation") or {}
    expected = classify(observation)
    if receipt.get("classification") != expected or expected not in TERMINAL:
        raise RecoveryError(f"classification must be {expected}")
    elapsed = (times[-1] - times[2]).total_seconds()
    clock = receipt.get("rollbackClock") or {}
    if clock.get("elapsedSeconds") != int(elapsed):
        raise RecoveryError("rollback clock does not match phase timestamps")
    budget = clock.get("budgetSeconds")
    if not isinstance(budget, int) or budget <= 0:
        raise RecoveryError("rollback clock budget must be a positive integer")
    if expected == "recovered" and elapsed > budget:
        raise RecoveryError("recovery exceeded the rollback clock")
    recovery = receipt.get("recovery") or {}
    if not recovery.get("command"):
        raise RecoveryError("receipt is missing the rollback command")
    if expected != "recovered" and not recovery.get("instructions"):
        raise RecoveryError("non-recovered receipt requires operator recovery instructions")
    if evidence_dir is not None:
        missing = [name for name in REQUIRED_EVIDENCE if not (evidence_dir / name).is_file() or not (evidence_dir / name).stat().st_size]
        if missing:
            raise RecoveryError("missing required recovery evidence: " + ", ".join(missing))
        declared = receipt.get("evidence") or {}
        for name in REQUIRED_EVIDENCE:
            if declared.get(name) != sha256_bytes(evidence_dir / name):
                raise RecoveryError(f"evidence digest mismatch: {name}")
    return receipt


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RecoveryError(f"{path} must contain a JSON object")
    return value


def build(args: argparse.Namespace) -> dict[str, Any]:
    evidence_dir = Path(args.evidence_dir)
    observation = _read_json(Path(args.observation))
    phase_times = _read_json(Path(args.phase_times))
    phases = [{"name": name, "at": phase_times[name]} for name in REQUIRED_PHASES]
    started = _timestamp(phase_times["recovery-started"], "recovery-started")
    terminal = _timestamp(phase_times["recovery-terminal"], "recovery-terminal")
    classification = classify(observation)
    evidence = {name: sha256_bytes(evidence_dir / name) for name in REQUIRED_EVIDENCE if (evidence_dir / name).is_file()}
    receipt = {
        "schema": "honua.upgrade-recovery-receipt/v1",
        "scenario": args.scenario,
        "bindings": {
            "candidateLockDigest": args.candidate_lock_digest,
            "priorLockDigest": args.prior_lock_digest,
            "candidateImageDigest": args.candidate_image_digest,
            "priorImageDigest": args.prior_image_digest,
            "workflowCommit": args.workflow_commit,
            "workflowRun": args.workflow_run,
        },
        "phases": phases,
        "rollbackClock": {
            "budgetSeconds": args.rollback_budget_seconds,
            "elapsedSeconds": int((terminal - started).total_seconds()),
        },
        "observation": observation,
        "recovery": {
            "command": args.rollback_command,
            "instructions": args.recovery_instructions,
        },
        "classification": classification,
        "evidence": evidence,
    }
    return validate(receipt, evidence_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    build_parser.add_argument("--candidate-lock-digest", required=True)
    build_parser.add_argument("--prior-lock-digest", required=True)
    build_parser.add_argument("--candidate-image-digest", required=True)
    build_parser.add_argument("--prior-image-digest", required=True)
    build_parser.add_argument("--workflow-commit", required=True)
    build_parser.add_argument("--workflow-run", required=True)
    build_parser.add_argument("--evidence-dir", required=True)
    build_parser.add_argument("--observation", required=True)
    build_parser.add_argument("--phase-times", required=True)
    build_parser.add_argument("--rollback-command", required=True)
    build_parser.add_argument("--recovery-instructions", default="")
    build_parser.add_argument("--rollback-budget-seconds", type=int, required=True)
    build_parser.add_argument("--output", required=True)
    validate_parser = sub.add_parser("validate")
    validate_parser.add_argument("--receipt", required=True)
    validate_parser.add_argument("--evidence-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            receipt = build(args)
            Path(args.output).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        else:
            validate(_read_json(Path(args.receipt)), Path(args.evidence_dir))
    except (OSError, KeyError, json.JSONDecodeError, RecoveryError) as exc:
        print(f"upgrade recovery receipt: FAIL: {exc}")
        return 1
    print("upgrade recovery receipt: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
