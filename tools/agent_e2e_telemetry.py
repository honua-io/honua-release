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
import math
import re
from pathlib import Path
import time
import tempfile

from agent_e2e_pack import canonical_digest


class TelemetryError(ValueError):
    pass


FIELDS = {
    "start": {"modelId", "returnedModelId"},
    "context": {"campaignMode", "knowledge", "conceptIds", "workflowIds"},
    "diagnosis": {"attribution", "basis", "evidenceDigest"},
    "discovery": {"interfaceKind", "operation", "schemaDigest", "observationDigest"},
    "knowledge-read": {"documentId", "documentDigest", "bundleDigest"},
    "provider-request": {"turn", "requestId"},
    "runtime-trace": {"action", "traceId", "spanId", "requestId", "evidenceDigest"},
    "model": {"turn", "durationMs", "inputTokens", "outputTokens", "costUsd", "responseDigest"},
    "action-intent": {"action", "kind", "requestDigest"},
    "action": {"action", "kind", "operation", "requestDigest", "resultDigest", "durationMs", "isError", "errorCategory"},
    "verification": {"status", "failedAssertions"},
    "end": {"status", "failureAttribution", "reason", "cleanupStatus"},
}
COMMON = {"schemaVersion", "campaignId", "caseId", "attempt", "sequence", "elapsedMs", "previousDigest", "event"}


def validate_identity(context):
    case = context.get("caseId")
    if not isinstance(case, str) or not re.fullmatch(
            r"(?:P(?:0[1-9]|1[0-9]|2[0-4]):[ABC]:(?:local-docker|aws-ecs-redis-on|aws-eks)|"
            r"L0[1-6]:[ABC]:aws-lambda|B0[1-6]:[ABC]:aws-batch)", case):
        raise TelemetryError("case outside frozen telemetry inventory")
    if type(context.get("attempt")) is not int or context["attempt"] not in (1, 2):
        raise TelemetryError("invalid attempt identity")
    if not isinstance(context.get("campaignId"), str) or not context["campaignId"]:
        raise TelemetryError("missing campaign identity")


