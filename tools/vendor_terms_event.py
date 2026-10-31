#!/usr/bin/env python3
"""Inspect and safely remove avoidable terminology from GitHub metadata.

GitHub delivers these events after the content is visible. Simple prose
sentences can be removed while preserving the rest; titles and ambiguous
formatting require a manual edit. Only the specific triggering comment object
may be deleted, never its containing issue or pull request.
"""

import argparse
import json
import re
import subprocess
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
    if event_name in ("issues", "pull_request", "pull_request_target"):
        key = "issue" if event_name == "issues" else "pull_request"
        item = payload.get(key)
        if not isinstance(item, dict) or not isinstance(item.get("title"), str):
            raise ValueError("Invalid issue or pull request")
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
    if body is None and event_name in ("issues", "pull_request", "pull_request_target", "pull_request_review"):
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


_UNSAFE_LINE = re.compile(r"https?://|www\.|[`<>]|\[[^]]+\]\(|^\s*(?:#{1,6}\s|>|[-*+]\s|\d+\.\s)", re.I)
_SENTENCE_END = re.compile(r"(?<=[.!?])(?:[\"'’”\)\]]*)(?=\s|$)")
_ABBREVIATION = re.compile(r"(?i)\b(?:e\.g|i\.e|etc|vs|mr|mrs|dr|prof|fig|no|ver|v)\.$")


def remove_offending_sentences(body: str, hits: list) -> str | None:
    """Return an edited body only when raw character offsets isolate sentences safely."""
    if not hits:
        return body
    lines = body.splitlines(keepends=True)
    if not lines and body == "":
        return None
    by_line: dict[int, list] = {}
    for hit in hits:
        if hit.path != "pull-request-body.md" or hit.line < 1 or hit.line > len(lines):
            return None
        by_line.setdefault(hit.line, []).append(hit)

    output = list(lines)
    for line_number, line_hits in by_line.items():
        index = line_number - 1
        raw = lines[index]
        content = raw.rstrip("\r\n")
        if not content.strip() or _UNSAFE_LINE.search(content):
            return None
        if index and lines[index - 1].strip() or index + 1 < len(lines) and lines[index + 1].strip():
            return None
        if any(
            hit.position < 0
            or content[hit.position:hit.position + len(hit.token)].casefold() != hit.token.casefold()
            for hit in line_hits
        ):
            return None

        boundaries = [match.end() for match in _SENTENCE_END.finditer(content)]
        for boundary in boundaries:
            preceding = content[:boundary].rstrip()
            if _ABBREVIATION.search(preceding):
                return None
            following = content[boundary:].lstrip()
            if following and following[0].islower():
                return None
        starts = [0, *boundaries]
        ends = [*boundaries, len(content)]
        remove: set[int] = set()
        for hit in line_hits:
            span_index = next(
                (position for position, (start, end) in enumerate(zip(starts, ends, strict=True))
                 if start <= hit.position < end),
                None,
            )
            if span_index is None:
                return None
            remove.add(span_index)
        remaining = "".join(
            content[start:end]
            for position, (start, end) in enumerate(zip(starts, ends, strict=True))
            if position not in remove
        )
        remaining = re.sub(r"[ \t]{2,}", " ", remaining).strip()
        ending = raw[len(content):]
        output[index] = remaining + ending

    return "".join(output)


