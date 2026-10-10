import copy
import importlib.util
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("installed_cert", HERE / "run.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def inputs():
    return yaml.safe_load((HERE.parents[1] / "platform-manifest.yaml").read_text()), json.loads((HERE / "matrix.json").read_text())


def legacy_inputs():
    """The committed matrix without the SDK regression cells, which test_regression.py covers."""
    manifest, matrix = inputs()
    matrix["cells"] = [cell for cell in matrix["cells"] if cell["driver"] not in mod.SUITE_DRIVERS]
    return manifest, matrix


def reblocked_setup_inputs():
    """legacy_inputs with the setup-view cell blocked again, as it was before 0.1.13 fixed sdk-js#1875."""
    manifest, matrix = legacy_inputs()
    cell = next(c for c in matrix["cells"] if c["id"] == "npm-mcp-setup-view-tools-list")
    cell.update(status="blocked", blockedBy=mod.SETUP_BLOCKER)
    return manifest, matrix


class InstalledCertificationTests(unittest.TestCase):
    def test_release_mode_rejects_each_omitted_artifact(self):
        manifest, matrix = inputs()
        for artifact in manifest["clientArtifacts"]:
            with self.subTest(artifact=artifact):
                reduced = copy.deepcopy(matrix)
                reduced["cells"] = [c for c in reduced["cells"] if c["artifact"] != artifact]
                with self.assertRaisesRegex(mod.CertificationError, "omits required"):
                    mod.validate_release_inputs(manifest, reduced)

    def test_admin_import_failure_fails_certification(self):
        pin = inputs()[0]["clientArtifacts"]["honua-admin-python-wheel"].copy()
        wheel = b"test archive bytes"
        pin["digest"] = "sha256:" + hashlib.sha256(wheel).hexdigest()
        metadata = {"urls": [{"filename": pin["filename"], "url": "https://example.invalid/wheel"}]}
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "install"

            def run(cmd, **kwargs):
                if "install" in cmd:
                    package = work / "site-packages" / "honua_admin"
                    package.mkdir()
                    (package / "__init__.py").touch()
                failed = "-c" in cmd
                return subprocess.CompletedProcess(cmd, int(failed), "", "missing dependency" if failed else "")

            with mock.patch.object(mod.urllib.request, "urlopen", side_effect=[
                io.BytesIO(json.dumps(metadata).encode()), io.BytesIO(wheel)
            ]), mock.patch.object(mod, "_run", side_effect=run):
                ok, detail = mod.install_pypi(pin, work)
            self.assertFalse(ok)
            self.assertIn("installed admin import failed", detail)

    def test_committed_release_inputs_are_exact(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            mod.validate_release_inputs(*inputs())

    def test_release_mode_rejects_floating_versions(self):
        for version in ["latest", "1.*", "^1.2.3", "local"]:
            with self.subTest(version=version), mock.patch.dict(os.environ, {}, clear=True):
                manifest, matrix = inputs()
                manifest["clientArtifacts"]["honua-sdk-js"]["version"] = version
                with self.assertRaises(mod.CertificationError):
                    mod.validate_release_inputs(manifest, matrix)

    def test_release_mode_rejects_local_server_override(self):
        with mock.patch.dict(os.environ, {"HONUA_SERVER_IMAGE": "local:test"}, clear=True):
            with self.assertRaises(mod.CertificationError):
                mod.validate_release_inputs(*inputs())

    def test_receipt_materializes_every_non_pass(self):
        manifest, matrix = legacy_inputs()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            mod, "install_npm", return_value=(True, "ok")
        ), mock.patch.object(mod, "install_pypi", return_value=(False, "digest mismatch")), mock.patch.object(
            mod, "install_nuget", return_value=(True, "ok")
        ):
            receipt = mod.execute(manifest, matrix, "https://example.invalid/evidence/1")
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(
            {r["cell"]: r["status"] for r in receipt["results"]},
            {
                "npm-node-geoservices-error": "pass",
                "npm-mcp-tools-list": "pass",
                "npm-mcp-setup-view-tools-list": "fail",
                "pypi-python-geoservices-error": "fail",
                "pypi-admin-clean-install": "fail",
                "nuget-net10-geoservices-error": "pass",
                "nuget-service-layer-import-fidelity": "blocked",
            },
        )
        setup = next(r for r in receipt["results"] if r["cell"] == "npm-mcp-setup-view-tools-list")
        self.assertNotIn("blockedBy", setup)
        self.assertIn("needs a live candidate", setup["detail"])
        imported = next(r for r in receipt["results"] if r["cell"] == "nuget-service-layer-import-fidelity")
        self.assertIn("missing evidence is not a pass", imported["detail"])
        self.assertEqual(len(mod.verify_receipt(matrix, receipt)), 3)

    def test_matrix_includes_mcp_consumer(self):
        _, matrix = inputs()
        self.assertTrue(any(cell["artifact"] == "honua-mcp-server" for cell in matrix["cells"]))


class MatrixExpectationTests(unittest.TestCase):
    """The committed matrix is the only source of expected outcomes; these literals pin it."""

    def test_committed_matrix_expectations(self):
        _, matrix = inputs()
        self.assertEqual(
            {cell["id"]: (cell["status"], cell.get("blockedBy")) for cell in matrix["cells"]},
            {
                "npm-node-geoservices-error": ("active", None),
                "npm-mcp-tools-list": ("active", None),
                # @honua/mcp-server 0.1.14 retains the initialize-bound setup view (sdk-js#1875).
                "npm-mcp-setup-view-tools-list": ("active", None),
                "pypi-python-geoservices-error": ("active", None),
                "pypi-admin-clean-install": ("active", None),
                "nuget-net10-geoservices-error": ("active", None),
                "nuget-service-layer-import-fidelity": (
                    "blocked", "https://github.com/honua-io/honua-release/issues/418"
                ),
                **{f"{driver}-{scenario}": ("active", None)
                   for driver in ("npm-sdk", "pypi-sdk", "nuget-sdk")
                   for scenario in ("auth", "admin-lifecycle", "ogc-features", "ogc-processes", "stac")},
                "npm-sdk-ogc-tiles": ("active", None),
                "npm-sdk-geoservices": ("blocked", [
                    "https://github.com/honua-io/honua-sdk-js/issues/1894",
                    "https://github.com/honua-io/honua-server/issues/5407",
                ]),
                "pypi-sdk-geoservices": ("blocked", [
                    "https://github.com/honua-io/honua-sdk-python/issues/236",
                    "https://github.com/honua-io/honua-server/issues/5407",
                ]),
                "pypi-sdk-ogc-tiles": ("blocked", ["https://github.com/honua-io/honua-sdk-python/issues/255"]),
                "nuget-sdk-geoservices": ("blocked", ["https://github.com/honua-io/honua-server/issues/5407"]),
                "nuget-sdk-ogc-tiles": ("blocked", ["https://github.com/honua-io/honua-sdk-dotnet/issues/405"]),
                "npm-cli-workflow": ("active", None),
                "pypi-cli-workflow": ("active", None),
                "npm-mcp-workflow": ("active", None),
                "interop-publish-query-edit": ("blocked", [
                    "https://github.com/honua-io/honua-sdk-dotnet/issues/410",
                    "https://github.com/honua-io/honua-sdk-python/issues/236",
                ]),
                "interop-import-render-buffer": ("active", None),
                "interop-proposal-approval": ("blocked", [
                    "https://github.com/honua-io/honua-sdk-dotnet/issues/411",
                    "https://github.com/honua-io/honua-server/issues/5449",
                    "https://github.com/honua-io/honua-server/issues/5788",
                ]),
                "interop-api-key-revocation": ("active", None),
            },
        )

    def test_setup_view_cell_is_the_journey_contract(self):
        _, matrix = inputs()
        cell = next(c for c in matrix["cells"] if c["id"] == "npm-mcp-setup-view-tools-list")
        self.assertEqual(cell["artifact"], "honua-mcp-server")
        self.assertEqual(cell["driver"], "npm-mcp-setup-view")
        self.assertEqual(cell["expect"], {"workflowView": "setup", "toolCount": 37})

    def test_workflow_cells_block_only_what_the_pinned_clients_cannot_do(self):
        _, matrix = inputs()
        cells = {c["id"]: c for c in matrix["cells"]}
        # The pinned 0.1.14 proxy retains the setup selector (sdk-js#1875): every MCP step must pass.
        self.assertNotIn("blockedSteps", cells["npm-mcp-workflow"])
        # honua-sdk 0.1.13 + honua-admin 0.1.10 ship a command for every step (sdk-python#258).
        self.assertNotIn("blockedSteps", cells["pypi-cli-workflow"])

    def test_mcp_executables_have_explicit_contracts(self):
        _, matrix = inputs()
        cell = next(c for c in matrix["cells"] if c["id"] == "npm-mcp-tools-list")
        self.assertEqual(
            cell["executables"],
            {"honua-mcp": "mcp-stdio", "honua-mcp-proxy": "mcp-proxy", "honua-zero-to-map-release": "help"},
        )

    def test_validation_rejects_implicit_expectations(self):
        cases = [
            ({"status": "blocked"}, "blocking issue URL"),
            ({"status": "blocked", "blockedBy": "#1875"}, "blocking issue URL"),
            ({"status": "active", "blockedBy": "https://github.com/honua-io/honua-sdk-js/issues/1875"}, "cannot carry"),
            ({"status": "skipped"}, "active or blocked"),
            ({"driver": "npm-mcp"}, "every executable"),
            ({"driver": "npm-mcp", "executables": {"honua-mcp": "version"}}, "every executable"),
            ({"driver": "npm-mcp-setup-view"}, "positive toolCount"),
            ({"driver": "npm-mcp-setup-view", "expect": {"workflowView": "setup", "toolCount": True}}, "positive toolCount"),
            ({"driver": "nuget-feed"}, "unknown driver"),
        ]
        for change, message in cases:
            with self.subTest(change=change):
                cell = {"id": "c", "artifact": "honua-sdk-js", "driver": "npm", "scenario": "s", "status": "active"}
                cell.update(change)
                with self.assertRaisesRegex(mod.CertificationError, message):
                    mod.validate_cell(cell)

    def test_all_active_passing_exits_zero_with_blocked_cells_reported(self):
        manifest, matrix = legacy_inputs()
        with mock.patch.dict(os.environ, {"HONUA_SERVER_URL": "http://127.0.0.1:9"}, clear=True), mock.patch.object(
            mod, "install_npm", return_value=(True, "ok")
        ), mock.patch.object(mod, "install_pypi", return_value=(True, "ok")), mock.patch.object(
            mod, "install_nuget", return_value=(True, "ok")
        ), mock.patch.object(mod, "probe_setup_view", return_value=(True, "25 tools")):
            receipt = mod.execute(manifest, matrix, "https://example.invalid/evidence/1")
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(
            [r["status"] for r in receipt["results"]],
            ["pass", "pass", "pass", "pass", "pass", "pass", "blocked"],
        )
        self.assertEqual(mod.verify_receipt(matrix, receipt), [])
        self.assertEqual(self._main(receipt), 0)

    def test_failed_active_cell_exits_one(self):
        manifest, matrix = legacy_inputs()
        with mock.patch.dict(os.environ, {"HONUA_SERVER_URL": "http://127.0.0.1:9"}, clear=True), mock.patch.object(
            mod, "install_npm", return_value=(True, "ok")
        ), mock.patch.object(mod, "install_pypi", return_value=(True, "ok")), mock.patch.object(
            mod, "install_nuget", return_value=(False, "restored NuGet package digest mismatch")
        ), mock.patch.object(mod, "probe_setup_view", return_value=(True, "25 tools")):
            receipt = mod.execute(manifest, matrix, "https://example.invalid/evidence/1")
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(
            mod.verify_receipt(matrix, receipt),
            ["nuget-net10-geoservices-error: active cell is fail: restored NuGet package digest mismatch"],
        )
        self.assertEqual(self._main(receipt), 1)

    def test_blocked_cell_that_passes_is_not_silent(self):
        manifest, matrix = reblocked_setup_inputs()
        with mock.patch.dict(os.environ, {"HONUA_SERVER_URL": "http://127.0.0.1:9"}, clear=True), mock.patch.object(
            mod, "install_npm", return_value=(True, "ok")
        ), mock.patch.object(mod, "install_pypi", return_value=(True, "ok")), mock.patch.object(
            mod, "install_nuget", return_value=(True, "ok")
        ), mock.patch.object(mod, "probe_setup_view", return_value=(True, "25 tools")) as probe:
            receipt = mod.execute(manifest, matrix, "https://example.invalid/evidence/1")
        self.assertEqual(probe.call_args.args[1], "http://127.0.0.1:9/mcp")
        setup = next(r for r in receipt["results"] if r["cell"] == "npm-mcp-setup-view-tools-list")
        self.assertEqual(setup["status"], "fail")
        self.assertIn("set it active in matrix.json", setup["detail"])
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(len(mod.verify_receipt(matrix, receipt)), 1)

    def test_unexpected_blocked_cell_failures_remain_fatal(self):
        manifest, matrix = legacy_inputs()
        setup_cell = matrix["cells"][2]
        import_cell = matrix["cells"][-1]
        for detail in ("npm download failed", "npm archive integrity mismatch", "npm install failed"):
            with self.subTest(detail=detail), mock.patch.object(mod, "install_npm", return_value=(False, detail)):
                observed, reason = mod.run_cell(setup_cell, manifest, Path("unused"), None)
            self.assertEqual(mod.classify(setup_cell, observed, reason), ("fail", detail))
        with mock.patch.dict(os.environ, {"HONUA_SERVER_URL": "http://127.0.0.1:9"}), mock.patch.object(
            mod, "install_npm", return_value=(True, "ok")
        ), mock.patch.object(mod, "probe_setup_view", return_value=(False, "invalid MCP evidence")):
            observed, reason = mod.run_cell(setup_cell, manifest, Path("unused"), None)
        self.assertEqual(mod.classify(setup_cell, observed, reason), ("fail", "invalid MCP evidence"))
        for receipt in ({}, {"schemaVersion": 999}):
            observed, reason = mod.run_cell(import_cell, manifest, Path("unused"), receipt)
            self.assertEqual(mod.classify(import_cell, observed, reason)[0], "fail")
        self.assertEqual(mod.classify(setup_cell, f"blocked:{mod.IMPORT_BLOCKER}", "wrong blocker")[0], "fail")

    def test_unexpected_blocked_failure_exits_one(self):
        manifest, matrix = legacy_inputs()
        for failed_cell in (matrix["cells"][2], matrix["cells"][-1]):
            def run(cell, *args):
                if cell == failed_cell:
                    return "fail", "unexpected infrastructure/evidence failure"
                if cell["status"] == "blocked":
                    return f"blocked:{cell['blockedBy']}", "expected blocker"
                return "pass", "ok"
            with self.subTest(cell=failed_cell["id"]), mock.patch.object(mod, "run_cell", side_effect=run):
                receipt = mod.execute(manifest, matrix, "https://example.invalid/evidence/1")
                self.assertEqual(receipt["status"], "fail")
                self.assertEqual(len(mod.verify_receipt(matrix, receipt)), 1)
                self.assertEqual(self._main(receipt), 1)

    def test_verify_receipt_requires_every_matrix_cell(self):
        _, matrix = legacy_inputs()
        results = [
            {"cell": c["id"], "status": "blocked" if c["status"] == "blocked" else "pass", "blockedBy": c.get("blockedBy")}
            for c in matrix["cells"]
        ]
        receipt = {"status": "blocked", "results": results[:-1]}
        self.assertIn(
            "receipt cells do not match the matrix cells one-to-one and in order",
            mod.verify_receipt(matrix, receipt),
        )
        wrong_blocker = copy.deepcopy(results)
        wrong_blocker[-1]["blockedBy"] = "https://github.com/honua-io/honua-release/issues/57"
        self.assertEqual(len(mod.verify_receipt(matrix, {"status": "blocked", "results": wrong_blocker})), 1)
        self.assertEqual(
            mod.verify_receipt(matrix, {"status": "pass", "results": results}),
            ["receipt status 'pass' does not follow its cell results"],
        )

    def _main(self, receipt):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            mod, "execute", return_value=receipt
        ), mock.patch.object(
            mod.sys, "argv",
            ["run.py", "--evidence-uri", "https://example.invalid/evidence/1", "--output", str(Path(tmp) / "r.json"),
             "--matrix", str(Path(tmp) / "matrix.json")],
        ), mock.patch("builtins.print"):
            (Path(tmp) / "matrix.json").write_text(json.dumps(legacy_inputs()[1]))
            code = mod.main()
            verify_argv = ["run.py", "--verify-receipt", str(Path(tmp) / "r.json"), "--matrix", str(Path(tmp) / "matrix.json")]
            with mock.patch.object(mod.sys, "argv", verify_argv), mock.patch.object(mod.sys, "stderr", io.StringIO()):
                self.assertEqual(mod.main(), code)
        return code


