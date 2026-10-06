"""Self-tests for the SDK regression suite: contracts, oracles, step classification, receipts."""
import base64
import copy
import hashlib
import hmac
import importlib.util
import json
import math
import os
import re
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import oracles  # noqa: E402
import regression  # noqa: E402

spec = importlib.util.spec_from_file_location("installed_cert_suite", HERE / "run.py")
run = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run)

FIXTURE = regression.load_fixture()
SCENARIOS = regression.load_scenarios()
MATRIX = json.loads((HERE / "matrix.json").read_text())
CLI_DRIVER = (HERE / "drivers/cli/driver.py").read_text()
MCP_DRIVER = (HERE / "drivers/mcp/driver.py").read_text()
INTEROP_ORCHESTRATOR = (HERE / "interop/orchestrator.py").read_text()
# (scenario family, client artifact) -> the driver program that runs it.
DRIVER_SOURCES = {
    ("sdk", "honua-sdk-python-wheel"): (HERE / "drivers/python/driver.py").read_text(),
    ("sdk", "honua-sdk-js"): (HERE / "drivers/js/driver.mjs").read_text(),
    ("sdk", "honua-sdk-dotnet"): (HERE / "drivers/dotnet/Program.cs").read_text(),
    ("cli", "honua-sdk-js"): CLI_DRIVER,
    ("cli", "honua-sdk-python-wheel"): CLI_DRIVER,
    ("mcp", "honua-mcp-server"): MCP_DRIVER,
}
SUITE_CELLS = [cell for cell in MATRIX["cells"] if cell["driver"] in run.SUITE_DRIVERS]
PINNED_DEFAULT_VIEW = json.loads(regression.TOOL_ROSTER.read_text())["defaultView"]["tools"]


def load_driver(name, plan_path):
    """Import a driver program as a module; drivers read SDKREG_PLAN at import."""
    with mock.patch.dict(os.environ, {"SDKREG_PLAN": str(plan_path)}):
        spec_ = importlib.util.spec_from_file_location(f"{name}_driver_under_test", HERE / "drivers" / name / "driver.py")
        module = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(module)
    return module


def png(width, height, painted):
    """An RGBA PNG whose pixels are (0, 0, 255, 128) where painted(x, y) and transparent elsewhere."""
    raw = b"".join(
        b"\x00" + b"".join(bytes((0, 0, 255, 128)) if painted(x, y) else bytes(4) for x in range(width))
        for y in range(height)
    )
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def field(number, wire, payload):
    key = varint((number << 3) | wire)
    return key + (varint(len(payload)) + payload if wire == 2 else varint(payload))


def mvt_polygon(points, extent=4096, name="layer"):
    zigzag = lambda v: (v << 1) ^ (v >> 31)  # noqa: E731
    commands, x, y = [], 0, 0
    commands.append((1 & 7) | (1 << 3))
    commands += [zigzag(points[0][0] - x), zigzag(points[0][1] - y)]
    x, y = points[0]
    commands.append((2 & 7) | ((len(points) - 1) << 3))
    for px, py in points[1:]:
        commands += [zigzag(px - x), zigzag(py - y)]
        x, y = px, py
    commands.append(7 | (1 << 3))
    geometry = b"".join(varint(value) for value in commands)
    feature = field(3, 0, 3) + field(4, 2, geometry)
    layer = field(1, 2, name.encode()) + field(2, 2, feature) + field(5, 0, extent)
    return field(3, 2, layer)


def observation(scenario, step, **payload):
    api = SCENARIOS[scenario]["clients"]["honua-sdk-js"][step]
    return {"scenario": scenario, "step": step, "api": api, **payload}


