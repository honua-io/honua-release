"""Authored inline-ingest/publication/readback oracle, with no client transport.

Execution stays in the protected release producer. Retained JSON is evidence
under that producer/workflow trust boundary, not process or database attestation.
"""
from __future__ import annotations
import argparse
import copy
from datetime import datetime
import ipaddress
import json
from pathlib import Path
import re
import sys
from urllib.parse import quote
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_render_validation import scenario as fixture_binding, negative_output
from mcp_map_package_validation import (strict_json, same_json, json_tool_output,
    observed_wire, client_observation, anonymous_tool_refusal, resource_refusal, wire_frame)

OPERATIONS = ["tools/call:ingest_dataset", "tools/call:publish_service", "resources/read:Published service"]
SCHEMA = "honua.mcp-ingest-publish-scenario/v1"
IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]{0,62}")
OWNED_DATABASE = {"host": "localhost", "port": 5432, "databaseName": "honua_test", "username": "honua", "provider": "PostGIS"}


def selector_guid(value):
    # Match the ordinary D/N/B/P Guid forms, preserving the caller's text in
    # evidence. Other strings remain names; no bootstrap identity is selected.
    selected = value
    if len(value) > 1 and (value[0], value[-1]) in (("{", "}"), ("(", ")")):
        selected = value[1:-1]
    if re.fullmatch(r"[a-fA-F0-9]{32}|[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}", selected):
        return str(UUID(selected))
    return None


def registry_output(rows, selector, owned_database):
    if (not same_json(owned_database, OWNED_DATABASE) or not isinstance(selector, str) or not 0 < len(selector) <= 64
            or any(ord(char) < 32 for char in selector) or not isinstance(rows, list) or len(rows) != 1):
        raise ValueError("actual selector lacks one owned active registry identity")
    row = rows[0]
    keys = {"connectionId", "name", "host", "port", "databaseName", "username", "provider", "sslRequired", "sslMode", "isActive"}
    if (not isinstance(row, dict) or set(row) != keys or not isinstance(row["connectionId"], str)
            or selector_guid(row["connectionId"]) != row["connectionId"]
            or not isinstance(row["name"], str) or not 0 < len(row["name"]) <= 64
            or row["host"] not in ("localhost", "127.0.0.1", "::1")
            or type(row["port"]) is not int or row["port"] != owned_database["port"]
            or row["databaseName"] != owned_database["databaseName"] or row["username"] != owned_database["username"]
            or row["provider"] != "postgis" or row["sslRequired"] is not False
            or row["sslMode"] != "disable" or row["isActive"] is not True):
        raise ValueError("registry destination is not the positively verified owned database")
    guid = selector_guid(selector)
    if (guid is not None and guid != row["connectionId"]) or (guid is None and selector != row["name"]):
        raise ValueError("registry identity replaced the actual returned connection selector")
    return row


def connection_provenance(value, selector, owned_database):
    if (not isinstance(value, dict) or set(value) != {"selector", "registry", "catalog", "target", "verifiedAddress"}
            or value["selector"] != selector):
        raise ValueError("independent connection provenance is missing")
    row = registry_output(value["registry"], selector, owned_database)
    address = value["verifiedAddress"]
    if not isinstance(address, str) or not ipaddress.ip_address(address).is_loopback:
        raise ValueError("registry credential destination was not owned loopback")
    if row["host"] != "localhost" and address != row["host"]:
        raise ValueError("observed connection changed its literal target address")
    catalog = value["catalog"]
    if (not isinstance(catalog, dict) or set(catalog) != {"clusterId", "database", "user"}
            or not isinstance(catalog["clusterId"], str) or not re.fullmatch(r"[1-9][0-9]{0,19}", catalog["clusterId"])
            or int(catalog["clusterId"]) > 18446744073709551615
            or catalog["database"] != owned_database["databaseName"] or catalog["user"] != owned_database["username"]
            or not same_json(value["target"], catalog)):
        raise ValueError("registry target and owned catalog have different real database identities")
    return row["connectionId"]


