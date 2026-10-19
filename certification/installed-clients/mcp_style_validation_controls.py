"""Authored offline controls only; never qualify an actual client/candidate."""
from __future__ import annotations
import base64
import copy
import json
import struct
from types import SimpleNamespace
import unittest
import zlib

import mcp_style_validation as validation
import mcp_style_state as state
from mcp_render_validation_controls import FIXTURE, bootstrap, runtime, SOURCE, IMAGE
from mcp_map_package_validation_controls import control_wire


def envelope(value, error=False):
    return {"isError": error, "structuredContent": value, "content": [{"type": "text", "text": json.dumps(value)}]}


def authored_png(color=(29, 123, 201, 255)):
    fixture = json.loads(FIXTURE); spec = fixture["mcp"]["render"]
    width, height = spec["width"], spec["height"]
    left, bottom, right, top = spec["bbox"]
    rows = []
    for y in range(height):
        row = bytearray([0]); latitude = top - (y + 0.5) * (top - bottom) / height
        for x in range(width):
            longitude = left + (x + 0.5) * (right - left) / width
            inside = any(a < longitude < c and b < latitude < d for a, b, c, d in (feature["envelope"] for feature in fixture["area"]["features"]))
            row.extend(color if inside else (0, 0, 0, 0))
        rows.append(bytes(row))
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")


def rendered(authored, color=(29, 123, 201, 255)):
    png = authored_png(color); encoded = base64.b64encode(png).decode()
    args = authored["renderArguments"]
    value = {key: copy.deepcopy(args[key]) for key in ("width", "height", "bbox", "bboxSrid", "layers")}
    value["layers"][0].update(styleId=authored["styles"][0]["id"], styleRendering="applied")
    value.update(format="image/png", byteLength=len(png), image={"format": "image/png", "width": args["width"], "height": args["height"],
        "byteLength": len(png), "inlined": True, "base64": encoded})
    return {"isError": False, "structuredContent": value, "content": [{"type": "text", "text": "Rendered the authored catalog fill."},
        {"type": "image", "mimeType": "image/png", "data": encoded}]}


def rebind(value):
    for position, entry in enumerate(value["calls"]):
        entry["wire"], entry["client"], entry["result"] = control_wire(entry["method"], entry["params"], entry["result"], position, entry["authenticated"])
    return value


