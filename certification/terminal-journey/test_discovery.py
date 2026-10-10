"""Credential-free contract tests; loopback cases exercise real HTTP sessions."""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import discovery
import live_driver
import stages
import probes


def tool(name="one", description="Original μ <descriptor>"):
    return {"name": name, "description": description, "inputSchema": {"type": "object", "properties": {}},
            "outputSchema": {"type": "object", "properties": {"value": {"type": "string"}}},
            "annotations": {"readOnlyHint": True}}


def wire(tools=None, *, patch=None, request_id=1, escaped=False):
    tools = tools if tools is not None else [tool()]
    array = json.dumps(tools, ensure_ascii=escaped, separators=(",", ":"))
    meta = {"view": "setup", "revision": "setup.v2", "revisionDigest": "sha256:" + "a" * 64,
            "membershipDigest": discovery.digest("".join(f"setup/{item['name']}\n" for item in tools).encode()),
            "descriptorDigest": discovery.digest(array.encode()), "toolCount": len(tools),
            "descriptorBytes": len(array.encode()), "estimatedTokens": len(array.encode()) // 4,
            "fullCatalogView": "full", "stages": [{"id": "setup", "tools": [item["name"] for item in tools]}]}
    if patch:
        meta.update(patch)
    return (f'{{"jsonrpc":"2.0","id":{request_id},"result":{{"tools":{array},"_meta":'
            + json.dumps(meta, separators=(",", ":")) + "}}").encode()


class WireContractTests(unittest.TestCase):
    def test_measures_exact_unicode_and_escaped_wire_bytes(self):
        literal = discovery.validate_view(wire())
        escaped = discovery.validate_view(wire(escaped=True))
        self.assertEqual(literal["tools"], escaped["tools"])
        self.assertNotEqual(literal["measured"]["descriptorDigest"], escaped["measured"]["descriptorDigest"])
        self.assertNotEqual(literal["measured"]["descriptorBytes"], escaped["measured"]["descriptorBytes"])
        self.assertEqual(literal["measured"]["descriptorBytes"], literal["measured"]["descriptorSizes"][0] + 2)

    def test_48_descriptors_pass_49_fail(self):
        discovery.validate_view(wire([tool(str(index)) for index in range(48)]))
        with self.assertRaisesRegex(discovery.DiscoveryError, "budget"):
            discovery.validate_view(wire([tool(str(index)) for index in range(49)]))

    def test_per_descriptor_ceiling_is_independent_of_aggregate(self):
        with self.assertRaisesRegex(discovery.DiscoveryError, "budget"):
            discovery.validate_view(wire([tool(description="x" * 16384)]))

    def test_aggregate_ceiling_is_independent_of_each_descriptor(self):
        with self.assertRaisesRegex(discovery.DiscoveryError, "budget"):
            discovery.validate_view(wire([tool(str(index), "x" * 15000) for index in range(9)]))

    def test_empty_view_never_passes(self):
        with self.assertRaises(discovery.DiscoveryError):
            discovery.validate_view(wire([]))

    def test_missing_or_duplicate_descriptors_fail(self):
        for items in ([tool(), tool()], [{"name": "missing-schema"}], [{"inputSchema": {"type": "object"}}]):
            with self.subTest(items=items), self.assertRaises(discovery.DiscoveryError):
                discovery.validate_view(wire(items) if all("name" in item for item in items) else b'{"result":{"tools":[{}]}}')

    def test_metadata_must_match_the_independent_wire_measurement(self):
        for patch in ({"view": "default"}, {"revision": ""}, {"revisionDigest": "a" * 64},
                      {"membershipDigest": "sha256:" + "b" * 64}, {"descriptorDigest": "sha256:" + "c" * 64},
                      {"toolCount": True}, {"toolCount": 2}, {"descriptorBytes": 1},
                      {"estimatedTokens": 1}, {"fullCatalogView": "default"}, {"stages": []},
                      {"stages": [{"id": "setup", "tools": []}]},
                      {"stages": [{"id": "setup", "tools": ["one", "one"]}]}):
            with self.subTest(patch=patch), self.assertRaises(discovery.DiscoveryError):
                discovery.validate_view(wire(patch=patch))

    def test_duplicate_keys_nonstandard_numbers_and_truncation_fail(self):
        for raw in (b'{"result":{},"result":{}}', b'{"result":NaN}', wire()[:-2], b'[]'):
            with self.subTest(raw=raw[:30]), self.assertRaises(discovery.DiscoveryError):
                discovery.validate_view(raw)

    def test_bounded_view_cannot_hide_a_second_page(self):
        document = json.loads(wire())
        document["result"]["nextCursor"] = "more"
        with self.assertRaisesRegex(discovery.DiscoveryError, "complete"):
            discovery.validate_view(json.dumps(document).encode())

    def test_member_spans_ignore_tools_text_inside_descriptions(self):
        measured = discovery.validate_view(wire([tool(description='fake \\"tools\\": [123], <μ>')]))
        self.assertEqual(measured["metadata"]["toolCount"], 1)


