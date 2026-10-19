"""Controlled offline composition evidence only; never a canonical producer."""
from __future__ import annotations

import json
import base64
import hashlib
import copy
from pathlib import Path
import subprocess
import sys
import unittest

import mcp_map_package_validation as validation
from mcp_render_validation_controls import FIXTURE, bootstrap, runtime

HERE = Path(__file__).resolve().parent
CREDENTIAL = "controlled-private-key"
CONTROL_SALT = bytes.fromhex("a" * 32)
CONTROL_FINGERPRINT = "scrypt:" + CONTROL_SALT.hex() + ":" + hashlib.scrypt(
    CREDENTIAL.encode(), salt=CONTROL_SALT, n=16384, r=8, p=1, dklen=32).hex()


def encoded_body(value):
    body = json.dumps(value).encode()
    return {"bodyBase64": base64.b64encode(body).decode(), "bodySha256": "sha256:" + hashlib.sha256(body).hexdigest()}


def control_wire(method, params, result, position, authenticated):
    fingerprint = CONTROL_FINGERPRINT
    session = "sha256:" + hashlib.sha256(f"controlled-session-{position}".encode()).hexdigest()
    def request(identifier, selected, arguments, fresh):
        frame = {"jsonrpc": "2.0", "id": identifier, "method": selected, "params": arguments}
        return {**frame, **encoded_body(frame), "sessionSha256": None if fresh else session,
                "credentialPresent": authenticated, "credentialFingerprint": fingerprint if authenticated else None,
                "authorizationPresent": False, "cookiePresent": False}
    def response(frame, initialize=False):
        return {"status": 200, "contentType": "application/json", "contentEncoding": "", "sessionSha256": session if initialize else None,
                "complete": True, "delivered": True, **encoded_body(frame)}
    frame = {"jsonrpc": "2.0", "id": 1}
    # Preserve even forbidden outer data in a controlled refusal: the semantic
    # judge must reject it, rather than the fixture silently deleting the leak.
    frame.update(copy.deepcopy(result) if isinstance(result, dict) and "error" in result else {"result": result})
    wire = {"schema": "honua.mcp-candidate-wire/v1", "upstream": "http://127.0.0.1:8080/mcp", "listener": "http://127.0.0.1:43123/mcp",
            "method": method, "expectedParams": params, "authenticated": authenticated, "expectedCredentialFingerprint": fingerprint,
            "invalid": False, "completed": True,
            "initialize": {"request": request(0, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "controlled-installed-sdk", "version": "1.32.1"}}, True),
                "response": response({"jsonrpc": "2.0", "id": 0, "result": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "serverInfo": {"name": "controlled", "version": "0.0.0"}}}, True)},
            "invocation": {"request": request(1, method, params, False), "response": response(frame)}}
    public = {"error": copy.deepcopy(frame["error"])} if "error" in frame else copy.deepcopy(frame["result"])
    client = {"kind": "sdk", "canonicalClient": "Official MCP TypeScript SDK", "version": "1.32.1", "processId": 123,
              "requestUrl": wire["listener"], "sessionSha256": session, "output": public}
    return wire, client, frame if "error" in frame else frame["result"]


def bind_controls(value):
    """Make changed controlled server output agree with wire/client, so the
    authored semantic oracle—not an earlier copied-response check—rejects it."""
    value["canonicalClient"], value["clientVersion"] = "Official MCP TypeScript SDK", "1.32.1"
    for position, case in enumerate(value["cases"]):
        case["wire"], case["client"], case["result"] = control_wire("tools/call", {"name": "honua_create_map_package", "arguments": case["arguments"]}, case["result"], position, case["authenticated"])
    for position, read in enumerate(value["reads"], 4):
        read["wire"], read["client"], read["result"] = control_wire("resources/read", {"uri": read["uri"]}, read["result"], position, read["authenticated"])
    return value


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
    return [{"isError": False, "structuredContent": value, "content": [{"type": "text", "text": json.dumps(value)}]}, error(), error(),
            {"error": {"code": -32000, "message": "Controlled unauthenticated creation", "data": {
                "code": "unauthenticated", "requiresReauthentication": True}}}]


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
    return bind_controls(value)


def sync_text(value):
    result = value["cases"][0]["result"]
    result["content"][0]["text"] = json.dumps(result["structuredContent"])


