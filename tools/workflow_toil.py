#!/usr/bin/env python3
"""Opt-in GA experiment counters; never infer execution or success from model prose.

The canary owns stage boundaries and emits counters after its normal verification.
This module retains no prompts, tool arguments, credential values, or responses.
Reports require matching pins and retain failed attempts in their denominators.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any, Callable

ARMS = ("imperative", "spec")
METRICS = ("modelCalls", "toolCalls", "terminalCommands", "inputTokens", "outputTokens",
           "modelMs", "toolMs", "wallMs", "approvalMs", "modelRetries", "formatRepairs")
TOKEN_METRICS = ("inputTokens", "outputTokens")


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def validate_config(config: dict[str, Any]) -> None:
    if set(config) != {"experimentId", "arm", "trial"}:
        raise ValueError("toil configuration requires exactly experimentId, arm and trial")
    if (not isinstance(config["experimentId"], str) or not config["experimentId"].strip()
            or len(config["experimentId"]) > 128):
        raise ValueError("experimentId must be a nonempty identifier of at most 128 characters")
    if config["arm"] not in ARMS or type(config["trial"]) is not int or not 1 <= config["trial"] <= 5:
        raise ValueError("toil arm must be imperative/spec and trial must be 1..5")


class ToilRecorder:
    """Bounded per-stage observation of one canary attempt, with unknown usage as null."""

    def __init__(self, config: dict[str, Any], stage_ids: list[str], *,
                 clock: Callable[[], float] = time.monotonic):
        validate_config(config)
        if not stage_ids or len(set(stage_ids)) != len(stage_ids):
            raise ValueError("journey stages must be nonempty and unique")
        self.config = dict(config)
        self.clock = clock
        self.rows = {key: {"stageId": key, "status": "not-run",
                          "metrics": dict.fromkeys(METRICS, 0)} for key in stage_ids}
        self.started: dict[str, float] = {}
        self.failed_tools: dict[str, set[str]] = {key: set() for key in stage_ids}
        self.unmeasured = ["humanReviewMs", "clarificationTurns", "validationFailures", "validationRepairs", "transportRetries"]

    def start(self, stage_id: str) -> None:
        if stage_id in self.started:
            raise ValueError("a journey stage cannot be started twice")
        self.rows[stage_id]["status"] = "running"
        self.started[stage_id] = self.clock()

    def model_call(self, stage_id: str, usage: dict[str, Any], elapsed_ms: int) -> None:
        row = self._active(stage_id)
        if not _number(elapsed_ms):
            raise ValueError("invalid model elapsed time")
        for source in ("prompt_tokens", "completion_tokens"):
            value = usage.get(source)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("token counts must be nonnegative integers or unavailable")
        row["modelCalls"] += 1
        row["modelMs"] += elapsed_ms
        for output, source in (("inputTokens", "prompt_tokens"), ("outputTokens", "completion_tokens")):
            value = usage.get(source)
            row[output] = None if value is None or row[output] is None else row[output] + value

    def action(self, stage_id: str, kind: str, operation: str, status: str, elapsed_ms: int) -> None:
        row = self._active(stage_id)
        if kind not in ("tool_call", "terminal_command") or status not in ("pass", "fail"):
            raise ValueError("invalid model-selected action observation")
        if not _number(elapsed_ms) or not operation:
            raise ValueError("invalid action identity or duration")
        row["toolCalls" if kind == "tool_call" else "terminalCommands"] += 1
        row["toolMs"] += elapsed_ms
        # A retry is a model reissue of a failed operation, not a failed parse,
        # a transport retry, or an unrelated corrective command.
        key = kind + ":" + operation
        failed = self.failed_tools[stage_id]
        if key in failed:
            row["modelRetries"] += 1
        if status == "fail":
            failed.add(key)
        else:
            failed.discard(key)

    def repair(self, stage_id: str) -> None:
        self._active(stage_id)["formatRepairs"] += 1

    def approval(self, stage_id: str, elapsed_ms: int) -> None:
        if not _number(elapsed_ms):
            raise ValueError("invalid human review duration")
        self._active(stage_id)["approvalMs"] += elapsed_ms

    def finish(self, stage_id: str, status: str) -> None:
        row = self._active(stage_id)
        if status not in ("pass", "fail", "blocked"):
            raise ValueError("invalid stage outcome")
        row["wallMs"] = max(0, round((self.clock() - self.started[stage_id]) * 1000))
        self.rows[stage_id]["status"] = status

    def _active(self, stage_id: str) -> dict[str, Any]:
        if self.rows[stage_id]["status"] != "running":
            raise ValueError("metric requires an active journey stage")
        return self.rows[stage_id]["metrics"]

    def snapshot(self, *, bindings: dict[str, Any], status: str) -> dict[str, Any]:
        rows = copy.deepcopy(list(self.rows.values()))
        for row in rows:
            if row["status"] == "running":
                row["metrics"]["wallMs"] = max(0, round(
                    (self.clock() - self.started[row["stageId"]]) * 1000))
                row["status"] = "fail" if status == "fail" else "blocked"
        return {"schemaVersion": "toil.v1", "emitter": "terminal-model-canary",
                **self.config, "bindings": copy.deepcopy(bindings), "status": status,
                "stages": rows, "unmeasured": list(self.unmeasured)}


def compare(records: list[dict[str, Any]], *, trials: int = 5) -> dict[str, Any]:
    """Compare complete paired cohorts only; report missing evidence without a benefit claim."""
    if type(trials) is not int or not 1 <= trials <= 5:
        raise ValueError("trials must be 1..5")
    if not records:
        raise ValueError("no toil receipts supplied")
    baseline = records[0]
    expected_bindings = baseline.get("bindings")
    required_bindings = {"model", "modelSettingsDigest", "serverDigest", "clientArtifactsDigest",
                         "fixtureRevision", "configRevision", "authPolicyRevision", "journeyDigest", "environment"}
    if not isinstance(expected_bindings, dict) or set(expected_bindings) != required_bindings:
        raise ValueError("comparison requires complete candidate, model and fixture bindings")
    if any(not isinstance(value, str) or not value for value in expected_bindings.values()):
        raise ValueError("comparison bindings cannot be empty or unavailable")
    stages = [row["stageId"] for row in baseline["stages"]]
    if not stages or len(stages) != len(set(stages)):
        raise ValueError("invalid stage roster")
    arms: dict[str, dict[int, dict[str, Any]]] = {arm: {} for arm in ARMS}
    for record in records:
        validate_config({key: record.get(key) for key in ("experimentId", "arm", "trial")})
        if (record.get("schemaVersion") != "toil.v1" or record.get("emitter") != "terminal-model-canary"
                or record["experimentId"] != baseline["experimentId"]
                or record.get("bindings") != expected_bindings):
            raise ValueError("comparison requires one experiment with identical pins and environment")
        if record["trial"] in arms[record["arm"]]:
            raise ValueError("duplicate arm/trial; do not replace failed attempts with retries")
        if record["trial"] > trials:
            raise ValueError("trial exceeds the declared cohort")
        if record.get("status") not in ("pass", "fail", "blocked", "skipped"):
            raise ValueError("invalid attempt status")
        if [row["stageId"] for row in record["stages"]] != stages:
            raise ValueError("stage roster differs between attempts")
        for row in record["stages"]:
            if row["status"] not in ("pass", "fail", "blocked", "not-run"):
                raise ValueError("invalid stage status")
            if record["status"] == "pass" and row["status"] != "pass":
                raise ValueError("passing attempt contains an incomplete stage")
            if set(row["metrics"]) != set(METRICS):
                raise ValueError("metric contract differs")
            for key, value in row["metrics"].items():
                if value is None and key in TOKEN_METRICS:
                    continue
                if not _number(value) or (not key.endswith("Ms") and type(value) is not int):
                    raise ValueError("invalid metric value")
        arms[record["arm"]][record["trial"]] = record
    missing = [{"arm": arm, "trial": n} for arm in ARMS for n in range(1, trials + 1)
               if n not in arms[arm]]
    report: dict[str, Any] = {"schemaVersion": "toil-comparison.v1", "experimentId": baseline["experimentId"],
                              "bindings": expected_bindings, "status": "incomplete" if missing else "complete",
                              "missing": missing, "arms": {}}
    for arm, attempts in arms.items():
        values = list(attempts.values())
        counts = {key: sum(r["status"] == key for r in values) for key in ("pass", "fail", "blocked", "skipped")}
        stage_reports = []
        for index, stage_id in enumerate(stages):
            attempted = [r["stages"][index] for r in values if r["stages"][index]["status"] != "not-run"]
            metrics = {}
            for metric in METRICS:
                observed = [r["metrics"][metric] for r in attempted if r["metrics"][metric] is not None]
                metrics[metric] = {"median": statistics.median(observed) if observed else None,
                                   "observed": len(observed), "missing": len(attempted) - len(observed)}
            stage_reports.append({"stageId": stage_id, "attempted": len(attempted),
                                  "passed": sum(r["status"] == "pass" for r in attempted), "metrics": metrics})
        report["arms"][arm] = {"attempts": len(values), "outcomes": counts,
                                "completionRate": counts["pass"] / len(values) if values else None,
                                "stages": stage_reports}
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipts", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        records = [json.loads(path.read_text()) for path in args.receipts]
        report = compare(records)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, f"invalid toil evidence: {exc}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
