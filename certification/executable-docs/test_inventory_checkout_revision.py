"""A default-branch document that runs from a checkout is pinned to the last commit under its cwd."""
from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from inventory import Resolver  # noqa: E402

SAMPLES = "honua-io/honua-samples"


def _resolver(commits: dict[str, str], seen: list[dict[str, list[str]]]) -> Resolver:
    def fetch(url, accept=None, token=None):
        if url == f"https://api.github.com/repos/{SAMPLES}":
            return b'{"default_branch": "trunk"}'
        prefix = f"https://api.github.com/repos/{SAMPLES}/commits?"
        assert url.startswith(prefix), url
        query = urllib.parse.parse_qs(url[len(prefix):])
        seen.append(query)
        return json.dumps([{"sha": commits[query.get("path", [""])[0]]}]).encode()

    return Resolver({}, fetch=fetch)


def test_repository_root_checkout_uses_the_branch_tip():
    # The samples README installs from the root lockfiles, which change without the README.
    seen: list[dict[str, list[str]]] = []
    doc = {"repo": SAMPLES, "path": "README.md", "revision": {"defaultBranch": True}, "checkout": {"cwd": ""}}
    assert _resolver({"": "a" * 40, "README.md": "b" * 40}, seen).revision(doc) == "a" * 40
    assert seen == [{"sha": ["trunk"], "per_page": ["1"]}]


def test_sample_checkout_uses_the_last_commit_under_its_cwd():
    seen: list[dict[str, list[str]]] = []
    doc = {"repo": SAMPLES, "path": "samples/odata-query-rest/README.md", "revision": {"defaultBranch": True},
           "checkout": {"cwd": "samples/odata-query-rest"}}
    commits = {"samples/odata-query-rest": "c" * 40, "samples/odata-query-rest/README.md": "d" * 40}
    assert _resolver(commits, seen).revision(doc) == "c" * 40
    assert [query["path"] for query in seen] == [["samples/odata-query-rest"]]


def test_document_without_checkout_keeps_the_document_commit():
    seen: list[dict[str, list[str]]] = []
    doc = {"repo": SAMPLES, "path": "docs/guide.md", "revision": {"defaultBranch": True}}
    assert _resolver({"docs/guide.md": "e" * 40}, seen).revision(doc) == "e" * 40
    assert [query["path"] for query in seen] == [["docs/guide.md"]]
