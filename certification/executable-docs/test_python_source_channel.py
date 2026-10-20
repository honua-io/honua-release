"""Private line and real Unix-socket controls; no container or installed-source proof."""
import base64
from io import BytesIO
import hashlib
import json
from pathlib import Path
import socket
import threading
import tempfile
from types import SimpleNamespace

import pytest

from python_source_channel import ReaderSocket, WorkerChannel
from python_source_execution import ExecutionRefusal


def fixture(tmp_path, mutate=None):
    receipts, wire = tmp_path / "receipts", tmp_path / "wire"
    receipts.mkdir()
    wire.mkdir()
    receipt = receipts / "0000-context-1.json.gz"
    receipt.write_bytes(b"actual private original receipt")
    ready = {"ready": True, "canonicalInnerVerified": True, "innerProposalSha256": "a" * 64,
             "sourceAdmitted": False, "publishedWheelIdentity": False, "candidateQualified": False}
    response = {"serial": 0, "contextIndex": 1, "operation": "initialize", "receipt": receipt.name,
                "receiptSha256": hashlib.sha256(receipt.read_bytes()).hexdigest(),
                "sourceAdmitted": False, "publishedWheelIdentity": False, "candidateQualified": False}
    if mutate:
        mutate(ready, response, receipt)
    ready_raw = json.dumps(ready, indent=None).encode() + b"\n"
    # Preserve the actual response spelling/whitespace, rather than serializing
    # parsed data and presenting those new bytes as the original worker stream.
    response_raw = json.dumps(response, separators=(", ", ":  ")).encode() + b"\n"
    process = SimpleNamespace(stdout=BytesIO(ready_raw + response_raw), stdin=BytesIO())
    return process, receipts, wire, ready_raw, response_raw


def test_original_private_streams_receipt_and_request_are_joined_without_reserialization(tmp_path):
    process, receipts, wire, ready, response = fixture(tmp_path)
    channel = WorkerChannel(process, inner_sha256="a" * 64, receipt_directory=receipts, wire_directory=wire)
    observed = channel.request({"contextIndex": 1, "operation": "initialize"})
    assert observed["receipt"] == "0000-context-1.json.gz"
    assert (wire / "ready.stdout").read_bytes() == ready
    assert (wire / "0000-response.stdout").read_bytes() == response
    assert json.loads(process.stdin.getvalue()) == {"contextIndex": 1, "operation": "initialize"}


@pytest.mark.parametrize("field,value", [
    ("serial", 1), ("serial", False), ("contextIndex", 0), ("operation", "audit"),
    ("receipt", "../../foreign"), ("receiptSha256", "0" * 64),
    ("sourceAdmitted", True), ("candidateQualified", True),
])
def test_foreign_worker_response_never_becomes_an_authenticated_private_receipt(tmp_path, field, value):
    def mutate(ready, response, receipt):
        response[field] = value

    process, receipts, wire, _, response = fixture(tmp_path, mutate)
    channel = WorkerChannel(process, inner_sha256="a" * 64, receipt_directory=receipts, wire_directory=wire)
    with pytest.raises(ExecutionRefusal):
        channel.request({"contextIndex": 1, "operation": "initialize"})
    assert (wire / "0000-response.stdout").read_bytes() == response


def test_real_socket_rejects_duplicate_json_fields_without_calling_broker(tmp_path):
    class Broker:
        def __init__(self):
            self.called = False

        def request(self, request):
            self.called = True
            raise AssertionError("duplicate packet reached privileged dispatch")

    broker = Broker()
    scratch = tempfile.TemporaryDirectory(prefix="honua-sock-")
    path = Path(scratch.name) / "request.sock"
    server = ReaderSocket(path, broker)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(path))
            client.sendall(b'{"operation":"install","operation":"audit"}\n')
            response = bytearray()
            while not response.endswith(b"\n"):
                response.extend(client.recv(4096))
        result = json.loads(response)
        assert result["exitCode"] == 1
        assert b"refused" in base64.b64decode(result["stderrBase64"])
        assert broker.called is False
    finally:
        thread.join()
        server.server_close()
        scratch.cleanup()
