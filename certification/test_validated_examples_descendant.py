"""Tests for the honua-server validated-examples descendant rule (validated_examples_descendant.py).

The rule may only ever let a descendant run stand in for the pin when every condition is verified,
so each refusal is proven here: a non-examples path, no digest, a different digest, not a descendant,
not on trunk, a red or newer-red run, a local build, a wrong SDK and a mismatched attempt.

Run: python -m pytest certification/test_validated_examples_descendant.py
"""
from __future__ import annotations

import copy
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validated_examples_descendant as vx  # noqa: E402

PIN = "8628668794df9f0c22a490e59cedaee409cc1bdd"
RUN_SHA = "a" * 40
TRUNK = "b" * 40
DIGEST = "sha256:24d20a8647f6dafcfc1bef683a31f4dddf6ef4476ebb33456afa8a96b11288da"
OTHER_DIGEST = "sha256:" + "c" * 64
IMAGE = f"ghcr.io/honua-io/honua-server@{DIGEST}"
COMPONENT = {"image": "ghcr.io/honua-io/honua-server:nightly-8628668", "digest": DIGEST, "sha": PIN}
SDK = "0.1.19"
RUN_ID = 4242
REPO = {"full_name": "honua-io/honua-server"}


def _compare(paths=("examples/manifest.json", "docs/internal/ci/workflow-inventory.md",
                    ".github/workflows/validated-examples-nightly.yml",
                    "tests/seed/mobile-offline-demo-v1.sql"), **over):
    value = {"status": "ahead", "ahead_by": 1, "behind_by": 0, "total_commits": 1,
             "base_commit": {"sha": PIN}, "merge_base_commit": {"sha": PIN},
             "commits": [{"sha": RUN_SHA}], "files": [{"filename": p} for p in paths]}
    value.update(over)
    return value


def _run(**over):
    value = {"id": RUN_ID, "run_attempt": 1, "path": vx.WORKFLOW_PATH, "event": "workflow_dispatch",
             "status": "completed", "conclusion": "success", "head_sha": RUN_SHA,
             "created_at": "2026-11-03T20:00:00Z", "run_started_at": "2026-11-03T20:00:00Z",
             "updated_at": "2026-11-03T20:30:00Z", "display_title": vx.TITLE_PREFIX + IMAGE,
             "repository": REPO, "head_repository": REPO, "html_url": "https://example.invalid/run"}
    value.update(over)
    return value


def _evidence(scenario, **over):
    value = {"schema": vx.EVIDENCE_SCHEMA, "scenario": scenario, "sourceSha": RUN_SHA, "runId": str(RUN_ID),
             "runAttempt": "1", "event": "workflow_dispatch", "candidateImage": IMAGE,
             "candidateRepoDigests": [IMAGE], "locallyBuiltServerImages": [],
             "pythonSdkVersion": SDK if scenario == "gp-local-dev" else None, "jobStatus": "success"}
    value.update(over)
    return value


def _zip(scenario, evidence):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr(f"validated-examples-{scenario}.json", json.dumps(evidence))
        archive.writestr(f"validated-examples-{scenario}.txt", "log")
    return buf.getvalue()


class World:
    """A fake GitHub: REST answers by path and artifact bytes by URL."""

    def __init__(self):
        self.runs = [_run()]
        self.total = None
        self.compare = _compare()
        self.trunk = {"status": "ahead", "behind_by": 0, "merge_base_commit": {"sha": RUN_SHA}}
        self.jobs = {"jobs": [{"name": s, "conclusion": "success", "head_sha": RUN_SHA} for s in vx.SCENARIOS]}
        self.evidence = {s: _evidence(s) for s in vx.SCENARIOS}

    def get_json(self, path):
        if path.startswith(f"repos/{vx.REPO}/actions/workflows/{vx.WORKFLOW_FILE}/runs?"):
            page = int(path.rsplit("page=", 1)[1])
            total = len(self.runs) if self.total is None else self.total
            return {"workflow_runs": self.runs[(page - 1) * 100: page * 100], "total_count": total}
        if path == f"repos/{vx.REPO}/compare/{PIN}...{RUN_SHA}":
            return self.compare
        if path == f"repos/{vx.REPO}/compare/{RUN_SHA}...trunk":
            return self.trunk
        if path.startswith(f"repos/{vx.REPO}/actions/runs/{RUN_ID}/jobs"):
            return self.jobs
        if path.startswith(f"repos/{vx.REPO}/actions/runs/{RUN_ID}/artifacts"):
            return {"artifacts": [{"name": f"validated-examples-{s}", "expired": False,
                                   "workflow_run": {"id": RUN_ID},
                                   "archive_download_url": f"https://api.github.com/artifact/{s}"}
                                  for s in self.evidence]}
        raise AssertionError(f"unexpected GitHub read {path}")

    def get_bytes(self, url):
        scenario = url.rsplit("/", 1)[-1]
        return _zip(scenario, self.evidence[scenario])

    def resolve(self, sdk=SDK, component=COMPONENT):
        return vx.resolve(PIN, component, sdk, self.get_json, self.get_bytes)