def scenario(fixture_bytes, bootstrap, nonce, canonical_client):
    binding = fixture_binding(fixture_bytes, bootstrap)
    if not same_json(bootstrap.get("database"), OWNED_DATABASE):
        raise ValueError("ingest observation requires the exact owned bootstrap database")
    if not isinstance(nonce, str) or not re.fullmatch(r"[a-f0-9]{12}", nonce):
        raise ValueError("ingest journey requires a bounded owned run identity")
    if canonical_client not in ("Official MCP TypeScript SDK", "MCP Inspector"):
        raise ValueError("ingest journey requires an actual canonical client")
    slug = "sdk" if canonical_client == "Official MCP TypeScript SDK" else "inspector"
    dataset = f"mcp_{slug}_{nonce}"
    service = f"mcp-{slug}-{nonce}"
    rows = json.loads(fixture_bytes)["sites"]["features"]
    expected = [{"properties": {key: row[key] for key in ("gid", "name", "rank")},
                 "geometry": {"type": "Point", "coordinates": [row["x"], row["y"]]}} for row in rows]
    if len(expected) < 3 or len({row["properties"]["gid"] for row in expected}) != len(expected):
        raise ValueError("authored ingest fixture lacks discriminating independent rows")
    geojson = {"type": "FeatureCollection", "features": [{"type": "Feature", **row} for row in expected]}
    return {key: binding[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha", "candidateImage")} | {
        "schema": SCHEMA, "nonce": nonce, "canonicalClient": canonical_client,
        "datasetName": dataset, "serviceName": service, "expectedRows": expected,
        "ingestArguments": {"format": "geojson", "datasetName": dataset, "sourceSrid": 4326,
                            "data": json.dumps(geojson, separators=(",", ":"))},
        "invalidIngestArguments": {"format": "unsupported", "datasetName": dataset + "_bad", "data": "{}"},
        "runtimeBinding": binding["runtimeBinding"], "ownedDatabase": copy.deepcopy(bootstrap["database"])}


def ingest_output(result, authored):
    value = json_tool_output(result)
    if (value.get("success") is not True or value.get("datasetName") != authored["datasetName"]
            or type(value.get("rowCount")) is not int or value["rowCount"] != len(authored["expectedRows"])
            or value.get("srid") != 4326 or type(value.get("srid")) is not int
            or value.get("table") != "imported_" + authored["datasetName"]
            or value.get("geometryColumn") != "geometry" or value.get("primaryKey") != "id"
            or value.get("warnings") != [] or value.get("rowErrors") != []
            or value.get("errorCode") is not None or value.get("errorMessage") is not None
            or not isinstance(value.get("connectionId"), str) or not value["connectionId"]
            or any(not isinstance(value.get(key), str) or not IDENTIFIER.fullmatch(value[key]) for key in ("schema", "table", "geometryColumn", "primaryKey"))):
        raise ValueError("ingest did not return the complete successful authored dataset and publish tuple")
    # These owned short names must retain their real physical identity. The
    # independent DB readback also validates the complete authored contents.
    return value


def publish_arguments(ingested, authored):
    return {**{key: ingested[key] for key in ("connectionId", "schema", "table", "geometryColumn", "primaryKey")},
            "serviceName": authored["serviceName"], "layerName": "authored-ingest", "srid": 4326, "geometryType": "Point"}


def publish_output(result, authored):
    value = json_tool_output(result)
    if (value.get("status") != "Completed" or value.get("requiresApproval") is not False
            or value.get("operationId") != "service.publish"
            or value.get("serviceName") != authored["serviceName"]
            or value.get("serviceUri") != "honua://published-services/" + quote(authored["serviceName"], safe="")
            or not isinstance(value.get("layerId"), str) or not re.fullmatch(r"0|[1-9][0-9]{0,9}", value["layerId"])
            or int(value["layerId"]) >= 2147483647
            or type(value.get("metadataRevision")) is not int or value["metadataRevision"] <= 0
            or any(not isinstance(value.get(key), str) or not value[key] for key in ("operationInstanceId", "handleId"))
            or value["handleId"] != value["operationInstanceId"]):
        raise ValueError("publication is incomplete, queued, approval-pending, or lost its exact returned identity")
    return value


def failed_publication(result, arguments, completed):
    # service.publish validation returns a Failed operation handle inside a
    # successful MCP transport result. It is not a resource RPC/tool error.
    value = json_tool_output(result)
    if (value.get("status") != "Failed" or value.get("requiresApproval") is not False
            or value.get("operationId") != "service.publish"
            or any(not isinstance(value.get(key), str) or not value[key] for key in ("operationInstanceId", "handleId", "correlationId"))
            or value["handleId"] != value["operationInstanceId"]
            or value["operationInstanceId"] == completed["operationInstanceId"]
            or not isinstance(value.get("message"), str)
            or arguments["schema"] + "." + arguments["table"] not in value["message"]
            or "not found" not in value["message"].lower()
            or any(value.get(key) is not None for key in ("serviceName", "serviceUri", "layerId", "metadataRevision", "proposalId", "jobId", "approvalLane"))
            or value.get("resourceIds") != {} or value.get("details") not in ({}, {"errorKind": "not-found"})):
        raise ValueError("invalid publication lacks its own Failed handle or carries a created/proposed/queued identity")
    return value


def resource_output(result, published, snapshot):
    content = result.get("contents") if isinstance(result, dict) else None
    if (not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict)
            or content[0].get("uri") != published["serviceUri"] or content[0].get("mimeType") != "application/json"
            or not isinstance(content[0].get("text"), str)):
        raise ValueError("fresh published resource did not return the exact requested JSON URI")
    value = strict_json(content[0]["text"])
    if (not isinstance(value, dict) or value.get("serviceId") != published["serviceName"]
            or value.get("resourceUri") != published["serviceUri"]
            or value.get("metadataServiceId") != snapshot["publication"]["serviceId"]
            or value.get("sourceId") != snapshot["publication"]["serviceId"]
            or value.get("sourceKind") != "MetadataV2Graph" or value.get("targetKind") != "FeatureService"
            or not same_json(value.get("layerIds"), [snapshot["publication"]["layerId"]])
            or not same_json(value.get("servingLayerIds"), [snapshot["publication"]["layerId"]])
            or value.get("featureServerEnabled") is not True or value.get("serving") is not True
            or value.get("status") != "Active"):
        raise ValueError("fresh resource identity/routability differs from independently persisted publication")
    return value


