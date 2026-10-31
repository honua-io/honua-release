#!/usr/bin/env python3
"""Read-only REST inventory of issue, pull request, review, and comment text.

Outputs JSON lines containing item URLs, counts, and endpoint coverage, never
submitted text. An error or request limit marks the result incomplete. REST
pagination is not a snapshot; this does not inspect discussions, releases,
git history, wikis, attachments, deleted content, or repositories hidden from
the authenticated account.
"""

import argparse
import json
import os
import re
import selectors
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import vendor_terms_event as event_guard

NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')
CURSOR = re.compile(r"[A-Za-z0-9_+/=-]{1,1024}\Z")


class Incomplete(Exception):
    pass


def valid_name(value: str) -> bool:
    return value not in (".", "..") and len(value) <= 100 and bool(NAME.fullmatch(value))


def emit(record: dict) -> None:
    print(json.dumps(record, ensure_ascii=True), flush=True)


def gh_response(url: str) -> bytes:
    """Read a bounded response without logging the server body or stderr."""
    limit = 32 * 1024 * 1024
    deadline = time.monotonic() + 60
    with subprocess.Popen(["gh", "api", "--include", url], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        try:
            if process.stdout is None:
                raise Incomplete("Request failed")
            data = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise Incomplete("Request timed out")
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    if len(data) + len(chunk) > limit:
                        raise Incomplete("Response too large")
                    data.extend(chunk)
            if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                raise Incomplete("Request failed")
            return bytes(data)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Incomplete("Request failed") from exc
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


class GitHub:
    def __init__(self, max_requests: int):
        self.max_requests = max_requests
        self.requests = 0

    def pages(self, endpoint: str):
        initial = urlsplit("https://api.github.com/" + endpoint.lstrip("/"))
        expected_path = initial.path
        expected_query = parse_qs(initial.query, keep_blank_values=True)
        url = "https://api.github.com/" + endpoint.lstrip("/")
        seen: set[str] = set()
        while url:
            parsed = urlsplit(url)
            params = parse_qs(parsed.query, keep_blank_values=True)
            page = params.pop("page", None)
            cursor = params.pop("after", None)
            if (parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.path != expected_path
                    or parsed.fragment or params != expected_query
                    or (page is not None and (len(page) != 1 or len(page[0]) > 10
                                              or not re.fullmatch(r"[0-9]+", page[0]) or int(page[0]) < 1))
                    or (cursor is not None and (len(cursor) != 1 or not CURSOR.fullmatch(cursor[0])))
                    or url in seen or self.requests >= self.max_requests):
                raise Incomplete("Invalid pagination or request limit")
            seen.add(url)
            self.requests += 1
            try:
                response = gh_response(url)
            except OSError as exc:
                raise Incomplete("Request failed") from exc
            header, separator, body = response.partition(b"\r\n\r\n")
            if not separator:
                raise Incomplete("Invalid response")
            try:
                items = json.loads(body)
                headers = header.decode("iso-8859-1")
            except (ValueError, UnicodeError) as exc:
                raise Incomplete("Invalid response") from exc
            if not isinstance(items, list):
                raise Incomplete("Invalid page")
            yield items
            links = [line for line in headers.splitlines() if line.lower().startswith("link:")]
            if len(links) > 1:
                raise Incomplete("Ambiguous pagination")
            match = NEXT_LINK.search(links[0]) if links else None
            if links and 'rel="next"' in links[0] and not match:
                raise Incomplete("Invalid pagination")
            if not match:
                url = ""
                continue
            try:
                next_link = urlsplit(match.group(1))
                next_params = parse_qs(next_link.query, keep_blank_values=True)
                next_page = next_params.pop("page", None)
                next_cursor = next_params.pop("after", None)
                repo_path = re.fullmatch(r"/repos/[^/]+/[^/]+(/.+)", expected_path)
                org_path = re.fullmatch(r"/orgs/[^/]+/repos", expected_path)
                alternate = ((repo_path and re.fullmatch(r"/repositories/[0-9]+" + re.escape(repo_path.group(1)), next_link.path))
                             or (org_path and re.fullmatch(r"/organizations/[0-9]+/repos", next_link.path)))
                current_page = int(page[0]) if page else 1
                if (next_link.scheme != "https" or next_link.netloc != "api.github.com" or next_link.fragment
                        or (next_link.path != expected_path and not alternate) or next_params != expected_query
                        or next_page is None or len(next_page) != 1 or len(next_page[0]) > 10
                        or not re.fullmatch(r"[0-9]+", next_page[0]) or int(next_page[0]) != current_page + 1
                        or (next_cursor is not None and (len(next_cursor) != 1 or not CURSOR.fullmatch(next_cursor[0])))
                        or (cursor is not None and next_cursor is None)):
                    raise Incomplete("Invalid next page")
                next_query = dict(expected_query)
                next_query["page"] = next_page
                if next_cursor is not None:
                    next_query["after"] = next_cursor
                url = urlunsplit(("https", "api.github.com", expected_path, urlencode(next_query, doseq=True), ""))
            except ValueError as exc:
                raise Incomplete("Invalid pagination") from exc


class Audit:
    def __init__(self, api: GitHub, review_shard: int = 0, review_shards: int = 1):
        self.api = api
        self.review_shard = review_shard
        self.review_shards = review_shards
        self.findings = 0
        self.failures = 0
        self.items = 0

    def item(self, repo: str, kind: str, item: object, *, title: bool = False) -> None:
        if not isinstance(item, dict) or not isinstance(item.get("html_url"), str):
            raise Incomplete("Invalid item")
        try:
            item_url = urlsplit(item["html_url"])
        except ValueError as exc:
            raise Incomplete("Invalid item URL") from exc
        if item_url.scheme != "https" or item_url.netloc != "github.com" or not item_url.path.startswith(f"/{repo}/"):
            raise Incomplete("Invalid item URL")
        if "body" not in item:
            raise Incomplete("Missing item text")
        text = item["body"]
        if text is None:
            text = ""
        heading = item.get("title") if title else ""
        if not isinstance(text, str) or not isinstance(heading, str):
            raise Incomplete("Invalid item text")
        self.items += 1
        try:
            matches = event_guard.find_event_hits(repo.split("/", 1)[1], heading, text)
        except (OSError, ValueError, RuntimeError, AssertionError) as exc:
            raise Incomplete("Cannot inspect item") from exc
        if matches:
            self.findings += 1
            emit({"type": "finding", "repository": repo, "surface": kind, "url": item["html_url"]})

    def endpoint(self, repo: str, name: str, endpoint: str, callback) -> None:
        count = 0
        try:
            for page in self.api.pages(endpoint):
                for item in page:
                    callback(item)
                    count += 1
        except Incomplete:
            self.failures += 1
            emit({"type": "coverage", "repository": repo, "surface": name, "status": "incomplete", "items": count})
        else:
            emit({"type": "coverage", "repository": repo, "surface": name, "status": "complete", "items": count})

    def repository(self, repo: str) -> None:
        base = f"repos/{repo}"
        prs: list[int] = []

        def issue(item):
            if not isinstance(item, dict):
                raise Incomplete("Invalid issue")
            if "pull_request" not in item:
                self.item(repo, "issue", item, title=True)

        def pull(item):
            if not isinstance(item, dict) or type(item.get("number")) is not int or item["number"] <= 0:
                raise Incomplete("Invalid pull request")
            self.item(repo, "pull_request", item, title=True)
            prs.append(item["number"])

        self.endpoint(repo, "issues", f"{base}/issues?state=all&per_page=100", issue)
        self.endpoint(repo, "pull_requests", f"{base}/pulls?state=all&per_page=100", pull)
        for surface, route in (("conversation_comments", "issues/comments"),
                               ("review_comments", "pulls/comments"), ("commit_comments", "comments")):
            self.endpoint(repo, surface, f"{base}/{route}?per_page=100", lambda item, kind=surface: self.item(repo, kind, item))
        for number in prs:
            if number % self.review_shards != self.review_shard:
                continue
            self.endpoint(repo, f"reviews/{number}", f"{base}/pulls/{number}/reviews?per_page=100",
                          lambda item: self.item(repo, "review", item))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--org")
    scope.add_argument("--repo", help="owner/repository")
    parser.add_argument("--max-requests", type=int, default=1000)
    parser.add_argument("--review-shards", type=int, default=1)
    parser.add_argument("--review-shard", type=int, default=0)
    args = parser.parse_args()
    if args.max_requests < 1:
        parser.error("--max-requests must be positive")
    if args.review_shards < 1 or not 0 <= args.review_shard < args.review_shards:
        parser.error("invalid review shard")
    if args.org and not valid_name(args.org):
        parser.error("invalid organization")
    if args.repo and (len(args.repo.split("/")) != 2 or not all(valid_name(p) for p in args.repo.split("/"))):
        parser.error("invalid repository")
    api = GitHub(args.max_requests)
    audit = Audit(api, args.review_shard, args.review_shards)
    repos: list[str] = []
    if args.repo:
        repos.append(args.repo)
    else:
        try:
            for page in api.pages(f"orgs/{args.org}/repos?type=all&per_page=100"):
                for item in page:
                    name = item.get("full_name") if isinstance(item, dict) else None
                    if (not isinstance(name, str) or len(name.split("/")) != 2
                            or not all(valid_name(p) for p in name.split("/"))
                            or name.split("/", 1)[0].lower() != args.org.lower()):
                        raise Incomplete("Invalid repository inventory")
                    repos.append(name)
        except Incomplete:
            audit.failures += 1
            emit({"type": "inventory", "status": "incomplete"})
    for repo in sorted(set(repos), key=str.lower):
        audit.repository(repo)
    status = ("incomplete" if audit.failures else
              "complete-for-selected-review-shard" if args.review_shards > 1 else "complete-for-visible-rest-surfaces")
    emit({"type": "summary", "status": status,
          "repositories": len(set(repos)), "items": audit.items, "findings": audit.findings,
          "incomplete_endpoints": audit.failures, "requests": api.requests,
          "review_shard": args.review_shard, "review_shards": args.review_shards})
    return 2 if audit.failures else (1 if audit.findings else 0)


if __name__ == "__main__":
    sys.exit(main())
