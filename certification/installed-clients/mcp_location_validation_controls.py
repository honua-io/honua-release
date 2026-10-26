"""Explicit synthetic oracle/wire fixtures; never candidate/client evidence."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest

import mcp_location_validation as validation
from mcp_map_package_validation_controls import control_wire, bind_launch, encoded_body
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


# D3 evidence mode name, spelled independently of the judge under test.
UNLISTED = "inspector-anonymous-unlisted"
UNLISTED_OPERATIONS = ("tools/call:geocode_addresses", "tools/call:solve_route")
DEFAULT_ANONYMOUS = [f"honua_default_{index}" for index in range(12)]


def inspector_client(item):
    item["client"] = {"kind": "inspector-cli", "canonicalClient": "MCP Inspector", "version": "2.10.1", "processId": 456,
        "requestUrl": item["wire"]["listener"], "exitCode": 5 if item["result"].get("isError") else 0,
        "exitSignal": None, "timedOut": False, "stdout": json.dumps({"result": item["result"]}), "stderr": ""}
    bind_launch(item["client"], item["wire"], "inspector")


def discovery_pages(wire, pages, first_id=1, params=None):
    """Synthetic anonymous default-view tools/list pages on the wire's fresh session."""
    session = wire["initialize"]["response"]["sessionSha256"]
    records = []
    for index, names in enumerate(pages):
        selected = dict(params or {}) | ({"cursor": f"page-{index}"} if index else {})
        frame = {"jsonrpc": "2.0", "id": first_id + index, "method": "tools/list", **({"params": selected} if selected else {})}
        request = {"jsonrpc": "2.0", "id": first_id + index, "method": "tools/list", **encoded_body(frame), "sessionSha256": session,
                   "credentialPresent": False, "credentialFingerprint": None, "authorizationPresent": False, "cookiePresent": False}
        if selected: request["params"] = selected
        result = {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in names]}
        if index < len(pages) - 1: result["nextCursor"] = f"page-{index + 1}"
        records.append({"request": request, "response": {"status": 200, "contentType": "application/json", "contentEncoding": "",
            "sessionSha256": None, "complete": True, "delivered": True, **encoded_body({"jsonrpc": "2.0", "id": first_id + index, "result": result})}})
    return records


def unlisted(value, item, pages=None, tool=None):
    """Retained D3 shape: initialize + anonymous tools/list walk, no tools/call,
    pinned Inspector exits 5 with tool_not_found naming the tool."""
    tool = tool or value["tool"]
    wire = item["wire"]
    wire["invocation"] = None
    wire["discovery"] = discovery_pages(wire, pages or [DEFAULT_ANONYMOUS[:6], DEFAULT_ANONYMOUS[6:]])
    wire["refusalEvidence"] = UNLISTED
    item["result"] = None
    item["client"] = {"kind": "inspector-cli", "canonicalClient": "MCP Inspector", "version": "2.10.1", "processId": 789,
        "requestUrl": wire["listener"], "exitCode": 5, "exitSignal": None, "timedOut": False, "stdout": "",
        "stderr": json.dumps({"error": {"code": "tool_not_found", "message": f"Tool '{tool}' not found on server."}}) + "\n"}
    bind_launch(item["client"], wire, "inspector")
    return value


def inspector_observations(operation, anonymous="unlisted"):
    value = observations(operation)
    value["canonicalClient"], value["clientVersion"] = "MCP Inspector", "2.10.1"
    for item in value["cases"]:
        inspector_client(item)
    item = value["cases"][-1]
    if anonymous == "unlisted":
        unlisted(value, item)
    elif anonymous == "listed":
        # The tool IS listed: Inspector sent tools/call and the server refused.
        item["wire"]["discovery"] = discovery_pages(item["wire"], [DEFAULT_ANONYMOUS + [value["tool"]]], first_id=7)
        item["wire"]["refusalEvidence"] = UNLISTED
    return value


