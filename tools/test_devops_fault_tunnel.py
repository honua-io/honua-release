"""Owned loopback source controls only; no AWS/provider/candidate proof."""
import base64
from dataclasses import replace
import socket
import socketserver
import ssl
import subprocess
import threading
import time
from urllib.parse import urlsplit

import pytest

from devops_fault_tunnel import EvidenceEvent, FaultProfile, FaultTunnel, TunnelRefusal


AWS = 'ec2.us-west-2.amazonaws.com'
SAFE = 's3.us-west-2.amazonaws.com'
OPERATION = 'urn:honua:provisioning:' + 'a' * 32
PROFILE = FaultProfile('1' * 64, (AWS, SAFE, 'registry.terraform.io'), (AWS,))


def event(kind, **overrides):
    result = EvidenceEvent(kind, OPERATION, '2' * 64, time.monotonic_ns())
    return replace(result, **overrides)


@pytest.fixture
def echo(tmp_path):
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                   check=True, capture_output=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                with tls.wrap_socket(self.request, server_side=True) as connection:
                    while value := connection.recv(65536):
                        connection.sendall(value)
            except (OSError, ssl.SSLError):
                pass
    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
    server = Server(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05})
    thread.start()
    connections = []
    def connect(address, timeout):
        connections.append(address)
        return socket.create_connection(server.server_address, timeout=timeout)
    yield connect, cert, connections
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def tunnel(echo):
    result = FaultTunnel(PROFILE, connect=echo[0]).start()
    yield result
    # All admissions in this file are explicitly synthetic source fixtures;
    # there is no external executor to interrupt or cloud state to reconcile.
    if not result.snapshot()['closed']:
        if result._admitted is not None and result._terminal is None:
            result.restore(event('executor-terminal'))
        result.close()


def open_connect(tunnel, host=AWS, *, auth=True, method='CONNECT', port=443):
    parsed = urlsplit(tunnel.child_environment()['HTTPS_PROXY'])
    connection = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
    authorization = ''
    if auth:
        token = base64.b64encode((parsed.username + ':' + parsed.password).encode()).decode()
        authorization = f'Proxy-Authorization: Basic {token}\r\n'
    connection.sendall((f'{method} {host}:{port} HTTP/1.1\r\n'
                        + authorization + '\r\n').encode('ascii'))
    response = bytearray()
    while not response.endswith(b'\r\n\r\n'):
        value = connection.recv(1)
        if not value:
            break
        response.extend(value)
    return connection, bytes(response)


def open_tls(tunnel, cert, host=AWS):
    connection, header = open_connect(tunnel, host)
    assert header == b'HTTP/1.1 200 Connection Established\r\n\r\n'
    context = ssl.create_default_context(cafile=str(cert))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    secured = context.wrap_socket(connection, server_hostname='localhost')
    assert secured.version() in ('TLSv1.2', 'TLSv1.3')
    return secured


def exchange(connection, value):
    connection.sendall(value)
    received = bytearray()
    while len(received) < len(value):
        chunk = connection.recv(len(value) - len(received))
        assert chunk
        received.extend(chunk)
    assert bytes(received) == value


def test_actual_end_to_end_tls_bytes_and_normal_dependency_passthrough(tunnel, echo):
    for host in (AWS, 'registry.terraform.io'):
        with open_tls(tunnel, echo[1], host) as connection:
            exchange(connection, b'private-source-control\x00\xff' * 200)
    snapshot = tunnel.snapshot()
    assert snapshot['acceptedConnections'] == 2
    assert snapshot['faultedConnections'] == 0
    assert snapshot['candidateQualified'] is False
    assert snapshot['sixAwsCellsQualified'] is False
    assert 'private-source-control' not in str(snapshot)


def test_arm_drops_existing_and_new_selected_tunnels_but_preserves_other_hosts(tunnel, echo):
    selected = open_tls(tunnel, echo[1])
    safe = open_tls(tunnel, echo[1], SAFE)
    try:
        exchange(selected, b'before')
        tunnel.admit(event('mutation-started'))
        tunnel.arm(event('owned-resource-observed'))
        # The actual selected TCP/TLS connection is cut, not a fabricated API result.
        try:
            assert selected.recv(1) == b''
        except (OSError, ssl.SSLError):
            pass
        connection, response = open_connect(tunnel)
        connection.close()
        assert response == b''
        exchange(safe, b'independent-unselected-provider')
        with pytest.raises(TunnelRefusal, match='not-terminal'):
            tunnel.close()
        assert tunnel.snapshot()['armed'] is True
        tunnel.restore(event('executor-terminal'))
        with open_tls(tunnel, echo[1]) as restored:
            exchange(restored, b'normal-cleanup-after-terminal')
        result = tunnel.snapshot()
        assert result['faultedConnections'] == 2
        assert [row['phase'] for row in result['journal']
                if row['kind'] == 'connection-dropped'] == ['existing', 'new']
    finally:
        selected.close()
        safe.close()


