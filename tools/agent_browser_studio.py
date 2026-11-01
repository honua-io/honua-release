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
import copy
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
COMPOSITE_CONTRACT_PATH = ROOT / 'certification/agent-e2e/expectations/local/studio-composite-contract.json'
CONSOLE_PIN_PATH = ROOT / 'certification/agent-e2e/expectations/local/studio-console-image.json'
GLYPH_DIR = ROOT / 'certification/agent-e2e/expectations/local/studio-glyphs'
RENDER_ASSETS = {
    'maplibre.js': ('maplibre-gl-D8l5aSvm.js', '22f45992fbbac596986073bf39f15834c5e37df78e338b10fde48d3bdf316caa'),
    'worker.js': ('assets/maplibre-gl-worker-BRmM5V8w.js', '5420da18c04dbefda660133d62583f6abe709233ca45a01139db121fe172f107'),
}
GLYPH_HASHES = {
    '0-255.pbf': '0c8bdb284f2ad6f69bbb434a0208393f3a517bc8bf7ed1eccc4ac4791ebbcbfe',
    '256-511.pbf': '9962ad38d064edbeb226a6c7dcf1d58e7678508a805f980d2fe81bb3178ba68a',
    '8192-8447.pbf': 'a57611157f0597b267ef36eb07ce5a41dddcc3b2f3b5b85779108b7bb48b1cde',
}
LIST_PATH = '/api/v1/studio/content-items?family=map&limit=200'
LAYER_NAMES = ('parcels', 'flood_zones', 'shelters')
SOURCE_LAYERS = (('parcels', 'parcels'), ('flood_zones', 'flood-zones'), ('shelters', 'shelters'))
STYLE_CONFIG = {'floodLayerId': 'flood_zones', 'shelterLayerId': 'shelters'}
FLOOD_COLOR = '#0088ff'
FLOOD_OPACITY = 0.35
RENDER_MINIMUM_NON_BACKGROUND = 1000


