"""Independent offline controls; these fixtures supply no candidate/client credit."""
from __future__ import annotations
import base64
import copy
import hashlib
import json
import math
import unittest

import mcp_execute_plan_validation as validation
import mcp_execute_plan_state as state
from mcp_render_validation_controls import FIXTURE, bootstrap, runtime, SOURCE, IMAGE
from mcp_map_package_validation_controls import control_wire


def envelope(value, error=False):
    return {"isError": error, "structuredContent": value, "content": [{"type": "text", "text": json.dumps(value)}]}


def resource(uri, value):
    return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(value)}]}


def rebind(value):
    for index, entry in enumerate(value["calls"]):
        entry["wire"], entry["client"], entry["result"] = control_wire(entry["method"], entry["params"], entry["result"], index, entry["authenticated"])
    return value


def controls():
    authored = validation.scenario(FIXTURE, bootstrap(), "abcdef123456", "Official MCP TypeScript SDK")
    spec = json.loads(FIXTURE)["processes"]
    cx, cy = spec["point"]; distance = spec["distance"]
    # Analytic circle sampling, independent of the server geometry engine.
    ring = [[cx+distance*math.cos(i*math.tau/64), cy+distance*math.sin(i*math.tau/64)] for i in range(64)]
    ring.append(ring[0])
    feature = {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}, "properties": {
        "processId": "geometry.buffer", "inputSrid": 3857, "bufferDistance": distance}}
    identifier = "gp-" + hashlib.sha256(authored["arguments"]["idempotencyKey"].encode()).hexdigest()[:24]
    uri = "honua://jobs/"+identifier
    timestamp = "2026-10-19T00:01:00+00:00"
    accepted = {"jobId": identifier, "status": "Queued", "createdAt": timestamp, "resourceUri": uri}
    job = {"jobId": identifier, "status": "Succeeded", "createdAt": timestamp, "updatedAt": timestamp,
           "completedAt": timestamp, "percentComplete": 100, "warnings": [], "resultsUri": uri+"/results"}
    artifact = {"artifactId": identifier+":artifact:1", "kind": "FeatureLayer", "label": "Authored buffer", "uri":
        "data:application/geo+json;base64,"+base64.b64encode(json.dumps(feature).encode()).decode(), "contentType": "application/geo+json", "metadata": {}}
    package = {"jobId": identifier, "resultPackageId": "controlled-result-package", "status": "Completed", "errors": [],
               "totalArtifactCount": 1, "artifactsTruncated": False, "artifacts": [artifact]}
    def tool(identifier, args, result, auth=True):
        return {"id": identifier, "method": "tools/call", "params": {"name": "honua_execute_plan", "arguments": args}, "authenticated": auth, "result": result}
    def read(identifier, selected, result, auth=True):
        return {"id": identifier, "method": "resources/read", "params": {"uri": selected}, "authenticated": auth, "result": result}
    message = "Plan failed catalog validation: INVALID_PARAMETER_VALUE — WKB must be base64"
    invalid = {"status": "error", "code": "invalid_argument", "message": message, "violations": [{"code": "INVALID_ARGUMENT", "message": message}],
               "error": {"kind": "ValidationFailed", "message": message, "violations": [{"code": "INVALID_ARGUMENT", "message": message}]}}
    denial = {"status": "error", "code": "unauthenticated", "message": "Controlled anonymous execution denial", "requiresReauthentication": True,
              "error": {"kind": "AuthorizationDenied", "message": "Controlled anonymous execution denial"}}
    denied_read = {"error": {"code": -32000, "message": "Controlled anonymous resource denial", "data": {"code": "unauthenticated", "requiresReauthentication": True}}}
    calls = [tool("invalid", authored["invalidArguments"], envelope(invalid, True)), tool("anonymous", authored["anonymousArguments"], envelope(denial, True), False),
        tool("execute", authored["arguments"], envelope(accepted)), read("poll-0", uri, resource(uri, job)),
        read("results", uri+"/results", resource(uri+"/results", package)), tool("replay", authored["arguments"], envelope({**accepted, "status": "Succeeded"})),
        read("anonymous-job", uri, copy.deepcopy(denied_read), False), read("anonymous-results", uri+"/results", copy.deepcopy(denied_read), False),
        read("restarted-job", uri, resource(uri, job)), read("restarted-results", uri+"/results", resource(uri+"/results", package))]
    parameters = {validation.PREFIX+"plan_id": authored["arguments"]["plan"]["planId"], validation.PREFIX+"process_definitions": "geometry.buffer",
                  validation.PREFIX+"output_artifact_kinds": "FeatureLayer"}
    parameters.update({validation.PREFIX+"step.0."+k: v for k,v in authored["arguments"]["plan"]["steps"][0]["inputs"].items()})
    persisted_job = {"operationId": identifier, "status": 3, "createdAt": timestamp, "spec": {"parameters": parameters}, "audit": {"idempotencyKey": authored["arguments"]["idempotencyKey"]}}
    persisted_result = {"resultPackageId": package["resultPackageId"], "status": 5, "errors": [], "artifacts": [{**artifact, "kind": 1}]}
    raw = [[b"jobs", identifier.encode(), json.dumps(persisted_job).encode()], [b"results", identifier.encode(), json.dumps(persisted_result).encode()]]
    committed = state.snapshot(authored["arguments"]["plan"]["planId"], execute=lambda _: raw)
    empty = {"schema": "honua.mcp-execution-persisted/v1", "jobs": [], "results": []}
    snapshots = {name: copy.deepcopy(empty if name in ("initial", "invalid", "anonymous") else committed) for name in
        ("initial", "invalid", "anonymous", "completed", "replay", "anonymous-job", "anonymous-results", "restarted", "restarted-readback")}
    restarted = runtime(); restarted["startedAt"] = "2026-10-19T00:05:00Z"
    return rebind({"schema": validation.SCHEMA, "nonce": authored["nonce"], "canonicalClient": authored["canonicalClient"], "clientVersion": "1.32.1",
        "candidateSourceSha": SOURCE, "candidateImage": IMAGE, "calls": calls, "snapshots": snapshots,
        "runtimeBefore": runtime(), "runtimeRestarted": restarted, "runtimeAfter": copy.deepcopy(restarted)})


