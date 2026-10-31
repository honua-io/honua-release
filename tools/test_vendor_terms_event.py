"""Tests for post-publication issue, review, and comment inspection."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import vendor_terms_event as guard
import yaml


class EventTests(unittest.TestCase):
    def test_extracts_each_supported_event(self):
        cases = {
            "issues": ({"issue": {"title": "Title", "body": "Body"}}, ("Title", "Body")),
            "issue_comment": ({"comment": {"body": "Body"}}, ("", "Body")),
            "pull_request_review": ({"review": {"body": "Body"}}, ("", "Body")),
            "pull_request_review_comment": ({"comment": {"body": "Body"}}, ("", "Body")),
            "commit_comment": ({"comment": {"body": "Body"}}, ("", "Body")),
        }
        for name, (payload, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(guard.event_texts(name, payload), expected)

    def test_missing_optional_bodies_are_empty(self):
        self.assertEqual(guard.event_texts("issues", {"issue": {"title": "Title", "body": None}}), ("Title", ""))
        self.assertEqual(guard.event_texts("pull_request_review", {"review": {"body": None}}), ("", ""))

    def test_invalid_or_unsupported_events_fail_closed(self):
        for name, payload in [("issues", {}), ("issue_comment", {"comment": {"body": 1}}), ("other", {})]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                guard.event_texts(name, payload)

    def test_cli_flags_text_without_echoing_it(self):
        mark = "Es" + "ri"
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            event.write_text(json.dumps({"comment": {"body": f"{mark} client private-sample-123"}}), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(Path(guard.__file__)), "--repo", "fixture", "--event-name", "issue_comment",
                 "--event", str(event)], capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(mark, result.stdout + result.stderr)
            self.assertNotIn("private-sample-123", result.stdout + result.stderr)

    def test_cli_checks_issue_title_and_accepts_neutral_text(self):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            command = [sys.executable, str(Path(guard.__file__)), "--repo", "fixture", "--event-name", "issues",
                       "--event", str(event)]
            event.write_text(json.dumps({"issue": {"title": "GeoServices REST", "body": "Compatible services"}}),
                             encoding="utf-8")
            self.assertEqual(subprocess.run(command, capture_output=True, check=False).returncode, 0)
            event.write_text(json.dumps({"issue": {"title": "Es" + "ri client", "body": None}}), encoding="utf-8")
            self.assertEqual(subprocess.run(command, capture_output=True, check=False).returncode, 1)

    def test_cli_checks_text_split_by_html(self):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            command = [sys.executable, str(Path(guard.__file__)), "--repo", "fixture", "--event-name", "issue_comment",
                       "--event", str(event)]
            for text in ("Arc<!-- -->" + "GIS", "<em>Arc</em>" + "GIS", "Arc**" + "GIS**",
                         "Arc[" + "GIS](https://example.com)", "Arc`" + "GIS`"):
                with self.subTest(text=text):
                    event.write_text(json.dumps({"comment": {"body": text}}), encoding="utf-8")
                    result = subprocess.run(command, capture_output=True, text=True, check=False)
                    self.assertEqual(result.returncode, 1)
                    self.assertNotIn(text, result.stdout + result.stderr)

    def test_html_blocks_do_not_join_separate_paragraphs(self):
        self.assertEqual(guard.visible_html_text("Arc<p>" + "GIS</p>"), "Arc\nGIS\n")

    def test_cli_rejects_oversized_event_without_echoing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            event.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
            result = subprocess.run(
                [sys.executable, str(Path(guard.__file__)), "--repo", "fixture", "--event-name", "issues",
                 "--event", str(event)], capture_output=True, text=True, check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertNotIn(str(event), result.stderr)

    def test_workflow_is_read_only_and_covers_the_supported_events(self):
        workflow = Path(guard.__file__).resolve().parent.parent / ".github/workflows/guard-vendor-event.yml"
        config = yaml.load(workflow.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertEqual(config["permissions"], {"contents": "read"})
        self.assertEqual(
            set(config["on"]),
            {"issues", "issue_comment", "pull_request_review", "pull_request_review_comment", "commit_comment",
             "workflow_call"},
        )
        steps = config["jobs"]["vendor-terms-event"]["steps"]
        self.assertEqual(steps[0]["with"]["persist-credentials"], "false")
        self.assertNotIn("checkout", steps[1]["run"])


if __name__ == "__main__":
    unittest.main()
