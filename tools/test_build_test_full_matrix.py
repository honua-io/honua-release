"""Regression tests for SHA-bound full-matrix evidence in the build/test gate."""
from __future__ import annotations

import io
import base64
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "certification"))
import check_build_test as bt  # noqa: E402


PINNED_SHA = "a" * 40
FULL_MATRIX_POLICY = {
    "workflow": ".github/workflows/ci.yml",
    "repository": "honua-io/honua-server",
    "shardInventory": {"path": ".github/ci-shards.json", "jobName": "Server Tests ({shard_name})", "testStep": "Run server test shard"},
    "events": frozenset({"schedule", "workflow_dispatch"}),
    "requiredChecks": frozenset({
        "Build & Format Check",
        ".NET Foundation Tests",
        "Python Integration Tests",
        "Test Suite Summary",
        "CI Gate",
    }),
}


def _full_matrix_payload(*, conclusion="success", omitted=frozenset(), overrides=None,
                         sha=PINNED_SHA, status="completed"):
    run_id, suite = 12345, 67
    overrides = overrides or {}
    names = sorted(FULL_MATRIX_POLICY["requiredChecks"] | {"Server Tests (Core)", "Server Tests (Mutation)"})
    checks, jobs = [], []
    for index, name in enumerate(names, 1):
        if name in omitted:
            continue
        result = overrides.get(name, "success")
        checks.append({"id": index, "name": name, "status": "completed", "conclusion": result,
                       "head_sha": sha, "app": {"slug": "github-actions", "id": 15368}, "check_suite": {"id": suite},
                       "details_url": f"https://github.com/honua-io/honua-server/actions/runs/{run_id}/job/{index}"})
        jobs.append({"id": index, "name": name, "status": "completed", "conclusion": result,
                     "head_sha": sha, "run_id": run_id, "run_attempt": 1,
                     "check_run_url": f"https://api.github.com/repos/honua-io/honua-server/check-runs/{index}",
                     "steps": [{"name": "Run server test shard", "status": "completed", "conclusion": "success"}]})
    raw = json.dumps({"shards": [{"shard_name": "Core"}, {"shard_name": "Mutation"}]})
    blob = hashlib.sha1(b"blob " + str(len(raw.encode())).encode() + b"\0" + raw.encode()).hexdigest()
    return {
        "check_runs": checks,
        "_workflow_runs": [{"id": run_id, "path": ".github/workflows/ci.yml", "event": "workflow_dispatch", "head_sha": sha,
                            "status": status, "conclusion": conclusion, "run_attempt": 1, "check_suite_id": suite,
                            "updated_at": "2026-08-25T00:00:00Z"}],
        "_matrix_execution": {"repository": "honua-io/honua-server", "runId": run_id, "runAttempt": 1, "checkSuiteId": suite,
                              "headSha": sha, "registryPath": ".github/ci-shards.json", "registryBlobSha": blob,
                              "registryContent": raw, "jobs": jobs},
    }


@pytest.mark.parametrize("payload", [
    {"check_runs": [], "_workflow_runs": []},
    _full_matrix_payload(sha="b" * 40),
    _full_matrix_payload(status="in_progress", conclusion=None),
])
def test_full_matrix_requires_completed_run_bound_to_exact_pinned_sha(payload):
    status, why = bt.classify_full_matrix(payload, FULL_MATRIX_POLICY, PINNED_SHA)
    assert status == "fail"
    assert "missing completed full-matrix run" in why
    assert "pinned sha" in why


def test_full_matrix_names_every_absent_expected_lane():
    missing = {"Build & Format Check", "Python Integration Tests"}
    status, why = bt.classify_full_matrix(
        _full_matrix_payload(omitted=missing), FULL_MATRIX_POLICY, PINNED_SHA
    )
    assert status == "fail"
    assert "missing expected lanes" in why
    assert all(name in why for name in missing)


def test_full_matrix_rejects_red_workflow_and_names_red_lane():
    payload = _full_matrix_payload(
        conclusion="failure", overrides={".NET Foundation Tests": "failure"}
    )
    status, why = bt.classify_full_matrix(payload, FULL_MATRIX_POLICY, PINNED_SHA)
    assert status == "fail"
    assert ".NET Foundation Tests" in why
    assert "workflow conclusion=failure" in why


