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
DRIVER_SOURCES = {
    "honua-sdk-python-wheel": (HERE / "drivers/python/driver.py").read_text(),
    "honua-sdk-js": (HERE / "drivers/js/driver.mjs").read_text(),
    "honua-sdk-dotnet": (HERE / "drivers/dotnet/Program.cs").read_text(),
}


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
    def test_every_scenario_names_one_api_per_step_for_every_sdk(self):
        self.assertEqual(set(SCENARIOS), {"sdk-auth", "sdk-admin-lifecycle", "sdk-geoservices", "sdk-ogc-features",
                                          "sdk-ogc-tiles", "sdk-ogc-processes", "sdk-stac"})
        for scenario in SCENARIOS.values():
            steps = [step["id"] for step in scenario["steps"]]
            for client, apis in scenario["clients"].items():
                self.assertEqual(list(apis), steps, (scenario["id"], client))

    def test_drivers_report_exactly_the_contract_apis(self):
        # A driver that drifts from the contract would make a failure name the wrong SDK API.
        for scenario in SCENARIOS.values():
            for client, apis in scenario["clients"].items():
                for step, api in apis.items():
                    self.assertIn(json.dumps(api)[1:-1], DRIVER_SOURCES[client], (scenario["id"], client, step))

    def test_drivers_never_make_raw_http_calls(self):
        self.assertNotRegex(DRIVER_SOURCES["honua-sdk-python-wheel"], r"\b(httpx|requests|urllib)\.")
        self.assertNotRegex(DRIVER_SOURCES["honua-sdk-js"], r"\bfetch\(")
        self.assertNotRegex(DRIVER_SOURCES["honua-sdk-dotnet"], r"new HttpClient|GetAsync\(\"|PostAsync\(")

    def test_scenario_validation_rejects_incomplete_contracts(self):
        base = copy.deepcopy(SCENARIOS["sdk-stac"])
        cases = [
            (lambda s: s.update(id="stac"), "sdk-\\* id"),
            (lambda s: s.update(steps=[]), "no steps"),
            (lambda s: s["steps"][0].update(oracle="vibes"), "known oracle"),
            (lambda s: s.update(receiptFields=["client", "body"]), "allowlist"),
            (lambda s: s["clients"].pop("honua-sdk-dotnet"), "every SDK client"),
            (lambda s: s["clients"]["honua-sdk-js"].update(search=""), "one API per step"),
        ]
        for change, message in cases:
            with self.subTest(message=message):
                scenario = copy.deepcopy(base)
                change(scenario)
                with self.assertRaisesRegex(regression.RegressionError, message):
                    regression.validate_scenario(scenario, "case.json")

    def test_matrix_covers_every_scenario_for_every_sdk(self):
        suite = [cell for cell in MATRIX["cells"] if cell["driver"] in run.SUITE_DRIVERS]
        self.assertEqual(len(suite), 3 * len(SCENARIOS))
        for artifact in regression.CLIENT_SLUGS:
            self.assertEqual({cell["scenario"] for cell in suite if cell["artifact"] == artifact}, set(SCENARIOS))
        for cell in suite:
            regression.validate_suite_cell(cell, SCENARIOS, run.BLOCKER)

    def test_matrix_omitting_a_scenario_is_rejected(self):
        manifest, matrix = run.load_inputs(run.ROOT / "platform-manifest.yaml", run.DEFAULT_MATRIX)
        matrix["cells"] = [cell for cell in matrix["cells"] if cell["id"] != "nuget-sdk-stac"]
        with self.assertRaisesRegex(run.CertificationError, "omits SDK regression scenarios"):
            run.validate_release_inputs(manifest, matrix)

    def test_suite_cell_validation(self):
        cell = next(c for c in MATRIX["cells"] if c["id"] == "pypi-sdk-geoservices")
        cases = [
            (lambda c: c.update(scenario="sdk-unknown"), "unknown scenario"),
            (lambda c: c.update(driver="npm-sdk"), "does not drive"),
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
        self.assertEqual(len(results), 3 * len(SCENARIOS))
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


if __name__ == "__main__":
    unittest.main()
