"""Negative controls for the dedicated runner trust boundary (no live admission claims)."""
import copy
import json
import subprocess

import pytest

import release_runner_policy as policy

SHA = "a" * 40
REPO = policy.REPOSITORY
WORKFLOW = policy.WORKFLOW
REF = f"{REPO}/{WORKFLOW}@refs/heads/trunk"


def fixture():
    certificate = {
        "runnerEnvironment": "self-hosted", "sourceRepositoryDigest": SHA,
        "sourceRepositoryRef": "refs/heads/trunk",
        "sourceRepositoryURI": f"https://github.com/{REPO}",
        "buildSignerURI": f"https://github.com/{REF}",
        "runInvocationURI": f"https://github.com/{REPO}/actions/runs/11/attempts/2",
    }
    rule = {"id": 17, "name": "honua-release-heavy", "repositories": [REPO],
            "workflow": WORKFLOW, "disposableJobEnvironment": True, "heavySlots": 1,
            "approvedAt": "2026-10-17T00:00:00Z"}
    config = {"schemaVersion": 1, "organization": "honua-io", "selfHostedGroups": [rule]}
    run = {"id": 11, "run_attempt": 2, "head_sha": SHA, "head_branch": "trunk", "path": WORKFLOW,
           "event": "workflow_dispatch", "status": "completed", "conclusion": "success",
           "repository": {"full_name": REPO}, "head_repository": {"full_name": REPO},
           "workflow_id": 7}
    job = {"id": 12, "name": policy.JOB, "run_id": 11, "head_sha": SHA,
           "status": "completed", "conclusion": "success", "runner_id": 13,
           "runner_group_id": 17, "runner_group_name": "honua-release-heavy",
           "labels": ["self-hosted", "Linux", "X64", "honua-release-heavy"],
           "started_at": "2026-10-17T01:00:00Z"}
    group = {"id": 17, "name": "honua-release-heavy", "visibility": "selected",
             "default": False, "restricted_to_workflows": True, "selected_workflows": [REF]}
    responses = {
        f"repos/{REPO}/actions/runs/11/attempts/2": run,
        f"repos/{REPO}/actions/workflows/capacity-soak-candidate.yml":
            {"id": 7, "path": WORKFLOW, "state": "active"},
        f"repos/{REPO}/branches/trunk": {"protected": True, "commit": {"sha": SHA}},
        f"repos/{REPO}/compare/{SHA}...{SHA}":
            {"status": "identical", "merge_base_commit": {"sha": SHA}},
        f"repos/{REPO}/actions/runs/11/attempts/2/jobs": [job],
        "orgs/honua-io/actions/runner-groups/17": group,
        "orgs/honua-io/actions/runner-groups/17/repositories": [{"full_name": REPO}],
    }
    return certificate, config, responses, run, job, group


class FakeGitHub:
    def __init__(self, responses):
        self.responses = responses
        self.observations = {}

    def get(self, path):
        self.observations[path] = self.responses[path]
        return self.responses[path]

    def pages(self, path, key):
        return self.get(path)


def test_approved_exact_job_and_restricted_group():
    cert, config, responses, *_ = fixture()
    identity = policy.verify_self_hosted(cert, config, FakeGitHub(responses))
    assert identity["runAttempt"] == 2
    assert identity["jobId"] == 12
    assert identity["runnerGroupId"] == 17


def test_exact_candidate_tag_supported():
    cert, config, responses, run, _, group = fixture()
    cert["sourceRepositoryRef"] = f"refs/tags/nightly-candidate/{SHA}"
    run["head_branch"] = f"nightly-candidate/{SHA}"
    workflow_ref = f"{REPO}/{WORKFLOW}@{cert['sourceRepositoryRef']}"
    cert["buildSignerURI"] = f"https://github.com/{workflow_ref}"
    group["selected_workflows"] = [workflow_ref]
    assert policy.verify_self_hosted(cert, config, FakeGitHub(responses))["workflowRef"] == workflow_ref


