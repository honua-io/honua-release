#!/usr/bin/env python3
"""Inspect GitHub issue and comment event text without echoing the payload.

These events arrive after publication. A failure is a detection signal, not a
posting barrier. Repository administrators must install the workflow in each
repository for it to receive events.
"""

import argparse
import json
import sys
from pathlib import Path

import vendor_terms_delta as guard


def visible_html_text(text: str) -> str:
    return guard.visible_html_text(text)


def visible_markdown_text(text: str) -> str:
    return guard.visible_markdown_text(text)


def event_texts(event_name: str, payload: dict) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise ValueError("Invalid event")
    if event_name == "issues":
        item = payload.get("issue")
        if not isinstance(item, dict) or not isinstance(item.get("title"), str):
            raise ValueError("Invalid issue")
        title = item["title"]
    elif event_name in ("issue_comment", "pull_request_review_comment", "commit_comment"):
        item = payload.get("comment")
        title = ""
    elif event_name == "pull_request_review":
        item = payload.get("review")
        title = ""
    else:
        raise ValueError("Unsupported event")
    if not isinstance(item, dict):
        raise ValueError("Missing event item")
    body = item.get("body")
    if body is None and event_name in ("issues", "pull_request_review"):
        body = ""
    if not isinstance(body, str):
        raise ValueError("Invalid event body")
    return title, body


def find_event_hits(repo: str, title: str, body: str) -> list:
    findings = guard.find_pr_metadata_hits(repo, {"pull_request": {"title": title, "body": body}})
    visible_title, visible_body = visible_html_text(title), visible_html_text(body)
    if (visible_title, visible_body) != (title, body):
        findings.extend(guard.find_pr_metadata_hits(
            repo, {"pull_request": {"title": visible_title, "body": visible_body}},
        ))
    markdown_title, markdown_body = visible_markdown_text(visible_title), visible_markdown_text(visible_body)
    if (markdown_title, markdown_body) != (visible_title, visible_body):
        findings.extend(guard.find_pr_metadata_hits(
            repo, {"pull_request": {"title": markdown_title, "body": markdown_body}},
        ))
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--event", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.event.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Event too large")
        payload = json.loads(args.event.read_text(encoding="utf-8"))
        title, body = event_texts(args.event_name, payload)
        findings = find_event_hits(args.repo, title, body)
    except (OSError, ValueError, RuntimeError, AssertionError):
        print("vendor event guard could not inspect the event", file=sys.stderr)
        return 2
    if findings:
        print("vendor event guard found terminology requiring review", file=sys.stderr)
        return 1
    print("vendor event guard found no actionable terminology")
    return 0


if __name__ == "__main__":
    sys.exit(main())
