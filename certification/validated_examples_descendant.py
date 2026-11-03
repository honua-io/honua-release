"""Validated-examples descendant rule for gate-build-test (release-session ruling 2026-11-03, rc.2 redo).

honua-server's "Validated examples (advisory)" workflow (.github/workflows/validated-examples-nightly.yml)
files three check-runs, stac-ops, mobile-offline and gp-local-dev, on each commit it runs on. On the
pinned server commit they are red for harness reasons: a stale example inventory, a missing SDK install
and a demo seed that cleared the wrong metadata environment. The harness fix is a later server commit.
For these three check-runs ONLY, a run on a descendant of the pin counts for the pin when the gate
itself verifies ALL of the following:

  (i)   GitHub's compare pin...run_sha shows a strict descendant, and every changed (or renamed-from)
        path is under examples/ or docs/, or is exactly the workflow file or exactly the mobile-offline
        demo seed. The run's commit is also on server trunk, so the workflow that wrote the evidence was
        reviewed and merged.
  (ii)  The run is the newest completed workflow_dispatch run of that workflow naming the pinned
        image digest, and it succeeded with all three jobs green. Each job's evidence artifact records
        that it ran the pinned image (its pulled RepoDigests), built no local server image, and ran at
        run_sha. The gate reads the run, its jobs and its artifacts itself; the run title is only an
        index. gp-local-dev's evidence must also show the manifest's honua-sdk-python version.
  (iii) The report records the pin sha, run sha, run id/attempt, changed paths, image digest and SDK
        version.

Otherwise the pin's own check-runs decide, as before. Pure verification functions take GitHub API
answers as arguments and are unit-tested; `resolve` takes injected fetchers.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from typing import Callable

RULING = "validated-examples-descendant-2026-11-03"
COMPONENT = "honua-server"
REPO = "honua-io/honua-server"
WORKFLOW_PATH = ".github/workflows/validated-examples-nightly.yml"
WORKFLOW_FILE = "validated-examples-nightly.yml"
SCENARIOS = ("stac-ops", "mobile-offline", "gp-local-dev")
ALLOWED_PREFIXES = ("examples/", "docs/")
ALLOWED_EXACT = frozenset({WORKFLOW_PATH, "tests/seed/mobile-offline-demo-v1.sql"})
EVIDENCE_SCHEMA = "honua.validated-examples-evidence/v1"
TITLE_PREFIX = "Validated examples (advisory) on "
_SHA = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_CANDIDATE = re.compile(r"(?P<repo>ghcr\.io/honua-io/honua-server)(?::[A-Za-z0-9._-]+)?@(?P<digest>sha256:[0-9a-f]{64})")


class NotQualified(ValueError):
    """The descendant run does not qualify; the pin's own check-runs decide."""


def pinned_image(component: dict) -> tuple[str, str]:
    """(image repository, index digest) of the pinned server image from the manifest component."""
    image = str(component.get("image") or "")
    digest = str(component.get("digest") or "")
    repo = image.split("@", 1)[0]
    if ":" in repo.rsplit("/", 1)[-1]:
        repo = repo.rsplit(":", 1)[0]
    if repo != "ghcr.io/honua-io/honua-server" or not _DIGEST.fullmatch(digest):
        raise NotQualified("manifest does not pin a ghcr.io/honua-io/honua-server image digest")
    return repo, digest


def _allowed(path: object) -> bool:
    return (isinstance(path, str) and ".." not in path.split("/") and not path.startswith("/")
            and (path in ALLOWED_EXACT or path.startswith(ALLOWED_PREFIXES)))