def validate_values(event, values):
    if event not in FIELDS or set(values) != FIELDS[event]:
        raise TelemetryError("unreviewed telemetry fields")
    if event == "context" and values["campaignMode"] not in ("development", "qualification"):
        raise TelemetryError("invalid campaign mode")
    if event in ("end", "verification") and values["status"] not in ("pass", "fail", "blocked"):
        raise TelemetryError("invalid observed status")
    if event == "end":
        if values["failureAttribution"] not in (None, "model", "product", "infrastructure", "undetermined"):
            raise TelemetryError("invalid failure attribution")
        if values["cleanupStatus"] not in ("pass", "fail", "blocked", "not-started"):
            raise TelemetryError("invalid cleanup status")
    for key in ("inputTokens", "outputTokens", "durationMs", "turn", "action"):
        if key in values and (type(values[key]) is not int or values[key] < 0):
            raise TelemetryError("invalid nonnegative telemetry count")
    if "costUsd" in values:
        try:
            cost = Decimal(values["costUsd"])
        except Exception as exc:
            raise TelemetryError("invalid model cost") from exc
        if not cost.is_finite() or cost < 0:
            raise TelemetryError("invalid model cost")
    if event in ("discovery", "knowledge-read", "diagnosis"):
        for key, value in values.items():
            pattern = r"sha256:[0-9a-f]{64}" if key.endswith("Digest") else r"[A-Za-z0-9._:/{}-]{1,256}"
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise TelemetryError("telemetry identifiers cannot carry raw payloads")
    if event in ("provider-request", "runtime-trace"):
        if not isinstance(values["requestId"], str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", values["requestId"]):
            raise TelemetryError("invalid request correlation identifier")
        ordinal = "turn" if event == "provider-request" else "action"
        if values[ordinal] < 1:
            raise TelemetryError("correlation requires a positive turn/action")
    if event == "runtime-trace":
        for key, width in (("traceId", 32), ("spanId", 16)):
            value = values[key]
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{"+str(width)+r"}", value) or int(value, 16) == 0:
                raise TelemetryError("invalid runtime trace identifier")
        if not isinstance(values["evidenceDigest"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", values["evidenceDigest"]):
            raise TelemetryError("runtime correlation requires evidence digest")
    if event in ('action-intent', 'action'):
        if values['action'] < 1 or values['kind'] not in ('terminal', 'mcp_list_tools', 'mcp_call_tool', 'ask_user'):
            raise TelemetryError('invalid action identity')
        if not isinstance(values['requestDigest'], str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', values['requestDigest']):
            raise TelemetryError('action requires a request digest, never raw arguments')


class Journal:
    """Exclusive immutable attempt identity; append and fsync before paid work.

    Retain the whole directory in controller storage across restarts. A missing
    end event is an interrupted attempt, never permission to start it again.
    Hash chaining detects edits but does not replace controller attestation.
    """
    def __init__(self, root, context, sink=None):
        validate_identity(context)
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.context = {k: context[k] for k in ("campaignId", "caseId", "attempt")}
        key = canonical_digest(self.context).split(":", 1)[1]
        self.path = root / (key + ".jsonl")
        self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        self.started = time.monotonic()
        self.sequence, self.previous = 0, None
        self.sink = sink

    def emit(self, event, **values):
        validate_values(event, values)
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
        if self.sink is not None:
            self.sink(row)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def validate_events(events):
    rows = []
    previous, identity = None, None
    dispatched, observed = {}, set()
    for row in events:
        event = row.get("event")
        if event not in FIELDS or set(row) != COMMON | FIELDS[event]:
            raise TelemetryError("unexpected telemetry event/fields")
        if type(row["schemaVersion"]) is not int or row["schemaVersion"] != 1:
            raise TelemetryError("unsupported telemetry schema")
        validate_identity(row)
        validate_values(event, {k: row[k] for k in FIELDS[event]})
        if event == 'action-intent':
            if row['action'] != len(dispatched)+1:
                raise TelemetryError('duplicate or nonsequential action dispatch')
            dispatched[row['action']] = row
        if event == 'action':
            sent = dispatched.get(row['action'])
            if sent is None or row['action'] in observed or any(row[k] != sent[k] for k in ('kind', 'requestDigest')):
                raise TelemetryError('action observation lacks matching prior dispatch')
            observed.add(row['action'])
        if event == 'runtime-trace' and row['action'] not in dispatched:
            raise TelemetryError('runtime trace lacks prior action dispatch')
        if type(row["sequence"]) is not int or row["sequence"] != len(rows) or row["previousDigest"] != previous:
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
    if ends and rows[-1]['status'] == 'pass' and set(dispatched) != observed:
        raise TelemetryError('passing attempt has unresolved action dispatches')
    return rows


def read_events(path):
    return validate_events(json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines())


def atomic_write(path, data):
    """Publish a complete file so readers never consume a partially written report."""
    path = Path(path)
    if path.is_symlink():
        raise TelemetryError("refusing symlink output")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="."+path.name+"-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def summarize(rows):
    first, last = rows[0], rows[-1]
    actions = [r for r in rows if r["event"] == "action"]
    dispatches = [r for r in rows if r['event'] == 'action-intent']
    observed = {row['action'] for row in actions}
    unresolved = [{key:row[key] for key in FIELDS['action-intent']}
                  for row in dispatches if row['action'] not in observed]
    models = [r for r in rows if r["event"] == "model"]
    same, longest, previous = 0, 0, None
    for action in actions:
        pair = (action["requestDigest"], action["resultDigest"])
        same = same + 1 if pair == previous else 1
        longest, previous = max(longest, same), pair
    counts = Counter(r["requestDigest"] for r in actions)
    errors = sum(r["isError"] is True for r in actions)
    parts = first["caseId"].split(":")
    contexts = [r for r in rows if r["event"] == "context"]
    if len(contexts) > 1:
        raise TelemetryError("duplicate context")
    context = contexts[0] if contexts else {}
    return {"campaignId": first["campaignId"], "caseId": first["caseId"], "attempt": first["attempt"],
            "scenarioId": parts[0], "variant": parts[1], "target": parts[2],
            "campaignMode": context.get("campaignMode", "unbound"),
            "knowledge": context.get("knowledge"),
            "conceptIds": context.get("conceptIds", []), "workflowIds": context.get("workflowIds", []),
            "diagnoses": [{k: r[k] for k in FIELDS["diagnosis"]} for r in rows if r["event"] == "diagnosis"],
            "discoveredInterfaces": [{k: r[k] for k in FIELDS["discovery"]} for r in rows if r["event"] == "discovery"],
            "knowledgeReads": [{k: r[k] for k in FIELDS["knowledge-read"]} for r in rows if r["event"] == "knowledge-read"],
            "providerRequests": [{k: r[k] for k in FIELDS["provider-request"]} for r in rows if r["event"] == "provider-request"],
            "runtimeTraces": [{k: r[k] for k in FIELDS["runtime-trace"]} for r in rows if r["event"] == "runtime-trace"],
            "cleanupStatus": last.get("cleanupStatus", "not-observed"),
            "status": last["status"] if last["event"] == "end" else "interrupted",
            "failureAttribution": last.get("failureAttribution"), "reason": last.get("reason"),
            "modelTurns": len(models), "actions": len(actions), "toolErrors": errors,
            "actionIntents":len(dispatches), "actionsUnresolved":len(unresolved), "unresolvedActions":unresolved,
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
    campaigns = []
    for cid in sorted({row["campaignId"] for row in attempts}):
        rows = [r for r in attempts if r["campaignId"] == cid]
        modes = {r["campaignMode"] for r in rows}
        if len(modes) != 1:
            raise TelemetryError("mixed campaign modes")
        if len({canonical_digest(r["knowledge"]) for r in rows}) != 1:
            raise TelemetryError("mixed campaign knowledge bindings")
        by_case = defaultdict(list)
        for row in rows:
            by_case[row["caseId"]].append(row)
        final = [max(values, key=lambda r: r["attempt"]) for values in by_case.values()]
        passed = sum(r["status"] == "pass" and r["cleanupStatus"] == "pass" for r in final)
        durations = sorted(r["elapsedMs"] for r in rows)
        def percentile(fraction):
            return durations[max(0, math.ceil(len(durations) * fraction) - 1)]
        campaigns.append({"campaignId": cid, "campaignMode": next(iter(modes)),
            "requiredCases": 252 if next(iter(modes)) == "qualification" else None,
            "casesObserved": len(by_case), "casesMissing": 252-len(by_case) if next(iter(modes)) == "qualification" else None,
            "finalGoalAndCleanupPasses": passed,
            "firstAttemptPasses": sum(r["attempt"] == 1 and r["status"] == "pass" and r["cleanupStatus"] == "pass" for r in rows),
            "goalCompletionFraction": passed/252 if next(iter(modes)) == "qualification" else None,
            "attemptOutcomes": dict(Counter(r["status"] for r in rows)),
            "failureAttributions": dict(Counter(r["failureAttribution"] or "unclassified" for r in rows if r["status"] != "pass")),
            "failedAssertions": dict(Counter(a for r in rows for a in r["failedAssertions"])),
            "cleanupOutcomes": dict(Counter(r["cleanupStatus"] for r in rows)),
            "durationP50Ms": percentile(.5), "durationP95Ms": percentile(.95),
            "inputTokens": sum(r["inputTokens"] for r in rows), "outputTokens": sum(r["outputTokens"] for r in rows),
            "modelCostUsd": str(sum((Decimal(r["modelCostUsd"]) for r in rows), Decimal(0))),
            "qualification": False})
    return {"schemaVersion": 1, "qualification": False,
            "interpretation": "Observed signals only. Repetition may be polling; clarification may be correct. No success or coverage inference from missing runs.",
            "attemptsObserved": len(attempts), "casesObserved": len(groups),
            "requiredCases": 252, "attempts": attempts,
            "campaigns": campaigns,
            "attention": sorted([r for r in attempts if r["status"] != "pass" or r["possibleStall"] or r["toolErrors"]],
                                key=lambda r: (-r["toolErrors"], -r["longestUnchangedRepeat"], -r["elapsedMs"]))}


def prometheus(value):
    """Textfile metrics from retained journals; no secrets or unbounded IDs in labels.

    Export one controller dataset consistently. These are cumulative observed
    attempts, not a claim that the release qualification gate passed.
    """
    groups = defaultdict(list)
    for row in value["attempts"]:
        groups[(row["target"], row["campaignMode"])].append(row)
    lines = ["# HELP honua_agent_attempts_total Observed attempts including interrupted attempts.",
             "# TYPE honua_agent_attempts_total counter",
             "# TYPE honua_agent_tool_errors_total counter",
             "# TYPE honua_agent_action_intents_total counter",
             "# TYPE honua_agent_actions_observed_total counter",
             "# TYPE honua_agent_actions_unresolved gauge",
             "# TYPE honua_agent_failure_attributions_total counter",
             "# TYPE honua_agent_cleanup_outcomes_total counter",
             "# TYPE honua_agent_input_tokens_total counter", "# TYPE honua_agent_output_tokens_total counter",
             "# TYPE honua_agent_model_cost_usd_total counter", "# TYPE honua_agent_duration_seconds histogram"]
    def labels(target, mode, **extra):
        return ','.join(k+'='+json.dumps(v) for k,v in {"target":target,"mode":mode,**extra}.items())
    for (target, mode), rows in sorted(groups.items()):
        base = labels(target, mode)
        lines.append(f'honua_agent_action_intents_total{{{base}}} {sum(r["actionIntents"] for r in rows)}')
        lines.append(f'honua_agent_actions_observed_total{{{base}}} {sum(r["actions"] for r in rows)}')
        lines.append(f'honua_agent_actions_unresolved{{{base}}} {sum(r["actionsUnresolved"] for r in rows)}')
        for status, count in sorted(Counter(r["status"] for r in rows).items()):
            lines.append(f'honua_agent_attempts_total{{{labels(target, mode, status=status)}}} {count}')
        for attribution, count in sorted(Counter(r["failureAttribution"] or "unclassified" for r in rows if r["status"] != "pass").items()):
            lines.append(f'honua_agent_failure_attributions_total{{{labels(target, mode, attribution=attribution)}}} {count}')
        for status, count in sorted(Counter(r["cleanupStatus"] for r in rows).items()):
            lines.append(f'honua_agent_cleanup_outcomes_total{{{labels(target, mode, status=status)}}} {count}')
        for name, field in (("tool_errors", "toolErrors"), ("input_tokens", "inputTokens"), ("output_tokens", "outputTokens")):
            lines.append(f'honua_agent_{name}_total{{{base}}} {sum(r[field] for r in rows)}')
        cost = sum((Decimal(r["modelCostUsd"]) for r in rows), Decimal(0))
        lines.append(f'honua_agent_model_cost_usd_total{{{base}}} {cost}')
        durations = [r["elapsedMs"]/1000 for r in rows]
        for bound in (1, 10, 60, 300, 900, 1800, 5400, math.inf):
            label = '+Inf' if math.isinf(bound) else str(bound)
            lines.append(f'honua_agent_duration_seconds_bucket{{{labels(target, mode, le=label)}}} {sum(d<=bound for d in durations)}')
        lines.append(f'honua_agent_duration_seconds_sum{{{base}}} {sum(durations)}')
        lines.append(f'honua_agent_duration_seconds_count{{{base}}} {len(durations)}')
    return '\n'.join(lines)+'\n'


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--journals", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--prometheus", type=Path)
    args = p.parse_args(argv)
    value = report(sorted(args.journals.glob("*.jsonl")))
    atomic_write(args.output, (json.dumps(value, indent=2, allow_nan=False) + "\n").encode())
    if args.prometheus:
        atomic_write(args.prometheus, prometheus(value).encode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
