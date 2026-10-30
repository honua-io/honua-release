#!/usr/bin/env python3
"""Arc-by-target coverage of what a live model actually ran.

Rows are bootstrap, data, publish, style/render, GP and operate. Columns are
local-docker, ECS, EKS and Lambda. A cell is unqualified unless a live model's
first attempt supplies it. functionalGpExecuted false, and a serving-plus-refusal
result, do not cover GP or operate. Saved-map restore, restart and Lambda
configuration changes do not cover operate.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


SCHEMA = "arc-coverage.v1"
ATTEMPT_SCHEMA = "live-model-attempt.v1"
ARCS = ("bootstrap", "data", "publish", "style-render", "gp", "operate")
TARGETS = ("local-docker", "aws-ecs", "aws-eks", "aws-lambda")
TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$")
SCENARIO_ARCS = {
    **dict.fromkeys(("P01", "P02", "L01"), "bootstrap"),
    **dict.fromkeys(("P03", "P04", "P05", "P06", "P07", "L02"), "data"),
    **dict.fromkeys(("P14", "P15", "P16", "P20"), "publish"),
    **dict.fromkeys(("P08", "P09", "P10", "L03"), "style-render"),
    "P11": "gp",
    **{f"O{i:02}": "operate" for i in range(1, 13)},
}
# Restoration, restart, cold start, refusal and cleanup are not the operate arc.
NOT_OPERATE = {"P12", "P13", "L04", "L05", "L06", "P21", "P22", "P23"}
STAGE_ARCS = {
    "installed-client-handoff": ("bootstrap",),
    "credential-preflight": ("bootstrap",),
    "publish-service": ("data", "publish"),
    "style-render": ("style-render",),
    "bounded-gp": ("gp",),
    "publication-proposal": ("publish",),
    "separate-principal-approval": ("publish",),
}
MODEL_JOURNEYS = {"nightly-model-journey", "genuine-model-journey"}


class CoverageError(ValueError):
    pass


def serving_refusal_only(record):
    """True when the cell only shows serving plus a refusal, or did not execute GP."""
    if not isinstance(record, dict):
        return False
    if record.get("profile") == "redis-off-serving-refusal":
        return True
    outcome = record.get("executionOutcome", record.get("outcome"))
    if outcome == "refused" and record.get("functionalGpExecuted") is not True:
        return True
    return False


def _passed(record):
    return record.get("status") in (None, "pass")


def covers_gp(record):
    """Functional GP on a completed full run. Refusal and functionalGpExecuted false do not count."""
    if not isinstance(record, dict) or serving_refusal_only(record) or not _passed(record):
        return False
    if record.get("functionalGpExecuted") is not True:
        return False
    outcome = record.get("executionOutcome", record.get("outcome"))
    return outcome in ("completed", "succeeded", "pass")


def covers_operate(record):
    """An explicit operate witness. A GP flag, a refusal, or a restored map does not count."""
    if not isinstance(record, dict) or serving_refusal_only(record) or not _passed(record):
        return False
    if record.get("functionalGpExecuted") is False or record.get("operateDemonstrated") is not True:
        return False
    if record.get("scenarioId") in NOT_OPERATE:
        return False
    outcome = record.get("executionOutcome", record.get("outcome"))
    return outcome in ("completed", "succeeded", "pass")


def _claims(record):
    claimed = set()
    if not isinstance(record, dict):
        return claimed
    if record.get("coversGp") is True:
        claimed.add("gp")
    if record.get("coversOperate") is True:
        claimed.add("operate")
    covers = record.get("covers")
    if isinstance(covers, list):
        claimed.update(item for item in covers if item in ("gp", "operate"))
    return claimed


def false_gp_operate_claim(record):
    """True when the record says it covers GP or operate and the witness does not."""
    claimed = _claims(record)
    if "gp" in claimed and not covers_gp(record):
        return True
    if "operate" in claimed and not covers_operate(record):
        return True
    return False


def attempt_index(record):
    """Promotion receipts number an attempt with `attempt`. Journey reports use `number`."""
    if not isinstance(record, dict):
        return None
    if "attempt" in record:
        return record.get("attempt")
    if "number" in record:
        return record.get("number")
    return None


def journey_cell_credit(cell):
    """(label, gp, operate, excluded, false_claim) from the cell's first attempt."""
    if not isinstance(cell, dict):
        return None, False, False, False, True
    attempts = cell.get("attempts") if isinstance(cell.get("attempts"), list) else []
    indexed = [row for row in attempts if isinstance(row, dict)]
    first = next((row for row in indexed if attempt_index(row) == 1), None)
    label = cell.get("cell") if isinstance(cell.get("cell"), str) else None
    # A later attempt is recorded and does not qualify the cell.
    if indexed and first is None:
        return label, False, False, False, false_gp_operate_claim(cell)
    subject = {**cell, **first} if first else cell
    gp = covers_gp(subject)
    operate = covers_operate(subject)
    excluded = serving_refusal_only(subject) or subject.get("functionalGpExecuted") is False
    false_claim = false_gp_operate_claim(cell) or false_gp_operate_claim(subject) or (excluded and (gp or operate))
    return label, gp, operate, excluded, false_claim


