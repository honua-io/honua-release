"""Immutable development-source admission. The default audit still refuses direct origins."""
from pathlib import Path
import hashlib
import json
import subprocess
import sys
import venv
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import python_environment_audit as probe
import python_source_admission as admission


OUTSIDE = "OUTSIDE-ORIGIN-MARKER"
PACKAGE = "packages/honua-sdk"
BACKEND = '''import pathlib, zipfile

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    root = pathlib.Path(__file__).resolve().parent
    payload = (root / "honua_sdk" / "__init__.py").read_bytes()
    name = "honua_sdk-0.1.16-py3-none-any.whl"
    with zipfile.ZipFile(pathlib.Path(wheel_directory) / name, "w") as archive:
        archive.writestr("honua_sdk/__init__.py", payload)
        archive.writestr("honua_sdk-0.1.16.dist-info/METADATA", "Metadata-Version: 2.1\\nName: honua-sdk\\nVersion: 0.1.16\\n")
        archive.writestr("honua_sdk-0.1.16.dist-info/WHEEL", "Wheel-Version: 1.0\\nGenerator: offline-control\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n")
        archive.writestr("honua_sdk-0.1.16.dist-info/RECORD", "")
    return name
'''


def git(repo, *args):
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "-c", "user.email=mike@honua.io",
         "-c", "user.name=Mike McDougall", *args],
        text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return result


def wheel_bytes(name, version, marker):
    import io
    dist = name.replace("-", "_")
    info = f"{dist}-{version}.dist-info"
    payload = f"MARKER = {marker!r}\n".encode()
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
    wheel = b"Wheel-Version: 1.0\nGenerator: offline-control\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{dist}/__init__.py", payload)
        archive.writestr(f"{info}/METADATA", metadata)
        archive.writestr(f"{info}/WHEEL", wheel)
        archive.writestr(f"{info}/RECORD", b"")
    return buffer.getvalue()


def write_checkout(repo, marker="bound-checkout"):
    package = repo / PACKAGE
    (package / "honua_sdk").mkdir(parents=True)
    (repo / "wheels").mkdir()
    (repo / ".gitignore").write_text("*.egg-info/\n*.egg-info\n__pycache__/\nbuild/\ndist/\n")
    (package / "pyproject.toml").write_text(
        "[build-system]\nrequires = []\nbuild-backend = \"backend\"\nbackend-path = [\".\"]\n\n"
        "[project]\nname = \"honua-sdk\"\nversion = \"0.1.16\"\n")
    (package / "backend.py").write_text(BACKEND)
    (package / "honua_sdk" / "__init__.py").write_text(f"MARKER = {marker!r}\n")
    wheel = wheel_bytes("offline-build-pin", "1.0.0", "pinned-build")
    relative = "wheels/offline_build_pin-1.0.0-py3-none-any.whl"
    (repo / relative).write_bytes(wheel)
    git(repo, "init", "-b", "trunk")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "bind source")
    return relative, hashlib.sha256(wheel).hexdigest()