def _repo_path(repository: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid repository")
    return "repos/" + repository


def _event_number(payload: dict, key: str) -> int:
    item = payload.get(key)
    number = item.get("number") if isinstance(item, dict) else None
    if type(number) is not int or number < 1:
        raise ValueError("Invalid event number")
    return number


def remediation_target(event_name: str, payload: dict, repository: str) -> tuple[str, str]:
    base = _repo_path(repository)
    if event_name == "issues":
        return "PATCH", f"{base}/issues/{_event_number(payload, 'issue')}"
    if event_name in ("pull_request", "pull_request_target"):
        return "PATCH", f"{base}/issues/{_event_number(payload, 'pull_request')}"
    if event_name == "pull_request_review":
        pull_number = _event_number(payload, "pull_request")
        review = payload.get("review")
        review_id = review.get("id") if isinstance(review, dict) else None
        if type(review_id) is not int or review_id < 1:
            raise ValueError("Invalid review ID")
        return "PUT", f"{base}/pulls/{pull_number}/reviews/{review_id}"
    if event_name in ("issue_comment", "pull_request_review_comment", "commit_comment"):
        comment = payload.get("comment")
        comment_id = comment.get("id") if isinstance(comment, dict) else None
        if type(comment_id) is not int or comment_id < 1:
            raise ValueError("Invalid comment ID")
        resource = {
            "issue_comment": "issues/comments",
            "pull_request_review_comment": "pulls/comments",
            "commit_comment": "comments",
        }[event_name]
        return "PATCH", f"{base}/{resource}/{comment_id}"
    raise ValueError("Unsupported remediation event")


def apply_body_update(method: str, endpoint: str, body: str) -> None:
    request = subprocess.run(
        ["gh", "api", "--method", method, endpoint, "--input", "-"],
        input=json.dumps({"body": body}),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if request.returncode != 0:
        raise RuntimeError("Content update failed")


def delete_comment(event_name: str, payload: dict, repository: str) -> None:
    base = _repo_path(repository)
    comment = payload.get("comment")
    comment_id = comment.get("id") if isinstance(comment, dict) else None
    if type(comment_id) is not int or comment_id < 1:
        raise ValueError("Invalid comment ID")
    resource = {
        "issue_comment": "issues/comments",
        "pull_request_review_comment": "pulls/comments",
        "commit_comment": "comments",
    }.get(event_name)
    if resource is None:
        raise ValueError("Unsupported comment event")
    request = subprocess.run(
        ["gh", "api", "--method", "DELETE", f"{base}/{resource}/{comment_id}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if request.returncode != 0:
        raise RuntimeError("Comment removal failed")


def remediate_event(event_name: str, payload: dict, repository: str, repo_name: str) -> bool:
    title, body = event_texts(event_name, payload)
    findings = find_event_hits(repo_name, title, body)
    if not findings:
        return False
    if any(hit.path == "pull-request-title.md" for hit in findings):
        raise RuntimeError("Title findings require manual correction")
    body_hits = [hit for hit in findings if hit.path == "pull-request-body.md"]
    updated_body = remove_offending_sentences(body, body_hits)
    if updated_body is None or updated_body == body:
        raise RuntimeError("The offending sentence could not be isolated safely")

    is_comment = event_name in ("issue_comment", "pull_request_review_comment", "commit_comment")
    is_issue_or_pr_body = event_name in ("issues", "pull_request", "pull_request_target")
    if is_issue_or_pr_body and not updated_body.strip():
        raise RuntimeError("Emptying an entire issue or pull request body requires manual correction")
    if is_comment and not updated_body.strip():
        delete_comment(event_name, payload, repository)
    else:
        method, endpoint = remediation_target(event_name, payload, repository)
        apply_body_update(method, endpoint, updated_body)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="repository name used by the classifier")
    parser.add_argument("--repository", help="owner/name for a remedial GitHub API call")
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--event", type=Path, required=True)
    parser.add_argument("--remediate", action="store_true")
    args = parser.parse_args()
    try:
        if args.event.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Event too large")
        payload = json.loads(args.event.read_text(encoding="utf-8"))
        if args.remediate:
            if not args.repository:
                raise ValueError("Repository required for remediation")
            changed = remediate_event(args.event_name, payload, args.repository, args.repo)
            if changed:
                print("vendor event guard removed the offending sentence")
            else:
                print("vendor event guard found no actionable terminology")
            return 0
        title, body = event_texts(args.event_name, payload)
        findings = find_event_hits(args.repo, title, body)
    except (OSError, ValueError, RuntimeError, AssertionError, subprocess.TimeoutExpired):
        print("vendor event guard requires a manual content correction", file=sys.stderr)
        return 2
    if findings:
        print("vendor event guard found terminology requiring a manual correction", file=sys.stderr)
        return 1
    print("vendor event guard found no actionable terminology")
    return 0


if __name__ == "__main__":
    sys.exit(main())
