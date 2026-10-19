"""Authored canonical style semantics; no network, SQL or client transport."""
from __future__ import annotations
import argparse
import base64
import copy
from datetime import datetime
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_render_validation import scenario as fixture_binding, positive_output, negative_output
from mcp_map_package_validation import strict_json, same_json, client_observation, anonymous_tool_refusal
from mcp_ingest_publish_validation import invocation_wire
from render_png import decode_png, render_expectations

SCHEMA = "honua.mcp-canonical-style-scenario/v1"
OPERATIONS = ["tools/call:get_style", "tools/call:apply_style_preset"]
ENCODINGS = {"mapbox-style": "application/vnd.mapbox.style+json", "esri-drawing-info": "application/vnd.esri.drawinginfo+json",
             "sld-1.0.0": "application/vnd.ogc.sld+xml;version=1.0", "sld-1.1.0": "application/vnd.ogc.sld+xml;version=1.1"}


def scenario(fixture_bytes, bootstrap, nonce, canonical_client):
    binding = fixture_binding(fixture_bytes, bootstrap)
    if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{12}", nonce):
        raise ValueError("style requires a bounded owned run identity")
    if canonical_client not in ("Official MCP TypeScript SDK", "MCP Inspector"):
        raise ValueError("style requires an actual canonical client")
    slug = "sdk" if canonical_client == "Official MCP TypeScript SDK" else "inspector"
    styles = []
    for suffix, color in (("a", "#1d7bc9"), ("b", "#d34729")):
        identifier = f"mcp-style-{slug}-{nonce}-{suffix}"
        styles.append({"id": identifier, "color": color, "document": {"version": 8, "name": identifier,
            "sources": {"authored": {"type": "geojson", "data": {"type": "FeatureCollection", "features": []}}},
            "layers": [{"id": "authored-fill", "type": "fill", "source": "authored",
                        "paint": {"fill-color": color, "fill-opacity": 1, "fill-antialias": False}}]}})
    area = bootstrap["ownedArea"]
    address = {"serviceId": area["serviceId"], "layerId": area["layerId"]}
    return {key: binding[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha", "candidateImage", "runtimeBinding")} | {
        "schema": SCHEMA, "nonce": nonce, "canonicalClient": canonical_client, "operations": OPERATIONS,
        "styles": styles, "ownedArea": area, "address": address,
        "renderArguments": binding["cases"][0]["arguments"]}


def tool_output(result):
    if (not isinstance(result, dict) or result.get("isError", False) is not False
            or not isinstance(result.get("structuredContent"), dict)
            or not isinstance(result.get("content"), list) or len(result["content"]) != 1):
        raise ValueError("style lacks successful structured output and text")
    value = result["structuredContent"]
    text = result["content"][0]
    if (not isinstance(text, dict) or set(text) != {"type", "text"} or text["type"] != "text"
            or not isinstance(text["text"], str) or not 0 < len(text["text"]) <= 2 * 1024 * 1024):
        raise ValueError("style contains unrelated or missing media")
    if text["text"].lstrip().startswith(("{", "[")) and not same_json(strict_json(text["text"]), value):
        raise ValueError("style JSON representations conflict")
    return value


def setup_output(setup, authored):
    if not isinstance(setup, list) or len(setup) != 2:
        raise ValueError("style creation setup missing")
    for actual, expected in zip(setup, authored["styles"], strict=True):
        if (not isinstance(actual, dict) or set(actual) != {"id", "method", "path", "document", "status", "location", "body"}
                or actual["id"] != expected["id"] or actual["method"] != "POST" or actual["path"] != "/ogc/styles"
                or not same_json(actual["document"], expected["document"]) or type(actual["status"]) is not int or actual["status"] != 201
                or actual["location"] not in ("http://localhost:8080/ogc/styles/" + expected["id"], "http://127.0.0.1:8080/ogc/styles/" + expected["id"])
                or not isinstance(actual["body"], dict) or actual["body"].get("id") != expected["id"]):
            raise ValueError("style setup did not create the exact authored catalog entry")


def get_output(result, style, *, inline=True):
    value = tool_output(result)
    if (value.get("styleId") != style["id"] or type(value.get("styleVersion")) is not int or value["styleVersion"] != 1
            or value.get("title") != style["id"] or value.get("description") is not None):
        raise ValueError("get_style selected another catalog style/version")
    encodings = value.get("encodings")
    if not isinstance(encodings, list) or len(encodings) != len(ENCODINGS):
        raise ValueError("get_style encodings missing")
    seen = set()
    for entry in encodings:
        encoding = entry.get("encoding") if isinstance(entry, dict) else None
        if encoding not in ENCODINGS or encoding in seen or entry.get("mediaType") != ENCODINGS[encoding]:
            raise ValueError("get_style encoding identity/media differs")
        seen.add(encoding)
        if encoding == "mapbox-style" and inline:
            if (not isinstance(entry.get("inlineBody"), str) or not same_json(strict_json(entry["inlineBody"]), style["document"])
                    or entry.get("storageRef") is not None):
                raise ValueError("get_style inlined a different authored stylesheet")
        elif entry.get("inlineBody") is not None or entry.get("storageRef") != "honua://styles/" + style["id"]:
            raise ValueError("get_style encoding reference differs")


def apply_output(result, authored, *, dry_run):
    value = tool_output(result)
    if (value.get("status") != "Completed" or value.get("operationId") != "style.apply-preset"
            or any(not isinstance(value.get(key), str) or not value[key] for key in ("operationInstanceId", "handleId", "correlationId"))
            or value["handleId"] != value["operationInstanceId"]
            or value.get("serviceId") != authored["address"]["serviceId"]
            or type(value.get("layerId")) is not int or value["layerId"] != authored["address"]["layerId"]
            or value.get("styleId") != authored["styles"][1 if dry_run else 0]["id"]
            or value.get("title") != authored["styles"][1 if dry_run else 0]["id"]
            or type(value.get("styleVersion")) is not int or value["styleVersion"] != 1
            or value.get("dryRun") is not dry_run or value.get("applied") is not (not dry_run)
            or value.get("warning") is not None
            or ("durable" in value and type(value["durable"]) is not bool)):
        raise ValueError("style application is pending, inconsistent or not the authored operation")
    # This field describes operation-instance persistence, not catalog/binding
    # persistence. Redis-off direct style is supported with durable=false; only
    # independent SQL/fresh reads/restart can prove the association persisted.
    return value


def snapshot_output(value, authored, fixture_bytes, *, applied):
    if (not isinstance(value, dict) or set(value) != {"schema", "styles", "refs", "mappings", "rows"}
            or value["schema"] != "honua.mcp-style-persisted/v1"):
        raise ValueError("independent style state missing")
    catalog = value["styles"]
    if not isinstance(catalog, list):
        raise ValueError("persisted style catalog missing")
    catalog_by_id = {}
    for row in catalog:
        if (not isinstance(row, dict) or set(row) != {"styleId", "title", "description", "document", "drawingInfo", "version", "createdAt", "updatedAt", "revisedBy", "changeSummary"}
                or not isinstance(row["styleId"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", row["styleId"])
                or row["styleId"] in catalog_by_id or not isinstance(row["document"], dict)
                or type(row["version"]) is not int or row["version"] < 1):
            raise ValueError("persisted style catalog has duplicate/invalid entries")
        if any(row[key] is not None and not isinstance(row[key], str) for key in ("title", "description", "revisedBy", "changeSummary")):
            raise ValueError("persisted catalog metadata value types differ")
        if row["drawingInfo"] is not None and not isinstance(row["drawingInfo"], dict):
            raise ValueError("persisted catalog drawing metadata malformed")
        for key in ("createdAt", "updatedAt"):
            if not isinstance(row[key], str) or datetime.fromisoformat(row[key].replace("Z", "+00:00")).tzinfo is None:
                raise ValueError("persisted catalog timestamp missing or timezone-free")
        catalog_by_id[row["styleId"]] = row
    for style in authored["styles"]:
        actual = catalog_by_id.get(style["id"])
        expected = {"styleId": style["id"], "title": style["id"], "description": None, "document": style["document"],
                    "drawingInfo": None, "version": 1, "revisedBy": None, "changeSummary": None}
        if actual is None or not same_json({key: actual[key] for key in expected}, expected):
            raise ValueError("persisted styles differ from authored catalog")
    expected_rows = [{"gid": row["gid"], "name": row["name"], "envelope": row["envelope"], "srid": 4326}
                     for row in json.loads(fixture_bytes)["area"]["features"]]
    rows = value["rows"]
    if not isinstance(rows, list) or len(rows) != len(expected_rows):
        raise ValueError("style changed authored feature row count")
    for actual, expected in zip(rows, sorted(expected_rows, key=lambda row: row["gid"]), strict=True):
        if (not isinstance(actual, dict) or set(actual) != set(expected) | {"geometry"}
                or not same_json({key: actual[key] for key in expected}, expected)):
            raise ValueError("style changed authored feature rows or bounds")
        geometry = actual["geometry"]
        if (not isinstance(geometry, dict) or set(geometry) != {"type", "coordinates"}
                or geometry["type"] != "Polygon" or not isinstance(geometry["coordinates"], list)
                or len(geometry["coordinates"]) != 1):
            raise ValueError("style changed authored polygon type/rings")
        # Independent authored rectangle, allowing only ring start/orientation
        # differences. No tolerance, additional vertices, holes or same-bbox
        # distortion is accepted. Raw full coordinates remain in state census.
        left, bottom, right, top = expected["envelope"]
        corners = [[left, bottom], [left, top], [right, top], [right, bottom]]
        alternatives = []
        for direction in (corners, list(reversed(corners))):
            for start in range(4):
                ring = direction[start:] + direction[:start]
                alternatives.append(ring + [ring[0]])
        if not any(same_json(geometry["coordinates"][0], ring) for ring in alternatives):
            raise ValueError("style changed the full authored rectangle geometry")
    mappings = value["mappings"]
    if not isinstance(mappings, list) or len(mappings) != 1 or not isinstance(mappings[0], dict):
        raise ValueError("style public/storage mapping is absent or ambiguous")
    mapping = mappings[0]
    area = authored["ownedArea"]
    if (set(mapping) != {"revision", "serviceId", "layerId", "storageLayerId", "locator", "connectionId", "styleResourceIds", "styleResources"}
            or type(mapping["revision"]) is not int or mapping["revision"] <= 0
            or any(not same_json(mapping[key], area[key]) for key in ("serviceId", "layerId", "storageLayerId", "locator", "connectionId"))):
        raise ValueError("style state substituted another public/storage/connection identity")
    refs = value["refs"]
    if not isinstance(refs, list):
        raise ValueError("persisted style associations missing")
    ids, ordinals = set(), set()
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) != {"styleId", "ordinal"} or not isinstance(ref["styleId"], str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", ref["styleId"]) or ref["styleId"] in ids
                or type(ref["ordinal"]) is not int or ref["ordinal"] < 0 or (ref["ordinal"] == 0 and 0 in ordinals)):
            raise ValueError("duplicate or invalid persisted style association")
        ids.add(ref["styleId"]); ordinals.add(ref["ordinal"])
    if [ref["ordinal"] for ref in refs] != sorted(ref["ordinal"] for ref in refs):
        raise ValueError("style association ordinal order changed")
    if any(ref["styleId"] == authored["styles"][1]["id"] for ref in refs):
        raise ValueError("dry-run alternate style was persisted")
    if applied and (not refs or refs[0] != {"styleId": authored["styles"][0]["id"], "ordinal": 0}):
        raise ValueError("authored applied style is not the sole primary")
    if not ids.issubset(catalog_by_id):
        raise ValueError("catalog census omits an associated style")
    # ASCII identifiers here use the exact MetadataV2StyleResourceFactory rule:
    # preserve letters/digits/underscore/dot/hyphen, then trim outer hyphens.
    expected_ids = ["style-" + (ref["styleId"].strip("-") or "default") for ref in refs]
    graph_ids = mapping["styleResourceIds"]
    if (len(set(expected_ids)) != len(expected_ids) or not isinstance(graph_ids, list)
            or any(not isinstance(identifier, str) for identifier in graph_ids)
            or len(graph_ids) != len(expected_ids) or len(set(graph_ids)) != len(graph_ids)
            or set(graph_ids) != set(expected_ids)):
        raise ValueError("active metadata graph disagrees with catalog associations")
    ordinal_by_id = {identifier: ref["ordinal"] for ref, identifier in zip(refs, expected_ids, strict=True)}
    graph_ordinals = [ordinal_by_id[identifier] for identifier in graph_ids]
    if graph_ordinals != sorted(graph_ordinals):
        raise ValueError("active graph style ordinal order differs from associations")
    resources = mapping["styleResources"]
    if (not isinstance(resources, list) or len(resources) != len(expected_ids)
            or any(not isinstance(row, dict) or set(row) != {"id", "name", "style"} for row in resources)
            or len({row["id"] for row in resources if isinstance(row["id"], str)}) != len(resources)
            or {row["id"] for row in resources if isinstance(row["id"], str)} != set(expected_ids)):
        raise ValueError("graph must retain exactly one resource for every referenced style")
    by_id = {row["id"]: row for row in resources}
    for ref, identifier in zip(refs, expected_ids, strict=True):
        resource = by_id[identifier]; stored = catalog_by_id[ref["styleId"]]
        style = resource["style"]
        if (resource["name"] != ref["styleId"] or not isinstance(style, dict)
                or type(style.get("styleVersion")) is not int or style["styleVersion"] != stored["version"]
                or not isinstance(style.get("encodings"), list)):
            raise ValueError("referenced graph style identity/version differs from the real catalog")
        encoding = [row for row in style["encodings"] if isinstance(row, dict) and row.get("encoding") == "mapbox-style"]
        if (len(encoding) != 1 or encoding[0].get("contentType") != ENCODINGS["mapbox-style"]
                or not isinstance(encoding[0].get("body"), str)
                or not same_json(strict_json(encoding[0]["body"]), stored["document"])):
            raise ValueError("referenced graph stylesheet differs from the real catalog")


def canonical_state(value):
    # Validate raw multiplicity, identities and primary/ordinal semantics first.
    # Catalog order is not semantic. Production locale-orders secondary ties;
    # normalize only within the same ordinal, never across precedence levels.
    result = copy.deepcopy(value)
    result["styles"].sort(key=lambda row: row["styleId"])
    result["refs"].sort(key=lambda row: (row["ordinal"], row["styleId"]))
    order = {"style-" + (row["styleId"].strip("-") or "default"): (row["ordinal"], row["styleId"]) for row in result["refs"]}
    for mapping in result["mappings"]:
        mapping["styleResourceIds"].sort(key=order.__getitem__)
        mapping["styleResources"].sort(key=lambda row: row["id"])
    return result


def stable_state(value):
    copy_value = canonical_state(value)
    for mapping in copy_value["mappings"]:
        mapping.pop("revision", None)
    return copy_value


def rendered_output(result, authored, fixture_bytes):
    output = positive_output(result, authored["renderArguments"])
    layer = result["structuredContent"]["layers"][0]
    if layer.get("styleId") != authored["styles"][0]["id"] or layer.get("styleRendering") != "applied" or layer.get("unsupportedStyleConstructs") not in (None, []):
        raise ValueError("render did not apply the authored catalog style")
    width, height, pixel = decode_png(base64.b64decode(output["png"], validate=True))
    args = authored["renderArguments"]
    if (width, height) != (args["width"], args["height"]):
        raise ValueError("style PNG dimensions differ")
    color = authored["styles"][0]["color"]
    expected = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5)) + (255,)
    samples = render_expectations(json.loads(fixture_bytes))
    if not any(inside for _, _, inside in samples) or not any(not inside for _, _, inside in samples):
        raise ValueError("style geometry fixture lacks interior/exterior discrimination")
    if any((pixel(x, y) != expected if inside else pixel(x, y)[3] != 0) for x, y, inside in samples):
        raise ValueError("style PNG color/geometry differs from independent authored expectations")


