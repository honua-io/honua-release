"""Request-owned CONNECT transport for the existing AWS recovery scenarios.

This is a mechanical network fault, not an AWS emulator or evidence authority.
TLS bytes pass unchanged. Only the native child's explicit proxy environment
uses the tunnel; independent observers must not use it. The coordinator must
authenticate the original mutation/resource/terminal events before calling the
lifecycle methods and independently rejudge them afterward. Journal entries
alone never establish resource creation, executor quiescence, or qualification.

There is deliberately no context-manager/finalizer that restores the network
or kills an admitted executor when an observer times out. The owner retains this
handle until the executor's actual terminal event. No cloud commands run here.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hmac
import re
import secrets
import select
import socket
import socketserver
import threading
import time


class TunnelRefusal(ValueError):
    """Fixed-code error; no header, credential, or TLS payload is interpolated."""


def require(condition, code):
    if not condition:
        raise TunnelRefusal(code)


@dataclass(frozen=True)
class EvidenceEvent:
    """Reference to an original in the coordinator's authenticated event store."""
    kind: str
    operation_id: str
    original_sha256: str
    monotonic_ns: int


def check_event(event, kind):
    require(type(event) is EvidenceEvent and event.kind == kind, 'fault-event-kind')
    require(re.fullmatch(r'urn:honua:provisioning:[0-9a-f]{32}', event.operation_id)
            is not None, 'fault-event-operation')
    require(re.fullmatch(r'[0-9a-f]{64}', event.original_sha256) is not None
            and type(event.monotonic_ns) is int and event.monotonic_ns > 0,
            'fault-event-original')


@dataclass(frozen=True)
class FaultProfile:
    """Finite endpoints from the trusted reviewed request, never tool arguments."""
    context_sha256: str
    allowed_hosts: tuple[str, ...]
    fault_hosts: tuple[str, ...]

    def validate(self):
        require(re.fullmatch(r'[0-9a-f]{64}', self.context_sha256) is not None,
                'fault-context')
        require(type(self.allowed_hosts) is tuple and type(self.fault_hosts) is tuple
                and 0 < len(self.allowed_hosts) <= 64 and 0 < len(self.fault_hosts) <= 8
                and len(set(self.allowed_hosts)) == len(self.allowed_hosts)
                and len(set(self.fault_hosts)) == len(self.fault_hosts)
                and set(self.fault_hosts) <= set(self.allowed_hosts), 'fault-host-closure')
        # Normal provider/CLI downloads must also pass through. The finite
        # reviewed context declares those exact dependency hosts. Faults apply
        # only to its AWS subset. No wildcard, URL, IP, or alternate port.
        label = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
        endpoint = rf'(?:{label}\.)+[a-z]{{2,63}}'
        require(all(type(host) is str and len(host) <= 253
                    and re.fullmatch(endpoint, host) is not None
                    for host in self.allowed_hosts), 'fault-host-shape')
        # This concrete profile faults EC2 only. Never cut STS, KMS, state S3
        # (including bucket aliases), or DynamoDB locking/approval authority.
        require(all(re.fullmatch(r'ec2\.[a-z]{2}(?:-[a-z]+)+-[0-9]\.amazonaws\.com', host)
                    is not None for host in self.fault_hosts), 'fault-target-not-provider')


