"""Independent semantic adjudication for the owned findings/event journey.

These functions inspect original store/configuration observations and decoded
responses. The installed-client runner must separately authenticate raw wire,
session, client executable, source hashes and exact candidate identity. Calling
these functions alone never qualifies an operation or candidate.
"""
from __future__ import annotations

from datetime import timedelta
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import mcp_map_package_validation as wire
from mcp_ops_events_state import SCHEMA as STORE_SCHEMA, fixture_rows, instant, setup_seed
from mcp_ops_events_setup import SETUP_SQL
from mcp_ops_events_config import declaration as declare_configuration
from mcp_render_validation import negative_output

FINDINGS_URI = "honua://ops/findings"
FINDINGS_SOURCE = "honua_ops_findings"
CONTROL_SOURCE = FINDINGS_SOURCE + ".control_plane"
FINDINGS_SECTIONS = ("alert_dispatch", "control_plane", "deploy_preflight", "gp_queue",
    "workflow_operations", "serving_latency_rollup", "database_pressure", "batch_backends")
FINDINGS_BACKENDS = {
    "alert_dispatch": ("durableStore", "alert-dispatch-store"),
    "control_plane": ("configProjection", "control-plane-options"),
    "deploy_preflight": ("inProcess", "deploy-preflight-probe"),
    "gp_queue": ("durableStore", "execution-job-store"),
    "workflow_operations": ("durableStore", "workflow-operation-store"),
    "serving_latency_rollup": ("durableStore", "ops-health-rollup-store"),
    "database_pressure": ("durableStore", "database-pressure-signal"),
    "batch_backends": ("inProcess", "batch-compute-backends"),
}
REASONS = frozenset(("sourceUnavailable", "neverSucceeded", "stale", "missingObservationTime",
    "malformedObservationTime", "futureObservationTime", "partialResult", "incompleteCoverage",
    "truncated", "backendUnverified", "notConfigured"))
COVERAGE_REASONS = frozenset(("partialResult", "incompleteCoverage", "truncated"))
OPERATIONS = ["tools/call:ops_findings", "tools/call:alert_events", "tools/call:operate_events", "resources/read:Ops findings"]