def judge(fixture_bytes, bootstrap, observed):
    if not isinstance(observed, dict):
        raise ValueError("style observations missing")
    authored = scenario(fixture_bytes, bootstrap, observed.get("nonce"), observed.get("canonicalClient"))
    keys = {"schema", "nonce", "canonicalClient", "clientVersion", "candidateSourceSha", "candidateImage", "setup", "calls", "snapshots", "runtimeBefore", "runtimeRestarted", "runtimeAfter"}
    if set(observed) != keys or observed["schema"] != SCHEMA or any(observed[key] != authored[key] for key in ("candidateSourceSha", "candidateImage")):
        raise ValueError("style observations belong to another scenario/candidate")
    before, restarted, after = (observed[key] for key in ("runtimeBefore", "runtimeRestarted", "runtimeAfter"))
    for runtime in (before, restarted, after):
        if not isinstance(runtime, dict):
            raise ValueError("style runtime observation missing")
        compare = {**runtime, "startedAt": authored["runtimeBinding"]["startedAt"]}
        if not same_json(compare, authored["runtimeBinding"]):
            raise ValueError("style candidate runtime identity changed")
    if (not same_json(restarted, after) or datetime.fromisoformat(restarted["startedAt"].replace("Z", "+00:00"))
            <= datetime.fromisoformat(before["startedAt"].replace("Z", "+00:00"))):
        raise ValueError("style restart did not preserve the owned candidate and advance start time")
    setup_output(observed["setup"], authored)
    a, b = authored["styles"]
    address = authored["address"]
    wanted = [("catalog", "honua_get_style", {}, True),
        ("direct", "honua_get_style", {"styleId": a["id"], "includeStylesheet": True}, True),
        ("dry-run", "honua_apply_style_preset", {**address, "styleId": b["id"], "dryRun": True}, True),
        ("apply", "honua_apply_style_preset", {**address, "styleId": a["id"]}, True),
        ("fresh", "honua_get_style", {**address, "includeStylesheet": True}, True),
        ("render", "honua_render_map", authored["renderArguments"], True),
        ("replay", "honua_apply_style_preset", {**address, "styleId": a["id"]}, True),
        ("unknown-style", "honua_apply_style_preset", {**address, "styleId": a["id"] + "-unknown"}, True),
        ("invalid-encoding", "honua_get_style", {"styleId": a["id"], "encoding": "unknown"}, True),
        ("unknown-layer", "honua_apply_style_preset", {**address, "layerId": 2147483647, "styleId": a["id"]}, True),
        ("anonymous-get", "honua_get_style", {"styleId": a["id"], "includeStylesheet": True}, False),
        ("anonymous-apply", "honua_apply_style_preset", {**address, "styleId": b["id"]}, False),
        ("restarted-fresh", "honua_get_style", {**address, "includeStylesheet": True}, True),
        ("restarted-render", "honua_render_map", authored["renderArguments"], True)]
    calls = observed["calls"]
    if not isinstance(calls, list) or len(calls) != len(wanted):
        raise ValueError("style positive/refusal/restart calls incomplete")
    sessions, fingerprints = set(), set()
    outcomes = {}
    for entry, (identifier, name, args, authenticated) in zip(calls, wanted, strict=True):
        params = {"name": name, "arguments": args}
        if (not isinstance(entry, dict) or set(entry) != {"id", "method", "params", "authenticated", "result", "wire", "client"}
                or entry["id"] != identifier or entry["method"] != "tools/call" or entry["authenticated"] is not authenticated
                or not same_json(entry["params"], params)):
            raise ValueError("style request differs from independent authored scenario")
        frame, session = invocation_wire(entry["wire"], "tools/call", params, authenticated, observed["canonicalClient"])
        if session in sessions or not same_json(entry["result"], frame if "error" in frame else frame["result"]):
            raise ValueError("style reused a session or changed original response")
        sessions.add(session); fingerprints.add(entry["wire"]["expectedCredentialFingerprint"])
        client_observation(entry["client"], frame, entry["wire"], observed["canonicalClient"], observed["clientVersion"])
        outcomes[identifier] = entry["result"]
    if len(fingerprints) != 1:
        raise ValueError("style credential authority changed")
    listed = tool_output(outcomes["catalog"]).get("styles")
    if not isinstance(listed, list):
        raise ValueError("style catalog list missing")
    for style in (a, b):
        matches = [row for row in listed if isinstance(row, dict) and row.get("styleId") == style["id"]]
        if len(matches) != 1 or matches[0].get("uri") != "honua://styles/" + style["id"] or matches[0].get("title") != style["id"]:
            raise ValueError("authored style missing/duplicated in actual catalog")
    for identifier in ("direct", "fresh", "restarted-fresh"):
        get_output(outcomes[identifier], a)
    handle_persistence = {}
    for identifier in ("apply", "replay"):
        handle_persistence[identifier] = apply_output(outcomes[identifier], authored, dry_run=False).get("durable")
    handle_persistence["dry-run"] = apply_output(outcomes["dry-run"], authored, dry_run=True).get("durable")
    for identifier, code in (("unknown-style", "invalid_argument"), ("invalid-encoding", "invalid_argument"), ("unknown-layer", "not_found")):
        negative_output(outcomes[identifier], code)
    for identifier in ("anonymous-get", "anonymous-apply"):
        anonymous_tool_refusal(outcomes[identifier])
    for identifier in ("render", "restarted-render"):
        rendered_output(outcomes[identifier], authored, fixture_bytes)
    snapshots = observed["snapshots"]
    names = ["initial", "dry-run", "applied", "readback", "replay", "unknown-style", "invalid-encoding", "unknown-layer", "anonymous-get", "anonymous-apply", "restarted", "restarted-readback"]
    if not isinstance(snapshots, dict) or set(snapshots) != set(names):
        raise ValueError("independent mutation/refusal/restart census incomplete")
    for name in names:
        snapshot_output(snapshots[name], authored, fixture_bytes, applied=name not in ("initial", "dry-run"))
    snapshots = {name: canonical_state(snapshot) for name, snapshot in snapshots.items()}
    if not same_json(snapshots["initial"], snapshots["dry-run"]):
        raise ValueError("style dry run mutated committed state")
    if snapshots["applied"]["mappings"][0]["revision"] <= snapshots["initial"]["mappings"][0]["revision"]:
        raise ValueError("first style application did not advance the committed graph revision")
    if not same_json(snapshots["initial"]["styles"], snapshots["applied"]["styles"]):
        raise ValueError("style apply changed pre-existing or authored catalog records")
    old_refs = snapshots["initial"]["refs"]
    authored_ids = {style["id"] for style in authored["styles"]}
    if any(ref["styleId"] in authored_ids for ref in old_refs):
        raise ValueError("authored preset was associated before the tested application")
    maximum = max((ref["ordinal"] for ref in old_refs), default=0)
    expected_refs = [{"styleId": authored["styles"][0]["id"], "ordinal": 0}]
    expected_refs += [{"styleId": ref["styleId"], "ordinal": maximum + 1 if ref["ordinal"] == 0 else ref["ordinal"]} for ref in old_refs]
    expected_refs.sort(key=lambda ref: (ref["ordinal"], ref["styleId"]))
    if not same_json(snapshots["applied"]["refs"], expected_refs):
        raise ValueError("style apply did not preserve/demote prior associations exactly")
    if not same_json(snapshots["applied"], snapshots["readback"]):
        raise ValueError("fresh read/render changed committed state or metadata revision")
    # Current executor reconciles the graph on every successful apply, including
    # replay. Its graph revision may advance while catalog/geometry/association
    # bytes remain equal. That allowance applies only to the mutating replay.
    if (not same_json(stable_state(snapshots["applied"]), stable_state(snapshots["replay"]))
            or snapshots["replay"]["mappings"][0]["revision"] < snapshots["applied"]["mappings"][0]["revision"]):
        raise ValueError("replay changed committed state or regressed revision")
    # Each census immediately follows the named refusal; the previous census
    # is its exact baseline. No read/refusal/anonymous/restart revision waiver.
    previous = snapshots["replay"]
    for name in names[5:]:
        if not same_json(previous, snapshots[name]):
            raise ValueError("refusal or restart changed committed state or metadata revision")
        previous = snapshots[name]
    return {key: authored[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha", "candidateImage")} | {
        "canonicalClient": observed["canonicalClient"], "clientVersion": observed["clientVersion"], "operations": OPERATIONS,
        "positive": True, "negative": True, "mediaSchema": True, "persistedReadback": True,
        "restartReadback": True, "anonymousInvocationRefusal": True, "negativeNonmutation": True, "coloredRender": True, "operationHandleDeclaredDurability": handle_persistence}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--nonce")
    parser.add_argument("--canonical-client")
    parser.add_argument("--observations", type=Path)
    args = parser.parse_args()
    try:
        fixture, bootstrap = args.fixture.read_bytes(), strict_json(args.bootstrap.read_bytes())
        value = judge(fixture, bootstrap, strict_json(args.observations.read_bytes())) if args.observations else scenario(fixture, bootstrap, args.nonce, args.canonical_client)
        print(json.dumps(value, allow_nan=False)); return 0
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, OSError):
        print("authored canonical style evidence refused", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