class TraceTests(unittest.TestCase):
    def setUp(self):
        discovery.reset_trace()

    def test_excerpt_masks_credential_members_and_bounds_length(self):
        body = json.dumps({"data": {"key": "hk_secret", "items": [{"apiKey": "s2", "name": "ok"}],
                                    "Authorization": "Bearer s3"}}).encode()
        text = discovery.excerpt(body)
        self.assertEqual(text, '{"data":{"key":"[redacted]","items":[{"apiKey":"[redacted]","name":"ok"}],'
                               '"Authorization":"[redacted]"}}')
        plain = discovery.excerpt(b"<p>api_key=s1 password: 's2' Authorization: Bearer s3</p>")
        self.assertNotIn("s1", plain); self.assertNotIn("s2", plain); self.assertNotIn("s3", plain)
        self.assertTrue(discovery.excerpt(b"x" * 1000).endswith("...(truncated)"))
        self.assertLessEqual(len(discovery.excerpt(b"x" * 1000)), discovery.EXCERPT_CHARS + 20)

    def test_describe_trace_names_step_and_last_exchange(self):
        self.assertEqual(discovery.describe_trace(), ["step: before the first instrumented step",
                                                      "last HTTP: none recorded"])
        discovery.mark_step("observe: setup discovery")
        discovery.record_http("POST", "http://127.0.0.1:1/mcp?x=1", None, None, "tools/list")
        self.assertEqual(discovery.describe_trace()[1], "last HTTP: POST /mcp (tools/list) -> no response; body: (empty)")
        discovery.record_http("GET", "http://127.0.0.1:1/api/v1/admin/roles/", 403, b'{"error":"forbidden"}')
        self.assertEqual(discovery.describe_trace(), ["step: observe: setup discovery",
            'last HTTP: GET /api/v1/admin/roles/ -> 403; body: {"error":"forbidden"}'])


class TransportPolicyTests(unittest.TestCase):
    def test_non_loopback_http_needs_the_harness_named_cell_host(self):
        cell = "http://cell-alb-1.us-east-1.elb.amazonaws.com/mcp"
        with self.assertRaisesRegex(discovery.DiscoveryError, "loopback or the harness-named cloud cell host"):
            discovery.HttpSession(cell, "key")
        with probes.allow_http_cell("cell-alb-1.us-east-1.elb.amazonaws.com"):
            self.assertEqual(discovery.HttpSession(cell, "key").url, cell)
            with self.assertRaises(discovery.DiscoveryError):
                discovery.HttpSession("http://other.example/mcp", "key")
        with self.assertRaises(discovery.DiscoveryError):
            discovery.HttpSession(cell, "key")


