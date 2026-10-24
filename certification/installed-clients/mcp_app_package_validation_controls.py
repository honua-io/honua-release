"""Independent offline app vectors. Never canonical client/candidate evidence."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import unittest

import mcp_app_package_validation as validation
from mcp_execute_plan_validation_controls import controls as execution_controls
from mcp_map_package_validation_controls import control_wire, observations as map_controls, responses as map_responses
from mcp_render_validation_controls import FIXTURE, bootstrap


def envelope(value, error=False):
    return {"isError": error, "structuredContent": value, "content": [{"type": "text", "text": json.dumps(value)}]}


def control_prerequisites():
    value = {"execution": execution_controls(), "map": copy.deepcopy(map_controls()["cases"][0])}
    item = value["map"]
    item["wire"], item["client"], item["result"] = control_wire("tools/call",
        {"name": "honua_create_map_package", "arguments": item["arguments"]}, item["result"], 200, True)
    return value


def created_response(artifact):
    # Independent expected server projection: exact supported map/app fields,
    # no synthetic bundle, generated files, deployment or runtime execution.
    package = {"appPackageId": "app_controlled", "format": "honua_app_package.v1", "status": "Draft",
        "targetSdk": "honua-sdk-js", "templateId": "sdkreg-authored-app-template", "mapPackageId": "map_controlled",
        "runtimeConfig": {"title": "Authored map and buffer", "showLegend": True, "initialPanel": "map", "bufferArtifactId": artifact},
        "boundArtifacts": [artifact], "sharePolicy": {"visibility": "private", "embed": False, "reviewed": False},
        "generatedFiles": [], "assetManifest": [], "createdAt": "2026-10-19T00:02:00Z"}
    return envelope({"status": "created", "packageId": "app_controlled", "resourceUri": "honua://app-packages/app_controlled",
        "package": package, "warnings": []})


def review_response(tool, blocked, artifact):
    findings = [] if not blocked else [{"code": "approval_required", "severity": "blocker", "category": "approval",
        "disposition": "requires_approval", "appliesTo": "publish", "message": "Explicit approval is required.",
        "requiredAction": {"actionKind": "obtain_approval", "targetPath": "/sharePolicy", "title": "Obtain approval."},
        "affectedArtifact": {"kind": "approval", "path": "/sharePolicy"},
        "evidence": [{"kind": "approval", "expected": "approved", "actual": "not_approved", "path": "/sharePolicy",
            "policyRef": "sdkreg-explicit-approval"}]}]
    value = {"contractVersion": "honua.package_review.v1", "reviewId": "prv_" + "a" * 32, "packageFamily": "app_package",
        "packageId": "app_controlled", "status": "blocked" if blocked else "ready", "canExecute": True,
        "canPublish": not blocked, "requiresApproval": blocked, "checkedAt": "2026-10-19T00:03:00Z", "findings": findings,
        "links": {}, "resourceRefs": {"app": "honua://app-packages/app_controlled"}}
    if tool == "honua_preview_package":
        value["previewPlan"] = {"planId": "preview:app_package:app_controlled", "mayMutatePublishedState": False,
            "operations": [{"operationId": "review", "operationKind": "app_package", "inputRefs": ["map_controlled", artifact],
                "outputArtifactKinds": ["app_package"], "dependsOn": [], "mayMutatePublishedState": False, "requiredCapabilities": []}]}
    return envelope(value)


def rebind(value):
    position = 201
    for family in ("cases", "reads", "reviews"):
        for item in value[family]:
            if family == "reads": method, params = "resources/read", {"uri": item["uri"]}
            else: method, params = "tools/call", {"name": item.get("tool", "honua_create_app_package"), "arguments": item["arguments"]}
            item["wire"], item["client"], item["result"] = control_wire(method, params, item["result"], position, item["authenticated"])
            position += 1
    return value


def controls():
    prior = control_prerequisites(); value = validation.scenario(FIXTURE, bootstrap(), prior)
    artifact = value["cases"][0]["arguments"]["boundArtifactIds"][0]
    created = created_response(artifact)
    old = map_responses()
    for item, result in zip(value["cases"], [created, old[1], old[2], old[3]], strict=True): item["result"] = copy.deepcopy(result)
    uri = "honua://app-packages/app_controlled"
    view = {"packageKind": "app_package", "packageId": "app_controlled", "resourceUri": uri, "packageStatus": "draft",
        "deploymentCount": 0, "deploymentResourceUris": [], "provenance": {}}
    reads = [{"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(view)}]},
        {"error": {"code": -32002, "message": "Unknown package", "data": {"code": "not_found"}}},
        {"error": {"code": -32000, "message": "Unauthenticated", "data": {"code": "unauthenticated", "requiresReauthentication": True}}}]
    value["reads"] = [{"id": name, "uri": selected, "authenticated": auth, "result": result} for (name, selected, auth), result in zip(
        [("fresh-created", uri, True), ("unknown-package", uri + "-uncreated", True), ("anonymous-created", uri, False)], reads, strict=True)]
    value["reviews"] = [{**case, "result": review_response(case["tool"], case["id"] == "approval-required", artifact)
        if case["authenticated"] else copy.deepcopy(old[3])} for case in validation.review_cases(created["structuredContent"])]
    value.update(prerequisites=prior, runtimeBefore=copy.deepcopy(prior["execution"]["runtimeAfter"]), runtimeAfter=copy.deepcopy(prior["execution"]["runtimeAfter"]))
    return rebind(value)


class AppPackageControls(unittest.TestCase):
    def assert_refused(self, value):
        with self.assertRaises((ValueError, KeyError, TypeError)):
            validation.judge(FIXTURE, bootstrap(), value)

    def test_complete_real_map_artifact_composition_and_all_four_operations(self):
        result = validation.judge(FIXTURE, bootstrap(), controls())
        self.assertEqual(result["operations"], validation.OPERATIONS)
        self.assertTrue(all(result[k] for k in ("positive", "negative", "mediaSchema", "freshReadback", "scratchOnly", "readOnlyPreview", "realMapAndArtifact")))

    def test_real_map_artifact_prerequisites_not_success_flags(self):
        for name in ("execution", "map", "artifact", "map-view", "client", "runtime"):
            with self.subTest(name=name):
                value = controls()
                if name in ("execution", "map"): value["prerequisites"][name] = {}
                elif name == "artifact": value["prerequisites"]["execution"]["snapshots"]["completed"]["results"] = []
                elif name == "map-view": value["prerequisites"]["map"]["result"]["structuredContent"]["package"]["initialView"]["bbox"] = [0, 0, 1, 1]
                elif name == "client": value["canonicalClient"] = "MCP Inspector"
                else: value["runtimeAfter"]["startedAt"] = "2026-10-19T01:00:00Z"
                self.assert_refused(value)

    def test_authored_content_and_closed_posture_cannot_drift_even_with_bound_raw_bytes(self):
        for field, changed in (("mapPackageId", "map_uncreated"), ("boundArtifacts", ["uncreated"]),
                ("runtimeConfig", {}), ("targetSdk", "other"), ("templateId", "ignored"), ("format", "other"),
                ("status", "Published"), ("sharePolicy", {"visibility": "public", "embed": True, "reviewed": True}),
                ("sharePolicy", {"visibility": "private", "embed": 0, "reviewed": False}),
                ("generatedFiles", ["manufactured.js"])):
            with self.subTest(field=field):
                value = controls(); output = value["cases"][0]["result"]["structuredContent"]; output["package"][field] = changed
                value["cases"][0]["result"] = envelope(output); self.assert_refused(rebind(value))

    def test_fresh_resource_is_exact_draft_metadata_not_package_or_deployment(self):
        for field, changed in (("packageKind", "map_package"), ("packageId", "app_other"), ("packageStatus", "published"),
                ("deploymentCount", 1), ("deploymentResourceUris", ["honua://deployments/fake"])):
            value = controls(); item = value["reads"][0]["result"]["contents"][0]; body = json.loads(item["text"]); body[field] = changed; item["text"] = json.dumps(body)
            self.assert_refused(rebind(value))

    def test_review_cannot_ignore_approval_or_mutate_state(self):
        for index, field, changed in ((0, "packageId", "app_other"), (1, "canPublish", True), (1, "requiresApproval", False),
                (1, "findings", []), (3, "previewPlan", None), (3, "status", "blocked")):
            value = controls(); output = value["reviews"][index]["result"]["structuredContent"]; output[field] = changed
            value["reviews"][index]["result"] = envelope(output); self.assert_refused(rebind(value))
        value = controls(); output = value["reviews"][3]["result"]["structuredContent"]; output["previewPlan"]["mayMutatePublishedState"] = True
        value["reviews"][3]["result"] = envelope(output); self.assert_refused(rebind(value))

    def test_all_primary_calls_and_fresh_auth_wire_are_required(self):
        for family in ("cases", "reads", "reviews"):
            value = controls(); value[family].pop(); self.assert_refused(value)
            value = controls(); value[family][0]["wire"]["invocation"]["request"]["credentialPresent"] = False; self.assert_refused(value)
            value = controls(); value[family][0]["wire"]["initialize"]["request"]["credentialPresent"] = False; self.assert_refused(value)
        value = controls(); value["reads"][0]["wire"]["initialize"]["response"]["sessionSha256"] = value["cases"][0]["wire"]["initialize"]["response"]["sessionSha256"]; self.assert_refused(value)

    def test_typed_negatives_never_accept_success_or_wrong_error(self):
        for family, index in (("cases", 1), ("cases", 2), ("cases", 3), ("reviews", 2), ("reviews", 5)):
            value = controls(); value[family][index]["result"]["isError"] = False; self.assert_refused(rebind(value))
        for index in (1, 2):
            value = controls(); value["reads"][index]["result"]["error"]["data"]["code"] = "internal"; self.assert_refused(rebind(value))


if __name__ == "__main__":
    unittest.main()
