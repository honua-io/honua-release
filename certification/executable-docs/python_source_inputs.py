"""Read the exact protected Python source-input witness; never grant admission.

This reader consumes an existing artifact. It has no install, generation,
publication or release-input mutation operation.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import zipfile

REPOSITORY = "honua-io/honua-sdk-python"
SELECTION = Path(__file__).with_name("python-source-inputs") / "selected.json"


class InputRefusal(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputRefusal("duplicate source-input field")
        result[key] = value
    return result


def read_json(data: bytes):
    return json.loads(data, object_pairs_hook=_unique_object)


def sha_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def safe_relative(value: str) -> Path:
    if not isinstance(value, str):
        raise InputRefusal("source-input path is not finite and relative")
    path = Path(value)
    if (not value or path.is_absolute()
            or str(path) != value or ".." in path.parts or "\\" in value):
        raise InputRefusal("source-input path is not finite and relative")
    return path


def selected_inputs(path: Path = SELECTION) -> tuple[dict, dict[str, dict]]:
    selection = read_json(path.read_bytes())
    producer, subject, artifact = (selection[key] for key in ("producer", "subject", "artifact"))
    if (selection.get("schema") != "honua.python-source-input-selection/v1"
            or producer.get("repository") != REPOSITORY or subject.get("repository") != REPOSITORY
            or producer.get("event") != "push" or producer.get("branch") != "trunk"
            or producer.get("workflow") != ".github/workflows/development-source-inputs.yml"
            or producer.get("job") != "governed-document-runtime"
            or any(selection.get(key) is not False for key in ("sourceAdmitted", "publishedWheelIdentity", "candidateQualified"))):
        raise InputRefusal("source-input selection is not a protected review-only producer")
    for value in (producer.get("sourceSha"), subject.get("sourceSha")):
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value):
            raise InputRefusal("source-input revisions must be immutable")
    for value in (artifact.get("id"), artifact.get("size"), artifact.get("entryCount"),
                  artifact.get("uncompressedBytes"), producer.get("runId"), producer.get("attempt"), producer.get("jobId")):
        if type(value) is not int or value <= 0:
            raise InputRefusal("source-input artifact/run bounds are incomplete")
    for value in (artifact.get("sha256"), artifact.get("filesSha256"),
                  selection.get("innerProposalSha256"), selection.get("executionContextSha256")):
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
            raise InputRefusal("source-input byte identities are incomplete")
    raw = (path.parent / safe_relative(artifact["files"])).read_bytes()
    if hashlib.sha256(raw).hexdigest() != artifact["filesSha256"]:
        raise InputRefusal("source-input path manifest bytes differ")
    inventory = read_json(raw)
    if inventory.get("schema") != "honua.python-source-artifact-files/v1":
        raise InputRefusal("source-input path manifest contract differs")
    files = {}
    for row in inventory["files"]:
        if (set(row) != {"path", "size", "sha256"} or type(row["size"]) is not int or row["size"] < 0
                or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"])):
            raise InputRefusal("source-input file identity is incomplete")
        safe_relative(row["path"])
        if row["path"] in files:
            raise InputRefusal("duplicate source-input file")
        files[row["path"]] = row
    if len(files) != artifact["entryCount"] or sum(x["size"] for x in files.values()) != artifact["uncompressedBytes"]:
        raise InputRefusal("source-input file census differs")
    return selection, files


def verify_producer(selection: dict, run: dict, artifact: dict, jobs: list[dict]) -> None:
    producer, expected = selection["producer"], selection["artifact"]
    if (run.get("id") != producer["runId"] or run.get("head_sha") != producer["sourceSha"]
            or run.get("head_branch") != producer["branch"] or run.get("event") != producer["event"]
            or run.get("path") != producer["workflow"] or run.get("run_attempt") != producer["attempt"]
            or run.get("status") != "completed" or run.get("conclusion") != "success"
            or run.get("repository", {}).get("full_name") != producer["repository"]
            or run.get("head_repository", {}).get("full_name") != producer["repository"]):
        raise InputRefusal("source-input producer is not the selected completed protected push")
    workflow = artifact.get("workflow_run", {})
    if (artifact.get("id") != expected["id"] or artifact.get("name") != expected["name"]
            or artifact.get("size_in_bytes") != expected["size"] or artifact.get("digest") != "sha256:" + expected["sha256"]
            or artifact.get("expired") is not False or workflow.get("id") != producer["runId"]
            or workflow.get("head_sha") != producer["sourceSha"] or workflow.get("head_branch") != producer["branch"]
            or workflow.get("repository_id") != run["repository"]["id"]
            or workflow.get("head_repository_id") != run["repository"]["id"]):
        raise InputRefusal("source-input artifact is expired, foreign or unbound")
    matches = [row for row in jobs if row.get("name") == producer["job"]]
    if (len(matches) != 1 or matches[0].get("status") != "completed" or matches[0].get("conclusion") != "success"
            or any(type(matches[0].get(key)) is not int for key in ("id", "run_id", "run_attempt"))
            or matches[0].get("id") != producer["jobId"] or matches[0].get("run_id") != producer["runId"]
            or matches[0].get("head_sha") != producer["sourceSha"] or matches[0].get("run_attempt") != producer["attempt"]):
        raise InputRefusal("source-input governed job did not complete successfully")


def unpack_originals(archive: Path, destination: Path, selection: dict, files: dict[str, dict]) -> list[dict]:
    expected = selection["artifact"]
    if archive.is_symlink() or archive.stat().st_size != expected["size"] or sha_file(archive) != expected["sha256"]:
        raise InputRefusal("source-input archive bytes differ")
    if destination.exists():
        raise InputRefusal("source-input extraction destination must be new")
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if len(names) != len(set(names)) or set(names) != set(files):
            raise InputRefusal("source-input archive has missing, extra or duplicate paths")
        for info in bundle.infolist():
            safe_relative(info.filename)
            kind = stat.S_IFMT(info.external_attr >> 16)
            if info.is_dir() or kind not in (0, stat.S_IFREG) or info.file_size != files[info.filename]["size"]:
                raise InputRefusal("source-input archive shape or file bound differs")
        destination.mkdir(mode=0o700)
        observed = []
        for info in bundle.infolist():
            target = destination / info.filename
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, 8 * 1024 * 1024)
            if target.stat().st_size != files[info.filename]["size"] or sha_file(target) != files[info.filename]["sha256"]:
                raise InputRefusal("source-input extracted file bytes differ")
            observed.append(files[info.filename])
    return observed


def retain_originals(selection: dict, root: Path, files: dict[str, dict]) -> None:
    actual = {str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() or path.is_symlink()}
    if actual != set(files):
        raise InputRefusal("source-input retained file census differs")
    for name, expected in files.items():
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != expected["size"] or sha_file(path) != expected["sha256"]:
            raise InputRefusal("source-input retained bytes changed")
    if (sha_file(root / "proposal/proposal.json") != selection["innerProposalSha256"]
            or sha_file(root / "execution-context.json") != selection["executionContextSha256"]):
        raise InputRefusal("source-input inner or original request binding differs")
