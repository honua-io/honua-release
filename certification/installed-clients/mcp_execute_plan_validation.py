"""Pure authored execute/replay/durable-result semantics; no transport or stores."""
from __future__ import annotations
import argparse
import base64
import copy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_plan_validation import buffer_plan
from mcp_render_validation import scenario as fixture_binding
from mcp_map_package_validation import strict_json, same_json, client_observation, anonymous_tool_refusal, resource_refusal
from mcp_ingest_publish_validation import invocation_wire
from mcp_style_validation import tool_output

SCHEMA = "honua.mcp-execute-plan-scenario/v1"
OPERATION = "tools/call:execute_plan"
PREFIX = "honua.geoprocessing."


def scenario(fixture_bytes, bootstrap, nonce, canonical_client):
    binding = fixture_binding(fixture_bytes, bootstrap)
    if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{12}", nonce):
        raise ValueError("execution requires owned run identity")
    if canonical_client not in ("Official MCP TypeScript SDK", "MCP Inspector"):
        raise ValueError("execution requires actual canonical client")
    slug = "sdk" if canonical_client == "Official MCP TypeScript SDK" else "inspector"
    plan = buffer_plan(strict_json(fixture_bytes)["processes"])
    plan["planId"] = plan["intentId"] = f"mcp-execute-{slug}-{nonce}"
    arguments = {"plan": plan, "idempotencyKey": plan["planId"]}
    invalid = copy.deepcopy(arguments)
    invalid["plan"]["planId"] += "-invalid"
    invalid["idempotencyKey"] += "-invalid"
    invalid["plan"]["steps"][0]["inputs"]["wkb"] = "not-base64"
    anonymous = copy.deepcopy(arguments)
    anonymous["plan"]["planId"] += "-anonymous"
    anonymous["idempotencyKey"] += "-anonymous"
    return {key: binding[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha", "candidateImage", "runtimeBinding")} | {
        "schema": SCHEMA, "operation": OPERATION, "nonce": nonce, "canonicalClient": canonical_client,
        "arguments": arguments, "invalidArguments": invalid, "anonymousArguments": anonymous}


def accepted(result):
    value = tool_output(result)
    if (set(value) != {"jobId", "status", "createdAt", "resourceUri"}
            or not isinstance(value["jobId"], str) or not re.fullmatch(r"gp-[a-f0-9]{24,32}", value["jobId"])
            or value["status"] not in ("Queued", "Provisioning", "Running", "Succeeded")
            or value["resourceUri"] != "honua://jobs/" + value["jobId"]):
        raise ValueError("execute returned no accepted canonical job")
    timestamp(value["createdAt"])
    return value


def timestamp(value):
    if not isinstance(value, str) or datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("job timestamp missing or lacks timezone")


def resource(result, uri):
    items = result.get("contents") if isinstance(result, dict) else None
    if ("error" in result or not isinstance(items, list) or len(items) != 1
            or not isinstance(items[0], dict) or set(items[0]) != {"uri", "mimeType", "text"}
            or items[0]["uri"] != uri or items[0]["mimeType"] != "application/json" or not isinstance(items[0]["text"], str)):
        raise ValueError("job resource JSON media/identity missing")
    value = strict_json(items[0]["text"])
    if not isinstance(value, dict):
        raise ValueError("job resource is not an object")
    return value


def job_output(result, submission, *, terminal):
    value = resource(result, submission["resourceUri"])
    if (value.get("jobId") != submission["jobId"] or value.get("createdAt") != submission["createdAt"]
            or value.get("resultsUri") != submission["resourceUri"] + "/results"
            or value.get("status") not in (("Succeeded",) if terminal else ("Queued", "Provisioning", "Running", "Succeeded"))
            or value.get("errorMessage") is not None or value.get("warnings") != []):
        raise ValueError("job identity/lifecycle differs or execution failed")
    timestamp(value.get("updatedAt"))
    if terminal:
        timestamp(value.get("completedAt"))
        if type(value.get("percentComplete")) not in (int, float) or value["percentComplete"] != 100:
            raise ValueError("successful job has incomplete progress")
    return value


def buffer_geometry(feature, processes):
    if (not isinstance(feature, dict) or feature.get("type") != "Feature"
            or not isinstance(feature.get("properties"), dict)
            or feature["properties"].get("processId") != processes["processId"]
            or type(feature["properties"].get("inputSrid")) is not int
            or feature["properties"]["inputSrid"] != processes["srid"]
            or type(feature["properties"].get("bufferDistance")) not in (int, float)
            or feature["properties"]["bufferDistance"] != processes["distance"]):
        raise ValueError("buffer artifact lost process/CRS provenance")
    geometry = feature.get("geometry")
    rings = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if not isinstance(geometry, dict) or geometry.get("type") != "Polygon" or not isinstance(rings, list) or len(rings) != 1:
        raise ValueError("point buffer must be a single polygon ring")
    ring = rings[0]
    if (not isinstance(ring, list) or not 32 <= len(ring) <= 4097 or ring[0] != ring[-1]
            or any(not isinstance(p, list) or len(p) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) for v in p) for p in ring)):
        raise ValueError("buffer ring is malformed, unclosed or unbounded")
    vertices = ring[:-1]
    if len({tuple(p) for p in vertices}) != len(vertices):
        raise ValueError("buffer repeats vertices")
    cx, cy = processes["point"]
    distance, tolerance = processes["distance"], processes["tolerance"]
    if any(abs(math.hypot(x-cx, y-cy)-distance) > tolerance for x, y in vertices):
        raise ValueError("buffer boundary differs from authored radius/center")
    cross = [((b[0]-a[0])*(c[1]-b[1])-(b[1]-a[1])*(c[0]-b[0]))
             for a, b, c in zip(vertices, vertices[1:]+vertices[:1], vertices[2:]+vertices[:2])]
    # Same-radius crossed/reordered rings must not pass a radial-only oracle.
    angles = [math.atan2(y-cy, x-cx) for x, y in vertices]
    deltas = [((b-a+math.pi) % (2*math.pi))-math.pi for a, b in zip(angles, angles[1:]+angles[:1])]
    if (not (all(v > 0 for v in cross) or all(v < 0 for v in cross))
            or not math.isclose(abs(sum(deltas)), 2*math.pi, abs_tol=1e-9)
            or not (all(v > 0 for v in deltas) or all(v < 0 for v in deltas))):
        raise ValueError("buffer is not one convex noncrossing circle traversal")
    area = abs(sum((a[0]-cx)*(b[1]-cy)-(b[0]-cx)*(a[1]-cy) for a, b in zip(ring, ring[1:]))) / 2
    if (not math.isclose(area, math.pi*distance**2, rel_tol=0.01)
            or math.hypot(sum(x for x, _ in vertices)/len(vertices)-cx, sum(y for _, y in vertices)/len(vertices)-cy) > tolerance
            or any(math.hypot((a[0]+b[0])/2-cx, (a[1]+b[1])/2-cy) < distance*0.99 for a, b in zip(ring, ring[1:]))):
        raise ValueError("buffer area, centroid or chord coverage differs from authored circle")