class ContractTests(unittest.TestCase):
    def test_every_scenario_names_one_api_per_step_for_every_client_of_its_family(self):
        self.assertEqual(set(SCENARIOS), {"sdk-auth", "sdk-admin-lifecycle", "sdk-geoservices", "sdk-ogc-features",
                                          "sdk-ogc-tiles", "sdk-ogc-processes", "sdk-stac", "cli-workflow", "mcp-workflow",
                                          "interop-publish-query-edit", "interop-import-render-buffer",
                                          "interop-proposal-approval", "interop-api-key-revocation"})
        for scenario in SCENARIOS.values():
            steps = [step["id"] for step in scenario["steps"]]
            family = regression.scenario_family(scenario["id"])
            if family == "interop":
                # An interop step names its own client (one of the family's) and its API.
                self.assertNotIn("clients", scenario)
                for step in scenario["steps"]:
                    self.assertIn(step["client"], regression.FAMILIES[family], (scenario["id"], step["id"]))
                    self.assertTrue(step["api"], (scenario["id"], step["id"]))
                continue
            self.assertEqual(set(scenario["clients"]), set(regression.FAMILIES[family]), scenario["id"])
            for client, apis in scenario["clients"].items():
                self.assertEqual(list(apis), steps, (scenario["id"], client))

    def test_drivers_report_exactly_the_contract_apis(self):
        # A driver that drifts from the contract would make a failure name the wrong SDK API.
        for scenario in SCENARIOS.values():
            if regression.scenario_family(scenario["id"]) == "interop":
                for step in scenario["steps"]:
                    self.assertIn(json.dumps(step["api"])[1:-1], INTEROP_ORCHESTRATOR, (scenario["id"], step["id"]))
                continue
            for client, apis in scenario["clients"].items():
                source = DRIVER_SOURCES[(regression.scenario_family(scenario["id"]), client)]
                for step, api in apis.items():
                    self.assertIn(json.dumps(api)[1:-1], source, (scenario["id"], client, step))

    def test_drivers_never_make_raw_http_calls(self):
        self.assertNotRegex(DRIVER_SOURCES[("sdk", "honua-sdk-python-wheel")], r"\b(httpx|requests|urllib)\.")
        self.assertNotRegex(DRIVER_SOURCES[("sdk", "honua-sdk-js")], r"\bfetch\(")
        self.assertNotRegex(DRIVER_SOURCES[("sdk", "honua-sdk-dotnet")], r"new HttpClient|GetAsync\(\"|PostAsync\(")
        # The command-line and MCP drivers only launch the installed executables.
        for source in (CLI_DRIVER, MCP_DRIVER):
            self.assertNotRegex(source, r"\b(httpx|requests|urllib|http\.client|socket)\b")

    def test_scenario_validation_rejects_incomplete_contracts(self):
        base = copy.deepcopy(SCENARIOS["sdk-stac"])
        cases = [
            (lambda s: s.update(id="stac"), "sdk-\\*, cli-\\*, mcp-\\* or interop-\\* id"),
            (lambda s: s.update(id="gui-stac"), "sdk-\\*, cli-\\*, mcp-\\* or interop-\\* id"),
            (lambda s: s.update(steps=[]), "no steps"),
            (lambda s: s["steps"][0].update(oracle="vibes"), "known oracle"),
            (lambda s: s.update(receiptFields=["client", "body"]), "allowlist"),
            (lambda s: s["clients"].pop("honua-sdk-dotnet"), "every sdk client"),
            (lambda s: s["clients"].update({"honua-mcp-server": s["clients"]["honua-sdk-js"]}), "every sdk client"),
            (lambda s: s["clients"]["honua-sdk-js"].update(search=""), "one API per step"),
        ]
        for change, message in cases:
            with self.subTest(message=message):
                scenario = copy.deepcopy(base)
                change(scenario)
                with self.assertRaisesRegex(regression.RegressionError, message):
                    regression.validate_scenario(scenario, "case.json")

    def test_matrix_covers_every_scenario_of_each_family_for_every_client(self):
        self.assertEqual(set(run.SUITE_DRIVERS), set(regression.DRIVER_CLIENTS))
        self.assertEqual(set(regression.SUITE_DRIVERS), set(regression.DRIVER_CLIENTS))
        for driver, (family, artifact) in regression.DRIVER_CLIENTS.items():
            family_scenarios = {sid for sid in SCENARIOS if regression.scenario_family(sid) == family}
            cells = [cell for cell in SUITE_CELLS if cell["driver"] == driver]
            self.assertEqual(sorted(cell["scenario"] for cell in cells), sorted(family_scenarios), driver)
            if family == "interop":
                # An interop cell's artifact is the client that starts its hand-off.
                artifact = None
                self.assertTrue(all(cell["artifact"] == SCENARIOS[cell["scenario"]]["steps"][0]["client"] for cell in cells))
            self.assertTrue(all(artifact is None or cell["artifact"] == artifact for cell in cells), driver)
        for cell in SUITE_CELLS:
            regression.validate_suite_cell(cell, SCENARIOS, run.BLOCKER)

    def test_matrix_omitting_a_scenario_is_rejected(self):
        for cell_id, message in (("nuget-sdk-stac", "omits SDK regression scenarios"),
                                 ("pypi-cli-workflow", r"omits regression drivers: \['pypi-cli'\]"),
                                 ("npm-mcp-workflow", r"omits regression drivers: \['npm-mcp-workflow'\]")):
            with self.subTest(cell=cell_id):
                manifest, matrix = run.load_inputs(run.ROOT / "platform-manifest.yaml", run.DEFAULT_MATRIX)
                matrix["cells"] = [cell for cell in matrix["cells"] if cell["id"] != cell_id]
                with self.assertRaisesRegex(run.CertificationError, message):
                    run.validate_release_inputs(manifest, matrix)

    def test_suite_cell_validation(self):
        cell = next(c for c in MATRIX["cells"] if c["id"] == "pypi-sdk-geoservices")
        cases = [
            (lambda c: c.update(scenario="sdk-unknown"), "unknown scenario"),
            (lambda c: c.update(driver="npm-sdk"), "does not drive"),
            (lambda c: c.update(driver="pypi-cli"), "does not drive"),
            (lambda c: c["blockedSteps"].update(nope={"blockedBy": c["blockedBy"][0], "signature": "x"}), "blocked steps of its scenario"),
            (lambda c: c["blockedSteps"]["count"].update(signature=""), "observed signature"),
            (lambda c: c["blockedSteps"]["count"].update(blockedBy="#236"), "issue URL"),
            (lambda c: c.update(blockedBy=c["blockedBy"][:1]), "exactly the blocked steps' issues"),
            (lambda c: c.update(status="active"), "cannot carry blockedSteps"),
        ]
        for change, message in cases:
            with self.subTest(message=message):
                changed = copy.deepcopy(cell)
                change(changed)
                with self.assertRaisesRegex(regression.RegressionError, message):
                    regression.validate_suite_cell(changed, SCENARIOS, run.BLOCKER)


