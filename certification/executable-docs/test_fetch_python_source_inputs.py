from copy import deepcopy
import hashlib
import io
import json

import pytest

import fetch_python_source_inputs as fetcher
import python_source_inputs as inputs
from test_python_source_inputs import producer


def fixture(monkeypatch, payload=b"finite original archive", *, mutation=""):
    selection, run, artifact, jobs = deepcopy(producer())
    selection["artifact"]["size"] = len(payload)
    selection["artifact"]["sha256"] = hashlib.sha256(payload).hexdigest()
    artifact["size_in_bytes"] = len(payload)
    artifact["digest"] = "sha256:" + selection["artifact"]["sha256"]
    if mutation == "foreign-job": jobs[0]["run_id"] += 1
    if mutation == "incomplete-list": total = 2
    else: total = len(jobs)
    calls = []

    def api(path, *, pages=False):
        calls.append((path, pages))
        if pages:
            return [{"total_count": total, "jobs": jobs}]
        if "/artifacts/" in path:
            return artifact
        return run

    class Process:
        def __init__(self, command, **kwargs):
            calls.append(command)
            assert kwargs == {"stdout": fetcher.subprocess.PIPE, "stderr": fetcher.subprocess.DEVNULL}
            self.stdout = io.BytesIO(payload + (b"extra" if mutation == "oversize" else b""))
            self.done = False

        def wait(self):
            self.done = True
            return 0

        def poll(self):
            return 0 if self.done else None

        def terminate(self):
            self.done = True

    monkeypatch.setattr(inputs, "selected_inputs", lambda: (selection, {}))
    monkeypatch.setattr(fetcher, "api", api)
    monkeypatch.setattr(fetcher.subprocess, "Popen", Process)
    return selection, calls


def test_fixed_fetch_authenticates_attempt_before_retaining_exact_archive(tmp_path, monkeypatch):
    selection, calls = fixture(monkeypatch)
    output = tmp_path / "witness"
    receipt = fetcher.fetch(output)
    assert receipt["archiveSha256"] == inputs.sha_file(output / "original.zip")
    assert receipt["sourceAdmitted"] is False
    assert receipt["publishedWheelIdentity"] is False
    assert receipt["candidateQualified"] is False
    assert calls[2] == ("repos/honua-io/honua-sdk-python/actions/runs/39402438874/attempts/1/jobs?per_page=100", True)
    assert calls[3][-1] == "repos/honua-io/honua-sdk-python/actions/artifacts/12256314217/zip"
    assert json.loads((output / "run.json").read_bytes())["head_sha"] == selection["producer"]["sourceSha"]
    assert set(path.name for path in output.iterdir()) == {"run.json", "jobs.json", "artifact.json", "original.zip", "fetch.json"}


@pytest.mark.parametrize("mutation", ["foreign-job", "incomplete-list"])
def test_foreign_or_incomplete_provenance_never_downloads_or_creates_output(tmp_path, monkeypatch, mutation):
    _, calls = fixture(monkeypatch, mutation=mutation)
    output = tmp_path / "witness"
    with pytest.raises(inputs.InputRefusal):
        fetcher.fetch(output)
    assert not output.exists()
    assert len(calls) == 3


def test_selected_archive_bound_is_enforced_during_download(tmp_path, monkeypatch):
    fixture(monkeypatch, mutation="oversize")
    output = tmp_path / "witness"
    with pytest.raises(inputs.InputRefusal, match="exceeds selected byte bound"):
        fetcher.fetch(output)
    assert not (output / "fetch.json").exists()


def test_fetch_never_clobbers_an_existing_directory(tmp_path, monkeypatch):
    _, calls = fixture(monkeypatch)
    output = tmp_path / "witness"
    output.mkdir()
    (output / "foreign").write_bytes(b"retained")
    with pytest.raises(FileExistsError):
        fetcher.fetch(output)
    assert (output / "foreign").read_bytes() == b"retained"
    assert len(calls) == 3
