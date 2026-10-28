"""Driver handoffs only; these tests do not qualify a live candidate journey."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import live_driver
import stages


class StageBindingTests(unittest.TestCase):
    def setUp(self):
        self.journey = json.loads((live_driver.HERE / "journey.v1.json").read_text())
        self.workspace = mock.Mock(status="pass")
        self.workspace.missing_for_stage.return_value = []

    def test_descriptor_number_and_id_observe_the_same_real_stage_evidence(self):
        stage = self.journey["stages"][2]
        evidence = [live_driver._stage_status(self.journey, stages.Observation(), self.workspace, ref)
                    for ref in (stage, stage["id"], stage["number"])]
        self.assertEqual(evidence[0], evidence[1])
        self.assertEqual(evidence[0], evidence[2])
        self.assertEqual(evidence[0]["status"], "blocked")
        self.assertEqual(evidence[0]["id"], "publish-service")
        self.assertEqual(evidence[0]["number"], 3)

    def test_descriptor_cannot_replace_the_reviewed_command_or_number(self):
        for patch in ({"command": "echo fake-success"}, {"number": 8}, {"id": "unknown"}):
            with self.subTest(patch=patch), self.assertRaises(live_driver.DriverError):
                live_driver._stage_status(self.journey, stages.Observation(), self.workspace,
                                          {**self.journey["stages"][2], **patch})

    def test_unexecutable_injection_does_not_write_fictional_armed_state(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.object(live_driver, "STATE_ROOT", Path(root)):
            state = {"workspaceId": "test-workspace", "armedError": None}
            live_driver._write_state(state["workspaceId"], state)
            response = live_driver.op_inject_error({"workspaceId": state["workspaceId"], "errorId": "error-1"})
            self.assertEqual(response["errorId"], "error-1")
            self.assertEqual(response["status"], "blocked")
            self.assertEqual(live_driver._read_state(state["workspaceId"]), state)

    def test_workspace_identifier_cannot_escape_the_session_directory(self):
        for identifier in ("../elsewhere", "/tmp/path", "a/b", "a\\b", ".", ""):
            with self.subTest(identifier=identifier), self.assertRaises(live_driver.DriverError):
                live_driver._state_path(identifier)


class RecoverableActionTransportTests(unittest.TestCase):
    def test_recoverable_refusal_remains_failed_but_reaches_the_canary(self):
        for recoverable, exit_code in ((True, 0), (False, 1)):
            with self.subTest(recoverable=recoverable):
                response = {"status": "fail", "stageStatus": {}, "canonicalIds": {}, "blockedBy": [],
                            "result": {"accepted": False, "injectedError": {"id": "fault-1", "recoverable": recoverable}}}
                stdin = io.StringIO(json.dumps({"protocol": live_driver.PROTOCOL, "operation": "execute"}))
                stdout = io.StringIO()
                with mock.patch.object(live_driver, "handle", return_value=response), \
                     mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", stdout):
                    actual = live_driver.main()
                self.assertEqual(actual, exit_code)
                observed = json.loads(stdout.getvalue())
                self.assertEqual(observed["status"], "fail")
                self.assertFalse(observed["result"]["accepted"])

    def test_other_operation_cannot_mask_a_process_failure_with_a_fault_marker(self):
        response = {"status": "fail", "result": {"injectedError": {"recoverable": True}}}
        stdin = io.StringIO(json.dumps({"protocol": live_driver.PROTOCOL, "operation": "verify"}))
        with mock.patch.object(live_driver, "handle", return_value=response), \
             mock.patch.object(sys, "stdin", stdin), mock.patch.object(sys, "stdout", io.StringIO()):
            self.assertEqual(live_driver.main(), 1)


class DatasourceProcessEnvironmentTests(unittest.TestCase):
    def engine(self, directory, kind):
        target = {"kind": kind, "adminPassword": {"env": "TEST_BOOTSTRAP", "default": ""},
                  "execution": {"datasource": {"passwordEnv": "HONUA_JOURNEY_DATASOURCE_PASSWORD"}}}
        with mock.patch.object(live_driver.local_fixture, "credentials", return_value={}):
            return live_driver._executor({"workdir": directory, "baseUrl": "http://localhost"},
                                         target, stages.Observation())

    def test_new_driver_process_resolves_local_datasource_without_setup_environment(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {}, clear=True):
            engine = self.engine(directory, "local-docker")
            self.assertEqual(engine.datasource_arguments(), {"password": "honua"})

    def test_explicit_environment_is_preserved_and_missing_cloud_secret_stays_blocked(self):
        for kind in ("local-docker", "aws-serverless", "aws-ecs", "aws-eks"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                with mock.patch.dict(os.environ, {"HONUA_JOURNEY_DATASOURCE_PASSWORD": "explicit-fixture"}, clear=True):
                    self.assertEqual(self.engine(directory, kind).datasource_arguments(),
                                     {"password": "explicit-fixture"})
                if kind != "local-docker":
                    with mock.patch.dict(os.environ, {}, clear=True):
                        with self.assertRaises(live_driver.ExecutionError) as refused:
                            self.engine(directory, kind).datasource_arguments()
                        self.assertTrue(refused.exception.blocked)
                        self.assertNotIn("HONUA_JOURNEY_DATASOURCE_PASSWORD", os.environ)


if __name__ == "__main__":
    unittest.main()


def test_approval_runs_final_and_authority_checks_before_next_observation():
    state = {"workspaceId": "unit", "execution": {}, "stackUp": True}
    state["execution"] = {"approvalResolution": {"proposalId": "proposal"}}
    engine = mock.Mock(evidence=state["execution"])
    engine.approve.return_value = {"proposalId": "proposal", "resolvedBy": "approver", "auditId": "audit",
                                   "executionOperationId": "opinst", "approvalId": None, "proposerSelfApproval": "denied"}
    with mock.patch.object(live_driver, "_rehydrate", return_value=(state, {}, {}, stages.Observation(), None)), \
         mock.patch.object(live_driver, "_executor", return_value=engine), mock.patch.object(live_driver, "_write_state"):
        response = live_driver.op_approve({"workspaceId": "unit", "proposalId": "proposal"})
    assert response["status"] == "approved"
    # The canary binds the server-emitted proposal, resolver and audit identities.
    assert (response["proposalId"], response["resolvedBy"], response["auditId"]) == ("proposal", "approver", "audit")
    assert [call[0] for call in engine.method_calls] == ["approve", "verify_final", "verify_authority"]


def test_observe_reports_persisted_blocker_instead_of_ready():
    import executor
    from transport import Transport
    engine = executor.JourneyExecutor({"workspaceId": "unit", "stackUp": True, "executionEnabled": True}, {},
        stages.Observation(setup_view_present=True), mock.Mock())
    engine._check(4, "pixel", "honua_render_map", lambda: (_ for _ in ()).throw(
        executor.ExecutionError("honua_render_map", "renderer unavailable", blocked=True)))
    stage = json.loads((live_driver.HERE / "journey.v1.json").read_text())["stages"][3]
    prerequisites = {"number": 4, "id": stage["id"], "command": stage["command"], "status": "blocked",
                     "blockedBy": [], "checks": []}
    with mock.patch.object(live_driver, "_rehydrate", return_value=(engine.state, {}, {}, engine.observation, None)), \
         mock.patch.object(live_driver, "_executor", return_value=engine), \
         mock.patch.object(live_driver, "_stage_status", return_value=prerequisites), \
         mock.patch.object(live_driver, "_write_state"):
        response = live_driver.op_observe({"stage": stage["id"]})
    assert response["status"] == response["stageStatus"]["status"] == "blocked"
    assert response["blockedBy"] == [stages.JOURNEY_DRIVER]
    assert response["stageStatus"]["checks"][0]["invocation"] == "honua_render_map"


def test_missing_unexecuted_assertions_remain_actionable_before_first_attempt():
    import executor
    engine = executor.JourneyExecutor({"workspaceId": "unit", "stackUp": True, "executionEnabled": True}, {},
        stages.Observation(setup_view_present=True), mock.Mock())
    stage = json.loads((live_driver.HERE / "journey.v1.json").read_text())["stages"][3]
    status = {"number": 4, "id": stage["id"], "command": stage["command"], "checks": [], "status": "blocked"}
    with mock.patch.object(live_driver, "_rehydrate", return_value=(engine.state, {}, {}, engine.observation, None)), \
         mock.patch.object(live_driver, "_executor", return_value=engine), \
         mock.patch.object(live_driver, "_stage_status", return_value=status), mock.patch.object(live_driver, "_write_state"):
        response = live_driver.op_observe({"stage": stage["id"]})
    assert response["status"] == response["stageStatus"]["status"] == "ready"
    assert response["blockedBy"] == []


def test_observe_exposes_the_actual_stage_terminal_bridge_contract():
    import executor
    engine = executor.JourneyExecutor({"workspaceId": "unit", "stackUp": True, "executionEnabled": True}, {},
        stages.Observation(setup_view_present=True,
            setup_discovery={"tools": [{"name": "honua_ingest_dataset"}], "metadata": {"view": "setup"}}),
        mock.Mock(credentials={"proposer": "test-proposer"}))
    stage = json.loads((live_driver.HERE / "journey.v1.json").read_text())["stages"][2]
    status = {"number": 3, "id": stage["id"], "command": stage["command"], "checks": [], "status": "blocked"}
    with mock.patch.object(live_driver, "_rehydrate", return_value=(engine.state, {}, {}, engine.observation, None)), \
         mock.patch.object(live_driver, "_executor", return_value=engine), \
         mock.patch.object(live_driver, "_stage_status", return_value=status), mock.patch.object(live_driver, "_write_state"):
        response = live_driver.op_observe({"stage": stage["id"]})
    assert response["observation"]["terminalSurface"] == engine.terminal_surface(3)
    assert response["stageStatus"]["status"] == "ready"
    assert not engine.evidence["actions"]


def test_next_stage_context_exposes_native_publication_identity_separately_from_fixture():
    import executor
    target = {"execution": {"serviceId": "journey"}}
    engine = executor.JourneyExecutor({"workspaceId": "unit", "stackUp": True, "executionEnabled": True}, target,
        stages.Observation(setup_view_present=True), mock.Mock(credentials={"proposer": "private-proposer"}))
    with mock.patch.object(engine, "check_features"):
        engine.record_publication({"status": "Completed", "requiresApproval": False, "layerId": "17",
            "serviceName": "observed-published", "serviceUri": "honua://published-services/observed-published"})
    stage = json.loads((live_driver.HERE / "journey.v1.json").read_text())["stages"][3]
    status = {"number": 4, "id": stage["id"], "command": stage["command"], "checks": [], "status": "blocked"}
    with mock.patch.object(live_driver, "_rehydrate", return_value=(engine.state, target, {}, engine.observation, None)), \
         mock.patch.object(live_driver, "_executor", return_value=engine), \
         mock.patch.object(live_driver, "_stage_status", return_value=status), mock.patch.object(live_driver, "_write_state"):
        response = live_driver.op_observe({"stage": stage["id"]})
    actual = response["observation"]["resources"]
    assert actual["serviceId"] == actual["serviceName"] == "observed-published" and actual["layerId"] == 17
    assert actual["serviceUri"] == "honua://published-services/observed-published"
    assert response["observation"]["fixture"] == {"serviceId": "journey"}
    assert "private-proposer" not in json.dumps(response)


def _cloud_target_file(path, cell="aws-ecs/redis-on"):
    import cloud_target
    target = cloud_target.build(json.loads(live_driver.DEFAULT_TARGET.read_text()),
        cell=cell, endpoint="https://owned-cell.example")
    path.write_text(json.dumps(target))
    return target


def test_cloud_setup_observes_external_endpoint_and_never_manages_compose(tmp_path):
    target_path = tmp_path / "target.json"
    target = _cloud_target_file(target_path)
    deployment = _cloud_deployment_receipt(tmp_path / "deployment.json", target, target_path)
    workspace = mock.Mock(status="pass", install_notes=[])
    workspace.as_receipt.return_value = {"status": "pass"}
    observation = stages.Observation(ready=True, setup_view_present=True)
    import run as driver
    with mock.patch.object(live_driver, "STATE_ROOT", tmp_path / "sessions"), \
         mock.patch.object(live_driver.pins, "resolve_client_workspace", return_value=workspace), \
         mock.patch.object(live_driver.pins, "install_executables", return_value=(tmp_path / "bin", "", [])), \
         mock.patch.object(live_driver, "_compose_for", side_effect=AssertionError("cloud must never start local Docker")), \
         mock.patch.object(driver, "observe", return_value=observation) as observe, \
         mock.patch.object(live_driver.local_fixture, "credentials", return_value={"operator": "unit"}) as credentials, \
         mock.patch.object(live_driver.local_fixture, "prepare_style_catalog") as prepare_style:
        response = live_driver.op_setup({"target": str(target_path), "deploymentReceipt": deployment})
        assert response["status"] == "ready"
        assert response["baseUrl"] == target["replicaBaseUrl"]
        assert observe.call_args.args[1] == target["replicaBaseUrl"]
        expected_image = json.loads(Path(deployment["path"]).read_text())["server"]["image"]
        assert observe.call_args.args[4] == expected_image
        assert "same-job deployment readback" in live_driver._read_state(response["workspaceId"])["deploymentDetail"]

        assert credentials.call_args.kwargs["mint"] is True
        credentials.assert_called_once()
        assert prepare_style.call_args.args[0] == target
        assert prepare_style.call_args.args[1].credentials == {"operator": "unit"}
        state = live_driver._read_state(response["workspaceId"])
        assert state["external"] is True


def test_cloud_teardown_revokes_application_principals_without_deleting_external_cell(tmp_path):
    target_path = tmp_path / "target.json"
    _cloud_target_file(target_path)
    with mock.patch.object(live_driver, "STATE_ROOT", tmp_path / "sessions"), \
         mock.patch.object(live_driver, "_compose_for", side_effect=AssertionError("no external infrastructure teardown")), \
         mock.patch.object(live_driver.local_fixture, "revoke", return_value=(["operator"], [])) as revoke:
        live_driver._write_state("cloud", {"workspaceId": "cloud", "targetPath": str(target_path),
            "workdir": str(tmp_path), "baseUrl": "https://owned-cell.example", "external": True})
        response = live_driver.op_teardown({"workspaceId": "cloud"})
        assert response["status"] == "pass" and "external cell retained" in response["detail"]
        revoke.assert_called_once()
        assert not live_driver._state_path("cloud").exists()


def test_failed_cloud_principal_revocation_retains_private_session_for_retry(tmp_path):
    target_path = tmp_path / "target.json"
    _cloud_target_file(target_path)
    with mock.patch.object(live_driver, "STATE_ROOT", tmp_path / "sessions"), \
         mock.patch.object(live_driver.local_fixture, "revoke", return_value=([], ["operator"])):
        live_driver._write_state("cloud", {"workspaceId": "cloud", "targetPath": str(target_path),
            "workdir": str(tmp_path), "baseUrl": "https://owned-cell.example", "external": True})
        response = live_driver.op_teardown({"workspaceId": "cloud"})
        assert response["status"] == "fail" and live_driver._state_path("cloud").exists()


def test_driver_refuses_target_changed_after_setup(tmp_path):
    target_path = tmp_path / "target.json"
    _cloud_target_file(target_path)
    with mock.patch.object(live_driver, "STATE_ROOT", tmp_path / "sessions"):
        live_driver._write_state("cloud", {"targetPath": str(target_path), "targetSha256": "0" * 64})
        with unittest.TestCase().assertRaisesRegex(live_driver.DriverError, "changed after setup"):
            live_driver._rehydrate({"workspaceId": "cloud"})



def _cloud_deployment_receipt(path, target, target_path):
    manifest = live_driver._load_yaml(live_driver.ROOT / "platform-manifest.yaml")
    server = manifest["components"]["honua-server"]
    image_name, digest_name = (("awsLambdaImage", "awsLambdaDigest") if target["kind"] == "aws-serverless"
                               else ("image", "digest"))
    receipt = {"status": "pass", "target": {"id": target["id"], "baseUrl": target["replicaBaseUrl"],
        "configSha256": live_driver.hashlib.sha256(target_path.read_bytes()).hexdigest()},
        "server": {"sourceSha": server["sha"], "image": f"{server[image_name].split('@', 1)[0]}@{server[digest_name]}"},
        "stages": [{"number": 1, "status": "pass", "checks": [{"id": "1.1-candidate-image", "status": "pass"}],
            "evidence": {"source": "live-" + target["kind"], "freshness": "verified-current"}}]}
    path.write_text(json.dumps(receipt))
    return {"path": str(path), "sha256": live_driver.hashlib.sha256(path.read_bytes()).hexdigest()}


def test_external_model_setup_requires_bound_observed_deployment_image(tmp_path):
    target_path = tmp_path / "target.json"
    target = _cloud_target_file(target_path)
    manifest = live_driver._load_yaml(live_driver.ROOT / "platform-manifest.yaml")
    digest = live_driver.hashlib.sha256(target_path.read_bytes()).hexdigest()
    proof = _cloud_deployment_receipt(tmp_path / "deployment.json", target, target_path)
    image, detail = live_driver._cloud_image(target, manifest, digest, proof)
    assert image == json.loads(Path(proof["path"]).read_text())["server"]["image"]
    assert proof["sha256"] in detail
    mutations = ("missing", "digest", "cell", "config", "origin", "image", "unobserved", "source")
    for mutation in mutations:
        proof = _cloud_deployment_receipt(tmp_path / "deployment.json", target, target_path)
        path = Path(proof["path"])
        receipt = json.loads(path.read_text())
        if mutation == "cell": receipt["target"]["id"] = "aws-serverless/redis-on"
        if mutation == "config": receipt["target"]["configSha256"] = "0" * 64
        if mutation == "origin": receipt["target"]["baseUrl"] = "https://wrong.example"
        if mutation == "image": receipt["server"]["image"] = "unobserved-image"
        if mutation == "unobserved": receipt["stages"][0]["checks"][0]["status"] = "blocked"
        if mutation == "source": receipt["stages"][0]["evidence"]["source"] = "harness-build"
        path.write_text(json.dumps(receipt))
        proof["sha256"] = live_driver.hashlib.sha256(path.read_bytes()).hexdigest()
        if mutation == "digest": proof["sha256"] = "0" * 64
        with unittest.TestCase().subTest(mutation=mutation), unittest.TestCase().assertRaises(live_driver.DriverError):
            live_driver._cloud_image(target, manifest, digest, None if mutation == "missing" else proof)


def test_server_log_is_filtered_and_redacted_before_teardown(tmp_path, monkeypatch):
    log = tmp_path / "server.log"
    monkeypatch.setenv(live_driver.SERVER_LOG_ENV, str(log))
    monkeypatch.setenv("HONUA_ADMIN_PASSWORD", "admin-secret-value")

    class Compose:
        def logs(self, service, tail=4000):
            assert service == "server"
            return ("[INF] request ok admin-secret-value\n"
                    "[WRN] StudioAiProxy provider request failed: AmazonBedrockRuntimeException key=admin-secret-value\n")

    live_driver._retain_server_log(Compose(), {"compose": {"serverService": "server"}})
    kept = log.read_text()
    assert "AmazonBedrockRuntimeException" in kept
    assert "request ok" not in kept
    assert "admin-secret-value" not in kept


def test_server_log_is_not_retained_without_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv(live_driver.SERVER_LOG_ENV, raising=False)

    class Compose:
        def logs(self, *_a, **_k):
            raise AssertionError("logs must not be read without opt-in")

    live_driver._retain_server_log(Compose(), {})


def test_unwritable_server_log_never_blocks_teardown(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    monkeypatch.setenv(live_driver.SERVER_LOG_ENV, str(blocker / "server.log"))

    class Compose:
        def logs(self, *_a, **_k):
            return "[ERR] StudioAiProxy failure\n"

    live_driver._retain_server_log(Compose(), {})  # must not raise
