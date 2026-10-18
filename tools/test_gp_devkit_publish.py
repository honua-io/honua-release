"""The publisher rejects foreign candidates before exposing any artifact."""
from copy import deepcopy
import json
from pathlib import Path
import stat
import subprocess
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gp_devkit_artifacts as gate
import gp_devkit_publish as publisher

SHA = "1" * 40


def run():
    return {"id": 123, "run_attempt": 2, "conclusion": "success", "status": "completed",
            "event": "workflow_dispatch", "head_branch": "trunk", "head_sha": SHA,
            "head_repository": {"full_name": publisher.RELEASE_REPOSITORY}, "path": publisher.WORKFLOW,
            "run_started_at": "2026-10-17T12:00:00Z", "updated_at": "2026-10-17T13:00:00Z"}


def artifact():
    return {"id": 456, "name": "gp-devkit-qualified-candidate", "expired": False,
            "workflow_run": {"id": 123, "head_sha": SHA}, "digest": "sha256:" + "2" * 64,
            "size_in_bytes": 1024, "created_at": "2026-10-17T12:30:00Z"}


def test_accepts_only_selected_successful_run():
    publisher.validate_run(run(), 123, 2, SHA)
    publisher.validate_artifact(artifact(), run(), 456, "gp-devkit-qualified-candidate")


@pytest.mark.parametrize("field,value", [("conclusion", "failure"), ("head_branch", "untrusted"),
    ("head_sha", "3" * 40), ("run_attempt", 1), ("event", "pull_request"),
    ("path", ".github/workflows/arbitrary.yml"), ("head_repository", {"full_name": "fork/release"})])
def test_refuses_substituted_or_unsuccessful_run(field, value):
    candidate = run()
    candidate[field] = value
    with pytest.raises(gate.ArtifactError):
        publisher.validate_run(candidate, 123, 2, SHA)


@pytest.mark.parametrize("mutate", [lambda value: value.update(expired=True),
    lambda value: value.update(digest=""), lambda value: value.update(id=999),
    lambda value: value.update(created_at="2026-10-17T11:00:00Z"),
    lambda value: value["workflow_run"].update(id=999),
    lambda value: value["workflow_run"].update(head_sha="3" * 40)])
def test_refuses_expired_foreign_or_previous_attempt_artifact(mutate):
    value = artifact()
    mutate(value)
    with pytest.raises(gate.ArtifactError):
        publisher.validate_artifact(value, run(), 456, "gp-devkit-qualified-candidate")


@pytest.mark.parametrize("name,mode", [("../outside", stat.S_IFREG), ("/absolute", stat.S_IFREG),
    ("link", stat.S_IFLNK), ("fifo", stat.S_IFIFO), ("back\\slash", stat.S_IFREG)])
def test_archive_cannot_write_links_special_files_or_escape(tmp_path, name, mode):
    path = tmp_path / "input.zip"
    with zipfile.ZipFile(path, "w") as archive:
        entry = zipfile.ZipInfo(name)
        entry.external_attr = (mode | 0o600) << 16
        archive.writestr(entry, b"untrusted")
    with pytest.raises(gate.ArtifactError, match="Unsafe"):
        publisher.extract_archive(path, tmp_path / "extracted")
    assert not (tmp_path / "outside").exists()


def test_duplicate_archive_members_are_refused(tmp_path):
    path = tmp_path / "input.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("proof.json", b"first")
        with pytest.warns(UserWarning):
            archive.writestr("proof.json", b"second")
    with pytest.raises(gate.ArtifactError, match="Unsafe"):
        publisher.extract_archive(path, tmp_path / "extracted")


def test_corrupted_download_is_refused_before_extraction(tmp_path, monkeypatch):
    monkeypatch.setattr(publisher, "api", lambda path: artifact())
    def command(args, **kwargs):
        kwargs["stdout"].write(b"substituted archive")
    monkeypatch.setattr(publisher, "command", command)
    with pytest.raises(gate.ArtifactError, match="digest changed"):
        publisher.download_artifact(456, "gp-devkit-qualified-candidate", run(), tmp_path)
    assert not (tmp_path / "gp-devkit-qualified-candidate").exists()


