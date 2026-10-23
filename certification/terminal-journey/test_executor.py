"""Independent proof fixtures and actual HTTP refusal tests (no release claim)."""
import base64
import copy
import hashlib
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

import jsonschema
import pytest

sys.path.insert(0, str(Path(__file__).parent))
import executor
import oracles
import stages
import transport as transport_module
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
                               size=[20, 10], rgba=[239, 32, 32, 255], media_type="image/png")
    assert (proof["x"], proof["y"]) == (10, 2)
    with pytest.raises(oracles.ProofError):
        oracles.prove_pixel(png_fixture((0, 0, 0, 0)), bbox=[0, 0, 4, 4], point=[2, 3],
                            size=[20, 10], rgba=[239, 32, 32, 255], media_type="image/png")


def test_pixel_proof_requires_image_png_and_the_shared_decoder():
    kwargs = dict(bbox=[0, 0, 4, 4], point=[2, 3], size=[20, 10], rgba=[239, 32, 32, 255])
    with pytest.raises(oracles.ProofError, match="media type"):
        oracles.prove_pixel(png_fixture(), media_type="application/octet-stream", **kwargs)
    rows = [bytearray(20 * 4) for _ in range(10)]
    rows[2][40:44] = bytes((239, 32, 32, 255))
    payload = zlib.compress(b"".join(b"\0" + bytes(row) for row in rows)) + b"\x00\x01"

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))

    # Unused bytes inside the final IDAT follow PNG Third Edition handling.
    padded = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 20, 10, 8, 6, 0, 0, 0))
              + chunk(b"IDAT", payload) + chunk(b"IEND", b""))
    assert oracles.prove_pixel(padded, media_type="image/png", **kwargs)["rgba"] == [239, 32, 32, 255]
    broken = padded[:33] + chunk(b"ABCD", b"x") + padded[33:]
    with pytest.raises(oracles.ProofError, match="critical-chunk"):
        oracles.prove_pixel(broken, media_type="image/png", **kwargs)


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
    assert result.operation_instance_id is None
    assert result.audit_id is None
    assert result.policy_decision_id is None
    assert result.actuator_id is None
    assert result.verification_id is None
    evidence = next(check for check in result.checks if check.id == "4.canonical-evidence")
    assert evidence.status == "blocked"
    assert "operationInstanceId" in evidence.detail and "auditId" in evidence.detail


STUDIO_ENVELOPE = {"operation": {"operationInstanceId": "opinst-1", "operationId": "studio.draft.save-version",
                                 "status": "Completed", "correlationId": "00-corr-01", "auditId": "audit-dev-1",
                                 "policyDecision": "Allow"},
                   "version": {"itemId": "item", "versionId": "version", "contentHash": "hash"}}


def passing_stage_6_checks(engine):
    for check in ("saved-map", "replica-map", "reopened-map"):
        engine.evidence["checks"].setdefault("6", {})[check] = {
            "id": f"6.{check}", "kind": "artifact", "invocation": check, "status": "pass", "detail": "verified"}


def test_canonical_evidence_is_keyed_on_server_emitted_identities_without_legacy_ids():
    engine = engine_for(mock.Mock())
    engine._record(6, "honua_studio_save_version", copy.deepcopy(STUDIO_ENVELOPE))
    passing_stage_6_checks(engine)
    result = engine.result(6)
    assert result.status == "pass"
    assert (result.operation_id, result.operation_instance_id, result.correlation_id, result.audit_id) == (
        "studio.draft.save-version", "opinst-1", "00-corr-01", "audit-dev-1")
    # The candidate emits no policy-decision/actuator/verification ids; none is invented.
    assert (result.policy_decision_id, result.actuator_id, result.verification_id, result.approval_id) == (
        None, None, None, None)
    assert next(c for c in result.checks if c.id == "6.canonical-evidence").status == "pass"


@pytest.mark.parametrize("missing", ["operationInstanceId", "correlationId", "auditId"])
def test_each_server_emitted_identity_is_required_for_a_passing_stage(missing):
    engine = engine_for(mock.Mock())
    envelope = copy.deepcopy(STUDIO_ENVELOPE)
    envelope["operation"].pop(missing)
    engine._record(6, "honua_studio_save_version", envelope)
    passing_stage_6_checks(engine)
    result = engine.result(6)
    assert result.status == "blocked"
    assert missing in next(c for c in result.checks if c.id == "6.canonical-evidence").detail


def test_publication_stage_also_requires_the_server_proposal_identity():
    engine = engine_for(mock.Mock())
    envelope = copy.deepcopy(STUDIO_ENVELOPE)
    engine._record(7, "honua_studio_propose_publication", envelope)
    engine.evidence["checks"]["7"] = {"durable-proposal": {"id": "7.durable-proposal", "kind": "artifact",
                                                           "invocation": "GET", "status": "pass", "detail": "ok"}}
    assert engine.result(7).status == "blocked"
    envelope["operation"]["proposalId"] = "proposal-1"
    engine._record(7, "honua_studio_propose_publication", envelope)
    result = engine.result(7)
    assert result.status == "pass"
    assert result.proposal_id == "proposal-1"


def test_studio_schema_version_is_read_from_the_candidate_family_descriptor():
    transport = mock.Mock(credentials={"proposer": "private-proposer"})
    engine = engine_for(transport)
    transport.get_json.return_value = {"success": True, "data": {"families": [
        {"family": "query", "currentSchemaVersion": "9.9"}, {"family": "map", "currentSchemaVersion": "1.0"}]}}
    assert engine.family_schema_version("map") == "1.0"
    assert transport.get_json.call_args.args == ("/api/v1/studio/package-families",)
    transport.get_json.return_value = {"data": {"families": [{"family": "query", "currentSchemaVersion": "1.0"}]}}
    with pytest.raises(ExecutionError, match="current map schema version"):
        engine.family_schema_version("map")


def test_stage_principals_keep_the_studio_author_non_admin_and_fall_back_without_an_operator():
    transport = mock.Mock(credentials={"proposer": "author", "operator": "admin", "approver": "approver"})
    engine = engine_for(transport)
    assert [engine.principal(n) for n in range(3, 9)] == [
        "operator", "operator", "operator", "proposer", "proposer", "operator"]
    transport.credentials.pop("operator")
    assert {engine.principal(n) for n in range(3, 9)} == {"proposer"}


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


def test_model_can_select_datasource_after_real_canary_redaction(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "tools"))
    from terminal_model_canary import Redactor

    engine = sdk_engine()
    monkeypatch.setenv("JOURNEY_TEST_PASSWORD", "private-database-key")
    redactor = Redactor(["private-database-key", "private-proposer"])
    observation = redactor.value({"fixture": engine.fixture, "terminalSurface": engine.terminal_surface(3)})
    # Reproduce the failed live canary boundary: a masked Env value cannot be
    # faithfully returned. Keep that masking; use the fixed authored reference.
    assert observation["fixture"]["datasource"]["passwordEnv"] == "[REDACTED]"
    selected = observation["terminalSurface"]["sdkFixtureReferences"]["datasource"]
    with mock.patch.object(executor.sdk, "invoke", return_value={"connectionId": "connection-1"}) as invoke:
        with pytest.raises(ExecutionError, match="do not bind"):
            engine.execute(3, sdk_action("CreateConnectionAsync", [observation["fixture"]["datasource"]]))
        invoke.assert_not_called()
        result = engine.execute(3, sdk_action("CreateConnectionAsync", [selected]))
    assert invoke.call_count == 1
    assert invoke.call_args.args[2] == [{"name": "source", "password": "private-database-key"}]
    assert result["resources"]["connectionId"] == "connection-1"
    assert "private-database-key" not in json.dumps([observation, result, selected])
    engine.transport.tool.assert_not_called()


@pytest.mark.parametrize("method,arguments", [
    ("CreateConnectionAsync", [{"fixtureReference": "foreign.datasource"}]),
    ("CreateConnectionAsync", [{"fixtureReference": "journey.datasource", "host": "foreign"}]),
    ("CreateConnectionAsync", [{"fixtureReference": "journey.datasource"}, {}]),
    ("TestConnectionAsync", [{"fixtureReference": "journey.datasource"}]),
])
def test_datasource_reference_cannot_override_fixture_or_other_sdk_arguments(method, arguments):
    engine = sdk_engine()
    with mock.patch.object(executor.sdk, "invoke") as invoke:
        with pytest.raises(ExecutionError, match="do not bind"):
            engine.execute(3, sdk_action(method, arguments))
    invoke.assert_not_called()


def test_observed_sdk_command_examples_survive_redaction_and_execute_exactly(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "tools"))
    from terminal_model_canary import Redactor

    engine = sdk_engine()
    monkeypatch.setenv("JOURNEY_TEST_PASSWORD", "private-database-key")
    redactor = Redactor(["private-database-key", "private-proposer"])
    surface = redactor.value(engine.terminal_surface(3))
    assert "TestConnectionAsync" not in surface["sdkCommandExamples"]
    with mock.patch.object(executor.sdk, "invoke", return_value={"connectionId": "connection-1"}) as invoke:
        engine.execute(3, {"kind": "terminal_command", "command": surface["sdkCommandExamples"]["CreateConnectionAsync"]})
    assert invoke.call_count == 1
    assert invoke.call_args.args[2] == [{"name": "source", "password": "private-database-key"}]
    surface = redactor.value(engine.terminal_surface(3))
    with mock.patch.object(executor.sdk, "invoke", return_value={"connectionId": "connection-1", "isHealthy": True}) as invoke:
        engine.execute(3, {"kind": "terminal_command", "command": surface["sdkCommandExamples"]["TestConnectionAsync"]})
    assert invoke.call_args.args[2] == ["connection-1"]
    assert engine.evidence["checks"]["3"]["datasource"]["status"] == "pass"
    assert "private-database-key" not in json.dumps(surface)
    engine.transport.tool.assert_not_called()


@pytest.mark.parametrize("command,number", [
    ("sh -c echo secret", 3), ("honua-journey-sdk DeleteConnectionAsync []", 3),
    ("honua-journey-sdk TestConnectionAsync [null]", 3),
    ("honua-journey-sdk CreateConnectionAsync []", 4),
    ("honua-journey-sdk CreateConnectionAsync [NaN]", 3),
    ('honua-journey-sdk CreateConnectionAsync [{"name":"source","name":"other"}]', 3)])
def test_terminal_bridge_refuses_arbitrary_shell_method_stage_or_unbound_input(command, number):
    engine = sdk_engine()
    with mock.patch.object(executor.sdk, "invoke") as invoke:
        with pytest.raises(ExecutionError):
            engine.execute(number, {"kind": "terminal_command", "command": command})
    invoke.assert_not_called()