def query_arguments(published, resource):
    # Service routes use the PUBLIC index exposed by the actual resource read;
    # publish_service layerId identifies physical storage and remains distinct.
    return {"serviceId": published["serviceName"], "layerId": resource["servingLayerIds"][0], "where": "1=1",
            "outFields": ["*"], "outSrid": 4326, "returnGeometry": True, "limit": 20}


def snapshot_output(value, ingested, published, authored):
    if not isinstance(value, dict) or set(value) != {"schema", "tuple", "rows", "publication", "revision", "connection"}:
        raise ValueError("independent persisted readback is missing")
    expected_tuple = {key: ingested[key] for key in ("connectionId", "schema", "table", "geometryColumn", "primaryKey")}
    if value["schema"] != "honua.mcp-ingest-persisted/v2" or not same_json(value["tuple"], expected_tuple):
        raise ValueError("persisted readback used a replacement import tuple")
    rows = value["rows"]
    if not isinstance(rows, list) or len(rows) != len(authored["expectedRows"]):
        raise ValueError("persisted ingest is partial or has extra rows")
    by_gid = {}
    ids = set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {"id", "properties", "geometry", "srid"}
                or type(row["id"]) is not int or row["id"] <= 0 or row["id"] in ids
                or type(row["srid"]) is not int or row["srid"] != 4326
                or not isinstance(row["properties"], dict) or type(row["properties"].get("gid")) is not int):
            raise ValueError("persisted feature identity/schema/SRID is invalid")
        gid = row["properties"]["gid"]
        if gid in by_gid:
            raise ValueError("persisted attributes are duplicated")
        by_gid[gid] = {key: row[key] for key in ("properties", "geometry")}
        ids.add(row["id"])
    expected = {row["properties"]["gid"]: row for row in authored["expectedRows"]}
    if not same_json(by_gid, expected):
        raise ValueError("persisted attributes or geometry differ from the authored source")
    pub = value["publication"]
    canonical = connection_provenance(value["connection"], ingested["connectionId"], authored["ownedDatabase"])
    if (not isinstance(pub, dict) or set(pub) != {"serviceId", "serviceName", "layerId", "storageLayerId", "locator", "connectionId"}
            or pub["serviceName"] != published["serviceName"] or pub["storageLayerId"] != int(published["layerId"])
            or type(pub["layerId"]) is not int or not 0 <= pub["layerId"] < 2147483647 or type(pub["storageLayerId"]) is not int
            or not isinstance(pub["serviceId"], str) or not pub["serviceId"]
            or pub["locator"] != ingested["schema"] + "." + ingested["table"]
            or (pub["connectionId"] != canonical and not (pub["connectionId"] is None and selector_guid(ingested["connectionId"]) is None))
            or type(value["revision"]) is not int or value["revision"] < published["metadataRevision"]):
        raise ValueError("returned service/layer is not bound to the exact persisted imported source")


IMPORTED_ATTRIBUTES_FIELD = "properties"


