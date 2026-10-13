"""Driver handoffs only; these tests do not qualify a live candidate journey."""
import io
import json
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
