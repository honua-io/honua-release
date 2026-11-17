"""Self-tests for the person's approval in the interop proposal scenario: the orchestrator observes every API
key approval as refused, and approves only through the Console password session helper."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_interop as suite

HERE = Path(__file__).resolve().parent
HUMAN_APPROVED = suite.HUMAN_APPROVED


class HumanApprovalRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "plan.json"
        path.write_text(json.dumps(suite.plan()))
        self.env = mock.patch.dict(os.environ, {"SDKREG_PLAN": str(path)})
        self.env.start()
        spec_ = importlib.util.spec_from_file_location("interop_orchestrator_human", HERE / "orchestrator.py")
        self.orchestrator = importlib.util.module_from_spec(spec_)
        spec_.loader.exec_module(self.orchestrator)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_key_approvals_are_observed_as_refused_and_only_a_person_approves(self):
        emitted = {}
        orchestrator = self.orchestrator

        class Cli:
            def run(self, args, env):
                failed = orchestrator.HumanSessionRequired if "approver" in args else orchestrator.CommandFailed
                raise failed(1, 403)

        def recorded(scenario, name, **payload):
            emitted[name] = payload
        refusals = orchestrator.Refusals("interop-proposal-approval", lambda: {"status": "AwaitingApproval"},
                                         {"proposal": "proposal-1"})
        with mock.patch.object(orchestrator, "emit", side_effect=recorded):
            for profile in ("proposer", "approver"):
                refusals.expect(profile, lambda: Cli().run([profile], {}))
        self.assertEqual(emitted["approver"]["observed"], {"status": "AwaitingApproval", "humanSessionRequired": True})
        self.assertFalse(emitted["proposer"]["observed"]["humanSessionRequired"])
        self.assertEqual(emitted["approver"]["error"]["status"], 403)

    def test_person_approval_runs_the_helper_with_the_session_endpoint(self):
        orchestrator, state = self.orchestrator, {"proposal": "proposal-1"}
        with self.assertRaises(orchestrator.Unsupported), mock.patch.dict(os.environ, {"SDKREG_ADMIN_PASSWORD": ""}):
            orchestrator.approve_as_person(state)
        orchestrator.PLAN["humanApproval"] = {"baseUrl": "https://candidate:8444", "cafile": "/ca.pem"}
        reply = dict(HUMAN_APPROVED, password="never-echoed")
        with mock.patch.dict(os.environ, {"SDKREG_ADMIN_PASSWORD": "pw"}), \
                mock.patch.object(orchestrator.human_approval, "approve_in_child", return_value=reply) as child:
            observed = orchestrator.approve_as_person(state)
        child.assert_called_once_with("https://candidate:8444", "proposal-1", "pw", "/ca.pem")
        self.assertTrue(state["approved"])
        self.assertEqual({k: v for k, v in observed.items() if v is not None}, HUMAN_APPROVED)


if __name__ == "__main__":
    unittest.main()