def query_output(result, published, snapshot):
    # QueryFeaturesTool deliberately puts a prose summary in text content and
    # the complete typed feature representations in structuredContent.
    if (not isinstance(result, dict) or result.get("isError", False) is not False
            or not isinstance(result.get("structuredContent"), dict)
            or not isinstance(result.get("content"), list) or len(result["content"]) != 1):
        raise ValueError("fresh query lacks its structured result and text summary")
    value = result["structuredContent"]
    text = result["content"][0]
    if (not isinstance(text, dict) or set(text) != {"type", "text"} or text["type"] != "text"
            or not isinstance(text["text"], str) or not 0 < len(text["text"]) <= 16384):
        raise ValueError("fresh query contains unrelated or missing summary media")
    if text["text"].lstrip().startswith(("{", "[")) and not same_json(strict_json(text["text"]), value):
        raise ValueError("fresh query has conflicting JSON/text representations")
    if "geojson" in value and "geoJson" in value and not same_json(value["geojson"], value["geoJson"]):
        raise ValueError("fresh query has conflicting GeoJSON aliases")
    collection = value.get("geojson", value.get("geoJson"))
    if (not isinstance(collection, dict) or collection.get("type") != "FeatureCollection"
            or not isinstance(collection.get("features"), list)
            or value.get("serviceId") != snapshot["publication"]["serviceId"]
            or type(value.get("layerId")) is not int or value["layerId"] != snapshot["publication"]["layerId"]
            or value.get("returnedCount") != len(snapshot["rows"]) or type(value.get("returnedCount")) is not int
            or value.get("exceededTransferLimit") is not False or len(collection["features"]) != len(snapshot["rows"])):
        raise ValueError("fresh query envelope is partial or addresses a different layer")
    expected = {row["id"]: row for row in snapshot["rows"]}
    seen = set()
    for feature in collection["features"]:
        if not isinstance(feature, dict) or feature.get("type") != "Feature" or type(feature.get("id")) is not int:
            raise ValueError("query lacks exact allocated feature IDs")
        identifier = feature["id"]
        row = expected.get(identifier)
        props = feature.get("properties")
        # 2026.1 import contract: the imported source attributes live in the layer's single
        # JSON `properties` field (typed-layer flattening is honua-server#6640), so the query
        # must return that field as the exact persisted JSON object, never its text.
        if (row is None or identifier in seen or not isinstance(props, dict)
                or not isinstance(props.get(IMPORTED_ATTRIBUTES_FIELD), dict)
                or not same_json(props[IMPORTED_ATTRIBUTES_FIELD], row["properties"])
                or not same_json(feature.get("geometry"), row["geometry"])):
            raise ValueError("fresh query changed an authored feature, attribute or geometry")
        seen.add(identifier)
    projected = value.get("features")
    if not isinstance(projected, list) or len(projected) != len(expected):
        raise ValueError("fresh query lacks its second typed feature representation")
    seen = set()
    for feature in projected:
        if not isinstance(feature, dict) or set(feature) != {"id", "attributes", "geometry"} or type(feature["id"]) is not int:
            raise ValueError("fresh query second feature representation is malformed")
        row = expected.get(feature["id"])
        if (row is None or feature["id"] in seen or not isinstance(feature["attributes"], dict)
                or not isinstance(feature["attributes"].get(IMPORTED_ATTRIBUTES_FIELD), dict)
                or not same_json(feature["attributes"][IMPORTED_ATTRIBUTES_FIELD], row["properties"])
                or not same_json(feature["geometry"], row["geometry"])):
            raise ValueError("fresh query typed attributes/geometry conflict with the persisted authored feature")
        seen.add(feature["id"])


