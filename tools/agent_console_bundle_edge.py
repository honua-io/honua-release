"""Loopback static-bundle Console edge for agent e2e Studio cases.

Serves the manifest-bound `honua-console` static bundle zip at `/console/` (plus
compatible `/_studio/` asset routes for the independent renderer) and
reverse-proxies every non-Console path to the case stack's loopback server.
No frozen Console container image is started and no administrator API key ever
reaches this edge.
"""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from agent_console_bundle_archive import (
    BoundConsoleBundle,
    exercised_bundle_custody,
    extract_console_bundle,
    load_bound_console_bundle,
    resolve_static_asset,
    static_content_type,
    unexercised_bundle_custody,
)

MAX_PROXY_BODY = 16 * 1024 * 1024
STREAM_CHUNK_BYTES = 8192
MAX_TCP_PORT = 65535
MIN_EDGE_SECRET_LENGTH = 32
LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})
HOP_BY_HOP = frozenset({
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailers', 'transfer-encoding', 'upgrade',
})
STRIPPED_UPSTREAM_HEADERS = frozenset({
    'host', 'x-honua-edge-auth', 'x-forwarded-user', 'x-forwarded-access-token', 'x-api-key',
})

__all__ = [
    'BoundConsoleBundle', 'CaseConsoleBundle', 'exercised_bundle_custody',
    'extract_console_bundle', 'load_bound_console_bundle', 'resolve_static_asset',
    'start_bound_console', 'static_content_type', 'unexercised_bundle_custody',
    'validated_loopback_upstream',
]


@dataclass(frozen=True)
class _StaticResponse:
    status: int
    body: bytes
    content_type: str
    cache_control: str = 'no-cache'


@dataclass(frozen=True)
class _RedirectResponse:
    status: int
    location: str


def start_bound_console(stack, component: dict, identity: dict, *, loader=None) -> 'CaseConsoleBundle':
    """Load the bound static Console bundle from `component` and start its loopback case edge."""
    bundle_bytes = (loader or load_bound_console_bundle)(component)
    bundle = BoundConsoleBundle(
        bundle_bytes=bundle_bytes,
        artifact_sha256=component['artifactSha256'],
        source_revision=component['sha'],
        artifact_version=component.get('artifactVersion'),
    )
    return CaseConsoleBundle(stack, bundle, edge_secret=identity['edgeSecret'])


def validated_loopback_upstream(base_url: str) -> tuple[str, str, int]:
    """Ensure the proxied backend is an explicit loopback http(s) origin without credentials."""
    if not isinstance(base_url, str) or not base_url:
        raise ValueError('case stack has no loopback base_url')
    parts = urlsplit(base_url)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
        raise ValueError('Console bundle upstream must be an http(s) origin without credentials')
    if parts.hostname not in LOOPBACK_HOSTS:
        raise ValueError('Console bundle upstream must be a loopback address')
    port = parts.port or (443 if parts.scheme == 'https' else 80)
    if not 0 < port <= MAX_TCP_PORT:
        raise ValueError('Console bundle upstream port is out of range')
    return parts.scheme, parts.hostname, port


def _console_config_bytes() -> bytes:
    payload = {
        'apiBaseUrl': '/',
        'auth': {'mode': 'builtin', 'authority': None, 'clientId': None, 'scopes': []},
        'v4Enabled': True,
    }
    return (json.dumps(payload, separators=(',', ':')) + '\n').encode('utf-8')


def _console_version_bytes(bundle: BoundConsoleBundle) -> bytes:
    payload = {
        'version': bundle.artifact_version or 'pre-release',
        'sourceRevision': bundle.source_revision,
        'artifactSha256': bundle.artifact_sha256,
        'mode': 'static-bundle',
    }
    return (json.dumps(payload, separators=(',', ':')) + '\n').encode('utf-8')


def _connection_hop_headers(headers) -> set[str]:
    extra = set()
    for value in headers.get_all('Connection') or ():
        for token in value.split(','):
            cleaned = token.strip().lower()
            if cleaned:
                extra.add(cleaned)
    return HOP_BY_HOP | extra