def target_of(value):
    if not isinstance(value, str):
        return None
    if value == "local-docker":
        return "local-docker"
    if value == "aws-ecs" or value.startswith("aws-ecs/") or value.startswith("aws-ecs-"):
        return "aws-ecs"
    if value == "aws-eks" or value.startswith("aws-eks/") or value.startswith("aws-eks-"):
        return "aws-eks"
    if value in ("aws-lambda", "aws-serverless") or value.startswith("aws-lambda") or value.startswith("aws-serverless/"):
        return "aws-lambda"
    return None


def _require_time(value, label):
    if not isinstance(value, str) or TIMESTAMP.fullmatch(value) is None:
        raise CoverageError(f"{label} must be an RFC3339 UTC timestamp")
    return value


def _outcome(record):
    if record.get("status") in ("pass", "fail", "blocked"):
        return record["status"]
    outcome = record.get("outcome", record.get("executionOutcome"))
    if outcome in ("pass", "completed", "succeeded"):
        return "pass"
    if outcome in ("fail", "failed", "refused"):
        return "fail"
    return "undetermined"


def _row(arc, target, scenario_id, record):
    if arc not in ARCS or target not in TARGETS:
        return None
    if record.get("liveModel") is not True or record.get("attempt") != 1:
        return None
    if scenario_id in NOT_OPERATE or (arc == "operate" and record.get("operateDemonstrated") is not True):
        return None
    outcome = _outcome(record)
    # A passing GP or operate cell needs a positive witness. Serving plus refusal,
    # and functionalGpExecuted false, are not that witness. A failed attempt of the
    # arc stays in the table so it is not reported as unqualified.
    if arc in ("gp", "operate") and serving_refusal_only(record):
        return None
    if arc == "gp" and outcome == "pass" and not covers_gp(record):
        return None
    if arc == "operate" and (record.get("functionalGpExecuted") is False or (outcome == "pass" and not covers_operate(record))):
        return None
    return {
        "arc": arc, "target": target, "scenarioId": scenario_id,
        "runId": str(record.get("runId", "")),
        "completedAt": _require_time(record.get("completedAt"), "completedAt"),
        "firstAttemptOutcome": _outcome(record), "liveModel": True,
    }


def _from_attempt(document):
    if document.get("schemaVersion") != ATTEMPT_SCHEMA or document.get("liveModel") is not True:
        return []
    scenario = document.get("scenarioId")
    if not isinstance(scenario, str):
        raise CoverageError("live model attempt is missing a scenario")
    # An unknown target, including Batch, has no column. It is not coverage.
    target = target_of(document.get("target"))
    if target is None:
        return []
    if scenario in NOT_OPERATE:
        return []
    arc = document.get("arc") if document.get("arc") in ARCS else SCENARIO_ARCS.get(scenario)
    if arc is None:
        return []
    if arc == "operate":
        document = {**document, "operateDemonstrated": document.get("operateDemonstrated", True)}
    if arc == "gp" and document.get("outcome") in ("pass", "completed", "succeeded") and "functionalGpExecuted" not in document:
        document = {**document, "functionalGpExecuted": True, "executionOutcome": "completed"}
    attempt = document["attempt"] if "attempt" in document else 1
    row = _row(arc, target, scenario, {**document, "liveModel": True, "attempt": attempt})
    return [row] if row else []