def scenario(fixture_bytes, bootstrap, setup, declaration, nonce, canonical_client, *, actual_cursor=None):
    """Author exact calls from original setup; never manufacture a continuation.

    The runner may obtain the second-page cursor only from the actual first
    page. The final judge must rederive it from the wire-authenticated result,
    rather than accepting the runner's selected cursor or booleans as proof.
    """
    binding = wire.scenario(fixture_bytes, bootstrap)
    if (not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{12,32}", nonce)
            or canonical_client not in ("Official MCP TypeScript SDK", "MCP Inspector")
            or not isinstance(setup, dict) or setup.get("schema") != "honua.mcp-ops-events-setup/v1"
            or setup.get("phase") != "complete"
            or setup.get("setupSqlSha256") != "sha256:" + hashlib.sha256(SETUP_SQL.encode()).hexdigest()):
        raise ValueError("operate scenario requires original complete owned fixture/client binding")
    raw = setup.get("setupResponseText")
    if (not isinstance(raw, str) or len(raw.encode()) > 65536
            or setup.get("setupResponseSha256") != "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
            or not wire.same_json(wire.strict_json(raw), setup.get("seed"))):
        raise ValueError("operate setup lost its original allocated SQL response")
    seed = setup_seed(setup["seed"])
    if (seed.get("setupMethod") != "owned-sql-read-fixture"
            or any(seed.get(key) is not False for key in ("evaluationExercised", "deliveryExercised", "lifecycleApiExercised"))):
        raise ValueError("read setup cannot invent engine, delivery or lifecycle API coverage")
    rows = fixture_rows(setup["originalStore"]["store"], seed)
    runtime = setup.get("runtimeBinding")
    if (not wire.same_json(runtime, setup["originalStore"].get("runtimeBinding"))
            or runtime.get("candidateSourceSha") != binding["candidateSourceSha"]
            or runtime.get("candidateImage") != binding["candidateImage"]
            or not isinstance(declaration, dict)
            or declaration.get("candidateSourceSha") != binding["candidateSourceSha"]
            or declaration.get("candidateImage") != binding["candidateImage"]):
        raise ValueError("operate setup/configuration belongs to another candidate")
    ordered = sorted(rows.values(), key=lambda row: (instant(row["occurredAt"]), row["eventId"]), reverse=True)
    start = ordered[-1]["occurredAt"]
    end = (instant(ordered[0]["occurredAt"]) + timedelta(microseconds=1)).isoformat()
    if instant(runtime.get("startedAt")) <= instant(end):
        raise ValueError("isolated historical event window must precede this fresh runtime")
    release = declaration.get("environment", {}).get("ControlPlane__PlatformRelease__Version")
    if not isinstance(release, str) or not release:
        raise ValueError("owned findings configuration release is missing")
    if actual_cursor is not None and (not isinstance(actual_cursor, str) or not actual_cursor or len(actual_cursor) > 1024):
        raise ValueError("continuation must be an actual bounded first-page cursor")
    event_ids = [row["eventId"] for row in ordered]
    alert_args = {"from": start, "to": end, "rule": str(seed["alerts"]["ruleId"]), "source": "gis", "pageSize": 200}
    operate_args = {"from": start, "to": end, "kind": ["alert"], "pageSize": 200}
    finding_args = {"findingId": finding_identity(release)}
    calls = []

    def tool(identifier, name, arguments, expectation, *, ids=None, has_more=False, role="full"):
        call = {"id": identifier, "method": "tools/call", "params": {"name": "honua_" + name, "arguments": arguments},
            "credentialRole": role, "authenticated": role != "anonymous", "expectation": expectation}
        if ids is not None:
            call.update(selectedEventIds=ids, hasMore=has_more)
        calls.append(call)

    tool("findings", "ops_findings", finding_args, "finding")
    calls.append({"id": "findings-resource", "method": "resources/read", "params": {"uri": FINDINGS_URI},
        "credentialRole": "full", "authenticated": True, "expectation": "finding"})
    tool("alerts", "alert_events", alert_args, "alerts", ids=event_ids)
    tool("operate", "operate_events", operate_args, "operate", ids=event_ids)
    tool("alerts-first", "alert_events", {**alert_args, "pageSize": 1}, "alerts", ids=event_ids[:1], has_more=True)
    second = {**alert_args, "pageSize": 1}
    if actual_cursor is not None:
        second["cursor"] = actual_cursor
    tool("alerts-second", "alert_events", second, "alerts", ids=event_ids[1:])
    calls[-1]["cursorFrom"] = "alerts-first"
    tool("alerts-resolved", "alert_events", {**alert_args, "severity": "critical", "lifecycleState": "resolved"},
        "alerts", ids=[seed["alerts"]["resolvedAlertEventId"]])
    tool("alerts-empty", "alert_events", {**alert_args, "rule": "absent-" + nonce}, "alerts", ids=[])
    tool("operate-bounded", "operate_events", {**operate_args, "pageSize": 1}, "operate", ids=event_ids[:1], has_more=True)
    tool("findings-filter", "ops_findings", {"rule": "PLATFORM-RELEASE-SKEW", "severity": "warning"}, "finding")
    tool("findings-empty", "ops_findings", {"findingId": "absent-" + nonce}, "empty-finding")
    for name, arguments in (("ops_findings", {"severity": "unknown-" + nonce}),
                            ("alert_events", {**alert_args, "severity": "unknown-" + nonce}),
                            ("operate_events", {**operate_args, "kind": ["unknown-" + nonce]})):
        tool("invalid-" + name, name, arguments, "invalid-argument")
    for role in ("anonymous", "limited"):
        for name, arguments in (("ops_findings", finding_args), ("alert_events", alert_args), ("operate_events", operate_args)):
            tool(role + "-" + name, name, arguments, role + "-refusal", role=role)
        calls.append({"id": role + "-findings-resource", "method": "resources/read", "params": {"uri": FINDINGS_URI},
            "credentialRole": role, "authenticated": role != "anonymous", "expectation": role + "-refusal"})
    for call in list(calls[:4]):
        calls.append({**call, "id": "restarted-" + call["id"]})
    return {key: binding[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha", "candidateImage")} | {
        "schema": "honua.mcp-ops-events-scenario/v1", "operations": OPERATIONS, "nonce": nonce,
        "canonicalClient": canonical_client, "calls": calls,
        "setupResponseSha256": setup["setupResponseSha256"], "environmentSha256": declaration.get("environmentSha256")}


def payload(result, summary, *, resource=False):
    if resource:
        if not isinstance(result, dict) or set(result) != {"contents"}:
            raise ValueError("findings resource wrapper differs")
        items = result["contents"]
        if (not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict)
                or set(items[0]) != {"uri", "mimeType", "text"}
                or items[0]["uri"] != FINDINGS_URI or items[0]["mimeType"] != "application/json"):
            raise ValueError("findings resource URI/media differs")
        return wire.strict_json(items[0]["text"])
    if (not isinstance(result, dict) or set(result) - {"isError", "content", "structuredContent"}
            or result.get("isError", False) is not False or not isinstance(result.get("structuredContent"), dict)
            or not isinstance(result.get("content"), list) or len(result["content"]) != 1):
        raise ValueError("operate tool lacks complete structured payload")
    item, value = result["content"][0], result["structuredContent"]
    if not isinstance(item, dict) or set(item) != {"type", "text"} or item["type"] != "text" or not isinstance(item["text"], str):
        raise ValueError("operate tool media differs")
    if len(json.dumps(value, separators=(",", ":"), ensure_ascii=True)) >= 1024:
        if item["text"] != summary:
            raise ValueError("operate large-payload summary differs")
    elif not wire.same_json(wire.strict_json(item["text"]), value):
        raise ValueError("operate text differs from complete structured payload")
    return value