def invocation_wire(wire, method, params, authenticated, canonical_client):
    if canonical_client != "MCP Inspector" or method != "tools/call":
        return observed_wire(wire, method, params, authenticated)
    if not isinstance(wire, dict) or "discovery" not in wire:
        raise ValueError("actual Inspector catalog request/response missing")
    original = {key: value for key, value in wire.items() if key != "discovery"}
    frame, session = observed_wire(original, method, params, authenticated)
    pages = wire["discovery"]
    if not isinstance(pages, list) or not 1 <= len(pages) <= 32:
        raise ValueError("actual Inspector discovery pages missing or unbounded")
    view = "full" if authenticated else "setup"
    identifiers = {("id", type(original[part]["request"]["id"]), original[part]["request"]["id"]) for part in ("initialize", "invocation")}
    cursor = None
    cursors = set()
    tools = []
    for position, page in enumerate(pages):
        request, response = wire_frame(page)
        selected = request.get("params")
        request_id = ("id", type(request["id"]), request["id"])
        if (request.get("method") != "tools/list" or request_id in identifiers
                or not isinstance(selected, dict) or set(selected) - {"cursor", "_meta"}
                or not isinstance(selected.get("_meta"), dict)
                or selected["_meta"].get("honua.io/workflow-view") != view
                or selected.get("cursor") != cursor or (position == 0 and "cursor" in selected)
                or "error" in response or not isinstance(response.get("result"), dict)
                or not isinstance(response["result"].get("tools"), list)):
            raise ValueError("Inspector discovery changed view, cursor, request identity or server result")
        identifiers.add(request_id)
        actual = page["request"]
        if (actual["sessionSha256"] != session or page["response"]["sessionSha256"] not in (None, session)
                or actual["credentialPresent"] is not authenticated or actual["authorizationPresent"] is not False
                or actual["cookiePresent"] is not False
                or actual["credentialFingerprint"] != (wire["expectedCredentialFingerprint"] if authenticated else None)):
            raise ValueError("Inspector catalog inherited or changed session/credential authority")
        tools.extend(response["result"]["tools"])
        cursor = response["result"].get("nextCursor")
        if cursor is not None and (not isinstance(cursor, str) or not cursor or cursor in cursors or position == len(pages) - 1):
            raise ValueError("Inspector did not retain complete actual catalog pagination")
        if cursor is not None:
            cursors.add(cursor)
        if cursor is None and position != len(pages) - 1:
            raise ValueError("unrelated Inspector catalog pages followed a terminal page")
    matches = [tool for tool in tools if isinstance(tool, dict) and tool.get("name") == params["name"]]
    if len(matches) != 1 or not isinstance(matches[0].get("inputSchema"), dict):
        raise ValueError("actual selected candidate catalog did not advertise the invoked tool exactly once")
    return frame, session


