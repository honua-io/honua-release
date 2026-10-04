"""Self-tests for the cross-client interop scenarios: contracts, oracles, seams, runners, wiring."""
import ast
import base64
import contextlib
import copy
import importlib.util
import io
import json
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SUITE = HERE.parent
sys.path[:0] = [str(HERE), str(SUITE)]
import engine  # noqa: E402
import judge  # noqa: E402
import oracles  # noqa: E402
import regression  # noqa: E402

spec = importlib.util.spec_from_file_location("installed_cert_interop", SUITE / "run.py")
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)

FIXTURE = judge.load_fixture()
SUITE_FIXTURE = regression.load_fixture()
SCENARIOS = {sid: scenario for sid, scenario in regression.load_scenarios().items() if sid.startswith("interop-")}
MATRIX = json.loads((SUITE / "matrix.json").read_text())
CELLS = {cell["id"]: cell for cell in MATRIX["cells"] if cell["driver"] == "interop"}
SOURCES = {
    "orchestrator": (HERE / "orchestrator.py").read_text(),
    "honua-sdk-python-wheel": (HERE / "drivers/python/runner.py").read_text(),
    "honua-sdk-js": (HERE / "drivers/js/runner.mjs").read_text(),
    "honua-sdk-dotnet": (HERE / "drivers/dotnet/Program.cs").read_text(),
}
LABELS = {"honua-sdk-python-wheel": "honua-sdk 0.1.12", "honua-sdk-js": "@honua/sdk-js 0.1.12",
          "honua-sdk-dotnet": "Honua.Sdk 1.10.1", "honua-mcp-server": "@honua/mcp-server 0.1.12"}
# The SDK call each SDK step goes through, as it appears in that SDK's runner.
RUNNER_CALLS = {
    "IHonuaAdminClient.PublishLayerAsync": ("honua-sdk-dotnet", "IHonuaAdminClient>().PublishLayerAsync("),
    "honua_sdk.GeoServicesFeatureServerClient.query": ("honua-sdk-python-wheel", ".feature_server(args[\"service\"]).query("),
    "honua_sdk.GeoServicesFeatureServerClient.query(extra_params returnIdsOnly)": (
        "honua-sdk-python-wheel", 'extra_params={"returnIdsOnly": "true"}'),
    "honua_sdk.HonuaClient.query(return_count_only=True)": ("honua-sdk-python-wheel", "return_count_only=True"),
    "HonuaFeatureLayer.applyEdits(updates)": ("honua-sdk-js", ".applyEdits({\n    updates:"),
    "IHonuaFeatureServerClient.QueryAsync": ("honua-sdk-dotnet", "IHonuaFeatureServerClient>().QueryAsync("),
    "honua_sdk.HonuaGeoprocessing.job": ("honua-sdk-python-wheel", "gp.job(args[\"jobId\"])"),
    "honua_sdk.HonuaGeoprocessing.results": ("honua-sdk-python-wheel", ".geoprocessing().results(args[\"jobId\"])"),
    "HonuaStudioLifecycleClient.drafts.create": ("honua-sdk-js", "studio.drafts.create("),
    "HonuaStudioLifecycleClient.drafts.createContentVersion": ("honua-sdk-js", "studio.drafts.createContentVersion("),
    "HonuaStudioLifecycleClient.publicationRequests.create": ("honua-sdk-js", "studio.publicationRequests.create("),
    "HonuaStudioLifecycleClient.publicationRequests.poll": ("honua-sdk-js", "studio.publicationRequests.poll("),
    "IHonuaStudioPackageClient.GetContentItemPointersAsync": ("honua-sdk-dotnet", ".GetContentItemPointersAsync("),
    "IHonuaStudioPackageClient.GetVersionAsync": ("honua-sdk-dotnet", ".GetVersionAsync("),
    "(no published Studio route reader in Honua.Sdk)": ("honua-sdk-dotnet", "throw new NotSupportedException("),
    "honua_sdk.HonuaClient(api_key).feature_server(...).query": ("honua-sdk-python-wheel", "HonuaClient(BASE, api_key=handle.read().strip())"),
    "HonuaClient({apiKey}).featureLayer().queryFeatures": ("honua-sdk-js", "new HonuaClient({ baseUrl: base, apiKey: readFileSync(args.secretFile"),
    "IHonuaFeatureServerClient.QueryAsync (HonuaSdkOptions.ApiKey)": ("honua-sdk-dotnet", "identity = Provider("),
}