def bracket(before, after, seed):
    left, right = (fixture_rows(value["store"], seed) for value in (before, after))
    if (left != right or before["store"]["databaseIdentity"] != after["store"]["databaseIdentity"]
            or before["runtimeBinding"] != after["runtimeBinding"]
            or instant(before["completedAt"]) > instant(after["startedAt"])):
        raise ValueError("actual durable fixture/runtime changed inside invocation bracket")
    return sorted(left.values(), key=lambda row: (instant(row["occurredAt"]), row["eventId"]), reverse=True)


def event_item(row, *, operate=False):
    if operate:
        return {"eventId": "alert:" + str(row["eventId"]), "kind": "alert", "severity": row["severity"],
            "occurredAt": row["occurredAt"], "title": "Enter alert on layer 1", "summary": row["ruleName"],
            "serviceId": row["serviceId"], "layerId": row["layerId"], "objectId": row["objectId"],
            "resourceRef": "alert/" + str(row["eventId"])}
    value = {key: row[key] for key in ("eventId", "ruleId", "ruleName", "zoneId", "serviceId", "layerId",
        "objectId", "severity", "occurredAt", "incidentDurationMs")}
    value.update(triggerType="enter", incidentStatus="ended" if row["incidentCode"] == 3 else "started",
        lifecycleStatus="resolved" if row["lifecycleCode"] == 3 else "open",
        resourceRef="alert/" + str(row["eventId"]))
    value.update({key: row[key] for key in ("acknowledgedAt", "acknowledgedBy", "suppressedUntil",
        "resolvedAt", "resolvedBy") if row[key] is not None})
    return value


def equivalent(actual, expected):
    if not isinstance(actual, dict) or set(actual) != set(expected):
        raise ValueError("actual event shape differs from independent durable row")
    for key, value in expected.items():
        if key in ("occurredAt", "resolvedAt", "acknowledgedAt", "suppressedUntil"):
            equal = instant(actual[key]) == instant(value)
        else:
            equal = wire.same_json(actual[key], value)
        if not equal:
            raise ValueError("actual event field differs from independent durable row: " + key)


def event_posture(value, rows, arguments, before, after, *, operate=False, has_more=False):
    if (not isinstance(value, dict) or set(value) != {"schemaVersion", "status", "sources"}
            or value["schemaVersion"] != "1.0" or not isinstance(value["sources"], list) or len(value["sources"]) != 1):
        raise ValueError("event evidence source was omitted or substituted")
    source = value["sources"][0]
    required = {"sourceId", "backendKind", "backendId", "observedAt", "lastSuccessfulAt", "completeness",
        "reasonCodes", "coverage", "maximumAgeSeconds", "validUntil"}
    if (not isinstance(source, dict) or set(source) != required
            or source["sourceId"] != ("honua_operate_events" if operate else "honua_alert_events")
            or source["backendKind"] != "durableStore"
            or source["backendId"] != ("operate-event-feed" if operate else "alert-event-store")
            or type(source["maximumAgeSeconds"]) is not int or source["maximumAgeSeconds"] != 300):
        raise ValueError("event source identity/backend/validity differs")
    observed = instant(source["observedAt"])
    if rows:
        if observed != max(instant(row["occurredAt"]) for row in rows):
            raise ValueError("event posture invents response-time freshness")
    elif not instant(before["completedAt"]) <= observed <= instant(after["startedAt"]):
        raise ValueError("empty-window observation lies outside actual invocation")
    if instant(source["lastSuccessfulAt"]) != observed or instant(source["validUntil"]) != observed + timedelta(seconds=300):
        raise ValueError("event freshness timestamps differ from actual observation")
    coverage = {"hasMore": has_more, "truncated": has_more}
    for argument, field in (("from", "requestedFrom"), ("to", "requestedTo")):
        if arguments.get(argument) is not None:
            coverage[field] = arguments[argument]
    if rows:
        coverage.update(returnedFrom=min(rows, key=lambda row: instant(row["occurredAt"]))["occurredAt"],
            returnedTo=max(rows, key=lambda row: instant(row["occurredAt"]))["occurredAt"])
    actual_coverage = source["coverage"]
    if not isinstance(actual_coverage, dict) or set(actual_coverage) != set(coverage):
        raise ValueError("event coverage bounds differ")
    for key, expected in coverage.items():
        if key in ("hasMore", "truncated"):
            if type(actual_coverage[key]) is not bool or actual_coverage[key] is not expected:
                raise ValueError("event truncation hidden")
        elif instant(actual_coverage[key]) != instant(expected):
            raise ValueError("event requested/returned time bound differs")
    reasons = ["truncated"] if has_more else []
    # Owned historical rows are deliberately older than the five-minute health
    # window. Reading them must succeed while honestly retaining unavailable
    # freshness. This is a read contract, never a current-health certification.
    if instant(before["completedAt"]) > observed + timedelta(seconds=300):
        reasons.append("stale")
    elif instant(after["startedAt"]) > observed + timedelta(seconds=300):
        raise ValueError("event read straddled validity expiry; no unambiguous verdict")
    expected_status = "unavailable" if "stale" in reasons else "partial" if reasons else "complete"
    if (source["reasonCodes"] != sorted(reasons) or source["completeness"] != expected_status
            or value["status"] != expected_status):
        raise ValueError("event completeness hides stale or truncated evidence")