class FaultTunnel:
    """Explicit start -> admit -> arm -> restore -> close lifecycle.

    ``connect`` exists for source controls using owned loopback sockets; the
    production coordinator must use the default socket connector. It cannot be
    selected through a serialized profile or external command.
    """
    def __init__(self, profile: FaultProfile, *, connect=socket.create_connection):
        profile.validate()
        self.profile = profile
        self._connect = connect
        self._lock = threading.RLock()
        self._nonce = secrets.token_hex(32)
        self._auth = 'Basic ' + base64.b64encode(
            (self._nonce + ':' + self._nonce).encode('ascii')).decode('ascii')
        self._journal = []
        self._exhausted = False
        self._armed = False
        self._closed = False
        self._admitted = None
        self._terminal = None
        self._armed_at = None
        self._sockets = {}  # active client -> (upstream, host)
        self._clients = set()  # includes accepted clients still parsing headers
        self._drops = 0
        self._connections = 0
        self._server = None
        self._thread = None

    def _record(self, kind, **fields):
        # No raw request headers, URL credentials, payloads, or exception text.
        if len(self._journal) >= 4096:
            self._exhausted = True
            return
        self._journal.append({'ordinal': len(self._journal), 'kind': kind,
                              'monotonicNs': time.monotonic_ns(), **fields})

    def start(self):
        with self._lock:
            require(self._server is None and not self._closed, 'fault-start-state')
            owner = self
            class Handler(socketserver.BaseRequestHandler):
                def handle(self):
                    owner._handle(self.request)
            class Server(socketserver.ThreadingTCPServer):
                daemon_threads = True
                block_on_close = False
                slots = threading.BoundedSemaphore(32)

                def process_request(self, request, client_address):
                    if not self.slots.acquire(blocking=False):
                        self.shutdown_request(request)
                        return
                    try:
                        super().process_request(request, client_address)
                    except BaseException:
                        self.slots.release()
                        raise

                def process_request_thread(self, request, client_address):
                    try:
                        super().process_request_thread(request, client_address)
                    finally:
                        self.slots.release()
            self._server = Server(('127.0.0.1', 0), Handler)
            self._thread = threading.Thread(target=self._server.serve_forever,
                                            kwargs={'poll_interval': 0.1}, daemon=True)
            self._thread.start()
            self._record('started', contextSha256=self.profile.context_sha256)
            return self

    def child_environment(self):
        """Private overrides only; never print/publish their credentialed value."""
        with self._lock:
            require(self._server is not None and not self._closed, 'fault-not-running')
            port = self._server.server_address[1]
            url = f'http://{self._nonce}:{self._nonce}@127.0.0.1:{port}'
            # Clear inherited bypasses in this child only. Do not touch os.environ
            # or the observer's AWS client/backend configuration.
            return {'HTTPS_PROXY': url, 'https_proxy': url, 'NO_PROXY': '', 'no_proxy': ''}

    def admit(self, event: EvidenceEvent):
        check_event(event, 'mutation-started')
        with self._lock:
            require(self._server is not None and not self._closed
                    and self._admitted is None, 'fault-admission-state')
            require(event.monotonic_ns <= time.monotonic_ns(), 'fault-event-future')
            self._admitted = event
            self._record('mutation-bound', originalSha256=event.original_sha256,
                         operationId=event.operation_id)

    def arm(self, event: EvidenceEvent):
        check_event(event, 'owned-resource-observed')
        with self._lock:
            require(self._admitted is not None and self._terminal is None
                    and not self._armed and not self._closed, 'fault-arm-state')
            require(event.operation_id == self._admitted.operation_id
                    and self._admitted.monotonic_ns <= event.monotonic_ns <= time.monotonic_ns(),
                    'fault-resource-order')
            self._armed = True
            self._armed_at = time.monotonic_ns()
            self._record('armed', originalSha256=event.original_sha256,
                         operationId=event.operation_id)
            for client, (upstream, host) in tuple(self._sockets.items()):
                if host in self.profile.fault_hosts:
                    self._drop(host, 'existing')
                    self._shutdown(client)
                    self._shutdown(upstream)

    def restore(self, event: EvidenceEvent):
        check_event(event, 'executor-terminal')
        with self._lock:
            require(self._admitted is not None and self._terminal is None
                    and not self._closed, 'fault-restore-state')
            require(event.operation_id == self._admitted.operation_id
                    and self._admitted.monotonic_ns <= event.monotonic_ns <= time.monotonic_ns(),
                    'fault-terminal-order')
            require(self._armed_at is None or event.monotonic_ns >= self._armed_at,
                    'fault-terminal-before-arm')
            self._terminal = event
            self._armed = False
            self._record('restored', originalSha256=event.original_sha256,
                         operationId=event.operation_id)

    def snapshot(self):
        with self._lock:
            return {'contextSha256': self.profile.context_sha256,
                    'journal': [dict(row) for row in self._journal],
                    'journalExhausted': self._exhausted, 'faultedConnections': self._drops,
                    'acceptedConnections': self._connections, 'armed': self._armed,
                    'closed': self._closed, 'candidateQualified': False,
                    'sixAwsCellsQualified': False}

    def close(self):
        with self._lock:
            require(self._admitted is None or self._terminal is not None,
                    'fault-executor-not-terminal')
            require(self._server is not None and not self._closed, 'fault-close-state')
            self._closed = True
            for client in tuple(self._clients):
                self._shutdown(client)
            for client, (upstream, _) in tuple(self._sockets.items()):
                self._shutdown(upstream)
            server, thread = self._server, self._thread
            self._record('closed')
        server.shutdown()
        server.server_close()
        thread.join()

    @staticmethod
    def _shutdown(connection):
        try:
            connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _drop(self, host, phase):
        self._drops += 1
        self._record('connection-dropped', host=host, phase=phase)

    def _handle(self, client):
        upstream = None
        try:
            with self._lock:
                require(not self._closed, 'fault-closed')
                self._clients.add(client)
            # Absolute protocol parse bound, not a deadline for the executor.
            header_deadline = time.monotonic() + 10
            header = bytearray()
            while not header.endswith(b'\r\n\r\n'):
                remaining = header_deadline - time.monotonic()
                if remaining <= 0:
                    return
                client.settimeout(remaining)
                value = client.recv(1)
                if not value or len(header) >= 8192:
                    return
                header.extend(value)
            lines = header.decode('ascii').split('\r\n')
            request = lines.pop(0).split(' ')
            require(len(request) == 3 and request[0] == 'CONNECT'
                    and request[2] in ('HTTP/1.0', 'HTTP/1.1'), 'fault-connect-only')
            authority = request[1]
            require(authority.endswith(':443'), 'fault-port')
            host = authority[:-4]
            require(host in self.profile.allowed_hosts, 'fault-host-not-declared')
            auth = [line.split(':', 1)[1].strip() for line in lines if ':' in line
                    and line.split(':', 1)[0].lower() == 'proxy-authorization']
            require(len(auth) == 1 and hmac.compare_digest(auth[0], self._auth), 'fault-owner')
            with self._lock:
                require(not self._closed and not self._exhausted
                        and len(self._sockets) < 32, 'fault-connection-bound')
                if self._armed and host in self.profile.fault_hosts:
                    self._drop(host, 'new')
                    return  # transport failure, no fabricated AWS/HTTP response
            upstream = self._connect((host, 443), timeout=10)
            client.settimeout(None)
            upstream.settimeout(None)
            with self._lock:
                # Admission can race the normal TCP connection establishment.
                require(not self._closed and not self._exhausted
                        and len(self._sockets) < 32, 'fault-connection-bound')
                if self._armed and host in self.profile.fault_hosts:
                    self._drop(host, 'connecting')
                    return
                self._sockets[client] = (upstream, host)
                self._connections += 1
                self._record('connected', host=host)
                client.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            while True:
                readable, _, _ = select.select((client, upstream), (), ())
                for source in readable:
                    payload = source.recv(65536)
                    if not payload:
                        return
                    (upstream if source is client else client).sendall(payload)
        except (OSError, UnicodeError, TunnelRefusal, ValueError):
            with self._lock:
                self._record('connection-ended')
        finally:
            with self._lock:
                self._sockets.pop(client, None)
                self._clients.discard(client)
            if upstream is not None:
                upstream.close()
