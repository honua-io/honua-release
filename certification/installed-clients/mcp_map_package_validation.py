"""Authored map composition and fresh resource-read semantics; no client runtime.

Drafts are bounded, per-replica scratch. These oracles do not claim durable
deployment, cross-replica visibility, style resolution or model inference.
"""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_render_validation import scenario as fixture_binding, negative_output

SCHEMA = "honua.mcp-map-package-scenario/v1"
OPERATIONS = ["tools/call:create_map_package", "resources/read:Map package"]
WIRE_LIMIT = 2 * 1024 * 1024


def strict_json(data):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate JSON member")
            value[key] = item
        return value
    def invalid_number(_):
        raise ValueError("nonfinite JSON number")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("nonfinite JSON number")
        return number
    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_number, parse_float=finite_float)


def rpc_id(value):
    return ((isinstance(value, str) and 0 < len(value) <= 256)
            or (type(value) is int and abs(value) <= 9007199254740991))


def same_json(left, right):
    # JSON has one number kind. JS parses integral floats as numbers and
    # serializes -90.0 as -90; compare their value without equating false/0.
    if type(left) in (int, float) and type(right) in (int, float):
        return (not isinstance(left, float) or math.isfinite(left)) and (not isinstance(right, float) or math.isfinite(right)) and left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same_json(value, right[key]) for key, value in left.items())
    if isinstance(left, list):
        return len(left) == len(right) and all(same_json(a, b) for a, b in zip(left, right, strict=True))
    return left == right