def event_output(result, before, after, seed, arguments, selected_ids, *, operate=False, has_more=False):
    rows = bracket(before, after, seed)
    by_id = {row["eventId"]: row for row in rows}
    if (not isinstance(selected_ids, list) or len(selected_ids) != len(set(selected_ids))
            or any(type(key) is not int or key not in by_id for key in selected_ids)):
        raise ValueError("authored selection lacks original event identities")
    selected = [by_id[key] for key in selected_ids]
    if selected != sorted(selected, key=lambda row: (instant(row["occurredAt"]), row["eventId"]), reverse=True):
        raise ValueError("authored event selection is not deterministic newest-first")
    summary = "Operate-event page returned in structuredContent." if operate else "Alert-event page returned in structuredContent."
    value = payload(result, summary)
    expected_fields = {"items", "evidencePosture"} | ({"partialResult"} if operate else ({"nextCursor"} if has_more else set()))
    if not isinstance(value, dict) or set(value) != expected_fields or not isinstance(value["items"], list) or len(value["items"]) != len(selected):
        raise ValueError("event page shape/count differs from independent selection")
    for actual, row in zip(value["items"], selected, strict=True):
        equivalent(actual, event_item(row, operate=operate))
    if operate:
        if value["partialResult"] is not False:
            raise ValueError("owned alert-only feed unexpectedly partial")
    elif has_more and (not isinstance(value["nextCursor"], str) or not value["nextCursor"] or len(value["nextCursor"]) > 1024):
        raise ValueError("actual continuation cursor is missing")
    event_posture(value["evidencePosture"], selected, arguments, before, after, operate=operate, has_more=has_more)
    return value.get("nextCursor")


def finding_identity(release):
    key = "target=|workload=|channel=|operation=|release=" + release + "|protocol="
    material = ("platform-release-skew\0" + key).encode()
    return "platform-release-skew-" + hashlib.sha256(material).hexdigest()[:32]


def findings_posture(value, generated):
    expected = {FINDINGS_SOURCE} | {FINDINGS_SOURCE + "." + suffix for suffix in FINDINGS_SECTIONS}
    if (not isinstance(value, dict) or set(value) != {"schemaVersion", "status", "sources"}
            or value["schemaVersion"] != "1.0" or not isinstance(value["sources"], list)
            or len(value["sources"]) != len(expected)
            or any(not isinstance(source, dict) for source in value["sources"])
            or {source.get("sourceId") for source in value["sources"]} != expected):
        raise ValueError("findings posture dropped or substituted an original source")
    sources = {source["sourceId"]: source for source in value["sources"]}
    allowed = {"sourceId", "backendKind", "backendId", "observedAt", "lastSuccessfulAt", "completeness",
        "reasonCodes", "coverage", "maximumAgeSeconds", "validUntil"}
    actionable = []
    for name, source in sources.items():
        if (set(source) - allowed or source.get("backendKind") not in ("inProcess", "durableStore", "configProjection", "composite", "unverified")
                or not isinstance(source.get("backendId"), str) or not source["backendId"]
                or source.get("completeness") not in ("complete", "partial", "unavailable", "notConfigured")
                or not isinstance(source.get("reasonCodes"), list)
                or any(not isinstance(reason, str) or reason not in REASONS for reason in source["reasonCodes"])
                or source["reasonCodes"] != sorted(set(source["reasonCodes"]))):
            raise ValueError("findings source vocabulary/shape differs")
        if name != FINDINGS_SOURCE and (source["backendKind"], source["backendId"]) != FINDINGS_BACKENDS[name.removeprefix(FINDINGS_SOURCE + ".")]:
            raise ValueError("findings source backend identity differs")
        reasons, state = set(source["reasonCodes"]), source["completeness"]
        for field in ("observedAt", "lastSuccessfulAt", "validUntil"):
            if source.get(field) is not None:
                instant(source[field])
        if source.get("observedAt") is None and "missingObservationTime" not in reasons:
            raise ValueError("findings source hides missing observation")
        if source.get("lastSuccessfulAt") is None and "neverSucceeded" not in reasons:
            raise ValueError("findings source hides missing successful collection")
        if source.get("validUntil") is not None and instant(source["validUntil"]) < generated and "stale" not in reasons:
            raise ValueError("findings source hides expired evidence")
        if state == "complete":
            if (reasons or source.get("observedAt") is None or source.get("lastSuccessfulAt") is None
                    or source["backendKind"] == "unverified" or type(source.get("maximumAgeSeconds")) is not int
                    or source["maximumAgeSeconds"] != 300 or source.get("validUntil") is None
                    or instant(source["observedAt"]) > generated + timedelta(minutes=1)
                    or instant(source["lastSuccessfulAt"]) != instant(source["observedAt"])
                    or instant(source["validUntil"]) != instant(source["observedAt"]) + timedelta(seconds=300)):
                raise ValueError("findings source claims completeness without fresh actual evidence")
            if name != FINDINGS_SOURCE:
                actionable.append(name)
        elif state == "partial":
            if not reasons or not reasons <= COVERAGE_REASONS:
                raise ValueError("findings partial source hides a source failure")
        elif state == "unavailable" and reasons <= COVERAGE_REASONS:
            raise ValueError("findings unavailable source lacks a source failure")
        elif not reasons or state == "notConfigured" and "notConfigured" not in reasons:
            raise ValueError("findings unavailable/not-configured source lacks its reason")
    control = sources[CONTROL_SOURCE]
    if (control.get("backendKind") != "configProjection" or control.get("backendId") != "control-plane-options"
            or control["completeness"] != "complete" or instant(control["observedAt"]) != generated):
        raise ValueError("required configuration source is not the actual fresh evaluation")
    aggregate = sources[FINDINGS_SOURCE]
    coverage = aggregate.get("coverage")
    status = "complete" if len(actionable) == 8 else "partial" if actionable else "unavailable"
    if (aggregate.get("backendKind") != "composite" or aggregate.get("backendId") != "ops-findings-engine"
            or aggregate["completeness"] != status or not isinstance(coverage, dict)
            or set(coverage) != {"includedComponentIds", "expectedComponentIds"}
            or coverage["expectedComponentIds"] != sorted(expected - {FINDINGS_SOURCE})
            or coverage["includedComponentIds"] != sorted(actionable)):
        raise ValueError("findings aggregate hides missing components")
    if actionable and instant(aggregate.get("observedAt")) != min(instant(sources[name]["observedAt"]) for name in actionable):
        raise ValueError("findings aggregate invents a more recent observation")
    overall = "unavailable" if any(source["completeness"] in ("unavailable", "notConfigured") for source in sources.values()) else (
        "partial" if any(source["completeness"] == "partial" for source in sources.values()) else "complete")
    if value["status"] != overall:
        raise ValueError("findings top-level posture hides the weakest source")
    return control


