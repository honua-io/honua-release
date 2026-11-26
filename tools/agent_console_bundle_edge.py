"""Loopback static-bundle Console edge for agent e2e Studio cases.

Serves the manifest-bound `honua-console` static bundle zip at `/console/` (plus
compatible `/_studio/` asset routes for the independent renderer) and
reverse-proxies every non-Console path to the case stack's loopback server.
No frozen Console container image is started and no administrator API key ever
reaches this edge.
"""
from __future__ import annotations

from dataclasses import dataclass
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
STREAM_CHUNK_BYTES = 8192
MAX_TCP_PORT = 65535
MIN_EDGE_SECRET_LENGTH = 32
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


@dataclass(frozen=True)
class BoundConsoleBundle:
    """Verified static Console bundle payload and its manifest identity."""
    bundle_bytes: bytes
    artifact_sha256: str
    source_revision: str
    artifact_version: str | None = None


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


def _bundle_custody_base(component: dict) -> dict:
    fact = {'artifactSha256': component.get('artifactSha256'), 'sourceRevision': component.get('sha')}
    if component.get('artifactVersion'):
        fact['artifactVersion'] = component['artifactVersion']
    return fact


def exercised_bundle_custody(component: dict) -> dict:
    """Custody fact recording that the case served the manifest-bound static Console bundle."""
    return {**_bundle_custody_base(component), 'exercised': True}


def unexercised_bundle_custody(component: dict) -> dict:
    """Custody fact recording that the case ran a fallback image instead of the bound bundle."""
    return {**_bundle_custody_base(component), 'exercised': False}


def load_bound_console_bundle(component: dict, *, github=None) -> bytes:
    """Fetch and hash-verify the static Console bundle bytes the manifest binds."""
    import static_bundle
    name = static_bundle.artifact_name(component)
    expected = component.get('artifactSha256')
    revision = component.get('artifactSourceRevision') or component.get('sha')
    repo_url = str(component.get('repository') or '')
    match = re.fullmatch(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?', repo_url)
    if not name or not match or not isinstance(expected, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', expected):
        raise ValueError('release manifest honua-console static bundle metadata is incomplete')
    if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('release manifest honua-console static bundle source revision is invalid')
    override_path = os.environ.get('HONUA_CONSOLE_BUNDLE_PATH', '').strip()
    if override_path:
        raw = Path(override_path).read_bytes()
    else:
        if github is None:
            from resolve_trunk_candidate import GitHub
            github = GitHub()
        artifact = {
            'kind': static_bundle.KIND,
            'coordinate': static_bundle.coordinate(match.group(1), name),
            'sourceRevision': revision,
            'sha256': expected,
        }
        raw = static_bundle.published_bytes(artifact, github)
    actual = 'sha256:' + hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise ValueError(f'bound Console static bundle digest {actual} differs from manifest {expected}')
    return raw


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
    """Resolve a relative static asset strictly under `root`; return None on traversal or miss."""
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
            self._send_static_response(_StaticResponse(error_status, b'invalid request body', 'text/plain; charset=utf-8', 'no-store'))
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
