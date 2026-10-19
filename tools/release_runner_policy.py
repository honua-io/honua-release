"""Admit self-hosted capacity evidence only through a reviewed Actions runner group.

This consumes live GitHub metadata, never a producer-supplied runner receipt. Cryptographic
attestation verification and candidate/run/artifact binding remain the caller's responsibility.
An empty committed policy intentionally admits no self-hosted runners.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

POLICY = Path(__file__).resolve().parents[1] / "certification/release-runners.v1.json"
REPOSITORY = "honua-io/honua-server"
WORKFLOW = ".github/workflows/capacity-soak-candidate.yml"
JOB = "Candidate capacity soak"


class PolicyError(ValueError):
    """Runner identity or access policy could not be established."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PolicyError(message)


def _positive(value: Any) -> bool:
    return type(value) is int and value > 0


def _time(value: Any) -> datetime:
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    _require(result.tzinfo is not None, "runner policy timestamps must include a timezone")
    return result.astimezone(timezone.utc)


class GitHub:
    def __init__(self) -> None:
        self.observations: dict[str, Any] = {}

    def get(self, path: str) -> Any:
        result = subprocess.run(
            ["gh", "api", path], capture_output=True, text=True, timeout=60, check=False
        )
        if result.returncode:
            raise PolicyError(f"cannot verify runner policy through GitHub API: {path}")
        data = json.loads(result.stdout)
        self.observations[path] = data
        return data

    def pages(self, path: str, key: str) -> list[dict]:
        rows: list[dict] = []
        for page in range(1, 101):
            data = self.get(f"{path}?per_page=100&page={page}")
            batch = data.get(key)
            _require(isinstance(batch, list), f"missing GitHub collection: {key}")
            rows.extend(batch)
            if len(batch) < 100:
                _require(len(rows) == data.get("total_count"), f"incomplete GitHub collection: {key}")
                return rows
        raise PolicyError("runner metadata pagination exceeded the bounded limit")