@pytest.mark.parametrize("field,value", [
    ("run_attempt", 1), ("head_sha", "b" * 40), ("path", "other.yml"), ("head_branch", "untrusted"),
    ("event", "pull_request"), ("event", "pull_request_target"), ("conclusion", "failure"),
    ("conclusion", "skipped"), ("status", "in_progress"), ("workflow_id", 8),
    ("head_repository", {"full_name": "someone/honua-server"}),
])
def test_wrong_run_cannot_borrow_attestation(field, value):
    cert, config, responses, run, *_ = fixture()
    run[field] = value
    with pytest.raises(policy.PolicyError):
        policy.verify_self_hosted(cert, config, FakeGitHub(responses))


@pytest.mark.parametrize("field,value", [
    ("runner_group_id", 999), ("runner_id", 0), ("runner_group_name", "Default"),
    ("conclusion", "skipped"), ("head_sha", "b" * 40), ("labels", ["self-hosted"]),
    ("started_at", "2026-10-16T00:00:00Z"), ("name", "untrusted"),
])
def test_unapproved_or_unexecuted_job(field, value):
    cert, config, responses, _, job, _ = fixture()
    job[field] = value
    with pytest.raises(policy.PolicyError):
        policy.verify_self_hosted(cert, config, FakeGitHub(responses))


@pytest.mark.parametrize("field,value", [
    ("visibility", "all"), ("default", True), ("restricted_to_workflows", False),
    ("selected_workflows", [REF, f"{REPO}/{WORKFLOW}@refs/pull/12/merge"]),
    ("selected_workflows", [REF, f"{REPO}/.github/workflows/ci.yml@refs/heads/trunk"]),
    ("selected_workflows", [REF, f"{REPO}/{WORKFLOW}@refs/tags/*"]),
    ("selected_workflows", []), ("id", 99),
])
def test_group_broadening_is_rejected(field, value):
    cert, config, responses, _, _, group = fixture()
    group[field] = value
    with pytest.raises(policy.PolicyError):
        policy.verify_self_hosted(cert, config, FakeGitHub(responses))


def test_unprotected_or_unlanded_source_is_rejected():
    cert, config, responses, *_ = fixture()
    responses[f"repos/{REPO}/branches/trunk"]["protected"] = False
    with pytest.raises(policy.PolicyError, match="not protected"):
        policy.verify_self_hosted(cert, config, FakeGitHub(responses))
    responses[f"repos/{REPO}/branches/trunk"]["protected"] = True
    responses[f"repos/{REPO}/compare/{SHA}...{SHA}"]["status"] = "diverged"
    with pytest.raises(policy.PolicyError, match="not on protected trunk"):
        policy.verify_self_hosted(cert, config, FakeGitHub(responses))


def test_extra_job_or_repository_is_rejected():
    cert, config, responses, _, job, _ = fixture()
    key = f"repos/{REPO}/actions/runs/11/attempts/2/jobs"
    responses[key].append(copy.deepcopy(job))
    with pytest.raises(policy.PolicyError, match="exactly one"):
        policy.verify_self_hosted(cert, config, FakeGitHub(responses))
    responses[key].pop()
    responses["orgs/honua-io/actions/runner-groups/17/repositories"].append({"full_name": "x/y"})
    with pytest.raises(policy.PolicyError, match="repository access"):
        policy.verify_self_hosted(cert, config, FakeGitHub(responses))


def test_committed_policy_does_not_invent_an_approved_runner():
    cert, *_ = fixture()
    assert "no self-hosted release runner group is approved" in policy.admit(cert)[0]


def test_api_outage_is_not_local_permission(tmp_path, monkeypatch):
    cert, config, *_ = fixture()
    config_path = tmp_path / "policy.json"
    config_path.write_text(json.dumps(config))
    monkeypatch.setattr(policy.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "", "403"))
    report = tmp_path / "runner-admission.json"
    assert "GitHub API" in policy.admit(cert, policy_path=config_path, report_path=report)[0]
    assert json.loads(report.read_text())["status"] == "fail"


def test_pagination_keeps_all_jobs_and_refuses_truncation(monkeypatch):
    api = policy.GitHub()
    pages = [{"jobs": [{"id": i} for i in range(100)], "total_count": 101},
             {"jobs": [{"id": 100}], "total_count": 101}]
    monkeypatch.setattr(api, "get", lambda path: pages[int(path.rsplit("=", 1)[-1]) - 1])
    assert len(api.pages("jobs", "jobs")) == 101
    pages[-1]["total_count"] = 102
    with pytest.raises(policy.PolicyError, match="incomplete"):
        api.pages("jobs", "jobs")