def png(width, height, painted):
    raw = b"".join(b"\x00" + b"".join(bytes((0, 0, 255, 128)) if painted(x, y) else bytes(4) for x in range(width))
                   for y in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def rounded_buffer(envelope, distance, per_corner=8):
    """The exact buffer of an axis-aligned rectangle: straight sides joined by quarter circles."""
    minx, miny, maxx, maxy = envelope
    ring = []
    for (cx, cy), start in (((maxx, miny), -90), ((maxx, maxy), 0), ((minx, maxy), 90), ((minx, miny), 180)):
        for index in range(per_corner + 1):
            angle = math.radians(start + 90 * index / per_corner)
            ring.append([cx + distance * math.cos(angle), cy + distance * math.sin(angle)])
    return {"type": "Polygon", "coordinates": [ring + [ring[0]]]}


def plan():
    published = {"sites": {"service": "sdkreg-sites", "layerId": 7}}
    principals = {"proposer": {"id": "p-id", "key": "hnua_proposer_secret"}, "approver": {"id": "a-id", "key": "hnua_approver_secret"}}
    return engine.build_plan(FIXTURE, SUITE_FIXTURE, {"connectionId": "conn-1", "tag": "abc123"}, published, sorted(SCENARIOS),
                             SCENARIOS, "http://candidate:8080/", principals=principals)


def obs(scenario, step, **payload):
    api = judge.scenario_step(SCENARIOS[scenario], step)["api"]
    return {"scenario": scenario, "step": step, "api": api, **payload}


def evaluate(cell_id, observations, plan_=None):
    cell = CELLS[cell_id]
    return judge.evaluate_cell(cell, SCENARIOS[cell["scenario"]], observations, FIXTURE, plan_ or plan(), LABELS, regression.scrub)


def handoff_observations():
    sid = "interop-publish-query-edit"
    rows = [{"attributes": {"gid": r["gid"], "name": r["name"], "rank": r["rank"]}, "x": r["x"], "y": r["y"]}
            for r in FIXTURE["handoff"]["features"]]
    edited = [{"attributes": {"gid": r["gid"], "name": r["name"], "rank": r["rank"]}, "x": r["x"], "y": r["y"]}
              for r in judge.edited_handoff(FIXTURE)]
    p = plan()
    return {
        (sid, "publish"): obs(sid, "publish", observed={"serviceName": p["handoff"]["service"], "layerName": "handoff",
                                                        "enabled": True, "layerId": 12}),
        (sid, "query"): obs(sid, "query", observed={"features": rows}),
        (sid, "ids"): obs(sid, "ids", observed={"ids": [1, 2, 3]}),
        (sid, "count"): obs(sid, "count", observed={"count": None}),
        (sid, "edit"): obs(sid, "edit", error={"type": "HonuaHttpError", "status": 400}),
        (sid, "read-back"): obs(sid, "read-back", observed={"features": rows}),
    }, edited


class ContractTests(unittest.TestCase):
    def test_four_scenarios_each_cross_clients_and_hand_state_between_them(self):
        self.assertEqual(set(SCENARIOS), {"interop-publish-query-edit", "interop-import-render-buffer",
                                          "interop-proposal-approval", "interop-api-key-revocation"})
        for sid, scenario in SCENARIOS.items():
            clients = {step["client"] for step in scenario["steps"]}
            self.assertGreaterEqual(len(clients), 3 if sid != "interop-proposal-approval" else 2, sid)
            seams = [step for step in scenario["steps"] if judge.seam(scenario, step, LABELS)]
            self.assertTrue(seams, sid)

    def test_the_scenarios_cover_the_operator_hand_offs(self):
        def clients(sid, step):
            return judge.scenario_step(SCENARIOS[sid], step)["client"], judge.scenario_step(SCENARIOS[sid], step)["api"]
        # (a) .NET publishes -> Python queries (ids, count, ordinates) -> JS edits -> .NET reads back.
        self.assertEqual(clients("interop-publish-query-edit", "publish")[0], "honua-sdk-dotnet")
        self.assertEqual({clients("interop-publish-query-edit", s)[0] for s in ("query", "ids", "count")}, {"honua-sdk-python-wheel"})
        self.assertEqual(clients("interop-publish-query-edit", "edit")[0], "honua-sdk-js")
        self.assertEqual(clients("interop-publish-query-edit", "read-back")[0], "honua-sdk-dotnet")
        # (b) honua CLI imports and publishes -> MCP renders and buffers -> Python reads the job.
        self.assertTrue(clients("interop-import-render-buffer", "import")[1].startswith("honua admin import"))
        self.assertTrue(all(clients("interop-import-render-buffer", s)[1].startswith("honua-mcp-proxy")
                            for s in ("read", "render", "buffer-submit")))
        self.assertEqual(clients("interop-import-render-buffer", "job-result")[0], "honua-sdk-python-wheel")
        # (c) JS proposes -> the admin CLI approves as a separate principal (self-approval refused) -> .NET verifies.
        self.assertEqual(clients("interop-proposal-approval", "request-publication")[0], "honua-sdk-js")
        self.assertIn("--profile proposer", clients("interop-proposal-approval", "self-approval-refused")[1])
        self.assertIn("--profile approver", clients("interop-proposal-approval", "approve")[1])
        self.assertEqual({clients("interop-proposal-approval", s)[0] for s in ("published-pointer", "published-content", "published-url")},
                         {"honua-sdk-dotnet"})
        # (d) one key minted by the admin CLI, used and then refused by all three SDKs and the proxy.
        revocation = SCENARIOS["interop-api-key-revocation"]
        self.assertIn("createAdminApiKey", clients("interop-api-key-revocation", "mint")[1])
        self.assertIn("revokeAdminApiKey", clients("interop-api-key-revocation", "revoke")[1])
        used = {step["client"] for step in revocation["steps"] if step["id"].endswith("-use")}
        refused = {step["client"] for step in revocation["steps"] if step["id"].endswith("-revoked")}
        self.assertEqual(used, refused)
        self.assertEqual(used, {"honua-sdk-python-wheel", "honua-sdk-js", "honua-sdk-dotnet", "honua-mcp-server"})

    def test_orchestrator_reports_exactly_the_contract_apis(self):
        table = ast.literal_eval(re.search(r"^API = (\{.*?^\})$", SOURCES["orchestrator"], re.S | re.M).group(1))
        self.assertEqual(table, {sid: {step["id"]: step["api"] for step in scenario["steps"]} for sid, scenario in SCENARIOS.items()})

    def test_every_sdk_step_goes_through_the_named_call_in_its_client_runner(self):
        for sid, scenario in SCENARIOS.items():
            for step in scenario["steps"]:
                if step["client"] == "honua-mcp-server" or step["api"].startswith("honua "):
                    # The orchestrator drives the proxy over stdio and the installed honua command line.
                    self.assertTrue(step["api"].startswith(("honua-mcp-proxy ", "honua admin ")), (sid, step["id"]))
                    continue
                client, call = RUNNER_CALLS[step["api"]]
                self.assertEqual(client, step["client"], (sid, step["id"]))
                self.assertIn(call, SOURCES[client], (sid, step["id"]))

    def test_no_client_program_makes_a_raw_http_call(self):
        network = r"(?m)^\s*(import|from)\s+(httpx|requests|urllib|http\.client|socket|aiohttp)\b|\b(urllib|httpx|requests)\.\w+\("
        self.assertNotRegex(SOURCES["honua-sdk-python-wheel"], network)
        self.assertNotRegex(SOURCES["orchestrator"], network)
        self.assertNotRegex(SOURCES["honua-sdk-js"], r"\bfetch\(|node:https?\b")
        self.assertNotRegex(SOURCES["honua-sdk-dotnet"], r"new HttpClient|GetAsync\(\"|PostAsync\(")

    def test_validation_rejects_contracts_that_do_not_hand_off(self):
        base = copy.deepcopy(SCENARIOS["interop-publish-query-edit"])
        cases = [
            (lambda s: s.update(id="publish-query-edit"), "interop-\\* id"),
            (lambda s: s.update(clients={}), "client per step"),
            (lambda s: s.update(receiptFields=["client"]), "allowlist"),
            (lambda s: s["steps"][1].update(client="honua-admin-python-wheel"), "client artifact"),
            (lambda s: s["steps"][1].update(api=""), "client artifact and API"),
            (lambda s: s["steps"][1].update(oracle="vibes"), "known oracle"),
            (lambda s: s["steps"][1].update(consumes=[{"step": "read-back", "handoff": "layer"}]), "earlier steps"),
            (lambda s: s["steps"][1].update(consumes=[{"step": "publish"}]), "named handoff"),
            (lambda s: [step.update(client="honua-sdk-js") for step in s["steps"]], "at least two clients"),
            (lambda s: [step.update(consumes=[]) for step in s["steps"]], "no step consumes"),
            (lambda s: s.update(steps=s["steps"][:1]), "at least two steps"),
        ]
        for change, message in cases:
            with self.subTest(message=message):
                scenario = copy.deepcopy(base)
                change(scenario)
                with self.assertRaisesRegex(judge.InteropError, message):
                    judge.validate_scenario(scenario, "case.json")
        # The suite surfaces an interop contract defect as a regression contract error.
        broken = copy.deepcopy(base)
        broken["steps"][0]["client"] = "nobody"
        with self.assertRaisesRegex(regression.RegressionError, "client artifact"):
            regression.validate_scenario(broken, "case.json")

    def test_cells_start_with_the_producing_client_and_declare_only_contract_steps(self):
        self.assertEqual(set(CELLS), set(SCENARIOS))
        for cell_id, cell in CELLS.items():
            self.assertEqual(cell["artifact"], judge.first_client(SCENARIOS[cell["scenario"]]))
            regression.validate_suite_cell(cell, regression.load_scenarios(), run.BLOCKER)
        wrong = dict(CELLS["interop-import-render-buffer"], artifact="honua-mcp-server")
        with self.assertRaisesRegex(regression.RegressionError, "does not drive"):
            regression.validate_suite_cell(wrong, regression.load_scenarios(), run.BLOCKER)

    def test_matrix_blocks_only_the_observed_seams(self):
        self.assertEqual(CELLS["interop-import-render-buffer"]["status"], "active")
        self.assertEqual(set(CELLS["interop-publish-query-edit"]["blockedSteps"]), {"count", "edit", "read-back"})
        self.assertEqual(set(CELLS["interop-api-key-revocation"]["blockedSteps"]), {"mcp-revoked"})
        approval = CELLS["interop-proposal-approval"]["blockedSteps"]
        self.assertNotIn("create-draft", approval)
        self.assertEqual(approval["published-url"]["blockedBy"], "https://github.com/honua-io/honua-sdk-dotnet/issues/411")
        self.assertTrue(all(entry["blockedBy"] == "https://github.com/honua-io/honua-server/issues/5433"
                            for step, entry in approval.items() if step != "published-url"))


class OracleTests(unittest.TestCase):
    def test_edited_handoff_applies_attributes_and_geometry(self):
        rows = {row["gid"]: row for row in judge.edited_handoff(FIXTURE)}
        edit = FIXTURE["handoff"]["edit"]
        self.assertEqual((rows[2]["name"], rows[2]["rank"], rows[2]["x"], rows[2]["y"]),
                         (edit["attributes"]["name"], edit["attributes"]["rank"], edit["x"], edit["y"]))
        self.assertEqual(len(rows), len(FIXTURE["handoff"]["features"]))

    def test_area_features_need_the_exact_envelope_ring(self):
        feature = FIXTURE["area"]["features"][0]
        ring = [list(point) for point in judge.envelope_ring(feature["envelope"])]
        attributes = [{"gid": feature["gid"], "name": feature["name"]}]

        def observed(open_ring, rows=attributes):
            return {"rings": [open_ring + [open_ring[0]]], "attributes": rows}

        rotated = ring[2:] + ring[:2]
        self.assertTrue(judge.oracle_area_features(observed(rotated), FIXTURE)[0])
        self.assertTrue(judge.oracle_area_features(observed(list(reversed(ring))), FIXTURE)[0])
        # Same four corners, opposite traversal: a bow-tie, not the fixture ring.
        bowtie = [ring[0], ring[2], ring[1], ring[3]]
        self.assertFalse(judge.oracle_area_features(observed(bowtie), FIXTURE)[0])
        shifted = [[x + 0.001, y] for x, y in ring]
        self.assertFalse(judge.oracle_area_features(observed(shifted), FIXTURE)[0])
        self.assertFalse(judge.oracle_area_features({"rings": [ring], "attributes": attributes}, FIXTURE)[0])
        self.assertFalse(judge.oracle_area_features({"rings": [], "attributes": []}, FIXTURE)[0])
        self.assertFalse(judge.oracle_area_features(observed(ring, [{"gid": feature["gid"], "name": "other"}]), FIXTURE)[0])
        self.assertFalse(judge.oracle_area_features({"rings": [ring + [ring[0]]]}, FIXTURE)[0])

    def test_render_oracle_reads_pixels_of_the_imported_polygon(self):
        spec_ = FIXTURE["area"]["render"]
        encode = lambda data: {"png": base64.b64encode(data).decode()}  # noqa: E731
        painted = png(spec_["width"], spec_["height"], lambda x, y: 40 <= x < 220 and 40 <= y < 280)
        self.assertTrue(judge.oracle_area_render(encode(painted), FIXTURE)[0])
        self.assertFalse(judge.oracle_area_render(encode(png(spec_["width"], spec_["height"], lambda x, y: False)), FIXTURE)[0])

    def test_buffer_oracle_measures_every_vertex_from_the_polygon(self):
        envelope, distance = FIXTURE["area"]["features"][0]["envelope"], FIXTURE["area"]["buffer"]["distance"]
        ok, summary = judge.oracle_area_buffer({"geometry": rounded_buffer(envelope, distance)}, FIXTURE)
        self.assertTrue(ok, summary)
        self.assertFalse(judge.oracle_area_buffer({"geometry": rounded_buffer(envelope, distance * 1.01)}, FIXTURE)[0])
        moved = [envelope[0] + 1, envelope[1], envelope[2] + 1, envelope[3]]
        self.assertFalse(judge.oracle_area_buffer({"geometry": rounded_buffer(moved, distance)}, FIXTURE)[0])
        self.assertFalse(judge.oracle_area_buffer({"geometry": None}, FIXTURE)[0])
        # Vertices sit on the offset and the bounds match, but the diagonals cut through the source polygon.
        minx, miny, maxx, maxy = envelope
        cut = [[minx - distance, miny], [maxx, miny - distance], [maxx + distance, maxy], [minx, maxy + distance]]
        self.assertFalse(judge.oracle_area_buffer(
            {"geometry": {"type": "Polygon", "coordinates": [cut + [cut[0]]]}}, FIXTURE)[0])
        # A one-segment fillet is still a buffer: straight sides plus a quarter-circle chord at each corner.
        chamfer = [[maxx, miny - distance], [maxx + distance, miny], [maxx + distance, maxy], [maxx, maxy + distance],
                   [minx, maxy + distance], [minx - distance, maxy], [minx - distance, miny], [minx, miny - distance]]
        self.assertTrue(judge.oracle_area_buffer(
            {"geometry": {"type": "Polygon", "coordinates": [chamfer + [chamfer[0]]]}}, FIXTURE)[0])
        # The exterior is a correct buffer, but a hole removes area from it.
        hole = [[minx, miny], [minx, miny + 1], [minx + 1, miny + 1], [minx, miny]]
        for geometry in ({"type": "Polygon", "coordinates": [chamfer + [chamfer[0]], hole]},
                         {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [chamfer + [chamfer[0]], hole]}}):
            ok, summary = judge.oracle_area_buffer({"geometry": geometry}, FIXTURE)
            self.assertFalse(ok)
            self.assertIn("1 interior ring", summary)

    def test_polygon_wkb_round_trips_the_ring(self):
        ring = [[-90.0, 0.0], [-45.0, 0.0], [-45.0, 60.0], [-90.0, 0.0]]
        data = base64.b64decode(judge.polygon_wkb([ring]))
        self.assertEqual(struct.unpack_from("<BII", data), (1, 3, 1))
        self.assertEqual(struct.unpack_from("<I", data, 9)[0], 4)
        self.assertEqual(struct.unpack_from("<dd", data, 13), (-90.0, 0.0))

    def test_revocation_window_is_the_first_call(self):
        bound = FIXTURE["identity"]["revocation"]["observationSeconds"]
        good = {"refused": True, "status": 499, "succeededAfterRevocation": 0, "refusedAfterSeconds": 0.1,
                "confirmations": [401, 403], "observationSeconds": bound}
        self.assertTrue(judge.oracle_revocation_observed(good, FIXTURE)[0])
        self.assertTrue(judge.oracle_revocation_observed(dict(good, status="permission_denied",
                                                              confirmations=["permission_denied", 499]), FIXTURE)[0])
        self.assertTrue(judge.oracle_revocation_observed(dict(good, refusedAfterSeconds=bound), FIXTURE)[0])
        late = dict(good, succeededAfterRevocation=2)
        self.assertFalse(judge.oracle_revocation_observed(late, FIXTURE)[0])
        self.assertFalse(judge.oracle_revocation_observed(dict(good, confirmations=[401, False]), FIXTURE)[0])
        self.assertFalse(judge.oracle_revocation_observed(dict(good, confirmations=[True, True]), FIXTURE)[0])
        self.assertFalse(judge.oracle_revocation_observed(dict(good, confirmations=[401, 500]), FIXTURE)[0])
        self.assertFalse(judge.oracle_revocation_observed(dict(good, confirmations=[401, None]), FIXTURE)[0])
        self.assertFalse(judge.oracle_revocation_observed(dict(good, status=500), FIXTURE)[0])
        hung = dict(good, refusedAfterSeconds=bound + 30, observationSeconds=999)
        ok, summary = judge.oracle_revocation_observed(hung, FIXTURE)
        self.assertFalse(ok)
        self.assertIn(f"outside the {bound}s observation bound", summary)
        self.assertFalse(judge.oracle_revocation_observed(dict(good, refusedAfterSeconds=None), FIXTURE)[0])
        still = {"refused": False, "succeededAfterRevocation": 120, "observationSeconds": bound}
        ok, summary = judge.oracle_revocation_observed(still, FIXTURE)
        self.assertFalse(ok)
        self.assertIn("still authenticated", summary)
        ok, summary = judge.oracle_revocation_observed({"timedOut": True, "timeoutSeconds": bound, "succeededAfterRevocation": 0}, FIXTURE)
        self.assertFalse(ok)
        self.assertRegex(summary, CELLS["interop-api-key-revocation"]["blockedSteps"]["mcp-revoked"]["signature"])

    def test_identity_oracles(self):
        minted = {"keyId": "k", "status": "active", "permissions": ["admin:write"], "secretWritten": True, "secretPrivate": True}
        self.assertTrue(judge.oracle_key_minted(minted, FIXTURE)[0])
        self.assertFalse(judge.oracle_key_minted(dict(minted, secretPrivate=False), FIXTURE)[0])
        self.assertFalse(judge.oracle_key_minted(dict(minted, permissions=["admin:*"]), FIXTURE)[0])
        self.assertTrue(judge.oracle_key_revoked({"status": "revoked", "revokedAt": "2026-10-04T00:00:00Z"})[0])
        self.assertFalse(judge.oracle_key_revoked({"status": "active", "revokedAt": None})[0])
        self.assertEqual(plan()["identity"]["siteCount"], len(SUITE_FIXTURE["sites"]["features"]))

    def test_publication_oracles(self):
        p = plan()
        saved = {"itemId": "i", "versionId": "v", "contentHash": "a" * 64}
        self.assertTrue(judge.oracle_version_saved(saved, {"itemId": "i"})[0])
        self.assertFalse(judge.oracle_version_saved(saved, {"itemId": "other"})[0])
        self.assertTrue(judge.oracle_publication_proposed({"proposalId": "proposal-1", "requestId": None})[0])
        self.assertFalse(judge.oracle_publication_proposed({"proposalId": None})[0])
        resolved = {"status": "Succeeded", "kind": "StudioDraftMutation", "requestedBy": "x:p-id", "resolvedBy": "x:a-id"}
        self.assertTrue(judge.oracle_proposal_resolved(resolved, p["principals"])[0])
        self.assertFalse(judge.oracle_proposal_resolved(dict(resolved, resolvedBy="x:p-id"), p["principals"])[0])
        self.assertFalse(judge.oracle_proposal_resolved(dict(resolved, kind="ServicePublish"), p["principals"])[0])
        ok, summary = judge.oracle_publication_active({"requestId": None}, p)
        self.assertFalse(ok)
        self.assertIn("cannot be polled", summary)
        self.assertTrue(judge.oracle_publication_active({"requestId": "r", "state": "Active",
                                                         "publicationUrl": "http://c" + p["proposal"]["route"]}, p)[0])
        self.assertTrue(judge.oracle_published_pointer({"publishedVersionId": "v"}, saved)[0])
        self.assertFalse(judge.oracle_published_pointer({"publishedVersionId": None}, saved)[0])
        content = {"family": "Map", "contentHash": "a" * 64, "body": p["proposal"]["envelope"]["body"]}
        self.assertTrue(judge.oracle_published_content(content, FIXTURE, p, saved)[0])
        self.assertFalse(judge.oracle_published_content(dict(content, contentHash="b" * 64), FIXTURE, p, saved)[0])
        self.assertFalse(judge.oracle_published_content(dict(content, body={}), FIXTURE, p, saved)[0])
        polled = {"state": "Active", "publicationUrl": "http://c" + p["proposal"]["route"]}
        self.assertTrue(judge.oracle_published_url(dict(content, url=polled["publicationUrl"]), FIXTURE, p, saved, polled)[0])
        # The .NET reader must consume the URL the JS SDK polled, not rebuild it from the fixture route.
        for url in ("http://other" + p["proposal"]["route"], p["proposal"]["route"], None):
            ok, summary = judge.oracle_published_url(dict(content, url=url), FIXTURE, p, saved, polled)
            self.assertFalse(ok)
            self.assertIn("not the URL the JS SDK polled", summary)
        self.assertFalse(judge.oracle_published_url(dict(content, url=None), FIXTURE, p, saved, None)[0])