def findings_output(result, *, resource, before, after, declaration, configuration, admin_preflight, expected_present=True):
    """Judge the actual deterministic L1 skew finding, never runtime deployment health."""
    value = payload(result, "Ops findings returned in structuredContent.", resource=resource)
    if (not isinstance(value, dict) or set(value) != {"evidencePosture", "generatedAt", "findings"}
            or not isinstance(value["findings"], list)):
        raise ValueError("findings evaluation shape differs")
    generated = instant(value["generatedAt"])
    if not instant(before) <= generated <= instant(after):
        raise ValueError("findings generatedAt lies outside the actual invocation")
    environment = declaration.get("environment")
    if (declaration.get("schema") != "honua.mcp-ops-config-declaration/v1"
            or not isinstance(environment, dict) or configuration.get("environment") != environment
            or configuration.get("schema") != "honua.mcp-ops-config-observation/v1"
            or configuration.get("environmentSha256") != declaration.get("environmentSha256")
            or configuration.get("selectedScopeEntryCount") != 8
            or configuration.get("deployedTargetObserved") is not False or configuration.get("actuationRequested") is not False
            or configuration.get("runtimeBinding", {}).get("candidateSourceSha") != declaration.get("candidateSourceSha")
            or configuration.get("runtimeBinding", {}).get("candidateImage") != declaration.get("candidateImage")
            or instant(configuration.get("observedAt")) > instant(before)):
        raise ValueError("findings lack original actual candidate configuration observation")
    encoded = json.dumps(environment, sort_keys=True, separators=(",", ":")).encode()
    if "sha256:" + hashlib.sha256(encoded).hexdigest() != declaration["environmentSha256"]:
        raise ValueError("findings configuration declaration hash differs")
    release = environment.get("ControlPlane__PlatformRelease__Version")
    projection = admin_preflight.get("platformRelease")
    if (not isinstance(release, str) or not release or not isinstance(projection, dict)
            or projection.get("releaseVersion") != release or projection.get("releaseDeclared") is not True
            or projection.get("isCoVersioned") is not False or projection.get("skewedIds") != ["cert-ops-skew"]
            or declaration.get("expectedSkewedIds") != ["cert-ops-skew"]):
        raise ValueError("independent preflight does not corroborate original declared skew")
    control = findings_posture(value["evidencePosture"], generated)
    matches = [item for item in value["findings"] if isinstance(item, dict) and item.get("id") == finding_identity(release)]
    if not expected_present:
        if value["findings"]:
            raise ValueError("nonmatching findings filter was ignored")
        return value
    if len(matches) != 1:
        raise ValueError("actual nonempty deterministic finding is missing or duplicated")
    item = matches[0]
    expected = {"id": finding_identity(release), "rule": "platform-release-skew", "severity": "Warning",
        "title": "Platform release is not co-versioned", "subject": {"releaseVersion": release},
        "evidenceRefs": ["GET /api/v1/admin/deploy/preflight?includeDiagnostics=true", "skewed:cert-ops-skew"],
        "requiredSourceIds": [CONTROL_SOURCE]}
    if (set(item) != set(expected) | {"detectedAt", "observationWindow", "explanation"}
            or any(not wire.same_json(item[key], expected_value) for key, expected_value in expected.items())
            or instant(item["detectedAt"]) != generated):
        raise ValueError("finding contract/identity/dependencies differ from declared configuration")
    explanation = (f"Declared platform release '{release}' is not co-versioned: 1 plane(s) explicitly pin an artifact "
        "that diverges from the release (cert-ops-skew). This is L1 guidance only: a runtime action cannot clear "
        "config-derived skew, since the pinned configuration would simply re-assert itself on the next reconcile. "
        "Remove or update the explicit pin, then redeploy the plane to the release revision to restore co-versioning.")
    window = item["observationWindow"]
    if (item["explanation"] != explanation or not isinstance(window, dict)
            or set(window) != {"returnedFrom", "returnedTo"}
            or any(instant(window[key]) != instant(control["observedAt"]) for key in window)):
        raise ValueError("finding explanation/window invents runtime authority or response-time evidence")
    return value


