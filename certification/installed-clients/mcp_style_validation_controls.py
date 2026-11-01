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


def catalog_record(identifier, document, version=1):
    return {"styleId": identifier, "title": identifier, "description": None, "document": copy.deepcopy(document),
        "drawingInfo": None, "version": version, "createdAt": "2026-10-19T00:01:00+00:00", "updatedAt": "2026-10-19T00:01:00+00:00",
        "revisedBy": None, "changeSummary": None}


def rebind(value):
    for position, entry in enumerate(value["calls"]):
        entry["wire"], entry["client"], entry["result"] = control_wire(entry["method"], entry["params"], entry["result"], position, entry["authenticated"])
    return value


def controls():
    authored = validation.scenario(FIXTURE, bootstrap(), "abcdef123456", "Official MCP TypeScript SDK")
    a, b = authored["styles"]; address = authored["address"]
    def got():
        return envelope({"styleId": a["id"], "title": a["id"], "styleVersion": 1, "encodings": [{"encoding": key, "mediaType": media,
            **({"inlineBody": json.dumps(a["document"])} if key == "mapbox-style" else {"storageRef": "honua://styles/" + a["id"]})}
            for key, media in validation.ENCODINGS.items()]})
    def applied(dry=False):
        return envelope({"status": "Completed", "operationId": "style.apply-preset", "operationInstanceId": "controlled-style-op",
            "handleId": "controlled-style-op", "correlationId": "controlled-correlation", **address,
            "styleId": (b if dry else a)["id"], "title": (b if dry else a)["id"], "styleVersion": 1, "dryRun": dry, "applied": not dry})
    def error(code):
        return envelope({"status": "error", "code": code, "message": "Controlled authored refusal"}, True)
    denial = error("unauthenticated")
    denial["structuredContent"].update(requiresReauthentication=True, error={"kind": "AuthorizationDenied", "message": "Controlled authored refusal"})
    denial["content"][0]["text"] = json.dumps(denial["structuredContent"])
    selected = [("catalog", "honua_get_style", {}, True, envelope({"styles": [{"styleId": style["id"], "title": style["id"], "uri": "honua://styles/" + style["id"]} for style in (a, b)]})),
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
    initial = {"schema": "honua.mcp-style-persisted/v1", "styles": [catalog_record(style["id"], style["document"]) for style in (a, b)],
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


def bind_catalog_control(value):
    # Independent offline stored fixture records are all advertised by the real
    # source contract. Keep controlled raw text/wire/client bindings coherent.
    value["calls"][0]["result"] = envelope({"styles": [
        {"styleId": row["styleId"], "title": row["title"], "uri": "honua://styles/" + row["styleId"]}
        for row in value["snapshots"]["initial"]["styles"]]})
    return rebind(value)


def existing_primary_controls():
    value = controls()
    identifier = "previous-catalog-style"
    document = {"version": 8, "sources": {}, "layers": [{"id": "old-fill", "type": "fill", "paint": {"fill-color": "#00aa11"}}]}
    resource = {"id": "style-" + identifier, "name": identifier, "style": {"styleVersion": 7, "encodings": [
        {"encoding": "mapbox-style", "contentType": "application/vnd.mapbox.style+json", "body": json.dumps(document)}]}}
    for name, snapshot in value["snapshots"].items():
        snapshot["styles"].append(catalog_record(identifier, document, 7))
        snapshot["styles"].sort(key=lambda row: row["styleId"])
        initial = name in ("initial", "dry-run")
        snapshot["refs"].append({"styleId": identifier, "ordinal": 0 if initial else 1})
        snapshot["mappings"][0]["styleResourceIds"].append(resource["id"])
        snapshot["mappings"][0]["styleResources"].append(copy.deepcopy(resource))
        snapshot["mappings"][0]["styleResources"].sort(key=lambda row: row["id"])
    return bind_catalog_control(value)


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
            "malformed-handle-persistence": lambda x: change_output(x["calls"][3], durable="false"),
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

    def test_ephemeral_handle_does_not_erase_real_persisted_binding_proof(self):
        value = controls()
        for index in (2, 3, 6):
            change_output(value["calls"][index], durable=False)
        proof = validation.judge(FIXTURE, bootstrap(), rebind(value))
        self.assertEqual({"dry-run": False, "apply": False, "replay": False}, proof["operationHandleDeclaredDurability"])
        self.assertTrue(proof["persistedReadback"] and proof["restartReadback"])
        value["snapshots"]["restarted"]["refs"] = []
        with self.assertRaises(ValueError):
            validation.judge(FIXTURE, bootstrap(), value)

    def test_handle_durability_disclosure_is_boolean_if_present(self):
        for declaration in (True, False):
            value = controls(); change_output(value["calls"][3], durable=declaration)
            proof = validation.judge(FIXTURE, bootstrap(), rebind(value))
            self.assertIs(proof["operationHandleDeclaredDurability"]["apply"], declaration)
        for malformed in (None, 0, 1, "false", {}, []):
            with self.subTest(malformed=malformed):
                value = controls(); change_output(value["calls"][3], durable=malformed)
                with self.assertRaises(ValueError):
                    validation.judge(FIXTURE, bootstrap(), rebind(value))

    def test_only_successful_replay_may_advance_graph_revision(self):
        value = controls()
        for name in ("replay", "unknown-style", "invalid-encoding", "unknown-layer", "anonymous-get", "anonymous-apply", "restarted", "restarted-readback"):
            value["snapshots"][name]["mappings"][0]["revision"] = 47
        self.assertTrue(validation.judge(FIXTURE, bootstrap(), value)["negativeNonmutation"])
        value["snapshots"]["anonymous-apply"]["mappings"][0]["revision"] = 48
        with self.assertRaisesRegex(ValueError, "metadata revision"):
            validation.judge(FIXTURE, bootstrap(), value)

    def test_referenced_secondary_graph_resources_are_complete_and_catalog_bound(self):
        self.assertTrue(validation.judge(FIXTURE, bootstrap(), existing_primary_controls())["persistedReadback"])
        for mutation in ("missing-secondary", "duplicate-secondary", "wrong-secondary-body", "wrong-secondary-version", "wrong-secondary-name", "unexpected-resource"):
            with self.subTest(mutation=mutation):
                value = existing_primary_controls()
                # Change all post-apply checkpoints consistently so equality
                # alone cannot catch the incomplete/wrong graph projection.
                for name, snapshot in value["snapshots"].items():
                    if name in ("initial", "dry-run"): continue
                    mapping = snapshot["mappings"][0]
                    selected = next(row for row in mapping["styleResources"] if row["name"] == "previous-catalog-style")
                    if mutation == "missing-secondary": mapping["styleResources"].remove(selected)
                    elif mutation == "duplicate-secondary": mapping["styleResources"].append(copy.deepcopy(selected))
                    elif mutation == "wrong-secondary-body": selected["style"]["encodings"][0]["body"] = '{}'
                    elif mutation == "wrong-secondary-version": selected["style"]["styleVersion"] = 8
                    elif mutation == "wrong-secondary-name": selected["name"] = "other-catalog-style"
                    else: mapping["styleResources"].append({**copy.deepcopy(selected), "id": "style-unreferenced"})
                with self.assertRaisesRegex(ValueError, "referenced"):
                    validation.judge(FIXTURE, bootstrap(), value)

    def test_successful_apply_preserves_existing_catalog_and_associations(self):
        for mutation in ("coherent-drop", "coherent-body-mutation", "metadata-mutation", "wrong-demotion"):
            with self.subTest(mutation=mutation):
                value = existing_primary_controls()
                for name, snapshot in value["snapshots"].items():
                    if name in ("initial", "dry-run"): continue
                    legacy = next(row for row in snapshot["styles"] if row["styleId"] == "previous-catalog-style")
                    graph = next(row for row in snapshot["mappings"][0]["styleResources"] if row["name"] == "previous-catalog-style")
                    if mutation == "coherent-drop":
                        snapshot["styles"].remove(legacy)
                        snapshot["refs"] = [row for row in snapshot["refs"] if row["styleId"] != "previous-catalog-style"]
                        snapshot["mappings"][0]["styleResourceIds"].remove("style-previous-catalog-style")
                        snapshot["mappings"][0]["styleResources"].remove(graph)
                    elif mutation == "coherent-body-mutation":
                        legacy["document"]["layers"][0]["paint"]["fill-color"] = "#123456"
                        graph["style"]["encodings"][0]["body"] = json.dumps(legacy["document"])
                    elif mutation == "metadata-mutation": legacy["description"] = "unexpected catalog edit"
                    else: snapshot["refs"][1]["ordinal"] = 3
                with self.assertRaisesRegex(ValueError, "catalog records|prior associations"):
                    validation.judge(FIXTURE, bootstrap(), value)

    def test_apply_preserves_existing_alternate_ordinal_and_demotes_after_maximum(self):
        value = existing_primary_controls()
        document = {"version": 8, "layers": []}
        for name, snapshot in value["snapshots"].items():
            snapshot["styles"].append(catalog_record("retained-alternate", document, 3))
            snapshot["styles"].sort(key=lambda row: row["styleId"])
            initial = name in ("initial", "dry-run")
            if not initial: next(row for row in snapshot["refs"] if row["styleId"] == "previous-catalog-style")["ordinal"] = 5
            snapshot["refs"].append({"styleId": "retained-alternate", "ordinal": 4})
            snapshot["refs"].sort(key=lambda row: (row["ordinal"], row["styleId"]))
            snapshot["mappings"][0]["styleResourceIds"] = ["style-" + row["styleId"] for row in snapshot["refs"]]
            snapshot["mappings"][0]["styleResources"].append({"id": "style-retained-alternate", "name": "retained-alternate",
                "style": {"styleVersion": 3, "encodings": [{"encoding": "mapbox-style", "contentType": "application/vnd.mapbox.style+json", "body": json.dumps(document)}]}})
            snapshot["mappings"][0]["styleResources"].sort(key=lambda row: row["id"])
        bind_catalog_control(value)
        self.assertTrue(validation.judge(FIXTURE, bootstrap(), value)["persistedReadback"])
        for name, snapshot in value["snapshots"].items():
            if name in ("initial", "dry-run"): continue
            next(row for row in snapshot["refs"] if row["styleId"] == "retained-alternate")["ordinal"] = 2
        with self.assertRaisesRegex(ValueError, "prior associations"):
            validation.judge(FIXTURE, bootstrap(), value)

    def test_apply_preserves_unrelated_catalog_records(self):
        value = controls()
        for snapshot in value["snapshots"].values():
            snapshot["styles"].append(catalog_record("unrelated-retained", {"version": 8, "layers": []}, 2))
            snapshot["styles"].sort(key=lambda row: row["styleId"])
        bind_catalog_control(value)
        self.assertTrue(validation.judge(FIXTURE, bootstrap(), value)["persistedReadback"])
        for name, snapshot in value["snapshots"].items():
            if name not in ("initial", "dry-run"):
                snapshot["styles"] = [row for row in snapshot["styles"] if row["styleId"] != "unrelated-retained"]
        with self.assertRaisesRegex(ValueError, "catalog records"):
            validation.judge(FIXTURE, bootstrap(), value)

    def test_first_application_requires_a_new_graph_revision(self):
        for delta in (0, -1):
            with self.subTest(delta=delta):
                value = controls()
                revision = value["snapshots"]["initial"]["mappings"][0]["revision"] + delta
                for name, snapshot in value["snapshots"].items():
                    if name not in ("initial", "dry-run"): snapshot["mappings"][0]["revision"] = revision
                with self.assertRaisesRegex(ValueError, "advance the committed graph revision"):
                    validation.judge(FIXTURE, bootstrap(), value)
        value = controls()
        for name, snapshot in value["snapshots"].items():
            if name not in ("initial", "dry-run"): snapshot["mappings"][0]["revision"] += 7
        self.assertTrue(validation.judge(FIXTURE, bootstrap(), value)["persistedReadback"])

    def test_catalog_locale_order_and_nonprimary_ordinal_ties_are_not_semantic(self):
        value = existing_primary_controls()
        identifiers = ("a_style", "A-style", "a-style")
        for name, snapshot in value["snapshots"].items():
            initial = name in ("initial", "dry-run")
            if not initial:
                next(row for row in snapshot["refs"] if row["styleId"] == "previous-catalog-style")["ordinal"] = 5
            for identifier in identifiers:
                document = {"version": 8, "name": identifier, "layers": []}
                snapshot["styles"].append(catalog_record(identifier, document, 3))
                snapshot["refs"].append({"styleId": identifier, "ordinal": 4})
                snapshot["mappings"][0]["styleResources"].append({"id": "style-" + identifier, "name": identifier,
                    "style": {"styleVersion": 3, "encodings": [{"encoding": "mapbox-style", "contentType": "application/vnd.mapbox.style+json", "body": json.dumps(document)}]}})
            snapshot["styles"].sort(key=lambda row: row["styleId"], reverse=name == "readback")
            if name == "readback": snapshot["refs"].reverse()
            snapshot["refs"].sort(key=lambda row: row["ordinal"])
            snapshot["mappings"][0]["styleResourceIds"] = ["style-" + row["styleId"] for row in snapshot["refs"]]
        bind_catalog_control(value)
        self.assertTrue(validation.judge(FIXTURE, bootstrap(), value)["persistedReadback"])
        for mutation in ("graph-precedence", "association-precedence", "duplicate-catalog", "missing-ref", "changed-ref"):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(value)
                for name, snapshot in changed["snapshots"].items():
                    if name in ("initial", "dry-run"): continue
                    if mutation == "graph-precedence":
                        ids = snapshot["mappings"][0]["styleResourceIds"]; ids[0], ids[-1] = ids[-1], ids[0]
                    elif mutation == "association-precedence": snapshot["refs"][0], snapshot["refs"][-1] = snapshot["refs"][-1], snapshot["refs"][0]
                    elif mutation == "duplicate-catalog": snapshot["styles"].append(copy.deepcopy(snapshot["styles"][0]))
                    elif mutation == "missing-ref": snapshot["refs"].pop()
                    else: snapshot["refs"][-1]["ordinal"] = 6
                with self.assertRaises(ValueError): validation.judge(FIXTURE, bootstrap(), changed)

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
        self.assertIn("'geoservices-rest-feature-layer'", sql)
        self.assertIn("metadata_v2_current", sql); self.assertIn("honua.layer_style_refs", sql)
        for forbidden in ("encrypted_connection_string", "PGPASSWORD", "INSERT ", "UPDATE ", "DELETE "):
            self.assertNotIn(forbidden, sql)
        duplicate = copy.deepcopy(value["snapshots"]["applied"]); duplicate["mappings"] *= 2
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            state.snapshot(FIXTURE, authored, execute=lambda _: json.dumps(duplicate))

    def test_discovery_catalog_matches_complete_independent_census(self):
        for mutation in ("missing", "duplicate", "unknown", "title", "uri"):
            with self.subTest(mutation=mutation):
                value = existing_primary_controls()
                rows = value["calls"][0]["result"]["structuredContent"]["styles"]
                if mutation == "missing": rows.pop()
                elif mutation == "duplicate": rows[-1] = copy.deepcopy(rows[0])
                elif mutation == "unknown": rows[-1]["styleId"] = "unrelated-style"
                elif mutation == "title": rows[-1]["title"] = "wrong title"
                else: rows[-1]["uri"] = "honua://styles/unrelated-style"
                value["calls"][0]["result"]["content"][0]["text"] = json.dumps(value["calls"][0]["result"]["structuredContent"])
                rebind(value)
                with self.assertRaisesRegex(ValueError, "discovery catalog"):
                    validation.judge(FIXTURE, bootstrap(), value)

    def test_setup_retains_only_validated_header_and_decoded_body(self):
        authored = validation.scenario(FIXTURE, bootstrap(), "abcdef123456", "Official MCP TypeScript SDK")
        credential = "private-setup-fixture-secret"
        class Connection:
            def __init__(self, response): self.response = response
            def request(self, *args, **kwargs): pass
            def getresponse(self): return self.response
            def close(self): pass
        for mutation in ("clean", "location-echo", "location-encoded", "raw-body-echo", "escaped-body-value", "escaped-body-key", "encoded-body-link", "double-encoded-body-link", "overbound-body-link", "wrong-id", "rejected-status"):
            with self.subTest(mutation=mutation):
                responses = []
                for style in authored["styles"]:
                    location = "http://127.0.0.1:8080/ogc/styles/" + style["id"]
                    body = {"id": style["id"], "links": [{"href": location}]}
                    status = 201
                    if mutation == "location-echo": location += "?credential=" + credential
                    elif mutation == "location-encoded": location += "?credential=" + "".join("%" + format(ord(char), "02X") for char in credential)
                    elif mutation in ("raw-body-echo", "escaped-body-value"): body["links"].append({"nested": [credential]})
                    elif mutation == "escaped-body-key": body[credential] = "unsafe key"
                    elif mutation in ("encoded-body-link", "double-encoded-body-link", "overbound-body-link"):
                        encoded = "".join("%" + format(ord(char), "02X") for char in credential)
                        if mutation == "double-encoded-body-link": encoded = encoded.replace("%", "%25")
                        if mutation == "overbound-body-link":
                            encoded = "safe"
                            for _ in range(6): encoded = "".join("%" + format(ord(char), "02X") for char in encoded)
                        body["links"][0]["href"] += "?credential=" + encoded
                    elif mutation == "wrong-id": body["id"] = "another-style"
                    elif mutation == "rejected-status": status = 500
                    raw = json.dumps(body).encode()
                    if mutation.startswith("escaped-body"):
                        raw = raw.replace(credential.encode(), "".join("\\u" + format(ord(char), "04x") for char in credential).encode())
                        self.assertNotIn(credential.encode(), raw)
                    responses.append(SimpleNamespace(status=status, getheader=lambda name, value=location: value, read=lambda limit, data=raw: data))
                receipt = []
                connect = lambda *args, **kwargs: Connection(responses.pop(0))
                if mutation == "clean":
                    state.create_styles(authored, credential, receipt, connect=connect)
                    self.assertEqual(2, len(receipt)); self.assertTrue(all(entry["body"] is not None and entry["location"] for entry in receipt))
                else:
                    with self.assertRaises(ValueError): state.create_styles(authored, credential, receipt, connect=connect)
                    self.assertEqual(1, len(receipt)); self.assertIsNone(receipt[0]["body"]); self.assertIsNone(receipt[0]["location"])
                retained = json.dumps(receipt, ensure_ascii=False)
                self.assertNotIn(credential, retained)
                self.assertNotIn("".join("%" + format(ord(char), "02X") for char in credential), retained)

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