def _build_outbound_headers(headers, edge_secret: str | None) -> dict[str, str]:
    hop = _connection_hop_headers(headers)
    outbound = {
        key: value for key, value in headers.items()
        if key.lower() not in hop and key.lower() not in STRIPPED_UPSTREAM_HEADERS
    }
    edge_auth = headers.get('X-Honua-Edge-Auth', '')
    forwarded_token = headers.get('X-Forwarded-Access-Token', '').strip()
    if (edge_secret is not None and forwarded_token
            and secrets.compare_digest(edge_auth, edge_secret)
            and not any(key.lower() == 'authorization' for key in outbound)):
        outbound['Authorization'] = 'Bearer ' + forwarded_token.removeprefix('Bearer ')
    outbound['X-Forwarded-For'] = '127.0.0.1'
    outbound['X-Forwarded-Proto'] = 'http'
    if headers.get('Host'):
        outbound['X-Forwarded-Host'] = headers['Host']
    return outbound


def _read_bounded_body(rfile, headers) -> tuple[bytes | None, int | None]:
    length_header = headers.get('Content-Length')
    if length_header is None:
        return b'', None
    try:
        length = int(length_header)
    except ValueError:
        return None, 400
    if length < 0 or length > MAX_PROXY_BODY:
        return None, 413
    return (rfile.read(length) if length else b''), None


def _asset_response(root: Path, rel_path: str) -> _StaticResponse:
    asset = resolve_static_asset(root, rel_path)
    if asset is None:
        return _StaticResponse(404, b'not found', 'text/plain; charset=utf-8', 'no-store')
    return _StaticResponse(200, asset.read_bytes(), static_content_type(asset), 'no-cache')


def _resolve_static_response(edge: 'CaseConsoleBundle', path: str) -> _StaticResponse | _RedirectResponse | None:
    redirects = {'/': (302, '/console/'), '/console': (301, '/console/'), '/studio': (302, '/console/studio')}
    if path in redirects:
        status, location = redirects[path]
        return _RedirectResponse(status, location)
    if path == '/console/config.json':
        return _StaticResponse(200, edge._config_bytes, 'application/json; charset=utf-8', 'no-store')
    if path in ('/console/version.json', '/version.json'):
        return _StaticResponse(200, edge._version_bytes, 'application/json; charset=utf-8', 'no-store')
    if path.startswith('/_studio/'):
        return _asset_response(edge._bundle_root, path.lstrip('/'))
    if not path.startswith('/console/'):
        return None
    rel = path.removeprefix('/console/')
    if rel and re.search(r'\.[A-Za-z0-9]+$', rel):
        return _asset_response(edge._bundle_root, rel)
    index_file = edge._bundle_root / 'index.html'
    return _StaticResponse(200, index_file.read_bytes(), 'text/html; charset=utf-8', 'no-store')


def _is_streaming_response(resp: http.client.HTTPResponse) -> bool:
    content_type = (resp.getheader('Content-Type') or '').lower()
    return 'text/event-stream' in content_type or resp.getheader('Content-Length') is None


