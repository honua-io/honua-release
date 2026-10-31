"""Origin-locked Map Studio browser for agent evals.

The agent gets a loopback MCP server whose tools can drive one headless Chromium
locked to the case stack's Studio origin. Every tool call is written to the runner
action log, including the sha256 of the controller screenshot, before the call
returns. Agent screenshots are not oracle evidence.

The runner observes saved maps through its own recorder. Render assertions stay
unobserved until an independently verified server render can supply the evidence.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import queue
import socket
import subprocess
import time
import urllib.request
from concurrent.futures import Future, TimeoutError as FutureTimeout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from agent_e2e_local_recorder import ProjectionError

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / 'certification/agent-e2e/corpus'
CONTRACT_PATH = ROOT / 'certification/agent-e2e/expectations/local/studio-render-contract.json'
LIST_PATH = '/api/v1/studio/content-items?family=map&limit=200'
LAYER_NAMES = ('parcels', 'flood_zones', 'shelters')
SOURCE_LAYERS = (('parcels', 'parcels'), ('flood_zones', 'flood-zones'), ('shelters', 'shelters'))
STYLE_CONFIG = {'floodLayerId': 'flood_zones', 'shelterLayerId': 'shelters'}
FLOOD_COLOR = '#0088ff'
FLOOD_OPACITY = 0.35
RENDER_MINIMUM_NON_BACKGROUND = 1000
RENDER_MINIMUM_MATCHING = 4000
MAX_BODY = 512 * 1024
OWNER_TIMEOUT = 35
CLOSE_TIMEOUT = 3
KEY = re.compile(r'^(Enter|Tab|Escape|Backspace|Arrow(Up|Down|Left|Right)|[A-Za-z0-9 ])$')
IDENT = re.compile(r'[A-Za-z0-9-]{1,80}')
TOOLS = (
    'studio_navigate', 'studio_click', 'studio_type', 'studio_press', 'studio_snapshot', 'studio_upload',
)
TOOL_ARGS = {
    'studio_navigate': {'url'},
    'studio_click': {'selector'},
    'studio_type': {'selector', 'text'},
    'studio_press': {'key'},
    'studio_snapshot': set(),
    'studio_upload': {'selector', 'name'},
}


class OriginLocked(ValueError):
    pass


class BrowserUnavailable(RuntimeError):
    pass


def remote_docker_host():
    """Only the explicitly provisioned eval hosts may run corpus containers."""
    value = os.environ.get('DOCKER_HOST', '')
    match = re.fullmatch(r'ssh://(honua-eval-[1-4])', value)
    return match.group(1) if match else None


class CaseConsole:
    """A digest-pinned quickstart Console attached to one remote case network."""
    def __init__(self, stack, image, *, edge_secret=None):
        self.host = remote_docker_host()
        if self.host is None:
            raise ValueError('Console requires an approved remote eval Docker host')
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', stack.compose_project):
            raise ValueError('invalid case Compose project')
        if not re.fullmatch(r'ghcr\.io/honua-io/honua-console@sha256:[0-9a-f]{64}', image):
            raise ValueError('Console image is not the pinned digest')
        self.name = stack.compose_project + '-console-' + secrets.token_hex(16)
        self._project = stack.compose_project
        self._docker_host = 'ssh://' + self.host
        self._attempted = False
        self._created = False
        self._tunnel = None
        self.url = None
        env = dict(os.environ, DOCKER_HOST=self._docker_host, HONUA_ADMIN_API_KEY=stack.admin_key)
        command = ['docker', 'run', '--detach', '--name', self.name,
                   '--label', 'honua.corpus.case=' + stack.compose_project,
                   '--network', stack.compose_project + '_default', '--publish', '127.0.0.1::8080',
                   '--env', 'ASPNETCORE_ENVIRONMENT=Development', '--env', 'ASPNETCORE_URLS=http://+:8080',
                   '--env', 'Studio__EmbedJsStudio=true',
                   '--env', 'HONUA_SERVER_BASE_URL=http://server:8080', '--env', 'HONUA_ADMIN_API_KEY',
                   '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
                   '--tmpfs', '/tmp:noexec,nosuid,size=100m', image]
        if edge_secret is not None:
            if not isinstance(edge_secret, str) or len(edge_secret) < 32:
                raise ValueError('Console edge secret is invalid')
            env['Honua__Console__Auth__EdgeForwarded__SharedSecret'] = edge_secret
            command[-1:-1] = ['--env', 'Honua__Console__Auth__Mode=EdgeForwarded',
                               '--env', 'Honua__Console__Auth__EdgeForwarded__SharedSecret']
        try:
            self._attempted = True
            result = subprocess.run(command, env=env, capture_output=True, timeout=180, check=False)
            if result.returncode:
                raise RuntimeError('could not start the case Console')
            self._created = True
            port = subprocess.run(['docker', 'port', self.name, '8080/tcp'], env=env,
                                  capture_output=True, text=True, timeout=20, check=True).stdout.strip()
            match = re.fullmatch(r'127\.0\.0\.1:([0-9]{1,5})', port)
            if not match or not 0 < int(match.group(1)) < 65536:
                raise RuntimeError('Console did not bind a private loopback port')
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                local_port = listener.getsockname()[1]
            self._tunnel = subprocess.Popen(['ssh', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
                '-N', '-L', f'127.0.0.1:{local_port}:127.0.0.1:{match.group(1)}', self.host],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.url = f'http://127.0.0.1:{local_port}'
            deadline = time.monotonic() + 45
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            while time.monotonic() < deadline:
                if self._tunnel.poll() is not None:
                    raise RuntimeError('Console SSH tunnel exited')
                try:
                    with opener.open(self.url + '/version.json', timeout=2) as response:
                        if response.status == 200:
                            break
                except OSError:
                    pass
                time.sleep(0.25)
            else:
                raise RuntimeError('case Console did not become ready')
        except Exception:
            self.close()
            raise

    def close(self):
        if self._tunnel is not None:
            self._tunnel.terminate()
            try:
                self._tunnel.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._tunnel.kill()
                self._tunnel.wait(timeout=5)
            self._tunnel = None
        if self._attempted and not self._created:
            probe = subprocess.run(['docker', 'inspect', '--format',
                                    '{{index .Config.Labels "honua.corpus.case"}}', self.name],
                                   env=dict(os.environ, DOCKER_HOST=self._docker_host),
                                   capture_output=True, text=True, timeout=20, check=False)
            self._created = probe.returncode == 0 and probe.stdout.strip() == self._project
        if self._created:
            result = subprocess.run(['docker', 'rm', '-f', self.name],
                                    env=dict(os.environ, DOCKER_HOST=self._docker_host),
                                    capture_output=True, timeout=30, check=False)
            if result.returncode:
                raise RuntimeError('could not remove the case Console')
            self._created = False
        self._attempted = False


class _DaemonOwner:
    """One thread owns Playwright; a wedged driver cannot hold the runner process open."""
    def __init__(self):
        self.jobs = queue.Queue()
        self.idle = None
        self.thread = threading.Thread(target=self._run, name='studio-browser', daemon=True)
        self.thread.start()

    def _run(self):
        while True:
            try:
                job = self.jobs.get(timeout=0.05)
            except queue.Empty:
                if self.idle is not None:
                    try:
                        self.idle()
                    except Exception:
                        self.idle = None
                continue
            if job is None:
                return
            future, callback, args = job
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(callback(*args))
            except BaseException as exc:
                future.set_exception(exc)

    def submit(self, callback, *args):
        future = Future()
        self.jobs.put((future, callback, args))
        return future

    def shutdown(self, wait=True):
        self.jobs.put(None)
        if wait:
            self.thread.join(CLOSE_TIMEOUT)


def require(condition, reason):
    if not condition:
        raise ProjectionError(reason)


class OriginLock:
    """One http(s) origin on the case-local stack. Nothing else may be fetched."""

    def __init__(self, studio_url):
        parts = urlsplit(studio_url)
        if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
            raise ValueError('studio origin is not a locked http(s) origin')
        if parts.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise ValueError('studio browser is locked to the case-local stack')
        self.scheme = parts.scheme
        self.hostname = parts.hostname
        self.port = parts.port or (443 if parts.scheme == 'https' else 80)
        host = f'[{self.hostname}]' if ':' in self.hostname else self.hostname
        self.origin = f'{self.scheme}://{host}:{self.port}'

    def resolve(self, url):
        if not isinstance(url, str) or not url or any(ord(c) < 33 or ord(c) > 126 for c in url):
            raise OriginLocked('refused url')
        lowered = url.lower()
        if url.startswith('//') or lowered.startswith(('javascript:', 'data:', 'file:', 'blob:', 'about:', 'devtools:')):
            raise OriginLocked('refused url scheme')
        if '\\' in url:
            raise OriginLocked('refused url')
        absolute = url if '://' in url else self.origin + (url if url.startswith('/') else '/' + url)
        parts = urlsplit(absolute)
        if parts.username or parts.password or not parts.hostname:
            raise OriginLocked('refused url credentials')
        if parts.scheme != self.scheme:
            raise OriginLocked('refused origin')
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        if parts.hostname != self.hostname or port != self.port:
            raise OriginLocked('refused origin')
        path = parts.path or '/'
        query = ('?' + parts.query) if parts.query else ''
        fragment = ('#' + parts.fragment) if parts.fragment else ''
        return f'{self.origin}{path}{query}{fragment}'

    def allows(self, url):
        try:
            self.resolve(url)
        except (ValueError, TypeError):
            return False
        return True

    def route_allows(self, url):
        # The browser's empty document is not an agent navigation target.
        return url == 'about:blank' or self.allows(url)


def launch_options():
    """Headless, with devtools left off. Playwright 1.64 has no devtools launch switch."""
    # Responses are fulfilled by the guarded route, which otherwise gives local
    # documents an unknown address space and Chromium rejects even their own WS.
    return {'headless': True, 'args': ['--disable-dev-shm-usage', '--disable-features=LocalNetworkAccessChecks']}


def context_options():
    return {'viewport': {'width': 512, 'height': 512}, 'device_scale_factor': 1,
            'accept_downloads': False, 'java_script_enabled': True, 'service_workers': 'block'}


def use_worktree_browsers():
    path = ROOT / '.playwright-browsers'
    if path.is_dir():
        os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(path))


def _selector(value):
    if not isinstance(value, str) or not value or len(value) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise OriginLocked('refused selector')
    if 'javascript:' in value.lower() or 'eval(' in value.lower():
        raise OriginLocked('refused selector')
    return value


def resolve_fixture(root, name):
    if not isinstance(name, str) or not name or name.startswith(('/', '\\')) or '\\' in name or '\x00' in name:
        raise OriginLocked('upload is outside the fixture directory')
    base = Path(root).resolve()
    candidate = (base / name).resolve()
    if candidate != base and base not in candidate.parents:
        raise OriginLocked('upload is outside the fixture directory')
    if not candidate.is_file():
        raise OriginLocked('upload is outside the fixture directory')
    return candidate


def _decode_png(data):
    import importlib.util
    module = importlib.util.spec_from_file_location(
        'honua_render_png', ROOT / 'certification/installed-clients/render_png.py')
    loaded = importlib.util.module_from_spec(module)
    module.loader.exec_module(loaded)
    return loaded.decode_png(data)


def _close(pixel, want, tolerance):
    return all(abs(int(pixel[index]) - int(want[index])) <= tolerance for index in range(4))


def measure_png(png, contract, *, http_status, loaded_layer_ids, visible_label_source_ids, browser_errors):
    """Decode a controller screenshot. The contract is the measurement procedure, not model output."""
    require(isinstance(contract, dict) and isinstance(contract.get('regions'), list), 'render contract is incomplete')
    try:
        width, height, pixel = _decode_png(png)
    except ValueError as exc:
        raise ProjectionError('render screenshot is not a decodable png') from exc
    background = tuple(contract['background'])
    tolerance = contract['backgroundTolerance']
    non_background = 0
    for y in range(height):
        for x in range(width):
            if not _close(pixel(x, y), background, tolerance):
                non_background += 1
    regions = {}
    for region in contract['regions']:
        left, top, wide, high = region['rect']
        want = tuple(region['rgba'])
        matched = 0
        for y in range(int(top), int(top) + int(high)):
            for x in range(int(left), int(left) + int(wide)):
                if 0 <= x < width and 0 <= y < height and _close(pixel(x, y), want, region['tolerance']):
                    matched += 1
        regions[region['id']] = {'matchingPixels': matched}
    return {'httpStatus': http_status, 'decodedWidth': width, 'decodedHeight': height,
            'loadedLayerIds': list(loaded_layer_ids), 'nonBackgroundPixels': non_background,
            'browserErrors': list(browser_errors), 'regions': regions,
            'visibleLabelSourceIds': list(visible_label_source_ids)}


def split_ids(value):
    if not isinstance(value, str) or not value.strip():
        return []
    return [part.strip() for part in value.split(',') if part.strip()]


def render_contract():
    return json.loads(CONTRACT_PATH.read_text(encoding='utf-8'))


class PlaywrightPage:
    """Controller page. It screenshots and reads attributes. It has no evaluate tool."""

    def __init__(self, origin, identity=None):
        use_worktree_browsers()
        from playwright.sync_api import sync_playwright
        self.origin = origin
        self.aborted = []
        self._initial_document = True
        self._origin_failed = False
        self._identity = identity
        self._identity_tokens = []
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(**launch_options())
        options = context_options()
        if identity is not None:
            options['extra_http_headers'] = {'X-Honua-Edge-Auth': identity['edgeSecret'],
                                             'X-Forwarded-User': identity['subject']}
        self._context = self._browser.new_context(**options)
        self._context.route('**/*', self._gate)
        # Do not use sync Playwright's WebSocket route: closing a socket from its
        # callback can deadlock the owning greenlet. The document's enforced CSP
        # below restricts WebSocket connections as well as fetch to its own origin.
        self._context.on('download', self._cancel_download)
        self._page = self._context.new_page()
        self._page.on('framenavigated', self._navigation)
        # This MCP surface owns one page. Popups cannot create an unobserved browsing surface.
        self._context.on('page', lambda page: page.close())

    def _navigation(self, frame):
        if not self.origin.allows(frame.url):
            self._origin_failed = True
            self._context.close()

    def _check_origin(self):
        frames = self._page.frames
        initial = self._initial_document and len(frames) == 1 and self._page.url == 'about:blank'
        if self._origin_failed or not initial and any(not self.origin.allows(frame.url) for frame in frames):
            self._origin_failed = True
            self._context.close()
            raise OriginLocked('refused document origin')

    def _interact(self, operation):
        self._check_origin()
        self._initial_document = False
        operation()
        self._check_origin()

    def _gate(self, route):
        url = route.request.url
        if not self.origin.route_allows(url):
            self.aborted.append(url)
            route.abort('blockedbyclient')
            return
        # Playwright's default continuation follows redirects internally without
        # invoking this handler for every hop. Fetch exactly one hop, inspect its
        # Location, then let the browser request an admitted redirect separately.
        try:
            fetch_args = {'max_redirects': 0, 'timeout': 10000}
            if self._identity is not None:
                token = self._identity['mintToken']()
                self._identity_tokens.append(token)
                fetch_args['headers'] = {**route.request.headers,
                    'X-Honua-Edge-Auth': self._identity['edgeSecret'],
                    'X-Forwarded-User': self._identity['subject'],
                    'X-Forwarded-Access-Token': token.removeprefix('Bearer ')}
            response = route.fetch(**fetch_args)
            headers = dict(response.headers)
            location = headers.get('location')
            if 300 <= response.status < 400 and location and not self.origin.allows(urljoin(url, location)):
                self.aborted.append(urljoin(url, location))
                route.abort('blockedbyclient')
                return
            if route.request.resource_type == 'document':
                ws_origin = ('wss://' if self.origin.scheme == 'https' else 'ws://') + self.origin.origin.split('://', 1)[1]
                policy = f"connect-src 'self' {ws_origin}; frame-src 'self'; object-src 'none'; base-uri 'self'"
                previous = headers.get('content-security-policy')
                headers['content-security-policy'] = previous + ', ' + policy if previous else policy
            route.fulfill(response=response, headers=headers)
        except Exception:
            route.abort('blockedbyclient')

    @staticmethod
    def _cancel_download(download):
        try:
            download.cancel()
        except Exception:
            pass

    def goto(self, url):
        url = self.origin.resolve(url)
        self._initial_document = False
        response = self._page.goto(url, wait_until='load', timeout=30000)
        self._check_origin()
        return 200 if response is None else response.status

    def current_url(self):
        return self._page.url

    def pump_events(self):
        # Sync Playwright dispatches popup and navigation events only while its
        # event loop is running, including between model-selected tool calls.
        self._page.wait_for_timeout(50)

    def click(self, selector):
        self._interact(lambda: self._page.click(selector, timeout=5000))

    def fill(self, selector, text):
        self._interact(lambda: self._page.fill(selector, text, timeout=5000))

    def press(self, key):
        self._interact(lambda: self._page.keyboard.press(key))

    def set_input_files(self, selector, path):
        self._interact(lambda: self._page.set_input_files(selector, path, timeout=5000))

    def screenshot(self):
        self._check_origin()
        canvas = self._page.locator('#map')
        if canvas.count() and canvas.is_visible():
            png = canvas.screenshot(type='png', timeout=3000)
        else:
            png = self._page.screenshot(type='png', timeout=3000)
        self._check_origin()
        return png

    def snapshot_text(self):
        self._check_origin()
        body = self._page.locator('body')
        try:
            text = body.aria_snapshot(timeout=5000)
        except Exception:
            text = body.inner_text(timeout=5000)
        self._check_origin()
        return (text or '')[:8000]

    def close(self):
        try:
            self._browser.close()
        finally:
            self._pw.stop()


class StudioBrowserServer:
    """Loopback MCP server. Tools are the allowlist above; evaluate and devtools are absent."""

    def __init__(self, studio_url, action_log, fixture_dir, *, page_factory=None, sensitive=(), identity=None):
        self.studio_url = studio_url
        self.action_log = action_log
        self.fixture_dir = fixture_dir
        self.page_factory = page_factory
        self.identity = identity
        self.token = secrets.token_hex(24)
        self._sensitive = tuple(value for value in sensitive if isinstance(value, str) and len(value) >= 8)
        self.page = None
        self.endpoint = None
        self.server = None
        self.decoy = []
        self.lock = None
        self._owner = None
        self._http_thread = None
        self._poisoned = False
        self.owner_timeout = OWNER_TIMEOUT

    def start(self):
        from agent_e2e_local_api import McpEndpoint
        self.lock = OriginLock(self.studio_url)
        self._owner = _DaemonOwner()
        try:
            factory = self.page_factory or (lambda lock: PlaywrightPage(lock, identity=self.identity))
            self.page = self._owner.submit(factory, self.lock).result(timeout=self.owner_timeout)
            if isinstance(self.page, PlaywrightPage):
                self._owner.idle = self.page.pump_events
            self._serve()
        except Exception:
            if self.page is not None:
                self._owner.submit(self.page.close).result(timeout=CLOSE_TIMEOUT)
            self._owner.shutdown(wait=False)
            self._owner = None
            self.page = None
            raise
        self.endpoint = McpEndpoint(url=f'http://127.0.0.1:{self.server.server_address[1]}/mcp',
                                    header='Authorization', token='Bearer ' + self.token)
        return self.endpoint

    def _serve(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, status, raw=b''):
                self.send_response(status)
                if raw:
                    self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def authorized(self):
                presented = self.headers.get('Authorization', '')
                return secrets.compare_digest(presented, 'Bearer ' + server.token)

            def do_GET(self):
                self.send(405)

            def do_POST(self):
                if not self.authorized():
                    return self.send(401, b'{"error":"unauthorized"}')
                try:
                    length = int(self.headers.get('Content-Length') or 0)
                except ValueError:
                    return self.send(400)
                if length < 0 or length > MAX_BODY:
                    return self.send(413)
                try:
                    message = json.loads(self.rfile.read(length) or b'{}')
                except ValueError:
                    return self.send(400, b'{"error":"invalid json"}')
                if not isinstance(message, dict):
                    return self.send(400, b'{"error":"invalid json"}')
                method = message.get('method')
                if isinstance(method, str) and method.startswith('notifications/'):
                    return self.send(202)
                if method == 'initialize':
                    result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                              'serverInfo': {'name': 'honua-studio-browser', 'version': '1'}}
                elif method == 'ping':
                    result = {}
                elif method == 'tools/list':
                    result = {'tools': tool_catalog()}
                elif method == 'tools/call':
                    params = message.get('params') if isinstance(message.get('params'), dict) else {}
                    from agent_e2e_local_runner import ActionBoundExceeded
                    try:
                        status, text, digest = server.perform(params.get('name'), params.get('arguments'),
                                                              rpc_id=message.get('id'))
                    except ActionBoundExceeded:
                        return self.send(200, json.dumps(_rpc_error(message, -32000, 'action bound exceeded')).encode())
                    except BrowserUnavailable:
                        return self.send(200, json.dumps(_rpc_error(message, -32001, 'browser unavailable')).encode())
                    result = _tool_result(status, text, digest)
                else:
                    return self.send(200, json.dumps(_rpc_error(message, -32601, 'method not available')).encode())
                from agent_e2e_local_recorder import action_bytes
                self.send(200, action_bytes({'jsonrpc': '2.0', 'id': message.get('id'), 'result': result}))

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._http_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._http_thread.start()

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self._http_thread.join()
            self.server = None
        try:
            if self.page is not None:
                if self._owner is not None:
                    self._owner.idle = None
                    close = self._owner.submit(self.page.close)
                    try:
                        close.result(timeout=CLOSE_TIMEOUT)
                    except FutureTimeout:
                        self._poisoned = True
                else:
                    self.page.close()
        finally:
            self.page = None
            if self._owner is not None:
                self._owner.shutdown(wait=False)
                self._owner = None

    def perform(self, name, arguments, *, rpc_id=None):
        if self._poisoned:
            raise BrowserUnavailable('browser unavailable')
        if self._owner is not None:
            future = self._owner.submit(self._perform, name, arguments, rpc_id)
            try:
                return future.result(timeout=self.owner_timeout)
            except FutureTimeout as exc:
                self._poisoned = True
                future.cancel()
                raise BrowserUnavailable('browser action timed out') from exc
        return self._perform(name, arguments, rpc_id)

    def _perform(self, name, arguments, rpc_id):
        if not isinstance(arguments, dict):
            arguments = {}
        selected = {'kind': 'tool_call', 'tool': name if isinstance(name, str) else 'refused',
                    'arguments': arguments}
        from agent_e2e_local_recorder import action_bytes
        try:
            action_bytes(selected)
        except (TypeError, ValueError):
            selected['arguments'] = {'refused': 'unserializable'}

        def execute(_kind, _action):
            status, text = 'fail', 'refused'
            try:
                if not isinstance(name, str) or name not in TOOLS or set(arguments) != TOOL_ARGS[name]:
                    raise OriginLocked('tool is not available')
                text = getattr(self, '_' + name)(arguments)
                status = 'ok'
            except OriginLocked as exc:
                text = str(exc)
            except Exception as exc:
                # Keep failed browser calls in the action history without returning page/credential details.
                text = 'browser action failed (' + type(exc).__name__ + ')'
            png = b''
            if self.page is not None:
                try:
                    png = self.page.screenshot() or b''
                except Exception:
                    pass
            digest = hashlib.sha256(png).hexdigest() if png else None
            result = {'status': status, 'text': text[:8000], 'screenshotSha256': digest}
            # The action is written before execution. Its result, including the screenshot digest,
            # is retained separately and bound by the runner's response digest and action chain.
            response = {'jsonrpc': '2.0', 'id': rpc_id, 'result': _tool_result(status, text[:8000], digest)}
            return {**result, '_rawResponse': (200, action_bytes(response))}

        result = self.action_log.dispatch('tool_call', selected, execute)
        # Not evidence. Tests corrupt this to prove the sealed action log is the recorder's.
        self.decoy.append('side-log-is-not-evidence')
        return result['status'], result['text'], result['screenshotSha256']

    def _studio_navigate(self, arguments):
        url = self.lock.resolve(arguments['url'])
        self.page.goto(url)
        return 'navigated'

    def _studio_click(self, arguments):
        self.page.click(_selector(arguments['selector']))
        return 'clicked'

    def _studio_type(self, arguments):
        text = arguments['text']
        if not isinstance(text, str) or len(text) > 500 or '\x00' in text:
            raise OriginLocked('refused text')
        self.page.fill(_selector(arguments['selector']), text)
        return 'typed'

    def _studio_press(self, arguments):
        if not isinstance(arguments['key'], str) or not KEY.fullmatch(arguments['key']):
            raise OriginLocked('refused key')
        self.page.press(arguments['key'])
        return 'pressed'

    def _studio_snapshot(self, _arguments):
        text = self.page.snapshot_text()
        values = self._sensitive + (self.token,)
        if self.identity is not None:
            values += (self.identity['edgeSecret'],)
            values += tuple(getattr(self.page, '_identity_tokens', ()))
            values += tuple(token.removeprefix('Bearer ') for token in getattr(self.page, '_identity_tokens', ()))
        context = getattr(self.page, '_context', None)
        if context is not None:
            values += tuple(value for cookie in context.cookies()
                            if isinstance((value := cookie.get('value')), str) and len(value) >= 8)
        if any(value and value in text for value in values):
            raise OriginLocked('refused sensitive page content')
        from agent_cli_sessions import screen_model_input
        try:
            screen_model_input(text)
        except ValueError:
            raise OriginLocked('refused sensitive page content') from None
        return text

    def _studio_upload(self, arguments):
        path = resolve_fixture(self.fixture_dir, arguments['name'])
        self.page.set_input_files(_selector(arguments['selector']), str(path))
        return 'uploaded'


def tool_catalog():
    descriptions = {
        'studio_navigate': 'Open a page on the studio origin.',
        'studio_click': 'Click an element on the studio page.',
        'studio_type': 'Type into an element on the studio page.',
        'studio_press': 'Press a key on the studio page.',
        'studio_snapshot': 'Read the accessible text of the studio page.',
        'studio_upload': 'Choose a file from the provided fixture directory.',
    }
    properties = {
        'studio_navigate': {'url': {'type': 'string'}},
        'studio_click': {'selector': {'type': 'string'}},
        'studio_type': {'selector': {'type': 'string'}, 'text': {'type': 'string'}},
        'studio_press': {'key': {'type': 'string'}},
        'studio_snapshot': {},
        'studio_upload': {'selector': {'type': 'string'}, 'name': {'type': 'string'}},
    }
    return [{'name': name, 'description': descriptions[name], 'inputSchema': {
        'type': 'object', 'properties': properties[name], 'required': sorted(TOOL_ARGS[name]),
        'additionalProperties': False}} for name in TOOLS]


def _rpc_error(message, code, text):
    return {'jsonrpc': '2.0', 'id': message.get('id') if isinstance(message, dict) else None,
            'error': {'code': code, 'message': text}}


def _tool_result(status, text, digest):
    return {'content': [{'type': 'text', 'text': text}], 'isError': status != 'ok',
            'structuredContent': {'screenshotSha256': digest}}


def open_studio_browser(stack, action_log, *, identity=None):
    """Start the studio MCP server for one session. The runner stops it."""
    origin = stack.extra.get('consoleUrl') or stack.extra.get('studioUrl')
    require(isinstance(origin, str) and origin, 'case has no provisioned Console origin')
    fixture = stack.extra.get('studioFixtureDir') or str(Path(stack.workdir) / 'studio-fixtures')
    Path(fixture).mkdir(parents=True, exist_ok=True)
    sensitive = (stack.admin_key, stack.approver_key, os.environ.get('HONUA_CORPUS_AGENT_KEY'),
                 os.environ.get('HONUA_CORPUS_BOOTSTRAP_KEY'))
    server = StudioBrowserServer(origin, action_log, fixture, page_factory=stack.extra.get('studioPageFactory'),
                                 sensitive=sensitive, identity=identity)
    server.start()
    return server


def client_mcp_config(honua, studio=None):
    """Claude and Codex MCP config for Honua plus the studio browser. The token is a header, not a URL."""
    def one(endpoint):
        if endpoint.token in endpoint.url:
            raise OriginLocked('mcp token must not be placed in the url')
        return {'type': 'http', 'url': endpoint.url, 'headers': {endpoint.header: endpoint.token}}
    servers = {'honua': one(honua)}
    if studio is not None:
        servers['studio'] = one(studio)
    return {'claude': {'mcpServers': servers},
            'codex': {'mcp_servers': {name: {'url': item['url'], 'http_headers': item['headers']}
                                      for name, item in servers.items()}}}


def fixture_path(variant, name):
    require(variant in ('A', 'B') and name in LAYER_NAMES, 'unknown fixture layer')
    return CORPUS / f'town-{variant}' / f'{name}.3857.geojson'


def fixture_extent(variant):
    """Exact union of the three town layers in EPSG:3857, with no padding."""
    xs, ys = [], []

    def walk(node):
        if isinstance(node, list):
            if len(node) >= 2 and not isinstance(node[0], list) and all(
                    type(value) in (int, float) and not isinstance(value, bool) for value in node[:2]):
                xs.append(node[0])
                ys.append(node[1])
            else:
                for item in node:
                    walk(item)
    for name in LAYER_NAMES:
        document = json.loads(fixture_path(variant, name).read_text(encoding='utf-8'))
        for feature in document['features']:
            walk(feature['geometry']['coordinates'])
    require(xs and ys, 'fixture has no coordinates')
    return [min(xs), min(ys), max(xs), max(ys)]


def map_spec(styled):
    """MapLibre document with no geometries, so the style hash does not depend on the town variant."""
    layers = [
        {'id': 'parcels', 'type': 'fill', 'source': 'parcels',
         'paint': {'fill-color': '#d9d3c5', 'fill-opacity': 1}},
        {'id': 'flood_zones', 'type': 'fill', 'source': 'flood_zones',
         'paint': {'fill-color': FLOOD_COLOR if styled else '#cccccc',
                   'fill-opacity': FLOOD_OPACITY if styled else 1}},
        {'id': 'shelters', 'type': 'circle', 'source': 'shelters',
         'paint': {'circle-color': '#222222', 'circle-radius': 6}},
    ]
    if styled:
        layers.append({'id': 'shelter-names', 'type': 'symbol', 'source': 'shelters',
                       'metadata': {'honua:compositionLayerId': 'shelters'},
                       'layout': {'text-field': ['to-string', ['get', 'name']]}})
    return {'version': 8, 'sources': {name: {'type': 'geojson'} for name in LAYER_NAMES}, 'layers': layers}


def map_body(variant, extent, styled):
    return {'mapPackageId': 'synthetic-town-' + variant.lower(), 'format': 'honua_map_package.v1',
            'layers': [{'id': name, 'visible': True} for name in LAYER_NAMES],
            'view': {'bbox': list(extent)}, 'mapSpec': map_spec(styled)}


def package_body(document):
    node = document
    if isinstance(node, dict) and isinstance(node.get('data'), dict):
        node = node['data']
    envelope = node.get('envelope') if isinstance(node, dict) else None
    if isinstance(envelope, dict) and isinstance(envelope.get('body'), dict):
        return envelope['body']
    if isinstance(node, dict) and isinstance(node.get('body'), dict):
        return node['body']
    raise ProjectionError('version document has no map body')


def composition_from_body(body):
    layers, view = body.get('layers'), body.get('view')
    require(isinstance(layers, list) and layers and isinstance(view, dict) and isinstance(view.get('bbox'), list)
            and len(view['bbox']) == 4, 'map composition is incomplete')
    ordered, visibility = [], {}
    for row in layers:
        require(isinstance(row, dict) and isinstance(row.get('id'), str) and row['id'] and type(row.get('visible')) is bool,
                'map layer is incomplete')
        ordered.append(row['id'])
        visibility[row['id']] = row['visible']
    require(len(set(ordered)) == len(ordered), 'map layers are duplicated')
    require(all(type(value) in (int, float) and not isinstance(value, bool) for value in view['bbox']),
            'map extent is not numeric')
    return {'orderedLayerIds': ordered, 'visibility': visibility, 'extent': list(view['bbox'])}


def style_from_body(body):
    from agent_e2e_studio_style_observer import project
    spec = body.get('mapSpec')
    require(isinstance(spec, dict), 'map has no style document')
    return project(spec, STYLE_CONFIG)


def saved_default_fill_export(body, *, service, layer_id):
    """Translate one unstyled saved fill into a bounded server export request.

    Studio's default fill palette and opacity are defined by its composition
    palette and style-presets modules. This deliberately refuses authored
    styles and multiple layers until their semantics are independently mapped.
    The source identity must come from the runner's published fixture binding.
    """
    from urllib.parse import urlencode
    import math
    require(isinstance(body, dict) and 'mapSpec' not in body, 'unsupported saved map style')
    require(isinstance(service, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', service)
            and type(layer_id) is int and 0 <= layer_id <= 2147483647, 'invalid published layer')
    layers, bindings = body.get('layers'), body.get('sourceBindings')
    require(isinstance(layers, list) and len(layers) == 1 and isinstance(layers[0], dict)
            and isinstance(bindings, list) and len(bindings) == 1 and isinstance(bindings[0], dict),
            'default export requires one bound layer')
    layer, binding = layers[0], bindings[0]
    layer_name = layer.get('id')
    require(isinstance(layer_name, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', layer_name)
            and layer.get('type') == 'fill' and layer.get('visible') is True
            and 'styleRef' not in layer and binding.get('sourceId') == layer.get('sourceId')
            and binding.get('protocol') == 'geoservices_feature_service', 'unsupported saved layer')
    locator = binding.get('locator')
    require(isinstance(locator, dict) and locator.get('layerId') == layer_id
            and locator.get('url') == f'/rest/services/{service}/FeatureServer/{layer_id}',
            'saved map source differs from fixture')
    view = body.get('view')
    bbox = view.get('bbox') if isinstance(view, dict) else None
    require(isinstance(bbox, list) and len(bbox) == 4 and all(type(v) in (int, float) and math.isfinite(v) for v in bbox)
            and -180 <= bbox[0] < bbox[2] <= 180 and -85 <= bbox[1] < bbox[3] <= 85,
            'saved map lacks a bounded longitude/latitude extent')
    # FNV-1a and palette from honua-studio/src/composition/palette.ts.
    palette = ('#0b6b4d', '#b4531b', '#3a5fa8', '#8a2f6b', '#1f7a8c', '#94741a', '#5c3b8f', '#a03a3a')
    hashed = 0x811c9dc5
    for char in layer_name:
        hashed = ((hashed ^ ord(char)) * 0x01000193) & 0xffffffff
    color = palette[hashed % len(palette)]
    rgb = [int(color[index:index + 2], 16) for index in (1, 3, 5)]
    rgba = (*rgb, 115)  # Studio's default fill opacity is 0.45.
    dynamic = [{'id': layer_id, 'source': {'type': 'mapLayer', 'mapLayerId': layer_id},
                'drawingInfo': {'renderer': {'type': 'simple', 'symbol': {
                    'type': 'esriSFS', 'style': 'esriSFSSolid', 'color': list(rgba),
                    'outline': {'type': 'esriSLS', 'style': 'esriSLSSolid', 'color': rgb + [255], 'width': 1}}}}}]
    query = urlencode({'f': 'image', 'bbox': ','.join(str(value) for value in bbox), 'bboxSR': 4326,
                       'imageSR': 3857, 'size': '512,512', 'format': 'png32', 'transparent': 'true',
                       'dynamicLayers': json.dumps(dynamic, separators=(',', ':'))})
    return f'/rest/services/{service}/MapServer/export?{query}', rgba


def _fixture_filter_sql(expression, fields, depth=0):
    """Translate a bounded subset of MapLibre filters without accepting raw SQL."""
    import math
    require(depth <= 8 and isinstance(expression, list) and 2 <= len(expression) <= 12,
            'unsupported authored filter')
    op = expression[0]
    if op in ('all', 'any'):
        require(len(expression) >= 3, 'unsupported authored filter')
        joiner = ' AND ' if op == 'all' else ' OR '
        return '(' + joiner.join(_fixture_filter_sql(item, fields, depth + 1) for item in expression[1:]) + ')'
    require(op in ('==', '!=', '>', '>=', '<', '<=') and len(expression) == 3, 'unsupported authored filter')
    lookup, value = expression[1:]
    require(isinstance(lookup, list) and len(lookup) == 2 and lookup[0] == 'get'
            and isinstance(lookup[1], str) and lookup[1] in fields, 'unsupported authored filter field')
    if type(value) is bool:
        literal = 'TRUE' if value else 'FALSE'
    elif type(value) in (int, float) and math.isfinite(value):
        literal = str(value)
    elif isinstance(value, str) and len(value) <= 200 and all(ord(char) >= 32 for char in value):
        literal = "'" + value.replace("'", "''") + "'"
    else:
        raise ProjectionError('unsupported authored filter value')
    return lookup[1] + ' ' + {'==': '=', '!=': '<>'}.get(op, op) + ' ' + literal


def saved_authored_exports(body, fixtures, *, variant='A'):
    """Translate supported literal saved layer paint to fixture-bound server exports.

    Unsupported expressions and external source bindings fail closed. The
    caller must independently inspect the PNGs and verify labels; request creation
    by itself is never render evidence.
    """
    import math
    from urllib.parse import urlencode
    require(isinstance(body, dict) and isinstance(fixtures, dict), 'invalid authored map')
    layers, bindings, spec = body.get('layers'), body.get('sourceBindings'), body.get('mapSpec')
    require(isinstance(layers, list) and isinstance(bindings, list) and isinstance(spec, dict)
            and spec.get('version') == 8 and isinstance(spec.get('layers'), list), 'authored map is incomplete')
    view = body.get('view')
    bbox = view.get('bbox') if isinstance(view, dict) else None
    require(isinstance(bbox, list) and len(bbox) == 4 and all(type(v) in (int, float) and math.isfinite(v) for v in bbox)
            and -180 <= bbox[0] < bbox[2] <= 180 and -85 <= bbox[1] < bbox[3] <= 85, 'invalid map extent')
    require(len(layers) == len(fixtures) and {row.get('id') for row in layers if isinstance(row, dict)} == set(fixtures),
            'saved layers differ from fixture')
    result = []
    for layer in layers:
        name, source = layer.get('id'), layer.get('sourceId')
        require(set(layer) <= {'id', 'type', 'title', 'visible', 'sourceId', 'metadata'},
                'unsupported composition layer property')
        fixture = fixtures[name]
        service, layer_id = fixture.get('service'), fixture.get('layerId')
        require(isinstance(service, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', service)
                and type(layer_id) is int and 0 <= layer_id <= 2147483647, 'invalid fixture layer')
        matching = [row for row in bindings if isinstance(row, dict) and row.get('sourceId') == source]
        require(isinstance(source, str) and source and len(matching) == 1 and layer.get('visible') is True
                and matching[0].get('protocol') == 'geoservices_feature_service'
                and matching[0].get('locator') == {'url': f'/rest/services/{service}/FeatureServer/{layer_id}',
                                                   'layerId': layer_id}, 'saved source differs from fixture')
        rules = [row for row in spec['layers'] if isinstance(row, dict) and row.get('id') == name]
        require(len(rules) == 1 and set(rules[0]) <= {'id', 'type', 'source', 'paint', 'layout', 'metadata', 'filter'}
                and rules[0].get('source') in (name, source)
                and rules[0].get('type') == layer.get('type'), 'unsupported authored rule')
        rule = rules[0]
        layout, paint = rule.get('layout', {}), rule.get('paint', {})
        require(isinstance(layout, dict) and set(layout) <= {'visibility'} and layout.get('visibility', 'visible') == 'visible'
                and isinstance(paint, dict), 'invalid authored paint')
        kind = rule['type']
        require(kind in ('fill', 'circle'), 'unsupported authored symbol')
        allowed = {'fill-color', 'fill-opacity'} if kind == 'fill' else {'circle-color', 'circle-opacity', 'circle-radius'}
        require(set(paint) <= allowed, 'unsupported authored paint property')
        color, opacity = paint.get(kind + '-color'), paint.get(kind + '-opacity', 1)
        require(isinstance(color, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', color)
                and type(opacity) in (int, float) and math.isfinite(opacity) and 0 <= opacity <= 1,
                'unsupported authored color')
        rgba = [int(color[i:i + 2], 16) for i in (1, 3, 5)] + [round(opacity * 255)]
        symbol = {'type': 'esriSFS', 'style': 'esriSFSSolid', 'color': rgba} if kind == 'fill' else {
            'type': 'esriSMS', 'style': 'esriSMSCircle', 'color': rgba}
        if kind == 'circle':
            radius = paint.get('circle-radius', 5)
            require(type(radius) in (int, float) and math.isfinite(radius) and 0 < radius <= 100,
                    'unsupported authored radius')
            symbol['size'] = radius * 2
        dynamic = [{'id': layer_id, 'source': {'type': 'mapLayer', 'mapLayerId': layer_id},
                    'drawingInfo': {'renderer': {'type': 'simple', 'symbol': symbol}}}]
        if 'filter' in rule:
            document = json.loads(fixture_path(variant, name).read_text(encoding='utf-8'))
            fields = {field for feature in document['features'] for field in feature['properties']}
            dynamic[0]['definitionExpression'] = _fixture_filter_sql(rule['filter'], fields)
        query = urlencode({'f': 'image', 'bbox': ','.join(str(v) for v in bbox), 'bboxSR': 4326,
                           'imageSR': 3857, 'size': '512,512', 'format': 'png32', 'transparent': 'true',
                           'dynamicLayers': json.dumps(dynamic, separators=(',', ':'))})
        result.append({'name': name, 'path': f'/rest/services/{service}/MapServer/export?{query}', 'rgba': rgba})
    return result


def verify_saved_authored_layers(recorder, version_step, prefix, *, fixtures, expected_rgba, regions, minimum_pixels):
    """Verify each saved, fixture-bound literal rule against controller-fetched pixels.

    This checks the individual server exports only. It does not assert that text
    labels were rendered, or that the layers form a correct composite image.
    """
    import base64
    limits = ({name: minimum_pixels for name in fixtures} if type(minimum_pixels) is int else minimum_pixels)
    require(version_step in recorder.records and isinstance(expected_rgba, dict)
            and set(expected_rgba) == set(fixtures) and isinstance(regions, dict) and set(regions) == set(fixtures)
            and isinstance(limits, dict) and set(limits) == set(fixtures)
            and all(type(value) is int and value > 0 for value in limits.values()),
            'invalid frozen authored expectations')
    exports = saved_authored_exports(package_body(recorder.records[version_step].json), fixtures)
    matches = {}
    for row in exports:
        wanted = expected_rgba[row['name']]
        require(isinstance(wanted, (list, tuple)) and len(wanted) == 4
                and all(type(value) is int and 0 <= value <= 255 for value in wanted)
                and row['rgba'] == list(wanted), 'authored style differs from frozen expectation')
        observation = recorder.json(prefix + '-' + row['name'].replace('_', '-'), 'GET', row['path'])['observation']
        require(observation.get('httpStatus') == 200, 'server layer export failed')
        try:
            raw = base64.b64decode(observation['rawBodyBase64'], validate=True)
            width, height, pixel = _decode_png(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProjectionError('server layer export is not a decodable PNG') from exc
        require(width == 512 and height == 512, 'server layer export has unexpected dimensions')
        rect = regions[row['name']]
        require(isinstance(rect, (tuple, list)) and len(rect) == 4 and all(type(v) is int for v in rect),
                'invalid frozen layer region')
        left, top, wide, high = rect
        require(0 <= left < width and 0 <= top < height and wide > 0 and high > 0
                and left + wide <= width and top + high <= height, 'invalid frozen layer region')
        count = sum(_close(pixel(x, y), wanted, 2)
                    for y in range(top, top + high) for x in range(left, left + wide))
        require(count >= limits[row['name']], 'server layer pixels differ from frozen expectation')
        matches[row['name']] = count
    return {'matchedPixels': matches}


def verify_saved_default_fill(recorder, version_step, export_step, *, service, layer_id, expected_rgba, minimum_pixels):
    """Check a recorded saved version against controller-fetched server pixels.

    This narrow proof is not the P08/P09/P24 render projection: it accepts only
    a single default fill, and does not claim to observe labels or authored styles.
    """
    import base64
    require(isinstance(expected_rgba, (tuple, list)) and len(expected_rgba) == 4
            and all(type(v) is int and 0 <= v <= 255 for v in expected_rgba)
            and type(minimum_pixels) is int and minimum_pixels > 0, 'invalid frozen render expectation')
    require(version_step in recorder.records, 'saved map version was not recorded')
    body = package_body(recorder.records[version_step].json)
    path, color = saved_default_fill_export(body, service=service, layer_id=layer_id)
    answer = recorder.json(export_step, 'GET', path)
    observation = answer['observation']
    require(observation['httpStatus'] == 200, 'server export failed')
    try:
        raw = base64.b64decode(observation['rawBodyBase64'], validate=True)
        width, height, pixel = _decode_png(raw)
    except (ValueError, TypeError) as exc:
        raise ProjectionError('server export is not a decodable PNG') from exc
    require(width >= 256 and height >= 256, 'server export is too small')
    matched = sum(_close(pixel(x, y), expected_rgba, 2) for y in range(height) for x in range(width))
    require(tuple(color) == tuple(expected_rgba) and matched >= minimum_pixels,
            'saved map render does not match the frozen fill expectation')
    return {'matchedPixels': matched, 'width': width, 'height': height,
            'versionStep': version_step, 'exportStep': export_step}


def saved_digest(body):
    from agent_e2e_pack import canonical_digest
    return canonical_digest(body)[7:]


def record_map_measurements(recorder, version_step, prefix):
    body = package_body(recorder.records[version_step].json)
    recorder.store_derived(prefix + '-composition', composition_from_body(body))
    if prefix != 'p08' and 'mapSpec' in body:
        recorder.store_derived(prefix + '-style', style_from_body(body))
    recorder.store_derived(prefix + '-saved', {'documentSha256': saved_digest(body)})


def snapshot_sources(recorder, prefix, layers, when, *, variant='A'):
    from agent_e2e_p10_observer import source_path
    from urllib.parse import quote
    for name, slug in SOURCE_LAYERS:
        raw = f'{prefix}-src-{slug}-{when}-raw'
        fixture = json.loads(fixture_path(variant, name).read_text(encoding='utf-8'))
        fields = sorted({field for feature in fixture['features'] for field in feature['properties']})
        require('source_id' in fields and all(re.fullmatch(r'[a-z][a-z0-9_]*', f) for f in fields),
                'fixture has unsupported source fields')
        path = source_path(layers[name]).replace('outFields=%2A', 'outFields=' + quote(','.join(['id'] + fields), safe=''))
        result = recorder.json(raw, 'GET', path)['observation']
        body = result.get('body')
        require(result.get('httpStatus') == 200 and isinstance(body, dict) and 'error' not in body
                and isinstance(body.get('features'), list),
                'published fixture source cannot be read with the required fields')


def items_of(body):
    require(isinstance(body, dict), 'map list is not an object')
    data = body['data'] if isinstance(body.get('data'), dict) else body
    rows = data.get('items')
    require(isinstance(rows, list), 'map list has no items')
    return rows


def version_path(item_id, version_id):
    require(isinstance(item_id, str) and isinstance(version_id, str) and IDENT.fullmatch(item_id)
            and IDENT.fullmatch(version_id), 'map version identity is not addressable')
    return f'/api/v1/studio/content-items/{item_id}/versions/{version_id}'


def seed_unstyled_map(recorder, variant, extent, owner):
    envelope = {'family': 'map', 'schemaVersion': '1', 'format': 'honua_map_package.v1',
                'body': map_body(variant, extent, False)}
    created = recorder.json('p09-seed-draft', 'POST', '/api/v1/studio/package-drafts', {
        'packageKey': 'synthetic-town-' + variant.lower(), 'ownerId': owner, 'envelope': envelope})
    require(created['observation']['httpStatus'] in (200, 201), 'unstyled map draft was not created')
    data = created['observation']['body']
    data = data.get('data', data) if isinstance(data, dict) else {}
    draft_id = data.get('draftId') if isinstance(data, dict) else None
    require(isinstance(draft_id, str) and IDENT.fullmatch(draft_id), 'unstyled map draft has no id')
    saved = recorder.json('p09-seed-save', 'POST', f'/api/v1/studio/package-drafts/{draft_id}/content-versions', {})
    require(saved['observation']['httpStatus'] in (200, 201), 'unstyled map was not saved')


def seed_case(scenario_id, stack, variant, *, create_map):
    from agent_e2e_local_api import Seeded
    key = scenario_id.lower() + 'Seed'
    if key in stack.extra:
        if stack.extra[key]['variant'] != variant:
            raise ValueError('case already seeded for a different variant')
        return Seeded(stack.extra[key])
    if variant not in ('A', 'B'):
        raise ValueError('unsupported variant')
    stack.extra['needsStudioBrowser'] = True
    origin = stack.extra.get('consoleUrl') or stack.extra.get('studioUrl')
    if not origin and callable(stack.extra.get('startConsole')):
        origin = stack.extra['startConsole']()
    require(isinstance(origin, str) and origin, 'case has no provisioned Console origin')
    user = stack.extra.get('consoleUserId')
    require(isinstance(user, str) and user and stack.extra.get('validatedConsoleCustomer') == user,
            'candidate has not authenticated the runner customer')
    stack.extra['studioUrl'] = origin
    # Expose only files the runner has already admitted from the frozen customer
    # inventory. The corpus directory also contains controller-only SQL.
    fixture_dir = Path(stack.workdir) / 'studio-fixtures'
    fixture_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    for attachment in stack.extra.get('customerFiles', ()):
        name = attachment.get('name')
        require(isinstance(name, str) and Path(name).name == name and name not in ('', '.', '..'),
                'invalid staged customer fixture name')
        raw = Path(attachment['path']).read_bytes()
        require('sha256:' + hashlib.sha256(raw).hexdigest() == attachment['sha256'],
                'staged customer fixture changed')
        from agent_e2e_local_recorder import write_private
        write_private(fixture_dir / name, raw)
    stack.extra['studioFixtureDir'] = str(fixture_dir)
    recorder = stack.extra['recorder']
    prefix = scenario_id.lower()
    if scenario_id == 'P24':
        layers = dict(stack.extra.get('publishedLayers') or {})
    else:
        from agent_e2e_scenario_backend import TownSetup
        if 'connectionId' not in stack.extra:
            stack.extra['connectionId'] = stack.extra['ensureConnection']()
        namespace = prefix + '_' + stack.compose_project.replace('-', '')[-16:]
        town = TownSetup(recorder, CORPUS / 'manifest.v1.json').prepare(
            variant=variant, namespace=namespace, starting_state='published-town',
            connection_id=stack.extra['connectionId'])
        layers = {name: {'service': town['layers'][name]['service'], 'layerId': town['layers'][name]['layerId']}
                  for name in LAYER_NAMES}
    if layers:
        snapshot_sources(recorder, prefix, layers, 'before', variant=variant)
    if create_map:
        seed_unstyled_map(recorder, variant, fixture_extent(variant), stack.extra.get('agentPrincipal'))
    rows = items_of(recorder.json(prefix + '-maps-before', 'GET', LIST_PATH)['observation']['body'])
    if create_map:
        require(len(rows) == 1, 'saved map seed did not leave one map')
    state = {'variant': variant, 'layers': layers,
             'mapId': rows[0]['itemId'] if rows else None,
             'versionId': rows[0].get('currentVersionId') if rows else None}
    stack.extra[key] = state
    return Seeded(state)


def observe_map(stack, seeded, prefix, *, created):
    recorder = stack.extra['recorder']
    layers = seeded.state.get('layers') or {}
    if layers:
        snapshot_sources(recorder, prefix, layers, 'after', variant=seeded.state['variant'])
    listing = recorder.json(prefix + '-maps', 'GET', LIST_PATH)
    rows = items_of(listing['observation']['body'])
    if created:
        before = {row.get('itemId') for row in items_of(recorder.records[prefix + '-maps-before'].json)}
        owners = {stack.extra.get('agentPrincipal'), stack.extra.get('consoleUserId')}
        matches = [row for row in rows if row.get('itemId') not in before and row.get('ownerId') in owners]
        require(len(matches) == 1, 'map list does not identify one new map')
        row = matches[0]
    else:
        matches = [row for row in rows if row.get('itemId') == seeded.state.get('mapId')]
        require(len(matches) == 1, 'seeded map is not in the live list')
        row = matches[0]
    recorder.json(prefix + '-version', 'GET', version_path(row.get('itemId'), row.get('currentVersionId')))
    record_map_measurements(recorder, prefix + '-version', prefix)
    # There is no reviewed server observation that binds a rendered image and
    # visible labels to this saved document. Do not synthesize that evidence.


def composition_spec(prefix, role):
    return {'object': {
        'mapId': {'target': prefix + '-version', 'role': role, 'at': {'pathSegment': 4}},
        'orderedLayerIds': {'body': prefix + '-composition', 'pointer': '/orderedLayerIds'},
        'visibility': {'body': prefix + '-composition', 'pointer': '/visibility'},
        'extent': {'body': prefix + '-composition', 'pointer': '/extent'}}}


def style_spec(prefix, role):
    return {'object': {
        'mapId': {'target': prefix + '-version', 'role': role, 'at': {'pathSegment': 4}},
        'styleDocumentSha256': {'body': prefix + '-style', 'pointer': '/styleDocumentSha256'},
        'labelField': {'body': prefix + '-style', 'pointer': '/labelField'},
        'floodColor': {'body': prefix + '-style', 'pointer': '/floodColor'},
        'floodOpacity': {'body': prefix + '-style', 'pointer': '/floodOpacity'}}}


def render_spec(prefix):
    return {'body': prefix + '-render', 'pointer': ''}


def saved_spec(prefix, resource_role, version_role):
    return {'object': {
        'resourceId': {'target': prefix + '-version', 'role': resource_role, 'at': {'pathSegment': 4}},
        'versionId': {'target': prefix + '-version', 'role': version_role, 'at': {'pathSegment': 6}},
        'documentSha256': {'body': prefix + '-saved', 'pointer': '/documentSha256'}}}


def source_content_spec(prefix):
    return {'protectedContent': [
        {'label': name, 'before': f'{prefix}-src-{slug}-before-raw', 'after': f'{prefix}-src-{slug}-after-raw',
         'resource': [
             {'role': f'seeded:service:{name}', 'at': {'pathSegment': 2}},
             {'role': f'seeded:layer:{name}', 'at': {'pathSegment': 4}}],
         'content': {'featureServerFeatures': '$step'}}
        for name, slug in SOURCE_LAYERS]}


def source_roles():
    from agent_e2e_local_api import RoleBinding
    return [binding for name, slug in SOURCE_LAYERS for binding in (
        RoleBinding(f'seeded:service:{name}', f'publish-{slug}-submit', pointer='/data/resourceIds/serviceName'),
        RoleBinding(f'seeded:layer:{name}', f'publish-{slug}-submit', pointer='/data/resourceIds/layerId'))]


def handoff_spec(prefix, role):
    return {'object': {'resources': {'list': []}, 'facts': {'object': {
        'mapId': {'role': role},
        'orderedLayerIds': {'body': prefix + '-composition', 'pointer': '/orderedLayerIds'}}}}}


def label_ids(scenario, variant):
    if scenario == 'P08':
        return []
    if scenario == 'P09':
        document = json.loads(fixture_path(variant, 'shelters').read_text(encoding='utf-8'))
        return sorted(feature['properties']['source_id'] for feature in document['features'])
    facts = json.loads((CORPUS / 'oracles' / f'town-{variant}.json').read_text(encoding='utf-8'))
    return list(facts['openCapacityOver50Ids'])