def controls():
    authored = validation.scenario(FIXTURE, bootstrap(), "abcdef123456", "Official MCP TypeScript SDK")
    a, b = authored["styles"]; address = authored["address"]
    def got():
        return envelope({"styleId": a["id"], "styleVersion": 1, "encodings": [{"encoding": key, "mediaType": media,
            **({"inlineBody": json.dumps(a["document"])} if key == "mapbox-style" else {"storageRef": "honua://styles/" + a["id"]})}
            for key, media in validation.ENCODINGS.items()]})
    def applied(dry=False):
        return envelope({"status": "Completed", "operationId": "style.apply-preset", "operationInstanceId": "controlled-style-op",
            "handleId": "controlled-style-op", "correlationId": "controlled-correlation", **address,
            "styleId": (b if dry else a)["id"], "styleVersion": 1, "dryRun": dry, "applied": not dry})
    def error(code):
        return envelope({"status": "error", "code": code, "message": "Controlled authored refusal"}, True)
    denial = error("unauthenticated")
    denial["structuredContent"].update(requiresReauthentication=True, error={"kind": "AuthorizationDenied", "message": "Controlled authored refusal"})
    denial["content"][0]["text"] = json.dumps(denial["structuredContent"])
    selected = [("catalog", "honua_get_style", {}, True, envelope({"styles": [{"styleId": style["id"], "uri": "honua://styles/" + style["id"]} for style in (a, b)]})),
        ("direct", "honua_get_style", {"styleId": a["id"], "includeStylesheet": True}, True, got()),
        ("dry-run", "honua_apply_style_preset", {**address, "styleId": b["id"], "dryRun": True}, True, applied(True)),
        ("apply", "honua_apply_style_preset", {**address, "styleId": a["id"]}, True, applied()),
        ("fresh", "honua_get_style", {**address, "includeStylesheet": True}, True, got()),
        ("render", "honua_render_map", authored["renderArguments"], True, rendered(authored)),
        ("replay", "honua_apply_style_preset", {**address, "styleId": a["id"]}, True, applied()),
        ("unknown-style", "honua_apply_style_preset", {**address, "styleId": a["id"] + "-unknown"}, True, error("invalid_argument")),
        ("invalid-encoding", "honua_get_style", {"styleId": a["id"], "encoding": "unknown"}, True, error("invalid_argument")),
        ("unknown-layer", "honua_apply_style_preset", {**address, "layerId": 2147483647, "styleId": a["id"]}, True, error("not_found")),
        ("anonymous-get", "honua_get_style", {"styleId": a["id"], "includeStylesheet": True}, False, copy.deepcopy(denial)),
        ("anonymous-apply", "honua_apply_style_preset", {**address, "styleId": b["id"]}, False, copy.deepcopy(denial)),
        ("restarted-fresh", "honua_get_style", {**address, "includeStylesheet": True}, True, got()),
        ("restarted-render", "honua_render_map", authored["renderArguments"], True, rendered(authored))]
    calls = [{"id": identifier, "method": "tools/call", "params": {"name": name, "arguments": args}, "authenticated": auth, "result": result}
             for identifier, name, args, auth, result in selected]
    initial = {"schema": "honua.mcp-style-persisted/v1", "styles": [{"styleId": style["id"], "document": style["document"], "version": 1} for style in (a, b)],
        "refs": [], "rows": [{key: row[key] for key in ("gid", "name", "envelope")} | {"srid": 4326, "geometry": {"type": "Polygon", "coordinates": [[
            [row["envelope"][0], row["envelope"][1]], [row["envelope"][0], row["envelope"][3]],
            [row["envelope"][2], row["envelope"][3]], [row["envelope"][2], row["envelope"][1]],
            [row["envelope"][0], row["envelope"][1]]]]}} for row in json.loads(FIXTURE)["area"]["features"]],
        "mappings": [{key: value for key, value in authored["ownedArea"].items() if key != "serviceName"} | {"revision": 45, "styleResourceIds": [], "styleResources": []}]}
    committed = copy.deepcopy(initial)
    committed["refs"] = [{"styleId": a["id"], "ordinal": 0}]
    committed["mappings"][0].update(revision=46, styleResourceIds=["style-" + a["id"]], styleResources=[{"id": "style-" + a["id"], "name": a["id"],
        "style": {"styleVersion": 1, "encodings": [{"encoding": "mapbox-style", "contentType": "application/vnd.mapbox.style+json", "body": json.dumps(a["document"])}]}}])
    snapshots = {name: copy.deepcopy(initial if name in ("initial", "dry-run") else committed) for name in
        ("initial", "dry-run", "applied", "readback", "replay", "unknown-style", "invalid-encoding", "unknown-layer", "anonymous-get", "anonymous-apply", "restarted", "restarted-readback")}
    restarted = runtime(); restarted["startedAt"] = "2026-10-19T00:05:00Z"
    return rebind({"schema": validation.SCHEMA, "nonce": authored["nonce"], "canonicalClient": authored["canonicalClient"], "clientVersion": "1.32.1",
        "candidateSourceSha": SOURCE, "candidateImage": IMAGE, "setup": [{"id": style["id"], "method": "POST", "path": "/ogc/styles", "document": style["document"],
            "status": 201, "location": "http://127.0.0.1:8080/ogc/styles/" + style["id"], "body": {"id": style["id"]}} for style in (a, b)],
        "calls": calls, "snapshots": snapshots, "runtimeBefore": runtime(), "runtimeRestarted": restarted, "runtimeAfter": copy.deepcopy(restarted)})


def change_output(entry, **values):
    entry["result"]["structuredContent"].update(**values)
    entry["result"]["content"][0]["text"] = json.dumps(entry["result"]["structuredContent"])


