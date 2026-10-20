"""Synthetic semantic controls only; actual client/candidate proof is separate."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest

from mcp_grounding_status_state import admin_release_projection, graph_resource
from mcp_grounding_status_validation import (clarification_output, declared_status,
                                             grounding_output, process_catalog_resource,
                                             scenario, source_context)
from mcp_render_validation_controls import FIXTURE, bootstrap


AREA = {"serviceId": "service-42", "serviceName": "sdkreg-area", "layerId": 9,
        "storageLayerId": 7, "connectionId": "connection-42", "locator": "honua_data.sdkreg_area"}
METADATA = AREA | {"revision": 12, "resourceId": "resource-42", "resourceName": "area",
                   "resourceType": "feature-dataset"}
CATALOG = {"status": "ok", "catalogVersion": "honua.process_catalog.v1", "processes": [{"processId": "geometry.buffer",
            "executionKind": "Job", "entryPoints": ["job"], "supportedExecutionModes": ["Async"]}]}


def candidate(identifier, kind, score):
    return {"id": identifier, "kind": kind, "score": score,
            "evidence": ["title:1"], "displayName": identifier, "confidenceBand": "Medium"}


def grounded(goal="Buffer sdkreg-area geometry"):
    datasets = [candidate("sdkreg-area", "Dataset", .9), candidate("7", "Dataset", .8)]
    processes = [candidate("geometry.buffer", "Process", .9)]
    return {"engine": "deterministic", "workflowFamily": {"value": "Analyze", "confidence": 1, "evidence": ["verb:buffer→Analyze"]},
            "draftIntent": {"intentId": "grounding-123", "goal": goal, "workflowFamily": "Analyze",
                "provenance": {"sources": [{"sourceId": d["id"], "description": d["displayName"]} for d in datasets],
                               "processDefinitions": ["geometry.buffer"], "clarificationsAnswered": []}},
            "candidates": {"datasets": datasets, "processes": processes}, "clarification": None}


def release_projection():
    posture = {"schemaVersion": "1.0", "status": "complete", "sources": [{
        "sourceId": "honua_platform_release_status", "backendKind": "configProjection",
        "backendId": "control-plane-options", "completeness": "complete"}]}
    return {"releaseVersion": "2026.1", "releaseDeclared": True, "isCoVersioned": True,
            "serving": [{"id": "api", "skewed": False}], "execution": [], "skewedIds": [],
            "evidencePosture": posture}


class GroundingStatusControls(unittest.TestCase):
    def test_scenario_uses_existing_owned_publication_and_exact_candidate(self):
        authored = scenario(FIXTURE, bootstrap())
        self.assertEqual(authored["authoredGoal"], "Buffer geometry from sdkreg-area")
        self.assertEqual(authored["clarificationGoal"], "sdkreg-area")
        self.assertEqual(authored["ownedArea"]["serviceId"], "allocated-service-id")
        changed = bootstrap()
        changed["candidateImage"] = "ghcr.io/honua-io/honua-server@sha256:" + "f" * 64
        with self.assertRaises(ValueError): scenario(FIXTURE, changed)

    def test_admin_preflight_retains_only_declared_projection_and_refuses_redirect_or_echo(self):
        class Response:
            status = 200
            body = json.dumps({"platformRelease": release_projection(), "secretDiagnostics": "unretained"}).encode()

            def getheader(self, _):
                return None

            def read(self, _):
                return self.body

        class Connection:
            def __init__(self, host, port, timeout):
                self.host, self.port, self.timeout = host, port, timeout

            def request(self, method, path, headers):
                self.method, self.path, self.headers = method, path, headers

            def getresponse(self):
                return Response()

            def close(self):
                pass

        salt = "a" * 32
        fingerprint = "scrypt:" + salt + ":" + hashlib.scrypt(b"dummy-secret",
            salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32).hex()
        self.assertEqual(admin_release_projection("dummy-secret", salt, connect=Connection),
                         {"platformRelease": release_projection(), "credentialFingerprint": fingerprint,
                          "responseSha256": "sha256:" + hashlib.sha256(Response.body).hexdigest()})
        Response.status = 302
        with self.assertRaises(ValueError): admin_release_projection("dummy-secret", salt, connect=Connection)
        Response.status = 200
        Response.body = b'{"platformRelease":{"credential":"\\u0064ummy-secret"}}'
        with self.assertRaises(ValueError): admin_release_projection("dummy-secret", salt, connect=Connection)

    def test_graph_readback_requires_one_current_resource_joined_to_publication(self):
        queries = []
        def execute(sql):
            queries.append(sql)
            return json.dumps([METADATA])
        self.assertEqual(graph_resource(AREA, execute=execute), METADATA)
        sql = queries[0]
        for required in ("metadata_v2_current", "metadata_v2_snapshots", "snap.revision=active.revision",
                         "pub->>'resourceId'=resource#>>'{metadata,id}'",
                         "binding->>'resourceId'=resource#>>'{metadata,id}'",
                         "pub->>'storageBindingId'=binding#>>'{metadata,id}'"):
            self.assertIn(required, sql)
        for bad in ([], [METADATA, METADATA], {"metadata": METADATA}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError): graph_resource(AREA, execute=lambda _: json.dumps(bad))

    def test_owned_graph_and_actual_job_catalog_are_required(self):
        context = source_context(AREA, METADATA, CATALOG)
        self.assertEqual(context["datasetIds"], {"7", "sdkreg-area"})
        for change in (lambda m, c: m.update(resourceType="table"),
                       lambda m, c: m.update(storageLayerId=8),
                       lambda m, c: c["processes"][0].update(entryPoints=["workflow"]),
                       lambda m, c: c["processes"].append(copy.deepcopy(c["processes"][0]))):
            with self.subTest(change=change):
                metadata, catalog = copy.deepcopy(METADATA), copy.deepcopy(CATALOG)
                change(metadata, catalog)
                with self.assertRaises(ValueError): source_context(AREA, metadata, catalog)

    def test_process_catalog_must_be_one_json_resource_with_exact_uri(self):
        result = {"contents": [{"uri": "honua://catalog/processes", "mimeType": "application/json",
                                "text": json.dumps(CATALOG)}]}
        self.assertEqual(process_catalog_resource(result), CATALOG)
        for change in (lambda r: r["contents"][0].update(uri="honua://catalog/other"),
                       lambda r: r["contents"][0].update(mimeType="text/plain"),
                       lambda r: r["contents"][0].update(text="{\"processes\":[],\"processes\":[]}"),
                       lambda r: r["contents"].append(copy.deepcopy(r["contents"][0]))):
            invalid = copy.deepcopy(result)
            change(invalid)
            with self.assertRaises(ValueError): process_catalog_resource(invalid)

    def test_shortlist_and_provenance_must_name_owned_graph_and_process(self):
        context = source_context(AREA, METADATA, CATALOG)
        good = grounded()
        self.assertEqual(grounding_output(good, context, good["draftIntent"]["goal"]), "grounding-123")
        for change in (lambda o: o["candidates"]["datasets"].clear(),
                       lambda o: o["candidates"]["processes"][0].update(id="unrelated"),
                       lambda o: o["draftIntent"]["provenance"].update(processDefinitions=[]),
                       lambda o: o["draftIntent"]["provenance"]["sources"].append({"sourceId": "unranked"}),
                       lambda o: o["candidates"]["datasets"][0].update(evidence=[""]),
                       lambda o: o["candidates"]["datasets"][1].update(score=.95)):
            value = copy.deepcopy(good); change(value)
            with self.assertRaises(ValueError): grounding_output(value, context, good["draftIntent"]["goal"])

    def test_material_answer_preserves_intent_and_records_answered_question(self):
        before = grounded("sdkreg-area")
        before["clarification"] = {"intentId": "grounding-123", "questions": [{"questionId": "workflow_family",
            "options": [{"id": "Analyze"}, {"id": "PublishData"}]}]}
        after = copy.deepcopy(before)
        after["workflowFamily"] = {"value": "PublishData", "confidence": 1, "evidence": ["clarification"]}
        after["draftIntent"]["workflowFamily"] = "PublishData"
        after["draftIntent"]["provenance"]["clarificationsAnswered"] = ["workflow_family"]
        after["clarification"] = {"intentId": "grounding-123", "questions": [{"questionId": "publish.source"}]}
        context = source_context(AREA, METADATA, CATALOG)
        self.assertEqual(clarification_output(before, after, "sdkreg-area", context), "grounding-123")
        native = copy.deepcopy(before)
        native["clarification"] = None
        native["elicitation"] = {"message": "Additional detail is needed for intent 'grounding-123'.",
                                  "requestedSchema": {"type": "object", "required": ["workflow_family"],
                                      "properties": {"workflow_family": {"type": "string", "enum": ["Analyze", "PublishData"]}}}}
        self.assertEqual(clarification_output(native, after, "sdkreg-area", context), "grounding-123")
        native["elicitation"]["requestedSchema"]["properties"]["workflow_family"]["enum"] = ["Analyze"]
        with self.assertRaises(ValueError): clarification_output(native, after, "sdkreg-area", context)
        for change in (lambda a: a["draftIntent"].update(intentId="other"),
                       lambda a: a["workflowFamily"].update(confidence=.2),
                       lambda a: a["draftIntent"]["provenance"].update(clarificationsAnswered=[]),
                       lambda a: a["draftIntent"]["provenance"].update(sources=[]),
                       lambda a: a.update(clarification=None),
                       lambda a: a.update(clarification=before["clarification"])):
            value = copy.deepcopy(after); change(value)
            with self.assertRaises(ValueError): clarification_output(before, value, "sdkreg-area", context)

    def test_status_is_only_same_runtime_declared_configuration(self):
        projection = release_projection()
        self.assertTrue(declared_status(projection, {"platformRelease": copy.deepcopy(projection)})["releaseDeclared"])
        for change in (lambda p: p.update(releaseDeclared=False),
                       lambda p: p["serving"][0].update(skewed=True),
                       lambda p: p["evidencePosture"]["sources"][0].update(backendKind="liveDeployment")):
            value = copy.deepcopy(projection); change(value)
            with self.assertRaises(ValueError): declared_status(value, {"platformRelease": projection})

    def test_reviewed_configuration_skew_is_explicitly_declared_not_deployment_health(self):
        serving = "ghcr.io/honua-io/honua-server@sha256:" + "a" * 64
        alternate = "ghcr.io/honua-io/honua-server@sha256:" + "b" * 64
        declaration = {"schema": "honua.mcp-ops-config-declaration/v1",
                       "environment": {"ControlPlane__PlatformRelease__Version": "2026.1",
                                       "ControlPlane__PlatformRelease__ServingArtifactReference": serving,
                                       "ControlPlane__DeployTargets__0__TargetId": "cert-ops-skew",
                                       "ControlPlane__DeployTargets__0__ArtifactReference": alternate},
                       "artifactBindings": {"declaredServing": serving, "explicitTestTarget": alternate},
                       "expectedSkewedIds": ["cert-ops-skew"]}
        projection = release_projection()
        projection.update(isCoVersioned=False, skewedIds=["cert-ops-skew"],
                          serving=[{"id": "cert-ops-skew", "runtimeProfile": None,
                                    "effectiveArtifactReference": alternate,
                                    "projectedFromRelease": False, "skewed": True}])
        self.assertEqual(declared_status(projection, {"platformRelease": projection}, declaration)["skewedIds"],
                         ["cert-ops-skew"])
        for change in (lambda p: p.update(skewedIds=[]),
                       lambda p: p["serving"][0].update(effectiveArtifactReference=serving),
                       lambda p: p["serving"][0].update(projectedFromRelease=True),
                       lambda p: p["serving"][0].update(runtimeProfile="unreviewed"),
                       lambda p: p["serving"][0].update(targetKind="unprojected")):
            changed = copy.deepcopy(projection); change(changed)
            with self.assertRaises(ValueError): declared_status(changed, {"platformRelease": changed}, declaration)


if __name__ == "__main__":
    unittest.main()
