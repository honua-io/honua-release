#!/usr/bin/env python3
"""Verify that required evidence producers are pinned to trusted branch workflow revisions."""
from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


class VerificationError(ValueError):
    """An evidence producer is not bound to its declared trust metadata."""


class GitHubClient:
    def __init__(self, token: str):
        if not token:
            raise VerificationError("GITHUB_TOKEN is required to verify evidence producer pins")
        self.token = token

    def json(self, path: str) -> dict:
        url = f"https://api.github.com/{path.lstrip('/')}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "User-Agent": "honua-release-producer-verifier",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                value = json.loads(response.read())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise VerificationError(f"GitHub API request failed for {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise VerificationError(f"GitHub API returned a non-object response for {path}")
        return value


def _workflow_triggers(workflow_bytes: bytes, path: str) -> dict[str, object]:
    try:
        workflow = yaml.load(workflow_bytes.decode("utf-8"), Loader=yaml.BaseLoader) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise VerificationError(f"{path} is not valid UTF-8 YAML") from exc
    triggers = workflow.get("on")
    if isinstance(triggers, str):
        return {triggers: None}
    if isinstance(triggers, list):
        return {str(value): None for value in triggers}
    if isinstance(triggers, dict):
        return {str(event): config for event, config in triggers.items()}
    return {}


def _workflow_events(workflow_bytes: bytes, path: str) -> set[str]:
    return set(_workflow_triggers(workflow_bytes, path))


_BRANCH_FILTER_EVENTS = {"push", "pull_request", "pull_request_target", "workflow_run"}


def _patterns(value: object, *, event: str, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    raise VerificationError(f"workflow event {event!r} has invalid {field!r} branch filters")


def _event_allows_branch(event: str, config: object, branch: str) -> bool:
    """Apply GitHub's ordered include/negation semantics to branch-aware event filters."""
    if event not in _BRANCH_FILTER_EVENTS or config is None:
        return True
    if not isinstance(config, dict):
        raise VerificationError(f"workflow event {event!r} has invalid configuration")
    included = _patterns(config.get("branches"), event=event, field="branches")
    ignored = _patterns(config.get("branches-ignore"), event=event, field="branches-ignore")
    if included and ignored:
        raise VerificationError(f"workflow event {event!r} declares both branches and branches-ignore")
    if included:
        allowed = False
        for pattern in included:
            negated = pattern.startswith("!")
            candidate = pattern[1:] if negated else pattern
            if candidate and fnmatch.fnmatchcase(branch, candidate):
                allowed = not negated
        return allowed
    return not any(fnmatch.fnmatchcase(branch, pattern) for pattern in ignored)


def verify_source(name: str, source: dict, client: GitHubClient) -> str:
    repository = str(source.get("repository", ""))
    producer_sha = str(source.get("producerSha", ""))
    trusted_branch = str(source.get("trustedBranch", ""))
    workflow_path = str(source.get("workflowPath", ""))
    artifact_identity = str(source.get("artifactIdentity", ""))
    trusted_events = {str(value) for value in source.get("trustedEvents") or []}

    commit = client.json(f"repos/{repository}/commits/{urllib.parse.quote(producer_sha, safe='')}")
    if commit.get("sha") != producer_sha:
        raise VerificationError(f"{name}: producer commit did not resolve exactly to {producer_sha}")

    comparison = client.json(
        f"repos/{repository}/compare/{urllib.parse.quote(producer_sha, safe='')}..."
        f"{urllib.parse.quote(trusted_branch, safe='')}"
    )
    if comparison.get("status") not in {"ahead", "identical"}:
        raise VerificationError(f"{name}: producer SHA is not on trusted branch {trusted_branch!r}")

    content = client.json(
        f"repos/{repository}/contents/{urllib.parse.quote(workflow_path, safe='/')}"
        f"?ref={urllib.parse.quote(producer_sha, safe='')}"
    )
    if content.get("encoding") != "base64" or not content.get("content"):
        raise VerificationError(f"{name}: workflow {workflow_path!r} is missing at the producer SHA")
    try:
        workflow_bytes = base64.b64decode(str(content["content"]), validate=False)
    except (ValueError, TypeError) as exc:
        raise VerificationError(f"{name}: workflow response contains invalid base64") from exc
    triggers = _workflow_triggers(workflow_bytes, workflow_path)
    actual_events = set(triggers)
    missing_events = trusted_events - actual_events
    if missing_events:
        raise VerificationError(
            f"{name}: workflow {workflow_path!r} does not declare trusted event(s) {sorted(missing_events)}"
        )
    filtered_events = sorted(
        event for event in trusted_events if not _event_allows_branch(event, triggers[event], trusted_branch)
    )
    if filtered_events:
        raise VerificationError(
            f"{name}: workflow {workflow_path!r} trusted event(s) {filtered_events} "
            f"cannot run for trusted branch {trusted_branch!r}"
        )
    if not artifact_identity:
        raise VerificationError(f"{name}: artifactIdentity is required")
    return f"{repository}@{producer_sha}:{workflow_path}:{artifact_identity}"


def verify_manifest(manifest: dict, client: GitHubClient) -> list[str]:
    sources = manifest.get("evidenceSources") or {}
    if not isinstance(sources, dict) or not sources:
        raise VerificationError("evidenceSources must be a non-empty mapping")
    verified = []
    for name, source in sorted(sources.items()):
        if not isinstance(source, dict):
            raise VerificationError(f"{name}: evidence source must be a mapping")
        if source.get("required", True) is False:
            continue
        verified.append(verify_source(name, source, client))
    return verified


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(REPO_ROOT / "platform-manifest.yaml"))
    parser.add_argument("--github-token-env", default="GITHUB_TOKEN")
    args = parser.parse_args(argv)
    manifest = yaml.safe_load(Path(args.manifest).read_text(encoding="utf-8")) or {}
    try:
        client = GitHubClient(os.environ.get(args.github_token_env, ""))
        verified = verify_manifest(manifest, client)
    except VerificationError as exc:
        print(f"ERROR {exc}")
        return 1
    for identity in verified:
        print(f"OK    {identity}")
    print(f"OK    verified {len(verified)} required trusted evidence producer(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