def results_output(result, submission, fixture_bytes):
    value = resource(result, submission["resourceUri"] + "/results")
    artifacts = value.get("artifacts")
    if (value.get("jobId") != submission["jobId"] or value.get("status") != "Completed" or value.get("errors") != []
            or value.get("artifactsTruncated") is not False or type(value.get("totalArtifactCount")) is not int
            or value["totalArtifactCount"] != 1 or not isinstance(artifacts, list) or len(artifacts) != 1
            or not isinstance(value.get("resultPackageId"), str) or not value["resultPackageId"]):
        raise ValueError("job result package missing, failed or incomplete")
    artifact = artifacts[0]
    prefix = "data:application/geo+json;base64,"
    if (not isinstance(artifact, dict) or artifact.get("kind") != "FeatureLayer"
            or artifact.get("contentType") != "application/geo+json" or artifact.get("artifactId") != submission["jobId"]+":artifact:1"
            or not isinstance(artifact.get("uri"), str) or not artifact["uri"].startswith(prefix) or len(artifact["uri"]) > 2*1024*1024):
        raise ValueError("job buffer artifact is not the supported identified inline GeoJSON")
    feature = strict_json(base64.b64decode(artifact["uri"][len(prefix):], validate=True))
    buffer_geometry(feature, strict_json(fixture_bytes)["processes"])
    return value


def invalid_refusal(result):
    if not isinstance(result, dict) or result.get("isError") is not True:
        raise ValueError("invalid plan did not produce a tool refusal")
    value = result.get("structuredContent")
    text = result.get("content")
    if (not isinstance(value, dict) or value.get("status") != "error" or value.get("code") != "invalid_argument"
            or not isinstance(value.get("error"), dict) or value["error"].get("kind") != "ValidationFailed"
            or not isinstance(value.get("message"), str) or "base64" not in value["message"].lower()
            or set(result) != {"isError", "structuredContent", "content"}
            or set(value) - {"status", "code", "message", "violations", "error"}
            or value["error"].get("message") != value["message"]
            or set(value["error"]) != {"kind", "message", "violations"}
            or not isinstance(value.get("violations"), list) or len(value["violations"]) != 1
            or not same_json(value["violations"], value["error"].get("violations"))
            or value["violations"][0].get("code") != "INVALID_ARGUMENT"
            or value["violations"][0].get("message") != value["message"]
            or not isinstance(text, list) or len(text) != 1 or text[0].get("type") != "text"
            or not same_json(strict_json(text[0]["text"]), value)
            or any(value.get(key) is not None for key in ("jobId", "resourceUri", "proposalId", "operationInstanceId"))):
        raise ValueError("invalid WKB refusal lacks actual typed validation diagnosis")