class OracleTests(unittest.TestCase):
    def test_expectations_come_from_the_fixture(self):
        self.assertEqual([row["gid"] for row in oracles.filtered_sites(FIXTURE)], [3, 4, 5, 6])
        self.assertEqual(oracles.where_clause(FIXTURE["sites"]["filter"]), "rank >= 30")
        self.assertEqual([row["gid"] for row in oracles.sites_in_bbox(FIXTURE)], [3, 4])
        edited = oracles.edited_features(FIXTURE)
        self.assertEqual([(row["gid"], row["rank"]) for row in edited], [(1, 1), (2, 20), (4, 4)])
        self.assertEqual(oracles.attachment_size(FIXTURE), 32)

    def test_feature_oracle_checks_attributes_and_exact_ordinates(self):
        rows = [{"attributes": {k: r[k] for k in ("gid", "name", "rank")}, "x": r["x"], "y": r["y"]}
                for r in oracles.filtered_sites(FIXTURE)]
        self.assertTrue(regression.ORACLES["features"]({"features": rows}, FIXTURE, {}, {}, "sdk-geoservices")[0])
        moved = copy.deepcopy(rows)
        moved[0]["x"] += 1e-6
        ok, summary = regression.ORACLES["features"]({"features": moved}, FIXTURE, {}, {}, "sdk-geoservices")
        self.assertFalse(ok)
        self.assertIn("ordinates", summary)
        self.assertFalse(regression.ORACLES["features"]({"features": rows[1:]}, FIXTURE, {}, {}, "sdk-geoservices")[0])

    def test_refusal_oracles_need_the_documented_error(self):
        self.assertTrue(oracles.oracle_refused({"error": {"type": "HonuaAuthError", "status": 499}})[0])
        self.assertFalse(oracles.oracle_refused({"error": {"type": "HonuaHttpError", "status": 500}})[0])
        self.assertFalse(oracles.oracle_refused({"observed": {"returned": 0}})[0])
        self.assertTrue(oracles.oracle_not_found({"error": {"type": "HonuaHttpError", "status": 404}})[0])
        self.assertFalse(oracles.oracle_not_found({"observed": {"returned": 2}})[0])

    def test_raster_tile_pixel_oracle(self):
        samples = oracles.expected_pixels(FIXTURE)
        self.assertEqual(sum(1 for *_, inside in samples if inside), 3)
        self.assertEqual(sum(1 for *_, inside in samples if not inside), 3)
        left, top, right, bottom = oracles._envelope_in_tile(FIXTURE["area"]["features"][0]["envelope"], FIXTURE["area"]["tiles"]["painted"], 256)
        good = png(256, 256, lambda x, y: left <= x + 0.5 <= right and top <= y + 0.5 <= bottom)
        ok, summary = oracles.oracle_raster_tile({"bytes": base64.b64encode(good).decode()}, FIXTURE)
        self.assertTrue(ok, summary)
        everywhere = png(256, 256, lambda x, y: True)
        self.assertFalse(oracles.oracle_raster_tile({"bytes": base64.b64encode(everywhere).decode()}, FIXTURE)[0])
        self.assertFalse(oracles.oracle_raster_tile({"bytes": base64.b64encode(b"not a png").decode()}, FIXTURE)[0])

    def test_vector_tile_oracle(self):
        want = [round(v) for v in oracles._envelope_in_tile(FIXTURE["area"]["features"][0]["envelope"], FIXTURE["area"]["tiles"]["painted"], 4096)]
        ring = [(want[2], want[1]), (want[2], want[3]), (want[0], want[3]), (want[0], want[1])]
        ok, summary = oracles.oracle_vector_tile({"bytes": base64.b64encode(mvt_polygon(ring)).decode()}, FIXTURE)
        self.assertTrue(ok, summary)
        shifted = [(x + 200, y) for x, y in ring]
        self.assertFalse(oracles.oracle_vector_tile({"bytes": base64.b64encode(mvt_polygon(shifted)).decode()}, FIXTURE)[0])
        self.assertFalse(oracles.oracle_vector_tile({"bytes": ""}, FIXTURE)[0])

    def test_buffer_oracle(self):
        (cx, cy), radius = FIXTURE["processes"]["point"], FIXTURE["processes"]["distance"]
        ring = [[cx + radius * math.cos(2 * math.pi * i / 32), cy + radius * math.sin(2 * math.pi * i / 32)] for i in range(32)]
        ring.append(ring[0])
        feature = {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]}}
        self.assertTrue(oracles.oracle_buffer({"geometry": feature}, FIXTURE)[0])
        wide = copy.deepcopy(feature)
        wide["geometry"]["coordinates"][0][3][0] += 0.01
        self.assertFalse(oracles.oracle_buffer({"geometry": wide}, FIXTURE)[0])
        self.assertFalse(oracles.oracle_buffer({"geometry": {"type": "Point", "coordinates": [cx, cy]}}, FIXTURE)[0])

    def test_empty_tile_and_job_oracles(self):
        self.assertTrue(oracles.oracle_empty_tile({"empty": True, "size": 0})[0])
        self.assertFalse(oracles.oracle_empty_tile({"empty": False, "size": 77})[0])
        self.assertTrue(oracles.oracle_job_accepted({"jobId": "gp-1", "status": "accepted"})[0])
        self.assertFalse(oracles.oracle_job_accepted({"jobId": "", "status": "successful"})[0])
        self.assertFalse(oracles.oracle_job_succeeded({"status": "failed"})[0])


def passing_observations():
    """Observations a correct SDK reports for sdk-stac and sdk-geoservices."""
    sites = oracles.filtered_sites(FIXTURE)
    in_bbox = oracles.sites_in_bbox(FIXTURE)
    edited = oracles.edited_features(FIXTURE)
    attachment = FIXTURE["edits"]["attachment"]
    rows = lambda items: [{"attributes": {k: r[k] for k in ("gid", "name", "rank")}, "x": r["x"], "y": r["y"]} for r in items]  # noqa: E731
    found = [
        observation("sdk-stac", "search", observed={"features": [{"id": str(r["gid"]), "x": r["x"], "y": r["y"]} for r in in_bbox]}),
        observation("sdk-geoservices", "query", observed={"features": rows(sites)}),
        observation("sdk-geoservices", "ids", observed={"ids": [r["gid"] for r in sites]}),
        observation("sdk-geoservices", "count", observed={"count": len(sites)}),
        observation("sdk-geoservices", "resolve-edit-ids", observed={"features": [{"gid": g, "objectId": 10 + g} for g in (1, 2, 3)]}),
        observation("sdk-geoservices", "apply-edits-add", observed={"results": [{"success": True, "objectId": 20}]}),
        observation("sdk-geoservices", "apply-edits-update", observed={"results": [{"success": True, "objectId": 12}]}),
        observation("sdk-geoservices", "apply-edits-delete", observed={"results": [{"success": True, "objectId": 13}]}),
        observation("sdk-geoservices", "edits-state", observed={"features": rows(edited)}),
        observation("sdk-geoservices", "add-attachment", observed={"results": [{"success": True, "objectId": 1}]}),
        observation("sdk-geoservices", "query-attachments", observed={"attachments": [
            {"name": attachment["name"], "contentType": attachment["contentType"], "size": oracles.attachment_size(FIXTURE)}]}),
    ]
    return {(item["scenario"], item["step"]): item for item in found}


def cell(scenario, **extra):
    return {"id": f"npm-sdk-{scenario[4:]}", "artifact": "honua-sdk-js", "driver": "npm-sdk", "scenario": scenario,
            "status": "active", **extra}


def evaluate(cell_, observations):
    return regression.evaluate_cell(cell_, SCENARIOS[cell_["scenario"]], "honua-sdk-js", observations, FIXTURE,
                                    {"sites": {"itemId": "3"}}, "@honua/sdk-js 0.1.12")


