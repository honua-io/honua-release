"""Explicit synthetic oracle/wire fixtures; never candidate/client evidence."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

import mcp_location_validation as validation
from mcp_map_package_validation_controls import control_wire, bind_launch
from mcp_render_validation_controls import FIXTURE, bootstrap, runtime

HERE = Path(__file__).resolve().parent


def output(value, error=False):
    return {"isError": error, "structuredContent": value,
            "content": [{"type": "text", "text": json.dumps(value)}]}


def responses(operation):
    authored = validation.scenario(FIXTURE, bootstrap(), operation)
    result = []
    for case in authored["cases"]:
        kind = case["kind"]
        if kind.startswith("single:"):
            index = int(kind[-1])
            value = [{"address": "380 New York St, Redlands, CA 92373", "x": -117.1956, "y": 34.0566,
                      "score": 95, "srid": 4326, "matchLevel": "approximate", "addressType": "PointAddress"},
                     {"address": "1 Microsoft Way, Redmond, WA 98052", "x": -122.1298, "y": 47.6396,
                      "score": 95, "srid": 4326, "matchLevel": "approximate", "addressType": "PointAddress"}][index]
            result.append(output({"provider": "local", "candidates": [value]}))
        elif kind == "single-empty":
            result.append(output({"provider": "local", "candidates": []}))
        elif kind in ("batch", "batch-empty"):
            entries = []
            expected = [("1 Microsoft Way, Redmond, WA 98052", -122.1298, 47.6396), None,
                        ("380 New York St, Redlands, CA 92373", -117.1956, 34.0566),
                        ("1 Microsoft Way, Redmond, WA 98052", -122.1298, 47.6396)]
            if kind == "batch-empty": expected = [None] * 4
            for query, found in zip(case["arguments"]["addresses"], expected, strict=True):
                entries.append({"input": query.strip(), "ok": found is not None, "error": None if found else "No match found for the address.",
                    "location": {"x": found[1], "y": found[2], "srid": 4326} if found else None,
                    "matchedAddress": found[0] if found else None, "score": 95 if found else None,
                    "provider": "local" if found else None, "matchLevel": "approximate" if found else None})
            result.append(output({"results": entries, "succeeded": 0 if kind == "batch-empty" else 3,
                                  "failed": 4 if kind == "batch-empty" else 1, "srid": 4326}))
        elif kind == "route":
            path = [[0, 0], [0.01, 0], [0.02, 0], [0.02, 0.01], [0.02, 0.02]]
            if case["arguments"]["outSrid"] == 3857:
                path = [[0, 0], [1113.1949079327358, 0], [2226.3898158654715, 0],
                        [2226.3898158654715, 1113.1949135843784], [2226.3898158654715, 2226.389861078614]]
            result.append(output({"provider": "pgrouting", "solved": True,
                "routeGeometryGeoJson": json.dumps({"type": "LineString", "coordinates": path}),
                "totalTimeMinutes": 4, "totalLengthMeters": 4437.875,
                "directions": [{"text": "Route", "lengthMeters": 4437.875, "timeMinutes": 4, "maneuverType": "straight"}]}))
        elif kind == "anonymous":
            result.append(output({"status": "error", "code": "unauthenticated", "message": "Controlled unauthenticated call",
                "requiresReauthentication": True, "error": {"kind": "AuthorizationDenied", "message": "Controlled unauthenticated call"}}, True))
        else:
            result.append(output({"status": "error", "code": "invalid_argument", "message": "Controlled invalid input"}, True))
    return result


def bind(value):
    for index, item in enumerate(value["cases"]):
        params = {"name": value["tool"], "arguments": item["arguments"]}
        item["wire"], item["client"], item["result"] = control_wire(
            "tools/call", params, item["result"], index, item["authenticated"])
    return value


def observations(operation):
    value = validation.scenario(FIXTURE, bootstrap(), operation)
    value.update(canonicalClient="Official MCP TypeScript SDK", clientVersion="1.32.1", runtimeBefore=runtime(), runtimeAfter=runtime())
    for item, result in zip(value["cases"], responses(operation), strict=True): item["result"] = result
    return bind(value)


def sync(item):
    item["result"]["content"][0]["text"] = json.dumps(item["result"]["structuredContent"])


class LocationControls(unittest.TestCase):
    def test_each_independently_authored_primary_path(self):
        for operation in validation.OPERATIONS:
            with self.subTest(operation=operation):
                proof = validation.judge(FIXTURE, bootstrap(), observations(operation), operation)
                self.assertTrue(all(proof[k] for k in ("positive", "negative", "coordinateSemantics", "limitSemantics", "anonymousInvocationRefusal")))

    def test_single_geocode_axis_crs_provider_rank_and_address_refuse(self):
        operation = validation.OPERATIONS[0]
        for mode in ("axes", "crs", "provider", "address", "empty", "rank", "score", "boolean"):
            with self.subTest(mode=mode):
                value = observations(operation); item = value["cases"][0]; actual = item["result"]["structuredContent"]
                candidate = actual["candidates"][0]
                if mode == "axes": candidate["x"], candidate["y"] = candidate["y"], candidate["x"]
                elif mode == "crs": candidate["srid"] = 3857
                elif mode == "provider": actual["provider"] = "mock"
                elif mode == "address": candidate["address"] = "unrelated address"
                elif mode == "empty": actual["candidates"] = []
                elif mode == "rank": actual["candidates"].append(copy.deepcopy(candidate))
                elif mode == "score": candidate["score"] = 101
                else: candidate["score"] = True
                sync(item)
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), bind(value), operation)

    def test_ordered_batch_partial_failure_and_duplicate_input_refuse(self):
        operation = validation.OPERATIONS[1]
        for mode in ("order", "missing", "duplicate-omitted", "count", "boolean-count", "false-success", "error-leak", "coordinates", "provider"):
            with self.subTest(mode=mode):
                value = observations(operation); item = value["cases"][0]; out = item["result"]["structuredContent"]; rows = out["results"]
                if mode == "order": rows[0], rows[2] = rows[2], rows[0]
                elif mode in ("missing", "duplicate-omitted"): rows.pop(3 if mode == "duplicate-omitted" else 1)
                elif mode == "count": out["succeeded"] = 4
                elif mode == "boolean-count": out["failed"] = True
                elif mode == "false-success": rows[1]["ok"] = True
                elif mode == "error-leak": rows[1]["location"] = {"x": 0, "y": 0, "srid": 4326}
                elif mode == "coordinates": rows[0]["location"]["y"] = -122.1298
                else: rows[0]["provider"] = "nominatim"
                sync(item)
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), bind(value), operation)

    def test_accepted_but_ignored_country_filter_is_not_success(self):
        for operation in validation.OPERATIONS[:2]:
            with self.subTest(operation=operation):
                value = observations(operation)
                excluded = next(c for c in value["cases"] if c["id"] == "excluded-country")
                excluded["result"] = copy.deepcopy(value["cases"][0]["result"])
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), bind(value), operation)
        value = observations(validation.OPERATIONS[0])
        included = next(c for c in value["cases"] if c["id"] == "included-country")
        included["result"] = output({"provider": "local", "candidates": []})
        with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), bind(value), validation.OPERATIONS[0])

    def test_route_network_cost_crs_direction_and_distance_refuse(self):
        operation = validation.OPERATIONS[2]
        for mode in ("mock", "unsolved", "direct", "diagonal", "reversed", "ignored-projection", "cost", "units", "directions", "nonfinite", "boolean"):
            with self.subTest(mode=mode):
                value = observations(operation); item = value["cases"][1 if mode == "ignored-projection" else 0]; out = item["result"]["structuredContent"]
                geometry = json.loads(out["routeGeometryGeoJson"])
                if mode == "mock": out["provider"] = "mock"
                elif mode == "unsolved": out["solved"] = False
                elif mode == "direct": geometry["coordinates"] = [[0, 0], [0.02, 0.02]]
                elif mode == "diagonal": geometry["coordinates"][1] = [0.01, 0.01]
                elif mode == "reversed": geometry["coordinates"].reverse()
                elif mode == "ignored-projection": geometry["coordinates"] = [[0, 0], [0.01, 0], [0.02, 0], [0.02, 0.01], [0.02, 0.02]]
                elif mode == "cost": out["totalTimeMinutes"] = 3
                elif mode == "units": out["totalLengthMeters"] /= 1000
                elif mode == "directions": out["directions"][0]["timeMinutes"] = 5
                elif mode == "nonfinite": geometry["coordinates"][1][0] = float("nan")
                else: out["totalTimeMinutes"] = True
                out["routeGeometryGeoJson"] = json.dumps(geometry); sync(item)
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), bind(value), operation)

    def test_raw_session_candidate_process_and_negative_controls_refuse(self):
        for operation in validation.OPERATIONS:
            for mode in ("missing", "argument", "wire", "session", "client", "image", "runtime", "runtime-boolean", "fixture", "auth", "negative"):
                with self.subTest(operation=operation, mode=mode):
                    value = observations(operation)
                    if mode == "missing": value["cases"].pop()
                    elif mode == "argument": value["cases"][0]["arguments"]["unrequested"] = True
                    elif mode == "wire": value["cases"][0]["wire"] = None
                    elif mode == "session": value["cases"][1]["wire"] = value["cases"][0]["wire"]
                    elif mode == "client": value["cases"][0]["client"]["launch"]["processId"] += 1
                    elif mode == "image": value["candidateImage"] += "-other"
                    elif mode == "runtime": value["runtimeAfter"]["containerId"] = "f" * 64
                    elif mode == "runtime-boolean": value["runtimeAfter"]["running"] = 1
                    elif mode == "fixture": value["locationFixtureSha256"] = "sha256:" + "0" * 64
                    elif mode == "auth": value["cases"][-1]["authenticated"] = True
                    else:
                        item = next(c for c in value["cases"] if c["kind"] == "invalid")
                        item["result"]["isError"] = False; value = bind(value)
                    with self.assertRaises((ValueError, TypeError)): validation.judge(FIXTURE, bootstrap(), value, operation)

    def test_anonymous_and_invalid_semantics_refuse_even_with_matching_raw_and_client_bytes(self):
        for operation in validation.OPERATIONS:
            for mode in ("anonymous-success", "anonymous-wrong-code", "invalid-success", "invalid-wrong-code"):
                with self.subTest(operation=operation, mode=mode):
                    value = observations(operation)
                    item = value["cases"][-1] if mode.startswith("anonymous") else next(c for c in value["cases"] if c["kind"] == "invalid")
                    if mode.endswith("success"):
                        item["result"] = copy.deepcopy(value["cases"][0]["result"])
                    else:
                        item["result"]["structuredContent"]["code"] = "internal"
                        sync(item)
                    with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), bind(value), operation)

    def test_inspector_rejudges_its_own_bounded_process_output(self):
        for operation in validation.OPERATIONS:
            with self.subTest(operation=operation):
                value = observations(operation); value["canonicalClient"], value["clientVersion"] = "MCP Inspector", "2.10.1"
                for item in value["cases"]:
                    item["client"] = {"kind": "inspector-cli", "canonicalClient": "MCP Inspector", "version": "2.10.1", "processId": 456,
                        "requestUrl": item["wire"]["listener"], "exitCode": 5 if item["result"].get("isError") else 0,
                        "exitSignal": None, "timedOut": False, "stdout": json.dumps({"result": item["result"]}), "stderr": ""}
                    bind_launch(item["client"], item["wire"], "inspector")
                self.assertTrue(validation.judge(FIXTURE, bootstrap(), value, operation)["coordinateSemantics"])
                value["cases"][0]["client"]["exitCode"] = 2
                bind_launch(value["cases"][0]["client"], value["cases"][0]["wire"], "inspector")
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), value, operation)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--write-controls":
        directory = Path(sys.argv[2])
        subprocess.run([sys.executable, str(HERE / "mcp_render_validation_controls.py"), "--write-controls", str(directory)], check=True)
        # Preserve actual bootstrap bytes: JS numeric serialization cannot
        # replace the original source-bound Python JSON digest.
        values = {operation: responses(operation) for operation in validation.OPERATIONS}
        (directory / "location-responses.json").write_text(json.dumps(values))
    else:
        unittest.main()
