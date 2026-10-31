"""Tests for read-only GitHub discussion inventory."""

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import vendor_terms_github_audit as audit
import yaml


class FakeAPI:
    def __init__(self, pages):
        self.data = pages
        self.calls = []

    def pages(self, endpoint):
        self.calls.append(endpoint)
        result = self.data.get(endpoint, [])
        if isinstance(result, Exception):
            raise result
        yield result


class GitHubAuditTests(unittest.TestCase):
    def test_repository_names_cannot_escape_the_expected_path(self):
        self.assertTrue(audit.valid_name(".github"))
        self.assertFalse(audit.valid_name(".."))
        self.assertFalse(audit.valid_name("a/b"))

    def test_scheduled_workflow_is_read_only_and_scoped_to_its_repository(self):
        root = Path(audit.__file__).resolve().parent.parent
        workflow = root / ".github/workflows/audit-vendor-github.yml"
        config = yaml.load(workflow.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
        self.assertEqual(config["permissions"], {"contents": "read", "issues": "read", "pull-requests": "read"})
        self.assertEqual(set(config["on"]), {"schedule", "workflow_dispatch", "workflow_call"})
        self.assertIn("review_shard", config["on"]["workflow_dispatch"]["inputs"])
        self.assertIn("review_shard", config["on"]["workflow_call"]["inputs"])
        steps = config["jobs"]["audit"]["steps"]
        self.assertEqual(steps[0]["with"]["persist-credentials"], "false")
        self.assertEqual(steps[1]["env"]["REPOSITORY"], "${{ github.repository }}")
        self.assertIn('--repo "$REPOSITORY"', steps[1]["run"])

    def test_traverses_every_surface_and_each_pull_request_review(self):
        base = "repos/example/project"
        pr_url = "https://github.com/example/project/pull/3"
        comment_url = "https://github.com/example/project/pull/3#discussion_r7"
        data = {
            f"{base}/issues?state=all&per_page=100": [
                {"html_url": pr_url, "pull_request": {}},
                {"html_url": "https://github.com/example/project/issues/2", "title": "Safe", "body": None},
            ],
            f"{base}/pulls?state=all&per_page=100": [{"html_url": pr_url, "title": "Safe", "body": "", "number": 3}],
            f"{base}/pulls/comments?per_page=100": [{"html_url": comment_url, "body": "Es" + "ri client"}],
            f"{base}/pulls/3/reviews?per_page=100": [{"html_url": pr_url + "#review-8", "body": "Safe"}],
        }
        api = FakeAPI(data)
        scanner = audit.Audit(api)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            scanner.repository("example/project")
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(scanner.items, 4)
        self.assertEqual(scanner.findings, 1)
        self.assertEqual(scanner.failures, 0)
        self.assertEqual([r["url"] for r in records if r["type"] == "finding"], [comment_url])
        self.assertNotIn("Es" + "ri client", output.getvalue())
        self.assertIn(f"{base}/issues/comments?per_page=100", api.calls)
        self.assertIn(f"{base}/comments?per_page=100", api.calls)
        self.assertIn(f"{base}/pulls/3/reviews?per_page=100", api.calls)

    def test_unavailable_endpoint_is_not_treated_as_empty(self):
        base = "repos/example/project"
        api = FakeAPI({f"{base}/issues?state=all&per_page=100": audit.Incomplete("denied")})
        scanner = audit.Audit(api)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            scanner.repository("example/project")
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(scanner.failures, 1)
        self.assertEqual(records[0]["status"], "incomplete")
        self.assertIn(f"{base}/pulls?state=all&per_page=100", api.calls)

    def test_review_shards_cover_distinct_pull_requests(self):
        base = "repos/example/project"
        pulls = [{"html_url": f"https://github.com/example/project/pull/{n}", "title": "Safe", "body": "", "number": n}
                 for n in (1, 2, 3, 4)]
        api = FakeAPI({f"{base}/pulls?state=all&per_page=100": pulls})
        with contextlib.redirect_stdout(io.StringIO()):
            audit.Audit(api, review_shard=1, review_shards=2).repository("example/project")
        self.assertIn(f"{base}/pulls/1/reviews?per_page=100", api.calls)
        self.assertIn(f"{base}/pulls/3/reviews?per_page=100", api.calls)
        self.assertNotIn(f"{base}/pulls/2/reviews?per_page=100", api.calls)
        self.assertNotIn(f"{base}/pulls/4/reviews?per_page=100", api.calls)

    def test_invalid_item_url_is_incomplete_without_echo(self):
        base = "repos/example/project"
        api = FakeAPI({f"{base}/comments?per_page=100": [{"html_url": "https://evil.invalid/secret", "body": "x"}]})
        scanner = audit.Audit(api)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            scanner.repository("example/project")
        self.assertEqual(scanner.failures, 1)
        self.assertNotIn("evil.invalid", output.getvalue())

    def test_missing_body_is_incomplete_but_explicit_null_is_valid(self):
        scanner = audit.Audit(FakeAPI({}))
        item = {"html_url": "https://github.com/example/project/issues/2", "title": "Safe"}
        with self.assertRaises(audit.Incomplete):
            scanner.item("example/project", "issue", item, title=True)
        item["body"] = None
        scanner.item("example/project", "issue", item, title=True)

    def test_pagination_follows_only_same_endpoint_and_limits_requests(self):
        first = b'HTTP/2 200 OK\r\nLink: <https://api.github.com/repos/a/b/comments?per_page=100&page=2>; rel="next"\r\n\r\n[]'
        second = b'HTTP/2 200 OK\r\n\r\n[]'
        with patch.object(audit, "gh_response", side_effect=[first, second]):
            self.assertEqual(list(audit.GitHub(2).pages("repos/a/b/comments?per_page=100")), [[], []])
        rewritten = first.replace(b"repos/a/b/comments", b"repositories/123/comments")
        with patch.object(audit, "gh_response", side_effect=[rewritten, second]) as fetch:
            self.assertEqual(list(audit.GitHub(2).pages("repos/a/b/comments?per_page=100")), [[], []])
            self.assertEqual(fetch.call_args_list[1].args[0], "https://api.github.com/repos/a/b/comments?per_page=100&page=2")
        bad = first.replace(b"api.github.com/repos/a/b", b"evil.invalid/repos/a/b")
        with patch.object(audit, "gh_response", return_value=bad):
            with self.assertRaises(audit.Incomplete):
                list(audit.GitHub(2).pages("repos/a/b/comments?per_page=100"))
        bad_query = first.replace(b"per_page=100&page=2", b"per_page=100&state=open&page=2")
        with patch.object(audit, "gh_response", return_value=bad_query):
            with self.assertRaises(audit.Incomplete):
                list(audit.GitHub(2).pages("repos/a/b/comments?per_page=100"))
        for query in (b"page=2", b"per_page=100&per_page=100&page=2", b"per_page=100&page=3",
                      b"per_page=100&page=1"):
            with self.subTest(query=query):
                invalid = first.replace(b"per_page=100&page=2", query)
                with patch.object(audit, "gh_response", return_value=invalid):
                    with self.assertRaises(audit.Incomplete):
                        list(audit.GitHub(2).pages("repos/a/b/comments?per_page=100"))
        with patch.object(audit, "gh_response", return_value=first):
            with self.assertRaises(audit.Incomplete):
                list(audit.GitHub(1).pages("repos/a/b/comments?per_page=100"))

    def test_issue_pagination_preserves_a_valid_opaque_cursor(self):
        first = (b'HTTP/2 200 OK\r\nLink: <https://api.github.com/repositories/123/issues?'
                 b'state=all&per_page=100&after=Y3Vy&page=2>; rel="next"\r\n\r\n[]')
        second = b'HTTP/2 200 OK\r\n\r\n[]'
        endpoint = "repos/a/b/issues?state=all&per_page=100"
        with patch.object(audit, "gh_response", side_effect=[first, second]) as fetch:
            self.assertEqual(list(audit.GitHub(2).pages(endpoint)), [[], []])
            self.assertEqual(fetch.call_args_list[1].args[0],
                             "https://api.github.com/repos/a/b/issues?state=all&per_page=100&page=2&after=Y3Vy")
        for replacement in (b"after=bad%20cursor", b"after=one&after=two"):
            with self.subTest(cursor=replacement):
                with patch.object(audit, "gh_response", return_value=first.replace(b"after=Y3Vy", replacement)):
                    with self.assertRaises(audit.Incomplete):
                        list(audit.GitHub(2).pages(endpoint))


if __name__ == "__main__":
    unittest.main()