class _BundleEdgeHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    edge: 'CaseConsoleBundle'

    def log_message(self, *args):
        pass

    def _write_static_headers(self, response: _StaticResponse) -> None:
        self.send_response(response.status)
        self.send_header('Content-Type', response.content_type)
        self.send_header('Content-Length', str(len(response.body)))
        self.send_header('Cache-Control', response.cache_control)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()

    def _send_static_response(self, response: _StaticResponse) -> None:
        self._write_static_headers(response)
        if response.body:
            self.wfile.write(response.body)

    def _send_redirect(self, redirect: _RedirectResponse) -> None:
        self.send_response(redirect.status)
        self.send_header('Location', redirect.location)
        self.send_header('Content-Length', '0')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()

    def _serve_static_get(self) -> bool:
        outcome = _resolve_static_response(self.edge, urlsplit(self.path).path)
        if outcome is None:
            return False
        if isinstance(outcome, _RedirectResponse):
            self._send_redirect(outcome)
        else:
            self._send_static_response(outcome)
        return True

    def _serve_static_head(self) -> bool:
        outcome = _resolve_static_response(self.edge, urlsplit(self.path).path)
        if outcome is None:
            return False
        if isinstance(outcome, _RedirectResponse):
            self._send_redirect(outcome)
        else:
            self._write_static_headers(outcome)
        return True

    def _write_upstream_headers(self, resp: http.client.HTTPResponse) -> None:
        resp_hop = _connection_hop_headers(resp.headers)
        self.send_response(resp.status, resp.reason)
        for key, value in resp.getheaders():
            if key.lower() not in resp_hop:
                self.send_header(key, value)
        self.end_headers()

    def _write_chunked_upstream_headers(self, resp: http.client.HTTPResponse) -> None:
        resp_hop = _connection_hop_headers(resp.headers)
        self.send_response(resp.status, resp.reason)
        for key, value in resp.getheaders():
            lowered = key.lower()
            if lowered not in resp_hop and lowered != 'content-length':
                self.send_header(key, value)
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()

    def _stream_chunked_body(self, resp: http.client.HTTPResponse) -> None:
        while True:
            chunk = resp.read1(STREAM_CHUNK_BYTES)
            if not chunk:
                break
            self.wfile.write(f'{len(chunk):x}\r\n'.encode('ascii') + chunk + b'\r\n')
            self.wfile.flush()
        self.wfile.write(b'0\r\n\r\n')
        self.wfile.flush()

    def _proxy(self) -> None:
        body, error_status = _read_bounded_body(self.rfile, self.headers)
        if error_status is not None:
            self._send_static_response(
                _StaticResponse(error_status, b'invalid request body', 'text/plain; charset=utf-8', 'no-store'))
            return
        outbound = _build_outbound_headers(self.headers, self.edge._edge_secret)
        scheme, host, port = self.edge._upstream
        conn_cls = http.client.HTTPSConnection if scheme == 'https' else http.client.HTTPConnection
        conn = conn_cls(host, port, timeout=30)
        try:
            conn.request(self.command, self.path, body=body if body else None, headers=outbound)
            resp = conn.getresponse()
            if self.command == 'HEAD':
                self._write_upstream_headers(resp)
                return
            if _is_streaming_response(resp):
                self._write_chunked_upstream_headers(resp)
                self._stream_chunked_body(resp)
                return
            raw = resp.read()
            self._write_upstream_headers(resp)
            if raw:
                self.wfile.write(raw)
        except OSError:
            err = _StaticResponse(502, b'upstream unavailable', 'text/plain; charset=utf-8', 'no-store')
            if self.command == 'HEAD':
                self._write_static_headers(err)
            else:
                self._send_static_response(err)
        finally:
            conn.close()

    def do_GET(self):
        if not self._serve_static_get():
            self._proxy()

    def do_HEAD(self):
        if not self._serve_static_head():
            self._proxy()

    def do_POST(self):
        self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_PATCH(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def do_OPTIONS(self):
        self._proxy()


class CaseConsoleBundle:
    """Loopback case edge serving the manifest-bound static Console bundle at `/console/`."""

    def __init__(self, stack, bundle: BoundConsoleBundle, *, edge_secret: str | None = None):
        if not isinstance(bundle.source_revision, str) or not re.fullmatch(r'[0-9a-f]{40}', bundle.source_revision):
            raise ValueError('invalid bound Console sourceRevision')
        if edge_secret is not None and (not isinstance(edge_secret, str) or len(edge_secret) < MIN_EDGE_SECRET_LENGTH):
            raise ValueError('Console edge secret is invalid')
        self._upstream = validated_loopback_upstream(getattr(stack, 'base_url', ''))
        self.artifact_sha256 = bundle.artifact_sha256
        self.source_revision = bundle.source_revision
        self.artifact_version = bundle.artifact_version
        self._edge_secret = edge_secret
        self._temp_dir = Path(tempfile.mkdtemp(prefix='honua-console-bundle-'))
        os.chmod(self._temp_dir, 0o700)
        self._server = None
        self._thread = None
        self.url = None
        try:
            self._bundle_root = extract_console_bundle(
                bundle.bundle_bytes, self._temp_dir, expected_sha256=bundle.artifact_sha256)
            self._config_bytes = _console_config_bytes()
            self._version_bytes = _console_version_bytes(bundle)
            handler_cls = type('CaseConsoleBundleHandler', (_BundleEdgeHandler,), {'edge': self})
            self._server = ThreadingHTTPServer(('127.0.0.1', 0), handler_cls)
            self._thread = threading.Thread(target=self._server.serve_forever, name='case-console-bundle', daemon=True)
            self._thread.start()
            self.url = f'http://127.0.0.1:{self._server.server_address[1]}'
        except Exception:
            self.close()
            raise

    def close(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            if self._thread is not None:
                self._thread.join(timeout=5)
            self._server = None
            self._thread = None
        if self._temp_dir is not None and self._temp_dir.exists():
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None
