"""Admin list replies are pages ({success, data: {items, page, pageSize, total}}); the drivers read the rows."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
JS_DRIVER = (HERE / "drivers/js/driver.mjs").read_text(encoding="utf-8")


def page(items, number, total):
    return {"success": True, "data": {"items": items, "page": number, "pageSize": 2, "total": total}}


def load_cli_driver(directory: Path):
    plan = directory / "plan.json"
    plan.write_text(json.dumps({"baseUrl": "http://localhost:8080"}), encoding="utf-8")
    with mock.patch.dict(os.environ, {"SDKREG_PLAN": str(plan)}):
        spec = importlib.util.spec_from_file_location("cli_driver_pages_under_test", HERE / "drivers/cli/driver.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class CliPageRowsTests(unittest.TestCase):
    def test_page_rows_reads_the_items_of_an_enveloped_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = load_cli_driver(Path(tmp))
        rows = [{"layerId": 7, "enabled": True}]
        self.assertEqual(driver.page_rows(page(rows, 1, 1)), rows)
        self.assertEqual(driver.page_rows({"items": rows}), rows)
        self.assertEqual(driver.page_rows(rows), rows)

    def test_page_rows_rejects_a_reply_without_an_item_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = load_cli_driver(Path(tmp))
        with self.assertRaises(TypeError):
            driver.page_rows({"success": True, "data": {"page": 1}})


class PypiLayerIdTests(unittest.TestCase):
    """honua-admin 0.3.x replies name the layer ``storageLayerId``/``storage_layer_id`` (rc.2 gate run 44832532862)."""

    def test_cli_reads_storage_layer_id_then_falls_back_to_layer_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            driver = load_cli_driver(Path(tmp))
        self.assertEqual(driver.pypi_layer_id({"storageLayerId": 7, "layerName": "sites"}), 7)
        self.assertEqual(driver.pypi_layer_id({"layerId": 5}), 5)
        with self.assertRaises(KeyError):
            driver.pypi_layer_id({"layerName": "sites"})

    def test_python_driver_reads_the_layer_id_only_through_the_helper(self):
        source = (HERE / "drivers/python/driver.py").read_text(encoding="utf-8")
        direct = [line for line in source.splitlines() if re.search(r"\.layer_id\b", line)]
        self.assertEqual(direct, ["    return found if found is not None else layer.layer_id"])


def js_helpers() -> str:
    """The driver's unwrap and listAll, cut out so they run without the installed SDK packages."""
    unwrap = re.search(r"^const unwrap = .*$", JS_DRIVER, re.MULTILINE)
    list_all = re.search(r"^async function listAll\(.*?^}$", JS_DRIVER, re.MULTILINE | re.DOTALL)
    if unwrap is None or list_all is None:
        raise AssertionError("driver.mjs no longer defines unwrap and listAll")
    return f"{unwrap.group(0)}\n{list_all.group(0)}\n"


class JsCallSiteTests(unittest.TestCase):
    def test_both_admin_list_steps_read_every_page(self):
        self.assertRegex(JS_DRIVER, r'await listAll\([^;]*"listServices"')
        self.assertRegex(JS_DRIVER, r'await listAll\(admin, "getPublishedLayers"')
        self.assertNotRegex(JS_DRIVER, r'call\("(listServices|getPublishedLayers)"')


@unittest.skipIf(shutil.which("node") is None, "node is not installed")
class JsListAllTests(unittest.TestCase):
    def run_list_all(self, pages):
        script = js_helpers() + f"""
const pages = {json.dumps(pages)};
const calls = [];
const client = {{ call: async (operationId, request) => {{
  calls.push({{ operationId, request }});
  return {{ operationId, data: pages[request.query.page - 1] ?? pages.at(-1) }};
}} }};
const items = await listAll(client, "getPublishedLayers", {{ path: {{ id: "c1" }}, query: {{ serviceName: "s" }} }});
process.stdout.write(JSON.stringify({{ items, calls }}));
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list_all.mjs"
            path.write_text(script, encoding="utf-8")
            done = subprocess.run(["node", str(path)], capture_output=True, text=True, check=True, timeout=60)
        return json.loads(done.stdout)

    def test_list_all_reads_every_page_and_keeps_the_request(self):
        result = self.run_list_all([page([{"n": 1}, {"n": 2}], 1, 3), page([{"n": 3}], 2, 3)])
        self.assertEqual(result["items"], [{"n": 1}, {"n": 2}, {"n": 3}])
        self.assertEqual([call["request"]["query"] for call in result["calls"]],
                         [{"serviceName": "s", "page": 1}, {"serviceName": "s", "page": 2}])
        self.assertEqual(result["calls"][0]["request"]["path"], {"id": "c1"})

    def test_list_all_stops_on_an_empty_page_and_accepts_a_bare_array(self):
        self.assertEqual(self.run_list_all([page([], 1, 5)])["items"], [])
        self.assertEqual(self.run_list_all([{"success": True, "data": [{"n": 1}]}])["items"], [{"n": 1}])


if __name__ == "__main__":
    unittest.main()
