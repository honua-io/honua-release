import json
import shutil
import subprocess
from datetime import timedelta

import pytest

import check_promotion_readiness as readiness
import fetch_promotion_evidence as fetcher
from test_check_promotion_readiness import NOW, OTHER_LOCK, _fixture, _stamp

REPO = "honua-io/honua-release"


class FakeGitHub(fetcher.GitHub):
    """Actions metadata and artifacts served from a readiness fixture's retained layout."""

    def __init__(self, fixture, *, minting_path=".github/workflows/nightly-certification.yml",
                 minting_event="schedule"):
        super().__init__(REPO, runner=None)
        record, _, evidence, _ = fixture
        self.runs, self.artifacts, self.attempts, self.canaries = {}, {}, {}, []
        self.promote_runs, self.released = [], set()
        train = record["strictTrains"][0]
        self.add_run(train["runId"], train["completedAt"], path=minting_path, event=minting_event)
        self.artifacts[(train["runId"], "certified-candidate")] = [
            path for path in (evidence / "trains" / train["runId"]).iterdir() if path.name != "run.json"]
        for row in record["evidence"]:
            if row["runId"] not in self.runs:
                self.add_run(row["runId"], row["completedAt"], path=".github/workflows/qualify.yml",
                             event="workflow_dispatch")
            self.artifacts[(row["runId"], f"promotion-receipt-{row['class']}")] = [
                evidence / "evidence" / row["class"] / row["runId"] / "receipt.json"]
        for run in json.loads((evidence / "canary-sequence.json").read_text())["runs"]:
            self.add_canary(run["runId"], run["completedAt"],
                            evidence=evidence / "canaries" / run["runId"] / "live-canary-evidence.json")

    def add_run(self, run_id, completed, *, path, event, conclusion="success", status="completed",
                branch="trunk", repository=REPO, attempt=1):
        self.runs[str(run_id)] = {
            "id": int(run_id), "repository": {"full_name": repository}, "head_repository": {"full_name": repository},
            "head_branch": branch, "path": path, "event": event, "status": status, "conclusion": conclusion,
            "updated_at": completed, "created_at": completed, "run_attempt": attempt}
        return self.runs[str(run_id)]

    def add_canary(self, run_id, completed, *, evidence=None, conclusion="success", status="completed", attempt=1):
        run = self.add_run(run_id, completed, path=fetcher.CANARY_WORKFLOW, event="schedule",
                           conclusion=conclusion, status=status, attempt=attempt)
        self.canaries.append(run)
        if evidence is not None:
            self.artifacts[(str(run_id), "live-canary-evidence")] = [evidence]

    def api(self, path=""):
        if not path:
            return {"full_name": REPO, "default_branch": "trunk"}
        parts = path.split("/")
        if parts[:2] == ["actions", "runs"] and len(parts) == 3:
            return self.runs[parts[2]]
        if parts[:2] == ["actions", "runs"] and parts[3] == "attempts":
            return self.attempts[(parts[2], int(parts[4]))]
        raise AssertionError(path)

    def pages(self, path, key):
        if path.startswith("actions/workflows/demo-canary.yml/runs?event=schedule&created=%3E%3D"):
            return list(self.canaries)
        if path.startswith("actions/workflows/promote.yml/runs"):
            return list(self.promote_runs)
        raise AssertionError(path)

    def download(self, run_id, artifact, dest):
        files = self.artifacts.get((str(run_id), artifact))
        if not files:
            return False
        dest.mkdir(parents=True, exist_ok=True)
        for path in files:
            shutil.copyfile(path, dest / path.name)
        return True

    def release_exists(self, tag):
        return tag in self.released


def _fetch_and_check(tmp_path, fixture, gh):
    out = tmp_path / "fetched"
    fetcher.fetch(fixture[0], gh, out)
    rc = fixture[0]["rcTrainRunId"]
    return readiness.evaluate(fixture[0], lock_path=out / "trains" / rc / "platform-lock.json",
                              evidence_dir=out, now=NOW)[0]


def test_fetched_layout_passes_readiness_for_a_scheduled_minting_train(tmp_path):
    fixture = _fixture(tmp_path / "source")
    decision = _fetch_and_check(tmp_path, fixture, FakeGitHub(fixture))
    assert decision["status"] == "pass", decision["checks"]
    # The checker read the selected lock's retained bytes from the minting train.
    assert decision["lockDigest"] == fixture[0]["lock"]["digest"]


def test_dispatched_minting_train_is_also_accepted(tmp_path):
    fixture = _fixture(tmp_path / "source")
    gh = FakeGitHub(fixture, minting_path=".github/workflows/release-train.yml", minting_event="workflow_dispatch")
    assert _fetch_and_check(tmp_path, fixture, gh)["status"] == "pass"


