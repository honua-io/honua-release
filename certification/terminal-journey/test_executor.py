"""Independent proof fixtures and actual HTTP refusal tests (no release claim)."""
import base64
import copy
import io
import json
import math
import sys
import struct
import tempfile
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import executor
import oracles
import stages
from transport import ExecutionError, Transport, safe_url


def test_shoelace_centroid_known_triangle_and_reversed_winding():
    ring = [[0, 0], [6, 0], [0, 3], [0, 0]]
    assert oracles.centroid(ring) == (2, 1)
    assert oracles.centroid(list(reversed(ring))) == (2, 1)
    with pytest.raises(oracles.ProofError):
        oracles.centroid([[0, 0], [1, 0], [2, 0], [0, 0]])


def test_point_wkb_has_independently_authored_wire_bytes():
    assert base64.b64decode(oracles.point_wkb(1, 2)).hex() == (
        "0101000000000000000000f03f0000000000000040")


def buffer_fixture():
    # Authored unit circle polygon. Tests exercise the verifier, not Buffer().
    ring = [[5 + 2 * math.cos(i * math.pi / 16), 7 + 2 * math.sin(i * math.pi / 16)] for i in range(32)]
    return {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring + [ring[0]]]}, "properties": {}}


@pytest.mark.parametrize("change", ["translate", "duplicate", "nan", "open", "count", "type"])
def test_buffer_rejects_content_that_a_success_status_and_hash_cannot_prove(change):
    value = buffer_fixture()
    ring = value["geometry"]["coordinates"][0]
    if change == "translate":
        for p in ring:
            p[0] += 0.1
    elif change == "duplicate":
        ring[4] = ring[3]
    elif change == "nan":
        ring[2][1] = float("nan")
    elif change == "open":
        ring[-1] = [9, 9]
    elif change == "count":
        ring.pop(3)
    else:
        value["geometry"]["type"] = "LineString"
    with pytest.raises(oracles.ProofError):
        oracles.prove_buffer(value, x=5, y=7, distance=2)


def test_buffer_accepts_rotation_and_winding_but_verifies_every_ordinate():
    value = buffer_fixture()
    ring = value["geometry"]["coordinates"][0][:-1]
    ring = list(reversed(ring[5:] + ring[:5]))
    value["geometry"]["coordinates"] = [ring + [ring[0]]]
    assert oracles.prove_buffer(value, x=5, y=7, distance=2)["vertexCount"] == 32


def png_fixture(colour=(239, 32, 32, 255)):
    rows = [bytearray(20 * 4) for _ in range(10)]
    # point (2, 3) in bbox (0, 0, 4, 4) is independently calculated as (10, 2).
    rows[2][40:44] = bytes(colour)
    def chunk(tag, payload):
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", zlib.crc32(tag + payload))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 20, 10, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(b"\0" + row for row in rows))) + chunk(b"IEND", b""))


def test_pixel_proof_reads_the_authored_position_and_exact_colour():
    proof = oracles.prove_pixel(png_fixture(), bbox=[0, 0, 4, 4], point=[2, 3],
                               size=[20, 10], rgba=[239, 32, 32, 255])
    assert (proof["x"], proof["y"]) == (10, 2)
    with pytest.raises(oracles.ProofError):
        oracles.prove_pixel(png_fixture((0, 0, 0, 0)), bbox=[0, 0, 4, 4], point=[2, 3],
                            size=[20, 10], rgba=[239, 32, 32, 255])


def test_feature_proof_compares_ordinates_ids_and_count():
    expected = [{"properties": {"journey_id": 4}, "geometry": {"type": "Point", "coordinates": [2, 3]}}]
    value = {"type": "FeatureCollection", "features": copy.deepcopy(expected)}
    assert oracles.prove_features(value, expected)["featureCount"] == 1
    value["features"][0]["geometry"]["coordinates"][0] = 3
    with pytest.raises(oracles.ProofError):
        oracles.prove_features(value, expected)


@pytest.mark.parametrize("field", ["body", "versionId", "itemId", "family", "contentHash"])
def test_final_map_rejects_wrong_actual_content_even_with_an_expected_server_hash(field):
    body = {"layers": [{"sourceId": "journey"}], "view": {"center": [2, 3], "zoom": 4}}
    value = {"itemId": "item", "versionId": "version", "contentHash": "hash",
             "envelope": {"family": "map", "body": copy.deepcopy(body)}}
    assert oracles.prove_map(value, body, item_id="item", version_id="version", content_hash="hash")
    if field in {"body", "family"}:
        value["envelope"][field] = {} if field == "body" else "dashboard"
    else:
        value[field] = "other"
    with pytest.raises(oracles.ProofError):
        oracles.prove_map(value, body, item_id="item", version_id="version", content_hash="hash")


