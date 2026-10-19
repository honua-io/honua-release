"""Controlled authored render responses; never used by a canonical producer."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import unittest
import zlib

import mcp_render_bootstrap as bootstrap_module
import mcp_render_validation as validation

HERE = Path(__file__).resolve().parent
FIXTURE = (HERE / "fixture.v1.json").read_bytes()
SOURCE = "b" * 40
IMAGE = "ghcr.io/honua-io/honua-server@sha256:" + "c" * 64


def runtime():
    return {"schema": "honua.mcp-render-runtime/v1", "running": True, "candidateSourceSha": SOURCE,
            "candidateImage": IMAGE, "imageReference": IMAGE, "imageId": "sha256:" + "d" * 64,
            "containerId": "e" * 64, "startedAt": "2026-10-19T00:00:00Z"}


def bootstrap():
    fixture = json.loads(FIXTURE)
    return {"schema": "honua.mcp-render-bootstrap/v1", "status": "provisioned", "phase": "complete",
            "candidateSourceSha": SOURCE, "candidateImage": IMAGE,
            "runtimeBinding": runtime(),
            "fixtureSha256": "sha256:" + hashlib.sha256(FIXTURE).hexdigest(),
            "seedSqlSha256": "sha256:" + hashlib.sha256(bootstrap_module.regression.seed_sql(fixture, [], []).encode()).hexdigest(),
            "published": {"area": {"service": "sdkreg-area", "layerId": 17}},
            "connectionId": "allocated-connection",
            "ownedArea": {"serviceId": "allocated-service-id", "serviceName": "sdkreg-area", "layerId": 3,
                          "storageLayerId": 17, "locator": "honua_data.sdkreg_area", "connectionId": "allocated-connection"},
            "areaReadback": [{"gid": row["gid"], "name": row["name"], "envelope": row["envelope"],
                              "srid": 4326, "geometryType": "ST_Polygon"} for row in fixture["area"]["features"]]}


def authored_png():
    # Independent affine world-to-pixel raster of the authored envelopes. Do
    # not call render_expectations or copy a server-rendered image here.
    fixture = json.loads(FIXTURE)
    render = fixture["mcp"]["render"]
    width, height = render["width"], render["height"]
    min_x, min_y, max_x, max_y = render["bbox"]
    rows = []
    for y in range(height):
        row = bytearray([0])
        world_y = max_y - (y + 0.5) * (max_y - min_y) / height
        for x in range(width):
            world_x = min_x + (x + 0.5) * (max_x - min_x) / width
            inside = any(a <= world_x <= c and b <= world_y <= d
                         for a, b, c, d in (feature["envelope"] for feature in fixture["area"]["features"]))
            row.extend((12, 90, 180, 255) if inside else (0, 0, 0, 0))
        rows.append(bytes(row))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def responses():
    scenario = validation.scenario(FIXTURE, bootstrap())
    arguments = scenario["cases"][0]["arguments"]
    png = authored_png()
    encoded = base64.b64encode(png).decode()
    structured = {key: arguments[key] for key in ("width", "height", "bbox", "bboxSrid", "layers")}
    structured.update({"format": "image/png", "byteLength": len(png), "image": {
        "format": "image/png", "width": arguments["width"], "height": arguments["height"],
        "byteLength": len(png), "inlined": True, "base64": encoded}})

    def error(code):
        value = {"status": "error", "code": code, "message": "Controlled authored negative"}
        return {"isError": True, "structuredContent": value, "content": [{"type": "text", "text": json.dumps(value)}]}

    return [{"isError": False, "structuredContent": structured, "content": [
        {"type": "text", "text": "Controlled authored render"}, {"type": "image", "data": encoded, "mimeType": "image/png"}]},
        error("invalid_argument"), error("not_found")]


def observations():
    value = validation.scenario(FIXTURE, bootstrap())
    for case, result in zip(value["cases"], responses(), strict=True):
        case["result"] = result
    return value


class RenderControls(unittest.TestCase):
    def test_authored_geometry_and_all_three_requests_pass(self):
        result = validation.judge(FIXTURE, bootstrap(), observations())
        self.assertTrue(all(result[key] for key in ("positive", "negative", "mediaSchema")))

    def test_corrupt_png_conflicting_representations_and_missing_media_refuse(self):
        for mutation in ("corrupt", "mime", "missing", "wrong-size", "wrong-bbox", "wrong-layer", "json-conflict", "bad-byte-length"):
            with self.subTest(mutation=mutation):
                value = observations()
                result = value["cases"][0]["result"]
                if mutation == "corrupt":
                    result["content"][1]["data"] = result["structuredContent"]["image"]["base64"] = base64.b64encode(b"not PNG").decode()
                elif mutation == "mime": result["content"][1]["mimeType"] = "image/jpeg"
                elif mutation == "missing": result["content"].pop()
                elif mutation == "wrong-size": result["structuredContent"]["image"]["width"] += 1
                elif mutation == "wrong-bbox": result["structuredContent"]["bbox"] = [0, 0, 1, 1]
                elif mutation == "wrong-layer": result["structuredContent"]["layers"] = [{"serviceId": "other", "layerId": 17}]
                elif mutation == "json-conflict": result["content"][0]["text"] = "{}"
                else: result["structuredContent"]["byteLength"] = True
                with self.assertRaises((ValueError, TypeError)):
                    validation.judge(FIXTURE, bootstrap(), value)

    def test_negative_type_and_authored_requests_cannot_be_substituted(self):
        for mutation in ("typed", "accepted", "conflict", "arguments", "missing-case", "duplicate-case", "bootstrap", "readback"):
            with self.subTest(mutation=mutation):
                value, binding = observations(), bootstrap()
                if mutation == "typed": value["cases"][1]["result"]["structuredContent"]["code"] = "internal"
                elif mutation == "accepted": value["cases"][2]["result"]["isError"] = False
                elif mutation == "conflict": value["cases"][2]["result"]["content"][0]["text"] = "{}"
                elif mutation == "arguments": value["cases"][2]["arguments"]["layers"][0]["layerId"] -= 1
                elif mutation == "missing-case": value["cases"].pop()
                elif mutation == "duplicate-case": value["cases"][2] = copy.deepcopy(value["cases"][1])
                elif mutation == "bootstrap": binding["candidateSourceSha"] = "d" * 40
                else: binding["areaReadback"][0]["envelope"][0] -= 1
                with self.assertRaises((ValueError, TypeError)):
                    validation.judge(FIXTURE, binding, value)

    def test_provision_calls_genuine_seed_and_publication_and_retains_allocations(self):
        expected = bootstrap()["areaReadback"]
        sql_calls, api_calls = [], []

        def run(command, **kwargs):
            sql_calls.append((command, kwargs))
            output = json.dumps(expected) if len(sql_calls) == 2 else json.dumps([bootstrap()["ownedArea"]]) if len(sql_calls) == 3 else ""
            return subprocess.CompletedProcess(command, 0, output, "")

        class Api:
            def __init__(self, base_url, api_key):
                self.base_url, self.api_key = base_url, api_key

            def request(self, method, path, body):
                api_calls.append((method, path, body))
                if path == "/api/v1/admin/connections/": return {"connectionId": "allocated-connection"}
                return {"layerId": 17 if body["table"] == "sdkreg_area" else 16}

        receipt = bootstrap_module.provision(FIXTURE, candidate_source_sha=SOURCE, candidate_image=IMAGE,
            database_password="private-db-password", api_key="private-api-key", base_url="http://localhost:8080",
            runtime_binding=runtime(), psql_run=run, api_factory=Api)
        self.assertEqual("provisioned", receipt["status"])
        self.assertEqual(expected, receipt["areaReadback"])
        self.assertEqual(17, receipt["published"]["area"]["layerId"])
        self.assertEqual(bootstrap()["ownedArea"], receipt["ownedArea"])
        self.assertEqual(bootstrap_module.regression.seed_sql(json.loads(FIXTURE), [], []), sql_calls[0][1]["input"])
        self.assertIn("ST_XMin", sql_calls[1][1]["input"])
        self.assertEqual("honua_test", api_calls[0][2]["databaseName"])
        self.assertEqual("sdkreg_area", api_calls[2][2]["table"])
        self.assertNotIn("private-db-password", json.dumps(receipt))
        self.assertNotIn("private-api-key", json.dumps(receipt))

    def test_bootstrap_refuses_wrong_candidate_remote_host_and_bad_readback(self):
        for mutation in ("source", "image", "remote", "readback", "sql-error"):
            with self.subTest(mutation=mutation):
                api_calls = []
                def run(command, **kwargs):
                    return subprocess.CompletedProcess(command, 1 if mutation == "sql-error" else 0, "[]", "private-message")
                def api(*args):
                    api_calls.append(args)
                    raise AssertionError("unverified seed must never publish")
                with self.assertRaises(ValueError):
                    bootstrap_module.provision(FIXTURE, candidate_source_sha="trunk" if mutation == "source" else SOURCE,
                        candidate_image="latest" if mutation == "image" else IMAGE,
                        database_password="secret", api_key="secret", base_url="http://remote.invalid" if mutation == "remote" else "http://localhost:8080",
                        runtime_binding=runtime(), psql_run=run, api_factory=api)
                self.assertEqual([], api_calls)

    def test_started_container_digest_and_source_are_independently_read(self):
        for mutation in (None, "stopped", "container-image", "image-id", "source", "digest", "container-id", "started"):
            with self.subTest(mutation=mutation):
                container, calls = runtime(), []
                image = {"imageId": container["imageId"], "sourceSha": SOURCE, "repoDigests": [IMAGE]}
                if mutation == "stopped": container["running"] = False
                elif mutation == "container-image": container["imageReference"] = "latest"
                elif mutation == "image-id": image["imageId"] = "sha256:" + "f" * 64
                elif mutation == "source": image["sourceSha"] = "f" * 40
                elif mutation == "digest": image["repoDigests"] = []
                elif mutation == "container-id": container["containerId"] = "invalid"
                elif mutation == "started": container["startedAt"] = ""
                def run(command, **kwargs):
                    calls.append(command)
                    return subprocess.CompletedProcess(command, 0, json.dumps(container if len(calls) == 1 else image), "")
                if mutation is None:
                    self.assertEqual(runtime(), bootstrap_module.inspect_candidate(SOURCE, IMAGE, run=run))
                    self.assertEqual("honua-server", calls[0][-1])
                    self.assertEqual(container["imageId"], calls[1][-1])
                else:
                    with self.assertRaises(ValueError):
                        binding = bootstrap_module.inspect_candidate(SOURCE, IMAGE, run=run)
                        bootstrap_module.provision(FIXTURE, candidate_source_sha=SOURCE, candidate_image=IMAGE,
                            database_password="secret", api_key="secret", base_url="http://localhost:8080",
                            runtime_binding=binding, psql_run=lambda *a, **k: self.fail("must reject before SQL"))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--write-controls":
        directory = Path(sys.argv[2])
        (directory / "bootstrap.json").write_text(json.dumps(bootstrap()))
        (directory / "responses.json").write_text(json.dumps(responses()))
    else:
        unittest.main()
