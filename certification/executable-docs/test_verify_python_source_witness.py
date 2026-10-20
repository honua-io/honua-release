from copy import deepcopy
import json
from pathlib import Path

import pytest

import python_source_inputs as inputs
import verify_python_source_witness as verifier


def context():
    selection, _ = inputs.selected_inputs()
    chain = {"configDigest": selection["image"]["configDigest"]}
    value = {"mountSources": {"implementation": "/tmp/honua-governed-inputs-abc123/implementation",
                              "subject": "/tmp/honua-governed-inputs-abc123/subject",
                              "tool": "/tmp/honua-implementation-tool-def456/packaging-26.3-py3-none-any.whl",
                              "output": "/home/runner/work/_temp/governed-development-inputs"},
             "user": "1001:1001", "tmpfs": "/tmp:rw,exec,mode=0700,uid=1001,gid=1001",
             "image": {"id": chain["configDigest"], "repoDigests": ["python@" + selection["image"]["indexDigest"]],
                       "declaredImage": "python:3.12-bookworm@" + selection["image"]["indexDigest"],
                       "os": "linux", "architecture": "amd64"}}
    return value, selection, chain


def test_authenticates_original_request_with_distinct_config_and_index_ids():
    verifier.authenticated_context(*context())


def test_actual_committed_oci_bytes_join_the_witness_selection():
    selection, _ = inputs.selected_inputs()
    chain = verifier.selected_image_chain(selection, inputs.SELECTION.parent)
    assert chain["platformManifestDigest"] == selection["image"]["manifestDigest"]
    assert chain["configDigest"] == selection["image"]["configDigest"]
    assert chain["indexDigest"] != chain["configDigest"]


@pytest.mark.parametrize("field", ["manifestDigest", "configDigest"])
def test_actual_oci_chain_refuses_different_selected_platform_or_config(field):
    selection, _ = inputs.selected_inputs()
    selection["image"][field] = "sha256:" + "0" * 64
    with pytest.raises(inputs.InputRefusal):
        verifier.selected_image_chain(selection, inputs.SELECTION.parent)


@pytest.mark.parametrize("field", ["source", "subject", "tool", "output", "user", "tmpfs", "image-id", "image-platform", "extra"])
def test_context_refuses_coherent_observations_outside_original_launch_contract(field):
    value, selection, chain = deepcopy(context())
    if field == "source": value["mountSources"]["implementation"] = "/tmp/foreign/implementation"
    if field == "subject": value["mountSources"]["subject"] = "/tmp/honua-governed-inputs-other/subject"
    if field == "tool": value["mountSources"]["tool"] = "/tmp/honua-implementation-tool-def456/packaging-26.4-py3-none-any.whl"
    if field == "output": value["mountSources"]["output"] += "-foreign"
    if field == "user": value["user"] = "0:0"
    if field == "tmpfs": value["tmpfs"] = "/tmp:rw,exec,mode=0777"
    if field == "image-id": value["image"]["id"] = selection["image"]["indexDigest"]
    if field == "image-platform": value["image"]["architecture"] = "arm64"
    if field == "extra": value["unreviewed"] = True
    with pytest.raises(inputs.InputRefusal):
        verifier.authenticated_context(value, selection, chain)


