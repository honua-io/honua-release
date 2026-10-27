"""Independent offline-address and real network-route semantics for MCP clients.

The local geocoder supports EPSG:4326. Route scenarios separately exercise
EPSG:3857 input/output. Missing setup or entitlements cannot qualify a row.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_map_package_validation as common
from mcp_render_validation import scenario as fixture_binding, negative_output

SCHEMA = "honua.mcp-location-scenario/v1"
OPERATIONS = ["tools/call:geocode_address", "tools/call:geocode_addresses", "tools/call:solve_route"]
ADDRESSES = [
    {"query": "380 New York St Redlands", "address": "380 New York St, Redlands, CA 92373",
     "point": [-117.1956, 34.0566]},
    {"query": "1 Microsoft Way Redmond", "address": "1 Microsoft Way, Redmond, WA 98052",
     "point": [-122.1298, 47.6396]},
]
NO_MATCH = "sdkreg-location-unmatched-284-917"
VERTICES = [(x / 100, y / 100) for y in range(3) for x in range(3)]
EDGES = {frozenset((a, b)) for a in VERTICES for b in VERTICES
         if sum(abs(a[i] - b[i]) for i in range(2)) < 0.01000001
         and sum(abs(a[i] - b[i]) for i in range(2)) > 0.00999999}
STOPS = [{"lon": 0, "lat": 0}, {"lon": 0.02, "lat": 0}, {"lon": 0.02, "lat": 0.02}]
LOCATION_FIXTURE = {"addresses": ADDRESSES, "unmatched": NO_MATCH,
    "vertices": VERTICES, "edges": sorted(sorted(edge) for edge in EDGES),
    "localCsvSha256": "80371b8f945cdc87d45107478d53dae58be151ac9ec019aba42e5493a4c73a59",
    "routingSqlSha256": "eac8f620e47107a86bf6390f2b0a15c25c60f049d37a026f5e926ad068b4eb4c"}
LOCATION_SHA = "sha256:" + hashlib.sha256(json.dumps(LOCATION_FIXTURE, sort_keys=True).encode()).hexdigest()


def project(point):
    longitude, latitude = point
    return [6378137 * math.radians(longitude),
            6378137 * math.log(math.tan(math.pi / 4 + math.radians(latitude) / 2))]


def scenario(fixture, bootstrap, operation):
    if operation not in OPERATIONS:
        raise ValueError("unknown location operation")
    binding = fixture_binding(fixture, bootstrap)
    cases = []

    def case(identifier, arguments, kind="invalid", authenticated=True):
        cases.append({"id": identifier, "arguments": copy.deepcopy(arguments), "kind": kind,
                      "authenticated": authenticated})

    if operation == OPERATIONS[0]:
        base = {"address": ADDRESSES[0]["query"], "provider": "local", "maxResults": 1, "outSrid": 4326}
        case("redlands", base, "single:0")
        case("redmond", base | {"address": ADDRESSES[1]["query"]}, "single:1")
        case("included-country", base | {"countryCodes": "US"}, "single:0")
        case("mixed-countries", base | {"countryCodes": "ca, US"}, "single:0")
        case("excluded-country", base | {"countryCodes": "CA"}, "single-empty")
        case("zero-limit", base | {"maxResults": 0})
        case("above-limit", base | {"maxResults": 51})
        case("empty-address", base | {"address": "  "})
        case("invalid-crs", base | {"outSrid": 0})
    elif operation == OPERATIONS[1]:
        base = {"addresses": ["  " + ADDRESSES[1]["query"] + "  ", NO_MATCH,
                              ADDRESSES[0]["query"], ADDRESSES[1]["query"]],
                "provider": "local", "outSrid": 4326}
        case("ordered-partial", base, "batch")
        case("excluded-country", base | {"countryCodes": "CA"}, "batch-empty")
        case("empty-batch", base | {"addresses": []})
        case("above-limit", base | {"addresses": [ADDRESSES[0]["query"]] * 101})
        case("empty-item", base | {"addresses": [ADDRESSES[0]["query"], " "]})
        case("invalid-crs", base | {"outSrid": 0})
    else:
        base = {"stops": STOPS, "inSrid": 4326, "outSrid": 4326, "travelProfile": "driving"}
        case("ordered-via", base, "route")
        case("projected-output", base | {"outSrid": 3857}, "route")
        projected = [{"lon": project((p["lon"], p["lat"]))[0],
                      "lat": project((p["lon"], p["lat"]))[1]} for p in STOPS]
        case("projected-input", base | {"stops": projected, "inSrid": 3857}, "route")
        case("too-few-stops", base | {"stops": STOPS[:1]})
        case("above-limit", base | {"stops": [STOPS[0]] * 26})
        case("missing-ordinate", base | {"stops": [{"lon": 0}, STOPS[-1]]})
        case("invalid-crs", base | {"outSrid": 0})
    case("anonymous", base, "anonymous", False)
    return {key: binding[key] for key in ("fixtureSha256", "bootstrapSha256", "candidateSourceSha", "candidateImage", "runtimeBinding")} | {
        "schema": SCHEMA, "operation": operation, "locationFixtureSha256": LOCATION_SHA,
        "tool": "honua_" + operation.split(":", 1)[1], "cases": cases}


def number(value, minimum=None, maximum=None):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or minimum is not None and value < minimum or maximum is not None and value > maximum):
        raise ValueError("location response contains a nonfinite, boolean or out-of-range number")
    return value


def coordinate(value, expected, tolerance):
    return abs(number(value) - expected) <= tolerance


def single_output(result, index):
    output = common.json_tool_output(result)
    candidates = output.get("candidates")
    if output.get("provider") != "local" or not isinstance(candidates, list) or len(candidates) != 1:
        raise ValueError("single geocode ignored local provider or requested candidate bound")
    actual, expected = candidates[0], ADDRESSES[index]
    if (not isinstance(actual, dict) or actual.get("address") != expected["address"]
            or not common.same_json(actual.get("srid"), 4326)
            or not coordinate(actual.get("x"), expected["point"][0], 1e-8)
            or not coordinate(actual.get("y"), expected["point"][1], 1e-8)):
        raise ValueError("single geocode differs from independently authored address/longitude/latitude/CRS")
    number(actual.get("score"), 1, 100)


def batch_output(result, arguments, excluded=False):
    output = common.json_tool_output(result)
    if (not common.same_json(output.get("succeeded"), 0 if excluded else 3)
            or not common.same_json(output.get("failed"), 4 if excluded else 1)
            or not common.same_json(output.get("srid"), 4326)
            or not isinstance(output.get("results"), list) or len(output["results"]) != 4):
        raise ValueError("batch geocoding lost partial-failure counts or ordered entries")
    for actual, query, index in zip(output["results"], arguments["addresses"], (None,)*4 if excluded else (1, None, 0, 1), strict=True):
        if not isinstance(actual, dict) or actual.get("input") != query.strip():
            raise ValueError("batch geocoding reordered, omitted or failed to trim an input")
        if index is None:
            if (actual.get("ok") is not False or not isinstance(actual.get("error"), str) or not actual["error"]
                    or any(actual.get(key) is not None for key in ("location", "score", "matchedAddress"))):
                raise ValueError("unmatched batch address did not retain a clean per-item failure")
            continue
        expected = ADDRESSES[index]
        location = actual.get("location")
        if (actual.get("ok") is not True or actual.get("provider") != "local" or actual.get("error") is not None
                or actual.get("matchedAddress") != expected["address"] or not isinstance(location, dict)
                or not common.same_json(location.get("srid"), 4326)
                or not coordinate(location.get("x"), expected["point"][0], 1e-8)
                or not coordinate(location.get("y"), expected["point"][1], 1e-8)):
            raise ValueError("batch success differs from the independent expected local record")
        number(actual.get("score"), 1, 100)


def route_output(result, arguments):
    output = common.json_tool_output(result)
    if output.get("provider") != "pgrouting" or output.get("solved") is not True:
        raise ValueError("route did not use the real solved pgRouting provider")
    geometry = common.strict_json(output.get("routeGeometryGeoJson"))
    coordinates = geometry.get("coordinates") if isinstance(geometry, dict) else None
    if (not isinstance(geometry, dict) or geometry.get("type") != "LineString"
            or not isinstance(coordinates, list) or len(coordinates) != 5):
        raise ValueError("route is not a four-edge network LineString")
    vertices = {tuple(project(point) if arguments["outSrid"] == 3857 else point): point for point in VERTICES}
    path = []
    tolerance = 1e-5 if arguments["outSrid"] == 3857 else 1e-8
    for point in coordinates:
        if not isinstance(point, list) or len(point) != 2:
            raise ValueError("route has missing or extra ordinates")
        matches = [original for projected, original in vertices.items()
                   if all(coordinate(point[i], projected[i], tolerance) for i in range(2))]
        if len(matches) != 1:
            raise ValueError("route coordinate does not match requested CRS and reviewed network")
        path.append(matches[0])
    if (path[0] != (0, 0) or path[2] != (0.02, 0) or path[-1] != (0.02, 0.02)
            or any(frozenset((a, b)) not in EDGES for a, b in zip(path, path[1:]))):
        raise ValueError("route ignored ordered stops or traversed an absent network edge")
    if not coordinate(output.get("totalTimeMinutes"), 4, 1e-8):
        raise ValueError("route cost differs from four unit-minute network edges")
    # Independent spherical calculation, with 1% allowance for the production
    # WGS84 ellipsoidal geography calculation. It rejects gross unit/length drift.
    length = 0
    for a, b in zip(path, path[1:]):
        lat1, lat2 = math.radians(a[1]), math.radians(b[1])
        h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(math.radians(b[0]-a[0])/2)**2
        length += 6371000 * 2 * math.asin(math.sqrt(h))
    if not math.isclose(number(output.get("totalLengthMeters"), 1), length, rel_tol=0.01):
        raise ValueError("route length is inconsistent with its independently checked geometry")
    directions = output.get("directions")
    if (not isinstance(directions, list) or len(directions) != 1 or not isinstance(directions[0], dict)
            or directions[0].get("text") != "Route" or directions[0].get("maneuverType") != "straight"
            or not coordinate(directions[0].get("lengthMeters"), output["totalLengthMeters"], 1e-8)
            or not coordinate(directions[0].get("timeMinutes"), 4, 1e-8)):
        raise ValueError("route directions disagree with solved route length/time")


def judge(fixture, bootstrap, observed, operation):
    authored = scenario(fixture, bootstrap, operation)
    if (not isinstance(observed, dict) or set(observed) != set(authored) | {"canonicalClient", "clientVersion", "runtimeBefore", "runtimeAfter"}
            or any(not common.same_json(observed.get(key), value) for key, value in authored.items() if key != "cases")
            or not common.same_json(observed.get("runtimeBefore"), authored["runtimeBinding"])
            or not common.same_json(observed.get("runtimeAfter"), authored["runtimeBinding"])
            or not isinstance(observed.get("cases"), list) or len(observed["cases"]) != len(authored["cases"])):
        raise ValueError("location observations differ from exact authored scenario/candidate")
    sessions, fingerprints = set(), set()
    # D3: only the Inspector's anonymous probe of the two fixed tools observes
    # discovery; there, absence from the actual anonymous catalog plus the
    # Inspector's local tool_not_found refusal is the refusal evidence.
    unlisted_lane = (observed["canonicalClient"] == "MCP Inspector"
                     and authored["tool"] in common.ANONYMOUS_UNLISTED_REFUSAL_TOOLS)
    refusal_evidence = "server-refusal"
    for expected, actual in zip(authored["cases"], observed["cases"], strict=True):
        if (not isinstance(actual, dict) or set(actual) != set(expected) | {"result", "wire", "client"}
                or any(not common.same_json(actual.get(key), value) for key, value in expected.items())):
            raise ValueError("location invocation changed authored input, operation or authority")
        params = {"name": authored["tool"], "arguments": expected["arguments"]}
        if (unlisted_lane and expected["kind"] == "anonymous" and expected["authenticated"] is False
                and isinstance(actual["wire"], dict) and actual["wire"].get("invocation") is None):
            if actual["result"] is not None:
                raise ValueError("unlisted anonymous probe cannot retain a server result")
            session = common.anonymous_unlisted_refusal(actual["wire"], actual["client"], params, observed["canonicalClient"], observed["clientVersion"])
            if session in sessions:
                raise ValueError("location response or fresh session differs from actual retained wire")
            sessions.add(session); fingerprints.add(actual["wire"]["expectedCredentialFingerprint"])
            refusal_evidence = common.ANONYMOUS_UNLISTED_REFUSAL
            continue
        if unlisted_lane and expected["kind"] == "anonymous":
            frame, session = common.listed_anonymous_invocation(actual["wire"], params, observed["canonicalClient"])
        else:
            frame, session = common.observed_wire(actual["wire"], "tools/call", params, expected["authenticated"])
        result = frame if "error" in frame else frame["result"]
        if session in sessions or not common.same_json(result, actual["result"]):
            raise ValueError("location response or fresh session differs from actual retained wire")
        sessions.add(session); fingerprints.add(actual["wire"]["expectedCredentialFingerprint"])
        common.client_observation(actual["client"], frame, actual["wire"], observed["canonicalClient"], observed["clientVersion"])
        kind = expected["kind"]
        if kind.startswith("single:"): single_output(result, int(kind[-1]))
        elif kind == "single-empty":
            output = common.json_tool_output(result)
            if output.get("provider") != "local" or output.get("candidates") != []:
                raise ValueError("local geocoding ignored excluded-country restriction")
        elif kind in ("batch", "batch-empty"): batch_output(result, expected["arguments"], kind == "batch-empty")
        elif kind == "route": route_output(result, expected["arguments"])
        elif kind == "anonymous": common.anonymous_tool_refusal(result)
        else: negative_output(result, "invalid_argument")
    if len(fingerprints) != 1:
        raise ValueError("location cases changed the expected private credential")
    return {key: authored[key] for key in ("candidateSourceSha", "candidateImage", "fixtureSha256", "bootstrapSha256", "locationFixtureSha256", "operation")} | {
        "canonicalClient": observed["canonicalClient"], "clientVersion": observed["clientVersion"],
        "positive": True, "negative": True, "mediaSchema": True, "anonymousInvocationRefusal": True,
        "coordinateSemantics": True, "limitSemantics": True, "anonymousRefusalEvidence": refusal_evidence}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True); parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--operation", choices=OPERATIONS, required=True); parser.add_argument("--observations", type=Path)
    args = parser.parse_args()
    try:
        fixture, bootstrap = args.fixture.read_bytes(), common.strict_json(args.bootstrap.read_bytes())
        output = judge(fixture, bootstrap, common.strict_json(args.observations.read_bytes()), args.operation) if args.observations else scenario(fixture, bootstrap, args.operation)
        print(json.dumps(output, sort_keys=True, allow_nan=False))
    except (KeyError, TypeError, ValueError, OSError, OverflowError):
        print("location scenario/oracle refused incomplete or mismatched evidence", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
