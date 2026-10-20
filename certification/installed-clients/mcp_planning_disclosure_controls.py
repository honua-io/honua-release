"""Synthetic full-wire planning controls; never actual client/model evidence."""
from __future__ import annotations

import copy
import base64
import json
import sys
import unittest

import mcp_planning_disclosure as oracle
from mcp_ops_events_journey_controls import managed_receipt, stamp, rebind
from mcp_render_validation_controls import FIXTURE, bootstrap


def response(value, *, error=False):
    return {"isError": error, "structuredContent": value,
            "content": [{"type": "text", "text": json.dumps(value)}]}


def demo(context):
    # Literal public contract vector, independently authored from the demo's
    # advertised filter/DAG. It does not call the production mapper or oracle.
    return {"engine": "fixture", "status": "planned", "fixtureCase": "success",
        "contractVersion": "honua.ai_builder.spatial_query.v1", "context": context,
        "warnings": [{"code": "mutable_source_cache_warning",
                      "message": "Sources can change; preserve the recorded snapshot token."}],
        "nextSteps": "Fixture demo, not compiled from your intent. Your client model can read "
                     "honua://catalog/processes and use honua_validate_plan and honua_dry_run_plan "
                     "before honua_execute_plan.",
        "plan": {"planId": "plan-success-linked-dashboard", "intentId": "success-linked-dashboard",
            "steps": [
                {"stepId": "query-open-hospitals", "kind": "QueryFeatures", "dependsOn": [], "inputs": {
                    "layerId": "1", "where": "facility_type = 'hospital' AND status = 'open'"}},
                {"stepId": "distance-filter", "kind": "Geoprocess", "processId": "analytics.spatial-join",
                    "dependsOn": ["query-open-hospitals"], "inputs": {
                        "layerId": "1", "joinLayerId": "2", "predicate": "dwithin", "distance": "1000"}},
                {"stepId": "compose-packages", "kind": "Export", "dependsOn": ["distance-filter"], "inputs": {}}],
            "outputs": [], "warnings": ["mutable_source_cache_warning"]}}


def journey(*, inspector=False):
    original = bootstrap()
    canonical = "MCP Inspector" if inspector else "Official MCP TypeScript SDK"
    value = oracle.scenario(FIXTURE, original, "0123456789abcdef01234567", canonical)
    runtime = original["runtimeBinding"]
    empty = {"schema": "honua.mcp-execution-persisted/v1", "jobs": [], "results": []}
    value.update(clientVersion="2.10.1" if inspector else "1.32.1",
        runtimeBefore=copy.deepcopy(runtime), runtimeAfter=copy.deepcopy(runtime),
        identityFixtures={role: managed_receipt(role, runtime) for role in ("full", "limited")},
        snapshots=[{"schema": "honua.mcp-planning-store/v1", "runtimeBinding": copy.deepcopy(runtime),
                    "startedAt": stamp(start), "completedAt": stamp(start + 1), "value": copy.deepcopy(empty)}
                   for start in (-20, 100)])
    for index, entry in enumerate(value["calls"]):
        entry.update(startedAt=stamp(10 * index), completedAt=stamp(10 * index + 1))
        expectation = entry["expectation"]
        if expectation == "catalog":
            catalog = {"status": "ok", "catalogVersion": "controlled-original",
                "processes": [{"processId": "geometry.buffer", "executionKind": "Job", "entryPoints": ["job"],
                               "supportedExecutionModes": ["Async"]}]}
            entry["result"] = {"contents": [{"uri": "honua://catalog/processes", "mimeType": "application/json",
                                            "text": json.dumps(catalog)}]}
        elif expectation == "demo":
            entry["result"] = response(demo(copy.deepcopy(entry["params"]["arguments"]["context"])))
        elif expectation == "unmatched":
            output = demo(copy.deepcopy(entry["params"]["arguments"]["context"]))
            output.update(status="rejected", plan=None, specDraft=None, appPackage=None,
                reason="Fixture demo did not attempt to compile this unmatched intent; use a client model and honua_validate_plan.")
            entry["result"] = response(output)
        elif expectation == "handoff":
            entry["result"] = response({"isExecutable": True, "requiresApproval": False, "violations": [], "warnings": []})
        elif expectation == "invalid":
            entry["result"] = response({"status": "error", "code": "invalid_argument", "message": "Missing intent"}, error=True)
        else:
            entry["result"] = response({"status": "error", "code": "unauthenticated", "message": "Authentication required",
                "error": {"kind": "AuthorizationDenied", "message": "Authentication required"},
                "requiresReauthentication": True}, error=True)
    return rebind(value, inspector=inspector)


