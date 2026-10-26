"""Synthetic semantic controls only; actual client/candidate proof is separate."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest

from mcp_grounding_status_state import DATASET_CATALOG_SQL, admin_release_projection, dataset_catalog, graph_resource
from mcp_grounding_status_validation import (clarification_output, declared_status,
                                             grounding_output, process_catalog_resource, catalog_cursors,
                                             scenario, source_context)
from mcp_render_validation_controls import FIXTURE, bootstrap


AREA = {"serviceId": "service-42", "serviceName": "sdkreg-area", "layerId": 9,
        "storageLayerId": 7, "connectionId": "connection-42", "locator": "honua_data.sdkreg_area"}
METADATA = AREA | {"revision": 12, "resourceId": "resource-42", "resourceName": "area",
                   "resourceType": "feature-dataset"}
# Readable datasets as honua-server 8cb074f7 exposes them on the certification seed: two
# resources share storage layer 1, and an unowned published service sits beside the owned one.
DATASETS = {"layers": [{"id": "7", "name": "area"}, {"id": "1", "name": "Mixed Geometry Test Layer"},
                       {"id": "1", "name": "Mixed Geometry Test Layer imagery"}],
            "services": ["sdkreg-area", "sdkreg-sites"]}
CATALOG = {"status": "ok", "catalogVersion": "honua.process_catalog.v1", "processes": [{"processId": "geometry.buffer",
            "displayName": "Buffer geometry", "executionKind": "Job", "entryPoints": ["job"],
            "supportedExecutionModes": ["Async"]}]}


def candidate(identifier, kind, score):
    display_name = {"7": "area", "geometry.buffer": "Buffer geometry"}.get(identifier, identifier)
    return {"id": identifier, "kind": kind, "score": score,
            "evidence": ["title:1"], "displayName": display_name, "confidenceBand": "Medium"}


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

    def test_readable_dataset_catalog_readback_is_bounded_and_exact(self):
        self.assertEqual(dataset_catalog(execute=lambda _: json.dumps(DATASETS)), DATASETS)
        self.assertIn("'raster-dataset'", DATASET_CATALOG_SQL)
        for bad in ([], {"layers": []}, {"layers": [{"id": "x", "name": "a"}], "services": []},
                    {"layers": [{"id": "1", "name": ""}], "services": []},
                    {"layers": [{"id": "1", "name": "a"}, {"id": "1", "name": "a"}], "services": []},
                    {"layers": [], "services": ["s", "s"]}, {"layers": [], "services": [], "extra": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                dataset_catalog(execute=lambda _, bad=bad: json.dumps(bad))

    def test_owned_graph_and_actual_job_catalog_are_required(self):
        context = source_context(AREA, METADATA, CATALOG, DATASETS)
        self.assertEqual(context["datasetIds"], {"7", "sdkreg-area"})
        self.assertEqual(context["datasetIds"], {"7", "sdkreg-area"})
        self.assertEqual(context["datasetNames"], {"7": frozenset({"area"}), "sdkreg-area": frozenset({"sdkreg-area"}),
            "1": frozenset({"Mixed Geometry Test Layer", "Mixed Geometry Test Layer imagery"}),
            "sdkreg-sites": frozenset({"sdkreg-sites"})})
        self.assertEqual(context["processNames"], {"geometry.buffer": "Buffer geometry"})
        for change in (lambda m, c: m.update(resourceType="table"),
                       lambda m, c: m.update(storageLayerId=8),
                       lambda m, c: c["processes"][0].update(entryPoints=["workflow"]),
                       lambda m, c: c["processes"].append(copy.deepcopy(c["processes"][0]))):
            with self.subTest(change=change):
                metadata, catalog = copy.deepcopy(METADATA), copy.deepcopy(CATALOG)
                change(metadata, catalog)
                with self.assertRaises(ValueError): source_context(AREA, metadata, catalog, DATASETS)

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

    def test_shortlist_may_rank_any_independently_observed_readable_dataset(self):
        context = source_context(AREA, METADATA, CATALOG, DATASETS)
        def with_sources(value):
            value["draftIntent"]["provenance"]["sources"] = [
                {"sourceId": d["id"], "description": d["displayName"]} for d in value["candidates"]["datasets"]]
            return value
        output = grounded()
        output["candidates"]["datasets"] += [candidate("1", "Dataset", .5), candidate("sdkreg-sites", "Dataset", .5)]
        output["candidates"]["datasets"][2]["displayName"] = "Mixed Geometry Test Layer imagery"
        self.assertEqual(grounding_output(with_sources(output), context, "Buffer sdkreg-area geometry"), "grounding-123")
        for change in (lambda d: d.append(candidate("99", "Dataset", .1)),
                       lambda d: d.append(candidate("sdkreg-unpublished", "Dataset", .1)),
                       lambda d: d[2].update(displayName="Some other layer"),
                       lambda d: d.append(dict(d[2]))):
            changed = copy.deepcopy(output)
            change(changed["candidates"]["datasets"])
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "Dataset candidates"):
                grounding_output(with_sources(changed), context, "Buffer sdkreg-area geometry")

    def test_observed_readable_datasets_must_include_the_owned_layer_and_service(self):
        for change in (lambda d: d["layers"].pop(0), lambda d: d["services"].remove("sdkreg-area"),
                       lambda d: d["layers"][0].update(name="renamed"), lambda d: d["services"].append("7"),
                       lambda d: d.update(extra=[]), lambda d: d["layers"].append({"id": "2"})):
            datasets = copy.deepcopy(DATASETS)
            change(datasets)
            with self.subTest(change=change), self.assertRaises(ValueError):
                source_context(AREA, METADATA, CATALOG, datasets)

    def test_paged_catalog_is_joined_only_across_a_complete_continuation(self):
        text = json.dumps(CATALOG)
        page = lambda part, cursor=None: {"contents": [{"uri": "honua://catalog/processes",
            "mimeType": "application/json", "text": part}], **({"nextCursor": cursor} if cursor else {})}
        cut = len(text) // 2
        pages = [page(text[:cut], "r1:0:%d" % cut), page(text[cut:])]
        self.assertEqual(process_catalog_resource(pages), CATALOG)
        self.assertEqual(catalog_cursors(pages), ["r1:0:%d" % cut])
        for invalid in ([pages[0]], pages[0], [pages[1], pages[0]], [pages[0], pages[0]],
                        [pages[0], page(text[cut:], "r1:0:1")], [pages[0], page("")]):
            with self.assertRaises(ValueError): process_catalog_resource(invalid)
        with self.assertRaises(ValueError): catalog_cursors([pages[0]])
        with self.assertRaises(ValueError): catalog_cursors([page(text, "same"), page(text, "same"), page(text)])

    def test_shortlist_and_provenance_must_name_owned_graph_and_process(self):
        context = source_context(AREA, METADATA, CATALOG, DATASETS)
        good = grounded()
        self.assertEqual(grounding_output(good, context, good["draftIntent"]["goal"]), "grounding-123")
        for change in (lambda o: o["candidates"]["datasets"].clear(),
                       lambda o: o["candidates"]["processes"][0].update(id="unrelated"),
                       lambda o: o["draftIntent"]["provenance"].update(processDefinitions=[]),
                       lambda o: o["draftIntent"]["provenance"]["sources"].append({"sourceId": "unranked"}),
                       lambda o: o["candidates"]["datasets"][0].update(evidence=[""]),
                       lambda o: o["candidates"]["datasets"][0].update(displayName="invented area"),
                       lambda o: o["candidates"]["datasets"][1].update(score=.95)):
            value = copy.deepcopy(good); change(value)
            with self.assertRaises(ValueError): grounding_output(value, context, good["draftIntent"]["goal"])
        # A provenance list that repeats an invented top hit used to pass the
        # old owned-ID intersection check. Neither an extra dataset nor a
        # catalog-absent process is independently grounded.
        for kind, invented in (("datasets", candidate("missing-layer", "Dataset", .95)),
                               ("processes", candidate("missing.process", "Process", .95))):
            value = copy.deepcopy(good)
            value["candidates"][kind].insert(0, invented)
            provenance = value["draftIntent"]["provenance"]
            if kind == "datasets":
                provenance["sources"].insert(0, {"sourceId": invented["id"],
                                                  "description": invented["displayName"]})
            else:
                provenance["processDefinitions"].insert(0, invented["id"])
            with self.assertRaises(ValueError): grounding_output(value, context, good["draftIntent"]["goal"])
        value = copy.deepcopy(good)
        value["candidates"]["datasets"][1]["score"] = .9
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
        context = source_context(AREA, METADATA, CATALOG, DATASETS)
        self.assertEqual(clarification_output(before, after, "sdkreg-area", context), "grounding-123")
        for kind, invented in (("datasets", candidate("missing-layer", "Dataset", .95)),
                               ("processes", candidate("missing.process", "Process", .95))):
            changed = copy.deepcopy(after)
            changed["candidates"][kind].insert(0, invented)
            provenance = changed["draftIntent"]["provenance"]
            if kind == "datasets":
                provenance["sources"].insert(0, {"sourceId": invented["id"],
                                                  "description": invented["displayName"]})
            else:
                provenance["processDefinitions"].insert(0, invented["id"])
            with self.assertRaises(ValueError): clarification_output(before, changed, "sdkreg-area", context)
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
                                       "ControlPlane__PlatformRelease__Workers__0__RuntimeProfile": "managed",
                                       "ControlPlane__PlatformRelease__Workers__0__ArtifactReference": serving,
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

    def test_declared_worker_projects_execution_without_skew(self):
        # honua-server builds execution entries from ControlPlane:ExecutionWorkloads
        # (its appsettings ship geoprocessing-local and geoprocessing-aws-batch with no
        # runtime profile) and resolves each against PlatformRelease:Workers. The
        # declared managed worker projects only onto a managed-profile workload.
        declaration, projection, serving, alternate = self.reviewed_skew()
        projection["execution"] = [
            {"id": "geoprocessing-local", "runtimeProfile": None, "effectiveArtifactReference": None,
             "projectedFromRelease": False, "skewed": False},
            {"id": "geoprocessing-aws-batch", "runtimeProfile": None, "effectiveArtifactReference": None,
             "projectedFromRelease": False, "skewed": False},
            {"id": "managed-gp", "runtimeProfile": "Managed", "effectiveArtifactReference": serving,
             "projectedFromRelease": True, "skewed": False}]
        self.assertEqual(len(declared_status(projection, {"platformRelease": projection}, declaration)["execution"]), 3)
        for change in (lambda p: p["execution"][0].update(effectiveArtifactReference=serving),
                       lambda p: p["execution"][0].update(projectedFromRelease=True),
                       lambda p: p["execution"][2].update(effectiveArtifactReference=alternate),
                       lambda p: p["execution"][2].update(projectedFromRelease=False),
                       lambda p: p["execution"][2].update(skewed=True),
                       lambda p: p["execution"][2].update(runtimeProfile="native"),
                       lambda p: p["execution"][1].update(id="geoprocessing-local"),
                       lambda p: p["execution"][1].update(id="cert-ops-skew"),
                       lambda p: p["execution"][1].update(workloadKind="unprojected"),
                       lambda p: p["execution"].append("geoprocessing-local")):
            changed = copy.deepcopy(projection); change(changed)
            with self.assertRaises(ValueError): declared_status(changed, {"platformRelease": changed}, declaration)

    def test_reviewed_declaration_must_co_version_the_governed_managed_worker(self):
        declaration, projection, serving, alternate = self.reviewed_skew()
        environment = declaration["environment"]
        for change in (lambda e: e.pop("ControlPlane__PlatformRelease__Workers__0__ArtifactReference"),
                       lambda e: [e.pop(k) for k in list(e) if "__Workers__" in k],
                       lambda e: e.update(ControlPlane__PlatformRelease__Workers__0__ArtifactReference=alternate),
                       lambda e: e.update(ControlPlane__PlatformRelease__Workers__0__RuntimeProfile="native"),
                       lambda e: e.update(ControlPlane__PlatformRelease__Workers__1__RuntimeProfile="native",
                                          ControlPlane__PlatformRelease__Workers__1__ArtifactReference=serving),
                       lambda e: e.update(ControlPlane__PlatformRelease__Workers__2__RuntimeProfile="native")):
            changed = copy.deepcopy(declaration); change(changed["environment"])
            with self.assertRaises(ValueError): declared_status(projection, {"platformRelease": projection}, changed)
        self.assertEqual(environment["ControlPlane__PlatformRelease__Workers__0__ArtifactReference"], serving)

    def reviewed_skew(self):
        serving = "ghcr.io/honua-io/honua-server@sha256:" + "a" * 64
        alternate = "ghcr.io/honua-io/honua-server@sha256:" + "b" * 64
        declaration = {"schema": "honua.mcp-ops-config-declaration/v1",
                       "environment": {"ControlPlane__PlatformRelease__Version": "2026.1",
                                       "ControlPlane__PlatformRelease__ServingArtifactReference": serving,
                                       "ControlPlane__PlatformRelease__Workers__0__RuntimeProfile": "managed",
                                       "ControlPlane__PlatformRelease__Workers__0__ArtifactReference": serving,
                                       "ControlPlane__DeployTargets__0__TargetId": "cert-ops-skew",
                                       "ControlPlane__DeployTargets__0__ArtifactReference": alternate},
                       "artifactBindings": {"declaredServing": serving, "explicitTestTarget": alternate},
                       "expectedSkewedIds": ["cert-ops-skew"]}
        projection = release_projection()
        projection.update(isCoVersioned=False, skewedIds=["cert-ops-skew"],
                          serving=[{"id": "cert-ops-skew", "runtimeProfile": None,
                                    "effectiveArtifactReference": alternate,
                                    "projectedFromRelease": False, "skewed": True}])
        return declaration, projection, serving, alternate


if __name__ == "__main__":
    unittest.main()
