#!/usr/bin/env python3
"""lib/human_approval.py approves the way a person does, against a fake TLS candidate."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE / "lib")]
import fake_candidate  # noqa: E402
import human_approval  # noqa: E402

HELPER = HERE / "lib" / "human_approval.py"


def run_helper(fake: fake_candidate.FakeCandidate, proposal: str, password: str = fake_candidate.PASSWORD,
               base: str | None = None) -> tuple[int, dict, str]:
    env = {"PATH": os.environ.get("PATH", ""), "HONUA_ADMIN_PASSWORD": password}
    argv = [sys.executable, str(HELPER), "--base", base or fake.https_base, "--proposal", proposal,
            "--cafile", f"{fake.tls_dir}/tls.crt"]
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
    return proc.returncode, json.loads(proc.stdout), proc.stdout + proc.stderr


def run_helper_with_tenant(fake: fake_candidate.FakeCandidate, proposal: str,
                           tenant: str) -> tuple[int, dict, str]:
    env = {"PATH": os.environ.get("PATH", ""), "HONUA_ADMIN_PASSWORD": fake_candidate.PASSWORD}
    argv = [sys.executable, str(HELPER), "--base", fake.https_base, "--proposal", proposal,
            "--cafile", f"{fake.tls_dir}/tls.crt", "--tenant", tenant]
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
    return proc.returncode, json.loads(proc.stdout), proc.stdout + proc.stderr


class HumanApprovalTest(unittest.TestCase):
    def test_session_approval_with_review_capability(self) -> None:
        with fake_candidate.FakeCandidate() as fake:
            proposal, _ = fake.state.new_proposal()
            code, result, output = run_helper(fake, proposal)
            sent = [request for request in fake.state.requests if request["tls"]]
        self.assertEqual(code, 0, output)
        self.assertEqual(result["result"], "approved")
        self.assertEqual(result["approvalChannel"], "console-interactive-password")
        self.assertEqual([(r["method"], r["path"].rsplit("/", 1)[-1]) for r in sent],
                         [("GET", "csrf"), ("POST", "session"), ("GET", "csrf"), ("GET", "approval-review"),
                          ("POST", "approve")])
        self.assertFalse([r for r in sent if {"X-API-Key", "Authorization"} & set(r["headers"])])
        self.assertNotIn(fake_candidate.PASSWORD, output)

    def test_refused_sign_in_is_a_typed_refusal(self) -> None:
        with fake_candidate.FakeCandidate() as fake:
            proposal, _ = fake.state.new_proposal()
            code, result, output = run_helper(fake, proposal, password="wrong")
        self.assertEqual(code, 1)
        self.assertEqual((result["result"], result["stage"], result["httpStatus"]), ("refused", "session", 401))
        self.assertNotIn("wrong", output)

    def test_a_server_that_echoes_the_password_does_not_leak_it(self) -> None:
        with fake_candidate.FakeCandidate() as fake:
            fake.state.echo_refused_password = True
            proposal, _ = fake.state.new_proposal()
            code, result, output = run_helper(fake, proposal, password="wrong-but-secret")
        self.assertEqual((code, result["stage"], result["httpStatus"]), (1, "session", 401))
        self.assertNotIn("wrong-but-secret", output)

    def test_unknown_proposal_is_refused_at_review(self) -> None:
        with fake_candidate.FakeCandidate() as fake:
            code, result, _ = run_helper(fake, "proposal-missing")
        self.assertEqual(code, 1)
        self.assertEqual((result["stage"], result["httpStatus"]), ("review", 404))

    def test_plain_http_base_is_an_error_not_a_request(self) -> None:
        with fake_candidate.FakeCandidate() as fake:
            proposal, _ = fake.state.new_proposal()
            code, result, _ = run_helper(fake, proposal, base=fake.http_base)
            sent = list(fake.state.requests)
        self.assertEqual(code, 2)
        self.assertEqual(result["result"], "error")
        self.assertEqual(sent, [])

    def test_missing_password_is_an_error(self) -> None:
        with fake_candidate.FakeCandidate() as fake:
            proposal, _ = fake.state.new_proposal()
            code, result, _ = run_helper(fake, proposal, password="")
        self.assertEqual((code, result["result"]), (2, "error"))

    def test_human_refusal_is_recognised_exactly(self) -> None:
        self.assertTrue(human_approval.is_human_approval_refusal(403, fake_candidate.HUMAN_REFUSAL))
        self.assertFalse(human_approval.is_human_approval_refusal(403, "The caller lacks admin:approve."))
        self.assertFalse(human_approval.is_human_approval_refusal(401, fake_candidate.HUMAN_REFUSAL))

    def test_tenant_flag_sends_tenant_header_on_review_and_approve(self) -> None:
        with fake_candidate.FakeCandidate() as fake:
            proposal, _ = fake.state.new_proposal()
            code, result, output = run_helper_with_tenant(fake, proposal, "public")
            sent = [request for request in fake.state.requests if request["tls"]]
        self.assertEqual(code, 0, output)
        self.assertEqual(result["result"], "approved")
        by_stage = {r["path"].rsplit("/", 1)[-1]: r["headers"].get("X-Honua-Tenant") for r in sent}
        self.assertIsNone(by_stage["csrf"])
        self.assertIsNone(by_stage["session"])
        self.assertEqual(by_stage["approval-review"], "public")
        self.assertEqual(by_stage["approve"], "public")

    def test_malformed_tenant_is_rejected_without_sending_requests(self) -> None:
        with fake_candidate.FakeCandidate() as fake:
            proposal, _ = fake.state.new_proposal()
            code, result, _ = run_helper_with_tenant(fake, proposal, "bad tenant")
            sent = [r for r in fake.state.requests if r["path"].endswith(("approval-review", "approve"))]
        self.assertEqual((code, result["result"]), (2, "error"))
        self.assertEqual(sent, [])

    def test_approve_in_child_forwards_approval_options_tenant(self) -> None:
        with fake_candidate.FakeCandidate() as fake:
            proposal, _ = fake.state.new_proposal()
            options = human_approval.ApprovalOptions(cafile=f"{fake.tls_dir}/tls.crt", tenant="public")
            result = human_approval.approve_in_child(fake.https_base, proposal, fake_candidate.PASSWORD, options)
            sent = [request for request in fake.state.requests if request["tls"]]
        self.assertEqual(result["result"], "approved")
        by_stage = {r["path"].rsplit("/", 1)[-1]: r["headers"].get("X-Honua-Tenant") for r in sent}
        self.assertEqual(by_stage["approval-review"], "public")
        self.assertEqual(by_stage["approve"], "public")


if __name__ == "__main__":
    unittest.main()
