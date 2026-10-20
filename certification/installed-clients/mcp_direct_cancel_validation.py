"""Original direct-cancel job, caller, wire, store and late-output oracle.

An acknowledgement is never terminal cancellation proof. This source only
credits a real Running job that remains Cancelled without output after a
bounded late read. It does not establish native child-process cessation.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_execute_plan_validation import accepted, resource, snapshot_output
from mcp_managed_identity_fixture import verify_receipt
from mcp_map_package_validation import strict_json, same_json, observed_wire, client_observation, anonymous_tool_refusal
from mcp_plan_validation import oracle_mcp_plan_valid, tool_output as validation_output
from mcp_render_validation import scenario as fixture_binding
from mcp_style_validation import tool_output

OPERATION = "tools/call:cancel_job"
PROCESS = "analytics.buffer-aggregate-managed"
CATALOG = "honua://catalog/processes"
PREFIX = "honua.geoprocessing."


def instant(value):
    if not isinstance(value, str):
        raise ValueError("direct cancel timestamp missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("direct cancel timestamp is not absolute")
    return parsed


def authored_plan(nonce):
    if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{24}", nonce):
        raise ValueError("direct cancel requires fresh bounded nonce")
    features = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [n % 100, n // 100]},
                 "properties": {}} for n in range(10000)]
    geojson = json.dumps({"type": "FeatureCollection", "features": features}, separators=(",", ":"), allow_nan=False).encode()
    uri = "data:application/geo+json;base64," + base64.b64encode(geojson).decode("ascii")
    plan_id = "mcp-direct-cancel-" + nonce
    plan = {"planId": plan_id, "intentId": plan_id, "outputs": ["FeatureLayer"], "steps": [
        {"stepId": "owned-buffer", "kind": "Geoprocess", "processId": PROCESS,
         "inputs": {"input": uri, "distance": "0.8", "unit": "meters", "dissolve": "true", "sourceCrs": "EPSG:3857"}}]}
    if (len(features) != 10000 or len(geojson) > 900000
            or len(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params":
                               {"name": "honua_execute_plan", "arguments": {"plan": plan, "idempotencyKey": plan_id}}})) >= 2 * 1024 * 1024):
        raise ValueError("direct cancel workload exceeds reviewed bounded transport")
    return plan


def scenario(fixture_bytes, bootstrap, nonce, canonical_client):
    binding = fixture_binding(fixture_bytes, bootstrap)
    if canonical_client not in ("Official MCP TypeScript SDK", "MCP Inspector"):
        raise ValueError("direct cancel requires canonical client")
    plan = authored_plan(nonce)
    return {key: binding[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha", "candidateImage", "runtimeBinding")} | {
        "schema": "honua.mcp-direct-cancel-scenario/v1", "operation": OPERATION,
        "nonce": nonce, "canonicalClient": canonical_client, "plan": plan,
        "arguments": {"plan": plan, "idempotencyKey": plan["planId"]},
        "fakeJobId": "gp-" + nonce}


def catalog(result):
    contents = result.get("contents") if isinstance(result, dict) else None
    if (not isinstance(contents, list) or len(contents) != 1 or not isinstance(contents[0], dict)
            or set(contents[0]) != {"uri", "mimeType", "text"} or contents[0]["uri"] != CATALOG
            or contents[0]["mimeType"] != "application/json"):
        raise ValueError("direct cancel did not read original process catalog")
    value = strict_json(contents[0]["text"])
    rows = value.get("processes") if isinstance(value, dict) else None
    selected = [row for row in rows if isinstance(row, dict) and row.get("processId") == PROCESS] if isinstance(rows, list) else []
    if (value.get("status") != "ok" or len(selected) != 1
            or selected[0].get("executionKind") != "Job"
            or "job" not in (selected[0].get("entryPoints") or [])
            or "Async" not in (selected[0].get("supportedExecutionModes") or [])):
        raise ValueError("direct cancel workload was not actually job-callable")


def refusal(result, *, anonymous=False):
    if anonymous:
        anonymous_tool_refusal(result)
        return
    value = result.get("structuredContent") if isinstance(result, dict) else None
    if (result.get("isError") is not True or not isinstance(value, dict)
            or value.get("status") != "error" or value.get("code") not in
            ("invalid_argument", "permission_denied", "forbidden", "not_found")
            or not isinstance(value.get("message"), str) or not value["message"]):
        raise ValueError("direct cancel negative lacked typed refusal")


def acknowledgement(result, job_id):
    value = tool_output(result)
    if set(value) != {"jobId", "status", "cancellationRequested"} or value != {
            "jobId": job_id, "status": "cancellation_requested", "cancellationRequested": True}:
        raise ValueError("direct cancel did not acknowledge original job")


def job_resource(result, submission):
    value = resource(result, submission["resourceUri"])
    if (value.get("jobId") != submission["jobId"] or value.get("createdAt") != submission["createdAt"]
            or value.get("resultsUri") != submission["resourceUri"] + "/results"
            or value.get("status") not in ("Queued", "Provisioning", "Running", "Cancelled", "Succeeded", "Failed")):
        raise ValueError("direct cancel poll changed original job identity")
    instant(value.get("updatedAt"))
    if value["status"] == "Cancelled":
        instant(value.get("completedAt"))
    return value


def cancelled_results(result, submission):
    value = resource(result, submission["resourceUri"] + "/results")
    errors = value.get("errors")
    if (value.get("jobId") != submission["jobId"] or value.get("status") != "Cancelled"
            or not isinstance(value.get("resultPackageId"), str) or not value["resultPackageId"]
            or value.get("artifacts") != [] or value.get("totalArtifactCount") != 0
            or value.get("artifactsTruncated") is not False or value.get("workspaceRefs") != []
            or value.get("mapPackageId") is not None or value.get("appPackageId") is not None
            or not isinstance(errors, list) or len(errors) != 1 or errors[0].get("kind") != "Cancelled"):
        raise ValueError("cancelled result package emitted output or wrong lifecycle")
    return value


def checked_store(item, authored, job_id, actor, *, status=None):
    if (not isinstance(item, dict) or set(item) != {"schema", "runtimeBinding", "startedAt", "completedAt", "value", "job"}
            or item["schema"] != "honua.mcp-direct-cancel-store/v1"
            or not same_json(item["runtimeBinding"], authored["runtimeBinding"])
            or instant(item["startedAt"]) > instant(item["completedAt"])):
        raise ValueError("direct cancel independent store observation missing")
    snapshot_output(item["value"])
    job = item["job"]
    if job_id is None:
        if job is not None:
            raise ValueError("direct cancel initial store contains an invented job")
        return None
    rows = [row for row in item["value"]["jobs"] if row["jobId"] == job_id]
    expected_parameters = {PREFIX + "plan_id": authored["plan"]["planId"],
                           PREFIX + "process_definitions": PROCESS,
                           PREFIX + "output_artifact_kinds": "FeatureLayer"}
    expected_parameters.update({PREFIX + "step.0." + key: value
                                for key, value in authored["plan"]["steps"][0]["inputs"].items()})
    if (len(rows) != 1 or not isinstance(job, dict) or job.get("jobId") != job_id
            or job.get("recordSha256") != rows[0]["sha256"] or job.get("planId") != authored["plan"]["planId"]
            or job.get("processDefinitions") != PROCESS
            or job.get("idempotencyKey") != authored["arguments"]["idempotencyKey"]
            or job.get("requestedBy") != actor or type(job.get("status")) is not int
            or job["status"] not in range(6)
            or rows[0]["binding"] != {"operationId": job_id, "status": job["status"],
                                      "createdAt": job.get("createdAt"),
                                      "idempotencyKey": job["idempotencyKey"],
                                      "parameters": expected_parameters}
            or job.get("artifactReferences") != [] or job.get("committedEffectReferences") != []):
        raise ValueError("direct cancel job not bound to original caller/plan or has effects")
    if status is not None and job["status"] not in status:
        raise ValueError("direct cancel durable job has wrong lifecycle")
    instant(job.get("createdAt")); instant(job.get("updatedAt"))
    if job["status"] == 5:
        instant(job.get("completedAt"))
    return job


def judge(fixture_bytes, bootstrap, observed):
    if not isinstance(observed, dict):
        raise ValueError("direct cancel original observations missing")
    authored = scenario(fixture_bytes, bootstrap, observed.get("nonce"), observed.get("canonicalClient"))
    fields = set(authored) - {"plan", "arguments", "fakeJobId"} | {
        "clientVersion", "runtimeBefore", "runtimeAfter", "identityFixtures", "calls", "snapshots", "lateWaitMilliseconds"}
    if (set(observed) != fields or any(not same_json(observed[k], authored[k]) for k in
                                       ("schema", "operation", "nonce", "canonicalClient", "candidateSourceSha", "candidateImage",
                                        "fixtureSha256", "bootstrapSha256", "runtimeBinding"))
            or observed["runtimeBefore"] != authored["runtimeBinding"]
            or observed["runtimeAfter"] != authored["runtimeBinding"]
            or not isinstance(observed["clientVersion"], str)
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", observed["clientVersion"])):
        raise ValueError("direct cancel changed original source, client, runtime or plan")
    identities = observed["identityFixtures"]
    if not isinstance(identities, dict) or set(identities) != {"full", "limited"}:
        raise ValueError("direct cancel lacks managed callers")
    callers = {role: verify_receipt(identities[role], authored["runtimeBinding"], profile)
               for role, profile in (("full", "admin"), ("limited", "limited-read"))}
    if (identities["full"]["credentialSalt"] != identities["limited"]["credentialSalt"]
            or callers["full"]["keyId"] == callers["limited"]["keyId"]):
        raise ValueError("direct cancel callers share a key or differ in original salt")
    calls, snapshots = observed["calls"], observed["snapshots"]
    required_snapshots = {"initial", "preflight", "submitted", "running", "limited", "anonymous", "acknowledged",
                          "terminal", "results", "late"}
    if (not isinstance(calls, list) or not isinstance(snapshots, dict) or set(snapshots) != required_snapshots
            or type(observed["lateWaitMilliseconds"]) is not int or observed["lateWaitMilliseconds"] < 3000):
        raise ValueError("direct cancel omitted original calls, snapshots or late bound")
    sessions = set()
    previous = instant(snapshots["initial"]["completedAt"])
    def call(index, identifier, method, params, role="full"):
        nonlocal previous
        if index >= len(calls):
            raise ValueError("direct cancel omitted an authored call")
        entry = calls[index]
        if (not isinstance(entry, dict)
                or set(entry) != {"id", "method", "params", "authenticated", "credentialRole", "wire", "client",
                                  "result", "startedAt", "completedAt"}
                or entry["id"] != identifier or entry["method"] != method
                or not same_json(entry["params"], params) or entry["credentialRole"] != role
                or entry["authenticated"] is not (role != "anonymous")):
            raise ValueError("direct cancel call differs from original request")
        frame, session = observed_wire(entry["wire"], method, params, role != "anonymous")
        selected = callers["limited" if role == "limited" else "full"]
        if (session in sessions or entry["wire"].get("expectedCredentialFingerprint") != selected["credentialFingerprint"]
                or not same_json(entry["result"], frame if "error" in frame else frame["result"])):
            raise ValueError("direct cancel reused session, caller or substituted wire")
        sessions.add(session)
        started, completed = instant(entry["startedAt"]), instant(entry["completedAt"])
        if (started < previous or completed < started or started < instant(selected["observedAt"])
                or completed > instant(selected["revokedAt"])):
            raise ValueError("direct cancel causal clock or managed lifecycle differs")
        previous = completed
        client_observation(entry["client"], frame, entry["wire"], observed["canonicalClient"], observed["clientVersion"])
        return entry["result"]

    checked_store(snapshots["initial"], authored, None, callers["full"]["canonicalActorId"])
    checked_store(snapshots["preflight"], authored, None, callers["full"]["canonicalActorId"])
    if not same_json(snapshots["initial"]["value"], snapshots["preflight"]["value"]):
        raise ValueError("direct cancel preflight mutated durable records")
    index = 0
    catalog(call(index, "catalog", "resources/read", {"uri": CATALOG})); index += 1
    passed, _ = oracle_mcp_plan_valid(validation_output(call(index, "validate", "tools/call",
        {"name": "honua_validate_plan", "arguments": {"plan": authored["plan"]}})))
    if not passed:
        raise ValueError("direct cancel authored plan did not validate")
    index += 1
    refusal(call(index, "invalid", "tools/call", {"name": "honua_cancel_job", "arguments": {"jobId": ""}})); index += 1
    refusal(call(index, "anonymous-pre", "tools/call", {"name": "honua_cancel_job", "arguments":
           {"jobId": authored["fakeJobId"]}}, "anonymous"), anonymous=True); index += 1
    refusal(call(index, "limited-pre", "tools/call", {"name": "honua_cancel_job", "arguments":
           {"jobId": authored["fakeJobId"]}}, "limited")); index += 1
    if instant(snapshots["preflight"]["startedAt"]) < previous:
        raise ValueError("direct cancel preflight store read preceded negative calls")
    previous = instant(snapshots["preflight"]["completedAt"])
    submitted = accepted(call(index, "execute", "tools/call", {"name": "honua_execute_plan",
        "arguments": authored["arguments"]})); index += 1
    job_id, uri = submitted["jobId"], submitted["resourceUri"]
    if job_id == authored["fakeJobId"]:
        raise ValueError("direct cancel job reused negative identity")
    if instant(snapshots["submitted"]["startedAt"]) < previous:
        raise ValueError("direct cancel submission store read preceded original submit")
    previous = instant(snapshots["submitted"]["completedAt"])
    for name in ("submitted", "running", "limited", "anonymous", "acknowledged", "terminal", "results", "late"):
        allowed = {"submitted": range(6), "running": (2,), "limited": (2,), "anonymous": (2,),
                   "acknowledged": (2, 5), "terminal": (5,), "results": (5,), "late": (5,)}[name]
        checked_store(snapshots[name], authored, job_id, callers["full"]["canonicalActorId"], status=allowed)
    if snapshots["submitted"]["job"]["createdAt"] != submitted["createdAt"]:
        raise ValueError("direct cancel durable creation differs from accepted submission")
    initial_jobs = {row["jobId"]: row for row in snapshots["initial"]["value"]["jobs"]}
    initial_results = {row["jobId"]: row for row in snapshots["initial"]["value"]["results"]}
    if job_id in initial_jobs or job_id in initial_results:
        raise ValueError("direct cancel reused a preexisting job")
    for name in required_snapshots - {"initial", "preflight"}:
        snap = snapshots[name]["value"]
        jobs = {row["jobId"]: row for row in snap["jobs"]}
        results = {row["jobId"]: row for row in snap["results"]}
        if (set(jobs) != set(initial_jobs) | {job_id}
                or any(not same_json(jobs[k], v) for k, v in initial_jobs.items())
                or not set(results).issubset(set(initial_results) | {job_id})
                or any(not same_json(results[k], v) for k, v in initial_results.items())):
            raise ValueError("direct cancel changed unrelated jobs or results")
        own = results.get(job_id)
        if own is not None and (own["binding"].get("artifacts") != []
                                 or own["binding"].get("status") not in (7,)):
            raise ValueError("direct cancel persisted an output or successful result")
    if not same_json(snapshots["running"]["value"], snapshots["limited"]["value"]):
        # An executing worker may advance its own heartbeat/version, but the
        # target cannot be cancelled by a read-only caller.
        if snapshots["limited"]["job"]["status"] != 2:
            raise ValueError("read-only caller cancelled the original job")
    running = 0
    while index < len(calls) and calls[index].get("id") == "poll-running-" + str(running):
        value = job_resource(call(index, "poll-running-" + str(running), "resources/read", {"uri": uri}), submitted)
        if value["status"] in ("Succeeded", "Failed", "Cancelled"):
            raise ValueError("job completed before direct cancellation")
        index += 1; running += 1
        if value["status"] == "Running":
            break
    if running == 0 or value["status"] != "Running":
        raise ValueError("direct cancel never observed the actual job Running")
    if instant(snapshots["running"]["startedAt"]) < previous:
        raise ValueError("direct cancel durable Running read preceded MCP Running")
    previous = instant(snapshots["running"]["completedAt"])
    refusal(call(index, "limited-target", "tools/call", {"name": "honua_cancel_job", "arguments":
           {"jobId": job_id}}, "limited")); index += 1
    if instant(snapshots["limited"]["startedAt"]) < previous:
        raise ValueError("limited denial durable read preceded original refusal")
    previous = instant(snapshots["limited"]["completedAt"])
    refusal(call(index, "anonymous-target", "tools/call", {"name": "honua_cancel_job", "arguments":
           {"jobId": job_id}}, "anonymous"), anonymous=True); index += 1
    if instant(snapshots["anonymous"]["startedAt"]) < previous:
        raise ValueError("anonymous denial durable read preceded original refusal")
    previous = instant(snapshots["anonymous"]["completedAt"])
    acknowledgement(call(index, "owner-cancel", "tools/call", {"name": "honua_cancel_job", "arguments":
                     {"jobId": job_id}}), job_id); index += 1
    if instant(snapshots["acknowledged"]["startedAt"]) < previous:
        raise ValueError("cancellation durable read preceded original request")
    previous = instant(snapshots["acknowledged"]["completedAt"])
    acknowledgement(call(index, "repeat-cancel", "tools/call", {"name": "honua_cancel_job", "arguments":
                     {"jobId": job_id}}), job_id); index += 1
    terminal = 0
    while index < len(calls) and calls[index].get("id") == "poll-terminal-" + str(terminal):
        value = job_resource(call(index, "poll-terminal-" + str(terminal), "resources/read", {"uri": uri}), submitted)
        if value["status"] in ("Succeeded", "Failed"):
            raise ValueError("direct cancel reached wrong terminal state")
        index += 1; terminal += 1
        if value["status"] == "Cancelled":
            break
    if terminal == 0 or value["status"] != "Cancelled":
        raise ValueError("direct cancel has no terminal Cancelled readback")
    if instant(snapshots["terminal"]["startedAt"]) < previous:
        raise ValueError("terminal durable read preceded MCP Cancelled")
    previous = instant(snapshots["terminal"]["completedAt"])
    package = cancelled_results(call(index, "results", "resources/read", {"uri": uri + "/results"}), submitted)
    index += 1
    persisted = [row["binding"] for row in snapshots["results"]["value"]["results"] if row["jobId"] == job_id]
    if (len(persisted) != 1 or persisted[0].get("resultPackageId") != package["resultPackageId"]
            or persisted[0].get("status") != 7 or persisted[0].get("artifacts") != []):
        raise ValueError("cancelled MCP result differs from committed empty durable package")
    if instant(snapshots["results"]["startedAt"]) < previous:
        raise ValueError("result durable read preceded MCP cancelled result")
    previous = instant(snapshots["results"]["completedAt"])
    if job_resource(call(index, "late-job", "resources/read", {"uri": uri}), submitted)["status"] != "Cancelled":
        raise ValueError("late job continuation changed terminal cancellation")
    late_started = instant(calls[index]["startedAt"]); index += 1
    late_package = cancelled_results(call(index, "late-results", "resources/read", {"uri": uri + "/results"}), submitted)
    index += 1
    if (index != len(calls) or not same_json(package, late_package)
            or (late_started - instant(snapshots["results"]["completedAt"])).total_seconds() < 3
            or not same_json(snapshots["results"]["value"], snapshots["late"]["value"])):
        raise ValueError("direct cancel late continuation produced an output or omitted calls")
    if instant(snapshots["late"]["startedAt"]) < previous:
        raise ValueError("direct cancel late durable read preceded actual calls")
    return {key: authored[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha", "candidateImage", "operation")} | {
        "canonicalClient": observed["canonicalClient"], "clientVersion": observed["clientVersion"],
        "positive": True, "negative": True, "originalRunning": True, "terminalCancelled": True,
        "durableNoOutput": True, "boundedLateNoOutput": True, "nativeChildCessationProven": False}


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
        print("owned direct cancel proof refused", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