def test_attestation_from_another_attempt_cannot_authorize_publication(tmp_path, monkeypatch):
    statement = [{"verificationResult": {"statement": {"predicate": {"runDetails": {"metadata": {
        "invocationId": "https://github.com/honua-io/honua-release/actions/runs/123/attempts/1"}}}}}}]
    captured = []
    def command(args, **kwargs):
        captured.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(statement), "")
    monkeypatch.setattr(publisher, "command", command)
    with pytest.raises(gate.ArtifactError, match="selected candidate invocation"):
        publisher.verify_attestation(tmp_path / "package.nupkg", SHA, 123, 2)
    assert "--source-digest" in captured[0] and SHA in captured[0]
    assert "--deny-self-hosted-runners" in captured[0]


def test_unmerged_source_cannot_publish(monkeypatch):
    monkeypatch.setattr(publisher, "api", lambda path: {"status": "diverged"})
    with pytest.raises(gate.ArtifactError, match="not merged"):
        publisher.require_merged(publisher.SERVER_REPOSITORY, SHA)

def test_partial_release_verifies_existing_bytes_before_adding_missing_assets(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_bytes(b"first"); second.write_bytes(b"second")
    before = {"assets": [{"name": "first", "id": 1}]}
    after = {"assets": [{"name": "first", "id": 1}, {"name": "second", "id": 2}]}
    states = iter([before, after, after])
    monkeypatch.setattr(publisher, "release_status", lambda *_: next(states))
    operations = []
    monkeypatch.setattr(publisher, "verify_release_asset", lambda asset, path: operations.append(("verify", path.name)))
    monkeypatch.setattr(publisher, "command", lambda args, **_: operations.append(("upload", args[-1])))
    publisher.ensure_release_assets("tag", SHA, "1.0.0", [first, second])
    assert operations == [("verify", "first"), ("upload", str(second)), ("verify", "second"), ("verify", "first"), ("verify", "second")]


def test_foreign_existing_asset_prevents_every_upload(tmp_path, monkeypatch):
    path = tmp_path / "expected"; path.write_bytes(b"qualified")
    monkeypatch.setattr(publisher, "release_status", lambda *_: {"assets": [{"name": "foreign"}]})
    mutation = []
    monkeypatch.setattr(publisher, "command", lambda *args, **kwargs: mutation.append(args))
    with pytest.raises(gate.ArtifactError, match="foreign"):
        publisher.ensure_release_assets("tag", SHA, "1.0.0", [path])
    assert mutation == []


def test_lost_upload_response_reconciles_only_matching_existing_bytes(tmp_path, monkeypatch):
    path = tmp_path / "expected"; path.write_bytes(b"qualified")
    states = iter([{"assets": []}, {"assets": [{"name": "expected", "id": 1}]}, {"assets": [{"name": "expected", "id": 1}]}])
    monkeypatch.setattr(publisher, "release_status", lambda *_: next(states))
    def lost_response(args, **kwargs):
        assert "--clobber" not in args
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(publisher, "command", lost_response)
    verified = []
    monkeypatch.setattr(publisher, "verify_release_asset", lambda asset, selected: verified.append(selected))
    publisher.ensure_release_assets("tag", SHA, "1.0.0", [path])
    assert verified == [path, path]


def test_lost_create_response_reconciles_selected_release(tmp_path, monkeypatch):
    states = iter([None, {"assets": []}, {"assets": []}, {"assets": []}])
    monkeypatch.setattr(publisher, "release_status", lambda *_: next(states))
    calls = []
    def lost_response(args, **kwargs):
        calls.append(args)
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(publisher, "command", lost_response)
    publisher.ensure_release_assets("tag", SHA, "1.0.0", [])
    assert len(calls) == 1 and "create" in calls[0]


@pytest.mark.parametrize("field,value", [("target_commitish", "2" * 40), ("tag_name", "foreign"), ("draft", True), ("prerelease", True)])
def test_existing_release_identity_cannot_be_retargeted(monkeypatch, field, value):
    release = {"target_commitish": SHA, "tag_name": "tag", "draft": False, "prerelease": False}
    release[field] = value
    monkeypatch.setattr(publisher.subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps(release), ""))
    with pytest.raises(gate.ArtifactError, match="exact qualified source"):
        publisher.release_status("tag", SHA)