def verify_self_hosted(certificate: dict, policy: dict, api: GitHub) -> dict:
    """Verify the run's single producing job and the group's live access restrictions."""
    _require(policy.get("schemaVersion") == 1 and policy.get("organization") == "honua-io",
             "unknown release runner policy")
    groups = policy.get("selfHostedGroups")
    _require(isinstance(groups, list) and bool(groups), "no self-hosted release runner group is approved")
    _require(certificate.get("runnerEnvironment") == "self-hosted", "not a self-hosted attestation")
    source = certificate.get("sourceRepositoryDigest", "")
    _require(isinstance(source, str) and re.fullmatch(r"[0-9a-f]{40}", source) is not None,
             "invalid attested source revision")
    ref = certificate.get("sourceRepositoryRef")
    _require(ref in ("refs/heads/trunk", f"refs/tags/nightly-candidate/{source}"),
             "self-hosted producer must use trunk or the exact candidate tag")
    workflow_ref = f"{REPOSITORY}/{WORKFLOW}@{ref}"
    _require(certificate.get("sourceRepositoryURI") == f"https://github.com/{REPOSITORY}"
             and certificate.get("buildSignerURI") == f"https://github.com/{workflow_ref}",
             "unapproved self-hosted producer workflow")
    match = re.fullmatch(
        rf"https://github.com/{REPOSITORY}/actions/runs/([1-9][0-9]*)/attempts/([1-9][0-9]*)",
        str(certificate.get("runInvocationURI", "")),
    )
    _require(match is not None, "invalid attested Actions run/attempt")
    run_id, attempt = map(int, match.groups())
    branch_name = ref.removeprefix("refs/heads/").removeprefix("refs/tags/")
    prefix = f"repos/{REPOSITORY}"
    run = api.get(f"{prefix}/actions/runs/{run_id}/attempts/{attempt}")
    _require(run.get("id") == run_id and run.get("run_attempt") == attempt
             and run.get("head_sha") == source and run.get("path") == WORKFLOW
             and run.get("head_branch") == branch_name
             and run.get("event") == "workflow_dispatch"
             and run.get("status") == "completed" and run.get("conclusion") == "success"
             and run.get("repository", {}).get("full_name") == REPOSITORY
             and run.get("head_repository", {}).get("full_name") == REPOSITORY,
             "self-hosted producer run identity or success differs from the attestation")
    workflow = api.get(f"{prefix}/actions/workflows/{WORKFLOW.split('/')[-1]}")
    _require(workflow.get("id") == run.get("workflow_id") and workflow.get("path") == WORKFLOW
             and workflow.get("state") == "active", "producer workflow identity is not active")
    branch = api.get(f"{prefix}/branches/trunk")
    _require(branch.get("protected") is True, "producer trunk is not protected")
    trunk_sha = branch.get("commit", {}).get("sha")
    _require(isinstance(trunk_sha, str) and re.fullmatch(r"[0-9a-f]{40}", trunk_sha) is not None,
             "missing protected trunk revision")
    compare = api.get(f"{prefix}/compare/{source}...{trunk_sha}")
    _require(compare.get("status") in ("ahead", "identical")
             and compare.get("merge_base_commit", {}).get("sha") == source,
             "producer source is not on protected trunk")
    jobs = api.pages(f"{prefix}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs")
    _require(len(jobs) == 1, "capacity producer must have exactly one producing job")
    job = jobs[0]
    _require(job.get("name") == JOB and job.get("run_id") == run_id
             and job.get("head_sha") == source and job.get("status") == "completed"
             and job.get("conclusion") == "success" and _positive(job.get("id"))
             and _positive(job.get("runner_id")) and _positive(job.get("runner_group_id")),
             "capacity job identity, runner or result is invalid")
    approved = [g for g in groups if isinstance(g, dict) and g.get("id") == job["runner_group_id"]]
    _require(len(approved) == 1, "producing job used an unapproved runner group")
    rule = approved[0]
    _require(rule.get("name") == job.get("runner_group_name")
             and rule.get("repositories") == [REPOSITORY]
             and rule.get("workflow") == WORKFLOW
             and rule.get("disposableJobEnvironment") is True
             and rule.get("heavySlots") == 1,
             "committed runner-group policy is incomplete")
    _require(_time(rule.get("approvedAt")) <= _time(job.get("started_at")),
             "job predates runner-group approval")
    required_labels = {"self-hosted", "linux", "x64", "honua-release-heavy"}
    _require(required_labels <= {str(x).lower() for x in job.get("labels", [])},
             "capacity job did not select the approved Linux heavy pool")
    group_path = f"orgs/honua-io/actions/runner-groups/{rule['id']}"
    group = api.get(group_path)
    _require(group.get("id") == rule["id"] and group.get("name") == rule["name"]
             and group.get("visibility") == "selected" and group.get("default") is False
             and group.get("restricted_to_workflows") is True,
             "runner group is not restricted to selected repositories and workflows")
    selected = group.get("selected_workflows")
    # Candidate-tag entries must name full commits; no wildcard, PR or arbitrary branch admission.
    allowed = re.compile(rf"{re.escape(REPOSITORY + '/' + WORKFLOW)}@"
                         r"(?:refs/heads/trunk|refs/tags/nightly-candidate/[0-9a-f]{40})")
    _require(isinstance(selected, list) and workflow_ref in selected
             and all(isinstance(w, str) and allowed.fullmatch(w) for w in selected),
             "runner group workflow access is broader than the approved producer refs")
    repos = api.pages(f"{group_path}/repositories", "repositories")
    _require([r.get("full_name") for r in repos] == [REPOSITORY],
             "runner group repository access differs from committed policy")
    return {"repository": REPOSITORY, "runId": run_id, "runAttempt": attempt,
            "jobId": job["id"], "runnerId": job["runner_id"], "runnerGroupId": rule["id"],
            "sourceRevision": source, "workflowRef": workflow_ref}


def admit(certificate: dict, *, policy_path: Path = POLICY, report_path: Path | None = None) -> list[str]:
    api = GitHub()
    report: dict[str, Any] = {"status": "fail", "observations": api.observations}
    try:
        policy_bytes = policy_path.read_bytes()
        report["policySha256"] = hashlib.sha256(policy_bytes).hexdigest()
        report["identity"] = verify_self_hosted(certificate, json.loads(policy_bytes), api)
        report["status"] = "pass"
        return []
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.TimeoutExpired) as exc:
        report["error"] = str(exc)
        return [f"self-hosted runner admission failed: {exc}"]
    finally:
        if report_path is not None:
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
