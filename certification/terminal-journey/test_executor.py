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
    ("honua-journey-sdk CreateConnectionAsync [NaN]", 3),
    ('honua-journey-sdk CreateConnectionAsync [{"name":"source","name":"other"}]', 3)])
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


@pytest.mark.parametrize("method,key", [("TestConnectionAsync", "connectionId"),
                                       ("GetGeoservicesImportJobStatusAsync", "jobId")])
def test_sdk_poll_cannot_replace_the_expected_identity_with_its_own_result(method, key):
    engine = sdk_engine()
    engine.resources[key] = "submitted-identity"
    with mock.patch.object(executor.sdk, "invoke", return_value={key: "different-identity"}):
        with pytest.raises(ExecutionError, match="submitted resource identity"):
            engine.sdk_call(method, ["submitted-identity"])
    assert engine.resources[key] == "submitted-identity"
    assert not engine.evidence["actions"]


def test_approval_poll_accepts_recorded_pending_executing_succeeded_sequence():
    transport = mock.Mock(credentials={"proposer": "private-proposer", "approver": "private-approver"})
    engine = engine_for(transport)
    engine.resources["proposalId"] = "proposal-1"
    pending = {"proposalId": "proposal-1", "status": "Pending", "requestedBy": "actor-proposer"}
    handle = {"operationInstanceId": "opinst-exec", "operationId": "studio.content.create-publication-request",
              "proposalId": "proposal-1", "auditId": "audit-approval", "correlationId": "00-corr-approval"}
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
    engine = engine_for(transport)
    engine.resources["proposalId"] = "proposal-1"
    pending = {"proposalId": "proposal-1", "status": "Pending", "requestedBy": "actor-proposer"}
    final = {**pending, "status": "Succeeded", "resolvedBy": "actor-approver",
             "executionOperationId": "opinst-exec", "approvalId": "approval-1"}
    handle = {"operationInstanceId": "opinst-exec", "auditId": "audit-approval", "correlationId": "corr"}
    transport.get_json.side_effect = [pending, pending, final, handle]
    transport.cli_approve.side_effect = [(1, None), (0, None)]
    assert engine.approve("proposal-1")["approvalId"] == "approval-1"

    engine = engine_for(transport)
    engine.resources["proposalId"] = "proposal-1"
    transport.get_json.side_effect = [pending, pending, final, {**handle, "proposalId": "proposal-other"}]
    transport.cli_approve.side_effect = [(1, None), (0, None)]
    with pytest.raises(ExecutionError, match="does not join the proposal"):
        engine.approve("proposal-1")
    assert "approval" not in engine.evidence


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
            return {"structuredContent": {"jobId": "job-buffer-1", "status": "accepted"}}
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
    # This capture is not a live certification. The stage stays blocked until the
    # candidate returns the canonical operation instance, correlation and audit identities.
    assert stage.status == "blocked"
    assert any(check.id == "5.canonical-evidence" and check.status == "blocked" for check in stage.checks)

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