@pytest.mark.parametrize('arguments', [
    {'auth': False}, {'host': 'foreign.amazonaws.com'}, {'method': 'GET'}, {'port': 80},
])
def test_undeclared_or_unowned_connection_never_reaches_upstream(tunnel, echo, arguments):
    connection, response = open_connect(tunnel, **arguments)
    connection.close()
    assert response == b'' and echo[2] == []


def test_lifecycle_refuses_missing_or_reordered_original_event_references(tunnel):
    with pytest.raises(TunnelRefusal, match='arm-state'):
        tunnel.arm(event('owned-resource-observed'))
    with pytest.raises(TunnelRefusal, match='event-kind'):
        tunnel.admit(event('executor-terminal'))
    started = event('mutation-started')
    tunnel.admit(started)
    with pytest.raises(TunnelRefusal, match='resource-order'):
        tunnel.arm(event('owned-resource-observed', operation_id=OPERATION[:-1] + 'b'))
    with pytest.raises(TunnelRefusal, match='resource-order'):
        tunnel.arm(event('owned-resource-observed', monotonic_ns=started.monotonic_ns - 1))
    before_arm = event('executor-terminal')
    tunnel.arm(event('owned-resource-observed'))
    with pytest.raises(TunnelRefusal, match='before-arm'):
        tunnel.restore(before_arm)
    tunnel.restore(event('executor-terminal'))
    with pytest.raises(TunnelRefusal, match='arm-state'):
        tunnel.arm(event('owned-resource-observed'))


@pytest.mark.parametrize('hosts,targets', [
    (('127.0.0.1',), ('127.0.0.1',)),
    ((AWS + ':443',), (AWS + ':443',)),
    (('*.' + AWS,), ('*.' + AWS,)),
    ((AWS,), (SAFE,)),
    ((AWS, AWS), (AWS,)),
    (('registry.terraform.io',), ('registry.terraform.io',)),
    (('sts.us-west-2.amazonaws.com',), ('sts.us-west-2.amazonaws.com',)),
    ((SAFE,), (SAFE,)),
    (('state.s3.us-west-2.amazonaws.com',), ('state.s3.us-west-2.amazonaws.com',)),
    (('kms.us-west-2.amazonaws.com',), ('kms.us-west-2.amazonaws.com',)),
    (('dynamodb.us-west-2.amazonaws.com',), ('dynamodb.us-west-2.amazonaws.com',)),
])
def test_profile_is_finite_and_only_declared_aws_targets_can_fail(hosts, targets):
    with pytest.raises(TunnelRefusal):
        FaultTunnel(FaultProfile('1' * 64, hosts, targets))


def test_private_proxy_credentials_are_not_exported_as_evidence(tunnel):
    environment = tunnel.child_environment()
    assert environment['HTTPS_PROXY'] == environment['https_proxy']
    assert environment['NO_PROXY'] == environment['no_proxy'] == ''
    secret = urlsplit(environment['HTTPS_PROXY']).password
    assert secret not in str(tunnel.snapshot())
    tunnel.close()
    assert tunnel.snapshot()['journal'][-1]['kind'] == 'closed'
    with pytest.raises(TunnelRefusal, match='not-running'):
        tunnel.child_environment()


def test_close_closes_actual_client_still_parsing_an_incomplete_header(tunnel):
    parsed = urlsplit(tunnel.child_environment()['HTTPS_PROXY'])
    with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as connection:
        connection.sendall(b'CONNECT ')
        # Actual request handler must have accepted it before the close race.
        deadline = time.monotonic() + 5
        while not tunnel._clients:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        tunnel.close()
        assert connection.recv(1) == b''


def test_actual_slow_header_has_one_absolute_deadline_not_per_byte_timeout(tunnel):
    parsed = urlsplit(tunnel.child_environment()['HTTPS_PROXY'])
    with socket.create_connection((parsed.hostname, parsed.port), timeout=5) as connection:
        started = time.monotonic()
        connection.sendall(b'C')
        # Each byte arrives well within the socket's original ten-second wait.
        # A per-recv timeout would therefore remain live after this interval.
        for value in b'ONNECT ':
            time.sleep(1)
            connection.sendall(bytes([value]))
        assert connection.recv(1) == b''
        elapsed = time.monotonic() - started
        assert 9 <= elapsed < 13
