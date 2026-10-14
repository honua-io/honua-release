#!/usr/bin/env python3
"""Controller-only struggle signals, never model feedback or qualification.

Records observable actions and outcomes, not private reasoning or raw commands.
Repeated actions are diagnostic signals: they can also be legitimate polling.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import json
import os
from pathlib import Path
import time

from agent_e2e_pack import canonical_digest


class TelemetryError(ValueError):
    pass


FIELDS = {
    "start": {"modelId", "returnedModelId"},
    "model": {"turn", "durationMs", "inputTokens", "outputTokens", "costUsd", "responseDigest"},
    "action": {"action", "kind", "operation", "requestDigest", "resultDigest", "durationMs", "isError", "errorCategory"},
    "verification": {"status", "failedAssertions"},
    "end": {"status", "failureAttribution", "reason", "cleanupStatus"},
}
COMMON = {"schemaVersion", "campaignId", "caseId", "attempt", "sequence", "elapsedMs", "previousDigest", "event"}


class Journal:
    """Exclusive immutable attempt identity; append and fsync before paid work.

    Retain the whole directory in controller storage across restarts. A missing
    end event is an interrupted attempt, never permission to start it again.
    Hash chaining detects edits but does not replace controller attestation.
    """
    def __init__(self, root, context):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.context = {k: context[k] for k in ("campaignId", "caseId", "attempt")}
        key = canonical_digest(self.context).split(":", 1)[1]
        self.path = root / (key + ".jsonl")
        self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        self.started = time.monotonic()
        self.sequence, self.previous = 0, None

    def emit(self, event, **values):
        if event not in FIELDS or set(values) != FIELDS[event]:
            raise TelemetryError("unreviewed telemetry fields")
        row = {"schemaVersion": 1, **self.context, "sequence": self.sequence,
               "elapsedMs": round((time.monotonic() - self.started) * 1000),
               "previousDigest": self.previous, "event": event, **values}
        encoded = (json.dumps(row, sort_keys=True, allow_nan=False) + "\n").encode()
        # os.write may return a short write; do not silently truncate the journal.
        view = memoryview(encoded)
        while view:
            written = os.write(self.fd, view)
            if written <= 0:
                raise TelemetryError("journal write failed")
            view = view[written:]
        os.fsync(self.fd)
        self.previous = canonical_digest(row)
        self.sequence += 1

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def read_events(path):
    rows = []
    previous, identity = None, None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        event = row.get("event")
        if event not in FIELDS or set(row) != COMMON | FIELDS[event]:
            raise TelemetryError("unexpected telemetry event/fields")
        if row["sequence"] != len(rows) or row["previousDigest"] != previous:
            raise TelemetryError("broken telemetry chain")
        current = tuple(row[k] for k in ("campaignId", "caseId", "attempt"))
        if identity is not None and identity != current:
            raise TelemetryError("mixed attempt identity")
        identity = current
        if type(row["elapsedMs"]) is not int or row["elapsedMs"] < 0 or (rows and row["elapsedMs"] < rows[-1]["elapsedMs"]):
            raise TelemetryError("invalid elapsed time")
        rows.append(row)
        previous = canonical_digest(row)
    if not rows or rows[0]["event"] != "start" or sum(r["event"] == "start" for r in rows) != 1:
        raise TelemetryError("missing/duplicate start")
    ends = [i for i, r in enumerate(rows) if r["event"] == "end"]
    if ends and ends != [len(rows) - 1]:
        raise TelemetryError("events after completion")
    return rows


def summarize(rows):
    first, last = rows[0], rows[-1]
    actions = [r for r in rows if r["event"] == "action"]
    models = [r for r in rows if r["event"] == "model"]
    same, longest, previous = 0, 0, None
    for action in actions:
        pair = (action["requestDigest"], action["resultDigest"])
        same = same + 1 if pair == previous else 1
        longest, previous = max(longest, same), pair
    counts = Counter(r["requestDigest"] for r in actions)
    errors = sum(r["isError"] is True for r in actions)
    parts = first["caseId"].split(":")
    return {"campaignId": first["campaignId"], "caseId": first["caseId"], "attempt": first["attempt"],
            "scenarioId": parts[0], "variant": parts[1], "target": parts[2],
            "status": last["status"] if last["event"] == "end" else "interrupted",
            "failureAttribution": last.get("failureAttribution"), "reason": last.get("reason"),
            "modelTurns": len(models), "actions": len(actions), "toolErrors": errors,
            "agentEncounteredCorpus": bool(models),
            "toolErrorRate": errors / len(actions) if actions else None,
            "clarificationRequests": sum(r["kind"] == "ask_user" for r in actions),
            "repeatedRequests": sum(max(0, n - 1) for n in counts.values()),
            "longestUnchangedRepeat": longest,
            "possibleStall": longest >= 3,
            "elapsedMs": last["elapsedMs"],
            "inputTokens": sum(r["inputTokens"] for r in models),
            "outputTokens": sum(r["outputTokens"] for r in models),
            "modelCostUsd": str(sum((Decimal(r["costUsd"]) for r in models), Decimal(0))),
            "operations": dict(Counter(r["operation"] for r in actions)),
            "failedAssertions": [a for r in rows if r["event"] == "verification" for a in r["failedAssertions"]],
            "errorCategories": dict(Counter(r["errorCategory"] for r in actions if r["isError"]))}


def report(paths):
    attempts = [summarize(read_events(path)) for path in paths]
    identities = [(r["campaignId"], r["caseId"], r["attempt"]) for r in attempts]
    if len(identities) != len(set(identities)):
        raise TelemetryError("duplicate attempt journals")
    groups = defaultdict(list)
    for row in attempts:
        groups[(row["scenarioId"], row["variant"], row["target"])].append(row)
    return {"schemaVersion": 1, "qualification": False,
            "interpretation": "Observed signals only. Repetition may be polling; clarification may be correct. No success or coverage inference from missing runs.",
            "attemptsObserved": len(attempts), "casesObserved": len(groups),
            "requiredCases": 252, "attempts": attempts,
            "attention": sorted([r for r in attempts if r["status"] != "pass" or r["possibleStall"] or r["toolErrors"]],
                                key=lambda r: (-r["toolErrors"], -r["longestUnchangedRepeat"], -r["elapsedMs"]))}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--journals", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args(argv)
    value = report(sorted(args.journals.glob("*.jsonl")))
    args.output.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