LOCAL_TARGET = json.loads((Path(__file__).parent / "targets" / "local-docker.json").read_text())
UPLOAD_RESULT = {"success": True, "featureCount": 2, "tableName": "journey_source",
                 "physicalTableName": "imported_journey_source", "schema": "public", "format": "GeoJson"}


def upload_engine(**credentials):
    transport = mock.Mock()
    transport.credentials = {"proposer": "private-proposer", **credentials}
    engine = engine_for(transport, execution=copy.deepcopy(LOCAL_TARGET["execution"]))
    engine.observation.setup_discovery["tools"].extend([
        {"name": "honua_ingest_dataset", "inputSchema": {"type": "object"}},
        {"name": "honua_publish_service", "inputSchema": {"type": "object"}}])
    engine.resources["connectionId"] = "connection-1"
    return engine


def test_geoservices_url_import_is_no_longer_a_journey_sdk_method():
    assert executor.SDK_METHODS == {"CreateConnectionAsync", "TestConnectionAsync"}
    assert "importRequest" not in LOCAL_TARGET["execution"]
    engine = sdk_engine()
    with mock.patch.object(executor.sdk, "invoke") as invoke:
        with pytest.raises(ExecutionError):
            engine.execute(3, sdk_action("StartGeoservicesImportAsync", [{"serviceUrl": "http://source:8080"}]))
    invoke.assert_not_called()


def test_committed_upload_fixture_is_digest_pinned_and_equals_the_authored_rows():
    engine = upload_engine()
    spec, path = engine.fixture_dataset()
    assert path.name == "journey-source.geojson" and path.parent.name == "fixtures"
    assert json.loads(path.read_bytes())["features"] == LOCAL_TARGET["execution"]["features"]
    for change in ({"sha256": "0" * 64}, {"path": "../journey.v1.json"}):
        tampered = upload_engine()
        tampered.fixture["upload"].update(change)
        with pytest.raises(ExecutionError):
            tampered.upload_dataset()
        tampered.transport.cli_admin.assert_not_called()


def test_upload_runs_the_pinned_cli_multipart_operation_as_the_operator():
    engine = upload_engine(operator="private-operator")
    engine.transport.cli_admin.return_value = (0, copy.deepcopy(UPLOAD_RESULT))
    engine.upload_dataset()
    (group, operation, principal), kwargs = engine.transport.cli_admin.call_args
    assert (group, operation, principal) == ("import", "uploadImportFile", "operator")
    assert kwargs["content_type"] == "multipart/form-data" and kwargs["yes"] is True
    body = kwargs["body"]
    assert body["file"].startswith("@") and body["file"].endswith("journey-source.geojson")
    assert (body["TableName"], body["TargetSchema"], body["TargetSrid"]) == ("journey_source", "public", "4326")
    assert engine.resources["importTable"] == "imported_journey_source"
    assert engine.resources["importSchema"] == "public"
    assert engine.evidence["checks"]["3"]["import-complete"]["status"] == "pass"
    assert "private-operator" not in json.dumps(engine.evidence)
    with pytest.raises(ExecutionError, match="already been uploaded"):
        engine.upload_dataset()
    assert engine.transport.cli_admin.call_count == 1


def test_background_upload_follows_its_import_job_to_completion(monkeypatch):
    monkeypatch.setattr(executor.time, "sleep", lambda _: None)
    engine = upload_engine()
    engine.transport.cli_admin.side_effect = [
        (0, {"jobId": "import-job-1", "statusUrl": "/api/v1/admin/import/jobs/import-job-1"}),
        (0, {"status": "Running"}),
        (0, {"status": "Completed", "result": copy.deepcopy(UPLOAD_RESULT)})]
    engine.upload_dataset()
    reads = engine.transport.cli_admin.call_args_list[1:]
    assert all(call.args[:2] == ("import", "getImportJobStatus") and call.kwargs["path"] == {"jobId": "import-job-1"}
               for call in reads)
    assert engine.resources["importTable"] == "imported_journey_source"


@pytest.mark.parametrize("changes", [{"featureCount": 1}, {"featureCount": "2"}, {"success": False},
                                     {"tableName": "other"}, {"errorCode": "import.empty_dataset"},
                                     {"physicalTableName": None}])
def test_upload_proof_requires_a_clean_import_of_every_fixture_feature(changes):
    engine = upload_engine()
    with pytest.raises(oracles.ProofError):
        engine.prove_upload({**UPLOAD_RESULT, **changes})


def test_failed_cli_upload_is_not_a_pass():
    engine = upload_engine()
    engine.transport.cli_admin.return_value = (1, None)
    with pytest.raises(ExecutionError, match="multipart upload failed"):
        engine.upload_dataset()
    assert "importTable" not in engine.resources


PUBLISH_OUTPUT = {"status": "Completed", "requiresApproval": False, "operationId": "service.publish",
                  "operationInstanceId": "opinst-publish-1", "correlationId": "00-corr-publish",
                  "auditId": "audit-publish-1", "layerId": "7", "serviceName": "journey",
                  "serviceUri": "honua://published-services/journey"}


def publish_arguments(engine, **changes):
    return {**engine.fixture["publishRequest"], "connectionId": "connection-1", "schema": "public",
            "table": "imported_journey_source", **changes}


def test_publication_must_bind_the_uploaded_table_and_connection_before_any_call():
    engine = upload_engine()
    engine.resources.update(importTable="imported_journey_source", importSchema="public")
    for changes in ({"table": "other_table"}, {"connectionId": "connection-2"}, {"schema": "private"}):
        with pytest.raises(ExecutionError, match="does not bind the uploaded dataset"):
            engine.execute(3, {"kind": "tool_call", "tool": "honua_publish_service",
                               "arguments": publish_arguments(engine, **changes)})
    engine.transport.tool.assert_not_called()


@pytest.mark.parametrize("present,missing", [({}, "connectionId, table, schema"),
    ({"connectionId": "connection-1"}, "table, schema"),
    ({"importTable": "uploaded", "importSchema": "public"}, "connectionId")])
def test_premature_publication_is_action_feedback_with_exact_missing_prerequisites(present, missing):
    engine = upload_engine()
    engine.resources.clear()
    engine.resources.update(present)
    with pytest.raises(executor.ActionRejected, match="prerequisites are missing: " + missing) as error:
        engine.execute(3, {"kind": "tool_call", "tool": "honua_publish_service", "arguments": {}})
    assert error.value.blocked is False
    engine.transport.tool.assert_not_called()
    assert not engine.evidence["checks"].get("3")


def test_terminal_bridge_describes_grammar_and_argument_sources_without_resolving_credentials():
    engine = sdk_engine()
    surface = engine.terminal_surface(3)
    assert surface["sdkSyntax"] == "honua-journey-sdk METHOD JSON_ARRAY"
    assert set(surface["sdkArgumentSources"]) == executor.SDK_METHODS
    assert surface["sdkArgumentSources"]["TestConnectionAsync"] == ["currentState.connectionId"]
    assert surface["uploadCommand"] == executor.UPLOAD_COMMAND
    assert surface["uploadArguments"].startswith("None.")
    assert surface["publicationBindings"] == {"connectionId": "currentState.connectionId",
        "table": "currentState.importTable", "schema": "currentState.importSchema"}
    assert "private-proposer" not in json.dumps(surface)
    assert engine.terminal_surface(4) == {}
    engine.observation.setup_discovery["tools"] = []
    assert engine.terminal_surface(3) == {}
    engine.transport.tool.assert_not_called()


@pytest.mark.parametrize("layout", ["columns", "import-jsonb-properties"])
def test_completed_service_publish_supplies_stage_3_operation_ids_and_feature_proof(layout):
    engine = upload_engine(operator="private-operator")
    engine.resources.update(importTable="imported_journey_source", importSchema="public")
    engine.evidence["checks"]["3"] = {
        name: {"id": f"3.{name}", "kind": "artifact", "invocation": name, "status": "pass", "detail": "ok"}
        for name in ("datasource", "import-complete")}
    engine.transport.tool.return_value = {"structuredContent": copy.deepcopy(PUBLISH_OUTPUT)}
    features = copy.deepcopy(LOCAL_TARGET["execution"]["features"])
    if layout == "import-jsonb-properties":
        for feature in features:
            feature["properties"] = {"properties": feature["properties"], "created_at": "2026-10-08T00:00:00Z"}
    engine.transport.get_json.return_value = {"type": "FeatureCollection", "features": features}
    engine.execute(3, {"kind": "tool_call", "tool": "honua_publish_service", "arguments": publish_arguments(engine)})
    assert engine.transport.tool.call_args.args[3] == "operator"
    assert engine.resources["layerId"] == 7
    assert engine.transport.get_json.call_args.args[0] == "/ogc/features/collections/7/items"
    assert engine.evidence["proofs"]["imported-content"]["attributeLayout"] == layout
    result = engine.result(3)
    assert result.status == "pass"
    assert (result.operation_id, result.operation_instance_id, result.correlation_id, result.audit_id) == (
        "service.publish", "opinst-publish-1", "00-corr-publish", "audit-publish-1")


def test_publish_awaiting_operator_approval_is_not_a_published_layer():
    engine = upload_engine()
    engine.resources.update(importTable="imported_journey_source", importSchema="public")
    engine.transport.tool.return_value = {"structuredContent": {**PUBLISH_OUTPUT, "status": "RequiresApproval",
                                                                "requiresApproval": True, "layerId": None}}
    with pytest.raises(ExecutionError, match="did not complete"):
        engine.execute(3, {"kind": "tool_call", "tool": "honua_publish_service",
                           "arguments": publish_arguments(engine)})
    assert "layerId" not in engine.resources


@pytest.mark.parametrize("service", ["native-published", "default", 'Hālau district/"North"'])
def test_returned_publication_identity_replaces_fixture_alias_without_changing_expected_content(service):
    engine = upload_engine()
    fixture = copy.deepcopy(engine.fixture)
    engine.resources.update(importTable="imported_journey_source", importSchema="public")
    returned = {**PUBLISH_OUTPUT, "serviceName": service,
        "serviceUri": "honua://published-services/" + executor.urllib.parse.quote(service, safe="")}
    engine.transport.tool.return_value = {"structuredContent": returned}
    engine.transport.get_json.return_value = {"type": "FeatureCollection", "features": copy.deepcopy(fixture["features"])}
    result = engine.execute(3, {"kind": "tool_call", "tool": "honua_publish_service",
        "arguments": publish_arguments(engine, serviceName="model-requested")})
    assert engine.resources["serviceName"] == engine.resources["serviceId"] == service
    assert engine.resources["serviceUri"] == returned["serviceUri"] and engine.resources["layerId"] == 7
    assert result["resources"] == engine.resources
    assert result["nativePublication"]["fixtureServiceNameMatches"] is False
    assert result["nativePublication"]["serviceNameSha256"] == hashlib.sha256(service.encode()).hexdigest()
    assert engine.fixture == fixture and engine.evidence["checks"]["3"]["imported-content"]["status"] == "pass"