def test_verified_descendant_records_pin_run_paths_and_digest():
    record = World().resolve()
    assert record["status"] == "verified", record
    assert record["pinSha"] == PIN and record["runSha"] == RUN_SHA and record["runId"] == RUN_ID
    assert record["imageDigest"] == DIGEST and record["candidateImage"] == IMAGE
    assert record["pythonSdkVersion"] == SDK and record["ruling"] == vx.RULING
    assert record["changedPaths"] == sorted([
        ".github/workflows/validated-examples-nightly.yml", "docs/internal/ci/workflow-inventory.md",
        "examples/manifest.json", "tests/seed/mobile-offline-demo-v1.sql"])


@pytest.mark.parametrize("path", [
    "src/Honua.Server/Program.cs",
    "tests/seed/stac-ops-demo-v1.sql",          # only the one exact seed file, never tests/**
    "tests/dotnet/Honua.Server.Tests/DatabaseMigrationTests.cs",
    "scripts/examples/validate-customer-paths.sh",
    "docker-compose.yml",
    ".github/workflows/ci.yml",
    "docs/../src/x.cs",
])
def test_non_examples_path_refuses(path):
    world = World()
    world.compare = _compare(paths=("examples/manifest.json", path))
    record = world.resolve()
    assert record["status"] == "unavailable" and "allowlist" in record["reason"]


def test_rename_from_a_product_path_refuses():
    world = World()
    world.compare = _compare(paths=())
    world.compare["files"] = [{"filename": "docs/x.md", "previous_filename": "src/x.cs"}]
    assert world.resolve()["status"] == "unavailable"


@pytest.mark.parametrize("over", [
    {"status": "diverged", "behind_by": 1},
    {"status": "behind", "ahead_by": 0, "behind_by": 1},
    {"status": "identical", "ahead_by": 0},
    {"merge_base_commit": {"sha": "d" * 40}},
    {"base_commit": {"sha": "d" * 40}},
    {"commits": [{"sha": "e" * 40}]},
])
def test_not_a_strict_descendant_refuses(over):
    world = World()
    world.compare = _compare(**over)
    record = world.resolve()
    assert record["status"] == "unavailable", over


def test_truncated_compare_refuses():
    world = World()
    world.compare = _compare(paths=[f"docs/f{i}.md" for i in range(300)])
    assert world.resolve()["status"] == "unavailable"
    world.compare = _compare(total_commits=251)
    assert world.resolve()["status"] == "unavailable"


def test_run_commit_not_on_trunk_refuses():
    world = World()
    world.trunk = {"status": "diverged", "behind_by": 2, "merge_base_commit": {"sha": PIN}}
    record = world.resolve()
    assert record["status"] == "unavailable" and "trunk" in record["reason"]


def test_no_digest_dispatch_refuses():
    world = World()
    world.runs = [_run(display_title="Validated examples (advisory)")]   # a source-build dispatch
    assert world.resolve()["status"] == "unavailable"


def test_title_alone_is_not_trusted_evidence_must_name_the_digest():
    world = World()
    world.evidence["stac-ops"] = _evidence("stac-ops", candidateImage=None, candidateRepoDigests=[])
    record = world.resolve()
    assert record["status"] == "unavailable" and "candidateImage" in record["reason"]


def test_different_digest_refuses():
    other = f"ghcr.io/honua-io/honua-server@{OTHER_DIGEST}"
    world = World()
    world.runs = [_run(display_title=vx.TITLE_PREFIX + other)]
    assert world.resolve()["status"] == "unavailable"          # not even selected
    world = World()
    world.evidence["mobile-offline"] = _evidence("mobile-offline", candidateImage=other,
                                                 candidateRepoDigests=[other])
    assert world.resolve()["status"] == "unavailable"
    world = World()
    world.evidence["gp-local-dev"] = _evidence("gp-local-dev", candidateRepoDigests=[IMAGE, other])
    assert world.resolve()["status"] == "unavailable"


def test_other_repository_image_refuses():
    world = World()
    fake = f"ghcr.io/someone/honua-server@{DIGEST}"
    world.evidence["stac-ops"] = _evidence("stac-ops", candidateImage=fake, candidateRepoDigests=[fake])
    assert world.resolve()["status"] == "unavailable"