def _genuine_model_cell(cell, document):
    """A live-model cell. Deterministic mode and a serving receipt are not one."""
    mode = cell.get("mode")
    if mode == "genuine-model":
        return True
    if mode is not None:
        return False
    report = document.get("source") == "terminal-model-canary" or document.get("gate") in MODEL_JOURNEYS
    if not report:
        return False
    drivers = cell.get("drivers")
    attempts = cell.get("attempts") if isinstance(cell.get("attempts"), list) else []
    return cell.get("driver") == "genuine-model" or (
        isinstance(drivers, list) and "genuine-model" in drivers) or any(
            isinstance(row, dict) and row.get("driver") == "genuine-model" for row in attempts)


def _from_journey(document):
    if document.get("source") not in (None, "terminal-model-canary") and document.get("gate") not in MODEL_JOURNEYS:
        return []
    cells = document.get("cells")
    if not isinstance(cells, list):
        return []
    rows = []
    run_id = document.get("runId")
    for cell in cells:
        if not isinstance(cell, dict) or not _genuine_model_cell(cell, document):
            continue
        target = target_of(cell.get("cell"))
        attempts = cell.get("attempts") if isinstance(cell.get("attempts"), list) else [cell]
        for attempt in attempts:
            if not isinstance(attempt, dict) or attempt_index(attempt) not in (1, None):
                continue
            subject = {**cell, **attempt, "liveModel": True, "attempt": 1,
                       "runId": attempt.get("runId", run_id),
                       "completedAt": attempt.get("completedAt", document.get("generatedAt"))}
            stages = attempt.get("stages") if isinstance(attempt.get("stages"), list) else []
            emitted = False
            for stage in stages:
                if not isinstance(stage, dict) or stage.get("status") != "pass":
                    continue
                for arc in STAGE_ARCS.get(stage.get("id"), ()):
                    if arc == "gp" and not covers_gp(subject):
                        continue
                    row = _row(arc, target, stage["id"], subject)
                    if row:
                        rows.append(row)
                        emitted = True
            if not emitted and covers_gp(subject) and target:
                row = _row("gp", target, "bounded-gp", subject)
                if row:
                    rows.append(row)
    return rows


def load_evidence(path):
    """Load live-model attempts. A missing path is no evidence, not a covered cell."""
    path = Path(path)
    if not path.exists():
        return []
    files = [path] if path.is_file() else sorted(item for item in path.rglob("*.json") if item.is_file())
    rows = []
    for file in files:
        try:
            document = json.loads(file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CoverageError(f"malformed coverage evidence: {file.name}") from exc
        if isinstance(document, list):
            documents = document
        elif isinstance(document, dict):
            documents = [document]
        else:
            raise CoverageError(f"coverage evidence must be an object: {file.name}")
        for item in documents:
            if not isinstance(item, dict):
                raise CoverageError(f"coverage evidence must be an object: {file.name}")
            if item.get("schemaVersion") == ATTEMPT_SCHEMA:
                rows.extend(_from_attempt(item))
            elif item.get("schemaVersion") == "arc-coverage-evidence.v1" and isinstance(item.get("attempts"), list):
                for attempt in item["attempts"]:
                    if isinstance(attempt, dict):
                        rows.extend(_from_attempt({**attempt, "schemaVersion": ATTEMPT_SCHEMA}))
            else:
                rows.extend(_from_journey(item))
    return [row for row in rows if row and row["runId"]]


def build_table(paths):
    evidence = []
    for path in paths:
        evidence.extend(load_evidence(path))
    cells = []
    for arc in ARCS:
        for target in TARGETS:
            matched = [row for row in evidence if row["arc"] == arc and row["target"] == target]
            if not matched:
                cells.append({"arc": arc, "target": target, "coverage": "unqualified"})
                continue
            matched.sort(key=lambda row: row["completedAt"])
            last = matched[-1]
            scenarios = sorted({row["scenarioId"] for row in matched})
            cells.append({"arc": arc, "target": target, "coverage": {
                "scenarioIds": scenarios,
                "lastRun": {"runId": last["runId"], "completedAt": last["completedAt"]},
                "firstAttemptOutcome": last["firstAttemptOutcome"],
            }})
    return {"schemaVersion": SCHEMA, "arcs": list(ARCS), "targets": list(TARGETS), "cells": cells}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        table = build_table(args.evidence)
    except CoverageError as exc:
        print(f"arc-coverage: {exc}")
        return 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8")
    unqualified = sum(cell["coverage"] == "unqualified" for cell in table["cells"])
    print(f"arc-coverage: {len(table['cells']) - unqualified} observed, {unqualified} unqualified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