def pip_install(root, *args):
    result = subprocess.run(
        [sys.executable, "-m", "pip", "--python", str(root / "bin/python"), "install",
         "--no-index", "--no-deps", *args],
        text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr


def binding_for(repo, relative, digest):
    return admission.bind(
        repo, document="README.md", block_index=1, package_relative=PACKAGE,
        package_name="honua-sdk", package_version="0.1.16",
        metadata_files=[f"{PACKAGE}/pyproject.toml"], backend_files=[f"{PACKAGE}/backend.py"],
        package_files=[{"source": "honua_sdk/__init__.py", "installed": "honua_sdk/__init__.py"}],
        dependencies=[{"name": "offline-build-pin", "version": "1.0.0", "sha256": digest, "path": relative}])


@pytest.fixture(scope="module")
def admitted(tmp_path_factory):
    root = tmp_path_factory.mktemp("admitted")
    repo = root / "repo"
    repo.mkdir()
    relative, digest = write_checkout(repo)
    environment = root / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    pip_install(environment, str(repo / relative))
    pip_install(environment, "--no-build-isolation", str(repo / PACKAGE))
    status = git(repo, "status", "--porcelain", "--untracked-files=normal")
    assert status.stdout.strip() == ""
    bound = binding_for(repo, relative, digest)
    site = next((environment / "lib").glob("python*/site-packages"))
    origin = json.loads(next(site.glob("honua_sdk-0.1.16.dist-info/direct_url.json")).read_text())
    assert "dir_info" in origin
    return {"root": root, "repo": repo, "venv": environment, "site": site, "binding": bound,
            "wheel": relative, "digest": digest, "origin": origin}


def inventory(admitted_case, binding):
    return probe.inventory([admitted_case["root"]], [str(admitted_case["venv"] / "bin/python")],
                           source_admission=binding)


def test_reviewed_blocks_stay_the_three_exact_document_identities():
    assert admission.reviewed_blocks() == [dict(block) for block in admission.REQUIRED_BLOCKS]


def test_real_local_install_admits_source_without_wheel_or_candidate_credit(admitted, monkeypatch):
    monkeypatch.setattr(probe, "default_metadata_sites", lambda: [])
    result = inventory(admitted, admitted["binding"])
    evidence = result["sourceBuildEvidence"]
    assert evidence["sourceAdmitted"] is True
    assert evidence["publishedWheelIdentity"] is False
    assert evidence["candidateQualified"] is False
    assert evidence["archiveCredit"] is False
    assert evidence["wheelByteCredit"] is False
    assert result["publishedWheelIdentity"] is False
    assert result["candidateQualified"] is False
    blob = json.dumps(evidence)
    assert str(admitted["repo"]) not in blob
    assert "file:" not in blob
    assert evidence["blockSha256"] == admission.REQUIRED_BLOCKS[0]["sha256"]
    assert evidence["sourceSha"] == git(admitted["repo"], "rev-parse", "HEAD").stdout.strip()
    assert admission.qualification_from_source_evidence(evidence) is False
    assert admission.replay_covers_governed_documents(evidence) is False


def test_default_inventory_still_refuses_the_same_install(admitted, monkeypatch):
    monkeypatch.setattr(probe, "default_metadata_sites", lambda: [])
    with pytest.raises(ValueError, match="direct-origin installation") as captured:
        probe.inventory([admitted["root"]], [str(admitted["venv"] / "bin/python")])
    assert "file:" not in str(captured.value)
    empty = admitted["root"] / "empty-inventory"
    empty.mkdir(exist_ok=True)
    default = probe.inventory([empty])
    assert set(default) == {"environments", "documentedInterpreters", "packages"}


def test_wrong_checkout_revision_fails_closed(admitted):
    git(admitted["repo"], "commit", "--allow-empty", "-m", "move revision")
    try:
        with pytest.raises(admission.AdmissionRefusal, match="source revision moved"):
            admission.recheck(admitted["binding"])
    finally:
        git(admitted["repo"], "reset", "--hard", "HEAD~1")


def test_changed_metadata_and_backend_inputs_fail_closed(admitted, monkeypatch):
    monkeypatch.setattr(probe, "default_metadata_sites", lambda: [])
    metadata = admitted["repo"] / PACKAGE / "pyproject.toml"
    backend = admitted["repo"] / PACKAGE / "backend.py"
    metadata.write_text(metadata.read_text() + "\n")
    try:
        with pytest.raises(admission.AdmissionRefusal, match="package metadata input changed"):
            admission.recheck(admitted["binding"])
        with pytest.raises(ValueError, match="direct-origin installation") as captured:
            inventory(admitted, admitted["binding"])
        assert str(admitted["repo"]) not in str(captured.value)
    finally:
        git(admitted["repo"], "checkout", "--", f"{PACKAGE}/pyproject.toml")
    backend.write_text(backend.read_text() + "\n")
    try:
        with pytest.raises(admission.AdmissionRefusal, match="build backend input changed"):
            admission.recheck(admitted["binding"])
    finally:
        git(admitted["repo"], "checkout", "--", f"{PACKAGE}/backend.py")


def test_unrelated_same_version_local_install_is_not_the_bound_package(admitted, tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "default_metadata_sites", lambda: [])
    other = tmp_path / "other"
    other.mkdir()
    write_checkout(other, marker="other-checkout")
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    pip_install(environment, "--no-build-isolation", str(other / PACKAGE))
    with pytest.raises(ValueError, match="direct-origin installation") as captured:
        probe.inventory([tmp_path], [str(environment / "bin/python")], source_admission=admitted["binding"])
    assert "other-checkout" not in str(captured.value)
    assert str(other) not in str(captured.value)


def test_origin_escape_and_archive_origin_keep_the_url_out_of_the_failure(admitted, tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "default_metadata_sites", lambda: [])
    origin_path = next(admitted["site"].glob("honua_sdk-0.1.16.dist-info/direct_url.json"))
    original = origin_path.read_text()
    outside = tmp_path / OUTSIDE
    outside.mkdir()
    try:
        origin_path.write_text(json.dumps({"url": f"file://{outside}", "dir_info": {"editable": False}}))
        with pytest.raises(admission.AdmissionRefusal, match="outside the bound checkout") as raw:
            admission.judge_distribution(admitted["binding"], admitted["site"], origin_path.parent)
        assert OUTSIDE not in str(raw.value)
        assert "file:" not in str(raw.value)
        with pytest.raises(ValueError, match="direct-origin installation") as captured:
            inventory(admitted, admitted["binding"])
        assert OUTSIDE not in str(captured.value)
        origin_path.write_text(json.dumps({
            "url": f"file://{outside}/honua.whl", "archive_info": {"hash": "sha256=unadmitted"}}))
        with pytest.raises(admission.AdmissionRefusal, match="archive origins") as archived:
            admission.judge_distribution(admitted["binding"], admitted["site"], origin_path.parent)
        assert OUTSIDE not in str(archived.value)
        origin_path.write_text(json.dumps({
            "url": "https://example.invalid/honua-sdk-python.git",
            "vcs_info": {"vcs": "git", "commit_id": admitted["binding"]["sourceSha"]}}))
        with pytest.raises(admission.AdmissionRefusal, match="not a local checkout path"):
            admission.judge_distribution(admitted["binding"], admitted["site"], origin_path.parent)
    finally:
        origin_path.write_text(original)


def test_symlink_package_path_cannot_escape_the_checkout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_checkout(repo)
    outside = tmp_path / "outside"
    outside.mkdir()
    package = repo / PACKAGE
    escaped = outside / "honua-sdk"
    package.rename(escaped)
    package.symlink_to(escaped)
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "symlink escape")
    with pytest.raises(admission.AdmissionRefusal, match="escapes the bound checkout"):
        binding_for(repo, "wheels/offline_build_pin-1.0.0-py3-none-any.whl",
                    hashlib.sha256((repo / "wheels/offline_build_pin-1.0.0-py3-none-any.whl").read_bytes()).hexdigest())