def test_existing_release_asset_actual_bytes_must_match(tmp_path, monkeypatch):
    path = tmp_path / "expected"; path.write_bytes(b"qualified")
    def wrong_bytes(args, **kwargs):
        kwargs["stdout"].write(b"different")
    monkeypatch.setattr(publisher, "command", wrong_bytes)
    with pytest.raises(gate.ArtifactError, match="bytes differ"):
        publisher.verify_release_asset({"id": 1, "state": "uploaded", "size": len(b"qualified")}, path)

@pytest.mark.parametrize("different", [False, True])
def test_registry_resume_reuses_only_qualified_config_and_exact_manifest(monkeypatch, different):
    import hashlib
    config = "sha256:" + "2" * 64
    candidate = {"schema": "honua.gp.devkit-candidate/v1", "version": "1.2.3", "imageConfigDigest": config,
                 "package": {"file": "tool.nupkg"}}
    raw = json.dumps({"mediaType": "application/vnd.oci.image.manifest.v1+json", "config": {"digest": "sha256:" + "3" * 64 if different else config}}).encode()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(publisher.subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 0, raw, b""))
    monkeypatch.setattr(publisher, "command", lambda args, **kwargs: subprocess.CompletedProcess(args, 0, json.dumps({"digest": digest}), ""))
    if different:
        with pytest.raises(gate.ArtifactError, match="not the locally qualified"):
            publisher.registry_identity(candidate, "tag")
    else:
        published, manifest = publisher.registry_identity(candidate, "tag")
        assert manifest == raw and published["sandboxImage"].endswith(digest)


def test_registry_permission_failure_is_not_missing_image(monkeypatch):
    monkeypatch.setattr(publisher.subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 1, b"", b"unauthorized: manifest unknown"))
    with pytest.raises(gate.ArtifactError, match="cannot be checked"):
        publisher.registry_identity({}, "tag")


@pytest.mark.parametrize("error", [b'credential helper not found', b'network endpoint not found', b'unknown registry response'])
def test_registry_unverifiable_error_does_not_authorize_push(monkeypatch, error):
    monkeypatch.setattr(publisher.subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 1, b"", error))
    with pytest.raises(gate.ArtifactError, match="cannot be checked"):
        publisher.registry_identity({}, "tag")


@pytest.mark.parametrize("error", [b'manifest unknown', b'no such manifest', b'ERROR: ghcr.io/honua-io/honua-gp-workspace:tag: not found'])
def test_registry_recognizes_only_concrete_missing_manifest(monkeypatch, error):
    monkeypatch.setattr(publisher.subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 1, b"", error))
    assert publisher.registry_identity({}, "ghcr.io/honua-io/honua-gp-workspace:tag") is None


def test_existing_release_without_verified_image_never_authorizes_registry_mutation(monkeypatch):
    monkeypatch.setattr(publisher, "release_status", lambda *_: {"id": 1})
    monkeypatch.setattr(publisher, "registry_identity", lambda *_: None)
    operations = []
    monkeypatch.setattr(publisher, "command", lambda *args, **kwargs: operations.append(args))
    with pytest.raises(gate.ArtifactError, match="no verifiable matching image"):
        publisher.publication_preflight({"sourceSha": SHA}, "tag", "image:tag")
    assert not operations


def test_existing_image_without_release_is_a_recoverable_own_partial_state(monkeypatch):
    identity = ({"sandboxImage": "image@sha256:" + "b" * 64}, b'manifest')
    monkeypatch.setattr(publisher, "release_status", lambda *_: None)
    monkeypatch.setattr(publisher, "registry_identity", lambda *_: identity)
    assert publisher.publication_preflight({"sourceSha": SHA}, "tag", "image:tag") == identity