class SeamTests(unittest.TestCase):
    def test_a_failing_consumer_names_both_sides_of_the_seam(self):
        observations, _ = handoff_observations()
        status, detail, rows = evaluate("interop-publish-query-edit", observations)
        self.assertEqual(status, "blocked", detail)
        edit = next(row for row in rows if row["step"] == "edit")
        self.assertEqual(edit["client"], "@honua/sdk-js 0.1.12")
        self.assertIn("Honua.Sdk 1.10.1 `IHonuaAdminClient.PublishLayerAsync`", edit["oracle"])
        self.assertIn("-> @honua/sdk-js 0.1.12 `HonuaFeatureLayer.applyEdits(updates)`", edit["oracle"])
        self.assertTrue(all(set(row) <= run.SUITE_STEP_FIELDS for row in rows))
        self.assertEqual([row["step"] for row in rows], [step["id"] for step in SCENARIOS["interop-publish-query-edit"]["steps"]])
        passing = next(row for row in rows if row["step"] == "query")
        self.assertNotIn("seam", passing["oracle"])

    def test_a_blocked_seam_that_starts_passing_or_fails_differently_is_red(self):
        observations, edited = handoff_observations()
        sid = "interop-publish-query-edit"
        fixed = dict(observations)
        fixed[(sid, "edit")] = obs(sid, "edit", observed={"results": [{"success": True, "objectId": 2, "code": None}]})
        fixed[(sid, "read-back")] = obs(sid, "read-back", observed={"features": edited})
        status, detail, _ = evaluate(sid, fixed)
        self.assertEqual(status, "fail")
        self.assertIn("set it active", detail)
        other = dict(observations)
        other[(sid, "edit")] = obs(sid, "edit", error={"type": "HonuaHttpError", "status": 500})
        status, detail, _ = evaluate(sid, other)
        self.assertEqual(status, "fail")
        self.assertIn("was not observed", detail)

    def test_missing_steps_and_api_drift_are_red(self):
        observations, _ = handoff_observations()
        sid = "interop-publish-query-edit"
        missing = {key: value for key, value in observations.items() if key[1] != "ids"}
        self.assertEqual(evaluate(sid, missing)[0], "fail")
        drift = dict(observations)
        drift[(sid, "ids")] = dict(observations[(sid, "ids")], api="honua_sdk.something_else")
        status, detail, _ = evaluate(sid, drift)
        self.assertEqual(status, "fail")
        self.assertIn("contract names", detail)

    def test_the_declared_signatures_match_the_recorded_summaries(self):
        sid = "interop-proposal-approval"
        steps = [step["id"] for step in SCENARIOS[sid]["steps"]]
        observations = {(sid, "create-draft"): obs(sid, "create-draft", observed={
            "draftId": "d", "itemId": "i", "family": "map", "validation": "valid"})}
        observations[(sid, "save-version")] = obs(sid, "save-version", error={"type": "HonuaStudioError", "status": 400})
        needs = {"request-publication": ["version"], "self-approval-refused": ["proposal"], "approve": ["proposal"],
                 "proposal-resolved": ["approved"], "publication-url": ["request", "resolved"],
                 "published-pointer": ["version", "resolved"], "published-content": ["published"]}
        for step, missing in needs.items():
            observations[(sid, step)] = obs(sid, step, skipped=f"depends on {missing}, which did not complete")
        observations[(sid, "published-url")] = obs(sid, "published-url", unsupported=(
            "Honua.Sdk has no reader for a published Studio route (no published-content API in any Honua.Sdk.* package)"))
        status, detail, rows = evaluate("interop-proposal-approval", observations)
        self.assertEqual(status, "blocked", detail)
        self.assertEqual([row["step"] for row in rows], steps)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "plan.json"
        path.write_text(json.dumps(plan()))
        self.env = mock.patch.dict(os.environ, {"SDKREG_PLAN": str(path)})
        self.env.start()
        spec_ = importlib.util.spec_from_file_location("interop_orchestrator", HERE / "orchestrator.py")
        self.orchestrator = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(self.orchestrator)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def runner(self, script):
        return self.orchestrator.Runner("fake", [sys.executable, "-c", script])

    def test_runner_round_trip_keeps_replies_by_id(self):
        echo = ("import json, sys\n"
                "for line in sys.stdin:\n"
                "    r = json.loads(line)\n"
                "    print('noise'); print(json.dumps({'id': r['id'], 'observed': {'op': r['op'], **r['args']}}), flush=True)\n")
        runner = self.runner(echo)
        try:
            first, second = runner.send("a", value=1), runner.send("b", value=2)
            self.assertEqual(runner.receive(second)["observed"], {"op": "b", "value": 2})
            self.assertEqual(runner.receive(first)["observed"], {"op": "a", "value": 1})
            self.assertEqual(self.orchestrator.outcome(runner.call("c", value=3)), {"op": "c", "value": 3})
        finally:
            runner.close()

    def test_runner_silence_and_exit_are_errors_not_hangs(self):
        silent = self.runner("import time; time.sleep(30)")
        try:
            with self.assertRaisesRegex(self.orchestrator.RunnerError, "did not answer"):
                silent.call("x", timeout=0.5)
        finally:
            silent.process.kill()
        dead = self.runner("pass")
        dead.process.wait(timeout=10)
        with self.assertRaises(self.orchestrator.RunnerError):
            dead.call("x", timeout=5)

    def test_outcome_maps_refusals_and_unsupported(self):
        with self.assertRaises(self.orchestrator.Unsupported):
            self.orchestrator.outcome({"id": 1, "unsupported": "no API"})
        with self.assertRaises(self.orchestrator.StepError) as caught:
            self.orchestrator.outcome({"id": 1, "error": {"type": "HonuaAuthError", "status": 403, "message": "leak"}})
        self.assertEqual(caught.exception.error, {"type": "HonuaAuthError", "status": 403})

    def test_mcp_revocation_probe_reports_a_hang_and_a_refusal(self):
        mcp = self.orchestrator.probes

        class Hanging:
            def request(self, method, params=None):
                raise mcp.McpError("proxy did not answer tools/call within 30s")

        class Refusing:
            def request(self, method, params=None):
                return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "data": {"code": "permission_denied"}}}

        interop = self.orchestrator.Interop.__new__(self.orchestrator.Interop)
        probe = {"service": "s", "layerId": 1, "revokedAt": None, "observationSeconds": 30, "confirmations": 2}
        hung = interop._mcp_revoked(Hanging(), probe)
        self.assertTrue(hung["timedOut"])
        self.assertFalse(judge.oracle_revocation_observed(hung, FIXTURE)[0])
        refused = interop._mcp_revoked(Refusing(), probe)
        self.assertEqual((refused["status"], refused["confirmations"]), ("permission_denied", ["permission_denied", "permission_denied"]))
        self.assertTrue(judge.oracle_revocation_observed(refused, FIXTURE)[0])

        class ThenServerError:
            def __init__(self):
                self.calls = 0

            def request(self, method, params=None):
                self.calls += 1
                code = "permission_denied" if self.calls == 1 else "internal"
                return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "data": {"code": code}}}

        unhealthy = interop._mcp_revoked(ThenServerError(), probe)
        self.assertEqual(unhealthy["confirmations"], ["internal", "internal"])
        self.assertFalse(judge.oracle_revocation_observed(unhealthy, FIXTURE)[0])
        self.assertEqual(mcp.MCP_TIMEOUT_SECONDS, 120)

    def test_imported_attributes_are_the_properties_column(self):
        """The import stores gid and name inside the JSON properties column, not as columns."""
        stored = {"id": 1, "created_at": 1, "properties": '{"gid": 1, "name": "block"}'}
        self.assertEqual(self.orchestrator.imported_attributes(stored), {"gid": 1, "name": "block"})
        self.assertEqual(self.orchestrator.imported_attributes({"properties": {"gid": 1, "name": "block"}}),
                         {"gid": 1, "name": "block"})
        self.assertEqual(self.orchestrator.imported_attributes({"gid": 1, "name": "block"}),
                         {"gid": 1, "name": "block"})
        feature = FIXTURE["area"]["features"][0]
        ring = [list(point) for point in judge.envelope_ring(feature["envelope"])]
        dropped = self.orchestrator.imported_attributes({"id": 1, "properties": "{}"})
        self.assertFalse(judge.oracle_area_features(
            {"rings": [ring + [ring[0]]], "attributes": [dropped]}, FIXTURE)[0])
        self.assertFalse(judge.oracle_area_features(
            {"rings": [ring + [ring[0]]], "attributes": [self.orchestrator.imported_attributes({"id": 1})]}, FIXTURE)[0])

    def test_sdk_revocation_wait_is_one_observation_period_per_call(self):
        """observationSeconds + 120 let a 401 at 60–149s come back and be graded."""
        revocation = self.orchestrator.PLAN["identity"]["revocation"]
        bound = revocation["observationSeconds"]
        seen = []

        class Fake:
            def send(self, op, **kwargs):
                return 7

            def receive(self, request_id, timeout=None):
                seen.append(timeout)
                return {"observed": {
                    "refused": True, "status": 401, "succeededAfterRevocation": 0,
                    "refusedAfterSeconds": 0.4, "confirmations": [403, 401],
                    "observationSeconds": bound,
                }}

        interop = self.orchestrator.Interop.__new__(self.orchestrator.Interop)
        interop.python = interop.js = interop.dotnet = Fake()
        state = {"revokedAt": 1.0, "python-use": True, "js-use": True, "dotnet-use": True}
        with mock.patch.object(self.orchestrator, "emit"):
            interop._observe_revocation("interop-api-key-revocation", None, state)
        expect = bound * (1 + revocation["confirmations"])
        self.assertEqual(seen, [expect, expect, expect])
        self.assertLess(seen[0], bound + 120)

    def test_mcp_probe_crash_is_an_error_not_the_blocked_timeout(self):
        revocation = self.orchestrator.PLAN["identity"]["revocation"]
        interop = self.orchestrator.Interop.__new__(self.orchestrator.Interop)
        interop.python = interop.js = interop.dotnet = None
        state = {"revokedAt": 1.0, "mcp-use": True}

        def observe(probe):
            with mock.patch.object(interop, "_mcp_revoked", probe), mock.patch.object(self.orchestrator, "emit") as emitted, \
                    contextlib.redirect_stderr(io.StringIO()):
                interop._observe_revocation("interop-api-key-revocation", object(), state)
            return {call.args[1]: call.kwargs for call in emitted.call_args_list}["mcp-revoked"]

        def crash(session, probe):
            raise TypeError("malformed proxy response")
        crashed = observe(crash)
        self.assertEqual(crashed, {"error": {"type": "TypeError", "status": None}})
        self.assertNotIn("observed", crashed)
        timed_out = {"timedOut": True, "timeoutSeconds": revocation["observationSeconds"], "succeededAfterRevocation": 0}
        self.assertEqual(observe(lambda session, probe: dict(timed_out)), {"observed": timed_out})

    def test_published_url_reader_is_handed_the_polled_url(self):
        calls = []

        class Fake:
            def __init__(self, reply):
                self.reply = reply

            def call(self, op, timeout=None, **kwargs):
                calls.append((op, kwargs))
                return {"observed": self.reply(op)}

        class Cli:
            def write_profiles(self):
                pass

            def run(self, args, env):
                pass

            def json(self, args, env):
                return {"status": "Succeeded"}

        route = self.orchestrator.PLAN["proposal"]["route"]
        # Wrong origin and prefixed: the reader must be handed this string, not the fixture route.
        polled = "https://elsewhere.example/prefix" + route
        js = {"studio-create-draft": {"draftId": "d", "itemId": "i"},
              "studio-save-version": {"itemId": "i", "versionId": "v", "contentHash": "a" * 64},
              "studio-request-publication": {"proposalId": "proposal-1", "requestId": "r"},
              "studio-publication-url": {"requestId": "r", "state": "Active", "publicationUrl": polled}}
        interop = self.orchestrator.Interop.__new__(self.orchestrator.Interop)
        interop.js, interop.dotnet, interop.cli = Fake(js.get), Fake(lambda op: {"publishedVersionId": "v"}), Cli()
        interop.proposer = interop.approver = "key"
        with mock.patch.object(self.orchestrator, "emit"):
            interop.proposal_approval()
        self.assertIn(("studio-published-url", {"url": polled}), calls)
        self.assertNotIn(route, [kwargs.get("route") for op, kwargs in calls if op == "studio-published-url"])

    def test_sdk_runners_receive_only_their_own_credentials(self):
        script = "import json, os\nprint(json.dumps(dict(os.environ)), flush=True)\n"
        parent = {
            "PATH": os.environ.get("PATH", "/usr/bin"), "HOME": "/tmp/honua-home",
            "SDKREG_PLAN": "/tmp/plan.json", "SDKREG_API_KEY": "root-key", "SDKREG_BEARER": "bearer-token",
            "SDKREG_DB_PASSWORD": "db-secret", "SDKREG_PROPOSER_KEY": "proposer-key", "SDKREG_APPROVER_KEY": "approver-key",
            "E2E_API_KEY": "e2e", "HONUA_API_KEY": "honua", "GH_TOKEN": "gh",
            "DOTNET_ROOT": "/usr/share/dotnet", "NUGET_PACKAGES": "/tmp/packages",
        }

        def captured(name):
            with mock.patch.dict(os.environ, parent, clear=True):
                runner = self.orchestrator.Runner(name, [sys.executable, "-c", script])
            try:
                return json.loads(runner.process.stdout.readline())
            finally:
                runner.close()

        def credentials(env):
            return {key: value for key, value in env.items() if key.startswith(("SDKREG_", "E2E_", "HONUA_")) or key == "GH_TOKEN"}

        shared = {"SDKREG_PLAN": "/tmp/plan.json", "SDKREG_API_KEY": "root-key"}
        self.assertEqual(credentials(captured("python-runner")), shared)
        dotnet = captured("dotnet-runner")
        self.assertEqual(credentials(dotnet), shared)
        self.assertEqual(dotnet["DOTNET_ROOT"], "/usr/share/dotnet")
        self.assertEqual(dotnet["NUGET_PACKAGES"], "/tmp/packages")
        self.assertEqual(credentials(captured("js-runner")), {**shared, "SDKREG_PROPOSER_KEY": "proposer-key"})
        self.assertNotIn("DOTNET_ROOT", captured("python-runner"))
        # An unnamed runner still receives the plan (it holds no credentials) and nothing else.
        fake = captured("fake")
        self.assertEqual(credentials(fake), {"SDKREG_PLAN": "/tmp/plan.json"})
        for env in (dotnet, fake):
            self.assertNotIn("GH_TOKEN", env)
            self.assertNotIn("SDKREG_BEARER", env)
            self.assertNotIn("SDKREG_DB_PASSWORD", env)
            self.assertNotIn("SDKREG_APPROVER_KEY", env)

    def test_cli_children_carry_only_their_own_credential(self):
        cli = self.orchestrator.Cli("honua", Path(self.tmp.name))
        seen = {}

        def fake_run(argv, env, **kwargs):
            seen.update(env)
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        with mock.patch.dict(os.environ, {"SDKREG_API_KEY": "hnua_root", "E2E_API_KEY": "x", "HONUA_API_KEY": "y"}), \
                mock.patch.object(self.orchestrator.subprocess, "run", side_effect=fake_run):
            cli.json(["services"], {"HONUA_ADMIN_KEY": "hnua_one"}, quiet=True)
        self.assertEqual({k for k in seen if k.startswith(("SDKREG_", "E2E_", "HONUA_"))},
                         {"HONUA_ADMIN_KEY", "HONUA_BASE_URL", "HONUA_CONFIG_HOME"})
        self.assertEqual(seen["HONUA_ADMIN_KEY"], "hnua_one")
        cli.write_profiles()
        self.assertEqual(os.stat(cli.profiles / "config.json").st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(cli.profiles).st_mode & 0o777, 0o700)