@pytest.mark.parametrize("changes", [{"serviceName": None}, {"serviceUri": None},
    {"serviceUri": "honua://published-services/other"}, {"serviceUri": "https://foreign.test/journey"},
    {"serviceName": "not/a/service"}, {"serviceName": ""}, {"serviceName": " padded "},
    {"serviceName": "line\nbreak"}, {"serviceName": "a" * 201},
    {"requiresApproval": None}, {"requiresApproval": 0},
    {"layerId": None}, {"layerId": True}, {"layerId": "not-a-layer"}])
def test_completed_publish_cannot_supply_partial_or_contradictory_identity_from_requested_names(changes):
    engine = upload_engine()
    engine.resources.update(importTable="imported_journey_source", importSchema="public")
    engine.transport.tool.return_value = {"structuredContent": {**PUBLISH_OUTPUT, **changes}}
    with pytest.raises(ExecutionError):
        engine.execute(3, {"kind": "tool_call", "tool": "honua_publish_service", "arguments": publish_arguments(engine)})
    assert not {"serviceName", "serviceId", "serviceUri", "layerId"} & set(engine.resources)
    assert "nativePublication" not in engine.evidence
    engine.transport.get_json.assert_not_called()


def test_publication_identity_cannot_echo_the_operator_credential_into_context_or_public_hash():
    engine = upload_engine(operator="private-operator")
    engine.resources.update(importTable="imported_journey_source", importSchema="public")
    engine.transport.tool.return_value = {"structuredContent": {**PUBLISH_OUTPUT, "serviceName": "private-operator",
        "serviceUri": "honua://published-services/private-operator"}}
    with pytest.raises(ExecutionError, match="credential material"):
        engine.execute(3, {"kind": "tool_call", "tool": "honua_publish_service", "arguments": publish_arguments(engine)})
    assert "nativePublication" not in engine.evidence and "private-operator" not in json.dumps(engine.resources)


@pytest.mark.parametrize("layer", ["012345678901234567", 12345678901234567, "12345678901234567", "０１２３４５６７８９０１２３４５６７"])
def test_decimal_layer_identity_cannot_normalize_a_credential_into_model_context_or_receipt(layer):
    secret = "012345678901234567"
    engine = upload_engine(operator=secret)
    engine.resources.update(importTable="imported_journey_source", importSchema="public")
    engine.transport.tool.return_value = {"structuredContent": {**PUBLISH_OUTPUT, "layerId": layer}}
    with pytest.raises(ExecutionError):
        engine.execute(3, {"kind": "tool_call", "tool": "honua_publish_service", "arguments": publish_arguments(engine)})
    assert "nativePublication" not in engine.evidence and "layerId" not in engine.resources
    assert secret not in json.dumps(engine.evidence)
    engine.transport.get_json.assert_not_called()


OPERATOR_KEY = "hk_operator_0123456789abcdef"
# The shape honua-server McpToolHelpers.ErrorResult returns: the typed problem mirrored in
# structuredContent, plus members a receipt must never carry (remediation text, ids, raw content).
TOOL_ERROR = {"isError": True, "content": [{"type": "text", "text": "{...}"}], "structuredContent": {
    "status": "error", "code": "unavailable", "missingDependency": "redis", "capability": "operations.proposals",
    "retryable": False, "remediation": "compose Redis", "proposalId": "proposal-private",
    "message": f"The operation store requires Redis (caller {OPERATOR_KEY}; apiKey=hk_live_leaked) ",
    "error": {"kind": "PreconditionFailed", "message": "ignored when the top-level message exists"}}}


def test_mcp_tool_error_names_the_typed_problem_in_the_check_detail_and_masks_it():
    """Run 38052304982 recorded only "candidate returned an MCP tool error" on the Redis-off cell."""
    engine = upload_engine(operator=OPERATOR_KEY)
    engine.resources.update(importTable="imported_journey_source", importSchema="public")
    engine.transport.tool.return_value = copy.deepcopy(TOOL_ERROR)
    with pytest.raises(ExecutionError) as raised:
        engine.execute(3, {"kind": "tool_call", "tool": "honua_publish_service", "arguments": publish_arguments(engine)})
    reason = raised.value.reason
    assert reason.startswith("candidate returned an MCP tool error (")
    for part in ("code=unavailable", "missingDependency=redis", "capability=operations.proposals",
                 "kind=PreconditionFailed", "retryable=false", "message=\"The operation store requires Redis"):
        assert part in reason
    assert OPERATOR_KEY not in reason and "hk_live_leaked" not in reason and "[redacted]" in reason
    assert "proposal-private" not in reason and "compose Redis" not in reason and " " not in reason
    # The stage driver turns the error into the failing execution check verbatim.
    engine.transport.tool.return_value = {"isError": True, "content": [{"type": "text", "text": "no envelope"}]}
    with pytest.raises(ExecutionError, match=r"MCP tool error \(message=\"no envelope\"\)"):
        engine.execute(3, {"kind": "tool_call", "tool": "honua_publish_service", "arguments": publish_arguments(engine)})
    engine.transport.tool.return_value = {"isError": True}
    with pytest.raises(ExecutionError, match="MCP tool error without a typed problem"):
        engine.execute(3, {"kind": "tool_call", "tool": "honua_publish_service", "arguments": publish_arguments(engine)})


def test_tool_error_fields_are_bounded_scalars_and_drop_credential_shaped_values():
    fields = transport_module.tool_error({"structuredContent": {
        "type": "https://honua.io/problems/capability-unavailable", "title": "x" * 500, "code": {"nested": 1},
        "reasonCode": "dependency-unavailable", "capability": "session-token-store", "message": "y" * 5000}})
    assert fields["type"] == "https://honua.io/problems/capability-unavailable"
    assert fields["reasonCode"] == "dependency-unavailable" and len(fields["title"]) == 120
    assert "code" not in fields and "capability" not in fields
    assert len(fields["message"]) <= transport_module.TOOL_ERROR_MESSAGE_CHARS + len("...(truncated)")