FAKE_MCP = r"""#!{python}
import json, os, sys
# The proxy environment is scrubbed, so the test configures this fake through a file beside it.
config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake.json")
config = json.load(open(config_path)) if os.path.exists(config_path) else {{}}
contract = config.get("FAKE_CONTRACT", "proxy")
if contract == "proxy" and not os.environ.get("HONUA_MCP_REMOTE_URL", "").endswith("/mcp"):
    sys.exit("Fatal: HONUA_MCP_REMOTE_URL environment variable is required")
if contract == "stdio" and not os.environ.get("HONUA_BASE_URL"):
    sys.exit("Fatal: HONUA_BASE_URL environment variable is required.")
view = "default"
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        if config.get("FAKE_PRESERVE") == "1":
            view = message["params"].get("_meta", {{}}).get("honua.io/workflow-view", "default")
        result = {{"protocolVersion": "2025-06-18", "capabilities": {{}}, "serverInfo": {{"name": "fake", "version": "1"}}}}
    elif method == "tools/list":
        count = 37 if view == "setup" else 12
        tools = [{{"name": f"tool_{{i}}", "inputSchema": {{"type": "object"}}}} for i in range(count)]
        result = {{"tools": tools, "_meta": {{"view": view, "revision": {{"default": "default.v1", "setup": "setup.v3"}}.get(view, view + ".v1"), "toolCount": count}}}}
    else:
        continue
    if method == "initialize" and "FAKE_INITIALIZE" in config:
        result = json.loads(config["FAKE_INITIALIZE"])
    if method == "tools/list" and "FAKE_CATALOG" in config:
        result = json.loads(config["FAKE_CATALOG"])
    sys.stdout.write(json.dumps({{"jsonrpc": "2.0", "id": message["id"], "result": result}}) + "\n")
    sys.stdout.flush()
"""


class McpExchangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proxy = Path(self.tmp.name) / "honua-mcp-proxy"
        self.proxy.write_text(FAKE_MCP.format(python=sys.executable))
        self.proxy.chmod(0o755)
        self.expect = {"workflowView": "setup", "toolCount": 37}

    @contextlib.contextmanager
    def fake(self, config):
        path = self.proxy.parent / "fake.json"
        path.write_text(json.dumps(config))
        try:
            yield
        finally:
            path.unlink()

    def test_published_proxy_that_drops_the_setup_view_fails(self):
        with self.fake({"FAKE_PRESERVE": "0"}):
            with self.assertRaisesRegex(mod.ExpectedBlocker, "view='default' revision='default.v1' tools=12"):
                mod.probe_setup_view(self.proxy, "http://127.0.0.1:9/mcp", self.expect)

    def test_proxy_that_preserves_the_setup_view_passes(self):
        with self.fake({"FAKE_PRESERVE": "1"}):
            ok, detail = mod.probe_setup_view(self.proxy, "http://127.0.0.1:9/mcp", self.expect)
        self.assertTrue(ok, detail)
        self.assertIn("with 37 tools", detail)

    def test_setup_view_tool_count_is_exact(self):
        with self.fake({"FAKE_PRESERVE": "1"}):
            ok, _ = mod.probe_setup_view(self.proxy, "http://127.0.0.1:9/mcp", {"workflowView": "setup", "toolCount": 36})
        self.assertFalse(ok)

    def test_mcp_executable_contracts_launch_the_installed_shim_configured(self):
        with self.fake({"FAKE_CONTRACT": "proxy"}):
            self.assertEqual(mod.mcp_tools_list(self.proxy, "mcp-proxy", "http://127.0.0.1:9"), (True, "12 tools"))
        with self.fake({"FAKE_CONTRACT": "stdio"}):
            self.assertEqual(mod.mcp_tools_list(self.proxy, "mcp-stdio", "http://127.0.0.1:9"), (True, "12 tools"))
        # An inherited HONUA_BASE_URL never reaches the proxy, so it cannot satisfy the stdio contract.
        with self.fake({"FAKE_CONTRACT": "stdio"}), mock.patch.dict(os.environ, {"HONUA_BASE_URL": "http://127.0.0.1:9"}):
            ok, detail = mod.mcp_tools_list(self.proxy, "mcp-proxy", "http://127.0.0.1:9")
        self.assertFalse(ok)
        self.assertIn("live tools/list failed", detail)

    def test_invalid_initialize_results_fail_both_executables_and_setup_view(self):
        valid = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fake", "version": "1"}}
        invalid = [None, {}, {**valid, "protocolVersion": "2024-11-05"},
                   {**valid, "serverInfo": None}]
        for field in ("name", "version"):
            for value in (None, "", 7):
                invalid.append({**valid, "serverInfo": {**valid["serverInfo"], field: value}})
        for result in invalid:
            for contract in ("mcp-proxy", "mcp-stdio", "setup"):
                with self.subTest(result=result, contract=contract), self.fake({"FAKE_INITIALIZE": json.dumps(result),
                                 "FAKE_CONTRACT": "stdio" if contract == "mcp-stdio" else "proxy"}
                ):
                    if contract == "setup":
                        ok, detail = mod.probe_setup_view(self.proxy, "http://127.0.0.1:9/mcp", self.expect)
                    else:
                        ok, detail = mod.mcp_tools_list(self.proxy, contract, "http://127.0.0.1:9")
                    self.assertFalse(ok, detail)
                    self.assertIn("proxy response result/error must be an object" if result is None else "initialize omitted", detail)

    def test_invalid_tool_names_fail_live_catalog_validation(self):
        for tool in ({}, {"name": None}, {"name": ""}, {"name": 7}, {"name": []},
                     {"name": "valid"}, "not an object"):
            for contract in ("mcp-proxy", "mcp-stdio"):
                with self.subTest(tool=tool, contract=contract), self.fake({
                    "FAKE_CATALOG": json.dumps({"tools": [{"name": "valid"}, tool]}),
                    "FAKE_CONTRACT": "stdio" if contract == "mcp-stdio" else "proxy",
                }):
                    ok, detail = mod.mcp_tools_list(self.proxy, contract, "http://127.0.0.1:9")
                    self.assertFalse(ok)
                    self.assertIn("malformed catalog", detail)

    def test_only_complete_known_default_view_is_an_expected_blocker(self):
        default = {"tools": [{"name": f"tool_{i}"} for i in range(12)],
                   "_meta": {"view": "default", "revision": "default.v1", "toolCount": 12}}
        malformed = [None, {}, {**default, "nextCursor": "more"},
                     {**default, "tools": default["tools"][:-1] + [{}]},
                     {**default, "_meta": {**default["_meta"], "revision": "unknown"}}]
        for result in malformed:
            with self.subTest(result=result), self.fake({"FAKE_CATALOG": json.dumps(result)}):
                ok, detail = mod.probe_setup_view(self.proxy, "http://127.0.0.1:9/mcp", self.expect)
            self.assertFalse(ok, detail)

    def test_installed_executables_must_match_the_matrix_contract(self):
        pin = inputs()[0]["clientArtifacts"]["honua-mcp-server"]
        work = Path(self.tmp.name) / "install"

        def run(cmd, **kwargs):
            package = work / "node_modules" / "@honua" / "mcp-server"
            (package / "dist").mkdir(parents=True)
            (package / "dist" / "proxy.js").touch()
            (package / "package.json").write_text(json.dumps({"bin": {"honua-mcp-proxy": "dist/proxy.js"}}))
            lock = {"packages": {"node_modules/@honua/mcp-server": {"version": pin["version"], "integrity": pin["integrity"]}}}
            (work / "package-lock.json").write_text(json.dumps(lock))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.object(mod.shutil, "which", return_value="/usr/bin/npm"), mock.patch.object(
            mod, "_npm_archive_matches", return_value=(True, "archive.tgz")
        ), mock.patch.object(mod, "_run", side_effect=run):
            ok, detail = mod.install_npm(pin, work, executables={"honua-mcp": "mcp-stdio", "honua-mcp-proxy": "mcp-proxy"})
        self.assertFalse(ok)
        self.assertIn("differ from the matrix execution contract", detail)


class NugetInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pin = copy.deepcopy(inputs()[0]["clientArtifacts"]["honua-sdk-dotnet"])
        self.package = b"published nupkg bytes"
        self.pin["digest"] = "sha256:" + hashlib.sha256(self.package).hexdigest()

    def _install(self, restored_bytes, source=mod.NUGET_ORG, env=None):
        work = Path(self.tmp.name) / "install"
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[1] == "restore":
                version = self.pin["version"]
                packages = Path(kwargs["env"]["NUGET_PACKAGES"]) / "honua.sdk" / version
                packages.mkdir(parents=True)
                (packages / f"honua.sdk.{version}.nupkg").write_bytes(restored_bytes)
                (packages / ".nupkg.metadata").write_text(json.dumps({"source": source}))
                (kwargs["cwd"] / "obj").mkdir()
                (kwargs["cwd"] / "obj" / "project.assets.json").write_text(
                    json.dumps({"libraries": {f"Honua.Sdk/{version}": {}}})
                )
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with mock.patch.dict(os.environ, env or {}, clear=True), mock.patch.object(
            mod.shutil, "which", return_value="/usr/bin/dotnet"
        ), mock.patch.object(mod, "_run", side_effect=run):
            ok, detail = mod.install_nuget(self.pin, work)
        return ok, detail, calls, work

    def test_exact_nuget_org_bytes_pass(self):
        ok, detail, calls, work = self._install(self.package, env={"HONUA_SERVER_URL": "http://127.0.0.1:9"})
        self.assertTrue(ok, detail)
        self.assertIn("live GeoServices error probe passed", detail)
        self.assertEqual([c[1] for c in calls], ["restore", "build", str(work / "out" / "Consumer.dll")])
        csproj = (work / "consumer" / "Consumer.csproj").read_text()
        self.assertIn(f'<PackageReference Include="Honua.Sdk" Version="[{self.pin['version']}]" />', csproj)
        self.assertIn("<TargetFramework>net10.0</TargetFramework>", csproj)
        self.assertIn("<clear />", (work / "NuGet.config").read_text())

    def test_restored_bytes_must_match_the_pin(self):
        ok, detail, _, _ = self._install(b"other bytes")
        self.assertFalse(ok)
        self.assertIn("digest mismatch", detail)

    def test_restored_package_must_come_from_nuget_org(self):
        ok, detail, _, _ = self._install(self.package, source="https://nuget.pkg.github.com/honua-io/index.json")
        self.assertFalse(ok)
        self.assertIn("not nuget.org", detail)

    def test_non_public_registry_is_refused(self):
        self.pin["registry"] = "github-packages"
        ok, detail, calls, _ = self._install(self.package)
        self.assertFalse(ok)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