def test_full_matrix_ignores_expected_lane_from_an_unrelated_run():
    payload = _full_matrix_payload(omitted={"CI Gate"})
    payload["check_runs"].append({
        "name": "CI Gate",
        "status": "completed",
        "conclusion": "success",
        "details_url": "https://github.com/honua-io/honua-server/actions/runs/99999/job/1",
    })
    status, why = bt.classify_full_matrix(payload, FULL_MATRIX_POLICY, PINNED_SHA)
    assert status == "fail"
    assert "CI Gate" in why


def test_full_matrix_green_run_with_all_expected_lanes_passes():
    status, why = bt.classify_full_matrix(_full_matrix_payload(), FULL_MATRIX_POLICY, PINNED_SHA)
    assert status == "pass"
    assert "complete pinned shard execution" in why


def test_evaluate_rejects_green_subset_when_full_matrix_evidence_is_absent():
    manifest = {"components": {"honua-server": {"sha": PINNED_SHA}}}
    green_subset = {"check_runs": [{
        "name": "PR Gate",
        "status": "completed",
        "conclusion": "success",
    }]}
    report = bt.evaluate(
        manifest,
        lambda _repo, _sha: green_subset,
        full_matrix={"honua-server": FULL_MATRIX_POLICY},
    )
    row = report["components"][0]
    assert report["overallStatus"] == "fail"
    assert row["status"] == "fail"
    assert "missing completed full-matrix run" in row["why"]


def test_full_matrix_policy_names_the_stable_server_lanes():
    policy = bt.load_full_matrix()["honua-server"]
    assert policy["workflow"] == ".github/workflows/ci.yml"
    assert policy["events"] == frozenset({"schedule", "workflow_dispatch"})
    assert FULL_MATRIX_POLICY["requiredChecks"] == policy["requiredChecks"]


def test_github_collection_fetch_follows_every_page(monkeypatch):
    requested: list[str] = []
    pages = {
        1: {"total_count": 101, "check_runs": [{"id": i} for i in range(100)]},
        2: {"total_count": 101, "check_runs": [{"id": 100}]},
    }

    def fake_urlopen(request, timeout):
        requested.append(request.full_url)
        page = int(request.full_url.rsplit("page=", 1)[1])
        return io.BytesIO(json.dumps(pages[page]).encode())

    monkeypatch.setattr(bt.urllib.request, "urlopen", fake_urlopen)
    payload = bt._fetch_paginated_collection("https://api.github.test/check-runs", {}, "check_runs")

    assert len(payload["check_runs"]) == 101
    assert requested == [
        "https://api.github.test/check-runs?per_page=100&page=1",
        "https://api.github.test/check-runs?per_page=100&page=2",
    ]


@pytest.mark.parametrize('mutation', ['app-id', 'app-slug', 'suite', 'check-sha', 'check-id', 'job-sha', 'job-run', 'job-future-attempt',
                                     'missing-evidence', 'evidence-attempt', 'registry-ref', 'registry-bytes', 'step-failure', 'step-skipped',
                                     'step-missing', 'job-duplicate', 'shard-missing', 'shard-unexpected'])
def test_full_matrix_rejects_untrusted_or_incomplete_execution(mutation):
    payload = _full_matrix_payload()
    evidence = payload['_matrix_execution']
    check = next(c for c in payload['check_runs'] if c['name'] == 'Server Tests (Core)')
    job = next(j for j in evidence['jobs'] if j['name'] == 'Server Tests (Core)')
    if mutation == 'app-id': check['app']['id'] = 999
    elif mutation == 'app-slug': check['app']['slug'] = 'other-publisher'
    elif mutation == 'suite': check['check_suite']['id'] = 999
    elif mutation == 'check-sha': check['head_sha'] = 'b' * 40
    elif mutation == 'check-id': job['check_run_url'] = 'https://github.com/honua-io/honua-server/actions/runs/12345/job/1'
    elif mutation == 'job-sha': job['head_sha'] = 'b' * 40
    elif mutation == 'job-run': job['run_id'] = 999
    elif mutation == 'job-future-attempt': job['run_attempt'] = 2
    elif mutation == 'missing-evidence': del payload['_matrix_execution']
    elif mutation == 'evidence-attempt': evidence['runAttempt'] = 2
    elif mutation == 'registry-ref': evidence['headSha'] = 'b' * 40
    elif mutation == 'registry-bytes': evidence['registryContent'] += ' '
    elif mutation == 'step-failure': job['steps'][0]['conclusion'] = 'failure'
    elif mutation == 'step-skipped': job['steps'][0]['conclusion'] = 'skipped'
    elif mutation == 'step-missing': job['steps'] = []
    elif mutation == 'job-duplicate': evidence['jobs'].append(copy.deepcopy(job))
    elif mutation == 'shard-missing': evidence['jobs'].remove(job)
    else:
        extra = copy.deepcopy(job); extra.update(id=999, name='Server Tests (Unselected)'); evidence['jobs'].append(extra)
    assert bt.classify_full_matrix(payload, FULL_MATRIX_POLICY, PINNED_SHA)[0] == 'fail'