def test_dependency_ranges_missing_digests_and_absent_installs_fail_closed(admitted, monkeypatch):
    monkeypatch.setattr(probe, "default_metadata_sites", lambda: [])
    with pytest.raises(admission.AdmissionRefusal, match="exact pin"):
        admission.bind(
            admitted["repo"], document="README.md", block_index=1, package_relative=PACKAGE,
            package_name="honua-sdk", package_version="0.1.16",
            metadata_files=[f"{PACKAGE}/pyproject.toml"], backend_files=[f"{PACKAGE}/backend.py"],
            package_files=[{"source": "honua_sdk/__init__.py", "installed": "honua_sdk/__init__.py"}],
            dependencies=[{"name": "offline-build-pin", "version": ">=1.0.0", "sha256": admitted["digest"],
                           "path": admitted["wheel"]}])
    with pytest.raises(admission.AdmissionRefusal, match="immutable digest"):
        admission.bind(
            admitted["repo"], document="README.md", block_index=1, package_relative=PACKAGE,
            package_name="honua-sdk", package_version="0.1.16",
            metadata_files=[f"{PACKAGE}/pyproject.toml"], backend_files=[f"{PACKAGE}/backend.py"],
            package_files=[{"source": "honua_sdk/__init__.py", "installed": "honua_sdk/__init__.py"}],
            dependencies=[{"name": "offline-build-pin", "version": "1.0.0", "path": admitted["wheel"]}])
    with pytest.raises(admission.AdmissionRefusal, match="dependency receipt is missing"):
        admission.bind(
            admitted["repo"], document="README.md", block_index=1, package_relative=PACKAGE,
            package_name="honua-sdk", package_version="0.1.16",
            metadata_files=[f"{PACKAGE}/pyproject.toml"], backend_files=[f"{PACKAGE}/backend.py"],
            package_files=[{"source": "honua_sdk/__init__.py", "installed": "honua_sdk/__init__.py"}],
            dependencies=None)
    broken = json.loads(json.dumps(admitted["binding"]))
    del broken["dependencies"]
    with pytest.raises(admission.AdmissionRefusal, match="missing an immutable input"):
        admission.recheck(broken)
    installed = next(admitted["site"].glob("offline_build_pin-1.0.0.dist-info"))
    hidden = admitted["site"] / "hidden-dependency"
    installed.rename(hidden)
    try:
        with pytest.raises(admission.AdmissionRefusal, match="not installed at the bound version"):
            admission.judge_distribution(admitted["binding"], admitted["site"], next(admitted["site"].glob("honua_sdk-*.dist-info")))
    finally:
        hidden.rename(installed)


