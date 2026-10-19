"""Authored map composition and fresh resource-read semantics; no client runtime.

Drafts are bounded, per-replica scratch. These oracles do not claim durable
deployment, cross-replica visibility, style resolution or model inference.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import json
import math
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_render_validation import scenario as fixture_binding, negative_output

SCHEMA = "honua.mcp-map-package-scenario/v1"
OPERATIONS = ["tools/call:create_map_package", "resources/read:Map package"]


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
        values.append(json.loads(item["text"]))
    if not values or any(not isinstance(value, dict) for value in values):
        raise ValueError("map composition lacks JSON object output")
    canonical = [json.dumps(value, sort_keys=True, allow_nan=False) for value in values]
    if any(value != canonical[0] for value in canonical[1:]):
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
    value = json.loads(item["text"])
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


def judge(fixture_bytes, bootstrap, observations):
    authored = scenario(fixture_bytes, bootstrap)
    if not isinstance(observations, dict) or set(observations) != set(authored) | {"runtimeBefore", "runtimeAfter", "reads"}:
        raise ValueError("map observations lack exact authored fields, actual runtime or fresh reads")
    if any(observations[key] != value for key, value in authored.items() if key != "cases"):
        raise ValueError("map observations differ from the independently bound scenario")
    if any(observations[key] != authored["runtimeBinding"] for key in ("runtimeBefore", "runtimeAfter")):
        raise ValueError("candidate changed around actual map client calls")
    actual_cases = observations["cases"]
    if not isinstance(actual_cases, list) or len(actual_cases) != 3:
        raise ValueError("map composition is missing authored positive/negative cases")
    created = None
    for expected, actual in zip(authored["cases"], actual_cases, strict=True):
        if (not isinstance(actual, dict) or set(actual) != {"id", "arguments", "result"}
                or any(actual[key] != expected[key] for key in ("id", "arguments"))):
            raise ValueError("map request differs from the independent authored case")
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
        if (not isinstance(actual, dict) or set(actual) != {"id", "uri", "authenticated", "result"}
                or actual["id"] != case or actual["uri"] != expected_uri or actual["authenticated"] is not authenticated):
            raise ValueError("fresh map read differs from the authored resource scenario")
        if case == "fresh-created":
            resource_output(actual["result"], uri, identifier)
        else:
            resource_refusal(actual["result"], -32002 if authenticated else -32000, "not_found" if authenticated else "unauthenticated")
    return {key: authored[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha", "candidateImage")} | {
        "positive": True, "negative": True, "mediaSchema": True, "freshReadback": True,
        "scratchOnly": True, "operations": OPERATIONS}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--observations", type=Path)
    args = parser.parse_args()
    try:
        fixture, bootstrap = args.fixture.read_bytes(), json.loads(args.bootstrap.read_bytes())
        output = judge(fixture, bootstrap, json.loads(args.observations.read_bytes())) if args.observations else scenario(fixture, bootstrap)
        print(json.dumps(output, sort_keys=True, allow_nan=False))
    except (KeyError, TypeError, ValueError, OSError) as exc:
        parser.exit(1, f"map package validation refused: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