def judge(value):
    return oracle.judge(FIXTURE, bootstrap(), value)


def change_result(value, index, mutate, inspector):
    entry = value["calls"][index]
    mutate(entry["result"]["structuredContent"])
    entry["result"]["content"][0]["text"] = json.dumps(entry["result"]["structuredContent"])
    return rebind(value, inspector=inspector)


class PlanningDisclosureControls(unittest.TestCase):
    def test_both_complete_original_wire_vectors_pass_without_model_or_execution_credit(self):
        for inspector in (False, True):
            with self.subTest(inspector=inspector):
                proof = judge(journey(inspector=inspector))
                self.assertTrue(proof["positive"] and proof["negative"] and proof["fixtureDisclosure"])
                self.assertTrue(proof["handAuthoredValidation"] and proof["durableJobsResultsUnchanged"])
                self.assertFalse(proof["liveModelProven"] or proof["executionProven"])

    def test_coherent_engine_disclosure_context_dag_and_handoff_changes_refuse(self):
        mutations = [
            (1, lambda body: body.update(engine="live")),
            (1, lambda body: body.update(nextSteps="This tailored analysis is ready to execute")),
            (1, lambda body: body.update(context={"qualificationNonce": "different"})),
            (1, lambda body: body["plan"]["steps"][0]["inputs"].update(where="1=1")),
            (1, lambda body: body["plan"]["steps"][1].update(dependsOn=[])),
            (1, lambda body: body["plan"].update(outputs=["FeatureLayer"])),
            (1, lambda body: body["plan"].update(warnings=[])),
            (1, lambda body: body.update(warnings=[])),
            (1, lambda body: body.update(warnings=["mutable_source_cache_warning"])),
            (1, lambda body: body["warnings"][0].update(message="Everything is reproducible forever")),
            (3, lambda body: body.update(status="planned", plan={"steps": []})),
            (3, lambda body: body.update(reason="Your request is unsupported")),
            (4, lambda body: body.update(isExecutable=False)),
            (4, lambda body: body.update(warnings=[False])),
        ]
        for inspector in (False, True):
            for index, mutation in mutations:
                with self.subTest(inspector=inspector, index=index, mutation=mutations.index((index, mutation))):
                    value = change_result(journey(inspector=inspector), index, mutation, inspector)
                    with self.assertRaises(ValueError):
                        judge(value)

    def test_coherent_wrong_caller_is_not_hidden_by_valid_wire_and_client(self):
        for inspector in (False, True):
            value = journey(inspector=inspector)
            entry = value["calls"][2]
            fingerprint = value["identityFixtures"]["full"]["identity"]["credentialFingerprint"]
            entry["wire"]["expectedCredentialFingerprint"] = fingerprint
            for phase in ("initialize", "invocation"):
                entry["wire"][phase]["request"]["credentialFingerprint"] = fingerprint
            with self.subTest(inspector=inspector), self.assertRaisesRegex(ValueError, "caller"):
                judge(value)

    def test_missing_cleanup_original_identity_stores_calls_and_runtime_refuse(self):
        mutations = [
            lambda value: value["identityFixtures"]["limited"].update(cleanupComplete=False),
            lambda value: value["identityFixtures"].pop("full"),
            lambda value: value["calls"].pop(2),
            lambda value: value["snapshots"].pop(),
            lambda value: value["snapshots"][1]["value"]["jobs"].append({"jobId": "extra", "sha256": "sha256:" + "a" * 64, "binding": {}}),
            lambda value: value["runtimeAfter"].update(containerId="f" * 64),
            lambda value: value["calls"][0].update(startedAt=stamp(-100)),
            lambda value: value["calls"][-1].update(completedAt=stamp(3600)),
            lambda value: value["snapshots"][1].update(startedAt=stamp(-10)),
        ]
        for inspector in (False, True):
            for index, mutate in enumerate(mutations):
                value = journey(inspector=inspector)
                mutate(value)
                with self.subTest(inspector=inspector, mutation=index), self.assertRaises(ValueError):
                    judge(value)


if __name__ == "__main__":
    if sys.argv[1:] == ["--emit-offline-vector"]:
        original = json.dumps(bootstrap()).encode()
        sdk, inspector = journey(), journey(inspector=True)
        print(json.dumps({"meaning": "Synthetic complete planning oracle vectors; no actual client/model/candidate evidence",
            "bootstrapBase64": base64.b64encode(original).decode(),
            "sdk": {"judged": judge(sdk), "observations": sdk},
            "inspector": {"judged": judge(inspector), "observations": inspector}}, allow_nan=False))
    else:
        unittest.main()
