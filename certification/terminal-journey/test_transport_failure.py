"""A candidate request that yields no HTTP status names why, how long it took, and nothing private."""
import re
import socket
import sys
import threading
import urllib.error
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import transport as transport_module
from transport import ExecutionError, Transport

KEY = "private-operator-key"


@contextmanager
def silent_listener():
    """Accepts the connection and reads the request but never answers (a hung Lambda invoke)."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    accepted = []
    done = threading.Event()

    def serve():
        try:
            connection, _ = listener.accept()
        except OSError:
            return
        accepted.append(connection)
        done.wait(5)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        done.set()
        for connection in accepted:
            connection.close()
        listener.close()
        thread.join()


def test_client_timeout_names_exception_elapsed_and_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(transport_module, "HTTP_TIMEOUT_SECONDS", 0.3)
    with silent_listener() as url:
        with pytest.raises(ExecutionError) as raised:
            Transport(url, None, None, tmp_path, {"operator": KEY}).http(
                "POST", "/ogc/styles", principal="operator", body={"version": 8}, expected=(201,))
    reason = raised.value.reason
    assert raised.value.command == "POST /ogc/styles"
    match = re.fullmatch(r'candidate request failed \(exception=TimeoutError; elapsedMs=(\d+); timeoutS=0.3; '
                         r'message="[^"]*"\)', reason)
    assert match, reason
    assert 250 <= int(match.group(1)) < 5000
    assert KEY not in reason


def test_connection_refused_names_the_underlying_reason(tmp_path):
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    with pytest.raises(ExecutionError) as raised:
        Transport(f"http://127.0.0.1:{port}", None, None, tmp_path, {"operator": KEY}).http(
            "GET", "/healthz/ready", principal="operator")
    assert raised.value.reason.startswith("candidate request failed (exception=ConnectionRefusedError; elapsedMs=")


def test_failure_message_is_masked_and_bounded(tmp_path, monkeypatch):
    candidate = Transport("http://127.0.0.1:9", None, None, tmp_path, {"operator": KEY})

    def fail(*_args, **_kwargs):
        raise urllib.error.URLError(f"proxy said token={KEY} " + "x" * 600)

    monkeypatch.setattr(candidate.opener, "open", fail)
    with pytest.raises(ExecutionError) as raised:
        candidate.http("GET", "/healthz/ready", principal="operator")
    reason = raised.value.reason
    assert reason.startswith("candidate request failed (exception=URLError; elapsedMs=")
    assert KEY not in reason and "[redacted]" in reason
    assert reason.endswith('...(truncated)")')
    assert len(reason) < 400


def test_unexpected_status_keeps_its_prefix_and_adds_elapsed(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b'{"message":"Service Unavailable"}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(ExecutionError) as raised:
            Transport(f"http://127.0.0.1:{server.server_port}", None, None, tmp_path, {"operator": KEY}).http(
                "POST", "/ogc/styles", principal="operator", body={"version": 8}, expected=(201,))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert re.fullmatch(r"HTTP 503 \(elapsedMs=\d+\)", raised.value.reason)


def test_client_timeout_outlasts_the_gateway_ceiling():
    # The serverless cell's API Gateway answers 503 at 30 s; the client must still be listening.
    assert transport_module.HTTP_TIMEOUT_SECONDS > 30
