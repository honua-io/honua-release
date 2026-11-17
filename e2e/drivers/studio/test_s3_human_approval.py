#!/usr/bin/env python3
"""S3's approval lane against a fake candidate: a person approves, and an API key must not."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARNESS = HERE.parents[1] / "harness"
sys.path.insert(0, str(HARNESS))
import fake_candidate  # noqa: E402


def run_s3(fake: fake_candidate.FakeCandidate, https: bool = True) -> dict:
    """Run the S3 driver against the fake and return its scenario row."""
    with tempfile.TemporaryDirectory() as out:
        env = {**os.environ, "E2E_BASE": fake.http_base, "E2E_OUT": out, "E2E_API_KEY": "author-key",
               "E2E_ADMIN_PASSWORD": fake_candidate.PASSWORD, "E2E_REDIS": "on", "E2E_TLS_DIR": fake.tls_dir,
               "E2E_HTTPS_BASE": fake.https_base if https else ""}
        subprocess.run(["bash", str(HERE / "run.sh")], env=env, capture_output=True, text=True, timeout=300, check=True)
        rows = [json.loads(line) for line in Path(out, "scenarios.jsonl").read_text().splitlines()]
    return next(row for row in rows if row["scenario"] == "S3-studio-authoring")


def key_approvals(fake: fake_candidate.FakeCandidate) -> list[dict]:
    return [r for r in fake.state.requests if not r["tls"] and r["path"].endswith("/approve")]


class S3HumanApprovalTest(unittest.TestCase):
    def test_person_approves_after_the_key_is_refused(self) -> None:
        with fake_candidate.FakeCandidate() as fake:
            row = run_s3(fake)
            keyed = key_approvals(fake)
        self.assertEqual(row["status"], "pass", row)
        lane = row["evidence"]["approvalLane"]
        self.assertEqual(lane["keyApproval"], "refused: human session required")
        self.assertEqual(lane["proposalsApproved"], 2)
        self.assertEqual({a["approvalChannel"] for a in lane["humanApprovals"]}, {"console-interactive-password"})
        self.assertEqual({r["headers"].get("X-API-Key") for r in keyed}, {"author-key"})
        self.assertEqual(len(keyed), 2)

    def test_key_approval_that_succeeds_fails_the_scenario(self) -> None:
        with fake_candidate.FakeCandidate(key_approval="accepted") as fake:
            row = run_s3(fake)
        self.assertEqual(row["status"], "fail")
        self.assertIn("not refused with the human-approval 403: HTTP 200", row["evidence"]["families"]["query"]["detail"])

    def test_key_refused_for_another_reason_fails_the_scenario(self) -> None:
        with fake_candidate.FakeCandidate(key_approval="other-403") as fake:
            row = run_s3(fake)
        self.assertEqual(row["status"], "fail")
        self.assertIn("lacks admin:approve", row["evidence"]["families"]["map"]["detail"])

    def test_refused_session_fails_the_scenario(self) -> None:
        with fake_candidate.FakeCandidate(session_works=False) as fake:
            row = run_s3(fake)
        self.assertEqual(row["status"], "fail")
        self.assertIn('"stage": "session"', row["evidence"]["families"]["query"]["detail"])
        self.assertEqual(row["evidence"]["approvalLane"]["proposalsApproved"], 0)

    def test_no_https_endpoint_fails_before_authoring(self) -> None:
        with fake_candidate.FakeCandidate() as fake:
            row = run_s3(fake, https=False)
            drafts = [r for r in fake.state.requests if r["path"] == "/api/v1/studio/package-drafts"]
        self.assertEqual(row["status"], "fail")
        self.assertIn("only over HTTPS", row["why"])
        self.assertEqual(drafts, [])


if __name__ == "__main__":
    unittest.main()
