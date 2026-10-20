"""Authored planning-disclosure semantics; no model or execution qualification.

The server's canned planning demo is intentionally separate from the real,
hand-authored plan handed to validate_plan. A matching demo is never evidence
that a model compiled an intent or that any plan executed.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_map_package_validation import strict_json, same_json, observed_wire, client_observation, anonymous_tool_refusal
from mcp_render_validation import scenario as fixture_binding, negative_output
from mcp_style_validation import tool_output
from mcp_plan_validation import buffer_plan, oracle_mcp_plan_valid, tool_output as validation_output
from mcp_managed_identity_fixture import verify_receipt
from mcp_execute_plan_validation import snapshot_output

OPERATION = "tools/call:plan_analysis"
PROMPT = "Show open hospitals within 1 km of flood zones as a linked map, table, and chart."
CASE = "success-linked-dashboard"
CATALOG = "honua://catalog/processes"


def scenario(fixture_bytes, bootstrap, nonce, canonical_client):
    binding = fixture_binding(fixture_bytes, bootstrap)
    if (not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{24}", nonce)
            or canonical_client not in ("Official MCP TypeScript SDK", "MCP Inspector")):
        raise ValueError("planning requires an owned nonce and canonical client")
    context = {"fixtureScenarioId": CASE, "fixtureCase": "success", "qualificationNonce": nonce}
    arguments = {"intent": PROMPT, "context": context}
    unknown = {"intent": "Owned qualification unmatched intent " + nonce,
               "context": {"qualificationNonce": nonce, "unmatched": True}}
    plan = buffer_plan(strict_json(fixture_bytes)["processes"])
    plan["planId"] = plan["intentId"] = "mcp-planning-handoff-" + nonce
    calls = [
        ("catalog", "resources/read", {"uri": CATALOG}, "full", "catalog"),
        ("matched-demo", "tools/call", {"name": "honua_plan_analysis", "arguments": arguments}, "full", "demo"),
        ("scoped-demo", "tools/call", {"name": "honua_plan_analysis", "arguments": arguments}, "limited", "demo"),
        ("unmatched-demo", "tools/call", {"name": "honua_plan_analysis", "arguments": unknown}, "full", "unmatched"),
        ("hand-authored-handoff", "tools/call", {"name": "honua_validate_plan", "arguments": {"plan": plan}}, "full", "handoff"),
        ("empty-intent", "tools/call", {"name": "honua_plan_analysis", "arguments": {"intent": ""}}, "full", "invalid"),
        ("anonymous-demo", "tools/call", {"name": "honua_plan_analysis", "arguments": arguments}, "anonymous", "anonymous"),
    ]
    return {key: binding[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha", "candidateImage", "runtimeBinding")} | {
        "schema": "honua.mcp-planning-scenario/v1", "operation": OPERATION, "nonce": nonce,
        "canonicalClient": canonical_client, "handoffPlan": plan,
        "calls": [{"id": name, "method": method, "params": params, "credentialRole": role,
                   "authenticated": role != "anonymous", "expectation": expectation}
                  for name, method, params, role, expectation in calls]}


def disclosure(output, context):
    if (output.get("engine") != "fixture" or not same_json(output.get("context"), context)
            or not isinstance(output.get("nextSteps"), str)):
        raise ValueError("planning changed its declared engine or original context")
    text = output["nextSteps"].lower()
    if (not all(word in text for word in ("fixture", "demo", "not", "compiled", "intent", "client model"))
            or not all(name in output["nextSteps"] for name in
                       (CATALOG, "honua_validate_plan", "honua_dry_run_plan", "honua_execute_plan"))):
        raise ValueError("fixture planning did not disclose its limitation and supported next steps")


def matched_demo(result, context):
    output = tool_output(result)
    disclosure(output, context)
    plan = output.get("plan")
    if (output.get("status") != "planned" or output.get("fixtureCase") != "success"
            or output.get("contractVersion") != "honua.ai_builder.spatial_query.v1"
            or not isinstance(plan, dict) or plan.get("intentId") != CASE
            or plan.get("planId") != "plan-" + CASE or not isinstance(plan.get("steps"), list)
            or len(plan["steps"]) != 3 or plan.get("outputs") != []):
        raise ValueError("matched planning response lost its explicit canned-demo identity")
    expected = [
        ("query-open-hospitals", "QueryFeatures", None, [],
         {"layerId": "1", "where": "facility_type = 'hospital' AND status = 'open'"}),
        ("distance-filter", "Geoprocess", "analytics.spatial-join", ["query-open-hospitals"],
         {"layerId": "1", "joinLayerId": "2", "predicate": "dwithin", "distance": "1000"}),
        ("compose-packages", "Export", None, ["distance-filter"], {}),
    ]
    for actual, (identifier, kind, process, dependencies, inputs) in zip(plan["steps"], expected, strict=True):
        if (not isinstance(actual, dict) or actual.get("stepId") != identifier or actual.get("kind") != kind
                or actual.get("processId") != process or actual.get("dependsOn") != dependencies
                or actual.get("inputs") != inputs):
            raise ValueError("canned planning DAG lost its original source, filter or dependency")
    warnings = plan.get("warnings")
    if not isinstance(warnings, list) or "mutable_source_cache_warning" not in warnings:
        raise ValueError("canned planning omitted its mutable-source warning")
    messages = output.get("warnings")
    if (not isinstance(messages, list) or not messages
            or any(not isinstance(item, dict) or not isinstance(item.get("code"), str)
                   or not isinstance(item.get("message"), str) or not item["message"].strip()
                   for item in messages)
            or not any(item["code"] == "mutable_source_cache_warning"
                       and "change" in item["message"].lower() and "snapshot token" in item["message"].lower()
                       for item in messages)):
        raise ValueError("canned planning omitted its typed mutable-source explanation")
    return output


def unmatched_demo(result, context):
    output = tool_output(result)
    disclosure(output, context)
    reason = output.get("reason")
    if (output.get("status") != "rejected" or output.get("plan") is not None
            or output.get("specDraft") is not None or output.get("appPackage") is not None
            or not isinstance(reason, str)
            or not all(part in reason.lower() for part in
                       ("fixture", "demo", "did not attempt to compile", "client model", "honua_validate_plan"))):
        raise ValueError("unmatched fixture intent silently became a tailored or executable plan")
    return output


def process_catalog(result):
    items = result.get("contents") if isinstance(result, dict) else None
    if (not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict)
            or set(items[0]) != {"uri", "mimeType", "text"} or items[0]["uri"] != CATALOG
            or items[0]["mimeType"] != "application/json"):
        raise ValueError("planning handoff did not read the actual process catalog")
    catalog = strict_json(items[0]["text"])
    processes = catalog.get("processes") if isinstance(catalog, dict) else None
    if not isinstance(catalog, dict) or catalog.get("status") != "ok" or not isinstance(processes, list):
        raise ValueError("process catalog is incomplete")
    selected = [row for row in processes if isinstance(row, dict) and row.get("processId") == "geometry.buffer"]
    if (len(selected) != 1 or selected[0].get("executionKind") != "Job"
            or "job" not in (selected[0].get("entryPoints") or [])
            or "Async" not in (selected[0].get("supportedExecutionModes") or [])):
        raise ValueError("hand-authored handoff is not supported by the observed catalog")


def instant(value):
    if not isinstance(value, str):
        raise ValueError("planning observation time is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("planning observation time is not absolute")
    return parsed


def judge(fixture_bytes, bootstrap, observed):
    if not isinstance(observed, dict):
        raise ValueError("planning observations are missing")
    authored = scenario(fixture_bytes, bootstrap, observed.get("nonce"), observed.get("canonicalClient"))
    fields = set(authored) | {"clientVersion", "runtimeBefore", "runtimeAfter", "identityFixtures", "snapshots"}
    if (set(observed) != fields or any(not same_json(observed[key], value)
            for key, value in authored.items() if key != "calls")
            or not isinstance(observed["clientVersion"], str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", observed["clientVersion"])
            or observed["runtimeBefore"] != authored["runtimeBinding"]
            or observed["runtimeAfter"] != authored["runtimeBinding"]):
        raise ValueError("planning changed its candidate, client, input or runtime")
    identities = observed["identityFixtures"]
    if not isinstance(identities, dict) or set(identities) != {"full", "limited"}:
        raise ValueError("planning lacks original managed caller observations")
    callers = {role: verify_receipt(identities[role], authored["runtimeBinding"], profile)
               for role, profile in (("full", "admin"), ("limited", "limited-read"))}
    salts = {identities[role]["credentialSalt"] for role in identities}
    if (len(salts) != 1 or callers["full"]["credentialFingerprint"] == callers["limited"]["credentialFingerprint"]
            or identities["full"]["keyId"] == identities["limited"]["keyId"]):
        raise ValueError("planning callers are reused or have different original salts")
    snapshots = observed["snapshots"]
    if not isinstance(snapshots, list) or len(snapshots) != 2:
        raise ValueError("planning lacks original before/after durable censuses")
    for snapshot in snapshots:
        if (not isinstance(snapshot, dict) or set(snapshot) != {"schema", "runtimeBinding", "startedAt", "completedAt", "value"}
                or snapshot["schema"] != "honua.mcp-planning-store/v1"
                or snapshot["runtimeBinding"] != authored["runtimeBinding"]
                or instant(snapshot["startedAt"]) > instant(snapshot["completedAt"])):
            raise ValueError("planning store observation has another runtime or invalid clock")
        snapshot_output(snapshot["value"])
    if not same_json(snapshots[0]["value"], snapshots[1]["value"]):
        raise ValueError("read-only planning/validation changed durable jobs or results")
    actual_calls = observed["calls"]
    if not isinstance(actual_calls, list) or len(actual_calls) != len(authored["calls"]):
        raise ValueError("planning omitted an authored primary, handoff or refusal")
    sessions = set()
    previous = instant(snapshots[0]["completedAt"])
    for expected, actual in zip(authored["calls"], actual_calls, strict=True):
        if (not isinstance(actual, dict) or set(actual) != set(expected) | {"wire", "client", "result", "startedAt", "completedAt"}
                or any(not same_json(actual[key], value) for key, value in expected.items())):
            raise ValueError("planning call differs from the original authored request")
        frame, session = observed_wire(actual["wire"], expected["method"], expected["params"], expected["authenticated"])
        fingerprint = callers["limited" if expected["credentialRole"] == "limited" else "full"]["credentialFingerprint"]
        if (session in sessions or actual["wire"].get("expectedCredentialFingerprint") != fingerprint
                or not same_json(actual["result"], frame if "error" in frame else frame["result"])):
            raise ValueError("planning replaced the original caller, session or wire output")
        sessions.add(session)
        started, completed = instant(actual["startedAt"]), instant(actual["completedAt"])
        caller = callers["limited" if expected["credentialRole"] == "limited" else "full"]
        if (started < previous or completed < started
                or started < instant(caller["observedAt"]) or completed > instant(caller["revokedAt"])):
            raise ValueError("planning call predates its caller/readback or follows revocation")
        previous = completed
        client_observation(actual["client"], frame, actual["wire"], observed["canonicalClient"], observed["clientVersion"])
        result, expectation = actual["result"], expected["expectation"]
        if expectation == "catalog":
            process_catalog(result)
        elif expectation == "demo":
            matched_demo(result, expected["params"]["arguments"]["context"])
        elif expectation == "unmatched":
            unmatched_demo(result, expected["params"]["arguments"]["context"])
        elif expectation == "handoff":
            passed, _ = oracle_mcp_plan_valid(validation_output(result))
            if not passed:
                raise ValueError("real hand-authored plan failed its supported validation handoff")
        elif expectation == "invalid":
            negative_output(result, "invalid_argument")
        else:
            anonymous_tool_refusal(result)
    if previous > instant(snapshots[1]["startedAt"]):
        raise ValueError("planning final independent observation preceded the actual calls")
    return {key: authored[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha", "candidateImage", "operation")} | {
        "canonicalClient": observed["canonicalClient"], "clientVersion": observed["clientVersion"],
        "positive": True, "negative": True, "fixtureDisclosure": True, "handAuthoredValidation": True,
        "durableJobsResultsUnchanged": True, "liveModelProven": False, "executionProven": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--nonce")
    parser.add_argument("--canonical-client")
    parser.add_argument("--observations", type=Path)
    args = parser.parse_args()
    try:
        fixture, bootstrap = args.fixture.read_bytes(), strict_json(args.bootstrap.read_bytes())
        value = (judge(fixture, bootstrap, strict_json(args.observations.read_bytes())) if args.observations
                 else scenario(fixture, bootstrap, args.nonce, args.canonical_client))
        print(json.dumps(value, allow_nan=False, sort_keys=True))
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        print("owned planning disclosure refused", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