@pytest.mark.parametrize("field,value", [
    ("event", "push"), ("event", "pull_request"), ("path", ".github/workflows/demo-canary.yml"),
    ("head_branch", "feature/weaken-gates"), ("conclusion", "failure"), ("status", "in_progress"),
    ("repository", {"full_name": "fork/honua-release"}),
])
def test_minting_run_identity_is_enforced(tmp_path, field, value):
    fixture = _fixture(tmp_path / "source")
    gh = FakeGitHub(fixture)
    gh.runs["101"][field] = value
    with pytest.raises(fetcher.FetchError, match="identity"):
        fetcher.fetch(fixture[0], gh, tmp_path / "fetched")


def test_minting_train_without_retained_lock_refuses(tmp_path):
    fixture = _fixture(tmp_path / "source")
    gh = FakeGitHub(fixture)
    gh.artifacts[("101", "certified-candidate")] = [
        path for path in gh.artifacts[("101", "certified-candidate")] if path.name != "platform-lock.json"]
    with pytest.raises(fetcher.FetchError, match="retained certified candidate lock"):
        fetcher.fetch(fixture[0], gh, tmp_path / "fetched")


def test_missing_class_receipt_is_left_for_the_checker_to_refuse(tmp_path):
    fixture = _fixture(tmp_path / "source")
    gh = FakeGitHub(fixture)
    row = next(row for row in fixture[0]["evidence"] if row["class"] == "cite")
    del gh.artifacts[(row["runId"], "promotion-receipt-cite")]
    decision = _fetch_and_check(tmp_path, fixture, gh)
    assert decision["status"] == "refused"
    assert decision["checks"]["evidence:cite"]["status"] == "fail"


def test_qualifying_receipt_from_a_failed_or_foreign_run_refuses(tmp_path):
    fixture = _fixture(tmp_path / "source")
    gh = FakeGitHub(fixture)
    row = next(row for row in fixture[0]["evidence"] if row["class"] == "update-rollback")
    gh.runs[row["runId"]]["head_branch"] = "feature/forged-proof"
    with pytest.raises(fetcher.FetchError, match="identity"):
        fetcher.fetch(fixture[0], gh, tmp_path / "fetched")


@pytest.mark.parametrize("field,value", [("event", "workflow_dispatch"), ("path", ".github/workflows/other.yml"),
                                         ("conclusion", "failure")])
def test_recorded_canary_must_be_a_successful_scheduled_demo_canary(tmp_path, field, value):
    fixture = _fixture(tmp_path / "source")
    gh = FakeGitHub(fixture)
    gh.runs[fixture[0]["demoCanaries"][0]["runId"]][field] = value
    with pytest.raises(fetcher.FetchError, match="identity"):
        fetcher.fetch(fixture[0], gh, tmp_path / "fetched")


def _failed_canary_evidence(tmp_path, digest):
    path = tmp_path / "failed-canary" / "live-canary-evidence.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"runId": "199", "status": "fail", "candidateLock": {"digest": digest}}))
    return path


def test_lock_failure_before_recorded_burn_start_reaches_the_checker(tmp_path):
    fixture = _fixture(tmp_path / "source", age=50, minted=60)
    gh = FakeGitHub(fixture)
    gh.add_canary("199", _stamp(NOW - timedelta(hours=55)), conclusion="failure",
                  evidence=_failed_canary_evidence(tmp_path, fixture[0]["lock"]["digest"]))
    decision = _fetch_and_check(tmp_path, fixture, gh)
    assert decision["checks"]["lock-burn-health"]["status"] == "fail"


def test_canary_that_failed_before_binding_a_lock_is_unattributed(tmp_path):
    fixture = _fixture(tmp_path / "source", age=50, minted=60)
    gh = FakeGitHub(fixture)
    gh.add_canary("199", _stamp(NOW - timedelta(hours=55)), conclusion="failure")
    out = tmp_path / "fetched"
    fetcher.fetch(fixture[0], gh, out)
    sequence = json.loads((out / "canary-sequence.json").read_text())
    assert {"runId": "199", "completedAt": _stamp(NOW - timedelta(hours=55)), "status": "failure",
            "lockDigest": None} in sequence["runs"]
    decision = readiness.evaluate(fixture[0], lock_path=out / "trains/101/platform-lock.json", evidence_dir=out, now=NOW)[0]
    assert decision["checks"]["lock-burn-health"]["status"] == "fail"