def judge(fixture_bytes, bootstrap, observations):
    if not isinstance(observations, dict):
        raise ValueError("ingest journey observations missing")
    authored = scenario(fixture_bytes, bootstrap, observations.get("nonce"), observations.get("canonicalClient"))
    required = {"schema", "nonce", "canonicalClient", "clientVersion", "candidateSourceSha", "candidateImage", "cases", "reads", "queries", "snapshots", "runtimeBefore", "runtimeRestarted", "runtimeAfter"}
    if set(observations) != required or observations["schema"] != SCHEMA:
        raise ValueError("ingest journey schema differs")
    for key in ("candidateSourceSha", "candidateImage"):
        if observations[key] != authored[key]:
            raise ValueError("ingest journey belongs to another candidate")
    canonical, version = observations["canonicalClient"], observations["clientVersion"]
    expected_runtime = copy.deepcopy(authored["runtimeBinding"])
    before, restarted, after = (observations[key] for key in ("runtimeBefore", "runtimeRestarted", "runtimeAfter"))
    if not isinstance(before, dict) or not isinstance(restarted, dict) or not isinstance(after, dict):
        raise ValueError("candidate restart identities missing")
    for runtime in (before, restarted, after):
        comparable = copy.deepcopy(runtime)
        comparable["startedAt"] = expected_runtime["startedAt"]
        if not same_json(comparable, expected_runtime):
            raise ValueError("restart changed candidate container/image/source or safe identity")
    try:
        before_time = datetime.fromisoformat(before["startedAt"].replace("Z", "+00:00"))
        restarted_time = datetime.fromisoformat(restarted["startedAt"].replace("Z", "+00:00"))
        if before_time.tzinfo is None or restarted_time.tzinfo is None or restarted_time <= before_time:
            raise ValueError("restart did not advance the actual start timestamp")
    except (TypeError, AttributeError):
        raise ValueError("candidate restart timestamps invalid") from None
    if not same_json(restarted, after):
        raise ValueError("candidate was not independently observed restarted and stable through fresh reads")
    sessions, fingerprints = set(), set()
    def call(entry, method, params, authenticated):
        if not isinstance(entry, dict) or set(entry) != {"id", "method", "params", "authenticated", "result", "wire", "client"} or entry["method"] != method or entry["authenticated"] is not authenticated or not same_json(entry["params"], params):
            raise ValueError("actual operation differs from authored request")
        frame, session = invocation_wire(entry["wire"], method, params, authenticated, canonical)
        if session in sessions or not same_json(entry["result"], frame if "error" in frame else frame["result"]):
            raise ValueError("actual operation reused a session or changed the original response")
        sessions.add(session); fingerprints.add(entry["wire"]["expectedCredentialFingerprint"])
        client_observation(entry["client"], frame, entry["wire"], canonical, version)
        return entry["result"]
    def tool(entry, name, args, authenticated=True):
        return call(entry, "tools/call", {"name": name, "arguments": args}, authenticated)
    cases = observations["cases"]
    if not isinstance(cases, list) or [case.get("id") for case in cases] != ["ingest", "invalid-ingest", "publish", "invalid-publish", "anonymous-ingest", "anonymous-publish"]:
        raise ValueError("ingest/publish positive and refusal cases missing")
    ingested = ingest_output(tool(cases[0], "honua_ingest_dataset", authored["ingestArguments"]), authored)
    negative_output(tool(cases[1], "honua_ingest_dataset", authored["invalidIngestArguments"]), "invalid_argument")
    args = publish_arguments(ingested, authored)
    published = publish_output(tool(cases[2], "honua_publish_service", args), authored)
    bad = {**args, "table": "__missing_" + authored["datasetName"]}
    failed_publication(tool(cases[3], "honua_publish_service", bad), bad, published)
    for case, name, arguments in ((cases[4], "honua_ingest_dataset", authored["ingestArguments"]), (cases[5], "honua_publish_service", args)):
        anonymous_tool_refusal(tool(case, name, arguments, False))
    snapshots = observations["snapshots"]
    if not isinstance(snapshots, dict) or set(snapshots) != {"before", "after"}:
        raise ValueError("persisted restart snapshots missing")
    for snapshot in snapshots.values():
        snapshot_output(snapshot, ingested, published, authored)
    if not same_json({key: value for key, value in snapshots["before"].items() if key != "revision"},
                     {key: value for key, value in snapshots["after"].items() if key != "revision"}):
        raise ValueError("restart changed persisted imported data or publication mapping")
    reads = observations["reads"]
    wanted = [("fresh-created", published["serviceUri"], True), ("restarted-created", published["serviceUri"], True),
              ("unknown", published["serviceUri"] + "-uncreated", True), ("anonymous", published["serviceUri"], False)]
    if not isinstance(reads, list) or len(reads) != len(wanted):
        raise ValueError("fresh resource/restart/refusal reads missing")
    resource = None
    for entry, (identifier, uri, authenticated) in zip(reads, wanted, strict=True):
        if entry.get("id") != identifier:
            raise ValueError("fresh read order differs")
        result = call(entry, "resources/read", {"uri": uri}, authenticated)
        if identifier in ("fresh-created", "restarted-created"):
            view = resource_output(result, published, snapshots["before" if identifier == "fresh-created" else "after"])
            if resource is None:
                resource = view
            elif not same_json(resource.get("servingLayerIds"), view.get("servingLayerIds")):
                raise ValueError("restart changed the publicly exposed service address")
        else:
            resource_refusal(result, -32002 if authenticated else -32000, "not_found" if authenticated else "unauthenticated")
    queries = observations["queries"]
    if not isinstance(queries, list) or [entry.get("id") for entry in queries] != ["before", "after"]:
        raise ValueError("fresh queried geometry before/after restart missing")
    for entry in queries:
        query_output(tool(entry, "honua_query_features", query_arguments(published, resource)), published, snapshots[entry["id"]])
    if len(fingerprints) != 1:
        raise ValueError("probes changed expected caller authority")
    return {key: authored[key] for key in ("candidateSourceSha", "candidateImage", "fixtureSha256", "bootstrapSha256")} | {
        "canonicalClient": canonical, "clientVersion": version, "operations": OPERATIONS,
        "positive": True, "negative": True, "mediaSchema": True, "persistedReadback": True, "restartReadback": True,
        "anonymousInvocationRefusal": True, "queuedPublishExercised": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--observations", type=Path)
    parser.add_argument("--nonce")
    parser.add_argument("--canonical-client")
    args = parser.parse_args()
    try:
        fixture = args.fixture.read_bytes(); bootstrap = strict_json(args.bootstrap.read_bytes())
        output = judge(fixture, bootstrap, strict_json(args.observations.read_bytes())) if args.observations else scenario(fixture, bootstrap, args.nonce, args.canonical_client)
        print(json.dumps(output, allow_nan=False))
    except (ValueError, KeyError, TypeError, OSError, OverflowError):
        print("authored ingest/publication oracle refused observations or candidate binding", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