@pytest.mark.parametrize("fault", ["inner-refusal", "user", "tmpfs", "extra-write", "container-id", "network"])
def test_inner_uses_protected_projection_validator_with_real_shaped_tmpfs(tmp_path, monkeypatch, fault):
    _, selection, _ = context()
    originals, implementation, subject = [tmp_path / name for name in ("originals", "implementation", "subject")]
    tool = originals / "proposal/build/packaging.whl"
    tool.parent.mkdir(parents=True)
    tool.write_bytes(b"exact tool")
    policy = {"executionBinding": {"implementationPackagingFilename": tool.name,
                                  "implementationPackagingSha256": inputs.sha_file(tool)}}
    container, calls, snapshots = "a" * 64, [], []

    class Launcher:
        validations = []

        @staticmethod
        def validate_container(observed, expected_id, *, owner, image_id, arguments, mounts, user, tmpfs):
            # Boundary stand-in: assert the actual reader delegates its complete
            # request to the protected API, including the daemon's tmpfs row.
            Launcher.validations.append(deepcopy(observed))
            assert expected_id == container
            assert owner == snapshots[0]["ownerNonce"]
            assert image_id == selection["image"]["configDigest"]
            assert arguments == snapshots[0]["args-with-path"]
            assert len(mounts) == 4 and all(readonly is True for _, _, readonly in mounts)
            temporary = [row for row in observed["mounts"] if row["Type"] == "tmpfs"]
            assert temporary == [{"Type": "tmpfs", "Source": "", "Destination": "/tmp", "RW": True}]
            if (observed["id"] != expected_id or observed["user"] != user
                    or observed["tmpfs"] != {"/tmp": tmpfs.split(":", 1)[1]}
                    or len([row for row in observed["mounts"] if row["Type"] == "bind"]) != len(mounts)):
                raise ValueError("actual protected container projection refuses")

        @staticmethod
        def inspect_container(value):
            assert value == container
            state = deepcopy(snapshots[0] if len(snapshots) == 1 else snapshots[-1])
            return state

    def check_output(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["image", "inspect"]:
            return json.dumps({"Id": selection["image"]["configDigest"], "RepoDigests": ["python@" + selection["image"]["indexDigest"]],
                               "Os": "linux", "Architecture": "amd64"}).encode()
        if command[1] == "inspect":
            assert command == ["docker", "inspect", "--format", "{{json .HostConfig.NetworkMode}}", container]
            return b'"bridge"' if fault == "network" else b'"none"'
        if command[1] == "create":
            index = command.index("--label")
            owner = command[index + 1].split("=", 1)[1]
            image_index = command.index("python:3.12-bookworm@" + selection["image"]["indexDigest"])
            arguments = command[image_index + 1:]
            mounts = []
            for ordinal, item in enumerate(command):
                if item == "--mount":
                    parts = dict(part.split("=", 1) for part in command[ordinal + 1].split(",") if "=" in part)
                    mounts.append({"Type": "bind", "Source": parts["src"], "Destination": parts["dst"], "RW": False})
            mounts.append({"Type": "tmpfs", "Source": "", "Destination": "/tmp", "RW": True})
            snapshots.append({"id": container, "ownerNonce": owner, "imageId": selection["image"]["configDigest"], "status": "created",
                              "readonlyRootfs": True, "path": arguments[0], "args": arguments[1:], "args-with-path": arguments,
                              "user": command[command.index("--user") + 1], "tmpfs": {"/tmp": command[command.index("--tmpfs") + 1].split(":", 1)[1]},
                              "mounts": mounts, "running": False})
            if fault == "user": snapshots[0]["user"] = "foreign:foreign"
            if fault == "tmpfs": snapshots[0]["tmpfs"] = {"/tmp": "rw,mode=0777"}
            if fault == "extra-write": snapshots[0]["mounts"].append({"Type": "bind", "Source": "/foreign", "Destination": "/foreign", "RW": True})
            if fault == "container-id": snapshots[0]["id"] = "b" * 64
            assert arguments[arguments.index("--action") + 1] == "verify"
            assert "GH_TOKEN" not in " ".join(command)
            assert command[command.index("--network") + 1] == "none"
            return container + "\n"
        assert command == ["docker", "rm", container]
        return container + "\n"

    def run(command, **kwargs):
        calls.append(command)
        assert command == ["docker", "start", "--attach", container]
        kwargs["stdout"].write(b"original stdout\n")
        kwargs["stderr"].write(b"actual command or interpreter environment differs\n")
        snapshots.append({**snapshots[-1], "status": "exited", "exitCode": 1})
        return type("Terminal", (), {"returncode": 1})()

    monkeypatch.setattr(verifier.subprocess, "check_output", check_output)
    monkeypatch.setattr(verifier.subprocess, "run", run)
    with pytest.raises(ValueError):
        verifier.run_inner(tmp_path, originals, implementation, subject, selection, policy, Launcher)
    assert Launcher.validations
    if fault == "inner-refusal":
        assert len(Launcher.validations) == 2
        assert (tmp_path / "inner.stderr").read_bytes() == b"actual command or interpreter environment differs\n"
        assert json.loads((tmp_path / "inner-verification.json").read_bytes())["exitCode"] == 1
    else:
        assert not (tmp_path / "inner.stderr").exists()
    assert json.loads((tmp_path / "owned-cleanup.json").read_bytes()) == {"containerId": container, "removed": True}
    assert calls[-1] == ["docker", "rm", container]
