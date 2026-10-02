"""Driver handoffs only; these tests do not qualify a live candidate journey."""
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


if __name__ == "__main__":
    unittest.main()