class StyleControls(unittest.TestCase):
    def test_complete_authored_journey(self):
        proof = validation.judge(FIXTURE, bootstrap(), controls())
        self.assertEqual(validation.OPERATIONS, proof["operations"])
        self.assertTrue(proof["coloredRender"] and proof["negativeNonmutation"] and proof["restartReadback"])

    def test_independent_state_and_semantic_mutations_reject_with_wire_rebound(self):
        mutations = {
            "wrong-created-id": lambda x: x["setup"][0]["body"].update(id="replacement"),
            "false-created-status": lambda x: x["setup"][0].update(status=200),
            "wrong-inline-body": lambda x: x["calls"][1]["result"]["structuredContent"]["encodings"][0].update(inlineBody='{"version":8,"layers":[]}'),
            "wrong-service": lambda x: change_output(x["calls"][3], serviceId="another-service"),
            "physical-id-as-public": lambda x: change_output(x["calls"][3], layerId=17),
            "approval-pending": lambda x: change_output(x["calls"][3], status="RequiresApproval"),
            "reconciliation-pending": lambda x: change_output(x["calls"][3], warning="reconciliation pending"),
            "non-durable-handle": lambda x: change_output(x["calls"][3], durable=False),
            "dry-run-claimed-applied": lambda x: change_output(x["calls"][2], applied=True),
            "dry-run-mutated-catalog": lambda x: x["snapshots"]["dry-run"]["styles"][0].update(version=2),
            "dry-run-mutated-ref": lambda x: x["snapshots"]["dry-run"]["refs"].append({"styleId": x["setup"][1]["id"], "ordinal": 0}),
            "lost-ref": lambda x: x["snapshots"]["applied"].update(refs=[]),
            "duplicate-primary": lambda x: x["snapshots"]["applied"]["refs"].append({"styleId": "unexpected", "ordinal": 0}),
            "wrong-graph-body": lambda x: x["snapshots"]["applied"]["mappings"][0]["styleResources"][0]["style"]["encodings"][0].update(body='{}'),
            "unreconciled-graph": lambda x: x["snapshots"]["applied"]["mappings"][0].update(styleResourceIds=[]),
            "ambiguous-mapping": lambda x: x["snapshots"]["applied"]["mappings"].append(copy.deepcopy(x["snapshots"]["applied"]["mappings"][0])),
            "changed-feature": lambda x: x["snapshots"]["restarted"]["rows"][0].update(name="changed"),
            "replay-mutated-association": lambda x: x["snapshots"]["replay"]["refs"].append({"styleId": "unexpected", "ordinal": 1}),
            "same-bbox-distorted-polygon": lambda x: x["snapshots"]["unknown-style"]["rows"][0]["geometry"].update(coordinates=[[[-90, 0], [-90, 60], [-45, 0], [-90, 0]]]),
            "read-only-revision-drift": lambda x: x["snapshots"]["readback"]["mappings"][0].update(revision=47),
            "refusal-revision-drift": lambda x: x["snapshots"]["unknown-style"]["mappings"][0].update(revision=47),
            "anonymous-revision-drift": lambda x: x["snapshots"]["anonymous-get"]["mappings"][0].update(revision=47),
            "restart-read-revision-drift": lambda x: x["snapshots"]["restarted-readback"]["mappings"][0].update(revision=47),
            "restart-revision-drift": lambda x: x["snapshots"]["restarted"]["mappings"][0].update(revision=47),
            "refusal-mutated-state": lambda x: x["snapshots"]["unknown-style"]["styles"][0].update(version=2),
            "anonymous-hidden-mutation": lambda x: x["snapshots"]["anonymous-apply"]["refs"].append({"styleId": x["setup"][1]["id"], "ordinal": 1}),
            "wrong-color": lambda x: x["calls"][5].update(result=rendered(validation.scenario(FIXTURE, bootstrap(), x["nonce"], x["canonicalClient"]), (211, 71, 41, 255))),
            "missing-actual-restart": lambda x: x.update(runtimeRestarted=runtime(), runtimeAfter=runtime()),
            "wrong-container": lambda x: x["runtimeAfter"].update(containerId="a" * 64),
            "missing-census": lambda x: x["snapshots"].pop("anonymous-get"),
            "anonymous-success": lambda x: x["calls"][10].update(result=copy.deepcopy(x["calls"][1]["result"])),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                value = controls(); mutate(value); rebind(value)
                with self.assertRaises((ValueError, KeyError)):
                    validation.judge(FIXTURE, bootstrap(), value)

    def test_only_successful_replay_may_advance_graph_revision(self):
        value = controls()
        for name in ("replay", "unknown-style", "invalid-encoding", "unknown-layer", "anonymous-get", "anonymous-apply", "restarted", "restarted-readback"):
            value["snapshots"][name]["mappings"][0]["revision"] = 47
        self.assertTrue(validation.judge(FIXTURE, bootstrap(), value)["negativeNonmutation"])
        value["snapshots"]["anonymous-apply"]["mappings"][0]["revision"] = 48
        with self.assertRaisesRegex(ValueError, "metadata revision"):
            validation.judge(FIXTURE, bootstrap(), value)

    def test_missing_observation_wrong_session_auth_and_body_refuse(self):
        for name in ("missing", "session", "auth", "response", "wrong-uri"):
            with self.subTest(name=name):
                value = controls(); entry = value["calls"][10]
                if name == "missing": entry["wire"] = None
                elif name == "session": entry["wire"]["invocation"]["request"]["sessionSha256"] = value["calls"][0]["wire"]["initialize"]["response"]["sessionSha256"]
                elif name == "auth": entry["wire"]["initialize"]["request"]["credentialPresent"] = True
                elif name == "response": entry["wire"]["invocation"]["response"]["bodySha256"] = "sha256:" + "0" * 64
                else: entry["wire"]["upstream"] = "http://127.0.0.1:8080/unrelated"
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), value)

    def test_state_query_is_one_snapshot_and_keeps_duplicate_mappings(self):
        value = controls(); authored = validation.scenario(FIXTURE, bootstrap(), value["nonce"], value["canonicalClient"])
        queries = []
        def execute(sql):
            queries.append(sql); return json.dumps(value["snapshots"]["applied"])
        self.assertEqual(value["snapshots"]["applied"], state.snapshot(FIXTURE, authored, execute=execute))
        self.assertEqual(1, len(queries)); sql = queries[0]
        self.assertNotIn("DISTINCT", sql); self.assertEqual(1, sql.count("WITH mappings"))
        self.assertIn("'esri-feature-layer'", sql)
        self.assertIn("metadata_v2_current", sql); self.assertIn("honua.layer_style_refs", sql)
        for forbidden in ("encrypted_connection_string", "PGPASSWORD", "INSERT ", "UPDATE ", "DELETE "):
            self.assertNotIn(forbidden, sql)
        duplicate = copy.deepcopy(value["snapshots"]["applied"]); duplicate["mappings"] *= 2
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            state.snapshot(FIXTURE, authored, execute=lambda _: json.dumps(duplicate))

    def test_database_credential_environment_is_isolated(self):
        invocations = []
        def run(args, **kwargs):
            invocations.append((args, kwargs)); return SimpleNamespace(returncode=0, stdout="{}");
        self.assertEqual("{}", state.database_query("SELECT 1", "private-fixture-secret", run=run))
        args, options = invocations[0]
        self.assertIn("127.0.0.1", args)
        self.assertEqual({"PATH", "PGPASSWORD", "PGSSLMODE", "PGCONNECT_TIMEOUT"}, set(options["env"]))
        self.assertNotIn("private-fixture-secret", " ".join(args))


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--emit":
        from pathlib import Path
        Path(sys.argv[2]).write_text(json.dumps({"bootstrap": bootstrap(), "observations": controls()}))
    else:
        unittest.main()