def test_a_json_rpc_tool_refusal_names_its_typed_problem(monkeypatch, tmp_path):
    import probes

    view = {"metadata": {"view": "setup"}, "tools": [{"name": "honua_publish_service"}]}

    class Session:
        def __init__(self, *args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def initialize(self, **kwargs):
            return {}

        def request(self, method, params):
            if method == "tools/list":
                return {"result": {"_meta": view["metadata"], "tools": view["tools"]}}
            return {"error": {"code": -32603, "message": f"refused for {OPERATOR_KEY}",
                              "data": {"code": "unavailable", "missingDependency": "redis", "token": "secret"}}}

    monkeypatch.setattr(probes, "McpProxySession", Session)
    proxy = tmp_path / "honua-mcp-proxy"
    proxy.write_text("")
    transport = Transport("http://127.0.0.1:8123", proxy, None, tmp_path, {"proposer": OPERATOR_KEY})
    with pytest.raises(ExecutionError) as raised:
        transport.tool("honua_publish_service", {}, view)
    assert raised.value.reason == ('candidate tool call refused (code=unavailable; missingDependency=redis; '
                                   'message="refused for [redacted]")')


def test_a_publish_that_did_not_complete_names_its_terminal_state():
    engine = upload_engine(operator=OPERATOR_KEY)
    engine.resources.update(importTable="imported_journey_source", importSchema="public")
    engine.transport.tool.return_value = {"structuredContent": {
        **PUBLISH_OUTPUT, "status": "Failed", "layerId": None, "policyOutcome": "Allow",
        "message": f"Operation validation failed (NpgsqlException) for {OPERATOR_KEY}"}}
    with pytest.raises(ExecutionError) as raised:
        engine.execute(3, {"kind": "tool_call", "tool": "honua_publish_service", "arguments": publish_arguments(engine)})
    assert raised.value.reason == (
        'service.publish did not complete for the operator (status=Failed; requiresApproval=false; '
        'policyOutcome=Allow; message="Operation validation failed (NpgsqlException) for [redacted]")')


def test_model_can_select_the_fixed_upload_step_but_not_its_arguments():
    engine = upload_engine()
    engine.transport.cli_admin.return_value = (0, copy.deepcopy(UPLOAD_RESULT))
    response = engine.execute(3, {"kind": "terminal_command", "command": executor.UPLOAD_COMMAND})
    assert response["resources"]["importTable"] == "imported_journey_source"
    for command in (executor.UPLOAD_COMMAND + " --body @/etc/passwd", "honua admin import uploadImportFileFromUrl"):
        with pytest.raises(ExecutionError):
            upload_engine().execute(3, {"kind": "terminal_command", "command": command})
    with pytest.raises(ExecutionError):
        upload_engine().execute(4, {"kind": "terminal_command", "command": executor.UPLOAD_COMMAND})


JOB_OUTPUT = {"jobId": "job-buffer-1", "status": "accepted", "createdAt": "2026-10-08T00:00:00+00:00",
              "resourceUri": "honua://jobs/job-buffer-1"}


def test_stage_5_is_keyed_on_job_runtime_identities_not_operation_ids():
    assert executor.STAGE_EVIDENCE_KEYS[5] == ("jobId", "resourceUri", "jobStatus", "jobCreatedAt")
    assert all("operationInstanceId" in executor.STAGE_EVIDENCE_KEYS[n] for n in (3, 4, 6, 7, 8))
    engine = engine_for(mock.Mock())
    engine.record_job(dict(JOB_OUTPUT))
    engine.evidence["checks"]["5"] = {"buffer": {"id": "5.buffer", "kind": "artifact", "invocation": "job",
                                                 "status": "pass", "detail": "ok"}}
    result = engine.result(5)
    assert result.status == "pass"
    assert (result.job_id, result.resource_uri, result.job_status, result.job_created_at) == (
        "job-buffer-1", "honua://jobs/job-buffer-1", "accepted", "2026-10-08T00:00:00+00:00")
    assert result.operation_instance_id is None and result.audit_id is None
    assert next(c for c in result.checks if c.id == "5.canonical-evidence").invocation == "canonical job receipt"


@pytest.mark.parametrize("missing", ["jobId", "resourceUri", "status", "createdAt"])
def test_stage_5_requires_every_job_identity_the_runtime_returns(missing):
    engine = engine_for(mock.Mock())
    output = dict(JOB_OUTPUT)
    output.pop(missing)
    with pytest.raises(ExecutionError):
        engine.record_job(output)
    engine.evidence["checks"]["5"] = {"buffer": {"id": "5.buffer", "kind": "artifact", "invocation": "job",
                                                 "status": "pass", "detail": "ok"}}
    assert engine.result(5).status == "blocked"


def test_authored_map_source_binds_import_identity_without_reading_candidate_body():
    engine = sdk_engine()
    with pytest.raises(ExecutionError, match="imported layer identity"):
        engine.expected_map_body()
    engine.resources["layerId"] = 7
    assert engine.expected_map_body() == {"source": "/ogc/features/collections/7/items"}
    assert engine.fixture["mapBody"]["source"].endswith("{layerId}/items")


def test_cloud_import_cannot_send_remote_credentials_to_the_local_target_default():
    transport = mock.Mock()
    transport.base_url = "https://candidate.example"
    engine = engine_for(transport, replicaBaseUrl="http://127.0.0.1:8138", execution={"mapBody": {}})
    engine.resources.update(itemId="item", versionId="version", contentHash="hash")
    transport.get_json.return_value = {"itemId": "item", "versionId": "version", "contentHash": "hash",
                                      "envelope": {"family": "map", "body": {}}}
    with mock.patch.object(executor, "Transport") as replica:
        engine.check_map()
    replica.assert_not_called()
    check = engine.evidence["checks"]["6"]["replica-map"]
    assert check["status"] == "blocked"
    assert "remote replica endpoint" in check["detail"]


@pytest.mark.parametrize("method,key", [("TestConnectionAsync", "connectionId")])
def test_sdk_poll_cannot_replace_the_expected_identity_with_its_own_result(method, key):
    engine = sdk_engine()
    engine.resources[key] = "submitted-identity"
    with mock.patch.object(executor.sdk, "invoke", return_value={key: "different-identity"}):
        with pytest.raises(ExecutionError, match="submitted resource identity"):
            engine.sdk_call(method, ["submitted-identity"])
    assert engine.resources[key] == "submitted-identity"
    assert not engine.evidence["actions"]


SEALED = {"itemId": "item-1", "versionId": "version-1", "contentHash": "hash-1"}
SEALED_DIFF = ["itemId=item-1", "versionId=version-1", "contentHash=hash-1", "route=/journey-map"]
SEALED_HANDLE = {"status": "Completed", "resourceIds": {"itemId": "item-1", "versionId": "version-1"}}


def approval_engine(transport):
    engine = engine_for(transport)
    engine.resources.update(proposalId="proposal-1", **SEALED)
    return engine


def test_approval_poll_accepts_recorded_pending_executing_succeeded_sequence():
    transport = mock.Mock(credentials={"proposer": "private-proposer", "approver": "private-approver"})
    engine = approval_engine(transport)
    pending = {"proposalId": "proposal-1", "status": "Pending", "requestedBy": "actor-proposer", "diff": SEALED_DIFF}
    handle = {"operationInstanceId": "opinst-exec", "operationId": "studio.content.create-publication-request",
              "proposalId": "proposal-1", "auditId": "audit-approval", "correlationId": "00-corr-approval",
              **SEALED_HANDLE}
    transport.get_json.side_effect = [pending, pending, {**pending, "status": "Executing"},
        {**pending, "status": "Succeeded", "resolvedBy": "actor-approver", "executionOperationId": "opinst-exec"},
        {"data": handle}]
    transport.cli_approve.side_effect = [(1, None), (0, None)]
    with mock.patch.object(executor.time, "sleep") as sleep:
        result = engine.approve("proposal-1")
    # Keyed on what the candidate reports: proposal, resolver and the audited replay.
    assert result["proposalId"] == "proposal-1"
    assert result["resolvedBy"] == "actor-approver"
    assert result["executionOperationId"] == "opinst-exec"
    assert result["auditId"] == "audit-approval"
    assert result["approvalId"] is None
    assert transport.get_json.call_args.args == ("/api/v1/operations/handles/opinst-exec",)
    assert engine.evidence["canonicalIds"]["8"]["auditId"] == "audit-approval"
    assert sleep.call_count == 1
    assert engine.evidence["checks"]["8"]["separation"]["status"] == "pass"


def test_approval_retains_a_server_reported_approval_id_and_rejects_an_unjoined_replay():
    transport = mock.Mock(credentials={"proposer": "private-proposer", "approver": "private-approver"})
    engine = approval_engine(transport)
    pending = {"proposalId": "proposal-1", "status": "Pending", "requestedBy": "actor-proposer", "diff": SEALED_DIFF}
    final = {**pending, "status": "Succeeded", "resolvedBy": "actor-approver",
             "executionOperationId": "opinst-exec", "approvalId": "approval-1"}
    handle = {"operationInstanceId": "opinst-exec", "operationId": "studio.content.create-publication-request",
              "auditId": "audit-approval", "correlationId": "corr", **SEALED_HANDLE}
    transport.get_json.side_effect = [pending, pending, final, handle]
    transport.cli_approve.side_effect = [(1, None), (0, None)]
    assert engine.approve("proposal-1")["approvalId"] == "approval-1"

    for unjoined in ({"proposalId": "proposal-other"}, {"status": "Executing"},
                     {"resourceIds": {"itemId": "item-1", "versionId": "version-other"}}):
        engine = approval_engine(transport)
        transport.get_json.side_effect = [pending, pending, final, {**handle, **unjoined}]
        transport.cli_approve.side_effect = [(1, None), (0, None)]
        with pytest.raises(ExecutionError, match="does not join the proposal"):
            engine.approve("proposal-1")
        assert "approval" not in engine.evidence


# Shapes observed on candidate 87966c3: the approved Studio publication completes the
# publication's own handle; the proposal carries no executionOperationId and the
# handle omits proposalId.
OBSERVED_PROPOSAL = {"proposalId": "proposal-1", "kind": "StudioDraftMutation", "status": "Succeeded",
                     "requestedBy": "actor-proposer", "resolvedBy": "actor-approver", "diff": SEALED_DIFF}
OBSERVED_HANDLE = {"operationInstanceId": "opinst-pub", "operationId": "studio.content.create-publication-request",
                   "handleId": "opinst-pub", "correlationId": "00-corr-pub", "auditId": "15",
                   "authorizationOutcome": "approved", "policyDecision": "Allow", **SEALED_HANDLE}


def test_approval_without_execution_id_joins_the_publication_handle_on_sealed_content():
    transport = mock.Mock(credentials={"proposer": "private-proposer", "approver": "private-approver"})
    engine = approval_engine(transport)
    engine.evidence["publicationOperation"] = {
        "operationId": "studio.content.create-publication-request", "operationInstanceId": "opinst-pub",
        "proposalId": "proposal-1", "correlationId": "00-corr-pub", "auditId": "15"}
    pending = {**OBSERVED_PROPOSAL, "status": "Pending", "resolvedBy": None}
    transport.get_json.side_effect = [pending, pending, OBSERVED_PROPOSAL, {"data": OBSERVED_HANDLE}]
    transport.cli_approve.side_effect = [(1, None), (0, None)]
    result = engine.approve("proposal-1")
    assert (result["executionOperationId"], result["executionSource"], result["auditId"]) == (
        "opinst-pub", "publication-handle", "15")
    assert engine.evidence["canonicalIds"]["8"]["proposalId"] == "proposal-1"
    assert not engine.canonical_gaps(8)

    # A proposal for different content cannot borrow the publication handle.
    engine = approval_engine(transport)
    engine.evidence["publicationOperation"] = {"operationInstanceId": "opinst-pub", "proposalId": "proposal-1"}
    other = {**OBSERVED_PROPOSAL, "diff": ["itemId=item-1", "versionId=version-1", "contentHash=other"]}
    transport.get_json.side_effect = [pending, pending, other, {"data": OBSERVED_HANDLE}]
    transport.cli_approve.side_effect = [(1, None), (0, None)]
    with pytest.raises(ExecutionError, match="does not join the proposal"):
        engine.approve("proposal-1")


@pytest.mark.parametrize("handle_proposal,status", [(None, "pass"), ("proposal-1", "pass"), ("proposal-other", "fail")])
def test_publication_join_accepts_an_omitted_handle_proposal_but_not_a_different_one(handle_proposal, status):
    transport = mock.Mock(credentials={"proposer": "p", "approver": "a", "operator": "o"})
    engine = approval_engine(transport)
    engine.evidence["publicationOperation"] = {
        "operationId": "studio.content.create-publication-request", "operationInstanceId": "opinst-pub",
        "proposalId": "proposal-1", "correlationId": "00-corr-pub", "auditId": "15"}
    handle = {**OBSERVED_HANDLE, **({"proposalId": handle_proposal} if handle_proposal else {})}
    transport.get_json.return_value = {"data": handle}
    transport.http.return_value = (b"{}", 403)
    engine.verify_authority()
    checks = engine.evidence["checks"]["8"]
    assert checks["canonical-join"]["status"] == status
    assert checks["current-authority"]["status"] == "pass"


@pytest.mark.parametrize("failure", [oracles.ProofError("bad render"), ExecutionError("render", "unavailable", blocked=True),
                                    AttributeError("shape"), StopIteration()])
def test_latest_failed_attempt_clears_prior_proof_and_records_failure(failure):
    engine = engine_for(mock.Mock())
    assert engine._check(4, "pixel", "render", lambda: {"rgba": [1, 2, 3, 4]}).status == "pass"
    def fail():
        raise failure
    check = engine._check(4, "pixel", "render", fail)
    assert check.status == ("blocked" if isinstance(failure, ExecutionError) else "fail")
    assert "pixel" not in engine.evidence["proofs"]


def test_failed_render_transport_invalidates_previous_pixel_before_submitting():
    transport = mock.Mock()
    engine = engine_for(transport)
    engine._check(4, "pixel", "render", lambda: {"rgba": [1, 2, 3, 4]})
    transport.tool.side_effect = ExecutionError("honua_render_map", "refused")
    with pytest.raises(ExecutionError):
        engine.execute(4, {"kind": "tool_call", "tool": "honua_render_map", "arguments": {}})
    assert "pixel" not in engine.evidence["proofs"]
    assert "pixel" not in engine.evidence["checks"]["4"]


@pytest.mark.parametrize("outputs", [{}, [], None, {"buffer": None}, {"buffer": {"href": 42}}])
def test_empty_or_malformed_job_outputs_are_stage_fails(outputs):
    transport = mock.Mock()
    engine = engine_for(transport, execution={"buffer": {"point": [0, 0], "distance": 2}})
    engine.resources["jobId"] = "job-1"
    transport.get_json.side_effect = [{"jobID": "job-1", "status": "successful"}, {"outputs": outputs}]
    check = engine.check_job()
    assert check.status == "fail"
    assert check.invocation == "geometry.buffer canonical job lifecycle"


@pytest.mark.parametrize("failure", [AttributeError("shape"), StopIteration()])
def test_execution_response_errors_preserve_all_six_stage_results(monkeypatch, failure):
    engine = sdk_engine()
    monkeypatch.setenv("JOURNEY_TEST_PASSWORD", "private-database-key")
    with mock.patch.object(engine, "sdk_call", side_effect=failure):
        results = engine.run_build()
    assert [result.number for result in results] == list(range(3, 9))
    assert results[0].status == "fail"
    assert results[0].first_failure.id == "3.execution"


def test_sdk_invoke_environment_excludes_unrelated_credentials(tmp_path, monkeypatch):
    dll = tmp_path / "bridge.dll"
    dll.touch()
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "HONUA_JOURNEY_APPROVER_KEY", "NuGetPackageSourceCredentials_journey"):
        monkeypatch.setenv(name, "private-unrelated-key")
    with mock.patch.object(executor.sdk.subprocess, "run", return_value=mock.Mock(stdout='{"status":"pass","result":{}}')) as run:
        executor.sdk.invoke({"dll": str(dll)}, "TestConnectionAsync", ["connection"],
                            base_url="http://127.0.0.1:8137", credential="private-proposer")
    env = run.call_args.kwargs["env"]
    assert set(env) <= {"PATH", "DOTNET_ROOT", "HOME", "TMPDIR", "LANG", "HONUA_JOURNEY_BASE_URL", "HONUA_JOURNEY_SDK_KEY"}
    assert "private-unrelated-key" not in env.values()
    assert env["HONUA_JOURNEY_SDK_KEY"] == "private-proposer"