def test_another_locks_failure_does_not_block(tmp_path):
    fixture = _fixture(tmp_path / "source", age=50, minted=60)
    gh = FakeGitHub(fixture)
    gh.add_canary("199", _stamp(NOW - timedelta(hours=55)), conclusion="failure",
                  evidence=_failed_canary_evidence(tmp_path, OTHER_LOCK))
    assert _fetch_and_check(tmp_path, fixture, gh)["status"] == "pass"


def test_a_successful_rerun_cannot_erase_the_failed_attempt(tmp_path):
    fixture = _fixture(tmp_path / "source")
    gh = FakeGitHub(fixture)
    rerun = gh.canaries[2]
    rerun["run_attempt"] = 2
    gh.attempts[(str(rerun["id"]), 1)] = {"conclusion": "failure", "updated_at": rerun["updated_at"]}
    decision = _fetch_and_check(tmp_path, fixture, gh)
    assert decision["checks"]["lock-burn-health"]["status"] == "fail"


def test_in_progress_canary_is_not_yet_an_observation(tmp_path):
    fixture = _fixture(tmp_path / "source")
    gh = FakeGitHub(fixture)
    gh.add_canary("999", _stamp(NOW), status="in_progress", conclusion=None)
    assert _fetch_and_check(tmp_path, fixture, gh)["status"] == "pass"


def _record(promotions, label, *, burn_hours, rc="123", platform_label=None):
    promotions.mkdir(exist_ok=True)
    (promotions / f"{label}.json").write_text(json.dumps({
        "platformLabel": platform_label or label, "rcTrainRunId": rc,
        "lock": {"burnStartedAt": _stamp(NOW - timedelta(hours=burn_hours))}}))


def _promote_run(label, *, hours_ago, status="completed"):
    return {"display_title": f"promote {label} (from run 123)", "status": status,
            "created_at": _stamp(NOW - timedelta(hours=hours_ago))}


def _candidates(tmp_path, gh, published=lambda _: False):
    return [row["label"] for row in fetcher.candidates(tmp_path / "promotions", gh, now=NOW, published=published)]


def test_candidates_open_at_hour_48(tmp_path):
    gh = FakeGitHub(_fixture(tmp_path / "source"))
    _record(tmp_path / "promotions", "2026.1-rc.3", burn_hours=47.99)
    _record(tmp_path / "promotions", "2026.1-rc.4", burn_hours=48)
    _record(tmp_path / "promotions", "2026.1-rc.5", burn_hours=200)
    assert _candidates(tmp_path, gh) == ["2026.1-rc.4", "2026.1-rc.5"]


def test_candidates_skip_published_pending_and_recent_requests(tmp_path):
    gh = FakeGitHub(_fixture(tmp_path / "source"))
    for n in (3, 4, 5, 6, 7):
        _record(tmp_path / "promotions", f"2026.1-rc.{n}", burn_hours=60)
    gh.promote_runs = [
        _promote_run("2026.1-rc.4", hours_ago=30, status="waiting"),    # awaiting its reviewer
        _promote_run("2026.1-rc.5", hours_ago=2),                       # just refused or failed
        _promote_run("2026.1-rc.6", hours_ago=25),                      # an old refusal: ask again
    ]
    assert _candidates(tmp_path, gh, published=lambda label: label == "2026.1-rc.7") == ["2026.1-rc.3", "2026.1-rc.6"]


def test_candidates_skip_records_whose_label_disagrees_with_their_path(tmp_path):
    gh = FakeGitHub(_fixture(tmp_path / "source"))
    _record(tmp_path / "promotions", "2026.1-rc.3", burn_hours=60, platform_label="2026.1-rc.4")
    _record(tmp_path / "promotions", "2026.1-rc.5", burn_hours=60, rc="not-a-run")
    assert _candidates(tmp_path, gh) == []


def test_gh_calls_are_read_only_and_scoped_to_the_repository(tmp_path):
    calls = []

    def runner(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout='{"default_branch":"trunk"}', stderr="")

    gh = fetcher.GitHub(REPO, runner=runner)
    assert gh.api() == {"default_branch": "trunk"}
    gh.api("actions/runs/7")
    gh.download("7", "certified-candidate", tmp_path / "download")
    gh.release_exists("honua-2026.1.0")
    assert calls == [
        ["gh", "api", f"repos/{REPO}"],
        ["gh", "api", f"repos/{REPO}/actions/runs/7"],
        ["gh", "run", "download", "7", "--repo", REPO, "--name", "certified-candidate",
         "--dir", str(tmp_path / "download")],
        ["gh", "release", "view", "honua-2026.1.0", "--repo", REPO],
    ]