def context(value, fixture, bootstrap, setup, declaration, runtime, full_identity):
    """Rejudge original independent reads, not runner-supplied success flags."""
    if (not isinstance(value, dict) or set(value) != {"schema", "startedAt", "completedAt", "storeObservation",
            "runtimeBinding", "configuration", "preflight"}
            or value["schema"] != "honua.mcp-ops-events-context/v1"
            or not wire.same_json(value["runtimeBinding"], runtime)
            or instant(value["completedAt"]) < instant(value["startedAt"])):
        raise ValueError("original independent operate context is missing or runtime changed")
    rebound = dict(bootstrap); rebound["runtimeBinding"] = runtime
    wire.scenario(fixture, rebound)
    store = value["storeObservation"]
    if (not isinstance(store, dict) or set(store) != {"schema", "startedAt", "completedAt", "runtimeBinding", "store"}
            or store["schema"] != STORE_SCHEMA
            or not wire.same_json(store["runtimeBinding"], runtime)
            or not instant(value["startedAt"]) <= instant(store["startedAt"]) <= instant(store["store"]["observedAt"])
            <= instant(store["completedAt"]) <= instant(value["completedAt"])):
        raise ValueError("original database read is outside its actual runtime/clock bracket")
    fixture_rows(store["store"], setup["seed"])
    configuration = value["configuration"]
    environment = declaration["environment"]
    if (not isinstance(configuration, dict) or set(configuration) != {"schema", "observedAt", "runtimeBinding",
            "environment", "environmentSha256", "selectedScopeEntryCount", "deployedTargetObserved", "actuationRequested"}
            or configuration["schema"] != "honua.mcp-ops-config-observation/v1"
            or not wire.same_json(configuration["runtimeBinding"], runtime)
            or not wire.same_json(configuration["environment"], environment)
            or configuration["environmentSha256"] != declaration["environmentSha256"]
            or type(configuration["selectedScopeEntryCount"]) is not int or configuration["selectedScopeEntryCount"] != 8
            or configuration["deployedTargetObserved"] is not False or configuration["actuationRequested"] is not False
            or not instant(value["startedAt"]) <= instant(configuration["observedAt"]) <= instant(value["completedAt"])):
        raise ValueError("original daemon-selected declaration was omitted, changed, or claims actuation")
    preflight = value["preflight"]
    if (not isinstance(preflight, dict) or set(preflight) != {"method", "url", "status", "responseText", "responseSha256",
            "credentialFingerprint", "startedAt", "completedAt"}
            or preflight["method"] != "GET" or preflight["url"] != "http://127.0.0.1:8080/api/v1/admin/deploy/preflight?includeDiagnostics=true"
            or type(preflight["status"]) is not int or preflight["status"] != 200
            or not isinstance(preflight["responseText"], str) or len(preflight["responseText"].encode()) > 2 * 1024 * 1024
            or preflight["responseSha256"] != "sha256:" + hashlib.sha256(preflight["responseText"].encode()).hexdigest()
            or preflight["credentialFingerprint"] != full_identity["credentialFingerprint"]
            or not instant(value["startedAt"]) <= instant(preflight["startedAt"]) <= instant(preflight["completedAt"])
            <= instant(value["completedAt"])):
        raise ValueError("preflight lacks original authenticated normal-route bytes and actual observation clock")
    projection = wire.strict_json(preflight["responseText"])
    if not isinstance(projection, dict):
        raise ValueError("normal deploy preflight is not its original root object")
    return projection


def limited_refusal(result):
    """A valid managed reader must receive authorization denial, never reauth."""
    if (not isinstance(result, dict) or set(result) != {"isError", "content", "structuredContent"}
            or result["isError"] is not True or not isinstance(result["structuredContent"], dict)
            or not isinstance(result["content"], list) or len(result["content"]) != 1):
        raise ValueError("limited reader did not receive the typed tool authorization refusal")
    item, value = result["content"][0], result["structuredContent"]
    if (not isinstance(item, dict) or set(item) != {"type", "text"} or item["type"] != "text"
            or not wire.same_json(wire.strict_json(item["text"]), value)
            or set(value) - {"status", "code", "message", "error", "correlationId"}
            or value.get("status") != "error" or value.get("code") != "permission_denied"
            or value.get("message") != "Caller is not authorized to read operational observability."
            or value.get("error") != {"kind": "AuthorizationDenied", "message": value["message"]}
            or ("correlationId" in value and (not isinstance(value["correlationId"], str)
                or not 0 < len(value["correlationId"]) <= 256))):
        raise ValueError("limited reader refusal changed authorization semantics or leaked result data")