def test_prerequisite_failures_survive_successful_executor_checks():
    original = stages.StageResult(4, "style", "render", "fail", checks=[
        executor.probes.Check("4.client-pins", "artifact", "consume pinned clients", "blocked", "missing client", [stages.INSTALLED_CLIENTS]),
        executor.probes.Check("4.1-style-tools-present", "mcp-tool", "tools/list", "fail", "missing honua_render_map"),
        executor.probes.blocked("4.3-decoded-png", "artifact", "decode PNG", "unexecuted placeholder", [stages.JOURNEY_DRIVER])])
    executed = stages.StageResult(4, "style", "render", "pass", checks=[
        executor.probes.Check("4.pixel", "artifact", "render", "pass", "decoded")], operation_id="operation")
    result = stages.merge_execution(original, executed)
    assert result.status == "fail"
    assert result.operation_id == "operation"
    assert {c.id for c in result.checks} == {"4.client-pins", "4.1-style-tools-present", "4.pixel"}
    assert result.blocked_by == [stages.INSTALLED_CLIENTS]


def test_local_fixture_mints_expiring_keys_and_fresh_signed_other_tenant_bearers(tmp_path, monkeypatch):
    import local_fixture
    target = json.loads((Path(__file__).parent / "targets" / "local-docker.json").read_text())
    monkeypatch.setenv("HONUA_JOURNEY_REPLICA_PORT", "19138")
    env = local_fixture.compose_env(target, tmp_path)
    assert env["HONUA_JOURNEY_REPLICA_PORT"] == "19138"
    assert local_fixture.replica_url(target) == "http://127.0.0.1:19138"
    transport = mock.Mock()
    grants = iter(local_fixture.GRANTS.items())
    def mint(*args, **kwargs):
        name, expected = next(grants)
        assert kwargs["body"]["permissions"] == expected
        assert "expiresAt" in kwargs["body"]
        transport.get_json.return_value = {"data": {"permissions": expected, "status": "active", "canAuthenticate": True}}
        return json.dumps({"data": {"key": "private-" + name, "apiKey": {"id": name}}}).encode(), 201
    transport.http.side_effect = mint
    with mock.patch.object(local_fixture, "Transport", return_value=transport), \
            mock.patch.object(local_fixture, "_grant_author_role") as author_role:
        keys = local_fixture.credentials(target, tmp_path, "http://127.0.0.1:8137", mint=True)
    author_role.assert_called_once_with(transport)
    assert set(keys) == {"operator", "proposer", "approver", "viewer", "other-tenant"}
    private_path = tmp_path / "private-principals.json"
    assert private_path.stat().st_mode & 0o777 == 0o600
    first_token = keys["other-tenant"]()
    token = first_token.split(" ")[1]
    header, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=="))
    assert claims["tenant_id"] == "journey-other"
    # Scope-governed bearer: the tenant denial must not be a missing-scope denial.
    assert claims["scope"].split() == ["honua.mcp.read", "honua.mcp.create", "honua.mcp.update", "honua.mcp.publish"]
    assert claims["exp"] - claims["iat"] == 300
    import hashlib
    import hmac
    expected = hmac.new(env["HONUA_JOURNEY_SIGNING_KEY"].encode(), (header + "." + payload).encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(signature + "==") == expected
    assert first_token != keys["other-tenant"]()
    assert not any("private-" in value for value in target["principals"].values())
    local_fixture.cleanup(tmp_path)
    assert not private_path.exists()


def test_studio_author_principal_never_holds_an_admin_grant():
    import local_fixture
    # An admin caller publishes immediately and never yields an AwaitingApproval proposal.
    assert not any(grant == "admin" or grant == "*" or grant.startswith("admin:")
                   for grant in local_fixture.GRANTS["proposer"])
    assert local_fixture.GRANTS["approver"] == ["admin:approve"]
    assert local_fixture.GRANTS["proposer"] != local_fixture.GRANTS["approver"]


@pytest.mark.parametrize("existing", [False, True])
def test_author_role_is_created_or_reused_and_its_grants_are_read_back(existing):
    import local_fixture
    transport = mock.Mock()
    transport.get_json.return_value = {"data": [{"roleId": "role-1", "name": "layer-write-key"}] if existing else []}
    calls = []
    def http(method, path, **kwargs):
        calls.append((method, path))
        if method == "POST":
            return json.dumps({"data": {"roleId": "role-1"}}).encode(), 201
        return json.dumps({"data": kwargs["body"]["permissions"]}).encode(), 200
    transport.http.side_effect = http
    local_fixture._grant_author_role(transport)
    expected = [("PUT", "/api/v1/admin/roles/role-1/permissions")]
    assert calls == ([] if existing else [("POST", "/api/v1/admin/roles/")]) + expected
    transport.http.side_effect = lambda method, path, **kwargs: (json.dumps({"data": []}).encode(), 200)
    transport.get_json.return_value = {"data": [{"roleId": "role-1", "name": "layer-write-key"}]}
    with pytest.raises(ExecutionError, match="author grants"):
        local_fixture._grant_author_role(transport)


def test_tenant_denial_cannot_pass_from_a_blanket_studio_rbac_refusal():
    transport = mock.Mock(credentials={"viewer": "unit-viewer", "other-tenant": "unit-other"})
    engine = engine_for(transport)
    engine.resources.update(itemId="item", versionId="version", contentHash="hash")
    transport.http.side_effect = ExecutionError("GET /api/v1/studio/package-families", "HTTP 403")
    result = engine.verify_authority()["tenantIsolation"]
    assert result.status == "fail"
    assert result.invocation == "GET /api/v1/studio/package-families"


def test_buffer_plan_sent_to_validate_dry_run_and_execute_matches_the_accepted_contract():
    """Capture the plans run_build submits. Expected kind and artifact names are contract literals."""
    spec = {"point": [5, 7], "srid": 3857, "distance": 2}
    transport = mock.Mock()
    transport.credentials = {"proposer": "private-proposer"}
    engine = engine_for(transport, execution={"buffer": spec})
    engine.observation.setup_discovery["tools"].extend(
        {"name": name, "inputSchema": {"type": "object"}}
        for name in ("honua_validate_plan", "honua_dry_run_plan", "honua_execute_plan"))
    captured = []

    def tool(name, arguments, view, principal="proposer"):
        captured.append((name, copy.deepcopy(arguments)))
        if name == "honua_execute_plan":
            return {"structuredContent": dict(JOB_OUTPUT)}
        return {"structuredContent": {"accepted": True}}

    transport.tool.side_effect = tool
    feature = buffer_fixture()
    transport.get_json.side_effect = [
        {"jobID": "job-buffer-1", "status": "successful"},
        {"outputs": {"result": {"value": feature}}},
    ]
    results = engine.run_build()
    stage = next(result for result in results if result.number == 5)
    buffer = next(check for check in stage.checks if check.id == "5.buffer")
    assert buffer.status == "pass"
    assert buffer.invocation == "geometry.buffer canonical job lifecycle"
    assert engine.evidence["proofs"]["buffer"] == {
        "centroid": [5, 7], "vertexCount": 32, "ordinatesVerified": True}
    # This capture is not a live certification. Stage 5 is keyed on the job runtime's
    # identities (no operation ids exist on that path), exactly as execute returned them.
    assert stage.status == "pass"
    assert (stage.job_id, stage.resource_uri) == ("job-buffer-1", "honua://jobs/job-buffer-1")
    assert stage.operation_instance_id is None

    expected_wkb = base64.b64encode(struct.pack("<BIdd", 1, 1, 5.0, 7.0)).decode()
    expected_plan = {
        "planId": "workspace-buffer",
        "intentId": "journey-buffer",
        "steps": [{
            "stepId": "buffer",
            "kind": "Geoprocess",
            "processId": "geometry.buffer",
            "inputs": {
                "wkb": expected_wkb,
                "srid": "3857",
                "distance": "2",
                "geodesic": "false",
            },
        }],
        "outputs": ["FeatureLayer"],
    }
    assert [name for name, _ in captured] == [
        "honua_validate_plan", "honua_dry_run_plan", "honua_execute_plan"]
    assert captured[0][1] == {"plan": expected_plan}
    assert captured[1][1] == {"plan": expected_plan}
    assert captured[2][1] == {
        "plan": expected_plan,
        "idempotencyKey": "workspace-buffer",
    }
    assert captured[0][1]["plan"] == captured[1][1]["plan"] == captured[2][1]["plan"]
    for name, arguments in captured:
        step, = arguments["plan"]["steps"]
        assert step["kind"] == "Geoprocess"
        assert arguments["plan"]["outputs"] == ["FeatureLayer"]
        assert step["kind"] != "Process"
        assert arguments["plan"]["outputs"] != ["buffer"]


def test_http_transport_mints_each_fixture_bearer_at_request_time(live_http):
    url, seen = live_http
    transport = transport_for(url)
    provider = mock.Mock(side_effect=["Bearer one", "Bearer two"])
    transport.credentials["other-tenant"] = provider
    transport.http("GET", "/map", principal="other-tenant")
    transport.http("GET", "/map", principal="other-tenant")
    assert provider.call_count == 2
    assert seen == [("GET", "/map", None), ("GET", "/map", None)]


def test_cli_admin_builds_fixed_argv_and_keeps_the_credential_in_the_child_environment(tmp_path):
    honua = tmp_path / "honua"
    honua.write_text("")
    transport = Transport("http://127.0.0.1:8137", None, honua, tmp_path, {"operator": "private-operator"})
    completed = mock.Mock(returncode=0, stdout=json.dumps({"success": True}))
    with mock.patch("transport.subprocess.run", return_value=completed) as run:
        code, document = transport.cli_admin("import", "uploadImportFile", "operator",
                                             body={"file": "@/fixtures/journey-source.geojson", "TableName": "t"},
                                             content_type="multipart/form-data", yes=True)
    assert (code, document) == (0, {"success": True})
    args, kwargs = run.call_args.args[0], run.call_args.kwargs
    assert args[:4] == [str(honua), "admin", "import", "uploadImportFile"]
    assert args[args.index("--content-type") + 1] == "multipart/form-data"
    assert json.loads(args[args.index("--body") + 1])["TableName"] == "t"
    assert args[args.index("--profile") + 1] == "operator" and args[-1] == "--yes"
    assert "shell" not in kwargs and "private-operator" not in " ".join(args)
    assert kwargs["env"]["HONUA_ADMIN_KEY"] == "private-operator"
    config = json.loads((tmp_path / "profiles" / "config.json").read_text())
    assert set(config["profiles"]) == {"operator", "proposer", "approver"}


RENDER_FIXTURE = {"bbox": [0, 0, 4, 4], "pixelPoint": [2, 3], "renderSize": [20, 10],
                  "pixelRgba": [239, 32, 32, 255], "styleId": "journey-solid-red"}


@pytest.mark.parametrize("reported", ["journey-solid-red", None])
def test_stage_4_pixel_failure_stays_a_fail_without_citing_a_closed_issue(reported):
    engine = engine_for(mock.Mock(), execution=dict(RENDER_FIXTURE))
    output = {"image": {"mimeType": "image/png",
                        "base64": base64.b64encode(png_fixture((45, 105, 165, 255))).decode()},
              "layers": [{"serviceId": "svc", "layerId": 1, "styleId": reported}]}
    assert engine.check_render(output).status == "fail"
    row = engine.evidence["checks"]["4"]["pixel"]
    assert row["status"] == "fail" and "blockedBy" not in row
    engine.evidence["checks"]["4"]["style-applied"] = {
        "id": "4.style-applied", "kind": "artifact", "invocation": "apply", "status": "pass", "detail": "ok"}
    result = engine.result(4)
    assert result.status == "fail"
    canonical = next(c for c in result.checks if c.id == "4.canonical-evidence")
    assert canonical.status == "blocked" and canonical.blocked_by == [stages.JOURNEY_DRIVER]


def test_a_passing_render_carries_no_blocker():
    engine = engine_for(mock.Mock(), execution=dict(RENDER_FIXTURE))
    output = {"image": {"mimeType": "image/png", "base64": base64.b64encode(png_fixture()).decode()},
              "layers": [{"styleId": "journey-solid-red"}]}
    assert engine.check_render(output).status == "pass"
    assert "blockedBy" not in engine.evidence["checks"]["4"]["pixel"]
    rejected = engine_for(mock.Mock(), execution=dict(RENDER_FIXTURE))
    output["image"]["mimeType"] = "image/jpeg"
    assert rejected.check_render(output).status == "fail"


def test_wrong_selected_size_stays_failed_until_a_later_render_repairs_it_over_real_http():
    import live_driver
    served, reads = [png_fixture()], []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            reads.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(served[0])

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        transport = transport_for(f"http://127.0.0.1:{server.server_port}")
        # The frozen authored goal is deliberately different from the first
        # PNG. Nothing changes that expected size to accommodate the result.
        fixture = {**RENDER_FIXTURE, "renderSize": [40, 20]}
        engine = engine_for(transport, execution=fixture)
        engine.state.update(stackUp=True, executionEnabled=True)
        engine.state["armedError"] = {"id": "fault", "stageNumber": 4, "status": "observed"}
        engine._check(4, "style-applied", "style", lambda: {"styleId": "journey-solid-red"})
        output = {"image": {"format": "image/png", "uri": "/render.png"},
                  "operation": {**STUDIO_ENVELOPE["operation"], "operationId": "map.render"}}
        stage = json.loads((live_driver.HERE / "journey.v1.json").read_text())["stages"][3]
        status = {"number": 4, "id": stage["id"], "command": stage["command"], "checks": [], "status": "ready"}

        def observe():
            with mock.patch.object(live_driver, "_rehydrate", return_value=(engine.state, {}, {}, engine.observation, None)), \
                 mock.patch.object(live_driver, "_executor", return_value=engine), \
                 mock.patch.object(live_driver, "_stage_status", return_value=status), \
                 mock.patch.object(live_driver, "_write_state"):
                return live_driver.op_observe({"stage": stage["id"]})

        with mock.patch.object(transport, "tool", return_value={"structuredContent": output}):
            result = engine.execute(4, {"kind": "tool_call", "tool": "honua_render_map",
                                        "arguments": {"width": 20, "height": 10}})
            assert result["recoveredError"] is None
            assert engine.result(4).status == "fail"
            assert "pixel" not in engine.evidence["proofs"]
            observed = observe()
            assert observed["stageStatus"]["status"] == "ready"
            assert observed["observation"]["repairableChecks"] == ["4.pixel"]
            assert next(c for c in observed["stageStatus"]["checks"] if c["id"] == "4.pixel")["status"] == "fail"
            assert engine.state["armedError"]["status"] == "observed"
            for blocked in (False, True):
                def refuse():
                    raise ExecutionError("style", "candidate style failure", blocked=blocked)
                engine._check(4, "style-applied", "style", refuse)
                assert observe()["stageStatus"]["status"] == ("blocked" if blocked else "fail")
            engine._check(4, "style-applied", "style", lambda: {"styleId": "journey-solid-red"})

            # A genuine decoded PNG of the authored size and independently
            # computed point colour is required for the repair.
            def chunk(tag, body):
                return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))
            rows = [bytearray(40 * 4) for _ in range(20)]
            rows[5][80:84] = bytes(RENDER_FIXTURE["pixelRgba"])
            served[0] = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 40, 20, 8, 6, 0, 0, 0))
                         + chunk(b"IDAT", zlib.compress(b"".join(b"\0" + row for row in rows))) + chunk(b"IEND", b""))
            result = engine.execute(4, {"kind": "tool_call", "tool": "honua_render_map",
                                        "arguments": {"width": 40, "height": 20}})
            assert result["recoveredError"] == {"id": "fault", "recovered": True}
            assert engine.result(4).status == "pass"
            assert observe()["stageStatus"]["status"] == "pass"
            assert engine.fixture["renderSize"] == [40, 20]
            assert reads == ["/render.png", "/render.png"]
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.mark.parametrize("selected", [{"width": 40, "height": 20}, {}, {"width": 20},
                                      {"width": "20", "height": 10}, {"width": True, "height": 10}])