def changed_paths(pinned: str, run_sha: str, compare: dict) -> list[str]:
    """Changed paths when run_sha is a strict descendant of pinned touching only allowed paths."""
    if not (_SHA.fullmatch(pinned or "") and _SHA.fullmatch(run_sha or "")) or pinned == run_sha:
        raise NotQualified("descendant rule needs two distinct full commit SHAs")
    if (compare.get("status") != "ahead" or compare.get("behind_by") != 0
            or not isinstance(compare.get("ahead_by"), int) or compare["ahead_by"] < 1
            or (compare.get("base_commit") or {}).get("sha") != pinned
            or (compare.get("merge_base_commit") or {}).get("sha") != pinned):
        raise NotQualified("run commit is not a strict descendant of the pinned commit")
    commits = compare.get("commits") or []
    if not commits or commits[-1].get("sha") != run_sha:
        raise NotQualified("compare does not end at the run commit")
    files = compare.get("files")
    total = compare.get("total_commits")
    if (not isinstance(files, list) or not files or len(files) >= 300
            or not isinstance(total, int) or total > 250 or len(commits) != total):
        raise NotQualified("compare file list is empty or truncated")
    paths = []
    for item in files:
        for key in ("filename", "previous_filename"):
            name = (item or {}).get(key)
            if name is None and key == "previous_filename":
                continue
            if not _allowed(name):
                raise NotQualified(f"descendant changes a path outside the examples allowlist: {name}")
            paths.append(name)
    return sorted(set(paths))


def require_on_trunk(run_sha: str, compare_to_trunk: dict) -> None:
    """compare run_sha...trunk must show the run commit as trunk or an ancestor of it."""
    if (compare_to_trunk.get("status") not in ("ahead", "identical") or compare_to_trunk.get("behind_by") != 0
            or (compare_to_trunk.get("merge_base_commit") or {}).get("sha") != run_sha):
        raise NotQualified("run commit is not on server trunk")


def select_run(runs_payload: dict, pinned: str, digest: str) -> dict:
    """The newest completed workflow_dispatch run whose title names the pinned digest (index only)."""
    candidates = []
    for run in runs_payload.get("workflow_runs") or []:
        title = str(run.get("display_title") or "")
        if (run.get("event") == "workflow_dispatch" and run.get("path") == WORKFLOW_PATH
                and run.get("status") == "completed" and run.get("head_sha") != pinned
                and title.startswith(TITLE_PREFIX) and title.endswith("@" + digest)):
            candidates.append(run)
    if not candidates:
        raise NotQualified("no completed candidate-image dispatch of the validated-examples workflow")
    return max(candidates, key=lambda r: (str(r.get("created_at") or ""), int(r.get("id") or 0)))


def verify_run(run: dict, run_sha: str) -> None:
    if (run.get("path") != WORKFLOW_PATH or run.get("event") != "workflow_dispatch"
            or run.get("status") != "completed" or run.get("conclusion") != "success"
            or run.get("head_sha") != run_sha
            or (run.get("repository") or {}).get("full_name") != REPO
            or (run.get("head_repository") or {}).get("full_name") != REPO):
        raise NotQualified(f"run {run.get('id')} is not a successful {REPO} dispatch of {WORKFLOW_PATH} "
                           f"(conclusion={run.get('conclusion')})")


def verify_jobs(run: dict, jobs_payload: dict, run_sha: str) -> None:
    jobs = jobs_payload.get("jobs") or []
    names = sorted(str(j.get("name")) for j in jobs)
    if names != sorted(SCENARIOS):
        raise NotQualified(f"run {run.get('id')} jobs {names} are not exactly {sorted(SCENARIOS)}")
    for job in jobs:
        if job.get("conclusion") != "success" or job.get("head_sha") != run_sha:
            raise NotQualified(f"job {job.get('name')} conclusion={job.get('conclusion')}")


