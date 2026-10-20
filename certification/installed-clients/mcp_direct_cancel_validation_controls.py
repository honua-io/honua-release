"""Synthetic original-wire cancellation vectors; never candidate/client credit."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import sys
import unittest

import mcp_direct_cancel_validation as oracle
from mcp_ops_events_journey_controls import managed_receipt, stamp, rebind
from mcp_render_validation_controls import FIXTURE, bootstrap


def envelope(body, *, error=False):
    return {"isError": error, "structuredContent": body,
            "content": [{"type": "text", "text": json.dumps(body)}]}


def job_read(uri, job, status, at):
    value = {"jobId": job, "status": status, "createdAt": stamp(50), "updatedAt": stamp(at),
             "completedAt": stamp(110) if status == "Cancelled" else None,
             "resultsUri": uri + "/results", "percentComplete": 0 if status == "Queued" else 30 if status == "Running" else 100,
             "currentPhase": "controlled", "errorMessage": None, "warnings": []}
    return {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(value)}]}


def result_read(uri, job, package):
    value = {"jobId": job, "resultPackageId": package, "status": "Cancelled", "artifacts": [],
             "totalArtifactCount": 0, "artifactsTruncated": False, "workspaceRefs": [],
             "mapPackageId": None, "appPackageId": None,
             "errors": [{"kind": "Cancelled", "message": "Cancelled"}]}
    return {"contents": [{"uri": uri + "/results", "mimeType": "application/json", "text": json.dumps(value)}]}


def journey(*, inspector=False):
    original = bootstrap()
    canonical = "MCP Inspector" if inspector else "Official MCP TypeScript SDK"
    authored = oracle.scenario(FIXTURE, original, "0123456789abcdef01234567", canonical)
    plan, arguments, fake = (authored[key] for key in ("plan", "arguments", "fakeJobId"))
    value = {key: copy.deepcopy(item) for key, item in authored.items() if key not in ("plan", "arguments", "fakeJobId")}
    runtime = original["runtimeBinding"]
    value.update(clientVersion="2.10.1" if inspector else "1.32.1", runtimeBefore=copy.deepcopy(runtime),
                 runtimeAfter=copy.deepcopy(runtime), lateWaitMilliseconds=3000,
                 identityFixtures={role: managed_receipt(role, runtime) for role in ("full", "limited")})
    job = "gp-" + "3" * 24
    uri, package = "honua://jobs/" + job, job + ":v3"
    actor = value["identityFixtures"]["full"]["identity"]["canonicalActorId"]
    params = {oracle.PREFIX + "plan_id": plan["planId"],
              oracle.PREFIX + "process_definitions": oracle.PROCESS,
              oracle.PREFIX + "output_artifact_kinds": "FeatureLayer"}
    params.update({oracle.PREFIX + "step.0." + key: item for key, item in plan["steps"][0]["inputs"].items()})
    def store(start, status=None, *, results=False):
        jobs = []
        if status is not None:
            hashed = "sha256:" + hashlib.sha256(("controlled-job-" + str(status)).encode()).hexdigest()
            jobs = [{"jobId": job, "sha256": hashed, "binding": {"operationId": job, "status": status,
                     "createdAt": stamp(50), "idempotencyKey": plan["planId"], "parameters": params}}]
            owned = {"jobId": job, "recordSha256": hashed, "status": status, "createdAt": stamp(50),
                     "updatedAt": stamp(start), "completedAt": stamp(110) if status == 5 else None,
                     "requestedBy": actor, "tenantId": "public", "idempotencyKey": plan["planId"],
                     "processDefinitions": oracle.PROCESS, "planId": plan["planId"],
                     "artifactReferences": [], "committedEffectReferences": []}
        else:
            owned = None
        result_rows = [{"jobId": job, "sha256": "sha256:" + "a" * 64,
                        "binding": {"resultPackageId": package, "status": 7, "artifacts": [], "errors": []}}] if results else []
        return {"schema": "honua.mcp-direct-cancel-store/v1", "runtimeBinding": copy.deepcopy(runtime),
                "startedAt": stamp(start), "completedAt": stamp(start + 1),
                "value": {"schema": "honua.mcp-execution-persisted/v1", "jobs": jobs, "results": result_rows}, "job": owned}
    value["snapshots"] = {"initial": store(-20), "preflight": store(45), "submitted": store(55, 0),
                          "running": store(65, 2), "limited": store(75, 2), "anonymous": store(85, 2),
                          "acknowledged": store(95, 2), "terminal": store(115, 5),
                          "results": store(125, 5, results=True), "late": store(145, 5, results=True)}
    denied_anon = envelope({"status": "error", "code": "unauthenticated", "message": "Authentication required",
                            "error": {"kind": "AuthorizationDenied", "message": "Authentication required"},
                            "requiresReauthentication": True}, error=True)
    denied_limited = envelope({"status": "error", "code": "permission_denied", "message": "Execute denied"}, error=True)
    ack = envelope({"jobId": job, "status": "cancellation_requested", "cancellationRequested": True})
    calls = [
        ("catalog", "resources/read", {"uri": oracle.CATALOG}, "full", {"contents": [{"uri": oracle.CATALOG,
            "mimeType": "application/json", "text": json.dumps({"status": "ok", "processes": [{"processId": oracle.PROCESS,
                "executionKind": "Job", "entryPoints": ["job"], "supportedExecutionModes": ["Async"]}]})}]}),
        ("validate", "tools/call", {"name": "honua_validate_plan", "arguments": {"plan": plan}}, "full",
            envelope({"isExecutable": True, "requiresApproval": False, "violations": [], "warnings": []})),
        ("invalid", "tools/call", {"name": "honua_cancel_job", "arguments": {"jobId": ""}}, "full",
            envelope({"status": "error", "code": "invalid_argument", "message": "jobId is required"}, error=True)),
        ("anonymous-pre", "tools/call", {"name": "honua_cancel_job", "arguments": {"jobId": fake}}, "anonymous", denied_anon),
        ("limited-pre", "tools/call", {"name": "honua_cancel_job", "arguments": {"jobId": fake}}, "limited", denied_limited),
        ("execute", "tools/call", {"name": "honua_execute_plan", "arguments": arguments}, "full",
            envelope({"jobId": job, "status": "Running", "createdAt": stamp(50), "resourceUri": uri})),
        ("poll-running-0", "resources/read", {"uri": uri}, "full", job_read(uri, job, "Running", 60)),
        ("limited-target", "tools/call", {"name": "honua_cancel_job", "arguments": {"jobId": job}}, "limited", denied_limited),
        ("anonymous-target", "tools/call", {"name": "honua_cancel_job", "arguments": {"jobId": job}}, "anonymous", denied_anon),
        ("owner-cancel", "tools/call", {"name": "honua_cancel_job", "arguments": {"jobId": job}}, "full", ack),
        ("repeat-cancel", "tools/call", {"name": "honua_cancel_job", "arguments": {"jobId": job}}, "full", ack),
        ("poll-terminal-0", "resources/read", {"uri": uri}, "full", job_read(uri, job, "Cancelled", 110)),
        ("results", "resources/read", {"uri": uri + "/results"}, "full", result_read(uri, job, package)),
        ("late-job", "resources/read", {"uri": uri}, "full", job_read(uri, job, "Cancelled", 110)),
        ("late-results", "resources/read", {"uri": uri + "/results"}, "full", result_read(uri, job, package)),
    ]
    value["calls"] = [{"id": ident, "method": method, "params": params, "credentialRole": role,
                       "authenticated": role != "anonymous", "result": result,
                       "startedAt": stamp(index * 10), "completedAt": stamp(index * 10 + 1)}
                      for index, (ident, method, params, role, result) in enumerate(calls)]
    return rebind(value, inspector=inspector)


def judge(value):
    return oracle.judge(FIXTURE, bootstrap(), value)


class DirectCancelValidationControls(unittest.TestCase):
    def test_both_original_synthetic_client_vectors_require_running_cancelled_and_empty_late_result(self):
        for inspector in (False, True):
            with self.subTest(inspector=inspector):
                result = judge(journey(inspector=inspector))
                self.assertTrue(result["originalRunning"] and result["terminalCancelled"])
                self.assertTrue(result["durableNoOutput"] and result["boundedLateNoOutput"])
                self.assertFalse(result["nativeChildCessationProven"])

    def test_early_completion_or_acknowledgement_without_terminal_proof_refuses(self):
        for inspector in (False, True):
            for index, status in ((6, "Succeeded"), (11, "Running")):
                value = journey(inspector=inspector)
                content = value["calls"][index]["result"]["contents"][0]
                body = json.loads(content["text"]); body["status"] = status; content["text"] = json.dumps(body)
                with self.subTest(inspector=inspector, index=index), self.assertRaises(ValueError):
                    judge(rebind(value, inspector=inspector))

    def test_wrong_durable_owner_or_result_effect_refuses(self):
        for inspector in (False, True):
            for mutation in ("owner", "artifact", "late", "status"):
                value = journey(inspector=inspector)
                if mutation == "owner": value["snapshots"]["running"]["job"]["requestedBy"] = "ApiKey:api-key:other"
                elif mutation == "artifact": value["snapshots"]["results"]["value"]["results"][0]["binding"]["artifacts"] = [{"artifactId": "bad"}]
                elif mutation == "late": value["calls"][13]["startedAt"] = stamp(127)
                else: value["snapshots"]["terminal"]["job"]["status"] = 3
                with self.subTest(inspector=inspector, mutation=mutation), self.assertRaises(ValueError):
                    judge(value)

    def test_wrong_caller_same_job_and_missing_cleanup_refuse(self):
        for inspector in (False, True):
            for mutation in ("caller", "job", "cleanup"):
                value = journey(inspector=inspector)
                if mutation == "caller":
                    full = value["identityFixtures"]["full"]["identity"]["credentialFingerprint"]
                    entry = value["calls"][7]
                    entry["wire"]["expectedCredentialFingerprint"] = full
                    for phase in ("initialize", "invocation"):
                        entry["wire"][phase]["request"]["credentialFingerprint"] = full
                elif mutation == "job":
                    value["calls"][10]["params"]["arguments"]["jobId"] = "gp-" + "4" * 24
                    value = rebind(value, inspector=inspector)
                else: value["identityFixtures"]["limited"]["cleanupComplete"] = False
                with self.subTest(inspector=inspector, mutation=mutation), self.assertRaises(ValueError):
                    judge(value)


if __name__ == "__main__":
    if sys.argv[1:] == ["--emit-offline-vector"]:
        original = json.dumps(bootstrap()).encode()
        sdk, inspector = journey(), journey(inspector=True)
        print(json.dumps({"meaning": "Synthetic direct cancel oracle vectors; no actual client/job/candidate evidence",
            "bootstrapBase64": base64.b64encode(original).decode(),
            "sdk": {"judged": judge(sdk), "observations": sdk},
            "inspector": {"judged": judge(inspector), "observations": inspector}}, allow_nan=False))
    else:
        unittest.main()