class WiringTests(unittest.TestCase):
    def test_plan_carries_ids_and_fixture_values_but_no_credentials(self):
        p = plan()
        text = json.dumps(p)
        self.assertNotIn("hnua_", text)
        self.assertEqual(p["principals"], {"proposerId": "p-id", "approverId": "a-id"})
        self.assertEqual(p["handoff"]["table"], "sdkreg_interop_handoff_abc123")
        self.assertEqual(p["proposal"]["route"], "/sdkreg-interop-map-abc123")
        self.assertIn("/ogc/features/collections/7/items", json.dumps(p["proposal"]["envelope"]["body"]))
        self.assertNotIn("{sitesCollectionId}", text)
        self.assertEqual(p["baseUrl"], "http://candidate:8080")

    def test_seed_sql_is_the_fixture_handoff_table(self):
        sql = engine.seed_sql(regression, FIXTURE, "abc123")
        self.assertIn("CREATE TABLE honua_data.sdkreg_interop_handoff_abc123", sql)
        for row in FIXTURE["handoff"]["features"]:
            self.assertIn(f"ST_MakePoint({row['x']!r}, {row['y']!r})", sql)

    def test_install_uses_the_pinned_bytes_of_every_client(self):
        manifest, _ = run.load_inputs(run.ROOT / "platform-manifest.yaml", run.DEFAULT_MATRIX)
        pins = manifest["clientArtifacts"]
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "suite-interop"
            calls = {}

            def npm(pin, path, **kwargs):
                calls["npm"] = (pin["package"], kwargs["companion_pin"]["package"])
                path.mkdir(parents=True)
                return True, "npm ok"
            with mock.patch.object(run, "install_npm", side_effect=npm), \
                    mock.patch.object(run, "_install_python_wheels", return_value=(Path(tmp) / "site", "pypi ok")), \
                    mock.patch.object(run, "_restore_nuget", return_value=(True, "nuget ok", {"NUGET_PACKAGES": "p"})), \
                    mock.patch.object(run.shutil, "which", return_value="/usr/bin/dotnet"):
                ok, detail, command, env = run.install_suite_client("interop", pins, work)
            self.assertTrue(ok, detail)
            self.assertEqual(calls["npm"], ("@honua/sdk-js", "@honua/mcp-server"))
            self.assertTrue((work / "npm" / "runner.mjs").is_file())
            self.assertTrue((work / "nuget" / "runner" / "Program.cs").is_file())
            self.assertEqual(command[1], str(HERE / "orchestrator.py"))
            argv = dict(zip(command[2::2], command[3::2]))
            self.assertEqual(json.loads(argv["--dotnet-runner"])[1], str(work / "nuget" / "out" / "InteropRunner.dll"))
            self.assertEqual(json.loads(argv["--python-runner"])[-1], str(HERE / "drivers" / "python" / "runner.py"))
            self.assertEqual(env, {"NUGET_PACKAGES": "p"})
            with mock.patch.object(run, "install_npm", return_value=(False, "npm archive integrity mismatch")):
                ok, detail, _, _ = run.install_suite_client("interop", pins, Path(tmp) / "other")
            self.assertFalse(ok)
            self.assertIn("integrity mismatch", detail)

    def test_receipt_rows_may_name_their_client(self):
        cells = [CELLS["interop-import-render-buffer"]]
        steps = [{"step": step["id"], "client": LABELS[step["client"]], "api": step["api"], "status": "pass", "oracle": "x"}
                 for step in SCENARIOS["interop-import-render-buffer"]["steps"]]
        result = {"cell": "interop-import-render-buffer", "status": "pass", "steps": steps}
        self.assertEqual(run.verify_suite_steps(cells, {"interop-import-render-buffer": result}), [])
        steps[0]["body"] = "{}"
        self.assertTrue(run.verify_suite_steps(cells, {"interop-import-render-buffer": result}))


if __name__ == "__main__":
    unittest.main()
