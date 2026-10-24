from copy import deepcopy
import hashlib
from pathlib import Path
import stat
import zipfile

import pytest

import python_source_inputs as inputs


def producer():
    selection, _ = inputs.selected_inputs()
    expected = selection["producer"]
    run = {"id": expected["runId"], "head_sha": expected["sourceSha"], "head_branch": "trunk",
           "event": "push", "path": expected["workflow"], "run_attempt": 1,
           "status": "completed", "conclusion": "success",
           "repository": {"full_name": inputs.REPOSITORY, "id": 1171772287},
           "head_repository": {"full_name": inputs.REPOSITORY}}
    artifact = {"id": selection["artifact"]["id"], "name": selection["artifact"]["name"],
                "size_in_bytes": selection["artifact"]["size"], "digest": "sha256:" + selection["artifact"]["sha256"],
                "expired": False, "workflow_run": {"id": expected["runId"], "head_sha": expected["sourceSha"],
                "head_branch": "trunk", "repository_id": 1171772287, "head_repository_id": 1171772287}}
    jobs = [{"id": expected["jobId"], "name": "governed-document-runtime", "status": "completed", "conclusion": "success",
             "run_id": expected["runId"], "head_sha": expected["sourceSha"], "run_attempt": expected["attempt"]}]
    return selection, run, artifact, jobs


def test_selection_binds_all338_original_paths_and_both_source_identities():
    selection, files = inputs.selected_inputs()
    assert len(files) == 338
    assert selection["producer"]["sourceSha"] != selection["subject"]["sourceSha"]
    assert sum(row["size"] for row in files.values()) == 3191283000
    assert selection["sourceAdmitted"] is False
    assert selection["publishedWheelIdentity"] is False
    assert selection["candidateQualified"] is False
    inputs.verify_producer(*producer())


@pytest.mark.parametrize("mutation", ["source", "repo", "workflow", "pr-event", "old-attempt", "live", "red",
                                     "expired", "artifact-name", "digest", "foreign-run", "foreign-head",
                                     "foreign-repo", "missing-job", "red-job", "duplicate-job", "job-run", "job-head",
                                     "job-attempt", "job-id", "missing-job-run", "missing-job-head", "missing-job-attempt",
                                     "bool-job-id", "zero-job-id"])
def test_producer_refuses_foreign_stale_incomplete_or_unbound_artifact(mutation):
    selection, run, artifact, jobs = deepcopy(producer())
    if mutation == "source": run["head_sha"] = "a" * 40
    if mutation == "repo": run["head_repository"]["full_name"] = "other/fork"
    if mutation == "workflow": run["path"] = ".github/workflows/other.yml"
    if mutation == "pr-event": run["event"] = "pull_request"
    if mutation == "old-attempt": run["run_attempt"] = 2
    if mutation == "live": run["status"] = "in_progress"
    if mutation == "red": run["conclusion"] = "failure"
    if mutation == "expired": artifact["expired"] = True
    if mutation == "artifact-name": artifact["name"] = "development-source-input-proposal"
    if mutation == "digest": artifact["digest"] = "sha256:" + "0" * 64
    if mutation == "foreign-run": artifact["workflow_run"]["id"] += 1
    if mutation == "foreign-head": artifact["workflow_run"]["head_sha"] = "a" * 40
    if mutation == "foreign-repo": artifact["workflow_run"]["head_repository_id"] += 1
    if mutation == "missing-job": jobs.clear()
    if mutation == "red-job": jobs[0]["conclusion"] = "failure"
    if mutation == "duplicate-job": jobs.append(deepcopy(jobs[0]))
    if mutation == "job-run": jobs[0]["run_id"] += 1
    if mutation == "job-head": jobs[0]["head_sha"] = "a" * 40
    if mutation == "job-attempt": jobs[0]["run_attempt"] += 1
    if mutation == "job-id": jobs[0]["id"] += 1
    if mutation == "missing-job-run": jobs[0].pop("run_id")
    if mutation == "missing-job-head": jobs[0].pop("head_sha")
    if mutation == "missing-job-attempt": jobs[0].pop("run_attempt")
    if mutation == "bool-job-id": jobs[0]["id"] = True
    if mutation == "zero-job-id": jobs[0]["id"] = 0
    with pytest.raises(inputs.InputRefusal):
        inputs.verify_producer(selection, run, artifact, jobs)


def archive(tmp_path: Path, mutation: str = ""):
    values = {"execution-context.json": b'{"original":"request"}', "proposal/proposal.json": b'{"source":"immutable"}'}
    files = {name: {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
             for name, data in values.items()}
    path = tmp_path / "original.zip"
    with zipfile.ZipFile(path, "w") as bundle:
        for name, data in values.items():
            if mutation == "missing" and name.startswith("proposal/"):
                continue
            if mutation == "changed" and name.startswith("proposal/"):
                data = data.replace(b"immutable", b"untrusted")
            bundle.writestr(name, data)
        if mutation == "extra": bundle.writestr("other.txt", b"extra")
        if mutation == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                bundle.writestr("execution-context.json", values["execution-context.json"])
        if mutation in ("escape", "symlink"):
            name = "../escape" if mutation == "escape" else "link"
            info = zipfile.ZipInfo(name)
            if mutation == "symlink": info.external_attr = (stat.S_IFLNK | 0o777) << 16
            bundle.writestr(info, b"outside")
            files[name] = {"path": name, "size": 7, "sha256": hashlib.sha256(b"outside").hexdigest()}
    selection = {"artifact": {"size": path.stat().st_size, "sha256": inputs.sha_file(path)}}
    return path, selection, files


def test_unpack_preserves_every_original_byte(tmp_path):
    path, selection, files = archive(tmp_path)
    result = inputs.unpack_originals(path, tmp_path / "originals", selection, files)
    assert {row["path"] for row in result} == set(files)
    assert (tmp_path / "originals/proposal/proposal.json").read_bytes() == b'{"source":"immutable"}'


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate", "changed", "escape", "symlink"])
def test_validly_hashed_archive_still_refuses_path_census_or_payload_faults(tmp_path, mutation):
    path, selection, files = archive(tmp_path, mutation)
    with pytest.raises(inputs.InputRefusal):
        inputs.unpack_originals(path, tmp_path / "originals", selection, files)
    assert not (tmp_path / "escape").exists()


def test_unpack_refuses_existing_destination_without_overwriting_it(tmp_path):
    path, selection, files = archive(tmp_path)
    destination = tmp_path / "originals"
    destination.mkdir()
    (destination / "unrelated").write_bytes(b"retained")
    with pytest.raises(inputs.InputRefusal):
        inputs.unpack_originals(path, destination, selection, files)
    assert (destination / "unrelated").read_bytes() == b"retained"