def judge(fixture, bootstrap, setup, manifest_bytes, declaration, observed):
    # This helper is one shared prerequisite for the three original MCP closure
    # streams. Missing source/receipt or failed normal revocation stays NONPASS.
    from mcp_managed_identity_fixture import verify_receipt
    from mcp_ops_events_data_read import verify as verify_limited_read

    if not isinstance(observed, dict):
        raise ValueError("original canonical-client operate observations are missing")
    rederived = declare_configuration(manifest_bytes, release_source_sha=declaration.get("releaseSourceSha"),
        candidate_source_sha=bootstrap.get("candidateSourceSha"), candidate_image=bootstrap.get("candidateImage"))
    if not wire.same_json(declaration, rederived):
        raise ValueError("declared configuration differs from the original protected manifest bytes")
    authored = scenario(fixture, bootstrap, setup, declaration, observed.get("nonce"), observed.get("canonicalClient"))
    extras = {"clientVersion", "runtimeBefore", "runtimeRestarted", "runtimeAfter", "identityFixtures", "limitedRead"}
    if (set(observed) != set(authored) | extras
            or any(not wire.same_json(observed[key], value) for key, value in authored.items() if key != "calls")
            or not isinstance(observed["clientVersion"], str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", observed["clientVersion"])):
        raise ValueError("canonical operate observations differ from the authored candidate/scenario")
    original, restarted, final = (observed[key] for key in ("runtimeBefore", "runtimeRestarted", "runtimeAfter"))
    for runtime in (original, restarted, final):
        binding = dict(bootstrap); binding["runtimeBinding"] = runtime
        wire.scenario(fixture, binding)
    if (not wire.same_json(original, {**setup["runtimeBinding"], "startedAt": original["startedAt"]})
            or instant(original["startedAt"]) < instant(setup["runtimeBinding"]["startedAt"])
            or not wire.same_json(original, {**restarted, "startedAt": original["startedAt"]})
            or not wire.same_json(restarted, final)
            or instant(restarted["startedAt"]) <= instant(original["startedAt"])):
        raise ValueError("operate journey did not restart this exact same container/image/source")
    receipts = observed["identityFixtures"]
    if not isinstance(receipts, dict) or set(receipts) != {"full", "limited"}:
        raise ValueError("both original managed caller lifecycles are required")
    identities = {role: verify_receipt(receipts[role], original, profile)
        for role, profile in (("full", "admin"), ("limited", "limited-read"))}
    if (identities["full"]["keyId"] == identities["limited"]["keyId"]
            or identities["full"]["credentialFingerprint"] == identities["limited"]["credentialFingerprint"]):
        raise ValueError("full and limited callers did not independently resolve to different managed keys")
    verify_limited_read(observed["limitedRead"], fixture, bootstrap, identities["limited"], original)
    calls = observed["calls"]
    if not isinstance(calls, list) or len(calls) != len(authored["calls"]):
        raise ValueError("canonical operate journey is missing authored operations or controls")
    sessions, findings_times, cursor = set(), [], None
    first_before, last_after = None, None
    for expected, actual in zip(authored["calls"], calls, strict=True):
        expected = dict(expected)
        if expected["id"] == "alerts-second":
            if cursor is None:
                raise ValueError("continuation lacks an independently adjudicated first-page cursor")
            expected["params"] = {"name": expected["params"]["name"],
                "arguments": {**expected["params"]["arguments"], "cursor": cursor}}
        positive = expected["expectation"] in ("finding", "empty-finding", "alerts", "operate")
        fields = set(expected) | {"wire", "client", "result"} | ({"before", "after"} if positive else set())
        if (not isinstance(actual, dict) or set(actual) != fields
                or any(not wire.same_json(actual[key], value) for key, value in expected.items())):
            raise ValueError("canonical invocation differs from authored method, parameters, or role")
        frame, session = wire.observed_wire(actual["wire"], expected["method"], expected["params"], expected["authenticated"])
        role = expected["credentialRole"]
        fingerprint = identities["limited" if role == "limited" else "full"]["credentialFingerprint"]
        if (session in sessions or actual["wire"]["expectedCredentialFingerprint"] != fingerprint
                or not wire.same_json(actual["result"], frame if "error" in frame else frame["result"])):
            raise ValueError("canonical call reused a session, switched managed caller, or replaced original wire output")
        sessions.add(session)
        wire.client_observation(actual["client"], frame, actual["wire"], authored["canonicalClient"], observed["clientVersion"])
        if positive:
            runtime = restarted if expected["id"].startswith("restarted-") else original
            preflights = [context(actual[side], fixture, bootstrap, setup, declaration, runtime, identities["full"])
                for side in ("before", "after")]
            before, after = actual["before"], actual["after"]
            if (instant(before["completedAt"]) > instant(after["startedAt"])
                    or last_after is not None and instant(before["startedAt"]) < last_after):
                raise ValueError("independent invocation brackets overlap or were reordered")
            if first_before is None:
                first_before = instant(before["startedAt"])
            last_after = instant(after["completedAt"])
            rows = bracket(before["storeObservation"], after["storeObservation"], setup["seed"])
            if rows != sorted(fixture_rows(setup["originalStore"]["store"], setup["seed"]).values(),
                    key=lambda row: (instant(row["occurredAt"]), row["eventId"]), reverse=True):
                raise ValueError("persisted original event rows changed across invocation or actual restart")
            if expected["expectation"] in ("finding", "empty-finding"):
                for preflight in preflights:
                    projection = preflight.get("platformRelease")
                    if (not isinstance(projection, dict) or projection.get("releaseDeclared") is not True
                            or projection.get("releaseVersion") != declaration["environment"]["ControlPlane__PlatformRelease__Version"]
                            or projection.get("isCoVersioned") is not False or projection.get("skewedIds") != ["cert-ops-skew"]):
                        raise ValueError("normal preflight changed declared skew across the actual invocation")
                value = findings_output(actual["result"], resource=expected["method"] == "resources/read",
                    before=before["completedAt"], after=after["startedAt"], declaration=declaration,
                    configuration=before["configuration"], admin_preflight=preflights[0],
                    expected_present=expected["expectation"] == "finding")
                stamp = instant(value["generatedAt"])
                if stamp <= instant(runtime["startedAt"]):
                    raise ValueError("findings evaluation was cached before this actual server start")
                findings_times.append(stamp)
            else:
                selected_cursor = event_output(actual["result"], before["storeObservation"], after["storeObservation"],
                    setup["seed"], expected["params"]["arguments"], expected["selectedEventIds"],
                    operate=expected["expectation"] == "operate", has_more=expected["hasMore"])
                if expected["id"] == "alerts-first":
                    cursor = selected_cursor
        elif expected["expectation"] == "invalid-argument":
            negative_output(actual["result"], "invalid_argument")
        elif expected["method"] == "resources/read":
            wire.resource_refusal(actual["result"], -32000,
                "unauthenticated" if role == "anonymous" else "permission_denied")
        elif role == "anonymous":
            wire.anonymous_tool_refusal(actual["result"])
        else:
            limited_refusal(actual["result"])
    if findings_times != sorted(findings_times) or findings_times[-1] <= findings_times[0]:
        raise ValueError("findings were not freshly recomputed across the actual restart")
    limited_read = observed["limitedRead"]
    if (instant(identities["limited"]["createdAt"]) > instant(limited_read["startedAt"])
            or instant(identities["limited"]["observedAt"]) > instant(limited_read["startedAt"])
            or instant(limited_read["completedAt"]) > first_before):
        raise ValueError("limited data-read positive does not follow provisioning and precede the Ops journey")
    for role in ("full", "limited"):
        if (instant(identities[role]["createdAt"]) > first_before
                or instant(identities[role]["observedAt"]) > first_before
                or instant(receipts[role]["revocation"]["revokedAt"]) < last_after):
            raise ValueError("normal managed-key provisioning and revocation do not bracket the actual journey")
    return {key: authored[key] for key in ("candidateSourceSha", "candidateImage", "fixtureSha256", "bootstrapSha256", "canonicalClient")} | {
        "clientVersion": observed["clientVersion"], "operations": OPERATIONS, "positive": True, "negative": True,
        "mediaSchema": True, "independentStores": True, "persistedRestart": True, "liveFindingsRecomputed": True,
        "anonymousInvocationRefusal": True, "limitedPermissionRefusal": True, "managedIdentityJoined": True,
        "limitedDataReadPositive": True,
        "actualCursorPaging": True, "configurationLevel": "L1-declared-configuration",
        "alertEvaluationExercised": False, "notificationDeliveryExercised": False, "lifecycleApiExercised": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("fixture", "bootstrap", "setup", "manifest", "declaration"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--nonce"); parser.add_argument("--canonical-client"); parser.add_argument("--actual-cursor")
    args = parser.parse_args()
    try:
        fixture, bootstrap = args.fixture.read_bytes(), wire.strict_json(args.bootstrap.read_bytes())
        setup, declaration = (wire.strict_json(getattr(args, name).read_bytes()) for name in ("setup", "declaration"))
        manifest = args.manifest.read_bytes()
        rederived = declare_configuration(manifest, release_source_sha=declaration.get("releaseSourceSha"),
            candidate_source_sha=bootstrap.get("candidateSourceSha"), candidate_image=bootstrap.get("candidateImage"))
        if not wire.same_json(declaration, rederived):
            raise ValueError("declaration differs from protected source manifest")
        result = judge(fixture, bootstrap, setup, manifest, declaration, wire.strict_json(args.observations.read_bytes())) \
            if args.observations else scenario(fixture, bootstrap, setup, declaration, args.nonce, args.canonical_client,
                actual_cursor=args.actual_cursor)
        print(json.dumps(result, allow_nan=False)); return 0
    except (ValueError, TypeError, KeyError, AttributeError, OSError, ImportError):
        print("authored findings/events client oracle refused evidence", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