def verify_evidence(scenario: str, evidence: dict, run: dict, repo: str, digest: str,
                    sdk_version: str | None) -> dict:
    """Check one job's evidence record against the pin; return its reported facts."""
    match = _CANDIDATE.fullmatch(str(evidence.get("candidateImage") or ""))
    expected_repo_digest = f"{repo}@{digest}"
    repo_digests = evidence.get("candidateRepoDigests")
    checks = {
        "schema": evidence.get("schema") == EVIDENCE_SCHEMA,
        "scenario": evidence.get("scenario") == scenario,
        "sourceSha": evidence.get("sourceSha") == run.get("head_sha"),
        "runId": str(evidence.get("runId")) == str(run.get("id")),
        "runAttempt": str(evidence.get("runAttempt")) == str(run.get("run_attempt")),
        "event": evidence.get("event") == "workflow_dispatch",
        "candidateImage": bool(match) and match.group("repo") == repo and match.group("digest") == digest,
        "candidateRepoDigests": (isinstance(repo_digests, list) and expected_repo_digest in repo_digests
                                 and all(str(d).endswith("@" + digest) for d in repo_digests)),
        "noLocalBuild": evidence.get("locallyBuiltServerImages") == [],
        "jobStatus": evidence.get("jobStatus") == "success",
    }
    if scenario == "gp-local-dev" and sdk_version:
        checks["pythonSdkVersion"] = evidence.get("pythonSdkVersion") == sdk_version
    bad = sorted(k for k, ok in checks.items() if not ok)
    if bad:
        raise NotQualified(f"{scenario} evidence does not prove the pinned candidate: {bad}")
    return {"candidateImage": evidence["candidateImage"], "pythonSdkVersion": evidence.get("pythonSdkVersion")}


def evidence_from_zip(scenario: str, blob: bytes) -> dict:
    name = f"validated-examples-{scenario}.json"
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            value = json.loads(archive.read(name).decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NotQualified(f"{scenario} artifact has no readable {name}") from exc
    if not isinstance(value, dict):
        raise NotQualified(f"{scenario} evidence is not an object")
    return value


def resolve(pinned: str, component: dict, sdk_version: str | None,
            get_json: Callable[[str], dict], get_bytes: Callable[[str], bytes]) -> dict:
    """Return {'status': 'verified', ...record} or {'status': 'unavailable', 'reason': ...}.

    get_json(path) answers GitHub REST paths (relative to https://api.github.com/); get_bytes(url)
    downloads an artifact archive. Any error leaves the pin's own check-runs in charge."""
    try:
        repo, digest = pinned_image(component)
        runs = get_json(f"repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/runs?event=workflow_dispatch&per_page=100")
        run = select_run(runs, pinned, digest)
        run_sha = str(run.get("head_sha") or "")
        paths = changed_paths(pinned, run_sha, get_json(f"repos/{REPO}/compare/{pinned}...{run_sha}"))
        require_on_trunk(run_sha, get_json(f"repos/{REPO}/compare/{run_sha}...trunk"))
        verify_run(run, run_sha)
        verify_jobs(run, get_json(f"repos/{REPO}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100"), run_sha)
        artifacts = get_json(f"repos/{REPO}/actions/runs/{run['id']}/artifacts?per_page=100").get("artifacts") or []
        facts = {}
        for scenario in SCENARIOS:
            matches = [a for a in artifacts if a.get("name") == f"validated-examples-{scenario}"
                       and not a.get("expired") and (a.get("workflow_run") or {}).get("id") == run["id"]]
            verified = []
            for artifact in matches:
                evidence = evidence_from_zip(scenario, get_bytes(str(artifact.get("archive_download_url"))))
                if str(evidence.get("runAttempt")) == str(run.get("run_attempt")):
                    verified.append(verify_evidence(scenario, evidence, run, repo, digest, sdk_version))
            if len(verified) != 1:
                raise NotQualified(f"{scenario}: expected one evidence artifact for attempt "
                                   f"{run.get('run_attempt')}, found {len(verified)}")
            facts[scenario] = verified[0]
        return {"status": "verified", "ruling": RULING, "pinSha": pinned, "runSha": run_sha,
                "runId": run["id"], "runAttempt": run.get("run_attempt"), "runUrl": run.get("html_url"),
                "changedPaths": paths, "imageDigest": digest,
                "candidateImage": facts[SCENARIOS[0]]["candidateImage"],
                "pythonSdkVersion": facts["gp-local-dev"]["pythonSdkVersion"],
                "scenarios": list(SCENARIOS)}
    except NotQualified as exc:
        return {"status": "unavailable", "ruling": RULING, "reason": str(exc)}
    except (KeyError, TypeError, ValueError, OSError) as exc:
        return {"status": "unavailable", "ruling": RULING, "reason": f"could not read run evidence: {exc}"}