class ClassificationTests(unittest.TestCase):
    def test_active_cell_passes_only_when_every_step_passes(self):
        observations = passing_observations()
        self.assertEqual(evaluate(cell("sdk-geoservices"), observations)[0], "pass")
        del observations[("sdk-geoservices", "count")]
        status, detail, rows = evaluate(cell("sdk-geoservices"), observations)
        self.assertEqual(status, "fail")
        self.assertIn("@honua/sdk-js 0.1.12 `HonuaFeatureLayer.queryFeatureCount` count", detail)
        self.assertIn("missing scenario step", detail)

    def test_api_drift_unsupported_and_skipped_steps_fail(self):
        for change in ({"api": "HonuaClient.request"}, {"unsupported": "no API"}, {"skipped": "dependency failed"}):
            with self.subTest(change=change):
                observations = passing_observations()
                item = observations[("sdk-stac", "search")]
                item.update(change)
                if "api" not in change:
                    item.pop("observed")
                self.assertEqual(evaluate(cell("sdk-stac"), observations)[0], "fail")

    def test_blocked_step_needs_its_declared_signature(self):
        blocker = "https://github.com/honua-io/honua-server/issues/5407"
        blocked = cell("sdk-geoservices", status="blocked", blockedBy=[blocker], blockedSteps={
            "apply-edits-update": {"blockedBy": blocker, "signature": r"\(status 500\)$"}})
        observations = passing_observations()
        observations[("sdk-geoservices", "apply-edits-update")] = observation(
            "sdk-geoservices", "apply-edits-update", error={"type": "HonuaHttpError", "status": 500})
        status, detail, rows = evaluate(blocked, observations)
        self.assertEqual(status, "blocked")
        row = next(r for r in rows if r["step"] == "apply-edits-update")
        self.assertEqual((row["status"], row["blockedBy"]), ("blocked", blocker))
        # A different failure is not the declared blocker.
        observations[("sdk-geoservices", "apply-edits-update")]["error"]["status"] = 400
        self.assertEqual(evaluate(blocked, observations)[0], "fail")
        # A fixed blocker must be flipped, never pass silently.
        status, detail, _ = evaluate(blocked, passing_observations())
        self.assertEqual(status, "fail")
        self.assertIn("set it active", detail)
        # An unblocked step failing in a blocked cell is still a failure.
        observations = passing_observations()
        observations[("sdk-geoservices", "apply-edits-update")] = observation(
            "sdk-geoservices", "apply-edits-update", error={"type": "HonuaHttpError", "status": 500})
        observations[("sdk-geoservices", "count")]["observed"]["count"] = 3
        self.assertEqual(evaluate(blocked, observations)[0], "fail")

    def test_duplicate_observations_never_overwrite_a_failure(self):
        lines = [json.dumps(observation("sdk-stac", "search", error={"type": "HonuaHttpError", "status": 500})),
                 json.dumps(observation("sdk-stac", "search", observed={"features": []}))]
        parsed = regression.parse_observations("\n".join(lines))
        self.assertEqual(parsed[("sdk-stac", "search")]["error"]["type"], "DuplicateObservation")

    def test_observations_drop_unknown_fields_and_receipt_rows_are_allowlisted(self):
        line = json.dumps({**observation("sdk-stac", "search", observed={"features": []}),
                           "body": "Authorization: Bearer eyJabcdefghijklmnop.qrs", "headers": {"X-API-Key": "k"}})
        parsed = regression.parse_observations("noise\n" + line)
        self.assertEqual(set(parsed[("sdk-stac", "search")]), {"scenario", "step", "api", "observed"})
        _, _, rows = evaluate(cell("sdk-stac"), parsed)
        self.assertTrue(all(set(row) <= run.SUITE_STEP_FIELDS for row in rows))

    def test_scrub_removes_credentials(self):
        text = regression.scrub("password=hunter2 X-API-Key: abc Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig")
        self.assertNotIn("hunter2", text)
        self.assertNotIn("abc", text)
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", text)


class HarnessTests(unittest.TestCase):
    def test_seed_sql_is_deterministic_and_per_client(self):
        sql = regression.seed_sql(FIXTURE, ["js", "python"])
        self.assertEqual(sql, regression.seed_sql(FIXTURE, ["js", "python"]))
        for table in ("sdkreg_sites", "sdkreg_area", "sdkreg_edits_js", "sdkreg_lifecycle_js", "sdkreg_edits_python"):
            self.assertIn(f"CREATE TABLE honua_data.{table}", sql)
        self.assertIn("(4, 'delta', 40, ST_SetSRID(ST_MakePoint(-122.1, 37.85), 4326))", sql)

    def test_operator_bearer_is_signed_for_the_overlay_issuer(self):
        token = regression.mint_bearer("k" * 48, lifetime=60)
        header, payload, signature = token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
        self.assertEqual((claims["iss"], claims["aud"], claims["tenant_id"], claims["role"]),
                         ("https://sdk-regression.invalid", "sdk-regression", "default", "admin"))
        expected = base64.urlsafe_b64encode(hmac.new(b"k" * 48, f"{header}.{payload}".encode(), hashlib.sha256).digest()).rstrip(b"=")
        self.assertEqual(signature, expected.decode())
        overlay = (HERE / "compose.sdk-regression.yml").read_text()
        self.assertIn(claims["iss"], overlay)
        self.assertIn(f'Oidc__Generic__ClientId: "{claims["aud"]}"', overlay)

    def test_plan_carries_no_credentials(self):
        published = {"sites": {"service": "sdkreg-sites", "layerId": 1}, "area": {"service": "sdkreg-area", "layerId": 2},
                     "edits": {"js": {"service": "sdkreg-edits-js", "layerId": 3}}}
        plan = regression.build_plan(FIXTURE, published, "js", list(SCENARIOS), SCENARIOS, "http://localhost:8080/")
        text = json.dumps(plan)
        self.assertNotIn("password", text.lower())
        self.assertEqual(plan["sites"]["where"], "rank >= 30")
        self.assertEqual(plan["sites"]["itemId"], "3")
        env = regression.driver_env({"SDKREG_STALE": "x", "PATH": "/bin"}, api_key="k", bearer="b", db_password="p",
                                    plan_path=Path("/tmp/plan.json"))
        self.assertNotIn("SDKREG_STALE", env)
        self.assertEqual(env["SDKREG_PLAN"], "/tmp/plan.json")

    def test_suite_cells_fail_closed_without_a_candidate_or_on_install_failure(self):
        manifest, matrix = run.load_inputs(run.ROOT / "platform-manifest.yaml", run.DEFAULT_MATRIX)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            run, "install_suite_client", return_value=(True, "ok", ["true"], {})
        ):
            results = run.run_suite(manifest, matrix, Path("/nonexistent"))
        self.assertEqual(len(results), len(SUITE_CELLS))
        self.assertTrue(all(status == "fail" and "live candidate" in detail for status, detail, _ in results.values()))
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            run, "install_suite_client", return_value=(False, "wheel digest mismatch", [], {})
        ):
            results = run.run_suite(manifest, matrix, Path("/nonexistent"))
        self.assertTrue(all(status == "fail" and "install failed: wheel digest mismatch" in detail
                            for status, detail, _ in results.values()))

    def test_harness_failure_fails_every_suite_cell(self):
        manifest, matrix = run.load_inputs(run.ROOT / "platform-manifest.yaml", run.DEFAULT_MATRIX)
        with mock.patch.dict(os.environ, {"HONUA_SERVER_URL": "http://127.0.0.1:9"}, clear=True), mock.patch.object(
            run, "install_suite_client", return_value=(True, "ok", ["true"], {})
        ), mock.patch.object(regression, "seed", side_effect=regression.RegressionError("fixture SQL failed")):
            run._REGRESSION = regression
            results = run.run_suite(manifest, matrix, Path("/nonexistent"))
        self.assertTrue(all(status == "fail" and "fixture preparation failed" in detail for status, detail, _ in results.values()))

    def test_driver_crash_fails_the_cells_even_with_partial_observations(self):
        manifest, matrix = run.load_inputs(run.ROOT / "platform-manifest.yaml", run.DEFAULT_MATRIX)
        matrix["cells"] = [c for c in matrix["cells"] if c["id"] == "npm-sdk-stac"]
        stdout = json.dumps(passing_observations()[("sdk-stac", "search")])
        published = {"sites": {"service": "sdkreg-sites", "layerId": 1}, "area": {"service": "sdkreg-area", "layerId": 2},
                     "edits": {"js": {"service": "sdkreg-edits-js", "layerId": 3}}}
        with mock.patch.dict(os.environ, {"HONUA_SERVER_URL": "http://127.0.0.1:9", "SDKREG_SIGNING_KEY": "k" * 48}, clear=True), \
                mock.patch.object(run, "install_suite_client", return_value=(True, "ok", ["node", "driver.mjs"], {})), \
                mock.patch.object(regression, "seed"), mock.patch.object(regression, "publish_harness", return_value=published), \
                mock.patch.object(regression, "log"), \
                mock.patch.object(regression, "run_driver", return_value=(1, stdout, "boom")), \
                mock.patch("pathlib.Path.write_text"):
            run._REGRESSION = regression
            results = run.run_suite(manifest, matrix, Path("/nonexistent"))
            self.assertEqual(results["npm-sdk-stac"][0], "fail")
            with mock.patch.object(regression, "run_driver", return_value=(0, stdout, "")):
                self.assertEqual(run.run_suite(manifest, matrix, Path("/nonexistent"))["npm-sdk-stac"][0], "pass")

    def test_verify_receipt_rejects_undeclared_blocked_steps_and_extra_fields(self):
        cells = [c for c in MATRIX["cells"] if c["id"] == "npm-sdk-stac"]
        steps = [{"step": "search", "api": "HonuaStacSearch.search", "status": "blocked", "oracle": "x"}]
        result = {"cell": "npm-sdk-stac", "status": "pass", "steps": steps}
        self.assertTrue(any("without a matrix declaration" in v for v in run.verify_suite_steps(cells, {"npm-sdk-stac": result})))
        steps[0].update(status="pass", body="{}")
        self.assertTrue(any("non-allowlisted" in v for v in run.verify_suite_steps(cells, {"npm-sdk-stac": result})))
        result["steps"] = []
        self.assertTrue(any("do not match" in v for v in run.verify_suite_steps(cells, {"npm-sdk-stac": result})))


