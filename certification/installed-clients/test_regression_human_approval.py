"""Self-tests for the human approval of governed proposals in the CLI cells: the server refuses every API
key approval, and a person approves in a Console password session (e2e/harness/lib/human_approval.py)."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import oracles  # noqa: E402
import test_regression as suite  # noqa: E402

load_driver = suite.load_driver


class HumanApprovalTests(unittest.TestCase):
    def test_key_approval_must_be_the_human_session_refusal(self):
        ok = lambda observation: oracles.oracle_key_approval_refused(observation)[0]  # noqa: E731
        refused = {"error": {"status": 403}, "observed": {"status": "AwaitingApproval", "humanSessionRequired": True}}
        self.assertTrue(ok(refused))
        # A 403 for another reason (say a missing permission) is not the server's human-approval refusal.
        self.assertFalse(ok({**refused, "observed": {"status": "AwaitingApproval", "humanSessionRequired": False}}))
        self.assertFalse(ok({**refused, "observed": {"status": "Succeeded", "humanSessionRequired": True}}))
        self.assertFalse(ok({"observed": {"status": "Succeeded", "humanSessionRequired": False}}))

    def test_approval_must_come_from_a_console_password_session(self):
        ok = lambda observed: oracles.oracle_human_approved(observed)[0]  # noqa: E731
        self.assertTrue(ok({"result": "approved", "status": "Succeeded", "approvalChannel": "console-interactive-password"}))
        self.assertFalse(ok({"result": "approved", "status": "Succeeded", "approvalChannel": None}))
        self.assertFalse(ok({"result": "refused", "stage": "session", "httpStatus": 401}))

    def test_cli_driver_reports_the_key_refusal_and_approves_as_a_person(self):
        refusal_text = ("Admin operation approveOperationProposal failed with HTTP 403. Human approval requires a live "
                        "interactive browser session and a valid review capability. Open the proposal in Console.")
        with tempfile.TemporaryDirectory() as tmp:
            honua, plan = Path(tmp) / "honua", Path(tmp) / "plan.json"
            honua.write_text(f"#!{sys.executable}\nimport json, sys\nif 'approveOperationProposal' in sys.argv:\n"
                             f"    print(json.dumps({{'error': {refusal_text!r}}})); sys.exit(1)\n"
                             "print(json.dumps({'status': 'AwaitingApproval'}))\n")
            honua.chmod(0o700)
            plan.write_text(json.dumps({"baseUrl": "http://localhost:8080",
                                        "humanApproval": {"baseUrl": "https://localhost:8444", "cafile": "/tls.crt"}}))
            driver = load_driver("cli", plan)
            cli, state = driver.Cli("npm", str(honua), None, Path(tmp)), {"proposal": "proposal-1"}
            attempt = lambda: cli.run(["admin", "operate", "approveOperationProposal"], {})  # noqa: E731
            approved = {"result": "approved", "status": "Succeeded", "approvalChannel": "console-interactive-password"}
            with mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                    mock.patch.object(driver.human_approval, "approve_in_child", return_value=approved) as child, \
                    mock.patch.dict(os.environ, {"SDKREG_ADMIN_PASSWORD": "admin-password"}):
                driver.Refusals("npm", lambda: cli.json(["get"], {})["status"], state).expect("key-approval-refused", attempt)
                driver.step("npm", "approve", lambda: driver.approve_as_person(state), state, ("proposal",))
        refused, approve = (json.loads(line) for line in out.getvalue().splitlines())
        self.assertEqual(refused["error"]["status"], 403)
        self.assertEqual(refused["observed"], {"status": "AwaitingApproval", "humanSessionRequired": True})
        self.assertEqual(approve["observed"]["approvalChannel"], "console-interactive-password")
        child.assert_called_once_with("https://localhost:8444", "proposal-1", "admin-password", "/tls.crt")
        self.assertTrue(state["approved"])


if __name__ == "__main__":
    unittest.main()