def studio_console_image():
    """The reviewed case Console image, independent of mutable tags."""
    pin = json.loads(CONSOLE_PIN_PATH.read_text(encoding='utf-8'))
    require(isinstance(pin, dict) and set(pin) == {'image', 'sourceRevision'}
            and isinstance(pin['image'], str)
            and re.fullmatch(r'ghcr\.io/honua-io/honua-console@sha256:[0-9a-f]{64}', pin['image'])
            and isinstance(pin['sourceRevision'], str) and re.fullmatch(r'[0-9a-f]{40}', pin['sourceRevision']),
            'invalid frozen Console image')
    return pin
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
        server_host = stack.extra.get('serverHost', 'server')
        if server_host not in ('server', 'honua'):
            raise ValueError('invalid case server host')
        self.name = stack.compose_project + '-console-' + secrets.token_hex(16)
        self._project = stack.compose_project
        self._docker_host = 'ssh://' + self.host
        self._attempted = False
        self._created = False
        self._tunnel = None
        self.url = None
        env = dict(os.environ, DOCKER_HOST=self._docker_host)
        env.pop('HONUA_ADMIN_API_KEY', None)
        for name in ('HONUA_AGENT_API_KEY', 'HONUA_NATIVE_MODEL_ENABLED', 'HONUA_AGENT_RELEASE_ID'):
            env.pop(name, None)
        command = ['docker', 'run', '--detach', '--name', self.name,
                   '--label', 'honua.corpus.case=' + stack.compose_project,
                   '--network', stack.compose_project + '_default', '--publish', '127.0.0.1::8080',
                   '--env', 'ASPNETCORE_ENVIRONMENT=Development', '--env', 'ASPNETCORE_URLS=http://+:8080',
                   '--env', 'Studio__EmbedJsStudio=true',
                   '--env', 'HONUA_SERVER_BASE_URL=http://' + server_host + ':8080',
                   '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
                   '--tmpfs', '/tmp:noexec,nosuid,size=100m', image]
        if edge_secret is not None:
            if not isinstance(edge_secret, str) or len(edge_secret) < 32:
                raise ValueError('Console edge secret is invalid')
            env['Honua__Console__Auth__EdgeForwarded__SharedSecret'] = edge_secret
            command[-1:-1] = ['--env', 'Honua__Console__Auth__Mode=EdgeForwarded',
                               '--env', 'Honua__Console__Auth__EdgeForwarded__ServerSessionIssuer=https://terminal-journey.invalid',
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

    def __init__(self, origin, identity=None, assistant_log=None, stage_gate=None, refusal_log=None):
        use_worktree_browsers()
        from playwright.sync_api import sync_playwright
        self.origin = origin
        self.aborted = []
        self._initial_document = True
        self._origin_failed = False
        self._identity = identity
        self._assistant_log = assistant_log
        self._refusal_log = refusal_log
        self._stage_gate = stage_gate
        self._assistant_model_calls = 0
        self._assistant_tool_calls = 0
        self._assistant_bound_hit = False
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
        if getattr(self, '_stage_gate', None) is not None and self._stage_gate.closed:
            route.abort('blockedbyclient')
            return
        if not self.origin.route_allows(url):
            self.aborted.append(url)
            route.abort('blockedbyclient')
            return
        # The Console assistant uses a long-lived SSE response. Browser CSP on
        # the admitted document enforces same-origin connect redirects, while
        # direct continuation allows response chunks to reach the chat promptly.
        if (route.request.method == 'POST'
                and urlsplit(url).path == '/api/agent-workspace/studio/ai'):
            if self._assistant_log is None:
                self.aborted.append(url)
                route.abort('blockedbyclient')
                if self._refusal_log is not None:
                    self._refusal_log.names.add('studio_ai.chat')
                    selected = {'kind': 'tool_call', 'tool': 'studio_ai.chat', 'arguments': {}}
                    self._refusal_log.dispatch('tool_call', selected,
                        lambda _kind, _action: {'status': 'fail', 'refusal': 'out-of-scope'})
                return
            if self._assistant_log is not None:
                if self._assistant_model_calls >= 60:
                    self._assistant_bound_hit = True
                    route.abort('blockedbyclient')
                    return
                self._assistant_model_calls += 1
            headers = dict(route.request.headers)
            if self._identity is not None:
                token = self._identity['mintToken']()
                self._identity_tokens.append(token)
                headers.update({'X-Honua-Edge-Auth': self._identity['edgeSecret'],
                                'X-Forwarded-User': self._identity['subject'],
                                'X-Forwarded-Access-Token': token.removeprefix('Bearer ')})
            route.continue_(headers=headers)
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
            def fetch():
                response = route.fetch(**fetch_args)
                headers = dict(response.headers)
                location = headers.get('location')
                if 300 <= response.status < 400 and location and not self.origin.allows(urljoin(url, location)):
                    self.aborted.append(urljoin(url, location))
                    raise OriginLocked('refused cross-origin redirect')
                if route.request.resource_type == 'document':
                    ws_origin = ('wss://' if self.origin.scheme == 'https' else 'ws://') + self.origin.origin.split('://', 1)[1]
                    policy = f"connect-src 'self' {ws_origin}; frame-src 'self'; object-src 'none'; base-uri 'self'"
                    previous = headers.get('content-security-policy')
                    headers['content-security-policy'] = previous + ', ' + policy if previous else policy
                route.fulfill(response=response, headers=headers)
                return response

            # Capture native Studio lifecycle traffic independently of model claims.
            # This route is reached only by the origin-locked signed-in browser.
            path = urlsplit(url).path
            lifecycle = (path == '/api/studio/v1/studio/package-drafts' or
                         re.fullmatch(r'/api/studio/v1/studio/package-drafts/[A-Za-z0-9-]+/content-versions', path))
            if lifecycle and getattr(self, '_refusal_log', None) is not None and route.request.method == 'POST':
                from datetime import datetime, timezone
                tool = 'studio_ui_create' if path.endswith('/package-drafts') else 'studio_ui_save'
                selected = {'kind': 'tool_call', 'tool': tool, 'arguments': {
                    'path': path, 'startedAt': datetime.now(timezone.utc).isoformat()}}
                self._refusal_log.names.add(tool)
                def lifecycle_request(_kind, _action):
                    response = fetch()
                    raw = response.body()
                    require(len(raw) <= MAX_BODY, 'Studio lifecycle response exceeds bound')
                    return {'status': 'ok' if 200 <= response.status < 300 else 'fail',
                            '_rawResponse': (response.status, raw)}
                self._refusal_log.dispatch('tool_call', selected, lifecycle_request)
                return

            if self._assistant_log is not None and route.request.method == 'POST' and \
                    urlsplit(url).path == '/api/agent-workspace/studio/mcp':
                payload = route.request.post_data_json
                if not isinstance(payload, dict):
                    raise OriginLocked('refused malformed assistant MCP request')
                if payload.get('method') == 'tools/call':
                    params = payload.get('params')
                    if not isinstance(params, dict) or not isinstance(params.get('name'), str) \
                            or not isinstance(params.get('arguments'), dict):
                        raise OriginLocked('refused malformed assistant tool call')
                    if self._assistant_tool_calls >= 60:
                        self._assistant_bound_hit = True
                        raise OriginLocked('assistant tool call bound exceeded')
                    self._assistant_tool_calls += 1
                    selected = {'kind': 'tool_call', 'tool': params['name'], 'arguments': params['arguments']}
                    self._assistant_log.names.add(params['name'])

                    def recorded(_kind, _action):
                        response = fetch()
                        raw = response.body()
                        require(len(raw) <= MAX_BODY, 'assistant tool response exceeds bound')
                        try:
                            content_type = response.headers.get('content-type', '').lower()
                            if 'text/event-stream' in content_type:
                                frames = raw.replace(b'\r\n', b'\n').split(b'\n\n')
                                messages = [json.loads(data) for frame in frames[:-1]
                                    if (data := b'\n'.join(line[5:].lstrip() for line in frame.split(b'\n')
                                                           if line.startswith(b'data:')))]
                                messages = [item for item in messages if isinstance(item, dict)
                                            and item.get('id') == payload.get('id') and 'id' in item]
                                if len(messages) != 1:
                                    raise ValueError('missing or duplicate MCP response')
                                message = messages[0]
                            else:
                                message = json.loads(raw)
                            error = message.get('error') if isinstance(message, dict) else None
                            result = message.get('result') if isinstance(message, dict) else None
                            failed = (not isinstance(message, dict) or 'error' in message or not isinstance(result, dict)
                                      or result.get('isError') is True)
                            code = error.get('code') if isinstance(error, dict) else None
                            code = code if type(code) is int else 'mcp-tool-error'
                        except (ValueError, TypeError):
                            failed, code = True, 'invalid-mcp-response'
                        out = {'status': 'ok' if 200 <= response.status < 300 and not failed else 'fail',
                               '_rawResponse': (response.status, raw)}
                        if out['status'] == 'fail':
                            out['errorCode'] = code if 200 <= response.status < 300 else response.status
                        return out

                    self._assistant_log.dispatch('tool_call', selected, recorded)
                    return
            fetch()
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

    def chat_state(self):
        """Read the visible Studio chat without evaluating page code."""
        self._check_origin()
        chat = self._page.locator('[data-agent-workspace]')
        if chat.count() != 1:
            raise BrowserUnavailable('Studio chat is unavailable or ambiguous')
        rows = chat.locator('[data-workspace-messages] > p')
        messages = [rows.nth(i).inner_text(timeout=5000) for i in range(rows.count())]
        answers = [line[len('assistant:'):].strip() for line in messages if line.lower().startswith('assistant:')]
        entry = chat.locator('[data-workspace-prompt]')
        send = chat.locator('form button[type="submit"]')
        working = chat.locator('[data-workspace-working]')
        panel_open = chat.get_attribute('open') is not None
        connected = chat.get_attribute('data-connected') == 'true'
        streaming = working.count() == 1 and working.is_visible()
        result = {'assistantMessages': len(answers), 'toolCalls': self._assistant_tool_calls,
            'streaming': streaming, 'panelOpen': panel_open, 'connected': connected,
            'mounted': chat.get_attribute('data-mounted') == 'true',
            'ready': (connected and entry.count() == 1 and send.count() == 1 and entry.is_visible()
                      and entry.is_enabled() and send.is_enabled()),
            'status': 'complete' if answers and not streaming else None,
            'text': answers[-1] if answers else '',
            'modelCalls': self._assistant_model_calls, 'dispatchedToolCalls': self._assistant_tool_calls,
            'boundHit': self._assistant_bound_hit}
        self._check_origin()
        return result

    def close(self):
        try:
            self._browser.close()
        finally:
            self._pw.stop()


class StudioBrowserServer:
    """Loopback MCP server. Tools are the allowlist above; evaluate and devtools are absent."""

    def __init__(self, studio_url, action_log, fixture_dir, *, page_factory=None, sensitive=(), identity=None,
                 scripted_customer=False, stage_gate=None):
        self.studio_url = studio_url
        self.action_log = action_log
        self.fixture_dir = fixture_dir
        self.page_factory = page_factory
        self.identity = identity
        self.scripted_customer = scripted_customer
        self.stage_gate = stage_gate
        if scripted_customer:
            self.action_log.names.update(('studio_chat_observe', 'studio_customer_type'))
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
        import ai_arc_evidence_images
        # The evidence sink open when this server starts (one Studio stage); a closed sink rejects late writes.
        self._evidence = ai_arc_evidence_images.current()
        self._shot_seq = 0

    def start(self):
        from agent_e2e_local_api import McpEndpoint
        self.lock = OriginLock(self.studio_url)
        self._owner = _DaemonOwner()
        try:
            factory = self.page_factory or (lambda lock: PlaywrightPage(
                lock, identity=self.identity, assistant_log=self.action_log if self.scripted_customer else None,
                stage_gate=self.stage_gate, refusal_log=self.action_log))
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
        if self.stage_gate is not None and self.stage_gate.closed:
            raise BrowserUnavailable('Studio stage has ended')
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

        shot = {}

        def execute(_kind, _action):
            status, text = 'fail', 'refused'
            try:
                internal_chat = self.scripted_customer and name == 'studio_chat_observe' and arguments == {}
                internal_type = (self.scripted_customer and name == 'studio_customer_type'
                                 and set(arguments) == {'selector', 'text'})
                if not (internal_chat or internal_type) and (not isinstance(name, str) or name not in TOOLS
                                                              or set(arguments) != TOOL_ARGS[name]):
                    raise OriginLocked('tool is not available')
                text = (self._studio_chat_observe() if internal_chat else
                        self._studio_customer_type(arguments) if internal_type else getattr(self, '_' + name)(arguments))
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
            if png:
                shot['png'] = png
                try:
                    shot['url'] = self.page.current_url() if self.page is not None else None
                except Exception:  # noqa: BLE001  (an unknown URL may be a sign-in page: never kept)
                    shot['url'] = None
            result = {'status': status, 'text': text[:8000], 'screenshotSha256': digest}
            # The action is written before execution. Its result, including the screenshot digest,
            # is retained separately and bound by the runner's response digest and action chain.
            response = {'jsonrpc': '2.0', 'id': rpc_id, 'result': _tool_result(status, text[:8000], digest)}
            return {**result, '_rawResponse': (200, action_bytes(response))}

        result = self.action_log.dispatch('tool_call', selected, execute)
        if self._evidence is not None and shot.get('png'):
            # Evidence only, kept after the action log sealed the result, and only with the sealed digest.
            # Retention never raises, so it cannot change the action's outcome.
            self._shot_seq += 1
            self._evidence.retain('step', f'{self._shot_seq:04d}-{name}', shot['png'],
                                  expected_sha256=result.get('screenshotSha256') or '', url=shot.get('url'),
                                  check_url=True)
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

    def _studio_customer_type(self, arguments):
        text = arguments['text']
        if not isinstance(text, str) or len(text) > 8192 or '\x00' in text:
            raise OriginLocked('refused customer text')
        if arguments['selector'] != '[data-workspace-prompt]':
            raise OriginLocked('refused customer input')
        self.page.fill(arguments['selector'], text)
        return 'typed'

    def _studio_press(self, arguments):
        if not isinstance(arguments['key'], str) or not KEY.fullmatch(arguments['key']):
            raise OriginLocked('refused key')
        self.page.press(arguments['key'])
        return 'pressed'

    def _studio_snapshot(self, _arguments):
        return self._screen_text(self.page.snapshot_text())

    def _screen_text(self, text):
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

    def _studio_chat_observe(self):
        state = self.page.chat_state()
        state['text'] = self._screen_text(state['text'])
        return json.dumps(state, separators=(',', ':'))

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


def open_studio_browser(stack, action_log, *, identity=None, scripted_customer=False):
    """Start the studio MCP server for one session. The runner stops it."""
    origin = stack.extra.get('consoleUrl') or stack.extra.get('studioUrl')
    require(isinstance(origin, str) and origin, 'case has no provisioned Console origin')
    fixture = stack.extra.get('studioFixtureDir') or str(Path(stack.workdir) / 'studio-fixtures')
    Path(fixture).mkdir(parents=True, exist_ok=True)
    sensitive = (stack.admin_key, stack.approver_key, os.environ.get('HONUA_CORPUS_AGENT_KEY'),
                 os.environ.get('HONUA_CORPUS_BOOTSTRAP_KEY'))
    server = StudioBrowserServer(origin, action_log, fixture, page_factory=stack.extra.get('studioPageFactory'),
                                 sensitive=sensitive, identity=identity, scripted_customer=scripted_customer,
                                 stage_gate=stack.extra.get('studioGate'))
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


def map_spec(styled, *, filtered=False):
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
                       'layout': {'text-field': ['to-string', ['get', 'name']],
                                  'text-variable-anchor': ['top', 'bottom', 'left', 'right'],
                                  'text-radial-offset': 1.5}})
    if filtered:
        require(styled, 'filtered fixture needs its authored style')
        for layer in layers:
            if layer.get('source') == 'shelters':
                layer['filter'] = ['all', ['==', ['get', 'is_open'], True], ['>', ['get', 'capacity'], 50]]
    return {'version': 8, 'sources': {name: {'type': 'geojson'} for name in LAYER_NAMES}, 'layers': layers}


