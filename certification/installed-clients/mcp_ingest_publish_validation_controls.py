"""Synthetic producer controls only; these never qualify an actual client/candidate."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import unittest
import mcp_ingest_publish_validation as validation
import mcp_ingest_publish_state as state
from mcp_render_validation_controls import FIXTURE, bootstrap, runtime, SOURCE, IMAGE
from mcp_map_package_validation_controls import control_wire


def envelope(value, error=False):
    return {"isError": error, "structuredContent": value, "content": [{"type": "text", "text": json.dumps(value)}]}


def rebind(value):
    for position, entry in enumerate(value["cases"] + value["reads"] + value["queries"]):
        entry["wire"], entry["client"], entry["result"] = control_wire(entry["method"], entry["params"], entry["result"], position, entry["authenticated"])
    return value


def controls():
    authored = validation.scenario(FIXTURE, bootstrap(), "abcdef123456", "Official MCP TypeScript SDK")
    ingested = {"success": True, "datasetName": authored["datasetName"], "rowCount": 6, "connectionId": "allocated-connection",
        "schema": "honua_data", "table": "imported_" + authored["datasetName"], "srid": 4326, "geometryColumn": "geometry",
        "primaryKey": "id", "warnings": [], "rowErrors": [], "errorCode": None, "errorMessage": None}
    args = validation.publish_arguments(ingested, authored)
    published = {"status": "Completed", "requiresApproval": False, "operationId": "service.publish", "serviceName": authored["serviceName"],
        "serviceUri": "honua://published-services/" + authored["serviceName"], "layerId": "741", "metadataRevision": 123,
        "operationInstanceId": "controlled-operation", "handleId": "controlled-handle"}
    rows = [{"id": index + 11, **row, "srid": 4326} for index, row in enumerate(authored["expectedRows"])]
    persisted = {"schema": "honua.mcp-ingest-persisted/v1", "tuple": {key: ingested[key] for key in ("connectionId", "schema", "table", "geometryColumn", "primaryKey")},
        "rows": rows, "publication": {"serviceId": "metadata-service-7", "serviceName": published["serviceName"], "layerId": 0,
            "storageLayerId": 741, "locator": "honua_data." + ingested["table"], "connectionId": ingested["connectionId"]}, "revision": 123}
    invalid = envelope({"status": "error", "code": "invalid_argument", "message": "Controlled invalid ingest"}, True)
    notfound = envelope({"status": "error", "code": "not_found", "message": "Controlled missing table"}, True)
    denied = envelope({"status": "error", "code": "unauthenticated", "message": "Controlled unauthenticated",
        "requiresReauthentication": True, "error": {"kind": "AuthorizationDenied", "message": "Controlled unauthenticated"}}, True)
    cases = []
    for identifier, name, arguments, authenticated, result in [
        ("ingest", "honua_ingest_dataset", authored["ingestArguments"], True, envelope(ingested)),
        ("invalid-ingest", "honua_ingest_dataset", authored["invalidIngestArguments"], True, invalid),
        ("publish", "honua_publish_service", args, True, envelope(published)),
        ("invalid-publish", "honua_publish_service", {**args, "table": "__missing_" + authored["datasetName"]}, True, notfound),
        ("anonymous-ingest", "honua_ingest_dataset", authored["ingestArguments"], False, denied),
        ("anonymous-publish", "honua_publish_service", args, False, denied)]:
        cases.append({"id": identifier, "method": "tools/call", "params": {"name": name, "arguments": arguments}, "authenticated": authenticated, "result": copy.deepcopy(result)})
    uri = published["serviceUri"]
    view = {"serviceId": published["serviceName"], "resourceUri": uri, "metadataServiceId": "metadata-service-7", "sourceId": "metadata-service-7",
        "sourceKind": "MetadataV2Graph", "targetKind": "FeatureService", "layerIds": [0], "servingLayerIds": [0], "featureServerEnabled": True,
        "serving": True, "status": "Active"}
    reads = []
    for identifier, selected, authenticated, result in [
        ("fresh-created", uri, True, {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(view)}]}),
        ("restarted-created", uri, True, {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(view)}]}),
        ("unknown", uri + "-uncreated", True, {"error": {"code": -32002, "message": "Controlled unknown service", "data": {"code": "not_found"}}}),
        ("anonymous", uri, False, {"error": {"code": -32000, "message": "Controlled unauthenticated", "data": {"code": "unauthenticated", "requiresReauthentication": True}}})]:
        reads.append({"id": identifier, "method": "resources/read", "params": {"uri": selected}, "authenticated": authenticated, "result": result})
    queried = envelope({"layerId": 0, "returnedCount": 6, "exceededTransferLimit": False, "geojson": {"type": "FeatureCollection",
        "features": [{"type": "Feature", "id": row["id"], "properties": row["properties"], "geometry": row["geometry"]} for row in rows]}})
    queries = [{"id": identifier, "method": "tools/call", "params": {"name": "honua_query_features", "arguments": validation.query_arguments(published, view)},
        "authenticated": True, "result": copy.deepcopy(queried)} for identifier in ("before", "after")]
    restarted = runtime(); restarted["startedAt"] = "2026-10-19T00:05:00Z"
    return rebind({"schema": validation.SCHEMA, "nonce": authored["nonce"], "canonicalClient": authored["canonicalClient"], "clientVersion": "1.32.1",
        "candidateSourceSha": SOURCE, "candidateImage": IMAGE, "cases": cases, "reads": reads, "queries": queries,
        "snapshots": {"before": copy.deepcopy(persisted), "after": copy.deepcopy(persisted)}, "runtimeBefore": runtime(),
        "runtimeRestarted": restarted, "runtimeAfter": copy.deepcopy(restarted)})


def replace_output(entry, transform):
    value = entry["result"]["structuredContent"]
    transform(value)
    entry["result"]["content"][0]["text"] = json.dumps(value)


class IngestPublishControls(unittest.TestCase):
    def test_complete_authored_persisted_restart_journey(self):
        proof = validation.judge(FIXTURE, bootstrap(), controls())
        self.assertEqual(proof["operations"], validation.OPERATIONS)
        self.assertTrue(proof["restartReadback"])
        self.assertFalse(proof["queuedPublishExercised"])

    def test_authored_semantic_mutations_rejected_after_wire_and_launch_rebinding(self):
        cases = {
            "missing-connection-hint": lambda x: replace_output(x["cases"][0], lambda y: y.update(connectionId=None)),
            "partial-ingest": lambda x: replace_output(x["cases"][0], lambda y: y.update(rowCount=5)),
            "row-errors": lambda x: replace_output(x["cases"][0], lambda y: y.update(rowErrors=[{"row": 2, "code": "bad"}])),
            "queued-success": lambda x: replace_output(x["cases"][2], lambda y: y.update(status="Queued", jobId="job1")),
            "approval-success": lambda x: replace_output(x["cases"][2], lambda y: y.update(status="RequiresApproval", requiresApproval=True)),
            "lost-revision": lambda x: replace_output(x["cases"][2], lambda y: y.update(metadataRevision=None)),
            "wrong-service-uri": lambda x: replace_output(x["cases"][2], lambda y: y.update(serviceUri="honua://published-services/other")),
            "replacement-publish-tuple": lambda x: x["cases"][2]["params"]["arguments"].update(table="working_replacement"),
            "invalid-ingest-success": lambda x: x["cases"][1].update(result=copy.deepcopy(x["cases"][0]["result"])),
            "invalid-publish-success": lambda x: x["cases"][3].update(result=copy.deepcopy(x["cases"][2]["result"])),
            "anonymous-ingest-success": lambda x: x["cases"][4].update(result=copy.deepcopy(x["cases"][0]["result"])),
            "anonymous-publish-success": lambda x: x["cases"][5].update(result=copy.deepcopy(x["cases"][2]["result"])),
            "missing-read": lambda x: x["reads"].pop(1),
            "missing-query": lambda x: x["queries"].pop(),
            "query-truncated": lambda x: replace_output(x["queries"][1], lambda y: y.update(exceededTransferLimit=True)),
            "wrong-query-tuple": lambda x: x["queries"][0]["params"]["arguments"].update(layerId=29),
            "wrong-candidate": lambda x: x.update(candidateSourceSha="f" * 40),
            "no-actual-restart": lambda x: x.update(runtimeRestarted=runtime(), runtimeAfter=runtime()),
            "changed-container": lambda x: x["runtimeRestarted"].update(containerId="f" * 64),
            "wrong-runtime-image": lambda x: x["runtimeAfter"]["imageReadback"].update(sourceSha="f" * 40),
            "missing-snapshot": lambda x: x["snapshots"].pop("after"),
            "mutated-row": lambda x: x["snapshots"]["before"]["rows"][1]["properties"].update(rank=999),
            "mutated-geometry": lambda x: x["snapshots"]["after"]["rows"][0]["geometry"].update(coordinates=[37.7, -122.4]),
            "wrong-srid": lambda x: x["snapshots"]["after"]["rows"][0].update(srid=3857),
            "wrong-layer-address": lambda x: x["snapshots"]["before"]["publication"].update(layerId=29),
            "wrong-source-binding": lambda x: x["snapshots"]["before"]["publication"].update(locator="honua_data.other"),
            "post-restart-lost-data": lambda x: x["snapshots"]["after"]["rows"].pop(),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                value = controls(); mutate(value); rebind(value)
                with self.assertRaises(ValueError):
                    validation.judge(FIXTURE, bootstrap(), value)

    def test_observer_keeps_storage_and_public_ids_distinct_in_one_snapshot(self):
        value = controls()
        expected = value["snapshots"]["before"]
        calls = []
        def execute(query):
            calls.append(query)
            return json.dumps({"rows": expected["rows"], "mappings": [{"publication": expected["publication"], "revision": expected["revision"]}]})
        observed = state.snapshot(FIXTURE, bootstrap(), value, execute=execute)
        self.assertEqual(observed["publication"]["storageLayerId"], 741)
        self.assertEqual(observed["publication"]["layerId"], 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("active.revision", calls[0])
        self.assertIn("ST_AsGeoJSON", calls[0])
        self.assertEqual(observed, expected)

    def test_observer_refuses_ambiguous_mapping_and_unsafe_tuple_without_sql(self):
        value = controls()
        expected = value["snapshots"]["before"]
        def ambiguous(query):
            mapping = {"publication": expected["publication"], "revision": expected["revision"]}
            return json.dumps({"rows": expected["rows"], "mappings": [mapping, mapping]})
        with self.assertRaises(ValueError):
            state.snapshot(FIXTURE, bootstrap(), value, execute=ambiguous)
        value = controls()
        replace_output(value["cases"][0], lambda result: result.update(table='unsafe";DROP TABLE x;--'))
        called = []
        with self.assertRaises(ValueError):
            state.snapshot(FIXTURE, bootstrap(), value, execute=lambda query: called.append(query))
        self.assertEqual(called, [])

    def test_actual_wire_auth_session_launch_and_response_controls(self):
        mutations = {
            "missing-observation": lambda x: x["reads"][0].update(wire=None),
            "unrelated-uri": lambda x: x["reads"][0]["wire"]["expectedParams"].update(uri="honua://other"),
            "wrong-response-id": lambda x: x["reads"][0]["wire"]["invocation"]["response"].update(bodySha256="sha256:" + "f" * 64),
            "borrowed-session": lambda x: x["reads"][1].update(wire=copy.deepcopy(x["reads"][0]["wire"])),
            "anonymous-forwarded-auth": lambda x: x["cases"][4]["wire"]["invocation"]["request"].update(credentialPresent=True),
            "unbound-client": lambda x: x["cases"][0]["client"].pop("launch"),
            "invented-client-version": lambda x: x.update(clientVersion="1.30.0"),
            "wrong-package": lambda x: x["queries"][0]["client"]["launch"]["package"].update(version="9.9.9"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                value = controls(); mutate(value)
                with self.assertRaises((ValueError, TypeError)):
                    validation.judge(FIXTURE, bootstrap(), value)


if __name__ == "__main__":
    unittest.main()