class ExecutionControls(unittest.TestCase):
    def assert_refused(self, value):
        with self.assertRaises((ValueError, TypeError, KeyError, IndexError, AttributeError)):
            validation.judge(FIXTURE, bootstrap(), value)

    def test_complete_authored_execution(self):
        proof = validation.judge(FIXTURE, bootstrap(), controls())
        self.assertTrue(proof["geometricResult"] and proof["negativeNonmutation"] and proof["idempotentReplay"])

    def test_refused_and_readonly_checkpoints_preserve_all_bytes(self):
        for checkpoint in ("invalid", "anonymous", "replay", "anonymous-job", "anonymous-results", "restarted", "restarted-readback"):
            with self.subTest(checkpoint=checkpoint):
                value = controls(); value["snapshots"][checkpoint]["jobs"].append({"jobId": "hidden-extra", "sha256": "sha256:"+"a"*64, "binding": {}})
                self.assert_refused(value)
        for family in ("jobs", "results"):
            value = controls(); value["snapshots"]["restarted"][family][0]["sha256"] = "sha256:"+"b"*64
            self.assert_refused(value)

    def test_missing_or_changed_runtime_restart(self):
        for key in ("runtimeBefore", "runtimeRestarted", "runtimeAfter"):
            value = controls(); original = value[key]["containerId"]
            value[key]["containerId"] = ("a" if original[0] != "a" else "b") + original[1:]
            self.assertNotEqual(value[key]["containerId"], original)
            self.assert_refused(value)
        value = controls(); value["runtimeRestarted"] = value["runtimeAfter"] = runtime(); self.assert_refused(value)

    def test_replay_and_job_identity_are_actual(self):
        for call, field, changed in ((2, "jobId", "gp-"+"f"*24), (5, "jobId", "gp-"+"f"*24), (5, "createdAt", "2026-10-19T00:02:00Z")):
            value = controls(); output = value["calls"][call]["result"]["structuredContent"]; output[field] = changed
            value["calls"][call]["result"] = envelope(output); self.assert_refused(rebind(value))
        for field in ("idempotencyKey", "parameters", "status"):
            value = controls(); value["snapshots"]["completed"]["jobs"][0]["binding"][field] = None; self.assert_refused(value)

    def test_resource_media_job_and_package_refusals(self):
        for index in (3, 4, 8, 9):
            for field, changed in (("uri", "honua://jobs/other"), ("mimeType", "text/plain")):
                value = controls(); value["calls"][index]["result"]["contents"][0][field] = changed; self.assert_refused(rebind(value))
        for field, changed in (("status", "Failed"), ("totalArtifactCount", 2), ("artifactsTruncated", True), ("errors", [{"kind":"Failure"}]), ("jobId", "other")):
            value = controls(); content = value["calls"][4]["result"]["contents"][0]; package = json.loads(content["text"]); package[field] = changed; content["text"] = json.dumps(package)
            self.assert_refused(rebind(value))

    def test_geometry_independent_center_radius_crs_and_topology(self):
        fixture = json.loads(FIXTURE)["processes"]
        source = controls()["calls"][4]["result"]["contents"][0]["text"]
        artifact = json.loads(source)["artifacts"][0]
        feature = json.loads(base64.b64decode(artifact["uri"].split(",",1)[1]))
        for mutation in ("radius", "center", "crs", "distance", "crossed", "hole", "open", "nan", "duplicate", "diamond"):
            with self.subTest(mutation=mutation):
                value = copy.deepcopy(feature); ring = value["geometry"]["coordinates"][0]
                if mutation == "radius": ring[1][0] += 1
                elif mutation == "center":
                    for point in ring: point[0] += 1
                elif mutation == "crs": value["properties"]["inputSrid"] = True
                elif mutation == "distance": value["properties"]["bufferDistance"] = 42
                elif mutation == "crossed": ring[1], ring[32] = ring[32], ring[1]
                elif mutation == "hole": value["geometry"]["coordinates"].append(copy.deepcopy(ring))
                elif mutation == "open": ring.pop()
                elif mutation == "nan": ring[1][0] = float("nan")
                elif mutation == "duplicate": ring[2] = copy.deepcopy(ring[1])
                elif mutation == "diamond": value["geometry"]["coordinates"][0] = ring[::16]+[ring[0]]
                with self.assertRaises(ValueError): validation.buffer_geometry(value, fixture)

    def test_negative_refusals_cannot_be_success_or_unrelated(self):
        for index in (0, 1):
            for field, changed in (("code", "not_found"), ("jobId", "unexpected-created-job")):
                value = controls(); output = value["calls"][index]["result"]["structuredContent"]; output[field] = changed
                value["calls"][index]["result"] = envelope(output, True); self.assert_refused(rebind(value))
            value = controls(); value["calls"][index]["result"]["isError"] = False; self.assert_refused(rebind(value))

    def test_wire_authority_and_session_binding(self):
        for request in ("initialize", "invocation"):
            value = controls(); value["calls"][1]["wire"][request]["request"]["credentialPresent"] = True; self.assert_refused(value)
        value = controls(); value["calls"][1]["wire"]["invocation"]["request"]["sessionSha256"] = value["calls"][0]["wire"]["invocation"]["request"]["sessionSha256"]
        self.assert_refused(value)

    def test_atomic_census_multiplicity_and_full_hashes(self):
        self.assertIn("EVAL_RO", state.snapshot.__code__.co_consts)
        value = controls()["snapshots"]["completed"]
        value["jobs"].append(copy.deepcopy(value["jobs"][0]))
        with self.assertRaises(ValueError): validation.snapshot_output(value)
        with self.assertRaises(ValueError): state.snapshot("owned", execute=lambda _: [[b"jobs", b"id", b'{"operationId":"other"}']])


if __name__ == "__main__":
    unittest.main()