def test_local_server_build_refuses():
    world = World()
    world.evidence["stac-ops"] = _evidence("stac-ops", locallyBuiltServerImages=["honua-server:local"])
    assert world.resolve()["status"] == "unavailable"


def test_wrong_python_sdk_refuses():
    world = World()
    world.evidence["gp-local-dev"] = _evidence("gp-local-dev", pythonSdkVersion="0.1.20")
    assert world.resolve()["status"] == "unavailable"


@pytest.mark.parametrize("field,value", [("sourceSha", PIN), ("runId", "1"), ("runAttempt", "2"),
                                         ("event", "schedule"), ("jobStatus", "failure"),
                                         ("schema", "other"), ("scenario", "stac-ops")])
def test_evidence_identity_mismatch_refuses(field, value):
    world = World()
    evidence = _evidence("mobile-offline")
    evidence[field] = value
    world.evidence["mobile-offline"] = evidence
    assert world.resolve()["status"] == "unavailable"


def test_red_run_or_job_refuses():
    world = World()
    world.runs = [_run(conclusion="failure")]
    assert world.resolve()["status"] == "unavailable"
    world = World()
    world.jobs["jobs"][1]["conclusion"] = "failure"
    assert world.resolve()["status"] == "unavailable"
    world = World()
    world.jobs["jobs"] = world.jobs["jobs"][:2]
    assert world.resolve()["status"] == "unavailable"


def test_newest_candidate_run_decides_an_older_green_cannot_hide_a_newer_red():
    world = World()
    newer_red = _run(id=RUN_ID + 1, conclusion="failure", created_at="2026-11-03T21:00:00Z",
                     run_started_at="2026-11-03T21:00:00Z", updated_at="2026-11-03T21:30:00Z")
    world.runs = [_run(), newer_red]
    record = world.resolve()
    assert record["status"] == "unavailable" and str(RUN_ID + 1) in record["reason"]


def test_fork_run_refuses():
    world = World()
    world.runs = [_run(head_repository={"full_name": "someone/honua-server"})]
    assert world.resolve()["status"] == "unavailable"


def test_missing_or_unreadable_artifact_refuses():
    world = World()
    del world.evidence["gp-local-dev"]
    assert world.resolve()["status"] == "unavailable"
    world = World()
    world.get_bytes = lambda url: b"not a zip"
    assert world.resolve()["status"] == "unavailable"


def test_manifest_without_pinned_digest_refuses():
    component = copy.deepcopy(COMPONENT)
    component["digest"] = ""
    assert World().resolve(component=component)["status"] == "unavailable"


def test_a_newer_red_on_the_pin_itself_decides():
    """Sol round 1: a newer failed candidate dispatch at the pin sha must not be filtered out."""
    world = World()
    world.runs.append(_run(id=RUN_ID + 1, head_sha=PIN, conclusion="failure", created_at="2026-11-03T21:00:00Z",
                           run_started_at="2026-11-03T21:00:00Z", updated_at="2026-11-03T21:30:00Z"))
    assert world.resolve()["status"] == "unavailable"


def test_a_failed_rerun_of_an_earlier_dispatch_decides():
    """Sol round 1: recency is execution recency (the latest attempt), not the original dispatch time."""
    world = World()
    world.runs.append(_run(id=RUN_ID - 1, run_attempt=2, conclusion="failure", created_at="2026-11-03T19:00:00Z",
                           run_started_at="2026-11-03T21:50:00Z", updated_at="2026-11-03T22:00:00Z"))
    record = world.resolve()
    assert record["status"] == "unavailable" and str(RUN_ID - 1) in record["reason"]


def test_an_older_red_that_finished_before_the_green_started_does_not_block_it():
    world = World()
    world.runs.append(_run(id=RUN_ID - 1, conclusion="failure", created_at="2026-11-03T18:00:00Z",
                           run_started_at="2026-11-03T18:00:00Z", updated_at="2026-11-03T18:40:00Z"))
    assert world.resolve()["status"] == "verified"


def test_a_concurrent_red_that_finished_first_still_refuses():
    """Sol round 3: green 20:00-20:40, red 20:10-20:20. The green does not supersede the red."""
    world = World()
    world.runs[0]["updated_at"] = "2026-11-03T20:40:00Z"
    world.runs.append(_run(id=RUN_ID + 1, conclusion="failure", created_at="2026-11-03T20:10:00Z",
                           run_started_at="2026-11-03T20:10:00Z", updated_at="2026-11-03T20:20:00Z"))
    record = world.resolve()
    assert record["status"] == "unavailable" and "overlap" in record["reason"]
    # The same overlap with the green starting later is still refused (it started before the red ended).
    world = World()
    world.runs[0].update(run_started_at="2026-11-03T20:15:00Z", updated_at="2026-11-03T20:40:00Z")
    world.runs.append(_run(id=RUN_ID + 1, conclusion="failure", created_at="2026-11-03T20:10:00Z",
                           run_started_at="2026-11-03T20:10:00Z", updated_at="2026-11-03T20:20:00Z"))
    assert world.resolve()["status"] == "unavailable"


