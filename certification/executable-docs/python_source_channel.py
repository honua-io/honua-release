"""Private line transport and finite reader socket; no document-supplied controller code."""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import socketserver
import threading

from python_source_execution import ExecutionRefusal


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ExecutionRefusal("controlled packet has duplicate fields")
        result[key] = value
    return result


def _false_flags(result):
    return all(result.get(key) is False for key in
               ("sourceAdmitted", "publishedWheelIdentity", "candidateQualified"))


def _sha_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class WorkerChannel:
    """The caller owns the actual attached process and terminal/nonce cleanup.

    No observer timeout or close/kill operation is provided here. Document code
    receives only the separate finite BlockBroker socket, never these streams.
    """

    def __init__(self, process, *, inner_sha256, receipt_directory, wire_directory):
        self.process = process
        self.receipts = Path(receipt_directory)
        self.wire = Path(wire_directory)
        self.serial = 0
        self.lock = threading.Lock()
        ready = self._read(self.wire / "ready.stdout")
        if (not isinstance(ready, dict) or ready.get("ready") is not True
                or ready.get("canonicalInnerVerified") is not True
                or ready.get("innerProposalSha256") != inner_sha256 or not _false_flags(ready)):
            raise ExecutionRefusal("private worker did not verify the actual original inner proposal")
        self.ready = ready

    def _read(self, destination):
        raw = self.process.stdout.readline(4 * 1024 * 1024 + 1)
        with destination.open("xb") as output:
            output.write(raw)
        if len(raw) > 4 * 1024 * 1024 or not raw.endswith(b"\n"):
            raise ExecutionRefusal("private worker stream ended or exceeded its finite bound")
        return json.loads(raw, object_pairs_hook=unique)

    def request(self, request):
        with self.lock:
            raw = json.dumps(request, sort_keys=True).encode() + b"\n"
            if len(raw) > 64 * 1024:
                raise ExecutionRefusal("private worker request exceeded its finite bound")
            with (self.wire / f"{self.serial:04d}-request.json").open("xb") as output:
                output.write(raw)
            self.process.stdin.write(raw)
            self.process.stdin.flush()
            result = self._read(self.wire / f"{self.serial:04d}-response.stdout")
            if (not isinstance(result, dict) or type(result.get("serial")) is not int
                    or result["serial"] != self.serial or type(result.get("contextIndex")) is not int
                    or result["contextIndex"] != request["contextIndex"]
                    or result.get("operation") != request["operation"] or not _false_flags(result)):
                raise ExecutionRefusal("private worker response is not joined to the original request")
            expected = f"{self.serial:04d}-context-{request['contextIndex']}.json.gz"
            receipt = self.receipts / expected
            if (result.get("receipt") != expected or receipt.is_symlink() or not receipt.is_file()
                    or _sha_file(receipt) != result.get("receiptSha256")):
                raise ExecutionRefusal("private worker original receipt bytes differ")
            self.serial += 1
            return result


class ReaderSocket(socketserver.UnixStreamServer):
    """One live context, one serialized handler; caller closes after actual reader exit."""

    allow_reuse_address = False

    def __init__(self, path, broker):
        self.broker = broker
        super().__init__(str(path), _ReaderRequest)
        Path(path).chmod(0o660)


class _ReaderRequest(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            raw = self.rfile.readline(64 * 1024 + 1)
            if len(raw) > 64 * 1024 or not raw.endswith(b"\n"):
                raise ExecutionRefusal("reader request exceeded its finite bound")
            packet = json.loads(raw, object_pairs_hook=unique)
            response = self.server.broker.request(packet)
        except Exception:
            # Rejected arbitrary payloads and credentialized URLs never enter
            # public diagnostics or a privileged controller request.
            error = b"controlled source request refused\n"
            response = {"exitCode": 1, "stdoutBase64": "", "stdoutSha256": hashlib.sha256(b"").hexdigest(),
                "stderrBase64": base64.b64encode(error).decode(), "stderrSha256": hashlib.sha256(error).hexdigest(),
                "successfulStderrCaptured": False}
        raw = json.dumps(response, sort_keys=True).encode() + b"\n"
        if len(raw) > 4 * 1024 * 1024:
            raise ExecutionRefusal("reader response exceeded its finite bound")
        self.wfile.write(raw)
        self.wfile.flush()