def snapshot_output(snapshot):
    if not isinstance(snapshot, dict) or set(snapshot) != {"schema", "jobs", "results"} or snapshot["schema"] != "honua.mcp-execution-persisted/v1":
        raise ValueError("independent durable job census missing")
    for family in ("jobs", "results"):
        rows = snapshot[family]
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError("durable job census unbounded")
        keys = []
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {"jobId", "sha256", "binding"}
                    or not isinstance(row["jobId"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", row["jobId"])
                    or not isinstance(row["sha256"], str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", row["sha256"])
                    or not isinstance(row["binding"], dict)):
                raise ValueError("durable census row identity/hash malformed")
            keys.append(row["jobId"])
        if keys != sorted(set(keys)):
            raise ValueError("durable census duplicate or unordered identity")


def committed(snapshot, initial, submission, authored, package):
    snapshot_output(snapshot)
    identifier = submission["jobId"]
    for family in ("jobs", "results"):
        before = {r["jobId"]: r for r in initial[family]}
        after = {r["jobId"]: r for r in snapshot[family]}
        if identifier in before or set(after) != set(before) | {identifier} or any(not same_json(after[k], v) for k, v in before.items()):
            raise ValueError("execution altered prior durable records or created extra jobs/results")
    job = next(r["binding"] for r in snapshot["jobs"] if r["jobId"] == identifier)
    plan = authored["arguments"]["plan"]
    expected = {PREFIX+"plan_id": plan["planId"], PREFIX+"process_definitions": "geometry.buffer", PREFIX+"output_artifact_kinds": "FeatureLayer"}
    expected.update({PREFIX+"step.0."+k: v for k, v in plan["steps"][0]["inputs"].items()})
    if (job.get("operationId") != identifier or type(job.get("status")) is not int or job["status"] != 3 or job.get("createdAt") != submission["createdAt"]
            or job.get("idempotencyKey") != authored["arguments"]["idempotencyKey"]
            or not same_json(job.get("parameters"), expected)):
        raise ValueError("durable job lost authored plan, key, lifecycle or identity")
    stored = next(r["binding"] for r in snapshot["results"] if r["jobId"] == identifier)
    if (stored.get("resultPackageId") != package["resultPackageId"] or type(stored.get("status")) is not int or stored["status"] != 5
            or not same_json(stored.get("artifacts"), [{**a, "kind": 1} for a in package["artifacts"]]) or stored.get("errors") != []):
        raise ValueError("resource result differs from actual durable result package")


def judge(fixture_bytes, bootstrap, observed):
    if not isinstance(observed, dict):
        raise ValueError("execution observations missing")
    authored = scenario(fixture_bytes, bootstrap, observed.get("nonce"), observed.get("canonicalClient"))
    fields = {"schema", "nonce", "canonicalClient", "clientVersion", "candidateSourceSha", "candidateImage", "calls", "snapshots", "runtimeBefore", "runtimeRestarted", "runtimeAfter"}
    if set(observed) != fields or any(not same_json(observed[k], authored[k]) for k in ("schema", "candidateSourceSha", "candidateImage")):
        raise ValueError("execution observations belong to another scenario")
    before, restarted, after = (observed[k] for k in ("runtimeBefore", "runtimeRestarted", "runtimeAfter"))
    for runtime in (before, restarted, after):
        if not isinstance(runtime, dict) or not same_json({**runtime, "startedAt": authored["runtimeBinding"]["startedAt"]}, authored["runtimeBinding"]):
            raise ValueError("execution actual candidate identity changed")
    if not same_json(after, restarted) or datetime.fromisoformat(restarted["startedAt"].replace("Z", "+00:00")) <= datetime.fromisoformat(before["startedAt"].replace("Z", "+00:00")):
        raise ValueError("execution did not prove same-container restart")
    calls = observed["calls"]
    if not isinstance(calls, list) or not 10 <= len(calls) <= 130:
        raise ValueError("execution call sequence incomplete or unbounded")
    sessions, fingerprints = set(), set()
    def call(index, identifier, method, params, authenticated=True):
        entry = calls[index]
        if (not isinstance(entry, dict) or set(entry) != {"id", "method", "params", "authenticated", "result", "wire", "client"}
                or entry["id"] != identifier or entry["method"] != method or entry["authenticated"] is not authenticated or not same_json(entry["params"], params)):
            raise ValueError("execution call differs from independently authored request")
        frame, session = invocation_wire(entry["wire"], method, params, authenticated, observed["canonicalClient"])
        if session in sessions or not same_json(entry["result"], frame if "error" in frame else frame["result"]):
            raise ValueError("execution reused session or changed observed response")
        sessions.add(session); fingerprints.add(entry["wire"]["expectedCredentialFingerprint"])
        client_observation(entry["client"], frame, entry["wire"], observed["canonicalClient"], observed["clientVersion"])
        return entry["result"]
    def tool(index, identifier, args, auth=True):
        return call(index, identifier, "tools/call", {"name": "honua_execute_plan", "arguments": args}, auth)
    invalid_refusal(tool(0, "invalid", authored["invalidArguments"]))
    anonymous_tool_refusal(tool(1, "anonymous", authored["anonymousArguments"], False))
    submission = accepted(tool(2, "execute", authored["arguments"]))
    uri = submission["resourceUri"]
    index = 3
    while index < len(calls) and calls[index].get("id") == f"poll-{index-3}":
        status = job_output(call(index, f"poll-{index-3}", "resources/read", {"uri": uri}), submission, terminal=False)
        index += 1
        if status["status"] == "Succeeded":
            break
    else:
        raise ValueError("execution lacks fresh terminal poll")
    package = results_output(call(index, "results", "resources/read", {"uri": uri+"/results"}), submission, fixture_bytes)
    replay = accepted(tool(index+1, "replay", authored["arguments"]))
    if any(replay[k] != submission[k] for k in ("jobId", "createdAt", "resourceUri")) or replay["status"] != "Succeeded":
        raise ValueError("idempotent replay returned a different/nonterminal job")
    resource_refusal(call(index+2, "anonymous-job", "resources/read", {"uri": uri}, False), -32000, "unauthenticated")
    resource_refusal(call(index+3, "anonymous-results", "resources/read", {"uri": uri+"/results"}, False), -32000, "unauthenticated")
    restarted_job = job_output(call(index+4, "restarted-job", "resources/read", {"uri": uri}), submission, terminal=True)
    if not same_json(restarted_job, status):
        raise ValueError("restart changed completed job readback")
    repeated = results_output(call(index+5, "restarted-results", "resources/read", {"uri": uri+"/results"}), submission, fixture_bytes)
    if index+6 != len(calls) or not same_json(repeated, package) or len(fingerprints) != 1:
        raise ValueError("execution result/authority changed or unrelated calls appended")
    snapshots = observed["snapshots"]
    if not isinstance(snapshots, dict) or set(snapshots) != {"initial", "invalid", "anonymous", "completed", "replay", "anonymous-job", "anonymous-results", "restarted", "restarted-readback"}:
        raise ValueError("execution persisted checkpoints incomplete")
    for value in snapshots.values():
        snapshot_output(value)
    if any(not same_json(snapshots[name], snapshots["initial"]) for name in ("invalid", "anonymous")):
        raise ValueError("refused execution changed actual durable records")
    committed(snapshots["completed"], snapshots["initial"], submission, authored, package)
    if any(not same_json(snapshots[name], snapshots["completed"]) for name in ("replay", "anonymous-job", "anonymous-results", "restarted", "restarted-readback")):
        raise ValueError("replay, refusal, fresh read or restart changed durable job/results")
    return {key: authored[key] for key in ("candidateSourceSha", "candidateImage", "fixtureSha256", "bootstrapSha256")} | {
        "schema": SCHEMA, "operation": OPERATION, "canonicalClient": observed["canonicalClient"], "clientVersion": observed["clientVersion"],
        "positive": True, "negative": True, "mediaSchema": True, "idempotentReplay": True, "geometricResult": True,
        "persistedReadback": True, "restartReadback": True, "anonymousInvocationRefusal": True, "negativeNonmutation": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True); parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--nonce"); parser.add_argument("--canonical-client"); parser.add_argument("--observations", type=Path)
    args = parser.parse_args()
    try:
        fixture, bootstrap = args.fixture.read_bytes(), strict_json(args.bootstrap.read_bytes())
        value = judge(fixture, bootstrap, strict_json(args.observations.read_bytes())) if args.observations else scenario(fixture, bootstrap, args.nonce, args.canonical_client)
        print(json.dumps(value, allow_nan=False)); return 0
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OSError):
        print("authored canonical execution evidence refused", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
