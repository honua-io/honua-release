"""Loopback static-bundle Console edge for agent e2e Studio cases (#1530).

Serves the manifest-bound `honua-console` static bundle zip at `/console/` and
reverse-proxies every non-Console path to the case stack's loopback server.
No frozen Console container image is started and no administrator API key ever
reaches this edge.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import json
import mimetypes
import os
import posixpath
import re
import secrets
import shutil
import stat
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

MAX_BUNDLE_FILES = 5000
MAX_BUNDLE_UNCOMPRESSED = 256 * 1024 * 1024
MAX_PROXY_BODY = 16 * 1024 * 1024
REQUIRED_ENTRYPOINTS = ('index.html', 'config.json', '_framework/blazor.webassembly.js')
LOOPBACK_HOSTS = frozenset({'127.0.0.1', 'localhost', '::1'})
HOP_BY_HOP = frozenset({
    'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
    'te', 'trailers', 'transfer-encoding', 'upgrade',
})
STRIPPED_UPSTREAM_HEADERS = frozenset({
    'host', 'x-honua-edge-auth', 'x-forwarded-user', 'x-forwarded-access-token', 'x-api-key',
})
EXTRA_MIME_TYPES = {
    '.mjs': 'text/javascript; charset=utf-8',
    '.cjs': 'text/javascript; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.wasm': 'application/wasm',
    '.json': 'application/json; charset=utf-8',
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.dat': 'application/octet-stream',
    '.blat': 'application/octet-stream',
}


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
    if not 0 < port < 65536:
        raise ValueError('Console bundle upstream port is out of range')
    return parts.scheme, parts.hostname, port


def _safe_zip_member(info: zipfile.ZipInfo, root: Path) -> Path:
    name = info.filename
    if not name or name.startswith(('/', '\\')) or '\\' in name or '\x00' in name:
        raise ValueError(f'unsafe static bundle entry: {name!r}')
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ValueError(f'static bundle must not contain symlinks: {name!r}')
    parts = posixpath.normpath(name).split('/')
    if any(part in ('', '.', '..') for part in parts):
        raise ValueError(f'unsafe static bundle path: {name!r}')
    target = (root / Path(*parts)).resolve()
    if root != target and root not in target.parents:
        raise ValueError(f'static bundle entry escapes root: {name!r}')
    return target


def _verify_extracted_bundle(root: Path) -> None:
    for required in REQUIRED_ENTRYPOINTS:
        if not (root / required).is_file():
            raise ValueError(f'Console bundle is missing required entrypoint: {required}')
    index_html = (root / 'index.html').read_text(encoding='utf-8', errors='replace')
    if '<base href="/console/"' not in index_html:
        raise ValueError('Console bundle index.html must use <base href="/console/">')


def extract_console_bundle(bundle_bytes: bytes, dest_dir: Path, *, expected_sha256: str) -> Path:
    """Verify `bundle_bytes` against `expected_sha256` and extract safely into `dest_dir`."""
    if not isinstance(expected_sha256, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', expected_sha256):
        raise ValueError('invalid bound Console bundle sha256')
    if not isinstance(bundle_bytes, (bytes, bytearray)):
        raise ValueError('Console bundle bytes must be bytes')
    actual = 'sha256:' + hashlib.sha256(bundle_bytes).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f'Console bundle digest {actual} differs from bound {expected_sha256}')
    root = dest_dir.resolve()
    try:
        archive = zipfile.ZipFile(io.BytesIO(bundle_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError('Console bundle is not a valid zip archive') from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_BUNDLE_FILES:
            raise ValueError('Console bundle exceeds entry count limit')
        total = 0
        for info in infos:
            if info.is_dir():
                continue
            target = _safe_zip_member(info, root)
            total += info.file_size
            if total > MAX_BUNDLE_UNCOMPRESSED:
                raise ValueError('Console bundle exceeds uncompressed size limit')
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, 'r') as src, target.open('wb') as dst:
                shutil.copyfileobj(src, dst)
    _verify_extracted_bundle(root)
    return root


def static_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in EXTRA_MIME_TYPES:
        return EXTRA_MIME_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or 'application/octet-stream'


def resolve_static_asset(root: Path, rel_path: str) -> Path | None:
    """Resolve a `/console/<rel_path>` file strictly under `root`; return None on traversal or miss."""
    decoded = unquote(rel_path)
    if '\x00' in decoded or '\\' in decoded or decoded.startswith('/'):
        return None
    parts = [part for part in decoded.split('/') if part]
    if any(part in ('.', '..') for part in parts):
        return None
    candidate = (root / Path(*parts)).resolve()
    if root not in candidate.parents or not candidate.is_file():
        return None
    return candidate


def _console_config_bytes() -> bytes:
    payload = {
        'apiBaseUrl': '/',
        'auth': {'mode': 'builtin', 'authority': None, 'clientId': None, 'scopes': []},
        'v4Enabled': True,
    }
    return (json.dumps(payload, separators=(',', ':')) + '\n').encode('utf-8')


def _console_version_bytes(source_revision: str, artifact_sha256: str, artifact_version: str | None) -> bytes:
    payload = {
        'version': artifact_version or 'pre-release',
        'sourceRevision': source_revision,
        'artifactSha256': artifact_sha256,
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


def _resolve_static_response(edge: 'CaseConsoleBundle', path: str) -> tuple | None:
    """Return ('redirect', status, location) or ('bytes', status, body, ctype, cache) for Console paths."""
    redirects = {'/': (302, '/console/'), '/console': (301, '/console/'), '/studio': (302, '/console/studio')}
    if path in redirects:
        status, location = redirects[path]
        return ('redirect', status, location)
    if path == '/console/config.json':
        return ('bytes', 200, edge._config_bytes, 'application/json; charset=utf-8', 'no-store')
    if path in ('/console/version.json', '/version.json'):
        return ('bytes', 200, edge._version_bytes, 'application/json; charset=utf-8', 'no-store')
    if not path.startswith('/console/'):
        return None
    rel = path.removeprefix('/console/')
    if rel and re.search(r'\.[A-Za-z0-9]+$', rel):
        asset = resolve_static_asset(edge._bundle_root, rel)
        if asset is None:
            return ('bytes', 404, b'not found', 'text/plain; charset=utf-8', 'no-store')
        return ('bytes', 200, asset.read_bytes(), static_content_type(asset), 'no-cache')
    index_file = edge._bundle_root / 'index.html'
    return ('bytes', 200, index_file.read_bytes(), 'text/html; charset=utf-8', 'no-store')


class _BundleEdgeHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    edge: 'CaseConsoleBundle'

    def log_message(self, *args):
        pass

    def _send_bytes(self, status: int, body: bytes, content_type: str, *,
                    cache_control: str = 'no-cache', head_only: bool = False):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache_control)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        if not head_only and body:
            self.wfile.write(body)

    def _send_redirect(self, status: int, location: str):
        self.send_response(status)
        self.send_header('Location', location)
        self.send_header('Content-Length', '0')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()

    def _serve_static(self, head_only: bool) -> bool:
        outcome = _resolve_static_response(self.edge, urlsplit(self.path).path)
        if outcome is None:
            return False
        if outcome[0] == 'redirect':
            self._send_redirect(outcome[1], outcome[2])
        else:
            _, status, body, content_type, cache_control = outcome
            self._send_bytes(status, body, content_type, cache_control=cache_control, head_only=head_only)
        return True

    def _proxy(self):
        body, error_status = _read_bounded_body(self.rfile, self.headers)
        if error_status is not None:
            self._send_bytes(error_status, b'invalid request body', 'text/plain; charset=utf-8')
            return
        outbound = _build_outbound_headers(self.headers, self.edge._edge_secret)
        scheme, host, port = self.edge._upstream
        conn_cls = http.client.HTTPSConnection if scheme == 'https' else http.client.HTTPConnection
        conn = conn_cls(host, port, timeout=30)
        try:
            conn.request(self.command, self.path, body=body if body else None, headers=outbound)
            resp = conn.getresponse()
            raw = resp.read()
            resp_hop = _connection_hop_headers(resp.headers)
            self.send_response(resp.status, resp.reason)
            for key, value in resp.getheaders():
                if key.lower() not in resp_hop and key.lower() != 'content-length':
                    self.send_header(key, value)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            if self.command != 'HEAD' and raw:
                self.wfile.write(raw)
        except OSError:
            self._send_bytes(502, b'upstream unavailable', 'text/plain; charset=utf-8',
                             cache_control='no-store', head_only=self.command == 'HEAD')
        finally:
            conn.close()

    def do_GET(self):
        if not self._serve_static(head_only=False):
            self._proxy()

    def do_HEAD(self):
        if not self._serve_static(head_only=True):
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

    def __init__(self, stack, bundle_bytes: bytes, *, artifact_sha256: str, source_revision: str,
                 artifact_version: str | None = None, edge_secret: str | None = None):
        if not isinstance(source_revision, str) or not re.fullmatch(r'[0-9a-f]{40}', source_revision):
            raise ValueError('invalid bound Console sourceRevision')
        if edge_secret is not None and (not isinstance(edge_secret, str) or len(edge_secret) < 32):
            raise ValueError('Console edge secret is invalid')
        self._upstream = validated_loopback_upstream(getattr(stack, 'base_url', ''))
        self.artifact_sha256 = artifact_sha256
        self.source_revision = source_revision
        self.artifact_version = artifact_version
        self._edge_secret = edge_secret
        self._temp_dir = Path(tempfile.mkdtemp(prefix='honua-console-bundle-'))
        os.chmod(self._temp_dir, 0o700)
        self._server = None
        self._thread = None
        self.url = None
        try:
            self._bundle_root = extract_console_bundle(
                bundle_bytes, self._temp_dir, expected_sha256=artifact_sha256)
            self._config_bytes = _console_config_bytes()
            self._version_bytes = _console_version_bytes(source_revision, artifact_sha256, artifact_version)
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