PRINCIPALS = {"proposer": {"id": "key-proposer", "key": "secret-proposer"}, "approver": {"id": "key-approver", "key": "secret-approver"}}
WORKFLOW_PUBLISHED = {"sites": {"service": "sdkreg-sites", "layerId": 1}, "area": {"service": "sdkreg-area", "layerId": 2}, "edits": {}}


def workflow_cell(cell_id):
    return copy.deepcopy(next(c for c in MATRIX["cells"] if c["id"] == cell_id))


def workflow_observations(scenario, client, observed):
    apis = SCENARIOS[scenario]["clients"][client]
    return {(scenario, step): {"scenario": scenario, "step": step, "api": apis[step], **payload} for step, payload in observed.items()}


def cli_plan():
    return regression.build_plan(FIXTURE, WORKFLOW_PUBLISHED, "clijs", ["cli-workflow"], SCENARIOS, "http://localhost:8080",
                                 principals=PRINCIPALS)


def passing_cli_observations():
    life, proposal = FIXTURE["lifecycle"], FIXTURE["proposal"]
    plan = cli_plan()
    rows = lambda items, fields: [{"properties": {k: r[k] for k in fields}, "x": r["x"], "y": r["y"]} for r in items]  # noqa: E731
    return workflow_observations("cli-workflow", "honua-sdk-js", {
        "discover": {"observed": {"services": ["sdkreg-area", FIXTURE["sites"]["service"]]}},
        "create-datasource": {"observed": {"connectionId": "c-1"}},
        "test-datasource": {"observed": {"success": True}},
        "publish": {"observed": {"layerId": 12, "layerName": life["layerName"], "serviceName": plan["lifecycle"]["service"], "enabled": True}},
        "list": {"observed": {"layers": [{"layerId": 12, "enabled": True}]}},
        "query": {"observed": {"features": rows(oracles.filtered_sites(FIXTURE), ("gid", "name", "rank"))}},
        "served": {"observed": {"count": len(life["features"])}},
        "propose-publication": {"observed": {"status": "RequiresApproval", "requiresApproval": True, "proposalId": "proposal-1",
                                             "servedBeforeApproval": False}},
        "self-approval-refused": {"error": {"type": "CommandFailed (exit 1)", "status": 403}, "observed": {"status": "AwaitingApproval"}},
        "approve": {"observed": {"status": "Succeeded"}},
        "proposal-resolved": {"observed": {"status": "Succeeded", "kind": "ServicePublish",
                                           "requestedBy": "apikey:api-key:key-proposer", "resolvedBy": "key-approver"}},
        "approved-served": {"observed": {"features": rows(proposal["features"], ("gid", "name"))}},
        "unpublish": {"observed": {"layerId": 12, "enabled": False}},
        "unpublished-refused": {"error": {"type": "CommandFailed (exit 1)", "status": 404}},
    })


def setup_view(view, names, revision):
    return {"view": view, "revision": revision, "toolCount": len(names), "names": names, "nextCursor": None}


def passing_mcp_observations(setup=None):
    default_names = PINNED_DEFAULT_VIEW
    setup_names = [f"setup_{i}" for i in range(FIXTURE["mcp"]["views"]["setup"]["toolCount"])]
    spec = FIXTURE["mcp"]["render"]
    painted = {(x, y) for x, y, inside in oracles.render_expectations(FIXTURE) if inside}
    box = lambda x, y: 40 <= x < 220 and 40 <= y < 280  # noqa: E731 - the fixture block, independently in pixels
    image = base64.b64encode(png(spec["width"], spec["height"], box)).decode()
    assert all(box(x, y) for x, y in painted)
    x, y, d = *FIXTURE["processes"]["point"], FIXTURE["processes"]["distance"]
    ring = [[x + d * math.cos(2 * math.pi * i / 32), y + d * math.sin(2 * math.pi * i / 32)] for i in range(32)]
    return workflow_observations("mcp-workflow", "honua-mcp-server", {
        "initialize-setup": {"observed": {"protocolVersion": "2025-06-18", "serverName": "honua.operator.mcp", "serverVersion": "v1"}},
        "setup-tools-list": {"observed": setup or setup_view("setup", setup_names, "setup.v2")},
        "default-tools-list": {"observed": setup_view("default", default_names, "default.v1")},
        "full-catalog-refused": {"error": {"type": "permission_denied", "status": None}},
        "full-catalog": {"observed": {"names": regression.load_tool_roster(), "pages": 5, "restoredView": "default"}},
        "read": {"observed": {"features": [{"attributes": {k: r[k] for k in ("gid", "name", "rank")}, "x": r["x"], "y": r["y"]}
                                           for r in oracles.filtered_sites(FIXTURE)]}},
        "render": {"observed": {"png": image, "mimeType": "image/png"}},
        "buffer-submit": {"observed": {"jobId": "gp-1", "status": "Queued"}},
        "buffer-poll": {"observed": {"status": "Succeeded"}},
        "buffer-result": {"observed": {"geometry": {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring + [ring[0]]]}}}},
    })


