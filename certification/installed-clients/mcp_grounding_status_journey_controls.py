"""Complete synthetic SDK/Inspector wire vectors; no candidate execution credit."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest

import yaml

import mcp_grounding_status_validation as oracle
from mcp_grounding_status_validation_controls import CATALOG, candidate, release_projection
from mcp_map_package_validation_controls import control_wire, bind_launch
from mcp_ops_events_config import declaration as declare_configuration
from mcp_ops_events_config_controls import manifest as configuration_manifest
from mcp_ops_events_journey_controls import managed_receipt
from mcp_render_validation_controls import FIXTURE, bootstrap


def manifest_bytes():
    source = bootstrap()["candidateSourceSha"]
    value = configuration_manifest()
    value["candidate"]["ref"] = source
    value["components"]["honua-server"].update(sha=source, digest="sha256:" + "c" * 64)
    return yaml.safe_dump(value).encode()


def result(value):
    return {"isError": False, "structuredContent": value,
            "content": [{"type": "text", "text": json.dumps(value)}]}


def refused(code="invalid_argument"):
    value = {"status": "error", "code": code, "message": "Controlled refusal"}
    if code == "unauthenticated":
        value.update(requiresReauthentication=True,
                     error={"kind": "AuthorizationDenied", "message": value["message"]})
    return {"isError": True, "structuredContent": value,
            "content": [{"type": "text", "text": json.dumps(value)}]}


def grounded(goal, area):
    dataset = candidate(area["serviceName"], "Dataset", .9)
    process = candidate("geometry.buffer", "Process", .9)
    return {"engine": "deterministic",
            "workflowFamily": {"value": "Analyze", "confidence": 1, "evidence": ["verb:buffer→Analyze"]},
            "draftIntent": {"intentId": "grounding-123", "goal": goal, "workflowFamily": "Analyze",
                "provenance": {"sources": [{"sourceId": dataset["id"], "description": dataset["displayName"]}],
                               "processDefinitions": ["geometry.buffer"], "clarificationsAnswered": []}},
            "candidates": {"datasets": [dataset], "processes": [process]}, "clarification": None}


def vector(*, inspector=False):
    owned = bootstrap()
    authored = oracle.scenario(FIXTURE, owned)
    raw_manifest = manifest_bytes()
    declaration = declare_configuration(raw_manifest, release_source_sha="a" * 40,
        candidate_source_sha=owned["candidateSourceSha"], candidate_image=owned["candidateImage"])
    area = owned["ownedArea"]
    metadata = dict(area, revision=12, resourceId="resource-42", resourceName="sdkreg-area",
                    resourceType="feature-dataset")
    projection = release_projection()
    projection.update(releaseVersion=declaration["environment"]["ControlPlane__PlatformRelease__Version"],
        isCoVersioned=False, skewedIds=["cert-ops-skew"],
        serving=[{"id": "cert-ops-skew", "runtimeProfile": None,
                  "effectiveArtifactReference": declaration["artifactBindings"]["explicitTestTarget"],
                  "projectedFromRelease": False, "skewed": True}])
    observation = {**authored, "canonicalClient": "MCP Inspector" if inspector else "Official MCP TypeScript SDK",
        "clientVersion": "2.10.1" if inspector else "1.32.1",
        "runtimeBefore": owned["runtimeBinding"], "runtimeAfter": owned["runtimeBinding"],
        "graphObservation": {"schema": "honua.mcp-grounding-graph/v1", "metadata": metadata,
            **{key: authored[key] for key in ("candidateSourceSha", "candidateImage", "fixtureSha256", "bootstrapSha256")}},
        "preflightBefore": None, "preflightAfter": None, "declaration": declaration, "calls": [],
        "identityFixture": managed_receipt("full", owned["runtimeBinding"]),
        "limitedIdentityFixture": managed_receipt("limited", owned["runtimeBinding"]),
        "credentialSalt": "a" * 32}
    fingerprint = observation["identityFixture"]["identity"]["credentialFingerprint"]
    limited_fingerprint = observation["limitedIdentityFixture"]["identity"]["credentialFingerprint"]
    for name in ("preflightBefore", "preflightAfter"):
        observation[name] = {"schema": "honua.mcp-grounding-admin-preflight/v1",
            "projection": {"platformRelease": copy.deepcopy(projection)},
            "credentialFingerprint": fingerprint,
            "responseSha256": "sha256:" + hashlib.sha256(json.dumps({"platformRelease": projection}).encode()).hexdigest(),
            **{key: authored[key] for key in ("candidateSourceSha", "candidateImage", "fixtureSha256", "bootstrapSha256")}}
    before = grounded(authored["clarificationGoal"], area)
    before["clarification"] = {"intentId": "grounding-123", "questions": [{"questionId": "workflow_family",
        "options": [{"id": "Analyze"}, {"id": "PublishData"}]}]}
    after = copy.deepcopy(before)
    after["workflowFamily"] = {"value": "PublishData", "confidence": 1, "evidence": ["clarification"]}
    after["draftIntent"]["workflowFamily"] = "PublishData"
    after["draftIntent"]["provenance"]["clarificationsAnswered"] = ["workflow_family"]
    after["clarification"] = {"intentId": "grounding-123", "questions": [{"questionId": "publish.source"}]}
    catalog = {"contents": [{"uri": oracle.CATALOG_URI, "mimeType": "application/json", "text": json.dumps(CATALOG)}]}
    outputs = [catalog, result(grounded(authored["authoredGoal"], area)), result(before), result(after),
               result(projection)] + [refused()] * 4 + [refused("permission_denied")]
    outputs += [refused("unauthenticated")] * 3
    for index, (expected, output) in enumerate(zip(oracle.calls(authored, intent_id="grounding-123"), outputs, strict=True)):
        record = {**expected, "result": output}
        record["wire"], record["client"], record["result"] = control_wire(
            expected["method"], expected["params"], output, index, expected["authenticated"])
        call_fingerprint = limited_fingerprint if expected["expectation"] == "permission_denied" else fingerprint
        record["wire"]["expectedCredentialFingerprint"] = call_fingerprint
        for field in ("initialize", "invocation"):
            record["wire"][field]["request"]["credentialFingerprint"] = call_fingerprint if expected["authenticated"] else None
        if inspector:
            frame = record["result"]
            error = frame.get("error")
            anonymous = bool(error and error.get("data", {}).get("code") == "unauthenticated")
            client = record["client"]
            client.update(kind="inspector-cli", canonicalClient="MCP Inspector", version="2.10.1",
                exitCode=3 if anonymous else 1 if error else 5 if frame.get("isError") else 0,
                stdout="" if error else json.dumps({"result": frame}),
                stderr=json.dumps({"error": {"code": "auth_required" if anonymous else "error",
                    "message": error["message"]}}) if error else "")
            bind_launch(client, record["wire"], "inspector")
        observation["calls"].append(record)
    return owned, raw_manifest, declaration, observation


class FullJourneyControls(unittest.TestCase):
    def test_complete_sdk_and_inspector_vectors_rejudge_all_three_operations(self):
        for inspector in (False, True):
            with self.subTest(inspector=inspector):
                owned, manifest, declaration, observed = vector(inspector=inspector)
                verdict = oracle.judge(FIXTURE, owned, manifest, declaration, observed)
                self.assertTrue(all(verdict[key] for key in ("positive", "clarificationContinuity",
                    "declaredStatusOnly", "negative", "freshSession", "sourceReadback")))

    def test_changed_real_intent_status_or_source_refuses_even_with_rebound_wire(self):
        for path in ("intent", "status", "graph", "session", "mixed-caller", "wrong-preflight-caller",
                     "limited-caller", "limited-receipt", "limited-result"):
            with self.subTest(path=path):
                owned, manifest, declaration, observed = vector()
                if path == "intent":
                    observed["calls"][3]["params"]["arguments"]["intentId"] = "other"
                elif path == "status":
                    observed["calls"][4]["result"]["structuredContent"]["skewedIds"] = []
                elif path == "graph":
                    observed["graphObservation"]["metadata"]["storageLayerId"] += 1
                elif path == "mixed-caller":
                    observed["calls"][3]["wire"]["expectedCredentialFingerprint"] = (
                        "scrypt:" + "a" * 32 + ":" + "f" * 64)
                elif path == "wrong-preflight-caller":
                    observed["preflightAfter"]["credentialFingerprint"] = (
                        "scrypt:" + "a" * 32 + ":" + "f" * 64)
                elif path == "limited-caller":
                    observed["calls"][9]["wire"]["expectedCredentialFingerprint"] = (
                        observed["identityFixture"]["identity"]["credentialFingerprint"])
                elif path == "limited-receipt":
                    observed["limitedIdentityFixture"] = copy.deepcopy(observed["identityFixture"])
                elif path == "limited-result":
                    observed["calls"][9]["result"] = result(release_projection())
                else:
                    observed["calls"][3]["wire"]["initialize"]["response"]["sessionSha256"] = observed["calls"][0]["wire"]["initialize"]["response"]["sessionSha256"]
                with self.assertRaises(ValueError):
                    oracle.judge(FIXTURE, owned, manifest, declaration, observed)


if __name__ == "__main__":
    unittest.main()
