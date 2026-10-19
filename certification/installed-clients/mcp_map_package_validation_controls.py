"""Controlled offline composition evidence only; never a canonical producer."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest

import mcp_map_package_validation as validation
from mcp_render_validation_controls import FIXTURE, bootstrap, runtime

HERE = Path(__file__).resolve().parent


def responses():
    # Explicit independent expected composition, not output from the oracle or
    # a server. In particular bindingId must become sourceId and integer EPSG
    # must become the canonical CRS spelling without losing the authored view.
    package = {"mapPackageId": "map_controlled", "format": "honua_map_package.v1", "status": "Draft",
               "templateId": "sdkreg-authored-template", "themeId": "theme_sdkreg_authored",
               "styleRefs": [{"styleId": "style_sdkreg_authored"}],
               "sourceBindings": [{"sourceId": "authored-area", "protocol": "geoservices_feature_service",
                   "locator": {"url": "http://localhost:8080/rest/services/allocated-service-id/FeatureServer",
                               "serviceId": "allocated-service-id", "layerId": "3"},
                   "filter": "name = 'block'", "metadata": {"sdkregPurpose": "authored-composition"}}],
               "initialView": {"bbox": [-90, 0, -45, 60], "crs": "EPSG:4326"},
               "createdAt": "2026-10-19T00:00:00Z"}
    value = {"status": "created", "packageId": "map_controlled", "resourceUri": "honua://map-packages/map_controlled",
             "warnings": [], "package": package}
    def error():
        item = {"status": "error", "code": "invalid_argument", "message": "Controlled authored invalid selector"}
        return {"isError": True, "structuredContent": item, "content": [{"type": "text", "text": json.dumps(item)}]}
    return [{"isError": False, "structuredContent": value, "content": [{"type": "text", "text": json.dumps(value)}]}, error(), error()]


def reads():
    uri = "honua://map-packages/map_controlled"
    value = {"packageKind": "map_package", "packageId": "map_controlled", "resourceUri": uri,
             "packageStatus": "draft", "deploymentCount": 0, "deploymentResourceUris": [], "provenance": {}}
    return [{"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(value)}]},
            {"error": {"code": -32002, "message": "Controlled unknown package", "data": {"code": "not_found"}}},
            {"error": {"code": -32000, "message": "Controlled unauthenticated read", "data": {
                "code": "unauthenticated", "requiresReauthentication": True}}}]


def observations():
    value = validation.scenario(FIXTURE, bootstrap())
    value["runtimeBefore"], value["runtimeAfter"] = runtime(), runtime()
    for case, result in zip(value["cases"], responses(), strict=True):
        case["result"] = result
    uri = "honua://map-packages/map_controlled"
    value["reads"] = [{"id": name, "uri": selected, "authenticated": authenticated, "result": result}
                      for (name, selected, authenticated), result in zip([
                          ("fresh-created", uri, True), ("unknown-package", uri + "-uncreated", True),
                          ("anonymous-created", uri, False)], reads(), strict=True)]
    return value


def sync_text(value):
    result = value["cases"][0]["result"]
    result["content"][0]["text"] = json.dumps(result["structuredContent"])


class MapPackageControls(unittest.TestCase):
    def test_authored_composition_and_actual_resource_contract(self):
        judged = validation.judge(FIXTURE, bootstrap(), observations())
        self.assertTrue(all(judged[key] for key in ("positive", "negative", "mediaSchema", "freshReadback", "scratchOnly")))
        authored = validation.scenario(FIXTURE, bootstrap())["cases"][0]["arguments"]
        self.assertEqual({"bbox": [-90, 0, -45, 60], "crs": 4326}, authored["initialView"])
        self.assertEqual("allocated-service-id", authored["sourceBindings"][0]["serviceId"])
        self.assertEqual("3", authored["sourceBindings"][0]["layerId"])

    def test_ignored_or_changed_geometry_and_selectors_refuse(self):
        for name in ("bbox", "boolean-coordinate", "crs", "binding-id", "protocol", "locator", "filter", "metadata", "style", "theme", "template", "status"):
            with self.subTest(name=name):
                value = observations(); package = value["cases"][0]["result"]["structuredContent"]["package"]
                if name == "bbox": package["initialView"]["bbox"] = [0, 0, 1, 1]
                elif name == "boolean-coordinate": package["initialView"]["bbox"][1] = False
                elif name == "crs": package["initialView"]["crs"] = "EPSG:3857"
                elif name == "binding-id": package["sourceBindings"][0]["sourceId"] = "ignored"
                elif name == "protocol": package["sourceBindings"][0]["protocol"] = "wms"
                elif name == "locator": package["sourceBindings"][0]["locator"]["layerId"] = "17"
                elif name in ("filter", "metadata"): package["sourceBindings"][0].pop(name)
                elif name == "style": package["styleRefs"] = []
                elif name == "theme": package["themeId"] = "ignored"
                elif name == "template": package["templateId"] = "ignored"
                else: package["status"] = "Ready"
                sync_text(value)
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), value)

    def test_missing_conflicting_or_unrelated_success_media_refuses(self):
        for name in ("missing", "conflict", "image", "array", "not-json"):
            with self.subTest(name=name):
                value = observations(); result = value["cases"][0]["result"]
                if name == "missing": result.clear()
                elif name == "conflict": result["content"][0]["text"] = "{}"
                elif name == "image": result["content"].append({"type": "image", "data": "AA==", "mimeType": "image/png"})
                elif name == "array": result["structuredContent"] = []
                else: result["content"][0]["text"] = "not JSON"
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), value)

    def test_new_uri_must_resolve_the_same_draft_with_no_deployment_credit(self):
        for name in ("missing", "id", "uri", "mime", "status", "count", "boolean-count", "edges", "kind"):
            with self.subTest(name=name):
                value = observations(); item = value["reads"][0]["result"]["contents"][0]; payload = json.loads(item["text"])
                if name == "missing": value["reads"] = []
                elif name == "uri": item["uri"] += "-other"
                elif name == "mime": item["mimeType"] = "text/plain"
                elif name == "id": payload["packageId"] = "map_other"
                elif name == "status": payload["packageStatus"] = "published"
                elif name == "count": payload["deploymentCount"] = 1
                elif name == "boolean-count": payload["deploymentCount"] = False
                elif name == "edges": payload["deploymentResourceUris"] = ["honua://deployments/imaginary"]
                else: payload["packageKind"] = "app_package"
                item["text"] = json.dumps(payload)
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), value)

    def test_resource_refusals_require_server_codes_and_no_data(self):
        for index in (1, 2):
            for name in ("accepted", "code", "semantic", "missing-message", "leak", "nested-leak", "reauth"):
                if index == 1 and name == "reauth": continue
                with self.subTest(index=index, name=name):
                    value = observations(); result = value["reads"][index]["result"]
                    if name == "accepted": result.clear(); result["contents"] = []
                    elif name == "code": result["error"]["code"] = -32603
                    elif name == "semantic": result["error"]["data"]["code"] = "internal"
                    elif name == "missing-message": result["error"]["message"] = ""
                    elif name == "leak": result["contents"] = [{"text": "secret"}]
                    elif name == "nested-leak": result["error"]["data"]["record"] = {"secret": "row"}
                    else: result["error"]["data"]["requiresReauthentication"] = False
                    with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), value)

    def test_bad_identity_timestamp_or_success_envelope_refuses(self):
        for name in ("id", "uri", "created", "naive", "warnings", "failed"):
            with self.subTest(name=name):
                value = observations(); output = value["cases"][0]["result"]["structuredContent"]
                if name == "id": output["package"]["mapPackageId"] = "map_other"
                elif name == "uri": output["resourceUri"] = "https://unowned.example/maps/map_controlled"
                elif name == "created": output["package"]["createdAt"] = "tomorrow"
                elif name == "naive": output["package"]["createdAt"] = "2026-10-19T00:00:00"
                elif name == "warnings": output["warnings"] = [{"code": "sourceNotResolved", "path": "source", "message": "unresolved"}]
                else: value["cases"][0]["result"]["isError"] = True
                sync_text(value)
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), value)

    def test_missing_raw_changed_request_bootstrap_or_runtime_refuses(self):
        for name in ("missing", "request", "source", "fixture", "bootstrap", "runtime-before", "runtime-after", "anonymous-auth"):
            with self.subTest(name=name):
                value, bound = observations(), bootstrap()
                if name == "missing": value["cases"].pop()
                elif name == "request": value["cases"][1]["arguments"]["initialView"]["bbox"] = [0, 0, 1, 1]
                elif name == "source": value["candidateSourceSha"] = "f" * 40
                elif name == "fixture": value["fixtureSha256"] = "sha256:" + "0" * 64
                elif name == "bootstrap": bound["ownedArea"]["layerId"] = 4
                elif name in ("runtime-before", "runtime-after"): value["runtimeBefore" if name == "runtime-before" else "runtimeAfter"]["startedAt"] = "different"
                else: value["reads"][2]["authenticated"] = True
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bound, value)

    def test_both_invalid_authored_calls_require_typed_refusal(self):
        for index in (1, 2):
            for name in ("accepted", "wrong-code", "conflicting-text"):
                with self.subTest(index=index, name=name):
                    value = observations(); result = value["cases"][index]["result"]
                    if name == "accepted": result["isError"] = False
                    elif name == "wrong-code": result["structuredContent"]["code"] = "internal"
                    else: result["content"][0]["text"] = "{}"
                    with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), value)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--write-controls":
        directory = Path(sys.argv[2])
        subprocess.run([sys.executable, str(HERE / "mcp_render_validation_controls.py"), "--write-controls", str(directory)], check=True)
        (directory / "responses.json").write_text(json.dumps(responses()))
        (directory / "reads.json").write_text(json.dumps(reads()))
    else:
        unittest.main()