@pytest.mark.parametrize("path", ["https://evil.example/artifact", "//evil.example/image", "/image?sig=secret", "/map#secret"])
def test_artifact_origin_and_private_parameters_are_rejected_before_transport(path):
    with pytest.raises(ExecutionError):
        safe_url("http://127.0.0.1:8123", path)


@pytest.fixture
def live_http():
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(("GET", self.path, self.headers.get("X-API-Key")))
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/private")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"Pending","proposalId":"proposal-1","requestedBy":"operator-1"}')
        def do_POST(self):
            seen.append(("POST", self.path, self.headers.get("X-API-Key")))
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b'{"error":"self-approval is forbidden"}')
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def transport_for(url):
    return Transport(url, None, None, Path("/tmp"), {"proposer": "private-proposer", "approver": "private-approver"})


def test_actual_http_transport_refuses_redirect_without_sending_credential_again(live_http):
    url, seen = live_http
    with pytest.raises(ExecutionError, match="HTTP 302"):
        transport_for(url).http("GET", "/redirect")
    assert seen == [("GET", "/redirect", "private-proposer")]


def test_actual_anonymous_final_read_sends_no_admin_key(live_http):
    url, seen = live_http
    transport_for(url).get_json("/map", principal=None)
    assert seen == [("GET", "/map", None)]


def engine_for(transport, **target):
    observation = stages.Observation(setup_view_present=True, setup_discovery={
        "tools": [{"name": "honua_render_map", "inputSchema": {"type": "object"}}], "metadata": {"view": "setup"}})
    return executor.JourneyExecutor({"workspaceId": "workspace"}, target, observation, transport)


def test_self_approval_cli_usage_error_cannot_substitute_for_candidate_denial(live_http):
    url, seen = live_http
    transport = transport_for(url)
    engine = engine_for(transport)
    engine.resources["proposalId"] = "proposal-1"
    with mock.patch.object(transport, "cli_approve", side_effect=[(2, None), (1, None)]) as cli:
        with pytest.raises(ExecutionError, match="separate-principal approval failed"):
            engine.approve("proposal-1")
    assert cli.call_args_list[0].args == ("proposal-1", "proposer")
    assert ("POST", "/api/v1/admin/proposals/proposal-1/approve", "private-proposer") in seen
    assert "approval" not in engine.evidence


def test_same_principal_approval_is_refused_before_any_request(live_http):
    url, seen = live_http
    transport = transport_for(url)
    transport.credentials["approver"] = transport.credentials["proposer"]
    engine = engine_for(transport)
    engine.resources["proposalId"] = "proposal-1"
    with pytest.raises(ExecutionError, match="distinct proposer"):
        engine.approve("proposal-1")
    assert not seen


def test_missing_canonical_evidence_never_invents_ids_or_reports_pass():
    engine = engine_for(mock.Mock())
    engine.evidence["checks"]["4"] = {"pixel": {"id": "4.pixel", "kind": "artifact", "invocation": "render",
                                               "status": "pass", "detail": "pixel verified"}}
    result = engine.result(4)
    assert result.status == "blocked"
    assert result.policy_decision_id is None
    assert result.actuator_id is None
    assert result.verification_id is None


def test_out_of_view_or_stage_calls_do_not_execute_a_transport():
    transport = mock.Mock()
    engine = engine_for(transport)
    for number, name in [(4, "honua_admin_connections_create"), (5, "honua_render_map")]:
        with pytest.raises(ExecutionError, match="outside"):
            engine.execute(number, {"kind": "tool_call", "tool": name, "arguments": {}})
    transport.tool.assert_not_called()


def test_render_fault_is_a_real_candidate_call_and_only_a_candidate_refusal_marks_it_observed():
    transport = mock.Mock()
    engine = engine_for(transport)
    engine.state["armedError"] = {"id": "fault-1", "stageNumber": 4, "status": "armed"}
    transport.tool.return_value = {"isError": True, "structuredContent": {"code": "invalid_argument"}}
    response = engine.execute(4, {"kind": "tool_call", "tool": "honua_render_map", "arguments": {"width": 20}})
    assert transport.tool.call_args.args[1]["width"] == -1
    assert engine.state["armedError"]["status"] == "observed"
    assert response["injectedError"] == {"id": "fault-1", "recoverable": True}
    assert not engine.evidence["actions"]


