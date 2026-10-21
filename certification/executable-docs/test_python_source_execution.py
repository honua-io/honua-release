"""Controlled-source mount boundaries; these do not grant source admission."""
from pathlib import Path
import hashlib
from copy import deepcopy

import pytest

import python_source_execution as execution
from python_source_protocol import SequenceProtocol


def mount_line(path, options):
    encoded = str(path).replace("\\", "\\134").replace(" ", "\\040")
    return f"41 1 0:1 / {encoded} {options} - ext4 /dev/owned rw"


@pytest.mark.parametrize("layout,accepted", [
    ("read-only", True),
    ("writable", False),
    ("stacked-ro-rw", False),
    ("stacked-rw-ro", False),
    ("stacked-ro-ro", True),
    ("nested-writable", False),
    ("nested-read-only", True),
    ("parent-writable-child-read-only", True),
    ("absent", False),
    ("malformed", False),
])
def test_actual_authority_path_checks_all_stacked_and_nested_mounts(tmp_path, monkeypatch, layout, accepted):
    authority = tmp_path / "space in source" / ".git"
    authority.mkdir(parents=True)
    rows = [mount_line(authority, "ro")]
    if layout == "writable": rows = [mount_line(authority, "rw")]
    if layout == "stacked-ro-rw": rows.append(mount_line(authority, "rw"))
    if layout == "stacked-rw-ro": rows.insert(0, mount_line(authority, "rw"))
    if layout == "stacked-ro-ro": rows.append(mount_line(authority, "ro"))
    if layout == "nested-writable": rows.append(mount_line(authority / "objects", "rw"))
    if layout == "nested-read-only": rows.append(mount_line(authority / "objects", "ro"))
    if layout == "parent-writable-child-read-only": rows.insert(0, mount_line(authority.parent, "rw"))
    if layout == "absent": rows = [mount_line(tmp_path, "rw")]
    if layout == "malformed": rows = ["not a mount record"]
    original_read = Path.read_text

    def read(path, *args, **kwargs):
        if path == Path("/proc/self/mountinfo"):
            return "\n".join(rows) + "\n"
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    if accepted:
        execution.require_git_authority(authority.parent)
    else:
        with pytest.raises(execution.ExecutionRefusal):
            execution.require_git_authority(authority.parent)


def test_git_authority_refuses_linked_metadata_before_mount_inspection(tmp_path):
    checkout = tmp_path / "source"
    checkout.mkdir()
    (checkout / ".git").write_text("gitdir: /foreign/metadata\n")
    with pytest.raises(execution.ExecutionRefusal, match="literal mounted directory"):
        execution.require_git_authority(checkout)


@pytest.mark.parametrize("layout", ["absent", "empty", "populated", "file", "symlink"])
def test_only_new_absent_or_literal_empty_bind_target_can_initialize(tmp_path, layout):
    target = tmp_path / "environment"
    if layout in ("empty", "populated"):
        target.mkdir()
    if layout == "populated": (target / "sitecustomize.py").write_text("untrusted")
    if layout == "file": target.write_text("untrusted")
    if layout == "symlink": target.symlink_to(tmp_path, target_is_directory=True)
    if layout in ("absent", "empty"):
        execution.require_new_environment(target)
    else:
        with pytest.raises(execution.ExecutionRefusal, match="newly prepared"):
            execution.require_new_environment(target)


class SequenceFixture:
    """No package installation or artifact admission is represented by this fixture."""

    def __init__(self):
        self.recorded = {"invocations": [{"command": command,
            "commandSha256": hashlib.sha256(command.encode()).hexdigest()} for command in
            ('pip install packages/honua-sdk', 'pip install -e "packages/honua-admin"')]}
        self.calls = []

    def initialize(self):
        self.calls.append("initialize")
        return {"sourceAdmitted": False, "publishedWheelIdentity": False, "candidateQualified": False}

    def install(self, ordinal):
        self.calls.append(("install", ordinal))
        return {"ordinal": ordinal, "originalCommand": self.recorded["invocations"][ordinal]["command"],
                "originalCommandSha256": self.recorded["invocations"][ordinal]["commandSha256"],
                "sourceAdmitted": False, "publishedWheelIdentity": False, "candidateQualified": False}

    def audit(self, ordinal):
        self.calls.append(("audit", ordinal))
        return {}


