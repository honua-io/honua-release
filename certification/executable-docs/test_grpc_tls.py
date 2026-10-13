"""Real TLS trust controls plus fixture lifecycle checks; the live docs lane proves RPCs."""
import contextlib
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from grpc_tls import CA_ENV, GrpcTlsFixture, TLS_PROBE, required  # noqa: E402


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    root = tmp_path_factory.mktemp("grpc-tls")
    for name in ("server", "other"):
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                        "-keyout", str(root / f"{name}.key"), "-out", str(root / f"{name}.crt")],
                       check=True, capture_output=True)
    return root


@contextlib.contextmanager
def tls_server(certificates, alpn):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificates / "server.crt", certificates / "server.key")
    context.set_alpn_protocols([alpn])
    stop = threading.Event()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.1)

        def serve():
            while not stop.is_set():
                try:
                    client, _ = listener.accept()
                except TimeoutError:
                    continue
                with client:
                    try:
                        with context.wrap_socket(client, server_side=True):
                            pass
                    except ssl.SSLError:
                        pass  # the untrusted client is required to refuse this handshake

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield listener.getsockname()[1]
        finally:
            stop.set()
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.parametrize("alpn,ca,pretrusted,passes", [
    ("h2", "server", False, True),
    ("h2", "other", False, False),
    ("http/1.1", "server", False, False),
    ("h2", "server", True, False),
])
def test_live_tls_requires_isolated_ca_and_http2(certificates, alpn, ca, pretrusted, passes):
    env = dict(os.environ)
    if pretrusted:
        env["SSL_CERT_FILE"] = str(certificates / "server.crt")
    with tls_server(certificates, alpn) as port:
        result = subprocess.run([sys.executable, "-c", TLS_PROBE, str(certificates / f"{ca}.crt"), str(port)],
                                capture_output=True, text=True, env=env, timeout=15)
    assert (result.returncode == 0) is passes, result.stderr
    if passes:
        proof = json.loads(result.stdout)
        assert proof["trustedTls"] and proof["untrustedCaRefused"] and proof["alpn"] == "h2"
    elif pretrusted:
        assert "unexpectedly accepted" in result.stderr
    elif ca == "other":
        assert "CERTIFICATE_VERIFY_FAILED" in result.stderr
    else:
        assert "did not negotiate HTTP/2" in result.stderr


def test_fixture_only_exports_public_ca_and_scopes_trust_to_session(tmp_path, certificates):
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "exec":
            return SimpleNamespace(returncode=0, stderr="", stdout=(certificates / "server.crt").read_text())
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "untrustedCaRefused": True, "trustedTls": True, "alpn": "h2"}))

    fixture = GrpcTlsFixture(docker, tmp_path / "fixture", "container:candidate", "python@sha256:fixture", "test")
    original_env = dict(os.environ)
    fixture.start()
    state = tmp_path / "session"
    state.mkdir()
    env = fixture.session_env(state)
    assert env == {CA_ENV: str(state / "grpc-test-root.crt")}
    assert (state / "grpc-test-root.crt").read_bytes() == (certificates / "server.crt").read_bytes()
    assert dict(os.environ) == original_env
    assert [c[2:] for c in calls if c[0] == "exec"] == [("cat", "/data/caddy/pki/authorities/local/root.crt")]
    assert not any(c[0] == "cp" for c in calls)
    fixture.close()
    assert {c[-1] for c in calls if c[0] == "rm"} == {fixture.name, fixture.probe_name}


def test_fixture_cleans_up_after_failed_trust_control(tmp_path, certificates):
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "exec":
            return SimpleNamespace(returncode=0, stderr="", stdout=(certificates / "server.crt").read_text())
        if "python3" in args:
            raise RuntimeError("certificate verification failed")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    fixture = GrpcTlsFixture(docker, tmp_path, "host", "python@sha256:fixture", "test")
    with pytest.raises(RuntimeError, match="certificate verification failed"):
        fixture.start()
    assert {c[-1] for c in calls if c[0] == "rm"} == {fixture.name, fixture.probe_name}
    with pytest.raises(RuntimeError, match="has not passed"):
        fixture.session_env(tmp_path)


def test_plaintext_variables_do_not_request_tls_trust():
    assert not required({"substitute": {"localhost:8081": {"value": "{candidate.grpcAddress}"}}})
    assert required({"substitute": {"server:443": {"value": "{candidate.grpcTlsAddress}"}}})


def test_cleanup_attempts_sidecar_even_when_probe_removal_raises(tmp_path):
    removed = []

    def docker(*args, **kwargs):
        removed.append(args[-1])
        if args[-1].endswith("-probe"):
            raise TimeoutError("probe cleanup timed out")
        return SimpleNamespace(returncode=0, stderr="")

    fixture = GrpcTlsFixture(docker, tmp_path, "host", "python@sha256:fixture", "test")
    fixture.started = True
    with pytest.raises(RuntimeError, match="probe cleanup timed out"):
        fixture.close()
    assert removed == [fixture.probe_name, fixture.name]


def test_fixture_refuses_private_key_export_and_retains_startup_diagnostics(tmp_path):
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        output = {"exec": "-----BEGIN PRIVATE KEY-----\nfixture-secret\n-----END PRIVATE KEY-----",
                  "inspect": '{"Status":"exited","ExitCode":1}',
                  "logs": "Caddy startup failed"}.get(args[0], "")
        return SimpleNamespace(returncode=0, stderr="", stdout=output)

    fixture = GrpcTlsFixture(docker, tmp_path, "host", "python@sha256:fixture", "test")
    with pytest.raises(RuntimeError, match="invalid public CA") as failure:
        fixture.start()
    assert "Caddy startup failed" in str(failure.value)
    assert "fixture-secret" not in str(failure.value)
    assert not fixture.ca.exists()
    assert {c[-1] for c in calls if c[0] == "rm"} == {fixture.name, fixture.probe_name}