class MapPackageControls(unittest.TestCase):
    def test_json_numeric_round_trip_preserves_value_but_never_boolean_identity(self):
        self.assertTrue(validation.same_json({"bbox": [-90.0, 0.0, -45.0, 60.0]}, {"bbox": [-90, 0, -45, 60]}))
        self.assertFalse(validation.same_json({"bbox": [-90, False, -45, 60]}, {"bbox": [-90.0, 0.0, -45.0, 60.0]}))
        self.assertFalse(validation.same_json(1, True))
        self.assertFalse(validation.same_json(float("nan"), float("nan")))
        value = observations()
        # This is the real JSON number normalization performed by JS, not a
        # copied expected scenario; unchanged wire/client outputs stay bound.
        value["cases"][0]["arguments"]["initialView"]["bbox"] = [-90, 0, -45, 60]
        self.assertTrue(validation.judge(FIXTURE, bootstrap(), value)["freshReadback"])

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
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), bind_controls(value))

    def test_missing_conflicting_or_unrelated_success_media_refuses(self):
        for name in ("missing", "conflict", "image", "array", "not-json"):
            with self.subTest(name=name):
                value = observations(); result = value["cases"][0]["result"]
                if name == "missing": result.clear()
                elif name == "conflict": result["content"][0]["text"] = "{}"
                elif name == "image": result["content"].append({"type": "image", "data": "AA==", "mimeType": "image/png"})
                elif name == "array": result["structuredContent"] = []
                else: result["content"][0]["text"] = "not JSON"
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), bind_controls(value))

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
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), bind_controls(value))

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
                    with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), bind_controls(value))

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
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), bind_controls(value))

    def test_resource_error_identifier_cannot_hide_arbitrary_data(self):
        valid = reads()[1]
        validation.resource_refusal(valid, -32002, "not_found")
        for identifier in ({"record": {"secret": "row"}}, ["secret"], True, "", 9007199254740992):
            with self.subTest(identifier=identifier):
                value = copy.deepcopy(valid); value["id"] = identifier
                with self.assertRaises(ValueError): validation.resource_refusal(value, -32002, "not_found")

    def test_anonymous_creation_requires_refusal_no_forwarded_authority_or_creator_session(self):
        for name in ("accepted", "wrong-code", "forwarded-auth", "creator-session", "missing-wire", "local-error"):
            with self.subTest(name=name):
                value = observations(); case = value["cases"][3]
                if name == "accepted":
                    case["result"] = responses()[0]; bind_controls(value)
                elif name == "wrong-code":
                    case["result"]["error"]["data"]["code"] = "not_found"; bind_controls(value)
                elif name == "forwarded-auth":
                    for record in (case["wire"]["initialize"], case["wire"]["invocation"]):
                        record["request"]["credentialPresent"] = True
                        record["request"]["credentialFingerprint"] = CONTROL_FINGERPRINT
                elif name == "creator-session":
                    creator = value["cases"][0]["wire"]["initialize"]["response"]["sessionSha256"]
                    case["wire"]["initialize"]["response"]["sessionSha256"] = creator
                    case["wire"]["invocation"]["request"]["sessionSha256"] = creator
                    case["client"]["sessionSha256"] = creator
                elif name == "missing-wire": case["wire"] = None
                else: case["client"]["output"] = {"error": {"message": "local catalog refusal"}}
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), value)

    def test_wire_response_id_must_match_the_actual_request_even_when_scalar(self):
        for identifier in ({"secret": "row"}, ["secret"], True, 2, "1", None):
            with self.subTest(identifier=identifier):
                value = observations(); record = value["reads"][1]["wire"]["invocation"]["response"]
                frame = json.loads(base64.b64decode(record["bodyBase64"])); frame["id"] = identifier
                record.update(encoded_body(frame)); value["reads"][1]["result"] = frame
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), value)

    def test_missing_wrong_uri_session_authority_candidate_or_response_observation_refuses(self):
        for name in ("missing", "uri", "method", "session", "reused", "credential", "anonymous-inherited", "authorization", "candidate", "response", "missing-delivery", "raw-header"):
            with self.subTest(name=name):
                value = observations(); wire = value["reads"][0]["wire"]
                if name == "missing": value["reads"][0]["wire"] = None
                elif name == "uri": wire["expectedParams"]["uri"] += "-other"
                elif name == "method": wire["method"] = "tools/list"
                elif name == "session": wire["invocation"]["request"]["sessionSha256"] = "sha256:" + "0" * 64
                elif name == "reused":
                    previous = value["cases"][0]["wire"]["initialize"]["response"]["sessionSha256"]
                    wire["initialize"]["response"]["sessionSha256"] = wire["invocation"]["request"]["sessionSha256"] = previous
                    value["reads"][0]["client"]["sessionSha256"] = previous
                elif name == "credential": wire["invocation"]["request"]["credentialFingerprint"] = "sha256:" + "0" * 64
                elif name == "anonymous-inherited": value["reads"][2]["wire"]["initialize"]["request"]["credentialPresent"] = True
                elif name == "authorization": wire["invocation"]["request"]["authorizationPresent"] = True
                elif name == "candidate": wire["upstream"] = "http://127.0.0.1:8081/mcp"
                elif name == "response": wire["invocation"]["response"]["bodyBase64"] = base64.b64encode(b"{}").decode()
                elif name == "missing-delivery": wire["invocation"]["response"]["delivered"] = False
                else: wire["invocation"]["request"]["Authorization"] = "secret"
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), value)

    def test_inspector_lossy_diagnostics_are_distinct_from_original_server_error(self):
        value = observations(); value["canonicalClient"], value["clientVersion"] = "MCP Inspector", "2.10.1"
        for item in value["cases"] + value["reads"]:
            _, frame = validation.wire_frame(item["wire"]["invocation"])
            error = frame.get("error")
            item["client"] = {"kind": "inspector-cli", "canonicalClient": "MCP Inspector", "version": "2.10.1", "processId": 456,
                "requestUrl": item["wire"]["listener"], "exitCode": 2 if error else 5 if frame["result"].get("isError") else 0,
                "exitSignal": None, "timedOut": False, "stdout": "" if error else json.dumps({"result": frame["result"]}),
                "stderr": json.dumps({"error": {"code": "error", "message": error["message"]}}) if error else ""}
        self.assertTrue(validation.judge(FIXTURE, bootstrap(), value)["freshReadback"])
        changed = copy.deepcopy(value); changed["reads"][1]["client"]["stderr"] = json.dumps({"error": {"code": "error", "message": "unrelated local failure"}})
        with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), changed)
        changed = copy.deepcopy(value); changed["reads"][1]["wire"] = None
        with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), changed)

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
                    with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), bind_controls(value))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--write-controls":
        directory = Path(sys.argv[2])
        subprocess.run([sys.executable, str(HERE / "mcp_render_validation_controls.py"), "--write-controls", str(directory)], check=True)
        (directory / "responses.json").write_text(json.dumps(responses()))
        (directory / "reads.json").write_text(json.dumps(reads()))
    else:
        unittest.main()