def test_finite_protocol_retains_order_and_requires_complete_final_audit():
    sequence = SequenceFixture()
    protocol = SequenceProtocol(sequence)
    protocol.request({"operation": "initialize"})
    protocol.request({"operation": "audit"})
    protocol.request({"operation": "install", "argv": ["install", "packages/honua-sdk"]})
    protocol.request({"operation": "install", "argv": ["install", "-e", "packages/honua-admin"]})
    result = protocol.request({"operation": "finish"})
    assert sequence.calls == ["initialize", ("audit", -1), ("install", 0), ("install", 1), ("audit", 1)]
    assert result["completedInvocations"] == 2
    assert all(result[key] is False for key in ("sourceAdmitted", "publishedWheelIdentity", "candidateQualified"))
    with pytest.raises(execution.ExecutionRefusal, match="already finished"):
        protocol.request({"operation": "audit"})


@pytest.mark.parametrize("payload", [
    None, [], {}, {"operation": False}, {"operation": "exec", "code": "foreign"},
    {"operation": "initialize", "command": "foreign"}, {"operation": "audit", "ordinal": 0},
    {"operation": "finish", "sourceAdmitted": True},
])
def test_unknown_protocol_requests_never_execute_operations(payload):
    sequence = SequenceFixture()
    with pytest.raises(execution.ExecutionRefusal):
        SequenceProtocol(sequence).request(payload)
    assert sequence.calls == []


@pytest.mark.parametrize("argv", [
    "install packages/honua-sdk", [False], ["install", "-e", "packages/honua-admin"],
    ["install", "packages/honua-sdk", "foreign"], ["install", "--index-url", "https://foreign.invalid"],
    ["install", "packages/honua-sdk", ";", "foreign"],
])
def test_foreign_reordered_and_expanded_pip_requests_do_not_install(argv):
    sequence = SequenceFixture()
    protocol = SequenceProtocol(sequence)
    protocol.request({"operation": "initialize"})
    with pytest.raises(execution.ExecutionRefusal, match="differs from the next"):
        protocol.request({"operation": "install", "argv": argv})
    assert sequence.calls == ["initialize"]


@pytest.mark.parametrize("operation", ["install", "audit", "finish"])
def test_protocol_cannot_execute_before_initialization(operation):
    sequence = SequenceFixture()
    request = {"operation": operation}
    if operation == "install":
        request["argv"] = ["install", "packages/honua-sdk"]
    with pytest.raises(execution.ExecutionRefusal, match="has not initialized"):
        SequenceProtocol(sequence).request(request)
    assert sequence.calls == []


def test_repeated_initialization_and_omitted_installs_refuse():
    sequence = SequenceFixture()
    protocol = SequenceProtocol(sequence)
    protocol.request({"operation": "initialize"})
    with pytest.raises(execution.ExecutionRefusal, match="repeated"):
        protocol.request({"operation": "initialize"})
    with pytest.raises(execution.ExecutionRefusal, match="omitted"):
        protocol.request({"operation": "finish"})
    assert sequence.calls == ["initialize"]


@pytest.mark.parametrize("field,value", [("ordinal", 9), ("originalCommand", "pip install foreign"),
                                        ("originalCommandSha256", "0" * 64)])
def test_installed_receipt_must_match_original_request(field, value):
    sequence = SequenceFixture()
    install = sequence.install

    def corrupt(ordinal):
        receipt = deepcopy(install(ordinal))
        receipt[field] = value
        return receipt

    sequence.install = corrupt
    protocol = SequenceProtocol(sequence)
    protocol.request({"operation": "initialize"})
    with pytest.raises(execution.ExecutionRefusal, match="installed receipt differs"):
        protocol.request({"operation": "install", "argv": ["install", "packages/honua-sdk"]})
    assert protocol.receipts == []


def test_declared_clone_requires_actual_clone_receipt_before_bootstrap():
    sequence = SequenceFixture()
    sequence.recorded["blockBytes"] = "git clone https://github.com/honua-io/honua-sdk-python.git\n"
    clone_calls = []

    def clone(argv, cwd):
        clone_calls.append((argv, cwd))
        return {"actualCloneReceipt": "fake transport only", "mutableNetworkCloneEquivalent": False}

    sequence.clone = clone
    protocol = SequenceProtocol(sequence)
    with pytest.raises(execution.ExecutionRefusal, match="omitted the actual"):
        protocol.request({"operation": "initialize"})
    with pytest.raises(execution.ExecutionRefusal, match="exact original"):
        protocol.request({"operation": "clone", "argv": ["clone", "https://foreign.invalid"], "cwd": "/owned"})
    assert clone_calls == []
    protocol.request({"operation": "clone", "argv": ["clone", "https://github.com/honua-io/honua-sdk-python.git"], "cwd": "/owned"})
    assert len(clone_calls) == 1
    protocol.request({"operation": "initialize"})
    with pytest.raises(execution.ExecutionRefusal, match="absent, repeated"):
        protocol.request({"operation": "clone", "argv": clone_calls[0][0], "cwd": "/owned"})
