"""Synthetic producer controls only; these never qualify an actual client/candidate."""
from __future__ import annotations
import copy
import json
from pathlib import Path
import unittest
import mcp_ingest_publish_validation as validation
import mcp_ingest_publish_state as state
from mcp_render_validation_controls import FIXTURE, bootstrap as render_bootstrap, runtime, SOURCE, IMAGE
from mcp_map_package_validation_controls import control_wire, encoded_body


def bootstrap():
    return {**render_bootstrap(), "database": copy.deepcopy(validation.OWNED_DATABASE)}


def connection_control(selector="22222222-2222-4222-8222-222222222222"):
    identity = {"clusterId": "7352416435566778899", "database": "honua_test", "user": "honua"}
    return {"selector": selector, "registry": [{"connectionId": "22222222-2222-4222-8222-222222222222",
        "name": "sdkreg-harness", "host": "localhost", "port": 5432, "databaseName": "honua_test", "username": "honua",
        "provider": "postgis", "sslRequired": False, "sslMode": "disable", "isActive": True}],
        "catalog": identity, "target": copy.deepcopy(identity), "verifiedAddress": "127.0.0.1"}


def envelope(value, error=False):
    return {"isError": error, "structuredContent": value, "content": [{"type": "text", "text": json.dumps(value)}]}


def rebind(value):
    for position, entry in enumerate(value["cases"] + value["reads"] + value["queries"]):
        entry["wire"], entry["client"], entry["result"] = control_wire(entry["method"], entry["params"], entry["result"], position, entry["authenticated"])
    return value