def test_render_ignoring_selected_size_or_missing_selection_stays_terminal(selected):
    engine = engine_for(mock.Mock(), execution={**RENDER_FIXTURE, "renderSize": [40, 20]})
    # Declared dimensions cannot override the actual decoded 20x10 PNG.
    output = {"image": {"format": "image/png", "width": 40, "height": 20,
                        "base64": base64.b64encode(png_fixture()).decode()}}
    check = engine.check_render(output, selected_arguments=selected)
    assert check.status == "fail"
    assert engine.evidence["renderDimensions"]["decoded"] == [20, 10]
    assert not engine.repairable_render_selection(4, check)


def test_matching_wrong_size_with_incorrect_pixels_cannot_be_repaired_away():
    engine = engine_for(mock.Mock(), execution={**RENDER_FIXTURE, "renderSize": [40, 20]})
    output = {"image": {"format": "image/png",
                        "base64": base64.b64encode(png_fixture((0, 0, 0, 0))).decode()}}
    check = engine.check_render(output, selected_arguments={"width": 20, "height": 10})
    assert check.status == "fail" and "render pixel at (10, 2)" in check.detail
    assert engine.evidence["renderDimensions"]["requested"] == engine.evidence["renderDimensions"]["decoded"] == [20, 10]
    assert engine.evidence["renderDimensions"]["requestedPixelVerified"] is False
    assert not engine.repairable_render_selection(4, check)
    assert "pixel" not in engine.evidence["proofs"]


@pytest.mark.parametrize("failure", ["pixel", "media", "png", "transport"])
def test_render_feedback_never_suppresses_other_pixel_or_transport_failures(failure):
    transport = mock.Mock(credentials={"proposer": "unit"})
    engine = engine_for(transport, execution=dict(RENDER_FIXTURE))
    image = {"format": "image/png", "base64": base64.b64encode(png_fixture((0, 0, 0, 0))).decode()}
    if failure == "media":
        image["format"] = "image/jpeg"
    elif failure == "png":
        image["base64"] = base64.b64encode(b"not a PNG").decode()
    elif failure == "transport":
        image = {"format": "image/png", "uri": "/render.png"}
        transport.http.side_effect = ExecutionError("render", "candidate failed")
    check = engine.check_render({"image": image}, selected_arguments={"width": 20, "height": 10})
    assert check.status == "fail"
    assert not engine.repairable_render_selection(4, check)
    assert "pixel" not in engine.evidence["proofs"]


def test_render_reads_the_servers_format_field_and_refuses_conflicts():
    # honua-server RenderMapTool returns image.format, not image.mimeType.
    png = base64.b64encode(png_fixture()).decode()
    engine = engine_for(mock.Mock(), execution=dict(RENDER_FIXTURE))
    assert engine.check_render({"image": {"format": "image/png", "base64": png, "inlined": True},
                                "layers": [{"styleId": "journey-solid-red"}]}).status == "pass"
    for image in ({"format": "image/jpeg", "base64": png}, {"base64": png},
                  {"format": "image/png", "mimeType": "image/jpeg", "base64": png}):
        engine = engine_for(mock.Mock(), execution=dict(RENDER_FIXTURE))
        assert engine.check_render({"image": image, "layers": [{"styleId": "journey-solid-red"}]}).status == "fail"