def rebind(value, item):
    bind_launch(item["client"], item["wire"], "inspector")
    return value


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
                value = inspector_observations(operation, "listed" if operation in UNLISTED_OPERATIONS else None)
                proof = validation.judge(FIXTURE, bootstrap(), value, operation)
                self.assertTrue(proof["coordinateSemantics"])
                self.assertEqual(proof["anonymousRefusalEvidence"], "server-refusal")
                value["cases"][0]["client"]["exitCode"] = 2
                bind_launch(value["cases"][0]["client"], value["cases"][0]["wire"], "inspector")
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), value, operation)


class InspectorAnonymousUnlistedControls(unittest.TestCase):
    """D3: absence from the observed anonymous catalog is refusal evidence only
    on the Inspector lane, only for honua_geocode_addresses/honua_solve_route."""

    def judge(self, value, operation):
        return validation.judge(FIXTURE, bootstrap(), value, operation)

    def refused(self, value, operation):
        with self.assertRaises((ValueError, TypeError, KeyError)):
            self.judge(value, operation)

    def test_valid_discovery_absence_passes_for_exactly_the_two_tools(self):
        for operation in UNLISTED_OPERATIONS:
            for pages in ([DEFAULT_ANONYMOUS], [DEFAULT_ANONYMOUS[:6], DEFAULT_ANONYMOUS[6:]], [DEFAULT_ANONYMOUS[:4], DEFAULT_ANONYMOUS[4:8], DEFAULT_ANONYMOUS[8:]]):
                with self.subTest(operation=operation, pages=len(pages)):
                    value = inspector_observations(operation, None)
                    unlisted(value, value["cases"][-1], pages)
                    proof = self.judge(value, operation)
                    self.assertTrue(proof["anonymousInvocationRefusal"])
                    self.assertEqual(proof["anonymousRefusalEvidence"], UNLISTED)

    def test_discovery_absence_cannot_qualify_any_other_tool(self):
        operation = validation.OPERATIONS[0]
        value = inspector_observations(operation, None)
        unlisted(value, value["cases"][-1])
        self.refused(value, operation)
        for tool in ("honua_geocode_address", "honua_create_map_package", "honua_create_app_package", "honua_ingest_dataset"):
            with self.subTest(tool=tool):
                value = inspector_observations(UNLISTED_OPERATIONS[0])
                item = value["cases"][-1]
                params = {"name": tool, "arguments": item["arguments"]}
                item["wire"]["expectedParams"] = params
                unlisted(value, item, tool=tool)
                with self.assertRaises(ValueError):
                    validation.common.anonymous_unlisted_refusal(item["wire"], item["client"], params, "MCP Inspector", "2.10.1")

    def test_sdk_lane_still_requires_the_server_refusal(self):
        for operation in UNLISTED_OPERATIONS:
            with self.subTest(operation=operation):
                value = inspector_observations(operation)
                value["canonicalClient"], value["clientVersion"] = "Official MCP TypeScript SDK", "1.32.1"
                self.refused(value, operation)
                value = observations(operation); item = value["cases"][-1]
                unlisted(value, item)
                item["client"] = {"kind": "sdk", "canonicalClient": "Official MCP TypeScript SDK", "version": "1.32.1", "processId": 123,
                    "requestUrl": item["wire"]["listener"], "sessionSha256": item["wire"]["initialize"]["response"]["sessionSha256"], "output": None}
                bind_launch(item["client"], item["wire"])
                self.refused(value, operation)
                with self.assertRaises(ValueError):
                    validation.common.anonymous_unlisted_refusal(item["wire"], item["client"], item["wire"]["expectedParams"],
                                                                 "Official MCP TypeScript SDK", "1.32.1")

    def test_tool_listed_to_anonymous_refuses_absence_evidence(self):
        for operation in UNLISTED_OPERATIONS:
            for where in ("first", "last", "only"):
                with self.subTest(operation=operation, where=where):
                    value = inspector_observations(operation, None); tool = value["tool"]
                    pages = {"first": [[tool] + DEFAULT_ANONYMOUS[:6], DEFAULT_ANONYMOUS[6:]],
                             "last": [DEFAULT_ANONYMOUS[:6], DEFAULT_ANONYMOUS[6:] + [tool]], "only": [[tool]]}[where]
                    unlisted(value, value["cases"][-1], pages)
                    self.refused(value, operation)

    def test_missing_incomplete_or_non_result_page_refuses(self):
        modes = ("no-pages", "missing-last", "missing-first", "incomplete", "undelivered", "status-500", "error-frame",
                 "body-hash", "wrong-id", "duplicate-id", "repeated-cursor", "terminal-then-more", "unnamed-tool",
                 "workflow-view", "foreign-session", "credential", "authorization", "too-many", "null-params", "projection-omits-params")
        for operation in UNLISTED_OPERATIONS:
            for mode in modes:
                with self.subTest(operation=operation, mode=mode):
                    value = inspector_observations(operation); item = value["cases"][-1]; wire = item["wire"]
                    pages = wire["discovery"]
                    def body(page, frame):
                        page["response"].update(encoded_body(frame))
                    if mode == "no-pages": wire["discovery"] = []
                    elif mode == "missing-last": pages.pop()
                    elif mode == "missing-first": pages.pop(0)
                    elif mode == "incomplete": pages[1]["response"]["complete"] = False
                    elif mode == "undelivered": pages[0]["response"]["delivered"] = False
                    elif mode == "status-500": pages[1]["response"]["status"] = 500
                    elif mode == "error-frame": body(pages[1], {"jsonrpc": "2.0", "id": 2, "error": {"code": -32603, "message": "controlled"}})
                    elif mode == "body-hash": pages[0]["response"]["bodySha256"] = "sha256:" + "0" * 64
                    elif mode == "wrong-id": body(pages[0], {"jsonrpc": "2.0", "id": 99, "result": {"tools": [], "nextCursor": "page-1"}})
                    elif mode == "duplicate-id":
                        wire["discovery"] = discovery_pages(wire, [DEFAULT_ANONYMOUS], first_id=0)
                    elif mode == "repeated-cursor":
                        wire["discovery"] = discovery_pages(wire, [DEFAULT_ANONYMOUS[:4], DEFAULT_ANONYMOUS[4:8], DEFAULT_ANONYMOUS[8:]])
                        body(wire["discovery"][1], {"jsonrpc": "2.0", "id": 2, "result": {"tools": [], "nextCursor": "page-1"}})
                    elif mode == "terminal-then-more":
                        body(pages[0], {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "honua_default_0"}]}})
                    elif mode == "unnamed-tool":
                        body(pages[1], {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"title": value["tool"]}]}})
                    elif mode == "workflow-view":
                        wire["discovery"] = discovery_pages(wire, [DEFAULT_ANONYMOUS], params={"_meta": {"honua.io/workflow-view": "analyze"}})
                    elif mode == "foreign-session": pages[1]["request"]["sessionSha256"] = "sha256:" + "f" * 64
                    elif mode == "credential": pages[0]["request"]["credentialPresent"] = True
                    elif mode == "authorization": pages[1]["request"]["authorizationPresent"] = True
                    elif mode == "too-many": wire["discovery"] = discovery_pages(wire, [[f"honua_default_{i}"] for i in range(33)])
                    elif mode == "null-params": pages[0]["request"]["params"] = None
                    else:
                        frame = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {"honua.io/workflow-view": "operate"}}}
                        pages[0]["request"].update(encoded_body(frame))
                    rebind(value, item)
                    self.refused(value, operation)

    def test_tools_call_sent_anyway_refuses_absence_evidence(self):
        for operation in UNLISTED_OPERATIONS:
            for mode in ("invocation-retained", "invocation-with-null-result", "result-retained"):
                with self.subTest(operation=operation, mode=mode):
                    reference = observations(operation)["cases"][-1]
                    value = inspector_observations(operation); item = value["cases"][-1]
                    if mode == "result-retained":
                        item["result"] = reference["result"]
                    else:
                        item["wire"]["invocation"] = copy.deepcopy(reference["wire"]["invocation"])
                        # Distinct request ids: refusal must come from the
                        # unlisted tool having been invoked, not an id clash.
                        item["wire"]["discovery"] = discovery_pages(item["wire"], [DEFAULT_ANONYMOUS], first_id=7)
                        if mode == "invocation-retained": item["result"] = reference["result"]
                    rebind(value, item)
                    self.refused(value, operation)

    def test_inspector_exit_or_diagnostic_differences_refuse(self):
        modes = ("exit-0", "exit-1", "exit-3", "signal", "timeout", "stdout", "code", "other-tool", "message", "plain-stderr", "extra-field")
        for operation in UNLISTED_OPERATIONS:
            for mode in modes:
                with self.subTest(operation=operation, mode=mode):
                    value = inspector_observations(operation); item = value["cases"][-1]; client = item["client"]
                    tool = value["tool"]; other = "honua_geocode_address"
                    if mode.startswith("exit-"): client["exitCode"] = int(mode[5:])
                    elif mode == "signal": client["exitSignal"] = "SIGTERM"
                    elif mode == "timeout": client["timedOut"] = True
                    elif mode == "stdout": client["stdout"] = "{}"
                    elif mode == "code": client["stderr"] = json.dumps({"error": {"code": "error", "message": f"Tool '{tool}' not found on server."}})
                    elif mode == "other-tool": client["stderr"] = json.dumps({"error": {"code": "tool_not_found", "message": f"Tool '{other}' not found on server."}})
                    elif mode == "message": client["stderr"] = json.dumps({"error": {"code": "tool_not_found", "message": "connection refused"}})
                    elif mode == "plain-stderr": client["stderr"] = f"Tool '{tool}' not found on server."
                    else: client["stderr"] = json.dumps({"error": {"code": "tool_not_found", "message": f"Tool '{tool}' not found on server.", "data": {}}})
                    rebind(value, item)
                    if mode in ("signal", "timeout"):
                        # bind_launch normalizes these; record them in both
                        # the client and its launch binding.
                        key, actual = ("exitSignal", "SIGTERM") if mode == "signal" else ("timedOut", True)
                        client[key] = client["launch"][key] = actual
                    self.refused(value, operation)
                    if mode in ("exit-0", "exit-1", "stdout", "code"):
                        # The launch record binds process output: the client
                        # JSON cannot be edited after launch without detection.
                        value = inspector_observations(operation); item = value["cases"][-1]
                        item["client"]["exitCode" if mode.startswith("exit") else "stdout" if mode == "stdout" else "stderr"] = (
                            int(mode[5:]) if mode.startswith("exit") else "{}" if mode == "stdout" else item["client"]["stderr"].replace("tool_not_found", "error"))
                        self.refused(value, operation)

    def test_undeclared_or_mismatched_wire_mode_refuses(self):
        for operation in UNLISTED_OPERATIONS:
            for mode in ("undeclared", "other-mode", "authenticated", "invalid", "incomplete", "no-discovery", "listener", "fresh-session-reuse", "authenticated-initialize"):
                with self.subTest(operation=operation, mode=mode):
                    value = inspector_observations(operation); item = value["cases"][-1]; wire = item["wire"]
                    if mode == "undeclared": wire.pop("refusalEvidence")
                    elif mode == "other-mode": wire["refusalEvidence"] = "server-refusal"
                    elif mode == "authenticated": item["authenticated"] = True
                    elif mode == "invalid": wire["invalid"] = True
                    elif mode == "incomplete": wire["completed"] = False
                    elif mode == "no-discovery": wire.pop("discovery")
                    elif mode == "listener": wire["listener"] = "http://127.0.0.1:8080/other"
                    elif mode == "fresh-session-reuse":
                        value["cases"][0]["wire"]["initialize"] = copy.deepcopy(wire["initialize"])
                    else: wire["initialize"]["request"]["credentialPresent"] = True
                    if mode != "fresh-session-reuse": rebind(value, item)
                    self.refused(value, operation)


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