def controls():
    authored = validation.scenario(FIXTURE, bootstrap(), "abcdef123456", "Official MCP TypeScript SDK")
    ingested = {"success": True, "datasetName": authored["datasetName"], "rowCount": 6, "connectionId": "22222222-2222-4222-8222-222222222222",
        "schema": "honua_data", "table": "imported_" + authored["datasetName"], "srid": 4326, "geometryColumn": "geometry",
        "primaryKey": "id", "warnings": [], "rowErrors": [], "errorCode": None, "errorMessage": None}
    args = validation.publish_arguments(ingested, authored)
    published = {"status": "Completed", "requiresApproval": False, "operationId": "service.publish", "serviceName": authored["serviceName"],
        "serviceUri": "honua://published-services/" + authored["serviceName"], "layerId": "741", "metadataRevision": 123,
        "operationInstanceId": "controlled-operation", "handleId": "controlled-operation"}
    rows = [{"id": index + 11, **row, "srid": 4326} for index, row in enumerate(authored["expectedRows"])]
    persisted = {"schema": "honua.mcp-ingest-persisted/v2", "tuple": {key: ingested[key] for key in ("connectionId", "schema", "table", "geometryColumn", "primaryKey")},
        "rows": rows, "publication": {"serviceId": "metadata-service-7", "serviceName": published["serviceName"], "layerId": 0,
            "storageLayerId": 741, "locator": "honua_data." + ingested["table"], "connectionId": ingested["connectionId"]}, "revision": 123,
        "connection": connection_control()}
    invalid = envelope({"status": "error", "code": "invalid_argument", "message": "Controlled invalid ingest"}, True)
    notfound = envelope({"status": "Failed", "requiresApproval": False, "operationId": "service.publish",
        "operationInstanceId": "controlled-missing-operation", "handleId": "controlled-missing-operation", "correlationId": "controlled-missing-correlation",
        "message": "Error: Table 'honua_data.__missing_" + authored["datasetName"] + "' was not found.",
        "resourceIds": {}, "details": {}})
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
    # The published import layer's fields as honua-server returns them (2026.1 JSON properties contract).
    fields = lambda row: {"id": row["id"], "created_at": "2026-10-26T10:09:56.622386+00:00",
                          "properties": copy.deepcopy(row["properties"]), "objectid": row["id"]}
    queried = envelope({"serviceId": "metadata-service-7", "features": [{"id": row["id"], "attributes": fields(row), "geometry": row["geometry"]} for row in rows], "layerId": 0, "returnedCount": 6, "exceededTransferLimit": False, "geojson": {"type": "FeatureCollection",
        "features": [{"type": "Feature", "id": row["id"], "properties": fields(row), "geometry": row["geometry"]} for row in rows]}})
    queried["content"][0]["text"] = "Returned six authored features; inspect structuredContent for geometry and attributes."
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
            "failed-publish-reused-id": lambda x: replace_output(x["cases"][3], lambda y: y.update(operationInstanceId="controlled-operation", handleId="controlled-operation")),
            "failed-publish-wrong-reason": lambda x: replace_output(x["cases"][3], lambda y: y.update(message="Unrelated authorization refusal")),
            "failed-publish-created-layer": lambda x: replace_output(x["cases"][3], lambda y: y.update(layerId="741")),
            "failed-publish-proposal": lambda x: replace_output(x["cases"][3], lambda y: y.update(proposalId="unexpected-proposal")),
            "failed-publish-job": lambda x: replace_output(x["cases"][3], lambda y: y.update(jobId="unexpected-job")),
            "failed-publish-revision": lambda x: replace_output(x["cases"][3], lambda y: y.update(metadataRevision=124)),
            "failed-publish-resources": lambda x: replace_output(x["cases"][3], lambda y: y.update(resourceIds={"serviceName":"unexpected"})),
            "replaced-ingest-table": lambda x: replace_output(x["cases"][0], lambda y: y.update(table="other_existing_table")),
            "anonymous-ingest-success": lambda x: x["cases"][4].update(result=copy.deepcopy(x["cases"][0]["result"])),
            "anonymous-publish-success": lambda x: x["cases"][5].update(result=copy.deepcopy(x["cases"][2]["result"])),
            "missing-read": lambda x: x["reads"].pop(1),
            "missing-query": lambda x: x["queries"].pop(),
            "query-truncated": lambda x: replace_output(x["queries"][1], lambda y: y.update(exceededTransferLimit=True)),
            "wrong-query-tuple": lambda x: x["queries"][0]["params"]["arguments"].update(layerId=29),
            "query-missing-structured": lambda x: x["queries"][0]["result"].pop("structuredContent"),
            "query-missing-projection": lambda x: replace_output(x["queries"][0], lambda y: y.pop("features")),
            "query-conflicting-projection": lambda x: replace_output(x["queries"][0], lambda y: y["features"][0]["attributes"]["properties"].update(rank=999)),
            "query-geojson-properties-changed": lambda x: replace_output(x["queries"][0], lambda y: y["geojson"]["features"][0]["properties"]["properties"].update(name="other")),
            "query-properties-stringified": lambda x: replace_output(x["queries"][0], lambda y: y["geojson"]["features"][0]["properties"].update(
                properties=json.dumps(y["geojson"]["features"][0]["properties"]["properties"]))),
            "query-properties-missing": lambda x: replace_output(x["queries"][0], lambda y: y["features"][0]["attributes"].pop("properties")),
            "query-wrong-service": lambda x: replace_output(x["queries"][0], lambda y: y.update(serviceId="other-service")),
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

    def test_named_selector_requires_independent_registry_and_real_database_binding(self):
        for stored in (None, "22222222-2222-4222-8222-222222222222"):
            value = controls()
            authored = validation.scenario(FIXTURE, bootstrap(), value["nonce"], value["canonicalClient"])
            ingested = value["cases"][0]["result"]["structuredContent"]
            published = value["cases"][2]["result"]["structuredContent"]
            ingested["connectionId"] = "sdkreg-harness"
            snapshot = value["snapshots"]["before"]
            snapshot["tuple"]["connectionId"] = ingested["connectionId"]
            snapshot["publication"]["connectionId"] = stored
            snapshot["connection"] = connection_control("sdkreg-harness")
            validation.snapshot_output(snapshot, ingested, published, authored)
            for mutation in ("missing", "unresolved-name", "wrong-guid", "wrong-cluster", "wrong-database", "wrong-user"):
                with self.subTest(stored=stored, mutation=mutation):
                    changed = copy.deepcopy(snapshot)
                    if mutation == "missing": changed.pop("connection")
                    elif mutation == "unresolved-name": changed["connection"]["registry"][0]["name"] = "other-name"
                    elif mutation == "wrong-guid": changed["publication"]["connectionId"] = "f" * 36
                    else:
                        key = {"wrong-cluster": "clusterId", "wrong-database": "database", "wrong-user": "user"}[mutation]
                        changed["connection"]["target"][key] = "unrelated"
                    with self.assertRaises(ValueError):
                        validation.snapshot_output(changed, ingested, published, authored)

    def test_guid_binding_cannot_be_replaced_with_null_or_known_bootstrap_guid(self):
        value = controls()
        authored = validation.scenario(FIXTURE, bootstrap(), value["nonce"], value["canonicalClient"])
        ingested = value["cases"][0]["result"]["structuredContent"]
        published = value["cases"][2]["result"]["structuredContent"]
        for replacement in (None, bootstrap()["connectionId"]):
            snapshot = copy.deepcopy(value["snapshots"]["before"])
            snapshot["publication"]["connectionId"] = replacement
            with self.assertRaises(ValueError):
                validation.snapshot_output(snapshot, ingested, published, authored)

    def test_complete_named_journey_keeps_canonical_or_proven_default_binding(self):
        for stored in (None, "22222222-2222-4222-8222-222222222222"):
            value = controls()
            replace_output(value["cases"][0], lambda result: result.update(connectionId="sdkreg-harness"))
            for index in (2, 3, 5): value["cases"][index]["params"]["arguments"]["connectionId"] = "sdkreg-harness"
            for snapshot in value["snapshots"].values():
                snapshot["tuple"]["connectionId"] = "sdkreg-harness"
                snapshot["publication"]["connectionId"] = stored
                snapshot["connection"] = connection_control("sdkreg-harness")
            self.assertTrue(validation.judge(FIXTURE, bootstrap(), rebind(value))["restartReadback"])
            value["snapshots"]["after"]["connection"]["registry"][0]["name"] = "changed"
            with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), value)

    def test_unowned_inactive_or_ambiguous_registry_never_reaches_credential_boundary(self):
        value = controls(); expected = value["snapshots"]["before"]
        for mutation in ("missing", "duplicate", "inactive", "host", "port", "databaseName", "username", "provider", "sslRequired", "sslMode"):
            with self.subTest(mutation=mutation):
                registry = copy.deepcopy(expected["connection"]["registry"])
                if mutation == "missing": registry = []
                elif mutation == "duplicate": registry *= 2
                elif mutation == "inactive": registry[0]["isActive"] = False
                else: registry[0][mutation] = 5433 if mutation == "port" else True if mutation == "sslRequired" else "unowned"
                def execute(query):
                    return json.dumps({"rows": expected["rows"], "mappings": [{"publication": expected["publication"], "revision": expected["revision"]}],
                        "registry": registry, "catalog": expected["connection"]["catalog"]})
                with self.assertRaises(ValueError):
                    state.snapshot(FIXTURE, bootstrap(), value, execute=execute,
                        inspect_target=lambda row: self.fail("unsafe or ambiguous destination must not receive fixture credentials"))

    def test_target_dns_and_process_environment_are_owned_before_credential_use(self):
        row = connection_control()["registry"][0]
        calls = []
        def execute(query, password, **target):
            calls.append(target)
            self.assertIn("pg_control_system()", query)
            self.assertEqual(password, "explicit-offline-password")
            return json.dumps(connection_control()["target"])
        def resolve(host, port, **kwargs):
            return [(2, 1, 6, "", ("127.0.0.1", port)), (10, 1, 6, "", ("::1", port, 0, 0))]
        address, identity = state.observe_target(row, "explicit-offline-password", resolve=resolve, execute=execute)
        self.assertEqual(address, "127.0.0.1"); self.assertEqual(identity, connection_control()["target"])
        self.assertEqual(calls, [{"host": "127.0.0.1", "port": 5432, "database": "honua_test", "username": "honua"}])
        for host, addresses in (("localhost", ["127.0.0.1", "203.0.113.9"]), ("127.0.0.1", ["::1"])):
            changed = {**row, "host": host}
            with self.assertRaises(ValueError):
                state.observe_target(changed, "explicit-offline-password",
                    resolve=lambda *args, **kwargs: [(2, 1, 6, "", (item, 5432)) for item in addresses],
                    execute=lambda *args, **kwargs: self.fail("unverified DNS target must not receive credentials"))
        import os
        from types import SimpleNamespace
        from unittest.mock import patch
        captured = []
        def run(command, **options):
            captured.append((command, options))
            return SimpleNamespace(returncode=0, stdout="controlled")
        with patch.dict(os.environ, {"PGSERVICE": "unowned", "PGHOSTADDR": "203.0.113.9", "PGOPTIONS": "unowned"}):
            self.assertEqual(state.database_query("SELECT 1;", "explicit-offline-password", run=run), "controlled")
        command, options = captured[0]
        self.assertIn("-X", command); self.assertIn("--no-password", command)
        self.assertEqual(set(options["env"]), {"PATH", "PGPASSWORD", "PGSSLMODE", "PGCONNECT_TIMEOUT"})
        self.assertEqual(options["env"]["PGSSLMODE"], "disable")
        for target in ({"host": "203.0.113.9"}, {"port": True}, {"database": "unowned"}, {"username": "unowned"}):
            with self.assertRaises(ValueError):
                state.database_query("SELECT 1;", "explicit-offline-password", run=lambda *a, **k: self.fail("must refuse before process spawn"), **target)

    def test_observer_keeps_storage_and_public_ids_distinct_in_one_snapshot(self):
        value = controls()
        expected = value["snapshots"]["before"]
        calls = []
        def execute(query):
            calls.append(query)
            return json.dumps({"rows": expected["rows"], "mappings": [{"publication": expected["publication"], "revision": expected["revision"]}],
                "registry": expected["connection"]["registry"], "catalog": expected["connection"]["catalog"]})
        target_calls = []
        def inspect_target(row):
            target_calls.append(row)
            return "127.0.0.1", expected["connection"]["target"]
        observed = state.snapshot(FIXTURE, bootstrap(), value, execute=execute, inspect_target=inspect_target)
        self.assertEqual(observed["publication"]["storageLayerId"], 741)
        self.assertEqual(observed["publication"]["layerId"], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(target_calls, expected["connection"]["registry"])
        self.assertIn("pg_control_system()", calls[0])
        self.assertIn("FROM honua.data_connections", calls[0])
        for secret in ("connection_string_encrypted", "secret_ref", "encryption_key_version", "secret_type"):
            self.assertNotIn(secret, calls[0])
        self.assertIn("active.revision", calls[0])
        self.assertNotIn("SELECT DISTINCT", calls[0])
        self.assertIn("pub->>'publicationType'='esri-feature-layer'", calls[0])
        self.assertIn("pub->>'resourceId'=binding->>'resourceId'", calls[0])
        self.assertIn("ST_AsGeoJSON", calls[0])
        self.assertEqual(observed, expected)

    def test_registry_sql_limits_both_selector_forms_to_active_rows(self):
        # Query construction is the control here, not a simulated SQL engine.
        # The maintained registry has UNIQUE(name); a historical same-name row
        # is not claimed as a constructible current-schema runtime fixture.
        for selector in ("22222222-2222-4222-8222-222222222222", "sdkreg-harness"):
            with self.subTest(selector=selector):
                value = controls()
                replace_output(value["cases"][0], lambda result: result.update(connectionId=selector))
                expected = value["snapshots"]["before"]
                expected["tuple"]["connectionId"] = selector
                expected["connection"] = connection_control(selector)
                queries = []
                def execute(query):
                    queries.append(query)
                    return json.dumps({"rows": expected["rows"], "mappings": [{"publication": expected["publication"], "revision": expected["revision"]}],
                        "registry": expected["connection"]["registry"], "catalog": expected["connection"]["catalog"]})
                state.snapshot(FIXTURE, bootstrap(), value, execute=execute,
                    inspect_target=lambda _: ("127.0.0.1", expected["connection"]["target"]))
                predicate = "connection_id='" + selector + "'::uuid" if selector.startswith("2222") else "name='sdkreg-harness'"
                self.assertIn("FROM honua.data_connections WHERE is_active AND " + predicate, queries[0])

    def test_observer_refuses_ambiguous_mapping_and_unsafe_tuple_without_sql(self):
        value = controls()
        expected = value["snapshots"]["before"]
        def ambiguous(query):
            mapping = {"publication": expected["publication"], "revision": expected["revision"]}
            return json.dumps({"rows": expected["rows"], "mappings": [mapping, mapping],
                "registry": expected["connection"]["registry"], "catalog": expected["connection"]["catalog"]})
        with self.assertRaises(ValueError):
            state.snapshot(FIXTURE, bootstrap(), value, execute=ambiguous, inspect_target=lambda row: self.fail("ambiguous snapshot must not connect"))
        value = controls()
        replace_output(value["cases"][0], lambda result: result.update(table='unsafe";DROP TABLE x;--'))
        called = []
        with self.assertRaises(ValueError):
            state.snapshot(FIXTURE, bootstrap(), value, execute=lambda query: called.append(query),
                inspect_target=lambda row: self.fail("unsafe tuple must not connect"))
        self.assertEqual(called, [])

    def test_inspector_requires_actual_advertisement_from_supported_view(self):
        value = controls()
        for authenticated in (True, False):
            entry = value["cases"][0 if authenticated else 4]
            wire = copy.deepcopy(entry["wire"])
            selected = {"_meta": {"honua.io/workflow-view": "full" if authenticated else "setup"}}
            catalog, _, _ = control_wire("tools/list", selected,
                {"tools": [{"name": "honua_ingest_dataset", "inputSchema": {"type": "object"}}]}, 99, authenticated)
            page = catalog["invocation"]
            session = wire["initialize"]["response"]["sessionSha256"]
            page["request"].update(id=2, sessionSha256=session)
            page["request"].update(encoded_body({key: page["request"][key] for key in ("jsonrpc", "id", "method", "params")}))
            page["response"].update(encoded_body({"jsonrpc": "2.0", "id": 2,
                "result": {"tools": [{"name": "honua_ingest_dataset", "inputSchema": {"type": "object"}}]}}))
            wire["discovery"] = [page]
            validation.invocation_wire(wire, "tools/call", entry["params"], authenticated, "MCP Inspector")
            for mutation in ("missing", "unsupported-view", "missing-tool", "borrowed-session", "forwarded-auth", "truncated-pages", "duplicate-id"):
                with self.subTest(authenticated=authenticated, mutation=mutation):
                    changed = copy.deepcopy(wire)
                    if mutation == "missing":
                        changed.pop("discovery")
                    elif mutation == "unsupported-view":
                        request = changed["discovery"][0]["request"]
                        request["params"]["_meta"]["honua.io/workflow-view"] = "unsupported"
                        request.update(encoded_body({key: request[key] for key in ("jsonrpc", "id", "method", "params")}))
                    elif mutation in ("missing-tool", "truncated-pages"):
                        result = {"tools": []} if mutation == "missing-tool" else {"tools": [{"name": "honua_ingest_dataset", "inputSchema": {}}], "nextCursor": "not-followed"}
                        changed["discovery"][0]["response"].update(encoded_body({"jsonrpc": "2.0", "id": 2, "result": result}))
                    elif mutation == "borrowed-session":
                        changed["discovery"][0]["request"]["sessionSha256"] = "sha256:" + "f" * 64
                    elif mutation == "forwarded-auth":
                        changed["discovery"][0]["request"]["authorizationPresent"] = True
                    else:
                        request = changed["discovery"][0]["request"]
                        request["id"] = changed["invocation"]["request"]["id"]
                        request.update(encoded_body({key: request[key] for key in ("jsonrpc", "id", "method", "params")}))
                        changed["discovery"][0]["response"].update(encoded_body({"jsonrpc": "2.0", "id": request["id"],
                            "result": {"tools": [{"name": "honua_ingest_dataset", "inputSchema": {}}]}}))
                    with self.assertRaises(ValueError):
                        validation.invocation_wire(changed, "tools/call", entry["params"], authenticated, "MCP Inspector")

    def test_complete_bounded_catalog_chain_and_limit_controls(self):
        entry = controls()["cases"][0]
        def observed(count):
            wire = copy.deepcopy(entry["wire"])
            pages = []
            for index in range(count):
                params = {"_meta": {"honua.io/workflow-view": "full"}}
                if index:
                    params["cursor"] = f"cursor-{index}"
                result = {"tools": [{"name": "honua_ingest_dataset" if index == count - 1 else f"other-{index}",
                                     "inputSchema": {"type": "object"}}]}
                if index < count - 1:
                    result["nextCursor"] = f"cursor-{index + 1}"
                catalog, _, _ = control_wire("tools/list", params, result, 99, True)
                page = catalog["invocation"]
                page["request"].update(id=index + 2, sessionSha256=wire["initialize"]["response"]["sessionSha256"])
                page["request"].update(encoded_body({key: page["request"][key] for key in ("jsonrpc", "id", "method", "params")}))
                page["response"].update(encoded_body({"jsonrpc": "2.0", "id": index + 2, "result": result}))
                pages.append(page)
            wire["discovery"] = pages
            return wire
        for count in (9, 32):
            validation.invocation_wire(observed(count), "tools/call", entry["params"], True, "MCP Inspector")
        for mutation in ("limit", "wrong-cursor", "repeated-cursor", "duplicate-id", "terminal-then-more", "truncated"):
            with self.subTest(mutation=mutation):
                wire = observed(33 if mutation == "limit" else 9)
                page = wire["discovery"][1]
                if mutation == "wrong-cursor":
                    page["request"]["params"]["cursor"] = "unrelated"
                elif mutation == "duplicate-id":
                    page["request"]["id"] = wire["discovery"][0]["request"]["id"]
                if mutation in ("wrong-cursor", "duplicate-id"):
                    page["request"].update(encoded_body({key: page["request"][key] for key in ("jsonrpc", "id", "method", "params")}))
                elif mutation in ("repeated-cursor", "terminal-then-more", "truncated"):
                    index = -1 if mutation == "truncated" else 1
                    page = wire["discovery"][index]
                    result = {"tools": [{"name": "honua_ingest_dataset" if index == -1 else "other", "inputSchema": {}}]}
                    if mutation != "terminal-then-more":
                        result["nextCursor"] = "cursor-1" if mutation == "repeated-cursor" else "not-followed"
                    page["response"].update(encoded_body({"jsonrpc": "2.0", "id": page["request"]["id"], "result": result}))
                with self.assertRaises(ValueError):
                    validation.invocation_wire(wire, "tools/call", entry["params"], True, "MCP Inspector")

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