class ProxyHttpsRefusalTests(unittest.TestCase):
    def test_proxy_https_refusal_is_named_and_other_stderr_stays_private(self):
        refusal = "Fatal: remoteUrl requires HTTPS except for exact loopback HTTP development endpoints"
        for stderr, named in ((refusal, True), ("Fatal: secret-bearing diagnostic", False)):
            code = f"import sys; sys.stderr.write({stderr!r}); sys.exit(1)"
            with probes.McpProxySession([sys.executable, "-c", code], "http://127.0.0.1:1/mcp") as session:
                with self.assertRaises(probes.McpError) as caught:
                    session.request("initialize")
            self.assertEqual(probes.CLIENT_HTTPS_REFUSAL_DETAIL in str(caught.exception), named)
            self.assertNotIn("secret-bearing", str(caught.exception))


class PaginationTests(unittest.TestCase):
    @staticmethod
    def page(tools, cursor=None):
        document = {"result": {"tools": tools}}
        if cursor is not None:
            document["result"]["nextCursor"] = cursor
        return document, json.dumps(document).encode()

    def test_drains_full_catalog_with_explicit_selection_on_every_page(self):
        request = mock.Mock(side_effect=[self.page([tool()], "next"), self.page([tool("two")])])
        items, pages = discovery.full_catalog(request)
        self.assertEqual([item["name"] for item in items], ["one", "two"])
        self.assertEqual(pages, 2)
        self.assertEqual(request.call_args_list, [mock.call("tools/list", {"view": "full"}),
                                                mock.call("tools/list", {"view": "full", "cursor": "next"})])

    def test_cursor_loops_and_duplicate_tools_fail(self):
        for second in (self.page([tool("two")], "next"), self.page([tool()])):
            request = mock.Mock(side_effect=[self.page([tool()], "next"), second])
            with self.subTest(second=second), self.assertRaises(discovery.DiscoveryError):
                discovery.full_catalog(request)

    def test_page_and_byte_bounds_fail_closed(self):
        with mock.patch.object(discovery, "MAX_CATALOG_PAGES", 1), self.assertRaisesRegex(discovery.DiscoveryError, "page bound"):
            discovery.full_catalog(mock.Mock(return_value=self.page([tool()], "next")))
        with mock.patch.object(discovery, "MAX_CATALOG_BYTES", 1), self.assertRaisesRegex(discovery.DiscoveryError, "byte bound"):
            discovery.full_catalog(mock.Mock(return_value=self.page([tool()])))


class HttpSessionTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        recorded = self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def respond(self, status, raw=b"", session=None):
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                if session:
                    self.send_header("Mcp-Session-Id", session)
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                if self.headers.get("X-API-Key") != "ephemeral-test-key":
                    self.respond(401)
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                recorded.append((body, self.headers.get("Mcp-Session-Id")))
                if body["method"] == "initialize":
                    if body["params"].get("_meta") != {discovery.VIEW_KEY: "setup"}:
                        self.respond(400)
                        return
                    self.respond(200, json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {
                        "protocolVersion": "2025-06-18", "serverInfo": {"name": "fixture", "version": "1"}, "capabilities": {}}}).encode(), "actual-issued-session")
                    return
                if self.headers.get("Mcp-Session-Id") != "actual-issued-session":
                    self.respond(404)
                    return
                if body["method"] == "notifications/initialized":
                    self.respond(202)
                elif body.get("params", {}).get("view") == "full":
                    self.respond(200, json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [tool(), tool("two")]}}).encode())
                else:
                    self.respond(200, wire(request_id=body["id"]))

            def do_DELETE(self):
                recorded.append(({"method": "DELETE"}, self.headers.get("Mcp-Session-Id")))
                self.respond(204)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/mcp"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_real_initialize_notification_session_override_and_restoration(self):
        session = discovery.HttpSession(self.url, "ephemeral-test-key")
        self.assertEqual(session.initialize()["serverInfo"]["name"], "fixture")
        _, raw = session.request("tools/list")
        discovery.validate_view(raw)
        self.assertEqual(len(discovery.full_catalog(session.request)[0]), 2)
        _, restored = session.request("tools/list")
        self.assertEqual(discovery.validate_view(restored), discovery.validate_view(raw))
        session.close()
        self.assertTrue(all(session_id == "actual-issued-session" for _, session_id in self.requests[1:]))
        self.assertEqual(self.requests[1][0]["method"], "notifications/initialized")

    def test_wrong_credential_is_not_session_authority(self):
        with self.assertRaisesRegex(discovery.DiscoveryError, "401"):
            discovery.HttpSession(self.url, "wrong").initialize()
        # The refusal leaves a redacted summary of the exchange for a raising driver's receipt.
        http = discovery.TRACE["http"]
        self.assertEqual((http["method"], http["path"], http["rpc"], http["status"]), ("POST", "/mcp", "initialize", 401))
        self.assertNotIn("wrong", json.dumps(discovery.TRACE))

    def test_partial_http_receipt_does_not_promote_a_missing_installed_proxy(self):
        receipt = discovery.capture_setup_view(None, self.url, "ephemeral-test-key")
        self.assertEqual(receipt["http"]["status"], "pass")
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(receipt["tools"], [])
        self.assertNotIn("ephemeral-test-key", json.dumps(receipt))
        self.assertNotIn("actual-issued-session", json.dumps(receipt))

    def test_proxy_descriptor_mutation_never_passes(self):
        for field in ("description", "inputSchema", "outputSchema", "annotations"):
            result = json.loads(wire())["result"]
            del result["tools"][0][field]
            fake = mock.MagicMock()
            fake.__enter__.return_value = fake
            fake.initialize.return_value = {"result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fixture", "version": "1"}}}
            fake.request.return_value = {"result": result}
            with self.subTest(field=field), mock.patch.object(Path, "is_file", return_value=True), mock.patch.object(discovery.probes, "McpProxySession", return_value=fake):
                receipt = discovery.capture_setup_view(Path("installed-proxy"), self.url, "ephemeral-test-key")
            self.assertEqual(receipt["status"], "fail")
            self.assertEqual(receipt["tools"], [])

    def test_full_canonical_extras_are_retained_without_expanding_setup(self):
        selected = json.loads(wire())
        full = {"result": {"tools": [tool(), tool("two")]}}
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake
        fake.initialize.return_value = {"result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "fixture", "version": "1"}}}
        fake.request.side_effect = [selected, full, selected]
        with mock.patch.object(Path, "is_file", return_value=True), mock.patch.object(discovery.probes, "McpProxySession", return_value=fake):
            receipt = discovery.capture_setup_view(Path("installed-proxy"), self.url, "ephemeral-test-key")
        self.assertEqual(receipt["status"], "pass")
        self.assertEqual(receipt["catalogToolNames"], ["one", "two"])
        self.assertEqual(receipt["http"]["fullCatalogDescriptors"], [tool(), tool("two")])
        self.assertEqual(receipt["tools"], [tool()])
        observation = stages.Observation(setup_view_present=True, setup_discovery=receipt,
                                         tool_names=tuple(receipt["catalogToolNames"]))
        view = live_driver._tool_view(observation)
        self.assertEqual(view["catalogToolCount"], 2)
        self.assertEqual(view["tools"], [tool()])


class ProxyWireTests(unittest.TestCase):
    def test_strict_original_line_parser_rejects_lossy_json(self):
        for raw in (b'{"jsonrpc":"2.0","id":1,"result":{},"result":{}}',
                    b'{"jsonrpc":"2.0","id":1,"result":{"value":NaN}}',
                    b'{"jsonrpc":"2.0","id":true,"result":{}}',
                    b'{"jsonrpc":"2.0","id":1,"result":{},"error":{}}',
                    b'{"jsonrpc":"1.0","id":1,"result":{}}', b'\xff'):
            with self.subTest(raw=raw), self.assertRaises(probes.McpError):
                probes.parse_mcp_response(raw)

    def test_spawned_proxy_responses_are_bounded_before_parsing(self):
        program = "import sys; sys.stdin.buffer.readline(); sys.stdout.buffer.write(b'x'*2048); sys.stdout.buffer.flush()"
        with mock.patch.object(probes, "MCP_MAX_RESPONSE_BYTES", 1024), \
             probes.McpProxySession([sys.executable, "-c", program], "http://127.0.0.1/mcp") as session:
            with self.assertRaisesRegex(probes.McpError, "byte bound"):
                session.request("tools/list")

    def test_spawned_proxy_cannot_hide_duplicate_keys(self):
        raw = b'{"jsonrpc":"2.0","id":1,"result":{},"result":{}}\n'
        program = f"import sys; sys.stdin.buffer.readline(); sys.stdout.buffer.write({raw!r}); sys.stdout.buffer.flush()"
        with probes.McpProxySession([sys.executable, "-c", program], "http://127.0.0.1/mcp") as session:
            with self.assertRaisesRegex(probes.McpError, "duplicate JSON"):
                session.request("tools/list")

    def test_spawned_proxy_environment_carries_only_the_session_credential(self):
        # The child answers tools/list with the names of its own environment variables.
        program = ("import json, os, sys; request = json.loads(sys.stdin.buffer.readline()); "
                   "sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': request['id'], "
                   "'result': {'env': dict(os.environ)}}) + '\\n'); sys.stdout.flush()")
        inherited = {"SDKREG_API_KEY": "root", "SDKREG_BEARER": "bearer", "SDKREG_PROPOSER_KEY": "proposer",
                     "E2E_API_KEY": "admin", "HONUA_API_KEY": "operator", "HONUA_ADMIN_KEY": "admin",
                     "HONUA_MCP_AUTH_TOKEN": "token", "HONUA_SERVER_URL": "http://localhost:8080", "GH_TOKEN": "gh"}
        cases = {"anonymous": ({}, {}), "authenticated": ({"HONUA_API_KEY": "proposer-key", "HONUA_ADMIN_KEY": ""},
                                                          {"HONUA_API_KEY": "proposer-key"})}
        for name, (env, credentials) in cases.items():
            with self.subTest(session=name), mock.patch.dict(probes.os.environ, inherited), \
                 probes.McpProxySession([sys.executable, "-c", program], "http://127.0.0.1/mcp", env=env) as session:
                seen = session.request("tools/list")["result"]["env"]
            self.assertEqual({key: value for key, value in seen.items() if key.startswith(("SDKREG_", "E2E_", "HONUA_"))},
                             {**credentials, "HONUA_MCP_REMOTE_URL": "http://127.0.0.1/mcp"})
            self.assertNotIn("GH_TOKEN", seen)
            self.assertNotIn("operator", seen.values())

    def test_buffered_notification_and_response_are_both_consumed(self):
        raw = b'{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}\n{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}\n'
        program = f"import sys; sys.stdin.buffer.readline(); sys.stdout.buffer.write({raw!r}); sys.stdout.buffer.flush()"
        with probes.McpProxySession([sys.executable, "-c", program], "http://127.0.0.1/mcp") as session:
            self.assertEqual(session.request("tools/list")["result"], {"tools": []})


class DriverBoundaryTests(unittest.TestCase):
    def test_only_a_verified_complete_view_is_exposed(self):
        observation = stages.Observation(setup_view_present=True,
            setup_discovery={"status": "pass", "tools": [tool()], "metadata": {"view": "setup"}})
        self.assertEqual(live_driver._tool_view(observation)["tools"], [tool()])
        observation.setup_view_present = False
        self.assertEqual(live_driver._tool_view(observation)["tools"], [])

    def test_verified_discovery_does_not_unblock_execution(self):
        observation = stages.Observation(setup_view_present=True,
            setup_discovery={"status": "pass", "tools": [tool()], "metadata": {"view": "setup"}})
        workspace = mock.Mock()
        workspace.status = "pass"
        workspace.missing_for_stage.return_value = []
        with mock.patch.object(live_driver, "_rehydrate", return_value=({}, {}, {}, observation, workspace)), \
             mock.patch.object(live_driver, "_stage_status", return_value={"status": "pass", "blockedBy": []}):
            response = live_driver.op_execute({"action": {"kind": "tool", "name": "one"}})
        self.assertEqual(response["status"], "blocked")
        self.assertFalse(response["result"]["accepted"])
        self.assertTrue(all(value is None for value in response["canonicalIds"].values()))


if __name__ == "__main__":
    unittest.main()
