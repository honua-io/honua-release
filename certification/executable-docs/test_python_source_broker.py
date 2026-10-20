"""Finite bridge controls; fake transport is not runtime or source admission."""
import base64
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from python_source_broker import BlockBroker, validate_original_phases
from python_source_execution import ExecutionRefusal


def fixture():
    calls = []
    response = {"contextIndex": 1, "operation": "install", "completedInvocations": 1,
                "stdoutBase64": base64.b64encode(b"actual pip stdout\n").decode(),
                "stdoutSha256": hashlib.sha256(b"actual pip stdout\n").hexdigest(),
                "successfulStderrCaptured": False, "sourceAdmitted": False,
                "publishedWheelIdentity": False, "candidateQualified": False}

    def dispatch(value):
        calls.append(deepcopy(value))
        if value["operation"] == "finish":
            return {**deepcopy(response), "operation": "finish"}
        return deepcopy(response)

    broker = BlockBroker(context_index=1, block_sha256="a" * 64, checkout="/owned/source",
        invocations=[{"command": 'pip install -e "packages/honua-sdk[grpc,geopandas]"'}], dispatch=dispatch)
    payload = {"operation": "install", "contextIndex": 1, "blockSha256": "a" * 64,
               "argv": ["install", "-e", "packages/honua-sdk[grpc,geopandas]"], "cwd": "/owned/source"}
    return broker, calls, response, payload


def test_bridge_preserves_actual_stdout_and_does_not_claim_successful_stderr_capture():
    broker, calls, _, payload = fixture()
    result = broker.request(payload)
    assert base64.b64decode(result["stdoutBase64"]) == b"actual pip stdout\n"
    assert result["successfulStderrCaptured"] is False
    assert calls == [{"contextIndex": 1, "operation": "install", "argv": payload["argv"]}]
    assert broker.ordinal == 1
    broker.finish()
    assert calls[-1] == {"contextIndex": 1, "operation": "finish"}
    with pytest.raises(ExecutionRefusal):
        broker.finish()


@pytest.mark.parametrize("field,value", [
    ("operation", "clone"), ("contextIndex", 0), ("contextIndex", True),
    ("blockSha256", "b" * 64), ("cwd", "/foreign/source"),
    ("cwd", "/owned/source/.."), ("argv", ["install", "foreign"]),
    ("argv", "install -e packages/honua-sdk[grpc,geopandas]"),
])
def test_foreign_context_command_and_cwd_never_enter_private_controller(field, value):
    broker, calls, _, payload = fixture()
    payload[field] = value
    with pytest.raises(ExecutionRefusal):
        broker.request(payload)
    assert calls == []
    with pytest.raises(ExecutionRefusal, match="already refused"):
        broker.request(fixture()[-1])


@pytest.mark.parametrize("field,value", [
    ("contextIndex", 0), ("contextIndex", True), ("operation", "audit"),
    ("completedInvocations", 2), ("completedInvocations", True),
    ("sourceAdmitted", True), ("publishedWheelIdentity", True), ("candidateQualified", True),
    ("stdoutSha256", "0" * 64), ("stdoutBase64", "not base64"),
    ("successfulStderrCaptured", True),
])
def test_unjoined_controller_response_cannot_advance_original_sequence(field, value):
    broker, calls, response, payload = fixture()
    response[field] = value
    with pytest.raises(ExecutionRefusal):
        broker.request(payload)
    assert len(calls) == 1
    assert broker.ordinal == 0


def test_omitted_repeated_or_extra_requests_cannot_complete_block():
    broker, _, _, payload = fixture()
    with pytest.raises(ExecutionRefusal, match="omitted"):
        broker.finish()
    broker.request(payload)
    with pytest.raises(ExecutionRefusal, match="exceeded"):
        broker.request(payload)
    with pytest.raises(ExecutionRefusal, match="refused"):
        broker.finish()


def test_declared_clone_joins_real_streams_and_initializes_before_first_install():
    _, _, response, _ = fixture()
    calls = []

    def dispatch(request):
        calls.append(request)
        result = {**response, "operation": request["operation"], "completedInvocations": 0}
        if request["operation"] == "clone":
            result.update(mutableNetworkCloneEquivalent=False, stderrBase64="",
                          stderrSha256=hashlib.sha256(b"").hexdigest())
        return result

    broker = BlockBroker(context_index=1, block_sha256="a" * 64, checkout="/owned/honua-sdk-python",
        invocations=[{"command": "pip install packages/honua-sdk"}], dispatch=dispatch, clone_required=True)
    payload = {"operation": "clone", "contextIndex": 1, "blockSha256": "a" * 64,
               "argv": ["clone", "https://github.com/honua-io/honua-sdk-python.git"], "cwd": "/owned"}
    result = broker.request(payload)
    assert [row["operation"] for row in calls] == ["clone", "initialize"]
    assert result["successfulStderrCaptured"] is True
    assert broker.cloned
    with pytest.raises(ExecutionRefusal, match="original position"):
        broker.request(payload)


def test_all_three_actual_selected_blocks_have_a_finite_pip_phase_before_untrusted_commands():
    root = Path(__file__).parent / "python-source-inputs"
    fixture = json.loads((root / "document-source-blocks.json").read_bytes())
    selected = json.loads((root / "selected.json").read_bytes())
    assert fixture["revision"] == selected["subject"]["sourceSha"]
    counts = []
    for row, binding in zip(fixture["blocks"], selected["documentation"], strict=True):
        assert row["document"] == binding["document"]
        assert row["documentGitBlob"] == binding["documentGitBlob"]
        assert row["documentSha256"] == binding["documentSha256"]
        assert row["index"] == binding["index"]
        assert hashlib.sha256(row["code"].encode()).hexdigest() == row["sha256"] == binding["sha256"]
        invocations = [{"command": line} for line in row["code"].splitlines() if line.startswith("pip install ")]
        observed = validate_original_phases(row["code"], invocations)
        assert observed["cloneRequired"] is (row["index"] == 1)
        assert observed["finalAuditRequiresTerminalReader"] is True
        counts.append(observed["installCount"])
    assert counts == [3, 3, 6]


@pytest.mark.parametrize("code", [
    "python -c 'modify_environment()'\npip install packages/honua-sdk\n",
    "export PYTHONPATH=/foreign\npip install packages/honua-sdk\n",
    "pip install packages/honua-sdk\npython mutate.py\npip install packages/honua-admin\n",
    "git clone https://foreign.invalid\npip install packages/honua-sdk\n",
    "git clone https://github.com/honua-io/honua-sdk-python.git\ncd honua-sdk-python && python mutate.py\npip install packages/honua-sdk\n",
])
def test_unsafe_original_phase_is_refused_before_any_controller_interpreter_execution(code):
    invocations = [{"command": line} for line in code.splitlines() if line.startswith("pip install ")]
    with pytest.raises(ExecutionRefusal):
        validate_original_phases(code, invocations)