def evaluate_workflow(cell_, observations, client, plan):
    return regression.evaluate_cell(cell_, SCENARIOS[cell_["scenario"]], client, observations, FIXTURE, plan, client)


class WorkflowTests(unittest.TestCase):
    def test_cli_workflow_passes_only_with_a_separately_approved_proposal(self):
        cell_, plan = workflow_cell("npm-cli-workflow"), cli_plan()
        status, detail, rows = evaluate_workflow(cell_, passing_cli_observations(), "honua-sdk-js", plan)
        self.assertEqual((status, len(rows)), ("pass", 14), detail)
        cases = {
            "proposal-resolved": ({"observed": {"status": "Succeeded", "kind": "ServicePublish",
                                                "requestedBy": "apikey:api-key:key-proposer", "resolvedBy": "key-proposer"}},
                                  "resolved by the approver"),
            "propose-publication": ({"observed": {"status": "Completed", "requiresApproval": False, "proposalId": None}},
                                    "requires a proposal"),
            "self-approval-refused": ({"observed": {"status": "Succeeded"}}, "approved its own proposal"),
            "approved-served": ({"observed": {"features": []}}, "0 features"),
        }
        for step, (payload, message) in cases.items():
            with self.subTest(step=step):
                observations = passing_cli_observations()
                observations[("cli-workflow", step)] = {**observations[("cli-workflow", step)], **payload}
                observations[("cli-workflow", step)].pop("error", None) if "error" not in payload else None
                status, detail, _ = evaluate_workflow(cell_, observations, "honua-sdk-js", plan)
                self.assertEqual(status, "fail")
                self.assertIn(message, detail)

    def test_self_approval_needs_a_403_and_a_still_pending_proposal(self):
        ok = lambda observation: oracles.oracle_self_approval_refused(observation)[0]  # noqa: E731
        self.assertTrue(ok({"error": {"status": 403}, "observed": {"status": "AwaitingApproval"}}))
        self.assertFalse(ok({"error": {"status": 403}, "observed": {"status": "Succeeded"}}))
        self.assertFalse(ok({"error": {"status": 401}, "observed": {"status": "AwaitingApproval"}}))
        self.assertFalse(ok({"observed": {"status": "AwaitingApproval"}}))

    def test_pypi_cli_cell_is_judged_on_every_step(self):
        # honua-sdk 0.1.13 + honua-admin 0.1.10 have a command for each step (sdk-python#258): nothing is blocked,
        # and a step the Python CLI reports unsupported now fails the cell.
        cell_ = workflow_cell("pypi-cli-workflow")
        self.assertEqual((cell_["status"], cell_.get("blockedSteps")), ("active", None))
        apis = SCENARIOS["cli-workflow"]["clients"]["honua-sdk-python-wheel"]
        observations = {key: {**value, "api": apis[key[1]]} for key, value in passing_cli_observations().items()}
        status, detail, _ = evaluate_workflow(cell_, observations, "honua-sdk-python-wheel", cli_plan())
        self.assertEqual(status, "pass", detail)
        observations[("cli-workflow", "served")] = {"scenario": "cli-workflow", "step": "served", "api": apis["served"],
                                                    "unsupported": "no command"}
        self.assertEqual(evaluate_workflow(cell_, observations, "honua-sdk-python-wheel", cli_plan())[0], "fail")

    def test_proposal_must_not_be_served_before_approval(self):
        pending = {"status": "RequiresApproval", "requiresApproval": True, "proposalId": "proposal-1"}
        self.assertTrue(oracles.oracle_proposal_pending({**pending, "servedBeforeApproval": False})[0])
        ok, detail = oracles.oracle_proposal_pending({**pending, "servedBeforeApproval": True})
        self.assertFalse(ok)
        self.assertIn("served before approval", detail)
        # The absence check must have run.
        self.assertFalse(oracles.oracle_proposal_pending(pending)[0])

    def test_cli_children_inherit_no_suite_credential(self):
        with tempfile.TemporaryDirectory() as tmp:
            honua, plan = Path(tmp) / "honua", Path(tmp) / "plan.json"
            honua.write_text(f"#!{sys.executable}\nimport json, os\nprint(json.dumps(dict(os.environ)))\n")
            honua.chmod(0o700)
            plan.write_text(json.dumps({"baseUrl": "http://localhost:8080"}))
            inherited = regression.driver_env({"PATH": os.environ["PATH"], "PYTHONPATH": "/wheels", "E2E_API_KEY": "admin",
                                               "HONUA_API_KEY": "operator", "GH_TOKEN": "gh"},
                                              api_key="root", bearer="bearer", db_password="db", plan_path=plan,
                                              principals=PRINCIPALS)
            with mock.patch.dict(os.environ, inherited, clear=True):
                driver = load_driver("cli", plan)
                cli = driver.Cli("npm", str(honua), None, Path(tmp))
                seen = json.loads(cli.run(["services"], {"HONUA_ADMIN_KEY": "secret-proposer"}))
        self.assertEqual({key: value for key, value in seen.items() if key.startswith(("SDKREG_", "E2E_", "HONUA_"))},
                         {"HONUA_BASE_URL": "http://localhost:8080", "HONUA_CONFIG_HOME": str(Path(tmp) / "honua-config"),
                          "HONUA_ADMIN_KEY": "secret-proposer"})
        self.assertEqual(seen["PYTHONPATH"], "/wheels")
        self.assertNotIn("GH_TOKEN", seen)

    def test_anonymous_full_view_is_judged_on_its_first_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = Path(tmp) / "plan.json"
            plan.write_text(json.dumps({"baseUrl": "http://localhost:8080"}))
            driver = load_driver("mcp", plan)
        session = mock.Mock()
        # A first page is disclosed and only the second is refused: the observation must carry the disclosure.
        session.request.side_effect = [
            {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "honua_admin_role_list"}], "nextCursor": "c2"}},
            {"jsonrpc": "2.0", "id": 2, "error": {"code": -32603, "data": {"code": "permission_denied"}}},
        ]
        observation = {"observed": driver.anonymous_full_view(session)}
        self.assertEqual(observation["observed"]["names"], ["honua_admin_role_list"])
        self.assertFalse(oracles.oracle_mcp_permission_denied(observation)[0])
        session.request.side_effect = [{"jsonrpc": "2.0", "id": 1, "error": {"code": -32603, "data": {"code": "permission_denied"}}}]
        with self.assertRaises(driver.RpcError) as refused:
            driver.anonymous_full_view(session)
        self.assertEqual(refused.exception.kind, "permission_denied")

    def test_default_view_must_be_the_pinned_roster_and_revision(self):
        pinned = json.loads(regression.TOOL_ROSTER.read_text())["defaultView"]
        good = setup_view("default", list(PINNED_DEFAULT_VIEW), pinned["revision"])
        judge = regression.ORACLES["mcp-default-view"]
        self.assertTrue(judge(good, FIXTURE, {}, {}, {})[0])
        swapped = setup_view("default", PINNED_DEFAULT_VIEW[1:] + ["honua_admin_role_list"], pinned["revision"])
        ok, detail = judge(swapped, FIXTURE, {}, {}, {})
        self.assertFalse(ok)
        self.assertIn("honua_admin_role_list", detail)
        self.assertFalse(judge(setup_view("default", list(PINNED_DEFAULT_VIEW), "default.v2"), FIXTURE, {}, {}, {})[0])

    def test_mcp_setup_view_is_blocked_only_by_the_dropped_selector(self):
        # The committed cell is active: @honua/mcp-server 0.1.13 keeps the setup view (sdk-js#1875).
        committed, plan = workflow_cell("npm-mcp-workflow"), cli_plan()
        status, detail, _ = evaluate_workflow(committed, passing_mcp_observations(), "honua-mcp-server", plan)
        self.assertEqual(status, "pass", detail)
        # A re-blocked cell still names only the dropped selector, and a fixed proxy flips it.
        cell_ = {**committed, "status": "blocked", "blockedBy": ["https://github.com/honua-io/honua-sdk-js/issues/1875"],
                 "blockedSteps": {"setup-tools-list": {
                     "blockedBy": "https://github.com/honua-io/honua-sdk-js/issues/1875",
                     "signature": r"returned view 'default' \(default\.v1\) with 12 tools"}}}
        default = passing_mcp_observations()[("mcp-workflow", "default-tools-list")]["observed"]
        status, detail, rows = evaluate_workflow(cell_, passing_mcp_observations(setup=default), "honua-mcp-server", plan)
        self.assertEqual(status, "blocked", detail)
        self.assertEqual([row["step"] for row in rows if row["status"] == "blocked"], ["setup-tools-list"])
        # The fixed proxy returns the setup view: the matrix must be flipped.
        status, detail, _ = evaluate_workflow(cell_, passing_mcp_observations(), "honua-mcp-server", plan)
        self.assertEqual(status, "fail")
        self.assertIn("set it active", detail)
        # A truncated setup view is a different failure, not the declared blocker.
        truncated = setup_view("setup", [f"setup_{i}" for i in range(24)], "setup.v2")
        status, detail, _ = evaluate_workflow(cell_, passing_mcp_observations(setup=truncated), "honua-mcp-server", plan)
        self.assertEqual(status, "fail")
        self.assertIn("was not observed", detail)

    def test_mcp_full_catalog_must_equal_the_pinned_roster_and_restore_the_view(self):
        roster, default = regression.load_tool_roster(), PINNED_DEFAULT_VIEW
        self.assertEqual(len(roster), len(set(roster)))
        self.assertGreater(len(roster), FIXTURE["mcp"]["views"]["setup"]["toolCount"])
        full = lambda names, restored="default": {"names": names, "pages": 5, "restoredView": restored}  # noqa: E731
        self.assertTrue(oracles.oracle_mcp_full_catalog(full(roster), roster, default)[0])
        self.assertTrue(oracles.oracle_mcp_full_catalog(full(list(reversed(roster))), roster, default)[0])
        cases = {
            "duplicate": (full(roster + roster[:1]), "unique"),
            "one tool missing": (full(roster[1:]), f"missing ['{sorted(roster)[0]}']"),
            "one tool renamed": (full(roster[:-1] + ["honua_unpinned"]), "not pinned ['honua_unpinned']"),
            # The former >setup-count check passed this truncated export.
            "truncated": (full(default + [name for name in roster if name not in default][:14]), "pinned roster"),
            "view not restored": (full(roster, "full"), "restores the default view"),
        }
        for case, (observed, message) in cases.items():
            with self.subTest(case=case):
                ok, detail = oracles.oracle_mcp_full_catalog(observed, roster, default)
                self.assertFalse(ok, detail)
                self.assertIn(message, detail)
        self.assertFalse(oracles.oracle_mcp_full_catalog(full(roster), roster, None)[0])
        self.assertFalse(oracles.oracle_mcp_full_catalog(full([]), [], default)[0])
        self.assertFalse(oracles.oracle_mcp_permission_denied({"observed": {"names": roster}})[0])
        self.assertFalse(oracles.oracle_mcp_permission_denied({"error": {"type": "InvalidRequest"}})[0])

    def test_mcp_driver_anonymous_session_inherits_no_credential(self):
        # A fake installed proxy that answers tools/list with its own environment.
        with tempfile.TemporaryDirectory() as tmp:
            proxy, plan = Path(tmp) / "honua-mcp-proxy", Path(tmp) / "plan.json"
            proxy.write_text(f"#!{sys.executable}\nimport json, os, sys\nrequest = json.loads(sys.stdin.readline())\n"
                             "print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': {'env': dict(os.environ)}}), flush=True)\n")
            proxy.chmod(0o700)
            plan.write_text(json.dumps({"baseUrl": "http://localhost:8080"}))
            inherited = regression.driver_env({"PATH": os.environ["PATH"], "E2E_API_KEY": "admin", "HONUA_API_KEY": "operator",
                                               "HONUA_SERVER_URL": "http://localhost:8080"},
                                              api_key="root", bearer="bearer", db_password="db", plan_path=plan,
                                              principals=PRINCIPALS)
            with mock.patch.dict(os.environ, inherited, clear=True):
                driver = load_driver("mcp", plan)
                for key, expected in (("", {}), ("root", {"HONUA_API_KEY": "root"})):
                    with self.subTest(key=key), driver.session(str(proxy), key) as session:
                        seen = session.request("tools/list")["result"]["env"]
                        credentials = {name: value for name, value in seen.items()
                                       if name.startswith(("SDKREG_", "E2E_", "HONUA_")) and name != "HONUA_MCP_REMOTE_URL"}
                        self.assertEqual(credentials, expected)
                        self.assertFalse({"admin", "operator", "bearer", "db", "secret-proposer", "secret-approver"} & set(seen.values()))

    def test_mcp_buffer_plan_uses_the_candidates_step_and_artifact_kinds(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            plan_path.write_text(json.dumps({"baseUrl": "http://localhost:8080"}))
            plan = load_driver("mcp", plan_path).buffer_plan(FIXTURE["processes"])
        self.assertEqual([(step["stepId"], step["kind"], step["processId"]) for step in plan["steps"]],
                         [("buffer", "Geoprocess", "geometry.buffer")])
        # AnalysisPlan.Outputs are artifact kinds; the buffer-result step reads the FeatureLayer artifact.
        self.assertEqual(plan["outputs"], ["FeatureLayer"])
        self.assertEqual(set(plan["steps"][0]["inputs"]), {"wkb", "srid", "distance", "geodesic"})

    def test_cli_proposal_is_read_until_terminal_within_a_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            plan_path.write_text(json.dumps({"baseUrl": "http://localhost:8080"}))
            driver = load_driver("cli", plan_path)
        with mock.patch.object(driver.time, "sleep"):
            reads = iter([{"status": "Executing"}, {"status": "Submitted"}, {"status": "Reconciling"}, {"status": "Succeeded"}])
            self.assertEqual(driver.until_terminal(lambda: next(reads)), {"status": "Succeeded"})
            for terminal in ("Failed", "Rejected", "RolledBack", "Cancelled"):
                self.assertEqual(driver.until_terminal(lambda: {"status": terminal})["status"], terminal)
            # Past the deadline the last, non-terminal record is returned and the oracle fails it.
            clock = iter([0.0, 0.5, 1.0, 2.0])
            with mock.patch.object(driver.time, "monotonic", lambda: next(clock)):
                last = driver.until_terminal(lambda: {"status": "Submitted"}, timeout=1.0)
        self.assertEqual(last, {"status": "Submitted"})
        self.assertFalse(oracles.oracle_proposal_resolved({**last, "kind": "ServicePublish", "requestedBy": "key-proposer",
                                                           "resolvedBy": "key-approver"}, cli_plan()["principals"])[0])

    def test_render_oracle_checks_painted_and_transparent_pixels(self):
        spec = FIXTURE["mcp"]["render"]
        samples = oracles.render_expectations(FIXTURE)
        self.assertEqual(sum(1 for *_, inside in samples if inside), 3)
        self.assertEqual(len(samples), 8)
        encode = lambda data: {"png": base64.b64encode(data).decode()}  # noqa: E731
        self.assertTrue(oracles.oracle_map_render(encode(png(spec["width"], spec["height"], lambda x, y: 40 <= x < 220 and 40 <= y < 280)), FIXTURE)[0])
        self.assertFalse(oracles.oracle_map_render(encode(png(spec["width"], spec["height"], lambda x, y: False)), FIXTURE)[0])
        self.assertFalse(oracles.oracle_map_render(encode(png(spec["width"], spec["height"], lambda x, y: True)), FIXTURE)[0])
        self.assertFalse(oracles.oracle_map_render(encode(png(64, 64, lambda x, y: True)), FIXTURE)[0])
        self.assertFalse(oracles.oracle_map_render({"png": "not-base64!"}, FIXTURE)[0])

    def test_mcp_job_oracles(self):
        self.assertTrue(oracles.oracle_mcp_job_accepted({"jobId": "gp-1", "status": "Queued"})[0])
        self.assertFalse(oracles.oracle_mcp_job_accepted({"jobId": "", "status": "Queued"})[0])
        self.assertFalse(oracles.oracle_mcp_job_accepted({"jobId": "gp-1", "status": "Failed"})[0])
        self.assertTrue(oracles.oracle_mcp_job_succeeded({"status": "Succeeded"})[0])
        self.assertFalse(oracles.oracle_mcp_job_succeeded({"status": "successful"})[0])

    def test_workflow_seed_principals_and_plan_keep_credentials_out_of_the_plan(self):
        sql = regression.seed_sql(FIXTURE, ["js"], ["clijs"])
        for table in ("sdkreg_lifecycle_clijs", "sdkreg_proposal_clijs", "sdkreg_edits_js"):
            self.assertIn(f"CREATE TABLE honua_data.{table}", sql)
        self.assertNotIn("sdkreg_edits_clijs", sql)
        self.assertIn("(1, 'proposed-east', ST_SetSRID(ST_MakePoint(-104.0, 39.5), 4326))", sql)
        api = mock.Mock()
        api.request.side_effect = lambda method, path, body: {"apiKey": {"id": f"id-{body['permissions'][0]}"}, "key": f"key-{body['name']}"}
        principals = regression.mint_principals(api, "t1")
        self.assertEqual({name: value["id"] for name, value in principals.items()},
                         {"proposer": "id-admin:write", "approver": "id-admin:approve"})
        api.request.side_effect = lambda method, path, body: {"apiKey": {"id": "x"}}
        with self.assertRaisesRegex(regression.RegressionError, "could not mint the proposer"):
            regression.mint_principals(api, "t2")
        plan = cli_plan()
        self.assertEqual(plan["principals"], {"proposerId": "key-proposer", "approverId": "key-approver"})
        self.assertNotIn("secret-", json.dumps(plan))
        self.assertEqual(plan["proposal"]["service"], "sdkreg-proposal-clijs")
        env = regression.driver_env({}, api_key="k", bearer="b", db_password="p", plan_path=Path("/tmp/plan.json"),
                                    principals=PRINCIPALS)
        self.assertEqual((env["SDKREG_PROPOSER_KEY"], env["SDKREG_APPROVER_KEY"]), ("secret-proposer", "secret-approver"))

    def test_workflow_policy_overlay_gates_only_service_publish(self):
        overlay = (HERE / "compose.sdk-regression.yml").read_text()
        self.assertIn('Operations__Policy__Rules__0__OperationId: "service.publish"', overlay)
        self.assertIn('Operations__Policy__Rules__0__Decision: "RequireApproval"', overlay)
        self.assertNotIn("Rules__1__", overlay)
        self.assertNotIn("DefaultDecision", overlay)


if __name__ == "__main__":
    unittest.main()