def test_tied_starts_are_ambiguous_and_refuse():
    """Sol round 2: attempt numbers are per dispatch; a tie never picks the green."""
    world = World()
    world.runs[0]["run_attempt"] = 2
    world.runs.append(_run(id=RUN_ID + 1, conclusion="failure", run_started_at=world.runs[0]["run_started_at"],
                           updated_at=world.runs[0]["updated_at"]))
    record = world.resolve()
    assert record["status"] == "unavailable" and "ambiguous" in record["reason"]


@pytest.mark.parametrize("field", ["updated_at", "run_started_at"])
@pytest.mark.parametrize("stamp", [None, "", "yesterday", "2026-11-03 20:40:00"])
def test_a_candidate_without_valid_ordering_timestamps_refuses(field, stamp):
    """Sol round 2: missing ordering evidence is not proof of age."""
    world = World()
    stamps = {"run_started_at": "2026-11-03T18:00:00Z", "updated_at": "2026-11-03T18:40:00Z", field: stamp}
    world.runs.append(_run(id=RUN_ID - 1, conclusion="failure", created_at="2026-11-03T18:00:00Z", **stamps))
    record = world.resolve()
    assert record["status"] == "unavailable" and "execution order is unknown" in record["reason"]


def _filler(count, start=10_000):
    return [_run(id=start + i, display_title="Validated examples (advisory)", run_started_at="2026-11-03T17:00:00Z",
                 updated_at="2026-11-03T17:30:00Z") for i in range(count)]


def test_every_page_of_dispatches_is_read_and_a_failed_rerun_on_page_two_decides():
    """Sol round 2: the run list is paginated; the deciding run may be beyond the first page."""
    world = World()
    rerun_red = _run(id=RUN_ID - 1, run_attempt=2, conclusion="failure", created_at="2026-11-03T18:00:00Z",
                     run_started_at="2026-11-03T21:50:00Z", updated_at="2026-11-03T22:00:00Z")
    world.runs = [world.runs[0]] + _filler(99) + [rerun_red]
    record = world.resolve()
    assert record["status"] == "unavailable" and str(RUN_ID - 1) in record["reason"]
    world.runs = [world.runs[0]] + _filler(100)
    assert world.resolve()["status"] == "verified"      # two pages, the green is still the latest candidate


def test_a_dispatch_inserted_between_pages_refuses():
    """Sol round 3: a run inserted at the front while paging shifts a duplicate onto page 2."""
    world = World()
    base = [world.runs[0]] + _filler(100)                 # 101 runs
    new_red = _run(id=20_000, conclusion="failure", run_started_at="2026-11-03T22:00:00Z",
                   updated_at="2026-11-03T22:30:00Z")
    served = {"n": 0}
    original = world.get_json

    def get_json(path):
        if "/runs?" in path and "page=" in path:
            served["n"] += 1
            page = int(path.rsplit("page=", 1)[1])
            runs = base if served["n"] == 1 else [new_red] + base
            return {"workflow_runs": runs[(page - 1) * 100: page * 100], "total_count": len(runs)}
        return original(path)

    world.get_json = get_json
    record = world.resolve()
    assert record["status"] == "unavailable" and "changed while it was read" in record["reason"]


def test_a_duplicate_run_id_or_a_changed_first_page_refuses():
    world = World()
    base = [world.runs[0]] + _filler(99)
    dup = base + [base[5]]                                 # 101 entries, 100 distinct
    world.runs = dup
    assert world.resolve()["status"] == "unavailable"


def test_an_incomplete_dispatch_list_refuses():
    world = World()
    world.total = 5                         # declares more runs than the API returns
    assert world.resolve()["status"] == "unavailable"
    world = World()
    world.total = vx.RUN_SEARCH_LIMIT + 1   # beyond the listing limit, completeness cannot be shown
    assert world.resolve()["status"] == "unavailable"


def test_a_running_candidate_dispatch_refuses():
    world = World()
    world.runs.append(_run(id=RUN_ID + 1, status="in_progress", conclusion=None, run_started_at="2026-11-03T21:00:00Z",
                           updated_at="2026-11-03T21:00:00Z"))
    record = world.resolve()
    assert record["status"] == "unavailable" and "not completed" in record["reason"]
