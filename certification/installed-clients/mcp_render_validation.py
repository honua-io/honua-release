"""Release-owned authored render requests and retained-response judgment.

The PNG oracle derives pixels from the fixture, never from a candidate image.
This CLI has no network or database access.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import re
import sys

# Python isolated mode omits this directory. The caller verifies every imported
# source file before starting this interpreter.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from render_png import oracle_map_render

SCHEMA = "honua.mcp-render-scenario/v1"
BOOTSTRAP = "honua.mcp-render-bootstrap/v1"


def scenario(fixture_bytes, bootstrap):
    fixture = json.loads(fixture_bytes)
    fixture_sha = "sha256:" + hashlib.sha256(fixture_bytes).hexdigest()
    if (not isinstance(bootstrap, dict) or bootstrap.get("schema") != BOOTSTRAP
            or bootstrap.get("status") != "provisioned"
            or bootstrap.get("fixtureSha256") != fixture_sha
            or not isinstance(bootstrap.get("candidateSourceSha"), str)
            or not re.fullmatch(r"[a-f0-9]{40}", bootstrap["candidateSourceSha"])
            or not isinstance(bootstrap.get("candidateImage"), str)
            or not re.fullmatch(r"[^\s]+@sha256:[a-f0-9]{64}", bootstrap["candidateImage"])
            or bootstrap.get("phase") != "complete"
            or not isinstance(bootstrap.get("seedSqlSha256"), str)
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", bootstrap["seedSqlSha256"])):
        raise ValueError("render requires the independently provisioned exact candidate fixture")
    runtime = bootstrap.get("runtimeBinding", {})
    if (not isinstance(runtime, dict) or runtime.get("schema") != "honua.mcp-render-runtime/v1"
            or runtime.get("running") is not True or runtime.get("candidateSourceSha") != bootstrap["candidateSourceSha"]
            or runtime.get("candidateImage") != bootstrap["candidateImage"] or runtime.get("imageReference") != bootstrap["candidateImage"]
            or not isinstance(runtime.get("imageId"), str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", runtime["imageId"])
            or not isinstance(runtime.get("containerId"), str) or not re.fullmatch(r"[a-f0-9]{64}", runtime["containerId"])
            or not isinstance(runtime.get("startedAt"), str) or not runtime["startedAt"]):
        raise ValueError("render fixture lacks the started candidate runtime identity")
    area = bootstrap.get("published", {}).get("area", {})
    if (area.get("service") != fixture["area"]["service"] or type(area.get("layerId")) is not int
            or not 0 <= area["layerId"] < 2147483647):
        raise ValueError("render lacks the allocated owned area layer")
    owned = bootstrap.get("ownedArea", {})
    if (not isinstance(owned, dict) or owned.get("serviceName") != fixture["area"]["service"]
            or not isinstance(owned.get("serviceId"), str) or not owned["serviceId"]
            or type(owned.get("layerId")) is not int or not 0 <= owned["layerId"] < 2147483647
            or owned.get("storageLayerId") != area["layerId"] or owned.get("locator") != "honua_data.sdkreg_area"
            or not isinstance(bootstrap.get("connectionId"), str) or not bootstrap["connectionId"]
            or owned.get("connectionId") != bootstrap["connectionId"]):
        raise ValueError("render lacks independently bound canonical service/publication identity")
    expected_rows = [{"gid": row["gid"], "name": row["name"], "envelope": row["envelope"],
                      "srid": 4326, "geometryType": "ST_Polygon"} for row in fixture["area"]["features"]]
    if bootstrap.get("areaReadback") != sorted(expected_rows, key=lambda row: row["gid"]):
        raise ValueError("render fixture database readback differs from authored geometry")
    render = fixture["mcp"]["render"]
    arguments = {"layers": [{"serviceId": owned["serviceId"], "layerId": owned["layerId"]}],
                 "bbox": render["bbox"], "bboxSrid": 4326, "width": render["width"],
                 "height": render["height"], "transparent": True, "maxInlineBytes": 1048576}
    invalid = copy.deepcopy(arguments)
    invalid["bbox"][0], invalid["bbox"][2] = invalid["bbox"][2], invalid["bbox"][0]
    missing = copy.deepcopy(arguments)
    missing["layers"][0]["layerId"] = 2147483647
    return {"schema": SCHEMA, "fixtureSha256": fixture_sha,
            "bootstrapSha256": "sha256:" + hashlib.sha256(json.dumps(bootstrap, sort_keys=True, allow_nan=False).encode()).hexdigest(),
            "candidateSourceSha": bootstrap["candidateSourceSha"], "candidateImage": bootstrap["candidateImage"],
            "operation": "tools/call:render_map", "tool": "honua_render_map", "cases": [
                {"id": "authored-polygon", "arguments": arguments},
                {"id": "invalid-bbox", "arguments": invalid},
                {"id": "unknown-owned-layer", "arguments": missing}]}


def positive_output(result, arguments):
    if not isinstance(result, dict) or result.get("isError", False) is not False:
        raise ValueError("render did not return a successful tool envelope")
    value = result.get("structuredContent")
    if not isinstance(value, dict):
        raise ValueError("render lacks structured output")
    image = value.get("image")
    if not isinstance(image, dict):
        raise ValueError("render lacks inline image metadata")
    content = result.get("content")
    if not isinstance(content, list):
        raise ValueError("render lacks actual image content")
    images = []
    for item in content:
        if not isinstance(item, dict):
            raise ValueError("render content has invalid schema")
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            # Captions are descriptive prose. If a JSON representation is
            # supplied instead, it must agree with structuredContent.
            if item["text"].lstrip().startswith(("{", "[")) and json.loads(item["text"]) != value:
                raise ValueError("render output representations conflict")
        elif item.get("type") == "image":
            images.append(item)
        else:
            raise ValueError("render contains unrelated output media")
    if len(images) != 1 or images[0].get("data") != image.get("base64"):
        raise ValueError("render image representations conflict or are missing")
    if (image.get("inlined") is not True or image.get("uri") is not None
            or image.get("format") != "image/png" or value.get("format") != "image/png"
            or images[0].get("mimeType") != "image/png"
            or any(type(value.get(key)) is not int or value[key] != arguments[key] for key in ("width", "height", "bboxSrid"))
            or any(type(image.get(key)) is not int or image[key] != arguments[key] for key in ("width", "height"))
            or value.get("bbox") != arguments["bbox"]
            or type(image.get("byteLength")) is not int or type(value.get("byteLength")) is not int
            or image["byteLength"] != value["byteLength"]
            or not isinstance(value.get("layers"), list) or len(value["layers"]) != 1):
        raise ValueError("render image metadata differs from the authored request")
    layer = value["layers"][0]
    if not isinstance(layer, dict) or any(layer.get(key) != expected for key, expected in arguments["layers"][0].items()):
        raise ValueError("render returned another layer")
    import base64
    encoded = image.get("base64")
    if not isinstance(encoded, str) or len(encoded) > 4 * ((arguments["maxInlineBytes"] + 2) // 3):
        raise ValueError("render encoded image exceeds the authored inline byte bound")
    data = base64.b64decode(encoded, validate=True)
    if not data or len(data) != image["byteLength"] or len(data) > arguments["maxInlineBytes"]:
        raise ValueError("render byte length differs from actual image")
    return {"png": image["base64"], "mimeType": images[0]["mimeType"]}


def negative_output(result, code):
    if not isinstance(result, dict) or result.get("isError") is not True:
        raise ValueError("render negative was not refused by a typed tool error")
    outputs = []
    if "structuredContent" in result:
        outputs.append(result["structuredContent"])
    content = result.get("content", [])
    if not isinstance(content, list):
        raise ValueError("render error content has invalid schema")
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "text" or not isinstance(item.get("text"), str):
            raise ValueError("render error contains unrelated media")
        outputs.append(json.loads(item["text"]))
    if not outputs or any(not isinstance(output, dict) or output.get("status") != "error"
                          or output.get("code") != code or not isinstance(output.get("message"), str)
                          or not output["message"].strip() for output in outputs):
        raise ValueError("render negative returned the wrong typed error")
    if any(output != outputs[0] for output in outputs[1:]):
        raise ValueError("render error representations conflict")


def judge(fixture_bytes, bootstrap, observations):
    authored = scenario(fixture_bytes, bootstrap)
    if not isinstance(observations, dict) or set(observations) != set(authored):
        raise ValueError("render observations lack the complete bound scenario")
    if any(observations[key] != value for key, value in authored.items() if key != "cases"):
        raise ValueError("render observations differ from authored fixture/candidate/bootstrap")
    if not isinstance(observations["cases"], list) or len(observations["cases"]) != 3:
        raise ValueError("render observations are incomplete or duplicated")
    for expected, actual in zip(authored["cases"], observations["cases"], strict=True):
        if (not isinstance(actual, dict) or set(actual) != {"id", "arguments", "result"}
                or actual["id"] != expected["id"] or actual["arguments"] != expected["arguments"]):
            raise ValueError("render observed request differs from the independent authored case")
        if expected["id"] == "authored-polygon":
            observed = positive_output(actual["result"], expected["arguments"])
            passed, reason = oracle_map_render(observed, json.loads(fixture_bytes))
            if not passed:
                raise ValueError(reason)
        else:
            negative_output(actual["result"], "invalid_argument" if expected["id"] == "invalid-bbox" else "not_found")
    return {key: authored[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha", "candidateImage")} | {
        "positive": True, "negative": True, "mediaSchema": True}


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
        parser.exit(1, f"render validation refused: {type(exc).__name__}\n")


if __name__ == "__main__":
    main()