def test_uri_render_rejects_a_conflicting_response_media_type():
    png = png_fixture()
    for header, status in (("image/jpeg", "fail"), ("application/octet-stream", "fail"),
                           ("image/png; charset=binary", "pass")):
        transport = mock.Mock(credentials={"proposer": "unit"})
        transport.http.return_value = (png, 200)
        transport.last_response_headers = {"content-type": header}
        engine = engine_for(transport, execution=dict(RENDER_FIXTURE))
        output = {"image": {"mimeType": "image/png", "uri": "/render.png"}, "layers": []}
        assert engine.check_render(output).status == status
        detail = engine.evidence["checks"]["4"]["pixel"]["detail"]
        if status == "fail":
            assert "media type" in detail
        else:
            assert engine.evidence["proofs"]["pixel"]["rgba"] == [239, 32, 32, 255]
    transport = mock.Mock(credentials={"proposer": "unit"})
    transport.http.return_value = (png_fixture((1, 2, 3, 255)), 200)
    transport.last_response_headers = {"content-type": "image/png"}
    engine = engine_for(transport, execution=dict(RENDER_FIXTURE))
    assert engine.check_render({"image": {"mimeType": "image/png", "uri": "/render.png"}, "layers": []}).status == "fail"


def test_failed_upload_proof_is_recorded_and_stops_the_stage_as_a_handled_failure():
    engine = upload_engine()
    engine.transport.cli_admin.return_value = (0, {**UPLOAD_RESULT, "success": False, "errorCode": "import.failed"})
    with pytest.raises(ExecutionError) as raised:
        engine.upload_dataset()
    assert raised.value.blocked is False
    assert engine.evidence["checks"]["3"]["import-complete"]["status"] == "fail"
    assert "importTable" not in engine.resources


def test_repeated_model_upload_is_recoverable_action_feedback_without_second_write():
    from transport import ActionRejected
    engine = upload_engine()
    engine.resources.update(importTable="owned_uploaded_table", importSchema="public")
    before = copy.deepcopy(engine.resources)
    with pytest.raises(ActionRejected, match="already been uploaded"):
        engine.execute(3, {"kind": "terminal_command", "command": executor.UPLOAD_COMMAND})
    engine.transport.cli_admin.assert_not_called()
    assert engine.resources == before


def test_run_build_records_a_failed_upload_instead_of_aborting(monkeypatch):
    engine = upload_engine()
    monkeypatch.setenv("HONUA_JOURNEY_DATASOURCE_PASSWORD", "private-database-key")
    engine.transport.cli_admin.return_value = (0, {**UPLOAD_RESULT, "featureCount": 1})
    calls = iter([{"connectionId": "connection-1"}, {"connectionId": "connection-1", "isHealthy": True}])
    with mock.patch.object(engine, "sdk_call", side_effect=lambda *args: next(calls)):
        results = engine.run_build()
    stage = results[0]
    assert stage.status == "fail"
    assert {c.id: c.status for c in stage.checks}["3.import-complete"] == "fail"
    assert {c.id: c.status for c in stage.checks}["3.execution"] == "fail"


@pytest.mark.parametrize("uri", ["honua://jobs/job-other", "honua://jobs/job-buffer-1-extra",
                                 "honua://jobs/job-buffer-1?x=1", "honua://jobs/"])
def test_stage_5_resource_uri_must_identify_the_returned_job(uri):
    engine = engine_for(mock.Mock())
    with pytest.raises(ExecutionError, match="does not identify the returned jobId"):
        engine.record_job({**JOB_OUTPUT, "resourceUri": uri})
    assert "jobEvidence" not in engine.evidence


@pytest.mark.parametrize("operation_id", ["studio publish", " ", None])
def test_approved_handle_operation_id_is_a_bounded_identity(operation_id):
    transport = mock.Mock(credentials={"proposer": "private-proposer", "approver": "private-approver"})
    engine = approval_engine(transport)
    engine.evidence["publicationOperation"] = {"operationInstanceId": "opinst-pub", "proposalId": "proposal-1"}
    pending = {**OBSERVED_PROPOSAL, "status": "Pending", "resolvedBy": None}
    transport.get_json.side_effect = [pending, pending, OBSERVED_PROPOSAL,
                                      {"data": {**OBSERVED_HANDLE, "operationId": operation_id}}]
    transport.cli_approve.side_effect = [(1, None), (0, None)]
    with pytest.raises(ExecutionError, match="bounded canonical identity"):
        engine.approve("proposal-1")
    assert "8" not in engine.evidence["canonicalIds"] and "approval" not in engine.evidence


@pytest.mark.parametrize("error_type, detail", [
    ("HonuaApiException", "published SDK refused the operation (HonuaApiException)"),
    ("Npgsql: Password=hunter2", "published SDK refused the operation"),
    (None, "published SDK refused the operation")])
def test_sdk_refusal_names_only_the_bridge_exception_type(tmp_path, error_type, detail):
    import sdk

    dll = tmp_path / "JourneyImport.dll"
    dll.write_bytes(b"")
    stdout = json.dumps({"status": "fail", "errorType": error_type})
    with mock.patch.object(sdk.subprocess, "run", return_value=mock.Mock(stdout=stdout)):
        with pytest.raises(ExecutionError) as raised:
            sdk.invoke({"dll": str(dll)}, "CreateConnectionAsync", [{}], base_url="http://127.0.0.1:1",
                       credential="private")
    assert raised.value.reason == detail


# ---- Redis-off cells record governed stages by the typed refusal (owner ruling, decision 3) ----------
# The refusal honua-server nightly-798d517 returned for honua_publish_service on a Production host
# without Redis (local reproduction of the run 38052304982 redis-off cell; masked by tool_error).
PUBLISH_REFUSAL = {"code": "unavailable", "missingDependency": "redis", "kind": "ExecutionFailed",
                   "retryable": "false", "message": "The operation proposal and approval control plane "
                   "requires a Redis-backed durable store."}
GP_REFUSAL = {"code": "unavailable", "missingDependency": "redis", "capability": "jobs.runner",
              "kind": "PreconditionFailed", "message": "Durable geoprocessing jobs and workflows require a "
              "Redis-backed job store."}


def capability_manifest(available=False, reason="dependency-unavailable"):
    return {"server": {"deploymentEnvironment": "Production"}, "capabilities": [
        {"id": capability, "available": available, "reasonCode": reason if not available else None}
        for capability in ("operations.proposals", "jobs.runner")]}


def cell_engine(cell="aws-serverless/redis-off", manifest=None):
    engine = upload_engine(operator=OPERATOR_KEY)
    engine.target.update(id=cell, kind=cell.split("/")[0])
    engine.transport.get_json.return_value = manifest or capability_manifest()
    engine.evidence["checks"]["3"] = {
        name: {"id": f"3.{name}", "kind": "artifact", "invocation": name, "status": "pass", "detail": "ok"}
        for name in ("datasource", "import-complete")}
    return engine


def stage_row(result):
    row = {"number": result.number, "stage": result.stage, "command": result.command, "status": result.status,
           "blockedBy": result.blocked_by, "checks": [c.as_receipt() for c in result.checks],
           **{key: None for key in ("operationId", "operationInstanceId", "correlationId", "auditId",
                                    "proposalId", "jobId", "resourceUri", "jobStatus", "jobCreatedAt")},
           "evidence": {"uri": "https://example.invalid/run", "source": "live-aws-serverless",
                        "freshness": "verified-current", "completeness": "complete",
                        "observedAt": "2026-10-10T00:00:00Z"}}
    if result.topology_refusal:
        row["topologyRefusal"] = result.topology_refusal
    return row


def validate_stage(row):
    schema = json.loads((Path(__file__).parent / "receipt.schema.json").read_text())
    jsonschema.validate(row, {"$defs": schema["$defs"], "$ref": "#/$defs/stage"})


@pytest.mark.parametrize("number, tool, problem, capability", [
    (3, "honua_publish_service", PUBLISH_REFUSAL, "operations.proposals"),
    (5, "honua_validate_plan", GP_REFUSAL, "jobs.runner")])
def test_redis_off_cell_records_the_typed_refusal_as_the_stage_outcome(number, tool, problem, capability):
    engine = cell_engine()
    error = ExecutionError(tool, executor.describe_tool_error(problem), problem=problem)
    assert engine.record_topology_refusal(number, error)
    result = engine.result(number)
    assert result.status == "pass" and result.blocked_by == []
    assert result.operation_instance_id is None and result.job_id is None
    refusal = result.topology_refusal
    assert (refusal["topology"], refusal["missingDependency"], refusal["manifestCapability"],
            refusal["manifestReasonCode"], refusal["rulingStatus"], refusal["tool"]) == (
        "redis-off", "redis", capability, "dependency-unavailable", "ruled", tool)
    assert refusal["deploymentEnvironment"] == "Production"
    execution = next(c for c in result.checks if c.id == f"{number}.execution")
    assert "topology: redis-off; " in execution.detail and "missingDependency=redis" in execution.detail
    assert execution.status == "pass"
    assert not any(c.id.endswith("canonical-evidence") for c in result.checks)
    validate_stage(stage_row(result))
    # Without the refusal record the same stage row may not pass without its canonical identities.
    row = stage_row(result)
    del row["topologyRefusal"]
    with pytest.raises(jsonschema.ValidationError):
        validate_stage(row)


def test_stage_3_passes_on_the_ruled_refusal_with_the_publish_evidence_it_carries():
    # Owner ruling 2026-10-10 (decision 3 extended to stage 3): the refusal of service.publish on a
    # Redis-off cell is the stage's pass, recorded with the same evidence as stages 5-8.
    assert executor.TOPOLOGY_REFUSAL_STAGES[3]["capability"] == "operations.proposals"
    engine = cell_engine()
    error = ExecutionError("honua_publish_service", executor.describe_tool_error(PUBLISH_REFUSAL),
                           problem=PUBLISH_REFUSAL)
    assert engine.record_topology_refusal(3, error)
    result = engine.result(3)
    assert result.status == "pass"
    assert {c.id: c.status for c in result.checks} == {
        "3.datasource": "pass", "3.import-complete": "pass", "3.execution": "pass"}
    refusal = result.topology_refusal
    assert {key: refusal[key] for key in ("code", "missingDependency", "kind", "manifestCapability",
                                          "manifestReasonCode", "rulingStatus", "ruling")} == {
        "code": "unavailable", "missingDependency": "redis", "kind": "ExecutionFailed",
        "manifestCapability": "operations.proposals", "manifestReasonCode": "dependency-unavailable",
        "rulingStatus": "ruled", "ruling": executor.REDIS_OFF_RULING}
    assert refusal["message"].startswith("The operation proposal and approval control plane requires")
    detail = next(c for c in result.checks if c.id == "3.execution").detail
    assert detail.startswith("topology: redis-off; honua_publish_service refused with the typed durable-store refusal")
    assert "reasonCode=dependency-unavailable" in detail and "retryable=false" in detail
    # A stage 3 refusal recorded under the former pending status is no longer a valid receipt row.
    row = stage_row(result)
    with pytest.raises(jsonschema.ValidationError):
        validate_stage({**row, "topologyRefusal": {**row["topologyRefusal"],
                                                   "rulingStatus": "extension-pending-owner-ruling"}})