def wire_bytes(value):
    encoded = value.get("bodyBase64")
    if not isinstance(encoded, str) or not 0 < len(encoded) <= 4 * ((WIRE_LIMIT + 2) // 3):
        raise ValueError("wire bytes missing or oversized")
    data = base64.b64decode(encoded, validate=True)
    if not 0 < len(data) <= WIRE_LIMIT or value.get("bodySha256") != "sha256:" + hashlib.sha256(data).hexdigest():
        raise ValueError("retained wire body hash differs")
    return data


def wire_frame(record):
    if not isinstance(record, dict) or set(record) != {"request", "response"}:
        raise ValueError("wire request/response record has an invalid schema")
    request, response = record.get("request"), record.get("response")
    if not isinstance(request, dict) or not isinstance(response, dict):
        raise ValueError("wire request/response missing")
    if (set(request) != {"jsonrpc", "id", "method", "params", "bodyBase64", "bodySha256", "sessionSha256", "credentialPresent", "credentialFingerprint", "authorizationPresent", "cookiePresent"}
            or set(response) != {"status", "contentType", "contentEncoding", "sessionSha256", "complete", "delivered", "bodyBase64", "bodySha256"}
            or not isinstance(response["contentType"], str) or not isinstance(response["contentEncoding"], str)):
        raise ValueError("wire observation contains unknown fields or media")
    request_body = strict_json(wire_bytes(request))
    if (not isinstance(request_body, dict) or request_body.get("jsonrpc") != "2.0" or not rpc_id(request_body.get("id"))
            or any(not same_json(request.get(key), request_body.get(key)) for key in ("jsonrpc", "id", "method", "params"))
            or type(request.get("id")) is not type(request_body["id"])):
        raise ValueError("wire request projection differs from original body")
    if (response.get("complete") is not True or response.get("delivered") is not True
            or type(response.get("status")) is not int or response["status"] != 200):
        raise ValueError("wire response was not completely delivered")
    data = wire_bytes(response)
    encoding = response.get("contentEncoding", "").strip().lower()
    if encoding in ("gzip", "deflate"):
        decoder = zlib.decompressobj(31 if encoding == "gzip" else 15)
        data = decoder.decompress(data, WIRE_LIMIT + 1)
        if len(data) > WIRE_LIMIT or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError("wire compression is incomplete, trailing or oversized")
    elif encoding not in ("", "identity"):
        raise ValueError("unsupported retained wire encoding")
    text = data.decode("utf-8")
    media = response.get("contentType", "").split(";", 1)[0].strip().lower()
    if media == "application/json":
        frames = [strict_json(text)]
    elif media == "text/event-stream":
        frames = [strict_json("\n".join(line[5:].lstrip() for line in event.splitlines() if line.startswith("data:")))
                  for event in re.split(r"\r?\n\r?\n", text)
                  if any(line.startswith("data:") for line in event.splitlines())]
    else:
        raise ValueError("wire response has unrelated media")
    if (len(frames) != 1 or not isinstance(frames[0], dict) or frames[0].get("jsonrpc") != "2.0"
            or not rpc_id(frames[0].get("id")) or type(frames[0]["id"]) is not type(request_body["id"])
            or frames[0]["id"] != request_body["id"] or (("result" in frames[0]) == ("error" in frames[0]))):
        raise ValueError("wire response does not correlate with the actual request")
    return request_body, frames[0]


def observed_wire(wire, method, params, authenticated):
    if (not isinstance(wire, dict) or wire.get("schema") != "honua.mcp-candidate-wire/v1"
            or wire.get("upstream") != "http://127.0.0.1:8080/mcp" or wire.get("method") != method
            or not same_json(wire.get("expectedParams"), params) or wire.get("authenticated") is not authenticated
            or wire.get("invalid") is not False or wire.get("completed") is not True
            or not isinstance(wire.get("expectedCredentialFingerprint"), str)
            or not re.fullmatch(r"scrypt:[a-f0-9]{32}:[a-f0-9]{64}", wire["expectedCredentialFingerprint"])):
        raise ValueError("wire observation is missing or bound to another request/candidate")
    if set(wire) != {"schema", "upstream", "listener", "method", "expectedParams", "authenticated", "expectedCredentialFingerprint", "initialize", "invocation", "invalid", "completed"}:
        raise ValueError("wire observation contains unrelated retained fields")
    listener = urlsplit(wire.get("listener", ""))
    if (listener.scheme != "http" or listener.hostname != "127.0.0.1" or listener.username or listener.password
            or not listener.port or listener.path != "/mcp" or listener.query or listener.fragment):
        raise ValueError("actual client did not use the owned observation listener")
    initial_request, initial_response = wire_frame(wire["initialize"])
    request, response = wire_frame(wire["invocation"])
    if (initial_request.get("method") != "initialize" or initial_request["id"] == request["id"]
            or request.get("method") != method or not isinstance(request.get("params"), dict)
            or any(not same_json(request["params"].get(key), value) for key, value in params.items())
            or set(request["params"]) - set(params) - {"_meta"}
            or ("_meta" in request["params"] and not isinstance(request["params"]["_meta"], dict))
            or not isinstance(initial_request.get("params"), dict)
            or not isinstance(initial_request["params"].get("clientInfo"), dict)
            or any(not isinstance(initial_request["params"]["clientInfo"].get(key), str)
                   or not initial_request["params"]["clientInfo"][key] for key in ("name", "version"))
            or not isinstance(initial_response.get("result"), dict)
            or not isinstance(initial_response["result"].get("protocolVersion"), str)
            or "error" in initial_response):
        raise ValueError("fresh initialization or actual invocation differs")
    fresh = wire["initialize"]["response"].get("sessionSha256")
    if not isinstance(fresh, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", fresh):
        raise ValueError("fresh server session was not observed")
    if (wire["initialize"]["request"].get("sessionSha256") is not None
            or wire["invocation"]["request"].get("sessionSha256") != fresh
            or wire["invocation"]["response"].get("sessionSha256") not in (None, fresh)):
        raise ValueError("actual invocation did not use its newly initialized session")
    for record in (wire["initialize"], wire["invocation"]):
        actual = record["request"]
        if (actual.get("credentialPresent") is not authenticated or actual.get("authorizationPresent") is not False
                or actual.get("cookiePresent") is not False
                or actual.get("credentialFingerprint") != (wire["expectedCredentialFingerprint"] if authenticated else None)):
            raise ValueError("actual request inherited or changed credential authority")
    return response, fresh


def client_observation(client, frame, wire, canonical_client, version):
    if (not isinstance(client, dict) or client.get("canonicalClient") != canonical_client or client.get("version") != version
            or client.get("requestUrl") != wire["listener"] or type(client.get("processId")) is not int or client["processId"] <= 0):
        raise ValueError("actual installed-client invocation diagnostics missing or mismatched")
    if "result" in frame and not isinstance(frame["result"], dict):
        raise ValueError("actual client requires an object resource/tool result")
    if canonical_client == "Official MCP TypeScript SDK":
        if (client.get("kind") != "sdk" or client.get("sessionSha256") != wire["initialize"]["response"]["sessionSha256"]):
            raise ValueError("actual SDK did not expose the independently observed fresh session")
        output = client.get("output")
        if "error" in frame:
            error = output.get("error") if isinstance(output, dict) else None
            if (not isinstance(error, dict) or type(error.get("code")) is not int or error["code"] != frame["error"]["code"]
                    or not same_json(error.get("data"), frame["error"].get("data")) or not isinstance(error.get("message"), str)
                    or frame["error"]["message"] not in error["message"]):
                raise ValueError("SDK server error differs from the independently observed response")
        elif not same_json(output, frame["result"]):
            raise ValueError("SDK result differs from independently observed response")
    elif canonical_client == "MCP Inspector":
        if (client.get("kind") != "inspector-cli" or client.get("exitSignal") is not None or client.get("timedOut") is not False
                or type(client.get("exitCode")) is not int or not isinstance(client.get("stdout"), str)
                or not isinstance(client.get("stderr"), str)):
            raise ValueError("actual Inspector CLI invocation did not terminate normally")
        if "error" in frame:
            # Inspector2.10.1 intentionally replaces the original RPC error
            # data with a CLI diagnostic. The wire remains observer evidence;
            # never pretend this stderr is the original server envelope.
            diagnostic = strict_json(client["stderr"])
            diagnostic = diagnostic.get("error") if isinstance(diagnostic, dict) else None
            expected = ((2, "error"), (3, "auth_required")) if frame["error"].get("data", {}).get("code") == "unauthenticated" else ((2, "error"),)
            if (client["stdout"].strip() or not isinstance(diagnostic, dict)
                    or (client["exitCode"], diagnostic.get("code")) not in expected
                    or not isinstance(diagnostic.get("message"), str) or frame["error"]["message"] not in diagnostic["message"]):
                raise ValueError("Inspector diagnostics do not correlate with the observed server refusal")
        else:
            output = strict_json(client["stdout"])
            output = output.get("result", output) if isinstance(output, dict) else output
            expected = 5 if frame["result"].get("isError") is True else 0
            if (client["exitCode"] != expected
                    or not same_json(output, frame["result"])):
                raise ValueError("Inspector result/outcome differs from observed server response")
    else:
        raise ValueError("map journey requires one of the actual canonical clients")


def scenario(fixture_bytes, bootstrap):
    # Reuse the genuine seed/publish, PostGIS and actual runtime binding. No
    # canned service identifier or newly invented setup path is accepted.
    binding = fixture_binding(fixture_bytes, bootstrap)
    fixture = json.loads(fixture_bytes)
    bounds = [feature["envelope"] for feature in fixture["area"]["features"]]
    extent = [min(b[0] for b in bounds), min(b[1] for b in bounds),
              max(b[2] for b in bounds), max(b[3] for b in bounds)]
    owned = bootstrap["ownedArea"]
    arguments = {
        "templateId": "sdkreg-authored-template", "styleId": "style_sdkreg_authored", "themeId": "theme_sdkreg_authored",
        "sourceBindings": [{"bindingId": "authored-area", "protocol": "geoservices_feature_service",
                            "url": f"http://localhost:8080/rest/services/{owned['serviceId']}/FeatureServer",
                            "serviceId": owned["serviceId"], "layerId": str(owned["layerId"]),
                            "filter": "name = 'block'", "metadata": {"sdkregPurpose": "authored-composition"}}],
        "initialView": {"bbox": extent, "crs": 4326},
    }
    reversed_bbox = copy.deepcopy(arguments)
    reversed_bbox["initialView"]["bbox"][0], reversed_bbox["initialView"]["bbox"][2] = extent[2], extent[0]
    duplicate = copy.deepcopy(arguments)
    duplicate["sourceBindings"].append(copy.deepcopy(duplicate["sourceBindings"][0]))
    return {key: binding[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha", "candidateImage", "runtimeBinding")} | {
        "schema": SCHEMA, "operations": OPERATIONS, "tool": "honua_create_map_package", "cases": [
            {"id": "authored-composition", "arguments": arguments},
            {"id": "invalid-view", "arguments": reversed_bbox},
            {"id": "duplicate-binding", "arguments": duplicate}]}


def json_tool_output(result):
    if not isinstance(result, dict) or result.get("isError", False) is not False:
        raise ValueError("map composition requires a successful tool envelope")
    values = []
    if "structuredContent" in result:
        values.append(result["structuredContent"])
    content = result.get("content", [])
    if not isinstance(content, list):
        raise ValueError("map composition content is not an array")
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text" or not isinstance(item.get("text"), str):
            raise ValueError("map composition contains unrelated media")
        values.append(strict_json(item["text"]))
    if not values or any(not isinstance(value, dict) for value in values):
        raise ValueError("map composition lacks JSON object output")
    if any(not same_json(value, values[0]) for value in values[1:]):
        raise ValueError("map composition representations conflict")
    return values[0]


def created_package(result, arguments):
    output = json_tool_output(result)
    identifier = output.get("packageId")
    if (output.get("status") != "created" or not isinstance(identifier, str)
            or not re.fullmatch(r"map_[A-Za-z0-9_-]{1,128}", identifier)
            or output.get("resourceUri") != "honua://map-packages/" + identifier or output.get("warnings") != []):
        raise ValueError("map creation lacks its addressable created draft identity")
    package = output.get("package")
    source = arguments["sourceBindings"][0]
    expected_binding = {"sourceId": source["bindingId"], "protocol": source["protocol"],
                        "locator": {key: source[key] for key in ("url", "serviceId", "layerId")},
                        "filter": source["filter"], "metadata": source["metadata"]}
    if (not isinstance(package, dict) or package.get("mapPackageId") != identifier
            or package.get("format") != "honua_map_package.v1" or package.get("status") != "Draft"
            or package.get("templateId") != arguments["templateId"] or package.get("themeId") != arguments["themeId"]
            or package.get("styleRefs") != [{"styleId": arguments["styleId"]}]
            or package.get("sourceBindings") != [expected_binding]):
        raise ValueError("created map ignored or changed authored composition selectors")
    view = package.get("initialView", {})
    bbox = view.get("bbox") if isinstance(view, dict) else None
    if (not isinstance(bbox, list) or len(bbox) != 4
            or any(type(number) not in (int, float) or not math.isfinite(number) for number in bbox)
            or bbox != arguments["initialView"]["bbox"] or view.get("crs") != "EPSG:4326"):
        raise ValueError("created map ignored or changed the independent authored extent/CRS")
    created = package.get("createdAt")
    if not isinstance(created, str) or datetime.fromisoformat(created.replace("Z", "+00:00")).utcoffset() is None:
        raise ValueError("created map lacks an aware creation timestamp")
    return output


def resource_output(result, uri, identifier):
    if not isinstance(result, dict) or "error" in result or not isinstance(result.get("contents"), list) or len(result["contents"]) != 1:
        raise ValueError("fresh package read did not return one resource")
    item = result["contents"][0]
    if (not isinstance(item, dict) or item.get("uri") != uri or item.get("mimeType") != "application/json"
            or not isinstance(item.get("text"), str) or "blob" in item):
        raise ValueError("fresh package read lacks bound JSON resource media")
    value = strict_json(item["text"])
    if (not isinstance(value, dict) or value.get("packageKind") != "map_package" or value.get("packageId") != identifier
            or value.get("resourceUri") != uri or value.get("packageStatus") != "draft"
            or type(value.get("deploymentCount")) is not int or value["deploymentCount"] != 0
            or value.get("deploymentResourceUris") != [] or not isinstance(value.get("provenance"), dict)):
        raise ValueError("fresh resource read did not resolve the created scratch draft")


def resource_refusal(result, code, semantic_code):
    error = result.get("error") if isinstance(result, dict) else None
    if (not isinstance(error, dict) or type(error.get("code")) is not int or error["code"] != code
            or not isinstance(error.get("message"), str) or not error["message"].strip()
            or not isinstance(error.get("data"), dict) or error["data"].get("code") != semantic_code
            or "result" in result or "contents" in result
            or (semantic_code == "unauthenticated" and error["data"].get("requiresReauthentication") is not True)):
        raise ValueError("resource refusal lacks the typed server error and no-data contract")
    permitted_data = {"code", "correlationId"} | ({"requiresReauthentication"} if semantic_code == "unauthenticated" else set())
    if (set(result) - {"error", "jsonrpc", "id"} or set(error) != {"code", "message", "data"}
            or any(value is not None for key, value in error["data"].items() if key not in permitted_data)
            or (error["data"].get("correlationId") is not None and not isinstance(error["data"]["correlationId"], str))
            or ("jsonrpc" in result and result["jsonrpc"] != "2.0")):
        raise ValueError("resource refusal contains unrelated response data")
    identifier = result.get("id")
    if (identifier is not None and not (isinstance(identifier, str) and 0 < len(identifier) <= 256)
            and not (type(identifier) is int and abs(identifier) <= 9007199254740991)):
        raise ValueError("resource refusal has an invalid JSON-RPC identifier")


def judge(fixture_bytes, bootstrap, observations):
    authored = scenario(fixture_bytes, bootstrap)
    if not isinstance(observations, dict) or set(observations) != set(authored) | {"runtimeBefore", "runtimeAfter", "reads", "canonicalClient", "clientVersion"}:
        raise ValueError("map observations lack exact authored fields, actual runtime or fresh reads")
    canonical, version = observations["canonicalClient"], observations["clientVersion"]
    if canonical not in ("Official MCP TypeScript SDK", "MCP Inspector") or not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("map observations lack the named/versioned installed client")
    if any(observations[key] != value for key, value in authored.items() if key != "cases"):
        raise ValueError("map observations differ from the independently bound scenario")
    if any(observations[key] != authored["runtimeBinding"] for key in ("runtimeBefore", "runtimeAfter")):
        raise ValueError("candidate changed around actual map client calls")
    actual_cases = observations["cases"]
    if not isinstance(actual_cases, list) or len(actual_cases) != 3:
        raise ValueError("map composition is missing authored positive/negative cases")
    created, sessions, credentials = None, set(), set()
    for expected, actual in zip(authored["cases"], actual_cases, strict=True):
        if (not isinstance(actual, dict) or set(actual) != {"id", "arguments", "result", "wire", "client"}
                or any(not same_json(actual[key], expected[key]) for key in ("id", "arguments"))):
            raise ValueError("map request differs from the independent authored case")
        frame, fresh = observed_wire(actual["wire"], "tools/call", {"name": authored["tool"], "arguments": expected["arguments"]}, True)
        if "error" in frame or not same_json(actual["result"], frame["result"]) or fresh in sessions:
            raise ValueError("map tool response or fresh client session differs from wire")
        sessions.add(fresh); credentials.add(actual["wire"]["expectedCredentialFingerprint"])
        client_observation(actual["client"], frame, actual["wire"], canonical, version)
        if expected["id"] == "authored-composition":
            created = created_package(actual["result"], expected["arguments"])
        else:
            negative_output(actual["result"], "invalid_argument")
    uri, identifier = created["resourceUri"], created["packageId"]
    reads = observations["reads"]
    expected_reads = [("fresh-created", uri, True), ("unknown-package", uri + "-uncreated", True), ("anonymous-created", uri, False)]
    if not isinstance(reads, list) or len(reads) != len(expected_reads):
        raise ValueError("map package lacks fresh read, unknown and anonymous controls")
    for actual, (case, expected_uri, authenticated) in zip(reads, expected_reads, strict=True):
        if (not isinstance(actual, dict) or set(actual) != {"id", "uri", "authenticated", "result", "wire", "client"}
                or actual["id"] != case or actual["uri"] != expected_uri or actual["authenticated"] is not authenticated):
            raise ValueError("fresh map read differs from the authored resource scenario")
        frame, fresh = observed_wire(actual["wire"], "resources/read", {"uri": expected_uri}, authenticated)
        if fresh in sessions or not same_json(actual["result"], (frame if "error" in frame else frame["result"])):
            raise ValueError("resource response was not read in its distinct fresh session")
        sessions.add(fresh); credentials.add(actual["wire"]["expectedCredentialFingerprint"])
        client_observation(actual["client"], frame, actual["wire"], canonical, version)
        if case == "fresh-created":
            resource_output(actual["result"], uri, identifier)
        else:
            resource_refusal(actual["result"], -32002 if authenticated else -32000, "not_found" if authenticated else "unauthenticated")
    if len(credentials) != 1:
        raise ValueError("actual probes changed the expected creator credential")
    return {key: authored[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha", "candidateImage")} | {
        "positive": True, "negative": True, "mediaSchema": True, "freshReadback": True,
        "scratchOnly": True, "operations": OPERATIONS, "canonicalClient": canonical, "clientVersion": version}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--observations", type=Path)
    args = parser.parse_args()
    try:
        fixture, bootstrap = args.fixture.read_bytes(), json.loads(args.bootstrap.read_bytes())
        output = judge(fixture, bootstrap, strict_json(args.observations.read_bytes())) if args.observations else scenario(fixture, bootstrap)
        print(json.dumps(output, sort_keys=True, allow_nan=False))
    except (KeyError, TypeError, ValueError, OSError) as exc:
        parser.exit(1, f"map package validation refused: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