def test_unreviewed_block_and_parent_path_are_refused(admitted):
    with pytest.raises(admission.AdmissionRefusal, match="not a reviewed"):
        admission.bind(
            admitted["repo"], document="README.md", block_index=2, package_relative=PACKAGE,
            package_name="honua-sdk", package_version="0.1.16",
            metadata_files=[f"{PACKAGE}/pyproject.toml"], backend_files=[f"{PACKAGE}/backend.py"],
            package_files=[{"source": "honua_sdk/__init__.py", "installed": "honua_sdk/__init__.py"}],
            dependencies=[])
    with pytest.raises(admission.AdmissionRefusal, match="escapes the bound checkout"):
        admission.bind(
            admitted["repo"], document="README.md", block_index=1, package_relative="../outside",
            package_name="honua-sdk", package_version="0.1.16",
            metadata_files=[f"{PACKAGE}/pyproject.toml"], backend_files=[f"{PACKAGE}/backend.py"],
            package_files=[{"source": "honua_sdk/__init__.py", "installed": "honua_sdk/__init__.py"}],
            dependencies=[])


def test_replay_requires_protected_delivery_of_every_governed_document():
    identifiers = admission.governed_document_ids()
    assert len(identifiers) == len(set(identifiers)) > 1
    assert admission.replay_covers_governed_documents({
        "protectedSourceDelivery": True, "exactCandidate": True, "documentIds": identifiers,
    }) is True
    assert admission.replay_covers_governed_documents({
        "protectedSourceDelivery": False, "exactCandidate": True, "documentIds": identifiers,
    }) is False
    assert admission.replay_covers_governed_documents({
        "protectedSourceDelivery": True, "exactCandidate": True, "documentIds": identifiers[:-1],
    }) is False
    assert admission.qualification_from_source_evidence({
        "sourceAdmitted": True, "candidateQualified": True,
    }) is False