@pytest.mark.parametrize("field, value", [("retryable", "true"), ("retryable", None), ("kind", "PreconditionFailed"),
                                          ("kind", None)])
def test_stage_3_passes_only_on_the_refusal_the_ruling_names(field, value):
    # The ruling names kind=ExecutionFailed and retryable=false; any other publish refusal keeps its failure.
    problem = {key: item for key, item in {**PUBLISH_REFUSAL, field: value}.items() if item is not None}
    engine = cell_engine()
    assert not engine.record_topology_refusal(3, ExecutionError("honua_publish_service", "x", problem=problem))
    assert "topologyRefusals" not in engine.evidence


def test_stage_3_refusal_still_requires_the_datasource_and_import_that_ran_before_it():
    engine = cell_engine()
    engine.evidence["checks"]["3"].pop("import-complete")
    assert engine.record_topology_refusal(3, ExecutionError("honua_publish_service", "x", problem=PUBLISH_REFUSAL))
    assert engine.result(3).status == "blocked"


@pytest.mark.parametrize("case", ["redis-on-cell", "local-target", "other-dependency", "not-unavailable",
                                  "wrong-capability", "manifest-available", "ungoverned-stage", "blocked"])
def test_anything_but_the_topology_refusal_on_a_redis_off_cell_keeps_its_failure(case):
    engine = cell_engine(cell="aws-ecs/redis-on" if case == "redis-on-cell" else "aws-serverless/redis-off",
                         manifest=capability_manifest(available=True) if case == "manifest-available" else None)
    if case == "local-target":
        engine.target.update(id="local-docker", kind="local-docker")
    problem = dict(PUBLISH_REFUSAL)
    if case == "other-dependency":
        problem["missingDependency"] = "postgres"
    if case == "not-unavailable":
        problem["code"] = "internal"
    if case == "wrong-capability":
        problem["capability"] = "jobs.runner"
    number = 4 if case == "ungoverned-stage" else 3
    error = ExecutionError("honua_publish_service", "refused", problem=problem, blocked=case == "blocked")
    assert not engine.record_topology_refusal(number, error)
    assert "topologyRefusals" not in engine.evidence
    if case == "manifest-available":
        assert "does not report operations.proposals dependency-unavailable" in error.reason


def test_a_local_target_may_declare_its_redis_topology():
    engine = cell_engine()
    engine.target.update(id="local-docker-no-redis", kind="local-docker", redisTopology="redis-off")
    assert engine.declared_redis_topology() == "redis-off"
    assert engine.record_topology_refusal(3, ExecutionError("honua_publish_service", "x", problem=PUBLISH_REFUSAL))


def test_only_the_governed_stages_may_carry_their_topology_refusal():
    engine = cell_engine()
    engine.record_topology_refusal(3, ExecutionError("honua_publish_service", "x", problem=PUBLISH_REFUSAL))
    row = stage_row(engine.result(3))
    validate_stage(row)
    # Stage 4 (style/render) is a Redis-off topology; stage 5 refuses on jobs.runner; 6-8 refuse on
    # operations.proposals like stage 3.
    for number in (4, 5):
        with pytest.raises(jsonschema.ValidationError):
            validate_stage({**row, "number": number})
    for number in (6, 7, 8):
        validate_stage({**row, "number": number})


def test_a_ruled_stage_6_refusal_is_recorded_like_stage_5():
    engine = cell_engine()
    error = ExecutionError("honua_studio_create_draft", "x", problem={**PUBLISH_REFUSAL, "kind": "PreconditionFailed"})
    assert engine.record_topology_refusal(6, error)
    result = engine.result(6)
    assert result.status == "pass" and result.topology_refusal["rulingStatus"] == "ruled"
    validate_stage(stage_row(result))


def test_a_receipt_may_carry_a_topology_refusal_only_for_a_redis_off_target():
    engine = cell_engine()
    engine.record_topology_refusal(3, ExecutionError("honua_publish_service", "x", problem=PUBLISH_REFUSAL))
    row = stage_row(engine.result(3))
    schema = json.loads((Path(__file__).parent / "receipt.schema.json").read_text())
    rule = next(rule for rule in schema["allOf"] if "redis-off" in rule.get("description", ""))

    def check(target):
        jsonschema.validate({"stages": [row], "target": target}, {"$defs": schema["$defs"], **rule})

    check({"id": "aws-serverless/redis-off", "kind": "aws-serverless", "redisTopology": "redis-off"})
    check({"id": "local-docker", "kind": "local-docker", "redisTopology": "redis-off"})
    for target in ({"id": "aws-ecs/redis-on", "kind": "aws-ecs", "redisTopology": "redis-off"},
                   {"id": "aws-ecs/redis-off", "kind": "aws-ecs"},
                   {"id": "aws-ecs/redis-on", "kind": "aws-ecs", "redisTopology": "redis-on"}):
        with pytest.raises(jsonschema.ValidationError):
            check(target)
    # The receipt's target names the topology the executor used.
    assert executor.declared_redis_topology({"id": "aws-ecs/redis-off", "kind": "aws-ecs"}) == "redis-off"
    assert executor.declared_redis_topology({"id": "local-docker", "kind": "local-docker"}) is None
    assert executor.declared_redis_topology({"id": "x/redis-off", "kind": "local-docker"}) is None


def test_the_run_credentials_are_scrubbed_from_every_retained_error_field():
    fields = transport_module.tool_error({"structuredContent": {
        "code": f"bad-{OPERATOR_KEY}", "kind": OPERATOR_KEY, "message": "ok"}}, [OPERATOR_KEY])
    assert OPERATOR_KEY not in json.dumps(fields)
    assert fields["code"] == "bad-[redacted]" and fields["kind"] == "[redacted]"


def test_only_pre_execution_refusals_of_the_selected_action_are_action_rejections():
    """Model feedback (ActionRejected) vs candidate/execution failure (plain ExecutionError)."""
    from transport import ActionRejected
    engine = engine_for(mock.Mock())
    for action in ({"kind": "prose"}, {"kind": "tool_call", "tool": "honua_not_in_view", "arguments": {}},
                   {"kind": "terminal_command", "command": "rm -rf /"}):
        with pytest.raises(ActionRejected):
            engine.execute(4, action)
    # A valid in-view render whose candidate request fails is an execution failure, not a rejection.
    transport = mock.Mock(credentials={"proposer": "k"})
    transport.tool.side_effect = ExecutionError("honua_render_map", "candidate request failed")
    engine = engine_for(transport)
    with pytest.raises(ExecutionError) as failed:
        engine.execute(4, {"kind": "tool_call", "tool": "honua_render_map", "arguments": {}})
    assert not isinstance(failed.value, ActionRejected)


def test_install_and_preflight_stages_act_as_the_operator_when_one_exists():
    # honua_list_capabilities needs Catalog Discover; the proposer's write:journey key lacks it.
    with_operator = engine_for(mock.Mock(credentials={"operator": "o", "proposer": "p"}))
    assert [with_operator.principal(n) for n in (1, 2, 6, 7)] == ["operator", "operator", "proposer", "proposer"]
    without = engine_for(mock.Mock(credentials={"proposer": "p"}))
    assert without.principal(1) == "proposer"


def test_typed_client_side_tool_errors_are_candidate_refusals_and_server_faults_are_not():
    import live_driver
    assert {"not_found", "failed_precondition"} <= live_driver.CANDIDATE_REFUSAL_CODES
    assert not {"internal", "unavailable", "permission_denied", "invalid_argument"} & live_driver.CANDIDATE_REFUSAL_CODES
    assert "invalid_argument" in live_driver.ADMITTED_CALL_DEFECT_CODES


def test_a_stage_admitted_call_rejected_as_invalid_argument_is_a_candidate_defect():
    """Fixture-proven inputs the candidate rejects are not model feedback.

    A schema-valid unknown style is still a value the model can correct.
    """
    import live_driver
    from transport import ActionRejected, ExecutionError

    admitted = ExecutionError(
        "honua_render_map", "candidate rejected supported inputs",
        problem={"code": "invalid_argument", "argumentsProven": True})
    unknown_style = ExecutionError(
        "honua_apply_style_preset", "unknown style",
        problem={"code": "invalid_argument"})
    missing = ExecutionError(
        "honua_render_map", "service is not published",
        problem={"code": "not_found"})
    invalid = ActionRejected("honua_render_map", "arguments do not satisfy the observed tool schema")

    def classify(exc):
        code = exc.problem.get("code")
        if isinstance(exc, ActionRejected):
            return "invalid-action"
        if code in live_driver.ADMITTED_CALL_DEFECT_CODES and exc.problem.get("argumentsProven") is True:
            return "candidate-execution"
        if code in live_driver.CANDIDATE_REFUSAL_CODES or code in live_driver.ADMITTED_CALL_DEFECT_CODES:
            return "candidate-refusal"
        return "candidate-execution"

    assert classify(admitted) == "candidate-execution"
    assert classify(unknown_style) == "candidate-refusal"
    assert classify(missing) == "candidate-refusal"
    assert classify(invalid) == "invalid-action"


def test_only_authored_fixture_arguments_mark_a_validation_error_proven():
    transport = mock.Mock()
    transport.credentials = {"proposer": "p", "operator": "o"}
    observation = stages.Observation(setup_view_present=True, setup_discovery={
        "tools": [
            {"name": "honua_render_map", "inputSchema": {"type": "object"}},
            {"name": "honua_apply_style_preset", "inputSchema": {"type": "object"}},
        ],
        "metadata": {"view": "setup"}})
    engine = executor.JourneyExecutor(
        {"workspaceId": "workspace"},
        {"execution": {"styleId": "journey-solid-red", "renderSize": [128, 128], "bbox": [-2, -2, 2, 2]}},
        observation, transport)
    transport.tool.return_value = {"isError": True, "structuredContent": {"code": "invalid_argument"}}

    def rejected(tool, arguments):
        with pytest.raises(ExecutionError) as caught:
            engine.execute(4, {"kind": "tool_call", "tool": tool, "arguments": arguments})
        return caught.value

    assert rejected("honua_apply_style_preset", {"styleId": "journey-solid-red-unknown"}).problem.get("argumentsProven") is not True
    assert rejected("honua_apply_style_preset", {"styleId": "journey-solid-red"}).problem["argumentsProven"] is True
    assert rejected("honua_render_map", {"width": 20, "height": 10}).problem.get("argumentsProven") is not True
    assert rejected("honua_render_map", {"width": 128, "height": 128, "bbox": [0, 0, 1, 1]}).problem.get("argumentsProven") is not True
    assert rejected("honua_render_map", {"width": 128, "height": 128, "bbox": [-2, -2, 2, 2]}).problem["argumentsProven"] is True
