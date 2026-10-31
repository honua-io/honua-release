"""Origin-locked Map Studio browser for agent evals.

The agent gets a loopback MCP server whose tools can drive one headless Chromium
locked to the case stack's Studio origin. Every tool call is written to the runner
action log, including the sha256 of the controller screenshot, before the call
returns. Agent screenshots are not oracle evidence.

The runner observes saved maps through its own recorder: API documents, a derived
style and composition measurement, and a second headless browser that loads the
saved map and decodes the canvas. Those measurements are stored with store_derived.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from agent_e2e_local_recorder import ProjectionError

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / 'certification/agent-e2e/corpus'
CONTRACT_PATH = ROOT / 'certification/agent-e2e/expectations/local/studio-render-contract.json'
LIST_PATH = '/api/v1/studio/content-items?family=map&limit=100'
LAYER_NAMES = ('parcels', 'flood_zones', 'shelters')
SOURCE_LAYERS = (('parcels', 'parcels'), ('flood_zones', 'flood-zones'), ('shelters', 'shelters'))
STYLE_CONFIG = {'floodLayerId': 'flood_zones', 'shelterLayerId': 'shelters'}
FLOOD_COLOR = '#0088ff'
FLOOD_OPACITY = 0.35
RENDER_MINIMUM_NON_BACKGROUND = 1000
RENDER_MINIMUM_MATCHING = 4000
MAX_BODY = 512 * 1024
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
        except OriginLocked:
            return False
        return True

    def route_allows(self, url):
        # The browser's empty document is not an agent navigation target.
        return url == 'about:blank' or self.allows(url)


def launch_options():
    """Headless, with devtools left off. Playwright 1.64 has no devtools launch switch."""
    return {'headless': True, 'args': ['--disable-dev-shm-usage']}


def context_options():
    return {'viewport': {'width': 512, 'height': 512}, 'device_scale_factor': 1,
            'accept_downloads': False, 'java_script_enabled': True}


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

    def __init__(self, origin):
        use_worktree_browsers()
        from playwright.sync_api import sync_playwright
        self.origin = origin
        self.aborted = []
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(**launch_options())
        self._context = self._browser.new_context(**context_options())
        self._context.route('**/*', self._gate)
        self._context.on('download', self._cancel_download)
        self._page = self._context.new_page()

    def _gate(self, route):
        url = route.request.url
        if not self.origin.route_allows(url):
            self.aborted.append(url)
            route.abort('blockedbyclient')
            return
        route.continue_()

    @staticmethod
    def _cancel_download(download):
        try:
            download.cancel()
        except Exception:
            pass

    def goto(self, url):
        response = self._page.goto(url, wait_until='load', timeout=30000)
        return 200 if response is None else response.status

    def current_url(self):
        return self._page.url

    def click(self, selector):
        self._page.click(selector, timeout=5000)

    def fill(self, selector, text):
        self._page.fill(selector, text, timeout=5000)

    def press(self, key):
        self._page.keyboard.press(key)

    def set_input_files(self, selector, path):
        self._page.set_input_files(selector, path, timeout=5000)

    def screenshot(self):
        canvas = self._page.locator('#map')
        if canvas.count():
            return canvas.screenshot(type='png')
        return self._page.screenshot(type='png')

    def snapshot_text(self):
        body = self._page.locator('body')
        try:
            text = body.aria_snapshot()
        except Exception:
            text = body.inner_text(timeout=5000)
        return (text or '')[:8000]

    def attribute(self, name):
        node = self._page.locator('#honua-render')
        if not node.count():
            return ''
        return node.get_attribute(name) or ''

    def close(self):
        try:
            self._browser.close()
        finally:
            self._pw.stop()


class StudioBrowserServer:
    """Loopback MCP server. Tools are the allowlist above; evaluate and devtools are absent."""

    def __init__(self, studio_url, action_log, fixture_dir, *, page_factory=None):
        self.studio_url = studio_url
        self.action_log = action_log
        self.fixture_dir = fixture_dir
        self.page_factory = page_factory
        self.token = secrets.token_hex(24)
        self.page = None
        self.endpoint = None
        self.server = None
        self.decoy = []
        self.lock = None

    def start(self):
        from agent_e2e_local_api import McpEndpoint
        self.lock = OriginLock(self.studio_url)
        self.page = self.page_factory(self.lock) if self.page_factory else PlaywrightPage(self.lock)
        try:
            self._serve()
        except Exception:
            self.page.close()
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
                length = int(self.headers.get('Content-Length') or 0)
                if length > MAX_BODY:
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
                    status, text, digest = server.perform(params.get('name'), params.get('arguments'))
                    result = {'content': [{'type': 'text', 'text': text}], 'isError': status != 'ok',
                              'structuredContent': {'screenshotSha256': digest}}
                else:
                    return self.send(200, json.dumps(_rpc_error(message, -32601, 'method not available')).encode())
                self.send(200, json.dumps({'jsonrpc': '2.0', 'id': message.get('id'), 'result': result}).encode())

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
        if self.page is not None:
            self.page.close()
            self.page = None

    def perform(self, name, arguments):
        if not isinstance(arguments, dict):
            arguments = {}
        status, text = 'fail', 'refused'
        try:
            if name not in TOOLS or set(arguments) != TOOL_ARGS[name]:
                raise OriginLocked('tool is not available')
            text = getattr(self, '_' + name)(arguments)
            status = 'ok'
        except OriginLocked as exc:
            text = str(exc)
        png = b''
        if self.page is not None:
            try:
                png = self.page.screenshot() or b''
            except Exception:
                png = b''
        digest = hashlib.sha256(png).hexdigest() if png else None
        selected = {'kind': 'tool_call', 'tool': name if isinstance(name, str) else 'refused',
                    'arguments': arguments, 'screenshotSha256': digest}
        from agent_e2e_local_recorder import action_bytes
        try:
            action_bytes(selected)
        except (TypeError, ValueError):
            selected['arguments'] = {'refused': 'unserializable'}

        def execute(_kind, _action):
            return {'status': 'ok' if status == 'ok' else 'fail', 'screenshotSha256': digest}
        self.action_log.dispatch('tool_call', selected, execute)
        # Not evidence. Tests corrupt this to prove the sealed action log is the recorder's.
        self.decoy.append('side-log-is-not-evidence')
        return status, text[:8000], digest

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
        return self.page.snapshot_text()

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


def open_studio_browser(stack, action_log):
    """Start the studio MCP server for one session. The runner stops it."""
    origin = stack.extra.get('studioUrl') or stack.base_url
    fixture = stack.extra.get('studioFixtureDir') or str(Path(stack.workdir) / 'studio-fixtures')
    Path(fixture).mkdir(parents=True, exist_ok=True)
    server = StudioBrowserServer(origin, action_log, fixture, page_factory=stack.extra.get('studioPageFactory'))
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


def _feature(feature):
    require(isinstance(feature, dict) and feature.get('type') == 'Feature', 'source feature is malformed')
    properties, geometry = feature.get('properties'), feature.get('geometry')
    require(isinstance(properties, dict) and isinstance(properties.get('source_id'), str) and properties['source_id'],
            'source feature has no source id')
    require(feature.get('id') == properties['source_id'], 'source feature id differs from source id')
    require(isinstance(geometry, dict) and geometry.get('type') in
            ('Point', 'Polygon', 'MultiPolygon', 'LineString', 'MultiLineString'),
            'source feature geometry is unsupported')
    return {'type': 'Feature', 'id': feature['id'], 'properties': properties, 'geometry': geometry}


def _sorted_features(features):
    require(isinstance(features, list), 'source features are missing')
    rows = [_feature(feature) for feature in features]
    require(len({row['id'] for row in rows}) == len(rows), 'source features repeat a source id')
    rows.sort(key=lambda row: row['properties']['source_id'])
    return rows


def normalize_source(body):
    """GeoJSON FeatureCollection, or an Esri feature set, to {srid, features} sorted by source_id.

    Polygon and polyline Esri responses drop only the object-id attribute. A live query whose
    attributes differ from the fixture still fails the protected-content comparison.
    """
    require(isinstance(body, dict), 'source response is not an object')
    if body.get('type') == 'FeatureCollection':
        crs = body.get('crs')
        name = crs.get('properties', {}).get('name') if isinstance(crs, dict) and isinstance(crs.get('properties'), dict) else None
        require(name == 'EPSG:3857', 'source coordinate reference is not EPSG:3857')
        return {'srid': 3857, 'features': _sorted_features(body.get('features'))}
    geometry = body.get('geometryType')
    if geometry == 'esriGeometryPoint':
        from agent_e2e_query_projection import feature_server_points
        value = feature_server_points(body)
        value['features'].sort(key=lambda feature: feature['properties']['source_id'])
        return value
    require(geometry in ('esriGeometryPolygon', 'esriGeometryPolyline'), 'unsupported source geometry')
    reference = body.get('spatialReference')
    require(isinstance(reference, dict) and reference.get('wkid') == 3857, 'source coordinate reference is not EPSG:3857')
    require(body.get('exceededTransferLimit') is False, 'source read is incomplete')
    oid = body.get('objectIdFieldName')
    rows = []
    for feature in body.get('features') or []:
        attributes = dict(feature.get('attributes') or {})
        source_id = attributes.get('source_id')
        require(isinstance(source_id, str) and source_id, 'source feature has no source id')
        if isinstance(oid, str) and oid in attributes:
            del attributes[oid]
        shape = feature.get('geometry') or {}
        if geometry == 'esriGeometryPolygon':
            coordinates = shape.get('rings')
            kind = 'Polygon'
        else:
            coordinates = shape.get('paths')
            kind = 'LineString' if isinstance(coordinates, list) and len(coordinates) == 1 else 'MultiLineString'
            if kind == 'LineString':
                coordinates = coordinates[0]
        require(isinstance(coordinates, list) and coordinates, 'source feature geometry is empty')
        rows.append({'type': 'Feature', 'id': source_id, 'properties': {**attributes, 'source_id': source_id},
                     'geometry': {'type': kind, 'coordinates': coordinates}})
    return {'srid': 3857, 'features': _sorted_features(rows)}


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


def saved_digest(body):
    from agent_e2e_pack import canonical_digest
    return canonical_digest(body)[7:]


def record_source_snapshot(recorder, raw_step, derived_step):
    record = recorder.records[raw_step]
    require(record.status == 200 and isinstance(record.json, dict), 'source read failed')
    recorder.store_derived(derived_step, normalize_source(record.json))


def record_map_measurements(recorder, version_step, prefix):
    body = package_body(recorder.records[version_step].json)
    recorder.store_derived(prefix + '-composition', composition_from_body(body))
    recorder.store_derived(prefix + '-style', style_from_body(body))
    recorder.store_derived(prefix + '-saved', {'documentSha256': saved_digest(body)})


def record_render(recorder, step, studio_url, map_path, contract, page=None):
    lock = OriginLock(studio_url)
    url = lock.resolve(map_path)
    own = None
    try:
        target = page if page is not None else PlaywrightPage(lock)
        own = None if page is not None else target
        status = target.goto(url)
        measured = measure_png(target.screenshot(), contract, http_status=status if type(status) is int else 0,
                               loaded_layer_ids=split_ids(target.attribute('data-loaded-layers')),
                               visible_label_source_ids=split_ids(target.attribute('data-label-sources')),
                               browser_errors=split_ids(target.attribute('data-browser-errors')))
        recorder.store_derived(step, measured)
        return measured
    finally:
        if own is not None:
            own.close()


def snapshot_sources(recorder, prefix, layers, when):
    from agent_e2e_p10_observer import source_path
    for name, slug in SOURCE_LAYERS:
        raw, derived = f'{prefix}-src-{slug}-{when}-raw', f'{prefix}-src-{slug}-{when}'
        recorder.json(raw, 'GET', source_path(layers[name]))
        record_source_snapshot(recorder, raw, derived)


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
    stack.extra.setdefault('studioUrl', stack.base_url)
    stack.extra['studioFixtureDir'] = str(CORPUS / f'town-{variant}')
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
        snapshot_sources(recorder, prefix, layers, 'before')
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
        snapshot_sources(recorder, prefix, layers, 'after')
    listing = recorder.json(prefix + '-maps', 'GET', LIST_PATH)
    rows = items_of(listing['observation']['body'])
    if created:
        before = {row.get('itemId') for row in items_of(recorder.records[prefix + '-maps-before'].json)}
        agent = stack.extra.get('agentPrincipal')
        matches = [row for row in rows if row.get('itemId') not in before and row.get('ownerId') == agent]
        require(len(matches) == 1, 'map list does not identify one new map')
        row = matches[0]
    else:
        matches = [row for row in rows if row.get('itemId') == seeded.state.get('mapId')]
        require(len(matches) == 1, 'seeded map is not in the live list')
        row = matches[0]
    recorder.json(prefix + '-version', 'GET', version_path(row.get('itemId'), row.get('currentVersionId')))
    record_map_measurements(recorder, prefix + '-version', prefix)
    origin = stack.extra.get('studioUrl') or stack.base_url
    record_render(recorder, prefix + '-render', origin, '/maps/' + row['itemId'], render_contract(),
                  page=stack.extra.get('renderPage'))


def composition_spec(prefix, role):
    return {'object': {
        'mapId': {'target': prefix + '-version', 'role': role},
        'orderedLayerIds': {'body': prefix + '-composition', 'pointer': '/orderedLayerIds'},
        'visibility': {'body': prefix + '-composition', 'pointer': '/visibility'},
        'extent': {'body': prefix + '-composition', 'pointer': '/extent'}}}


def style_spec(prefix, role):
    return {'object': {
        'mapId': {'target': prefix + '-version', 'role': role},
        'styleDocumentSha256': {'body': prefix + '-style', 'pointer': '/styleDocumentSha256'},
        'labelField': {'body': prefix + '-style', 'pointer': '/labelField'},
        'floodColor': {'body': prefix + '-style', 'pointer': '/floodColor'},
        'floodOpacity': {'body': prefix + '-style', 'pointer': '/floodOpacity'}}}


def render_spec(prefix):
    return {'body': prefix + '-render', 'pointer': ''}


def saved_spec(prefix, resource_role, version_role):
    return {'object': {
        'resourceId': {'target': prefix + '-version', 'role': resource_role},
        'versionId': {'target': prefix + '-version', 'role': version_role},
        'documentSha256': {'body': prefix + '-saved', 'pointer': '/documentSha256'}}}


def source_content_spec(prefix):
    return {'protectedContent': [
        {'label': name, 'before': f'{prefix}-src-{slug}-before', 'after': f'{prefix}-src-{slug}-after',
         'content': {'body': '$step', 'pointer': ''}}
        for name, slug in SOURCE_LAYERS]}


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


def render_expected(scenario, variant):
    return {'layerIds': ['flood_zones', 'parcels', 'shelters'],
            'minimumNonBackgroundPixels': RENDER_MINIMUM_NON_BACKGROUND,
            'regions': [{'id': 'flood', 'minimumMatchingPixels': RENDER_MINIMUM_MATCHING}],
            'labelSourceIds': label_ids(scenario, variant)}


def source_content_expected(variant):
    resources = []
    for name, _slug in SOURCE_LAYERS:
        document = json.loads(fixture_path(variant, name).read_text(encoding='utf-8'))
        resources.append({'resourceId': name, 'content': normalize_source(document)})
    return {'profile': 'honua.protected-content/v1', 'resources': resources}


def _role(name):
    return {'$role': name}


def expectation_document(scenario_id):
    """Fixture-derived expectation document. Variant C is not present."""
    variants = {}
    for variant in ('A', 'B'):
        extent = fixture_extent(variant)
        styled = map_body(variant, extent, True)
        if scenario_id == 'P08':
            variants[variant] = {
                'map.composition': {'mapId': _role('created:map'),
                                    'orderedLayerIds': list(LAYER_NAMES),
                                    'visibility': {name: True for name in LAYER_NAMES},
                                    'extent': extent},
                'render.actual': render_expected('P08', variant),
                'source-content.unchanged': source_content_expected(variant)}
        elif scenario_id == 'P09':
            variants[variant] = {
                'map.style': {'mapId': _role('seeded:map'), **style_from_body(styled)},
                'render.actual': render_expected('P09', variant),
                'state.saved': {'resourceId': _role('seeded:map'), 'versionId': _role('created:version'),
                                'documentSha256': saved_digest(styled)},
                'source-content.unchanged': source_content_expected(variant)}
        elif scenario_id == 'P24':
            variants[variant] = {
                'candidate.identity': {'componentBindingDigest': '', 'serverSourceRevision': '',
                                       'serverOciDigest': '', 'clientArtifactDigests': []},
                'deployment.ready': {'deploymentId': '', 'healthStatus': 0, 'authenticatedReadStatus': 0},
                'features.exact': {'$fixture': {'assertion': 'features.exact', 'layer': 'shelters'}},
                'filter.exact': {'$fixture': {'assertion': 'filter.exact'}},
                'buffer.metric': {'$fixture': {'assertion': 'buffer.metric'}},
                'map.style': {'mapId': _role('created:map'), **style_from_body(styled)},
                'state.reopened': {'resourceId': _role('created:map'), 'versionId': _role('created:version'),
                                   'documentSha256': saved_digest(styled), 'freshSessionId': ''},
                'approval.separate': {'proposalId': '', 'requesterPrincipalId': '', 'approverPrincipalId': '',
                                      'approvedVersionId': '', 'auditId': ''},
                'publication.public': {'publishedUrl': '', 'anonymousHttpStatus': 0,
                                       'resourceId': _role('created:map'), 'publishedVersionId': _role('created:version')},
                'render.actual': render_expected('P24', variant),
                'custody.bootstrap-revoked': {'bootstrapPrincipalId': '', 'revokedCredentialIds': [],
                                              'postRevocationProbeStatus': 0},
                'custody.worker-replaced': {'bootstrapWorkerId': '', 'taskWorkerId': '',
                                            'bootstrapWorkerTerminalState': '', 'taskPrincipalId': '',
                                            'taskProvisioningCredentialIds': []},
                'source-content.unchanged': source_content_expected(variant)}
        else:
            raise ValueError('unknown studio scenario')
    derivation = {
        'P08': 'Layer order is parcels, flood_zones, shelters from bottom to top. Extent is the exact union of those EPSG:3857 fixture geometries, with no padding. Render minimums follow the controller render contract. Source content is the three fixture layers sorted by source_id. The map id is the created:map role.',
        'P09': 'Style hash, flood color #0088ff, opacity 0.35 and shelter label field name come from the fixture style document with no geometries inside it. The saved document hash is that map body plus the variant extent. Render labels are every shelter source id. The map id is the seeded map and the version id is the created version.',
        'P24': 'Style, render and source content use the same fixture derivation as the studio scenarios. features.exact, filter.exact and buffer.metric are fixture references. Candidate identity, deployment, approval, publication, reopen session and worker custody are not observed on local docker, so those expectations are empty and their projections fail closed. A fresh deployment has no seed-phase source bytes unless the controller already published the layers.',
    }
    return {'schemaVersion': 1, 'scenario': scenario_id, 'derivation': derivation[scenario_id], 'variants': variants}