def test_unrelated_publisher_cannot_replace_skipped_real_python_check_using_a_public_url():
    payload = _full_matrix_payload(overrides={'Python Integration Tests': 'skipped'})
    real = next(c for c in payload['check_runs'] if c['name'] == 'Python Integration Tests')
    lookalike = copy.deepcopy(real)
    lookalike.update(id=999, conclusion='success', app={'slug': 'other-check-publisher', 'id': 999}, check_suite={'id': 999})
    payload['check_runs'].append(lookalike)
    status, why = bt.classify_full_matrix(payload, FULL_MATRIX_POLICY, PINNED_SHA)
    assert status == 'fail' and 'Python Integration Tests' in why


def test_latest_real_job_attempt_replaces_old_failure_only_with_same_run_and_sha():
    payload = _full_matrix_payload()
    payload['_workflow_runs'][0]['run_attempt'] = 2
    evidence = payload['_matrix_execution']; evidence['runAttempt'] = 2
    job = next(j for j in evidence['jobs'] if j['name'] == 'Server Tests (Mutation)')
    old = copy.deepcopy(job); old.update(id=99, conclusion='failure')
    job['run_attempt'] = 2
    evidence['jobs'].append(old)
    # Other, unchanged successful jobs remain explicitly attributed to attempt1 of this exact
    # immutable run. A failed-only rerun never invents that they executed in attempt2.
    assert bt.classify_full_matrix(payload, FULL_MATRIX_POLICY, PINNED_SHA)[0] == 'pass'
    job['conclusion'] = 'failure'
    assert bt.classify_full_matrix(payload, FULL_MATRIX_POLICY, PINNED_SHA)[0] == 'fail'


def test_default_adapter_collects_pinned_source_and_all_job_attempts(monkeypatch):
    payload = _full_matrix_payload()
    evidence = payload['_matrix_execution']
    raw = evidence['registryContent'].encode()
    called = []
    def urlopen(request, timeout):
        url = request.full_url; called.append(url)
        if '/commits/' in url: value = {'total_count': len(payload['check_runs']), 'check_runs': payload['check_runs']}
        elif '/actions/runs?' in url: value = {'total_count': 1, 'workflow_runs': payload['_workflow_runs']}
        elif '/jobs?' in url: value = {'total_count': len(evidence['jobs']), 'jobs': evidence['jobs']}
        elif '/contents/' in url:
            assert url.endswith('?ref=' + PINNED_SHA)
            value = {'type': 'file', 'encoding': 'base64', 'sha': evidence['registryBlobSha'], 'content': base64.b64encode(raw).decode()}
        else: pytest.fail('unexpected API route: ' + url)
        return io.BytesIO(json.dumps(value).encode())
    monkeypatch.setattr(bt.urllib.request, 'urlopen', urlopen)
    loaded = bt._default_fetch('honua-server', PINNED_SHA)
    assert bt.classify_full_matrix(loaded, bt.load_full_matrix()['honua-server'], PINNED_SHA)[0] == 'pass'
    assert any('/actions/runs/12345/jobs?filter=all&per_page=20&page=1' in url for url in called)


@pytest.mark.parametrize('response', [{'jobs': []}, {'total_count': 2, 'jobs': [{}]}, {'total_count': 0, 'wrong': []}])
def test_incomplete_or_unbounded_job_api_collection_fails_closed(monkeypatch, response):
    monkeypatch.setattr(bt.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(json.dumps(response).encode()))
    with pytest.raises(ValueError):
        bt._fetch_paginated_collection('https://api.github.com/repos/honua-io/honua-server/actions/runs/1/jobs', {}, 'jobs')


@pytest.mark.parametrize('field,value', [('app', None), ('app', []), ('check_suite', None), ('check_suite', []), ('steps', None)])
def test_malformed_publisher_or_steps_fails_without_exception(field, value):
    payload = _full_matrix_payload()
    if field == 'steps':
        next(j for j in payload['_matrix_execution']['jobs'] if j['name'] == 'Server Tests (Core)')[field] = value
    else:
        payload['check_runs'][-1][field] = value
    assert bt.classify_full_matrix(payload, FULL_MATRIX_POLICY, PINNED_SHA)[0] == 'fail'