def map_body(variant, extent, styled):
    return {'mapPackageId': 'synthetic-town-' + variant.lower(), 'format': 'honua_map_package.v1',
            'layers': [{'id': name, 'visible': True} for name in LAYER_NAMES],
            'view': {'bbox': list(extent)}, 'mapSpec': map_spec(styled)}


def fixture_map_body(variant, layers, source_ids, *, styled):
    """Build a real Studio package from controller-observed fixture bindings."""
    import math
    require(set(layers) == set(LAYER_NAMES) == set(source_ids), 'fixture map bindings are incomplete')
    extent = fixture_extent(variant)
    def longitude(value):
        return math.degrees(value / 6378137)
    def latitude(value):
        return math.degrees(2 * math.atan(math.exp(value / 6378137)) - math.pi / 2)
    body = map_body(variant, extent, styled)
    body['view']['bbox'] = [longitude(extent[0]), latitude(extent[1]), longitude(extent[2]), latitude(extent[3])]
    body['sourceBindings'] = []
    for row in body['layers']:
        name = row['id']
        binding, source_id = layers[name], source_ids[name]
        require(isinstance(source_id, str) and source_id and isinstance(binding, dict)
                and isinstance(binding.get('service'), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', binding['service'])
                and type(binding.get('layerId')) is int and 0 <= binding['layerId'] <= 2147483647,
                'invalid fixture source binding')
        row.update(type='circle' if name == 'shelters' else 'fill', sourceId=source_id)
        body['sourceBindings'].append({'sourceId': source_id, 'protocol': 'geoservices_feature_service',
            'locator': {'url': f"/rest/services/{binding['service']}/FeatureServer/{binding['layerId']}",
                        'layerId': binding['layerId']}})
    return body


def observed_fixture_source_ids(recorder, layers, step='p09-source-catalog'):
    """Resolve Studio catalog identities from an authenticated controller MCP read."""
    result = recorder.mcp_call(step, 'honua_list_layers', {'offset': 0, 'limit': 500})
    data = result.get('structuredContent') if isinstance(result, dict) else None
    require(isinstance(data, dict) and data.get('offset') == 0 and data.get('hasMore') is False
            and isinstance(data.get('layers'), list) and data.get('layerCount') == len(data['layers']),
            'fixture catalog is incomplete')
    resolved = {}
    for name in LAYER_NAMES:
        binding = layers[name]
        matches = [row for row in data['layers'] if isinstance(row, dict)
                   and row.get('serviceName') == binding['service'] and row.get('layerId') == binding['layerId']]
        require(len(matches) == 1 and isinstance(matches[0].get('serviceId'), str)
                and matches[0]['serviceId'], 'fixture catalog identity is ambiguous')
        resolved[name] = matches[0]['serviceId'] + '/' + str(binding['layerId'])
    return resolved


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
    return {'orderedLayerIds': ordered, 'visibility': visibility, 'extent': list(view['bbox']),
            'view': copy.deepcopy(view)}


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


def default_map_spec(body):
    """Studio's deterministic default paint for unstyled fixture composition layers."""
    layers = body.get('layers') if isinstance(body, dict) else None
    require(isinstance(layers, list) and [row.get('id') for row in layers if isinstance(row, dict)] == list(LAYER_NAMES),
            'default map composition differs from fixture')
    palette = ('#0b6b4d', '#b4531b', '#3a5fa8', '#8a2f6b', '#1f7a8c', '#94741a', '#5c3b8f', '#a03a3a')
    rules = []
    for row in layers:
        require(set(row) <= {'id', 'type', 'title', 'visible', 'sourceId', 'metadata'} and row.get('visible') is True,
                'unsupported default composition layer')
        name, kind = row['id'], row.get('type')
        require(kind == ('circle' if name == 'shelters' else 'fill'), 'unsupported default layer type')
        hashed = 0x811c9dc5
        for char in name:
            hashed = ((hashed ^ ord(char)) * 0x01000193) & 0xffffffff
        color = palette[hashed % len(palette)]
        paint = ({'fill-color': color, 'fill-opacity': 0.45, 'fill-outline-color': color} if kind == 'fill' else
                 {'circle-color': color, 'circle-radius': 4, 'circle-stroke-color': '#ffffff', 'circle-stroke-width': 1})
        rules.append({'id': name, 'type': kind, 'source': name, 'paint': paint})
    return {'version': 8, 'sources': {name: {'type': 'geojson'} for name in LAYER_NAMES}, 'layers': rules}


def normalize_catalog_map(body, source_ids, fixtures):
    """Bind an unstyled Studio map's source IDs to the controller's fixture catalog."""
    import math
    require(isinstance(body, dict) and 'mapSpec' not in body and 'sourceBindings' not in body
            and isinstance(body.get('layers'), list) and len(body['layers']) == len(LAYER_NAMES)
            and set(source_ids) == set(fixtures) == set(LAYER_NAMES)
            and len(set(source_ids.values())) == len(LAYER_NAMES), 'invalid catalog-bound map')
    reverse = {value: name for name, value in source_ids.items()}
    normalized = copy.deepcopy(body)
    original_ids = {}
    for row in normalized['layers']:
        require(isinstance(row, dict) and 'styleRef' not in row and row.get('sourceId') in reverse,
                'saved source is absent from fixture catalog or has an authored style')
        name = reverse[row['sourceId']]
        require(name not in original_ids and isinstance(row.get('id'), str) and row['id'],
                'saved source is duplicated in fixture catalog')
        original_ids[name] = row['id']
        row['id'] = name
    require(set(original_ids) == set(LAYER_NAMES), 'saved sources differ from fixture catalog')
    view = normalized.get('view')
    require(isinstance(view, dict) and view.get('crs', 'EPSG:4326') in ('EPSG:3857', 'EPSG:4326', 'CRS84')
            and isinstance(view.get('bbox'), list) and len(view['bbox']) == 4
            and all(type(v) in (int, float) and math.isfinite(v) and abs(v) <= 20037508.35 for v in view['bbox']),
            'unsupported catalog map view')
    x1, y1, x2, y2 = view['bbox']
    require(x1 < x2 and y1 < y2, 'invalid catalog map view')
    def geographic(x, y):
        return [math.degrees(x / 6378137), math.degrees(2 * math.atan(math.exp(y / 6378137)) - math.pi / 2)]
    if view.get('crs', 'EPSG:4326') == 'EPSG:3857':
        view.update(crs='EPSG:4326', bbox=geographic(x1, y1) + geographic(x2, y2))
    else:
        require(-180 <= x1 < x2 <= 180 and -85 <= y1 < y2 <= 85, 'invalid catalog geographic view')
    normalized['sourceBindings'] = [
        {'sourceId': source_ids[name], 'protocol': 'geoservices_feature_service',
         'locator': {'url': f"/rest/services/{fixtures[name]['service']}/FeatureServer/{fixtures[name]['layerId']}",
                     'layerId': fixtures[name]['layerId']}} for name in LAYER_NAMES]
    spec = default_map_spec(normalized)
    palette = ('#0b6b4d', '#b4531b', '#3a5fa8', '#8a2f6b', '#1f7a8c', '#94741a', '#5c3b8f', '#a03a3a')
    colors = {}
    for row in spec['layers']:
        name = row['id']
        hashed = 0x811c9dc5
        for char in original_ids[name]:
            hashed = ((hashed ^ ord(char)) * 0x01000193) & 0xffffffff
        color = palette[hashed % len(palette)]
        row['paint'][row['type'] + '-color'] = color
        if row['type'] == 'fill':
            row['paint']['fill-outline-color'] = color
        colors[name] = [int(color[i:i + 2], 16) for i in (1, 3, 5)] + [115 if row['type'] == 'fill' else 255]
    normalized['mapSpec'] = spec
    return normalized, colors


def catalog_composition_without_view(body, source_ids):
    """Preserve controller-observed layers when a saved map has no view."""
    require(isinstance(body, dict) and body.get('view') is None and isinstance(body.get('layers'), list)
            and len(body['layers']) == len(LAYER_NAMES) and set(source_ids) == set(LAYER_NAMES)
            and len(set(source_ids.values())) == len(LAYER_NAMES), 'invalid viewless catalog map')
    reverse = {value: name for name, value in source_ids.items()}
    ordered, visibility = [], {}
    for row in body['layers']:
        require(isinstance(row, dict) and 'styleRef' not in row and row.get('sourceId') in reverse
                and type(row.get('visible')) is bool, 'invalid viewless catalog layer')
        name = reverse[row['sourceId']]
        require(name not in visibility, 'duplicate viewless catalog layer')
        ordered.append(name)
        visibility[name] = row['visible']
    return {'orderedLayerIds': ordered, 'visibility': visibility, 'view': None}


def saved_authored_exports(body, fixtures, *, variant='A', allow_default=False):
    """Translate supported literal saved layer paint to fixture-bound server exports.

    Unsupported expressions and external source bindings fail closed. The
    caller must independently inspect the PNGs and verify labels; request creation
    by itself is never render evidence.
    """
    import math
    from urllib.parse import urlencode
    require(isinstance(body, dict) and isinstance(fixtures, dict), 'invalid authored map')
    layers, bindings, spec = body.get('layers'), body.get('sourceBindings'), body.get('mapSpec')
    if spec is None and allow_default:
        spec = default_map_spec(body)
    require(isinstance(layers, list) and isinstance(bindings, list) and isinstance(spec, dict)
            and spec.get('version') == 8 and isinstance(spec.get('layers'), list)
            and isinstance(spec.get('sources'), dict), 'authored map is incomplete')
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
                and rules[0].get('source') in spec['sources']
                and rules[0].get('type') == layer.get('type'), 'unsupported authored rule')
        rule = rules[0]
        layout, paint = rule.get('layout', {}), rule.get('paint', {})
        require(isinstance(layout, dict) and set(layout) <= {'visibility'} and layout.get('visibility', 'visible') == 'visible'
                and isinstance(paint, dict), 'invalid authored paint')
        kind = rule['type']
        require(kind in ('fill', 'circle'), 'unsupported authored symbol')
        allowed = ({'fill-color', 'fill-opacity', 'fill-outline-color'} if kind == 'fill' else
                   {'circle-color', 'circle-opacity', 'circle-radius', 'circle-stroke-color', 'circle-stroke-width'})
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
        outline_color = paint.get('fill-outline-color') if kind == 'fill' else paint.get('circle-stroke-color')
        if outline_color is not None:
            width = 1 if kind == 'fill' else paint.get('circle-stroke-width', 0)
            require(isinstance(outline_color, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', outline_color)
                    and type(width) in (int, float) and math.isfinite(width) and 0 <= width <= 10,
                    'unsupported authored outline')
            symbol['outline'] = {'type': 'esriSLS', 'style': 'esriSLSSolid',
                                 'color': [int(outline_color[i:i + 2], 16) for i in (1, 3, 5)] + [255],
                                 'width': width}
        dynamic = [{'id': layer_id, 'source': {'type': 'mapLayer', 'mapLayerId': layer_id},
                    'drawingInfo': {'renderer': {'type': 'simple', 'symbol': symbol}}}]
        if 'filter' in rule:
            document = json.loads(fixture_path(variant, name).read_text(encoding='utf-8'))
            fields = {field for feature in document['features'] for field in feature['properties']}
            dynamic[0]['definitionExpression'] = _fixture_filter_sql(rule['filter'], fields)
        query = urlencode({'f': 'image', 'layers': layer_id, 'bbox': ','.join(str(v) for v in bbox), 'bboxSR': 4326,
                           'imageSR': 3857, 'size': '512,512', 'format': 'png32', 'transparent': 'true',
                           'dynamicLayers': json.dumps(dynamic, separators=(',', ':'))})
        result.append({'name': name, 'path': f'/rest/services/{service}/MapServer/export?{query}', 'rgba': rgba})
    return result


def verify_saved_authored_layers(recorder, version_step, prefix, *, fixtures, expected_rgba, regions, minimum_pixels,
                                 variant='A', allow_default=False, render_body=None):
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
    exports = saved_authored_exports(render_body or package_body(recorder.records[version_step].json), fixtures,
                                     variant=variant, allow_default=allow_default)
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


def fixture_geojson_source(record, variant, name):
    """Validate controller GeoJSON against the frozen fixture before rendering it."""
    import math
    document = record.json
    require(record.status == 200 and isinstance(document, dict) and document.get('type') == 'FeatureCollection'
            and isinstance(document.get('features'), list), 'invalid controller GeoJSON')
    fixture = json.loads(fixture_path(variant, name).read_text(encoding='utf-8'))
    expected = {f['properties']['source_id']: f for f in fixture['features']}
    observed = {}
    for feature in document['features']:
        props = feature.get('properties') if isinstance(feature, dict) else None
        require(isinstance(props, dict) and isinstance(props.get('source_id'), str)
                and props['source_id'] not in observed, 'invalid controller GeoJSON identity')
        observed[props['source_id']] = feature
    require(set(observed) == set(expected), 'controller GeoJSON fixture inventory differs')

    def coordinate_match(actual, wanted, depth=0):
        require(depth <= 8 and isinstance(actual, list) and isinstance(wanted, list)
                and len(actual) == len(wanted), 'controller GeoJSON geometry differs')
        if len(wanted) == 2 and all(type(v) in (int, float) for v in wanted):
            require(all(type(v) in (int, float) and math.isfinite(v) for v in actual), 'invalid GeoJSON coordinate')
            x, y = wanted
            target = [math.degrees(x / 6378137), math.degrees(2 * math.atan(math.exp(y / 6378137)) - math.pi / 2)]
            require(all(abs(a - b) <= 1e-7 for a, b in zip(actual, target)), 'controller GeoJSON geometry differs')
            return
        for a, b in zip(actual, wanted):
            coordinate_match(a, b, depth + 1)

    clean = []
    for source_id in sorted(expected):
        actual, wanted = observed[source_id], expected[source_id]
        props, expected_props = actual['properties'], wanted['properties']
        require(all(key in props and type(props[key]) is type(value) and props[key] == value
                    for key, value in expected_props.items()), 'controller GeoJSON fixture attributes differ')
        actual_geometry, wanted_geometry = actual.get('geometry'), wanted['geometry']
        require(isinstance(actual_geometry, dict) and actual_geometry.get('type') == wanted_geometry['type'],
                'controller GeoJSON geometry differs')
        coordinate_match(actual_geometry.get('coordinates'), wanted_geometry['coordinates'])
        clean.append({'type': 'Feature', 'id': source_id, 'properties': {key: props[key] for key in expected_props},
                      'geometry': actual_geometry})
    return {'srid': 4326, 'features': clean}


def independent_maplibre_render(body, sources, console_origin, *, sample_mercator=None, allow_default=False):
    """Render controller-read features and a saved style in an isolated runner browser.

    The page is runner-owned static code, not a Console route or an agent screenshot.
    It reports MapLibre's rendered symbol features and network errors; all network
    requests are confined to its loopback server. Unsupported styles fail closed.
    """
    import copy
    import math
    from urllib.error import HTTPError
    from playwright.sync_api import sync_playwright

    require(isinstance(body, dict) and isinstance(sources, dict) and set(sources) == set(LAYER_NAMES),
            'independent render needs the three fixture sources')
    spec = copy.deepcopy(body.get('mapSpec'))
    if spec is None and allow_default:
        spec = default_map_spec(body)
    require(isinstance(spec, dict) and spec.get('version') == 8 and isinstance(spec.get('sources'), dict)
            and set(spec['sources']) == set(sources) and isinstance(spec.get('layers'), list)
            and len(spec['layers']) <= 32 and set(spec) <= {'version', 'sources', 'layers', 'name', 'metadata'},
            'unsupported independent render style')
    require(len(json.dumps(spec)) <= 65536, 'independent render style is too large')
    layers = spec['layers']
    require(all(isinstance(row, dict) and row.get('type') in ('fill', 'circle', 'symbol')
                and row.get('source') in sources and isinstance(row.get('id'), str) for row in layers),
            'unsupported independent render layer')
    symbols = [row for row in layers if row['type'] == 'symbol']
    require(len(symbols) <= 1 and all(row.get('source') == 'shelters' for row in symbols),
            'unsupported independent label layer')
    composition = body.get('layers')
    require(isinstance(composition, list) and all(isinstance(row, dict) for row in composition)
            and [row.get('id') for row in composition] == [row['id'] for row in layers if row['type'] != 'symbol'],
            'independent render layers differ from the saved composition')
    if symbols:
        style_from_body(body)  # validates the actual field, ownership and visible text paint
        label = symbols[0]
        layout, paint = label.get('layout', {}), label.get('paint', {})
        require(layers[-1] is label and isinstance(layout, dict)
                and set(layout) <= {'text-field', 'text-variable-anchor', 'text-radial-offset', 'text-font',
                                    'text-size', 'visibility', 'text-anchor', 'text-offset'}
                and isinstance(paint, dict) and set(paint) <= {'text-color', 'text-opacity'}
                and paint.get('text-color', '#000000') in ('#000000', '#000', 'black')
                and paint.get('text-opacity', 1) == 1
                and type(layout.get('text-size', 16)) in (int, float) and 8 <= layout.get('text-size', 16) <= 40,
                'unsupported independent label paint')
    names = [row['id'] for row in layers]
    require(len(set(names)) == len(names), 'duplicate independent render layer')
    view = body.get('view')
    require(isinstance(view, dict) and set(view) <= {'bbox', 'crs'}
            and view.get('crs', 'EPSG:4326') in ('EPSG:4326', 'CRS84'), 'unsupported independent map view')
    bounds = view.get('bbox')
    require(isinstance(bounds, list) and len(bounds) == 4 and all(type(v) in (int, float) and math.isfinite(v) for v in bounds)
            and -180 <= bounds[0] < bounds[2] <= 180 and -85 <= bounds[1] < bounds[3] <= 85,
            'independent render needs a bounded geographic view')
    if sample_mercator is not None:
        require(isinstance(sample_mercator, list) and len(sample_mercator) == 4
                and all(type(v) in (int, float) and math.isfinite(v) for v in sample_mercator)
                and sample_mercator[0] < sample_mercator[2] and sample_mercator[1] < sample_mercator[3],
                'invalid frozen sample region')

    def convert(node, *, geographic=False, depth=0):
        require(depth <= 8, 'invalid fixture coordinates')
        if isinstance(node, list) and len(node) == 2 and all(type(v) in (int, float) and math.isfinite(v) for v in node):
            x, y = node
            if geographic:
                require(-180 <= x <= 180 and -85 <= y <= 85, 'fixture coordinate is outside geographic bounds')
                return [x, y]
            require(abs(x) <= 20037508.35 and abs(y) <= 20037508.35, 'fixture coordinate is outside Web Mercator')
            return [math.degrees(x / 6378137), math.degrees(2 * math.atan(math.exp(y / 6378137)) - math.pi / 2)]
        require(isinstance(node, list) and len(node) <= 10000, 'invalid fixture coordinates')
        return [convert(part, geographic=geographic, depth=depth + 1) for part in node]

    for name, source in sources.items():
        require(isinstance(source, dict) and source.get('srid') in (3857, 4326) and isinstance(source.get('features'), list)
                and 0 < len(source['features']) <= 1000, 'invalid independent render source')
        features = copy.deepcopy(source['features'])
        for feature in features:
            require(isinstance(feature, dict) and isinstance(feature.get('geometry'), dict), 'invalid render feature')
            feature['geometry']['coordinates'] = convert(feature['geometry']['coordinates'], geographic=source['srid'] == 4326)
        spec['sources'][name] = {'type': 'geojson', 'data': {'type': 'FeatureCollection', 'features': features}}
    require(len(json.dumps(spec, ensure_ascii=False)) <= 2 * 1024 * 1024, 'independent render sources are too large')
    spec['glyphs'] = '/glyphs/{fontstack}/{range}.pbf'
    sample = None if sample_mercator is None else [convert(sample_mercator[:2]), convert(sample_mercator[2:])]

    parsed = urlsplit(console_origin)
    require(parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost') and parsed.port
            and parsed.path in ('', '/') and not parsed.query and not parsed.fragment and not parsed.username,
            'independent renderer requires the case Console loopback origin')

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    assets = {}
    for local, (path, digest) in RENDER_ASSETS.items():
        try:
            with opener.open(console_origin.rstrip('/') + '/_studio/' + path, timeout=10) as response:
                require(response.status == 200, 'independent renderer asset is unavailable')
                data = response.read(2 * 1024 * 1024 + 1)
        except (OSError, HTTPError) as exc:
            raise ProjectionError('independent renderer asset is unavailable') from exc
        require(len(data) <= 2 * 1024 * 1024 and hashlib.sha256(data).hexdigest() == digest,
                'independent renderer asset digest differs')
        assets['/' + local] = (data, 'text/javascript')
    for name, digest in GLYPH_HASHES.items():
        data = (GLYPH_DIR / name).read_bytes()
        require(hashlib.sha256(data).hexdigest() == digest, 'independent renderer glyph digest differs')
        assets['/glyphs/Open%20Sans%20Regular/' + name] = (data, 'application/x-protobuf')
        assets['/glyphs/Open%20Sans%20Regular,Arial%20Unicode%20MS%20Regular/' + name] = (data, 'application/x-protobuf')
        assets['/glyphs/Open%20Sans%20Regular%2CArial%20Unicode%20MS%20Regular/' + name] = (data, 'application/x-protobuf')

    html = b'''<!doctype html><meta charset="utf-8"><style>html,body,#map{margin:0;width:512px;height:512px}</style><div id="map"></div><script type="module">
import {Map,setWorkerUrl} from '/maplibre.js';
setWorkerUrl('/worker.js');
const d=await (await fetch('/document.json')).json();
const errors=[];
const map=new Map({container:'map',style:d.style,interactive:false,attributionControl:false,fadeDuration:0,preserveDrawingBuffer:true});
map.on('error',e=>errors.push(String(e.error)));
map.fitBounds(d.bbox,{duration:0});
map.once('idle',async()=>{
  const ids=d.symbols.length ? map.queryRenderedFeatures(undefined,{layers:d.symbols}).map(f=>f.properties.source_id) : [];
  const loaded=d.sources.filter(name=>map.isSourceLoaded(name));
  let sampleRect=null;
  if(d.sample){
    const a=map.project([d.sample[0][0],d.sample[1][1]]), b=map.project([d.sample[1][0],d.sample[0][1]]);
    const x=Math.ceil(a.x), y=Math.ceil(a.y), right=Math.floor(b.x), bottom=Math.floor(b.y);
    sampleRect=[x,y,right-x,bottom-y];
  }
  await fetch('/result',{method:'POST',body:JSON.stringify({ids,loaded,errors,sampleRect})});
});
</script>'''
    result = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            if self.path == '/':
                data, mime = html, 'text/html'
            elif self.path == '/document.json':
                data = json.dumps({'style': spec, 'bbox': bounds, 'sample': sample, 'symbols': [row['id'] for row in symbols],
                                   'sources': list(sources)}, separators=(',', ':')).encode()
                mime = 'application/json'
            elif self.path in assets:
                data, mime = assets[self.path]
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_POST(self):
            length = int(self.headers.get('Content-Length', '0'))
            if self.path != '/result' or not 0 < length <= 8192 or result:
                self.send_error(400)
                return
            try:
                value = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                self.send_error(400)
                return
            result.append(value)
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    blocked = []
    try:
        use_worktree_browsers()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(**launch_options())
            try:
                context = browser.new_context(**context_options())
                def route(request):
                    target = urlsplit(request.request.url)
                    if target.scheme == 'http' and target.hostname == '127.0.0.1' and target.port == server.server_port:
                        request.continue_()
                    else:
                        blocked.append('external request')
                        request.abort()
                context.route('**/*', route)
                page = context.new_page()
                page.on('pageerror', lambda _error: blocked.append('page error'))
                page.goto(origin + '/', wait_until='domcontentloaded', timeout=15000)
                deadline = time.monotonic() + 30
                while not result and time.monotonic() < deadline:
                    page.wait_for_timeout(100)
                require(len(result) == 1 and not blocked, 'independent map renderer did not complete safely')
                output = result[0]
                require(isinstance(output, dict) and set(output) == {'ids', 'loaded', 'errors', 'sampleRect'}
                        and isinstance(output['ids'], list) and all(isinstance(v, str) for v in output['ids'])
                        and isinstance(output['loaded'], list) and set(output['loaded']) == set(sources)
                        and output['errors'] == [], 'independent map renderer reported an error')
                known_ids = {feature.get('properties', {}).get('source_id') for feature in sources['shelters']['features']}
                require(set(output['ids']) <= known_ids, 'independent renderer returned an unknown label')
                rect = output['sampleRect']
                if sample is not None:
                    require(isinstance(rect, list) and len(rect) == 4 and all(type(v) is int for v in rect)
                            and 0 <= rect[0] < 512 and 0 <= rect[1] < 512 and rect[2] > 0 and rect[3] > 0
                            and rect[0] + rect[2] <= 512 and rect[1] + rect[3] <= 512,
                            'frozen sample is outside the saved map view')
                else:
                    require(rect is None, 'unexpected independent render sample')
                png = page.screenshot(timeout=5000)
                return {'png': png, 'visibleLabelSourceIds': sorted(set(output['ids'])),
                        'loadedLayerIds': sorted(output['loaded']), 'sampleRect': rect}
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def record_independent_render(stack, seeded, prefix, version_step, *, render_body=None, default_colors=None):
    """Score controller exports and a separate runner-owned composite from saved state."""
    import ai_arc_evidence_images
    evidence = ai_arc_evidence_images.current()       # captured when this observation starts
    recorder, variant = stack.extra['recorder'], seeded.state['variant']
    fixtures = seeded.state.get('layers') or {}
    require(set(fixtures) == set(LAYER_NAMES), 'render fixture bindings are incomplete')
    contract = json.loads(COMPOSITE_CONTRACT_PATH.read_text(encoding='utf-8'))
    require(variant in ('A', 'B') and isinstance(contract, dict), 'invalid frozen composite contract')
    default = prefix == 'p08' and 'mapSpec' not in package_body(recorder.records[version_step].json)
    colors = (default_colors or contract['defaultRgba']) if default else contract['authoredRgba']
    verify_saved_authored_layers(recorder, version_step, prefix + '-export', fixtures=fixtures,
        expected_rgba=colors, regions={name: [0, 0, 512, 512] for name in LAYER_NAMES},
        minimum_pixels=contract['minimumLayerPixels'], variant=variant, allow_default=default, render_body=render_body)
    sources = {name: fixture_geojson_source(recorder.records[f'{prefix}-src-{slug}-after-geo'], variant, name)
               for name, slug in SOURCE_LAYERS}
    body = render_body or package_body(recorder.records[version_step].json)
    try:
        from playwright.sync_api import Error as PlaywrightError
    except ImportError as exc:
        raise ProjectionError('independent render requires Playwright') from exc
    try:
        rendering = independent_maplibre_render(body, sources, stack.extra.get('consoleUrl') or stack.extra.get('studioUrl'),
            sample_mercator=contract['sampleMercator'][variant], allow_default=default)
    except PlaywrightError as exc:
        raise ProjectionError('independent browser render failed') from exc
    color = ([round(255 * (1 - 0.45) + channel * 0.45) for channel in colors['flood_zones'][:3]] + [255]
             if default_colors else contract['defaultFloodRgba'] if default else contract['floodRgba'])
    measurement = measure_png(rendering['png'], {'background': contract['background'],
        'backgroundTolerance': contract['backgroundTolerance'], 'regions': [
            {'id': 'flood', 'rect': rendering['sampleRect'], 'rgba': color, 'tolerance': contract['floodTolerance']}]},
        http_status=200, loaded_layer_ids=rendering['loadedLayerIds'],
        visible_label_source_ids=rendering['visibleLabelSourceIds'], browser_errors=[])
    recorder.store_derived(prefix + '-render', measurement)
    if evidence is not None:
        evidence.retain('render', prefix + '-independent', rendering['png'])


def verify_saved_default_fill(recorder, version_step, export_step, *, service, layer_id, expected_rgba, minimum_pixels):
    """Check a recorded saved version against controller-fetched server pixels.

    This narrow proof is not the P08/P09/P24 render projection: it accepts only
    a single default fill, and does not claim to observe labels or authored styles.
    """
    import base64
    import ai_arc_evidence_images
    evidence = ai_arc_evidence_images.current()       # captured when this observation starts
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
        if evidence is not None:
            evidence.retain('render', export_step + '-server-export', raw)
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


def record_map_measurements(recorder, version_step, prefix, *, composition_body=None):
    body = package_body(recorder.records[version_step].json)
    recorder.store_derived(prefix + '-composition', composition_from_body(composition_body or body))
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
        geo_path = path.replace('f=json', 'f=geojson').replace('outSR=3857', 'outSR=4326')
        geo = recorder.json(f'{prefix}-src-{slug}-{when}-geo', 'GET', geo_path)['observation']
        geo_body = geo.get('body')
        require(geo.get('httpStatus') == 200 and isinstance(geo_body, dict)
                and geo_body.get('type') == 'FeatureCollection' and isinstance(geo_body.get('features'), list),
                'published fixture GeoJSON cannot be read')


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


def seed_unstyled_map(recorder, variant, layers, owner):
    source_ids = observed_fixture_source_ids(recorder, layers)
    starting = fixture_map_body(variant, layers, source_ids, styled=False)
    expected = fixture_map_body(variant, layers, source_ids, styled=True)
    recorder.store_derived('p09-expected-saved', {'body': expected})
    envelope = {'family': 'map', 'schemaVersion': '1', 'format': 'honua_map_package.v1',
                'body': starting}
    created = recorder.json('p09-seed-draft', 'POST', '/api/v1/studio/package-drafts', {
        'packageKey': 'synthetic-town-' + variant.lower(), 'ownerId': owner, 'envelope': envelope})
    require(created['observation']['httpStatus'] in (200, 201), 'unstyled map draft was not created')
    data = created['observation']['body']
    data = data.get('data', data) if isinstance(data, dict) else {}
    draft_id = data.get('draftId') if isinstance(data, dict) else None
    require(isinstance(draft_id, str) and IDENT.fullmatch(draft_id), 'unstyled map draft has no id')
    saved = recorder.json('p09-seed-save', 'POST', f'/api/v1/studio/package-drafts/{draft_id}/content-versions', {})
    require(saved['observation']['httpStatus'] in (200, 201), 'unstyled map was not saved')


def case_namespace(prefix, workdir):
    """Keep case fixtures distinct even when drivers share a Compose project."""
    return prefix + '_' + hashlib.sha256(str(workdir).encode()).hexdigest()[:16]


def stage3_layers(recorder, handoff, variant):
    """Re-read each stage-three publication handle and audit with controller credentials."""
    from datetime import datetime, timezone
    from urllib.parse import quote
    require(isinstance(handoff, dict) and handoff.get('variant') == variant
            and handoff.get('fixtureId') == 'town-' + variant
            and isinstance(handoff.get('principal'), str) and handoff['principal']
            and isinstance(handoff.get('layers'), dict) and set(handoff['layers']) == set(LAYER_NAMES),
            'stage-three service handoff is unavailable')
    key = handoff.get('agentKeyId')
    require(isinstance(key, str) and re.fullmatch(r'[A-Za-z0-9-]{1,80}', key)
            and handoff['principal'] in (key, 'api-key:' + key, 'apikey:api-key:' + key),
            'stage-three principal is not the runner-bound agent key')
    def instant(value):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
            require(value.tzinfo is not None, 'stage-three timestamp lacks timezone')
            return value.astimezone(timezone.utc)
        except (TypeError, ValueError, AttributeError) as exc:
            raise ProjectionError('invalid stage-three timestamp') from exc
    start, end = instant(handoff.get('startedAt')), instant(handoff.get('endedAt'))
    require(start <= end, 'stage-three time window is invalid')
    events, cursor = {}, None
    for page in range(20):
        path = '/api/v1/admin/observability/audit?pageSize=200'
        if cursor is not None:
            path += '&cursor=' + quote(cursor, safe='')
        answer = recorder.json(f'stage3-audit-{page}', 'GET', path)['observation']
        body = answer.get('body')
        require(answer.get('httpStatus') == 200 and isinstance(body, dict) and isinstance(body.get('items'), list),
                'stage-three audit is unavailable')
        for event in body['items']:
            require(isinstance(event, dict) and type(event.get('auditId')) is int and event['auditId'] not in events,
                    'stage-three audit is malformed')
            events[event['auditId']] = event
        cursor = body.get('nextCursor')
        if cursor is None:
            break
        require(isinstance(cursor, str) and 0 < len(cursor) <= 2048, 'invalid audit cursor')
    else:
        raise ProjectionError('stage-three audit is incomplete')
    layers = {}
    for name, slug in SOURCE_LAYERS:
        item = handoff['layers'][name]
        require(isinstance(item, dict) and isinstance(item.get('service'), str)
                and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', item['service'])
                and type(item.get('layerId')) is int and item['layerId'] >= 0
                and isinstance(item.get('operationInstanceId'), str)
                and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', item['operationInstanceId'])
                and type(item.get('auditId')) is int, 'invalid stage-three layer binding')
        event = events.get(item['auditId'])
        require(isinstance(event, dict) and event.get('resourceId') == item['operationInstanceId']
                and event.get('resourceType') == 'operation_instance' and event.get('action') == 'operation.completed'
                and event.get('actor') == handoff['principal'] and event.get('outcome') == 'Success'
                and event.get('details', '').split(';')[:2] == ['operationId=service.publish', 'status=Completed']
                and start <= instant(event.get('timestamp')) <= end, 'stage-three publish audit does not match')
        answer = recorder.json('publish-' + slug + '-submit', 'GET',
                               '/api/v1/operations/handles/' + item['operationInstanceId'])['observation']
        data = answer.get('body', {}).get('data', {})
        resources = data.get('resourceIds', {}) if isinstance(data, dict) else {}
        require(answer.get('httpStatus') == 200 and data.get('operationInstanceId') == item['operationInstanceId']
                and data.get('operationId') == 'service.publish' and data.get('status') == 'Completed'
                and resources.get('serviceName') == item['service']
                and str(resources.get('layerId')) == str(item['layerId']), 'stage-three publish handle differs')
        layers[name] = {'service': item['service'], 'layerId': item['layerId']}
    require(len({(v['service'], v['layerId']) for v in layers.values()}) == len(LAYER_NAMES),
            'stage-three layer identities collide')
    return layers


def seed_case(scenario_id, stack, variant, *, create_map, external_handoff=None):
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
    if external_handoff is not None:
        require(scenario_id == 'P08' and not create_map, 'handoff is only supported for new arc maps')
        layers = stage3_layers(recorder, external_handoff, variant)
        stack.extra['arcServicesHandoff'] = copy.deepcopy(external_handoff)
    elif scenario_id == 'P24':
        layers = dict(stack.extra.get('publishedLayers') or {})
    else:
        from agent_e2e_local_scenarios.w2g3_common import town_setup
        if 'connectionId' not in stack.extra:
            stack.extra['connectionId'] = stack.extra['ensureConnection']()
        # Multiple Studio drivers share the arc's Compose project but need
        # distinct imported tables and service names.
        namespace = case_namespace(prefix, stack.workdir)
        town = town_setup(recorder, variant).prepare(
            variant=variant, namespace=namespace, starting_state='published-town',
            connection_id=stack.extra['connectionId'])
        layers = {name: {'service': town['layers'][name]['service'], 'layerId': town['layers'][name]['layerId']}
                  for name in LAYER_NAMES}
    if layers:
        snapshot_sources(recorder, prefix, layers, 'before', variant=variant)
    if external_handoff is not None:
        stack.extra['arcLineageVerified'] = True
    if create_map:
        seed_unstyled_map(recorder, variant, layers, stack.extra.get('agentPrincipal'))
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
        key = stack.extra.get('agentPrincipal')
        owners = {key, stack.extra.get('consoleUserId'), 'api-key:' + str(key)}
        matches = [row for row in rows if row.get('itemId') not in before and row.get('ownerId') in owners]
        require(len(matches) == 1, 'map list does not identify one new map')
        row = matches[0]
    else:
        matches = [row for row in rows if row.get('itemId') == seeded.state.get('mapId')]
        require(len(matches) == 1, 'seeded map is not in the live list')
        row = matches[0]
    recorder.json(prefix + '-version', 'GET', version_path(row.get('itemId'), row.get('currentVersionId')))
    render_body = default_colors = None
    saved_body = package_body(recorder.records[prefix + '-version'].json)
    if prefix == 'p08' and 'mapSpec' not in saved_body and 'sourceBindings' not in saved_body:
        catalog = observed_fixture_source_ids(recorder, layers, step='p08-source-catalog')
        if saved_body.get('view') is None:
            recorder.store_derived(prefix + '-composition', catalog_composition_without_view(saved_body, catalog))
            recorder.store_derived(prefix + '-saved', {'documentSha256': saved_digest(saved_body)})
            stack.extra[prefix + 'RenderFailure'] = 'saved map has no view'
            return
        render_body, default_colors = normalize_catalog_map(saved_body, catalog, layers)
    record_map_measurements(recorder, prefix + '-version', prefix, composition_body=render_body)
    if prefix == 'p24' and not layers:
        from agent_e2e_local_projections import audited_map_sources
        from types import SimpleNamespace
        recorder.mcp_call('p24-catalog', 'honua_list_layers', {'offset': 0, 'limit': 500})
        recorder.json('p24-audit', 'GET', '/api/v1/admin/observability/audit?action=operation.completed&resourceType=operation_instance&pageSize=200')
        receipts = []
        for index, action in enumerate(recorder.actions()):
            if action.kind != 'tool_call' or action.tool != 'honua_publish_service' or action.status != 'pass':
                continue
            try:
                result = recorder.action_result(f'p24-publish-result-{index}', action)
            except ProjectionError:
                continue
            receipts.append({'tool': action.tool, 'status': action.status, 'json': result.json})
        try:
            layers = audited_map_sources(recorder.records, receipts, stack.extra.get('custody'),
                                          stack.extra.get('agentPrincipal'), row.get('itemId'))
            snapshot_sources(recorder, prefix, layers, 'after', variant=seeded.state['variant'])
            seeded = SimpleNamespace(state={**seeded.state, 'layers': layers})
        except ProjectionError as exc:
            stack.extra[prefix + 'SourceFailure'] = str(exc)[:200]
    if set(layers) == set(LAYER_NAMES):
        try:
            record_independent_render(stack, seeded, prefix, prefix + '-version', render_body=render_body,
                                      default_colors=default_colors)
        except (ProjectionError, OSError, ValueError) as exc:
            # Keep other assertions observable. The missing render step fails closed.
            stack.extra[prefix + 'RenderFailure'] = str(exc)[:200]


def composition_spec(prefix, role, variant):
    return {'object': {
        'mapId': {'target': prefix + '-version', 'role': role, 'at': {'pathSegment': 4}},
        'orderedLayerIds': {'body': prefix + '-composition', 'pointer': '/orderedLayerIds'},
        'visibility': {'body': prefix + '-composition', 'pointer': '/visibility'},
        'extent': {'studioViewContainsFixture': {'body': prefix + '-composition', 'pointer': '/view'},
                   'fixtureBounds3857': fixture_extent(variant)}}}


def style_spec(prefix, role):
    return {'object': {
        'mapId': {'target': prefix + '-version', 'role': role, 'at': {'pathSegment': 4}},
        'styleDocumentSha256': {'body': prefix + '-style', 'pointer': '/styleDocumentSha256'},
        'labelField': {'body': prefix + '-style', 'pointer': '/labelField'},
        'floodColor': {'body': prefix + '-style', 'pointer': '/floodColor'},
        'floodOpacity': {'body': prefix + '-style', 'pointer': '/floodOpacity'}}}


def render_spec(prefix):
    fields = ('httpStatus', 'decodedWidth', 'decodedHeight', 'loadedLayerIds', 'nonBackgroundPixels',
              'browserErrors', 'regions', 'visibleLabelSourceIds')
    steps = [prefix + '-version']
    if prefix == 'p24':
        steps.extend(('p24-catalog', 'p24-audit'))
    for _name, slug in SOURCE_LAYERS:
        steps.extend((f'{prefix}-src-{slug}-after-raw', f'{prefix}-src-{slug}-after-geo',
                      f'{prefix}-export-{slug}'))
    return {'object': {**{field: {'body': prefix + '-render', 'pointer': '/' + field} for field in fields},
                       'sourceEvidence': {'list': [{'sha256': step} for step in steps]}}}


def saved_spec(prefix, resource_role, version_role):
    return {'object': {
        'resourceId': {'target': prefix + '-version', 'role': resource_role, 'at': {'pathSegment': 4}},
        'versionId': {'target': prefix + '-version', 'role': version_role, 'at': {'pathSegment': 6}},
        'documentSha256': {'body': prefix + '-saved', 'pointer': '/documentSha256'}}}


def source_content_spec(prefix):
    return {'protectedContent': [
        {'label': name, 'before': f'{prefix}-src-{slug}-before-raw', 'after': f'{prefix}-src-{slug}-after-raw',
         'resource': [
             {'role': f'{"audited" if prefix == "p24" else "seeded"}:service:{name}', 'at': {'pathSegment': 2}},
             {'role': f'{"audited" if prefix == "p24" else "seeded"}:layer:{name}', 'at': {'pathSegment': 4}}],
         'content': ({'featureServerFeatures': '$step', 'booleanFields': ['is_open'], 'booleanGeo': {
             f'{prefix}-src-{slug}-before-raw': f'{prefix}-src-{slug}-before-geo',
             f'{prefix}-src-{slug}-after-raw': f'{prefix}-src-{slug}-after-geo'}}
             if name == 'shelters' else {'featureServerFeatures': '$step'})}
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