def test_missing_replica_cannot_be_a_same_process_reopen_pass():
    transport = mock.Mock()
    transport.base_url = "http://127.0.0.1:8123"
    engine = engine_for(transport, replicaBaseUrl=transport.base_url)
    engine.resources.update(itemId="item", versionId="version", contentHash="hash")
    engine.fixture["mapBody"] = {}
    transport.get_json.return_value = {"itemId": "item", "versionId": "version", "contentHash": "hash",
                                       "envelope": {"family": "map", "body": {}}}
    engine.check_map()
    assert engine.evidence["checks"]["6"]["saved-map"]["status"] == "pass"
    assert engine.evidence["checks"]["6"]["replica-map"]["status"] == "blocked"


def sdk_engine():
    transport = mock.Mock()
    transport.credentials = {"proposer": "private-proposer"}
    engine = engine_for(transport, execution={
        "datasource": {"name": "source", "passwordEnv": "JOURNEY_TEST_PASSWORD"},
        "importRequest": {"serviceUrl": "http://source/FeatureServer", "layerId": 0},
        "publishRequest": {"table": "source"}, "features": [{}, {}],
        "mapBody": {"source": "/ogc/features/collections/{layerId}/items"}})
    engine.observation.setup_discovery["tools"].extend([
        {"name": "honua_ingest_dataset"}, {"name": "honua_publish_service"}])
    return engine


def sdk_action(method, arguments):
    return {"kind": "terminal_command", "command": "honua-journey-sdk " + method + " " + json.dumps(arguments)}


def test_model_sdk_commands_bind_observed_view_fixture_and_credential_reference(monkeypatch):
    engine = sdk_engine()
    monkeypatch.setenv("JOURNEY_TEST_PASSWORD", "private-database-key")
    with mock.patch.object(executor.sdk, "invoke", return_value={"connectionId": "connection-1"}) as invoke:
        result = engine.execute(3, sdk_action("CreateConnectionAsync", [engine.fixture["datasource"]]))
    assert invoke.call_args.args[2] == [{"name": "source", "password": "private-database-key"}]
    assert result["resources"]["connectionId"] == "connection-1"
    assert "private-database-key" not in json.dumps(result)
    assert "private-proposer" not in json.dumps(result)
    engine.transport.tool.assert_not_called()


@pytest.mark.parametrize("command,number", [
    ("sh -c echo secret", 3), ("honua-journey-sdk DeleteConnectionAsync []", 3),
    ("honua-journey-sdk TestConnectionAsync [null]", 3),
    ("honua-journey-sdk CreateConnectionAsync []", 4),
    ("honua-journey-sdk CreateConnectionAsync [NaN]", 3)])
def test_terminal_bridge_refuses_arbitrary_shell_method_stage_or_unbound_input(command, number):
    engine = sdk_engine()
    with mock.patch.object(executor.sdk, "invoke") as invoke:
        with pytest.raises(ExecutionError):
            engine.execute(number, {"kind": "terminal_command", "command": command})
    invoke.assert_not_called()


def test_sdk_import_submission_cannot_be_repeated():
    engine = sdk_engine()
    engine.resources["jobId"] = "import-job-1"
    with mock.patch.object(executor.sdk, "invoke") as invoke:
        with pytest.raises(ExecutionError, match="already been submitted"):
            engine.execute(3, sdk_action("StartGeoservicesImportAsync", [engine.fixture["importRequest"]]))
    invoke.assert_not_called()


@pytest.mark.parametrize("changes", [{"featuresProcessed": 1}, {"failedFeatures": 1},
                                     {"failedFeatures": False}, {"jobId": "different"}, {"status": "Running"}])
def test_import_status_requires_independent_counts_and_the_submitted_job(changes):
    engine = sdk_engine()
    engine.resources["jobId"] = "import-job-1"
    result = {"jobId": "import-job-1", "status": "Completed", "featuresProcessed": 2, "failedFeatures": 0, **changes}
    with pytest.raises(oracles.ProofError):
        engine.prove_import(result)


def test_authored_map_source_binds_import_identity_without_reading_candidate_body():
    engine = sdk_engine()
    with pytest.raises(ExecutionError, match="imported layer identity"):
        engine.expected_map_body()
    engine.resources["layerId"] = 7
    assert engine.expected_map_body() == {"source": "/ogc/features/collections/7/items"}
    assert engine.fixture["mapBody"]["source"].endswith("{layerId}/items")
