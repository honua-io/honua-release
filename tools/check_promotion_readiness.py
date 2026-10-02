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


class ReadinessError(ValueError):
    pass


def _time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z", value):
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
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ReadinessError(f"{field} contains duplicate JSON key {key!r}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ReadinessError(f"{field} contains a nonstandard JSON number")

    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_keys,
                           parse_constant=invalid_constant)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReadinessError(f"{field} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise ReadinessError(f"{field} must be a JSON object")
    return value


# R21: required classes and their production tier. Additional consumed classes must
# be explicitly declared too; declarations cannot change these mandated tiers.
EVIDENCE_CLASSES = {
    **dict.fromkeys(("build-test", "contract", "sbom", "security", "upgrade",
                     "capacity-soak", "dr", "lambda-certification", "protocol-ledger",
                     "deterministic-journey", "nightly-model-journey"), "nightly"),
    **dict.fromkeys(("genuine-model-journey", "update-rollback", "esri-bundle", "cite"), "qualifying"),
}
GA_CELLS = frozenset({"aws-ecs/redis-off", "aws-ecs/redis-on", "aws-serverless/redis-off", "aws-serverless/redis-on"})
# Policy maximum between a receipt's completion and its freshUntil. A producer may set a
# shorter bound but never a longer one, so the promotion window always closes. A declared
# class without a maximum here refuses.
MAX_FRESHNESS = {
    **dict.fromkeys(("build-test", "contract", "sbom", "security", "upgrade",
                     "capacity-soak", "dr", "lambda-certification", "protocol-ledger",
                     "deterministic-journey", "nightly-model-journey",
                     "genuine-model-journey", "update-rollback"), timedelta(days=7)),
    **dict.fromkeys(("esri-bundle", "cite"), timedelta(days=14)),
}
CANARY_SLOT = timedelta(hours=6, minutes=30)
JOURNEYS = {
    "deterministic-journey": ("deterministic", GA_CELLS),
    "nightly-model-journey": ("genuine-model", frozenset({"aws-ecs/redis-off"})),
    "genuine-model-journey": ("genuine-model", GA_CELLS),
}


def _journey(receipt: dict[str, Any], required: frozenset[str], mode: str,
             digest: str, earliest: datetime, latest: datetime) -> bool:
    """Evaluate the complete attempt ledger; Preview cells never count as GA."""
    cells = receipt.get("cells")
    if not isinstance(cells, list) or any(not isinstance(cell, dict) for cell in cells):
        return False
    selected = [cell for cell in cells if cell.get("cell") in required]
    if len(selected) != len(required) or {cell.get("cell") for cell in selected} != required:
        return False
    for cell in selected:
        attempts = cell.get("attempts")
        if (cell.get("mode") != mode or type(cell.get("attemptCount")) is not int
                or not isinstance(attempts, list) or len(attempts) not in (1, 2)
                or cell["attemptCount"] != len(attempts)):
            return False
        times = []
        for index, attempt in enumerate(attempts, 1):
            if not isinstance(attempt, dict):
                return False
            completed = _time(attempt.get("completedAt"), "journey attempt completedAt")
            times.append(completed)
            if (type(attempt.get("attempt")) is not int or attempt["attempt"] != index
                    or attempt.get("lockDigest") != digest or not earliest <= completed <= latest):
                return False
            if index == len(attempts):
                if attempt.get("status") != "pass":
                    return False
            elif attempt.get("status") != "fail" or attempt.get("failureAttribution") not in ("model", "infrastructure"):
                return False
        if times != sorted(times):
            return False
    return True


def _update_rollback(receipt: dict[str, Any]) -> bool:
    """Each GA cell appears once and passes both its update and its rollback."""
    cells = receipt.get("cells")
    if (not isinstance(cells, list) or any(not isinstance(cell, dict) for cell in cells)
            or receipt.get("updateStatus") != "pass" or receipt.get("rollbackStatus") != "pass"):
        return False
    names = [cell.get("cell") for cell in cells]
    return (len(names) == len(set(names)) == len(GA_CELLS) and set(names) == GA_CELLS
            and all(cell.get("updateStatus") == cell.get("rollbackStatus") == "pass" for cell in cells))


def _canary_key(run: dict[str, Any], field: str) -> tuple[str, datetime, Any, Any]:
    """Identity of one canary observation; the ledger may carry integer run ids and extra fields."""
    return (_run_id(run.get("runId"), f"{field} runId"), _time(run.get("completedAt"), f"{field} completedAt"),
            run.get("status"), run.get("lockDigest"))


def evaluate(
    record: dict[str, Any], *, lock_path: Path, evidence_dir: Path,
    lock_history: Path | None = None, now: datetime,
) -> tuple[dict[str, Any], list[str]]:
    # lock_history is accepted for old callers, but repository history does not
    # reset a burn. lock_path must be the selected lock's retained exact bytes.
    checks: dict[str, dict[str, Any]] = {}
    failures: list[str] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks[name] = {"status": "pass" if passed else "fail", "detail": detail}
        if not passed:
            failures.append(f"{name}: {detail}")

    check("record-schema", record.get("schemaVersion") == "promotion-evidence.v1",
          f"schemaVersion={record.get('schemaVersion')!r}")
    label = record.get("platformLabel")
    check("platform-label", isinstance(label, str) and bool(re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?-rc\.[1-9][0-9]*", label or "")),
          f"platformLabel={label!r}")
    lock = record.get("lock") if isinstance(record.get("lock"), dict) else {}
    digest = lock.get("digest")
    actual_digest = _digest(lock_path)
    check("lock-digest", isinstance(digest, str) and SHA256_RE.fullmatch(digest) is not None and digest == actual_digest,
          f"recorded={digest!r}; selected={actual_digest}")
    burn_start = _time(lock.get("burnStartedAt"), "lock.burnStartedAt")
    age = now - burn_start
    check("burn-duration", age >= timedelta(hours=48),
          f"lock burn age is {age.total_seconds() / 3600:.2f}h; required at least 48h")

    trains = record.get("strictTrains")
    rc_run_id = _run_id(record.get("rcTrainRunId"), "rcTrainRunId")
    train_ok = isinstance(trains, list) and len(trains) == 1 and isinstance(trains[0], dict)
    train_time = burn_start
    report: dict[str, Any] = {}
    if train_ok:
        row = trains[0]
        run_id = _run_id(row.get("runId"), "minting train runId")
        train_time = _time(row.get("completedAt"), "minting train completedAt")
        try:
            root = evidence_dir / "trains" / run_id
            report = _load(root / "gate-report.json", "minting train receipt")
            metadata = _load(root / "run.json", "minting train metadata")
            binding = report.get("candidate", {}).get("train", {})
            # Report freshness is checked at minting, rather than expiring the
            # certification itself before the mandated 48-hour burn can finish.
            from candidate_binding import validate_live_report
            valid_report, _ = validate_live_report(report, now=train_time)
            train_ok = (valid_report and str(binding.get("runId")) == run_id
                        and _digest(root / "platform-lock.json") == digest
                        and row.get("lockDigest") == digest and row.get("status") == "pass"
                        and metadata.get("updated_at") == row.get("completedAt")
                        and metadata.get("status") == "completed" and metadata.get("conclusion") == "success"
                        and train_time <= burn_start)
        except (ReadinessError, OSError, AttributeError):
            train_ok = False
    check("minting-train", train_ok, "one passing strict train minted the selected lock before burn start")
    check("exact-rc", bool(train_ok and str(trains[0].get("runId")) == rc_run_id),
          f"promotion source run {rc_run_id} must be the minting train")

    declarations = record.get("evidenceClasses")
    declaration_ok = (isinstance(declarations, dict)
                      and all(isinstance(key, str) and re.fullmatch(r"[a-z][a-z0-9-]*", key)
                              and value in ("nightly", "qualifying") for key, value in declarations.items())
                      and all(declarations.get(key) == value for key, value in EVIDENCE_CLASSES.items()))
    declarations = declarations if isinstance(declarations, dict) else {}
    rows = record.get("evidence")
    rows = rows if isinstance(rows, list) else []
    classes = [row.get("class") for row in rows if isinstance(row, dict)]
    consumed = report.get("evidenceClasses")
    declaration_ok &= (len(classes) == len(rows) and all(isinstance(key, str) for key in classes)
                       and len(set(classes)) == len(classes) and set(classes) == set(declarations)
                       and isinstance(consumed, list) and all(isinstance(key, str) for key in consumed)
                       and len(set(consumed)) == len(consumed)
                       and set(consumed) == {key for key, tier in declarations.items() if tier == "nightly"})
    check("evidence-declarations", bool(declaration_ok),
          "every consumed class is declared nightly or qualifying; all R21 classes are required")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("class"), str):
            continue
        name = row["class"]
        ok = name in declarations and bool(re.fullmatch(r"[a-z][a-z0-9-]*", name))
        detail = "passing retained workflow receipt, bound to this lock, fresh, and bounded by its class policy maximum"
        try:
            run_id = _run_id(row.get("runId"), f"{name} runId")
            completed = _time(row.get("completedAt"), f"{name} completedAt")
            if not ok:
                raise ReadinessError("undeclared evidence class")
            root = evidence_dir / "evidence" / name / run_id
            receipt = _load(root / "receipt.json", name)
            metadata = _load(root / "run.json", f"{name} metadata")
            expiry = _time(receipt.get("freshUntil"), f"{name} freshUntil")
            tier = declarations[name]
            ok &= (row.get("status") == receipt.get("status") == "pass"
                   and row.get("lockDigest") == receipt.get("lockDigest") == digest
                   and receipt.get("class") == name and receipt.get("kind") == tier
                   and str(receipt.get("runId")) == run_id
                   and receipt.get("completedAt") == row.get("completedAt")
                   and metadata.get("status") == "completed" and metadata.get("conclusion") == "success"
                   and completed <= now <= expiry and expiry > completed)
            if tier == "nightly":
                ok &= (run_id == rc_run_id and completed <= train_time
                       and _time(metadata.get("created_at", metadata.get("updated_at")), "run created_at")
                       <= completed <= _time(metadata.get("updated_at"), "run updated_at"))
                earliest, latest = train_time - timedelta(hours=24), completed
            else:
                ok &= burn_start <= completed <= now and row.get("completedAt") == metadata.get("updated_at")
                earliest, latest = burn_start, completed
            ok &= name in MAX_FRESHNESS and expiry - completed <= MAX_FRESHNESS.get(name, timedelta(0))
            if name in JOURNEYS:
                mode, required = JOURNEYS[name]
                ok &= _journey(receipt, required, mode, digest, earliest, latest)
            if name == "update-rollback":
                ok &= _update_rollback(receipt)
        except (ReadinessError, OSError, TypeError):
            ok = False
        check(f"evidence:{name}", bool(ok), detail)

    canaries = record.get("demoCanaries")
    if not isinstance(canaries, list):
        raise ReadinessError("demoCanaries must be an array")
    canary_ok = len(canaries) == 7
    times: list[datetime] = []
    ids: list[str] = []
    for row in canaries:
        if not isinstance(row, dict):
            canary_ok = False
            continue
        run_id = _run_id(row.get("runId"), "canary runId")
        completed = _time(row.get("completedAt"), "canary completedAt")
        ids.append(run_id)
        times.append(completed)
        try:
            root = evidence_dir / "canaries" / run_id
            receipt = _load(root / "live-canary-evidence.json", f"canary {run_id}")
            metadata = _load(root / "run.json", f"canary {run_id} metadata")
            canary_ok &= (row.get("status") == receipt.get("status") == "pass"
                          and row.get("lockDigest") == receipt.get("candidateLock", {}).get("digest") == digest
                          and str(receipt.get("runId")) == run_id
                          and metadata.get("updated_at") == row.get("completedAt")
                          and metadata.get("status") == "completed" and metadata.get("conclusion") == "success")
        except (ReadinessError, AttributeError):
            canary_ok = False
    canary_ok &= len(set(ids)) == len(ids)
    if len(times) == 7:
        canary_ok &= times == sorted(times) and times[0] >= burn_start
        canary_ok &= all(timedelta(hours=5, minutes=30) <= b - a <= CANARY_SLOT
                         for a, b in zip(times, times[1:]))
        canary_ok &= timedelta(0) <= now - times[-1] <= CANARY_SLOT
    # The fetcher supplies the complete observed canary ledger, including failed
    # runs and their lock bindings. A record cannot omit a failure of this lock, and
    # it cannot move burnStartedAt past one: the scan starts at the minting train's
    # completion, which the record does not control.
    burn_detail = "no failed or incomplete canary of this lock, or unattributed failure, since minting"
    start_ok = False
    try:
        sequence = _load(evidence_dir / "canary-sequence.json", "complete canary sequence")
        runs = sequence.get("runs")
        if not isinstance(runs, list) or sequence.get("lockDigest") != digest:
            raise ReadinessError("missing lock-bound canary sequence")
        observed = []
        burn_ok = True
        for run in runs:
            if not isinstance(run, dict):
                raise ReadinessError("malformed canary sequence")
            key = _canary_key(run, "observed canary")
            completed, status, bound = key[1:]
            if completed > now:
                continue
            if bound == digest:
                observed.append(key)
                if status != "pass":
                    burn_ok = False
                    burn_detail = f"canary {key[0]} of this lock did not pass; the lock's burn has ended"
            elif not (isinstance(bound, str) and SHA256_RE.fullmatch(bound)):
                # A canary that fails before it reads its lock cannot be attributed to another
                # lock, so it counts against every lock that was minted before it ran.
                if status != "pass" and completed >= train_time:
                    burn_ok = False
                    burn_detail = f"canary {key[0]} did not pass and recorded no lock digest; it is unattributable"
        observed.sort(key=lambda key: key[1])
        observed_ids = [key[0] for key in observed]
        burn_ok &= len(set(observed_ids)) == len(observed_ids)
        selected = [_canary_key(row, "canary") for row in canaries if isinstance(row, dict)]
        canary_ok &= len(observed) >= 7 and observed[-7:] == selected
        # The lock is deployed when burn starts, so its first canary follows within one slot.
        # A burn start claimed earlier than that would shorten the real burn.
        start_ok = bool(observed) and observed[0][1] - burn_start <= CANARY_SLOT
    except (ReadinessError, OSError):
        burn_ok = canary_ok = False
        burn_detail = "complete lock-bound canary sequence is missing or malformed"
    check("burn-start", start_ok,
          "the lock's first observed canary completed no more than 6h30m after the recorded burn start")
    check("lock-burn-health", bool(burn_ok), burn_detail)
    check("demo-canaries", bool(canary_ok),
          "seven consecutive passing lock-bound 6-hour canaries, latest no more than 6.5h old")
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
    parser.add_argument("--lock-history", type=Path, help="Legacy input; trunk history never resets a lock burn")
    parser.add_argument("--now")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    try:
        now = _time(args.now, "now") if args.now else datetime.now(timezone.utc)
        decision, failures = evaluate(_load(args.record, "promotion record"), lock_path=args.lock,
                                      evidence_dir=args.evidence_dir, lock_history=args.lock_history, now=now)
    except (ReadinessError, OSError, TypeError) as exc:
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
