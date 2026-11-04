"""GitHub REST reads for the build/test gate: a pinned commit's check-runs, enriched with the Actions workflow
behind each job, plus the full-matrix and validated-examples evidence the gate verifies."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Callable, Mapping

from build_test_policy import ORG, BuildTestPolicy

NOT_FOUND = "not_found"
API_ROOT = "https://api.github.com/"
USER_AGENT = "honua-release-gate"
PAGE_SIZE = 100
_ACTIONS_RUN_URL = re.compile(r"/actions/runs/(\d+)(?:/|$)")

Fetcher = Callable[[str, str], object]


def github_headers() -> dict[str, str]:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    return {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
        **({"Authorization": f"Bearer {token}"} if token else {}),
    }


def fetch_paginated_collection(url: str, headers: dict[str, str], key: str, *, page_size: int = PAGE_SIZE) -> dict:
    """Fetch every page of a GitHub REST collection, preserving first-page metadata."""
    items: list[dict] = []
    first_page: dict | None = None
    page = 1
    while True:
        separator = "&" if "?" in url else "?"
        page_url = f"{url}{separator}per_page={page_size}&page={page}"
        request = urllib.request.Request(page_url, headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
            raise ValueError(f"GitHub response did not contain a {key} collection")
        if first_page is None:
            first_page = payload
        batch = payload[key]
        items.extend(batch)
        total = payload.get("total_count")
        if type(total) is not int or total < 0:
            raise ValueError("GitHub response did not declare its collection count")
        if len(items) >= total:
            break
        if len(batch) < page_size:
            raise ValueError("GitHub collection ended before its declared count")
        page += 1

    result = dict(first_page or {})
    result[key] = items
    return result


def fetch_json(url: str, headers: dict) -> dict:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        value = json.loads(response.read().decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("GitHub API response is not an object")
    return value


def enrich_with_workflow_runs(payload: dict, workflow_runs: list[dict]) -> dict:
    """Record the commit's Actions runs and attach each Actions check-run's workflow id, path and run
    identity, so same-named jobs of different workflows stay independent."""
    payload["_workflow_runs"] = workflow_runs
    return enrich_action_workflow_ids(payload, {"workflow_runs": workflow_runs})


def enrich_action_workflow_ids(payload: dict, workflow_payload: dict) -> dict:
    """Attach stable workflow IDs to Actions check-runs so same-named jobs stay independent."""
    workflows = {}
    paths = {}
    replays = {}
    for run in (workflow_payload.get("workflow_runs") or []):
        if run.get("id") is None:
            continue
        run_id = int(run["id"])
        workflows[run_id] = str(run.get("workflow_id") or run.get("path") or "")
        if run.get("path"):
            paths[run_id] = str(run.get("path"))
        replays[run_id] = {"event": str(run.get("event") or ""), "title": str(run.get("display_title") or ""),
                           "branch": str(run.get("head_branch") or "")}
    for run in payload.get("check_runs") or []:
        app = run.get("app") or {}
        if not isinstance(app, dict) or app.get("slug") != "github-actions":
            continue
        match = _ACTIONS_RUN_URL.search(str(run.get("details_url") or ""))
        if not match:
            continue
        actions_run_id = int(match.group(1))
        workflow_id = workflows.get(actions_run_id)
        if workflow_id:
            run["_workflow_id"] = workflow_id
        path = paths.get(actions_run_id)
        if path:
            run["_workflow_path"] = path
        if actions_run_id in replays:
            run["_workflow_run"] = replays[actions_run_id]
    return payload


def github_fetcher(policy: BuildTestPolicy) -> Fetcher:
    return lambda repo, sha: fetch_check_runs(repo, sha, policy.component(repo).full_matrix)


def fetch_check_runs(repo: str, sha: str, full_matrix: Mapping | None) -> object:
    """The commit's check-runs, or NOT_FOUND. When the Actions metadata cannot be read the check-runs are
    returned unenriched: the classifier then keys Actions runs by check suite and retains every suite."""
    headers = github_headers()
    try:
        payload = fetch_paginated_collection(f"{API_ROOT}repos/{ORG}/{repo}/commits/{sha}/check-runs", headers,
                                             "check_runs")
    except urllib.error.HTTPError as e:
        if e.code in (404, 403, 422):
            return NOT_FOUND
        raise
    except (urllib.error.URLError, OSError, ValueError):
        return NOT_FOUND

    try:
        workflow_payload = fetch_paginated_collection(f"{API_ROOT}repos/{ORG}/{repo}/actions/runs?head_sha={sha}",
                                                      headers, "workflow_runs")
        enrich_with_workflow_runs(payload, workflow_payload.get("workflow_runs") or [])
        if full_matrix:
            import full_matrix_evidence as matrix
            selected = matrix.selected_run(payload, full_matrix, sha)
            payload["_matrix_execution"] = matrix.collect(f"{ORG}/{repo}", sha, selected, full_matrix, headers,
                                                          fetch_paginated_collection, fetch_json)
        return payload
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError):
        return payload


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_redirect):
        return None


def download_artifact(url: str) -> bytes:
    """Download a GitHub artifact archive. The token goes only to api.github.com; the signed storage
    URL it redirects to is fetched without it."""
    if not url.startswith(API_ROOT):
        raise ValueError("artifact URL is not a GitHub API URL")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers=github_headers()), timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as redirect:
        location = redirect.headers.get("Location") if redirect.code in (301, 302, 303, 307, 308) else None
        if not location or not location.startswith("https://"):
            raise
    with urllib.request.urlopen(urllib.request.Request(location, headers={"User-Agent": USER_AGENT}),
                                timeout=60) as response:
        return response.read()


def live_validated_examples(name: str, sha: str, component: dict, manifest: dict) -> dict:
    """Live resolver for the validated-examples descendant rule (GitHub API reads only)."""
    import validated_examples_descendant as vx
    sdk = ((manifest.get("components") or {}).get("honua-sdk-python") or {}).get("version")
    return vx.resolve(sha, component, str(sdk) if sdk else None,
                      lambda path: fetch_json(API_ROOT + path, github_headers()),
                      download_artifact)
