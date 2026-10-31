"""Origin lock, recorder action log, and studio oracle checks for P08, P09 and P24.

The headless browser test starts Chromium only when this worktree has it. Every other
test uses a fake page or recorded bytes, so the manifest gate can collect this file
without that browser.
"""
from __future__ import annotations

import hashlib
import base64
import dataclasses
import json
import os
import struct
import zlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from agent_browser_studio import (
    FLOOD_COLOR, FLOOD_OPACITY, LAYER_NAMES, LIST_PATH, STYLE_CONFIG, TOOLS, OriginLock, OriginLocked,
    PlaywrightPage, StudioBrowserServer, client_mcp_config, context_options, fixture_extent, fixture_path,
    label_ids, launch_options,
    fixture_map_body, map_body, map_spec, measure_png, open_studio_browser, render_contract, resolve_fixture,
)
from agent_e2e_local_api import McpEndpoint, SessionContext, Stack
from agent_e2e_local_projections import resolve_roles
from agent_e2e_local_recorder import ControllerRecorder, EvidenceVault, ProjectionError, read_logged_action
from agent_e2e_local_runner import (
    ActionLog, load_scenario, oracle, score_assertions, select_case,
    frozen_projections, frozen_roles,
)
from agent_e2e_local_scenarios.P08 import P08
from agent_e2e_local_scenarios.P09 import P09
from agent_e2e_local_scenarios.P24 import P24
from agent_e2e_pack import canonical_digest
from agent_e2e_studio_style_observer import project

ROOT = Path(__file__).resolve().parents[1]
PACK = Path(os.environ.get('HONUA_AGENT_E2E_DEV_PACK', ROOT / 'certification/agent-e2e/pack.v2.AB-only.json'))
AGENT = 'agent-key-1'
MAP = 'map-item-1'
VER_SEED = 'version-seed-1'
VER_LIVE = 'version-live-2'
DRAFT = 'draft-1'
LAYERS = {
    'parcels': {'service': 'svc-parcels', 'layerId': 0},
    'flood_zones': {'service': 'svc-flood', 'layerId': 1},
    'shelters': {'service': 'svc-shelters', 'layerId': 2},
}
EXTENTS = {'A': [-20010, -5010, -18800, -4700], 'B': [12990, -8010, 14200, -7700]}
DIGEST = 'sha256:' + 'ab' * 32


def _chunk(tag, data):
    return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xFFFFFFFF)


def _png(paint):
    width = height = 512
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw.extend(paint(x, y))
    header = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + _chunk(b'IHDR', header) + _chunk(b'IDAT', zlib.compress(bytes(raw), 9)) + _chunk(b'IEND', b'')


def _paint(color):
    def paint(x, y):
        if 40 <= x < 120 and 40 <= y < 100:
            return color
        return (255, 255, 255, 255)
    return paint


BLUE_PNG = _png(_paint((166, 213, 255, 255)))
RED_PNG = _png(_paint((255, 0, 0, 255)))


def _walk_extent(variant):
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
    return [min(xs), min(ys), max(xs), max(ys)]


class FakePage:
    def __init__(self, png=b'png-bytes'):
        self.png = png
        self.calls = []
        self.closed = False
        self.url = 'about:blank'
        self.aborted = []

    def goto(self, url):
        self.calls.append(('goto', url))
        self.url = url
        return 200

    def current_url(self):
        return self.url

    def click(self, selector):
        self.calls.append(('click', selector))

    def fill(self, selector, text):
        self.calls.append(('fill', selector, text))

    def press(self, key):
        self.calls.append(('press', key))

    def set_input_files(self, selector, path):
        self.calls.append(('files', selector, path))

    def screenshot(self):
        self.calls.append('screenshot')
        return self.png

    def snapshot_text(self):
        return 'studio'

    def evaluate(self, *args, **kwargs):
        self.calls.append('evaluate')
        raise AssertionError('evaluate is not a studio tool')

    def close(self):
        self.closed = True


def _dumps(value):
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


def _version_document(variant, flood, *, filtered=False, bound=False):
    source_ids = {name: f'svc-{name}/{binding["layerId"]}' for name, binding in LAYERS.items()}
    body = fixture_map_body(variant, LAYERS, source_ids, styled=True) if bound else map_body(variant, EXTENTS[variant], True)
    if not bound:
        body['view'] = fixture_map_body(variant, LAYERS, source_ids, styled=True)['view']
    if filtered:
        body['mapSpec'] = map_spec(True, filtered=True)
    if flood is not None:
        for layer in body['mapSpec']['layers']:
            if layer['id'] == 'flood_zones':
                layer['paint'] = {'fill-color': flood[0], 'fill-opacity': flood[1]}
    return {'success': True, 'data': {'envelope': {'body': body}}}


def _item(version):
    return {'itemId': MAP, 'currentVersionId': version, 'ownerId': AGENT, 'family': 'map', 'state': 'saved'}


def _factory(state):
    def factory(recorder, principal, method, path, body, headers):
        if path.startswith('/rest/services/'):
            for name, binding in LAYERS.items():
                if f"/rest/services/{binding['service']}/" in path:
                    if 'f=geojson' in path:
                        document = json.loads(fixture_path(state['variant'], name).read_bytes())
                        return 200, _dumps(document)
                    return 200, _dumps(_query_document(state['variant'], name))
            return 404, b'{}'
        if path == LIST_PATH:
            if recorder.phase == 'seed' and state['scenario'] == 'P09':
                rows = [_item(VER_SEED)]
            elif recorder.phase == 'seed':
                rows = []
            else:
                rows = [_item(VER_LIVE)]
            return 200, _dumps({'success': True, 'data': {'items': rows, 'total': len(rows)}})
        if method == 'POST' and path == '/api/v1/studio/package-drafts':
            return 201, _dumps({'success': True, 'data': {'draftId': DRAFT}})
        if method == 'POST' and path.startswith('/api/v1/admin/services/'):
            binding = LAYERS[path.rsplit('/', 1)[1]]
            return 201, _dumps({'data': {'resourceIds': {
                'serviceName': binding['service'], 'layerId': binding['layerId']}}})
        if method == 'POST' and path.endswith('/content-versions'):
            return 201, _dumps({'success': True, 'data': {}})
        if '/versions/' in path and method == 'GET':
            return 200, _dumps(_version_document(state['variant'], state['flood'], filtered=state['scenario'] == 'P24',
                                                 bound=state['scenario'] == 'P09'))
        return 404, b'{}'
    return factory


def _query_document(variant, name):
    document = json.loads(fixture_path(variant, name).read_bytes())
    kind = document['features'][0]['geometry']['type']
    features = []
    for index, feature in enumerate(document['features']):
        coords = feature['geometry']['coordinates']
        geometry = {'x': coords[0], 'y': coords[1]} if kind == 'Point' else {'rings': _esri_rings(coords)}
        features.append({'attributes': {'id': index, **feature['properties']}, 'geometry': geometry})
    return {'geometryType': 'esriGeometryPoint' if kind == 'Point' else 'esriGeometryPolygon',
            'spatialReference': {'wkid': 3857}, 'exceededTransferLimit': False,
            'objectIdFieldName': 'id', 'fields': [{'name': key} for key in features[0]['attributes']],
            'features': features}


def _esri_rings(rings):
    result = []
    for index, ring in enumerate(rings):
        area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:]))
        result.append(list(reversed(ring)) if (index == 0 and area > 0) or (index > 0 and area < 0) else ring)
    return result


def _seed_publications(recorder):
    for name, slug in (('parcels', 'parcels'), ('flood_zones', 'flood-zones'), ('shelters', 'shelters')):
        recorder.json(f'publish-{slug}-submit', 'POST', '/api/v1/admin/services/' + name, {})


def _recorder(tmp_path, state, *, vault=None):
    return ControllerRecorder(
        base_url='http://127.0.0.1:8137', workdir=str(tmp_path), candidate_digest=DIGEST,
        attempt_id='attempt-1', principals={'controller': (None, 'controller')}, http_factory=_factory(state), vault=vault)


def _run(tmp_path, monkeypatch, scenario_id, variant, *, png, flood=None, published=False):
    if not published:
        class Town:
            def __init__(self, http, manifest):
                self.recorder = http

            def prepare(self, **kwargs):
                _seed_publications(self.recorder)
                return {'layers': {name: dict(binding) for name, binding in LAYERS.items()}}

        import agent_e2e_scenario_backend
        monkeypatch.setattr(agent_e2e_scenario_backend, 'TownSetup', Town)
    import agent_browser_studio
    monkeypatch.setattr(agent_browser_studio, 'observed_fixture_source_ids', lambda _rec, layers:
                        {name: f'svc-{name}/{binding["layerId"]}' for name, binding in layers.items()})
    state = {'scenario': scenario_id, 'variant': variant, 'flood': flood}
    recorder = _recorder(tmp_path, state)
    extra = {
        'recorder': recorder, 'ensureConnection': lambda: 'conn-1', 'agentPrincipal': AGENT,
        'validatedConsoleCustomer': AGENT, 'consoleUserId': AGENT,
        'studioUrl': 'http://127.0.0.1:8137',
    }
    if published:
        _seed_publications(recorder)
        extra['publishedLayers'] = {name: dict(binding) for name, binding in LAYERS.items()}
    stack = Stack('http://127.0.0.1:8137', 'controller-key', None, 'g41studio', 'compose.yml',
                  str(tmp_path), DIGEST, extra)
    scenario = load_scenario(scenario_id)
    seeded = scenario.seed(stack, variant)
    recorder.set_phase('observe')
    observations = scenario.observe(stack, seeded, variant)
    custody = {'agentUserId': AGENT, 'agentKeyId': AGENT, 'agentKeyOwnerUserId': AGENT}
    roles = resolve_roles(frozen_roles(scenario, stack, seeded, variant, None), recorder.records,
                          agent_principal=AGENT, custody=custody)
    expected = scenario.expected(variant)
    assertions = list(select_case(PACK, scenario_id, variant)['assertions'])
    results = score_assertions(assertions, observations, expected, records=recorder.records,
                               roles=roles, custody={}, run_facts={}, scenario_id=scenario_id,
                               frozen=frozen_projections(scenario, variant))
    return SimpleNamespace(scenario=scenario, seeded=seeded, recorder=recorder, observations=observations,
                           results={row['assertion']: row for row in results}, stack=stack, roles=roles)


def _status(outcome, assertion):
    return outcome.results[assertion]['status']


def test_launch_leaves_devtools_off_and_refuses_downloads():
    options = launch_options()
    assert options['headless'] is True
    assert options.get('devtools') is not True
    assert '--auto-open-devtools-for-tabs' not in options['args']
    assert context_options()['accept_downloads'] is False


def test_origin_lock_allows_only_the_studio_origin():
    lock = OriginLock('http://127.0.0.1:8137/studio')
    assert lock.resolve('/maps/1') == 'http://127.0.0.1:8137/maps/1'
    assert lock.resolve('maps/1?x=1#y') == 'http://127.0.0.1:8137/maps/1?x=1#y'
    assert lock.allows('http://127.0.0.1:8137/maps/1')
    assert lock.route_allows('about:blank')
    assert not lock.allows('about:blank')
    for url in ('https://example.com/', 'http://127.0.0.1:9/x', 'javascript:alert(1)', 'data:text/html,hi',
                'file:///etc/passwd', 'http://user:pw@127.0.0.1:8137/x', 'http://localhost:8137/x'):
        assert not lock.allows(url)
        with pytest.raises(OriginLocked):
            lock.resolve(url)
    with pytest.raises(ValueError):
        OriginLock('https://example.com/')
    lock80 = OriginLock('http://127.0.0.1')
    assert lock80.origin == 'http://127.0.0.1:80'
    assert lock80.allows('http://127.0.0.1/a')
    assert not lock80.allows('http://127.0.0.1:8137/a')
    lock6 = OriginLock('http://[::1]:8137')
    assert lock6.resolve('/a') == 'http://[::1]:8137/a'
    assert not lock6.allows('http://127.0.0.1:8137/a')


def test_refused_navigation_stays_out_of_the_page_and_still_logs_the_screenshot(tmp_path):
    page = FakePage(BLUE_PNG)
    log = ActionLog(tmp_path / 'log', 1, set(), 20)
    server = StudioBrowserServer('http://127.0.0.1:8137', log, str(tmp_path), page_factory=lambda lock: page)
    server.lock = OriginLock('http://127.0.0.1:8137')
    server.page = page
    status, _text, digest = server.perform('studio_navigate', {'url': 'https://example.com/map'})
    assert status == 'fail'
    assert digest == hashlib.sha256(BLUE_PNG).hexdigest()
    assert not any(call[0] == 'goto' for call in page.calls if isinstance(call, tuple))
    assert log.rows[0]['status'] == 'fail'
    vault = EvidenceVault('attempt-1')
    recorder = _recorder(tmp_path / 'records', {'scenario': 'P08', 'variant': 'A', 'flood': None}, vault=vault)
    vault.append_actions(1, log.rows)
    action = recorder.actions()[0]
    logged = read_logged_action(action.path, action.digest)
    assert 'screenshotSha256' not in logged  # write-ahead action precedes the screenshot
    response = json.loads(Path(log.rows[0]['resultRawPath']).read_bytes())
    assert response['result']['structuredContent']['screenshotSha256'] == digest
    assert log.rows[0]['resultRawSha256'] == 'sha256:' + hashlib.sha256(Path(log.rows[0]['resultRawPath']).read_bytes()).hexdigest()
    assert logged['tool'] == 'studio_navigate'
    server.decoy[:] = ['this side log is not the action log']
    assert vault.records() == {}
    assert vault.action_results()[0]['json']['result']['structuredContent']['screenshotSha256'] == digest
    retained_response = Path(log.rows[0]['resultRawPath'])
    original = retained_response.read_bytes()
    retained_response.write_bytes(b'{}')
    with pytest.raises(ValueError, match='response digest mismatch'):
        vault.action_results()
    retained_response.write_bytes(original)
    Path(action.path).write_bytes(b'{"tampered":true}\n')
    with pytest.raises(ValueError):
        read_logged_action(action.path, action.digest)


def test_evaluate_is_not_a_tool_and_upload_stays_in_the_fixture_directory(tmp_path):
    page = FakePage()
    log = ActionLog(tmp_path / 'log', 1, set(), 20)
    fixture = tmp_path / 'fixtures'
    fixture.mkdir()
    (fixture / 'town.geojson').write_text('{}', encoding='utf-8')
    outside = tmp_path / 'secret.txt'
    outside.write_text('no', encoding='utf-8')
    (fixture / 'escape').symlink_to(outside)
    server = StudioBrowserServer('http://127.0.0.1:8137', log, str(fixture), page_factory=lambda lock: page)
    server.lock = OriginLock('http://127.0.0.1:8137')
    server.page = page
    from agent_browser_studio import tool_catalog
    names = [tool['name'] for tool in tool_catalog()]
    assert names == list(TOOLS)
    assert 'browser_evaluate' not in names
    status, _text, _digest = server.perform('browser_evaluate', {'expression': '1+1'})
    assert status == 'fail'
    assert 'evaluate' not in page.calls
    status, _text, _digest = server.perform('studio_upload', {'selector': '#file', 'name': '../secret.txt'})
    assert status == 'fail'
    assert not any(call[0] == 'files' for call in page.calls if isinstance(call, tuple))
    with pytest.raises(OriginLocked):
        resolve_fixture(fixture, 'escape')
    status, text, _digest = server.perform('studio_upload', {'selector': '#file', 'name': 'town.geojson'})
    assert status == 'ok' and text == 'uploaded'
    assert page.calls[-2][0] == 'files'


def test_store_derived_is_only_a_studio_measurement(tmp_path):
    recorder = _recorder(tmp_path, {'scenario': 'P08', 'variant': 'A', 'flood': None})
    with pytest.raises(ProjectionError, match='studio observations'):
        recorder.store_derived('p08-extra', {'measured': True})


def test_studio_endpoint_keeps_the_token_out_of_the_url(tmp_path):
    page = FakePage()
    stack = Stack('http://127.0.0.1:8137', 'k', None, 'g41', 'f', str(tmp_path), DIGEST, {
        'studioUrl': 'http://127.0.0.1:8137',
        'studioFixtureDir': str(tmp_path),
        'studioPageFactory': lambda lock: page,
    })
    server = open_studio_browser(stack, ActionLog(tmp_path / 'log', 1, set(), 5))
    try:
        assert server.endpoint.url.startswith('http://127.0.0.1:')
        assert server.endpoint.token not in server.endpoint.url
        honua = McpEndpoint('http://127.0.0.1:8137/mcp', 'Authorization', 'Bearer honua-token')
        config = client_mcp_config(honua, server.endpoint)
        studio = config['claude']['mcpServers']['studio']
        assert studio['headers']['Authorization'] == server.endpoint.token
        assert server.endpoint.token not in studio['url']
        assert config['codex']['mcp_servers']['studio']['http_headers']['Authorization'] == server.endpoint.token
        import urllib.error
        import urllib.request
        request = urllib.request.Request(
            server.endpoint.url, data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}).encode(),
            headers={'Authorization': server.endpoint.token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=5) as response:
            listed = json.loads(response.read())['result']['tools']
        assert [tool['name'] for tool in listed] == list(TOOLS)
        denied = urllib.request.Request(server.endpoint.url, data=b'{}', method='POST')
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(denied, timeout=5)
        assert caught.value.code == 401
    finally:
        server.stop()
    assert page.closed
    with pytest.raises(OriginLocked):
        client_mcp_config(McpEndpoint('http://127.0.0.1:1/mcp?secret', 'Authorization', 'secret'))


def test_studio_case_requires_explicit_console_origin(tmp_path):
    stack = Stack('http://127.0.0.1:8137', 'k', None, 'g41', 'f', str(tmp_path), DIGEST, {})
    with pytest.raises(ProjectionError, match='no provisioned Console origin'):
        open_studio_browser(stack, ActionLog(tmp_path / 'log', 1, set(), 5))
    with pytest.raises(ProjectionError, match='no provisioned Console origin'):
        P08().seed(stack, 'A')
    stack.extra.update(consoleUrl='http://127.0.0.1:25174', agentPrincipal=AGENT,
                       consoleUserId=AGENT, validatedConsoleCustomer='dev-operator')
    with pytest.raises(ProjectionError, match='candidate has not authenticated the runner customer'):
        P08().seed(stack, 'A')


def test_console_attachment_requires_approved_host_and_pinned_image(tmp_path, monkeypatch):
    from agent_browser_studio import CaseConsole
    stack = Stack('http://127.0.0.1:8137', 'k', None, 'g41', 'f', str(tmp_path), DIGEST, {})
    monkeypatch.setenv('DOCKER_HOST', 'ssh://other-host')
    with pytest.raises(ValueError, match='approved remote'):
        CaseConsole(stack, 'ghcr.io/honua-io/honua-console@sha256:' + 'a' * 64)
    monkeypatch.setenv('DOCKER_HOST', 'ssh://honua-eval-4')
    with pytest.raises(ValueError, match='pinned digest'):
        CaseConsole(stack, 'ghcr.io/honua-io/honua-console:latest')


def test_case_console_must_match_the_reviewed_image_and_manifest(tmp_path, monkeypatch):
    import agent_browser_studio as studio
    from agent_e2e_local_runner import LocalRuntime
    image = 'ghcr.io/honua-io/honua-console@sha256:' + 'b' * 64
    revision = 'c' * 40
    pin = {'image': image, 'sourceRevision': revision}
    pin_path = tmp_path / 'pin.json'
    pin_path.write_text(json.dumps(pin))
    monkeypatch.setattr(studio, 'CONSOLE_PIN_PATH', pin_path)
    manifest = {'components': {'honua-console': {'image': 'ghcr.io/honua-io/honua-console:nightly',
                                                  'digest': 'sha256:' + 'a' * 64, 'sha': revision}}}
    (tmp_path / 'platform-manifest.yaml').write_text(json.dumps(manifest))
    stack = Stack('http://127.0.0.1:8137', 'k', None, 'g41', 'f', str(tmp_path), DIGEST, {})
    custody = {}
    runtime = SimpleNamespace(root=tmp_path, _console_identities={'g41': {'edgeSecret': 'x', 'userId': 'user'}},
                              attest=lambda _stack, name, value, _steps: custody.update({name: value}))
    with pytest.raises(ValueError, match='manifest Console pin differs'):
        LocalRuntime.start_console(runtime, stack)
    manifest['components']['honua-console']['digest'] = 'sha256:' + 'b' * 64
    (tmp_path / 'platform-manifest.yaml').write_text(json.dumps(manifest))
    monkeypatch.setattr(studio, 'CaseConsole', lambda _stack, actual, **_kwargs:
                        SimpleNamespace(url='http://127.0.0.1:1234', image=actual))
    assert LocalRuntime.start_console(runtime, stack) == 'http://127.0.0.1:1234'
    assert stack.extra['_caseConsole'].image == image
    assert custody['studioConsoleImage'] == pin


def test_console_cleanup_keeps_its_original_remote_host(monkeypatch):
    from agent_browser_studio import CaseConsole
    calls = []
    monkeypatch.setenv('DOCKER_HOST', 'ssh://some-other-host')
    monkeypatch.setattr('agent_browser_studio.subprocess.run',
                        lambda command, **kwargs: calls.append((command, kwargs['env']['DOCKER_HOST']))
                        or SimpleNamespace(returncode=0))
    console = CaseConsole.__new__(CaseConsole)
    console._tunnel = None
    console._attempted = True
    console._created = True
    console.name = 'case-console'
    console._docker_host = 'ssh://honua-eval-4'
    console.close()
    assert calls == [(['docker', 'rm', '-f', 'case-console'], 'ssh://honua-eval-4')]


def test_p24_uploads_only_runner_admitted_customer_files(tmp_path):
    source = tmp_path / 'admitted.geojson'
    source.write_bytes(b'{"type":"FeatureCollection","features":[]}')
    state = {'scenario': 'P24', 'variant': 'A', 'flood': None}
    recorder = _recorder(tmp_path, state)
    stack = Stack('http://127.0.0.1:8137', 'k', None, 'g41', 'f', str(tmp_path), DIGEST, {
        'recorder': recorder, 'agentPrincipal': AGENT, 'validatedConsoleCustomer': AGENT,
        'consoleUserId': AGENT, 'consoleUrl': 'http://127.0.0.1:25174',
        'customerFiles': ({'name': 'parcels.3857.geojson', 'path': str(source),
                           'sha256': 'sha256:' + hashlib.sha256(source.read_bytes()).hexdigest()},),
    })
    P24().seed(stack, 'A')
    root = Path(stack.extra['studioFixtureDir'])
    assert resolve_fixture(root, 'parcels.3857.geojson').read_bytes() == source.read_bytes()
    with pytest.raises(OriginLocked):
        resolve_fixture(root, 'postgis.sql')


def test_session_context_offers_the_studio_browser_only_when_set():
    assert SessionContext.__dataclass_fields__['studio_browser'].default is None


def _rpc_call(server, name, arguments, request_id=1):
    import urllib.request
    request = urllib.request.Request(server.endpoint.url, data=_dumps({
        'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
        'params': {'name': name, 'arguments': arguments}}),
        headers={'Authorization': server.endpoint.token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=35) as response:
        return response.read()


def test_http_browser_actions_and_cleanup_use_the_creating_thread(tmp_path):
    calls = []
    log = ActionLog(tmp_path / 'log', 1, set(), 10)

    class ThreadPage(FakePage):
        def __init__(self, lock):
            super().__init__()
            self.owner = threading.get_ident()
            calls.append(('create', self.owner))

        def click(self, selector):
            assert threading.get_ident() == self.owner
            # The write-ahead record must already exist before a browser effect.
            assert len(log.rows) == 1
            assert json.loads(Path(log.rows[0]['logPath']).read_bytes())['tool'] == 'studio_click'
            calls.append(('click', threading.get_ident()))

        def screenshot(self):
            assert threading.get_ident() == self.owner
            calls.append(('screenshot', threading.get_ident()))
            return b'png'

        def close(self):
            assert threading.get_ident() == self.owner
            calls.append(('close', threading.get_ident()))

    server = StudioBrowserServer('http://127.0.0.1:8137', log, tmp_path, page_factory=ThreadPage)
    server.start()
    try:
        raw = _rpc_call(server, 'studio_click', {'selector': '#save'}, request_id=71)
        assert json.loads(raw)['result']['isError'] is False
        # The captured response is byte-for-byte what the HTTP client received.
        assert Path(log.rows[0]['resultRawPath']).read_bytes() == raw
    finally:
        server.stop()
    assert [name for name, _ in calls] == ['create', 'click', 'screenshot', 'close']
    assert len({owner for _, owner in calls}) == 1
    assert calls[0][1] != threading.get_ident()


def test_concurrent_http_calls_cannot_execute_past_the_action_bound(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    page = FakePage()
    log = ActionLog(tmp_path / 'log', 1, set(), 1)
    server = StudioBrowserServer('http://127.0.0.1:8137', log, tmp_path, page_factory=lambda lock: page)
    server.start()
    try:
        with ThreadPoolExecutor(max_workers=2) as clients:
            results = list(clients.map(lambda n: json.loads(_rpc_call(
                server, 'studio_click', {'selector': '#save'}, request_id=n)), (1, 2)))
        assert sum('result' in row for row in results) == 1
        assert sum(row.get('error', {}).get('message') == 'action bound exceeded' for row in results) == 1
        assert page.calls == [('click', '#save'), 'screenshot']
        assert len(log.rows) == 1
        assert log.rows[0]['status'] == 'pass'
    finally:
        server.stop()


def test_browser_exception_is_recorded_and_the_server_keeps_serving(tmp_path):
    class BrokenPage(FakePage):
        def click(self, selector):
            raise RuntimeError('page error containing private context')

    log = ActionLog(tmp_path / 'log', 1, set(), 4)
    server = StudioBrowserServer('http://127.0.0.1:8137', log, tmp_path, page_factory=lambda lock: BrokenPage())
    server.start()
    try:
        result = json.loads(_rpc_call(server, 'studio_click', {'selector': '#save'}))['result']
        assert result['isError'] is True
        assert 'private context' not in json.dumps(result)
        assert log.rows[0]['status'] == 'fail'
        assert json.loads(_rpc_call(server, 'studio_snapshot', {}))['result']['isError'] is False
        assert [row['status'] for row in log.rows] == ['fail', 'pass']
    finally:
        server.stop()


def test_browser_timeout_fails_closed_and_stop_is_bounded(tmp_path):
    release = threading.Event()
    class SlowPage(FakePage):
        def click(self, selector):
            release.wait(5)
    log = ActionLog(tmp_path / 'log', 1, set(), 4)
    server = StudioBrowserServer('http://127.0.0.1:8137', log, tmp_path, page_factory=lambda lock: SlowPage())
    server.owner_timeout = 0.1
    server.start()
    try:
        first = json.loads(_rpc_call(server, 'studio_click', {'selector': '#save'}))
        assert first['error']['message'] == 'browser unavailable'
        assert len(log.rows) == 1 and log.rows[0]['status'] == 'unknown'
        second = json.loads(_rpc_call(server, 'studio_click', {'selector': '#save'}))
        assert second['error']['message'] == 'browser unavailable'
        assert len(log.rows) == 1
    finally:
        release.set()
        server.stop()


def test_snapshot_refuses_runner_credential_in_page_text(tmp_path):
    class SecretPage(FakePage):
        def snapshot_text(self):
            return 'Visible content: secret-for-test-123'
    log = ActionLog(tmp_path / 'log', 1, set(), 4)
    server = StudioBrowserServer('http://127.0.0.1:8137', log, tmp_path,
                                 page_factory=lambda lock: SecretPage(), sensitive=('secret-for-test-123',))
    server.start()
    try:
        raw = _rpc_call(server, 'studio_snapshot', {})
        assert b'secret-for-test-123' not in raw
        assert json.loads(raw)['result']['isError'] is True
        assert log.rows[0]['status'] == 'fail'
    finally:
        server.stop()


def test_chromium_blocks_redirect_hops_and_cross_origin_sockets(tmp_path):
    pytest.importorskip('playwright')
    if not (ROOT / '.playwright-browsers').is_dir():
        pytest.skip('worktree chromium is not installed')
    requests = []
    class Other(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            requests.append(('other', self.path))
            self.send_response(200); self.end_headers()
    other = ThreadingHTTPServer(('127.0.0.1', 0), Other)
    other_thread = threading.Thread(target=other.serve_forever, daemon=True)
    other_thread.start()
    class Site(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            if self.path == '/popup':
                requests.append(('popup', self.path))
            if self.path == '/socket':
                requests.append(('same-socket', self.path))
                key = self.headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
                accept = base64.b64encode(hashlib.sha1(key.encode(), usedforsecurity=False).digest()).decode()
                self.send_response(101)
                self.send_header('Upgrade', 'websocket')
                self.send_header('Connection', 'Upgrade')
                self.send_header('Sec-WebSocket-Accept', accept)
                self.end_headers()
                return
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', f'http://127.0.0.1:{other.server_port}/redirect-target')
                self.end_headers()
                return
            raw = (f'<div id="map" hidden></div><p>ready</p><script>'
                   f'new WebSocket("ws://127.0.0.1:{self.server.server_port}/socket");'
                   f'new WebSocket("ws://127.0.0.1:{other.server_port}/foreign-socket");'
                   'setTimeout(() => window.open("/popup"), 200);'
                   '</script>').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    site = ThreadingHTTPServer(('127.0.0.1', 0), Site)
    site_thread = threading.Thread(target=site.serve_forever, daemon=True)
    site_thread.start()
    log = ActionLog(tmp_path / 'log', 1, set(), 5)
    server = StudioBrowserServer(f'http://127.0.0.1:{site.server_port}', log, tmp_path)
    server.start()
    try:
        import time
        popup_seen = threading.Event()
        server._owner.submit(lambda: server.page._context.on('page', lambda _: popup_seen.set())).result(timeout=2)
        started = time.monotonic()
        result = json.loads(_rpc_call(server, 'studio_navigate', {'url': '/'}))['result']
        assert result['isError'] is False
        assert time.monotonic() - started < 10
        assert json.loads(_rpc_call(server, 'studio_snapshot', {}))['result']['isError'] is False
        assert ('same-socket', '/socket') in requests
        assert not any(kind == 'other' for kind, _ in requests)
        assert popup_seen.wait(3)
        assert server._owner.submit(lambda: len(server.page._context.pages)).result(timeout=2) == 1
        redirected = json.loads(_rpc_call(server, 'studio_navigate', {'url': '/redirect'}))['result']
        assert redirected['isError'] is True
        assert not any(kind == 'other' for kind, _ in requests)
    finally:
        server.stop()
        site.shutdown(); site.server_close(); site_thread.join()
        other.shutdown(); other.server_close(); other_thread.join()


def test_unstyled_seed_is_not_already_the_finished_style():
    measured = project(map_spec(False), STYLE_CONFIG, require_label=False)
    assert measured['floodColor'] != FLOOD_COLOR or measured['floodOpacity'] != FLOOD_OPACITY
    assert measured['labelField'] is None
    assert measured['floodColor'] == '#cccccc'
    assert measured['floodOpacity'] == 1


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_expectations_match_the_pack_and_an_independent_fixture_walk(variant):
    for scenario_id, scenario in (('P08', P08()), ('P09', P09()), ('P24', P24())):
        expected = scenario.expected(variant)
        assert set(expected) == set(select_case(PACK, scenario_id, variant)['assertions'])
        render = expected['render.actual']
        assert render['minimumNonBackgroundPixels'] == 1000
        assert render['regions'][0]['minimumMatchingPixels'] == 100
        assert render['labelSourceIds'] == label_ids(scenario_id, variant)
        for row in expected['source-content.unchanged']['resources']:
            fixture = oracle.fixture_expectation(variant, 'features.exact', layer=row['resourceId'])
            for feature in fixture['features']:
                if feature['geometry']['type'] == 'Polygon':
                    feature['geometry']['coordinates'] = _esri_rings(feature['geometry']['coordinates'])
            assert 'tolerance' not in row['content']
            assert row['content']['srid'] == 3857
            assert row['content']['features'] == sorted(fixture['features'], key=lambda feature: feature['properties']['source_id'])
    walked = _walk_extent(variant)
    assert walked == EXTENTS[variant] == fixture_extent(variant)
    composition = P08().expected(variant)['map.composition']
    assert composition['extent'] is True
    assert composition['orderedLayerIds'] == ['parcels', 'flood_zones', 'shelters']


@pytest.mark.parametrize('variant', ['A', 'B'])
def test_frozen_composite_sample_is_inside_flood_and_outside_parcels(variant):
    contract = json.loads((ROOT / 'certification/agent-e2e/expectations/local/studio-composite-contract.json').read_text())
    left, bottom, right, top = contract['sampleMercator'][variant]
    def bounds(feature):
        ring = feature['geometry']['coordinates'][0]
        return min(p[0] for p in ring), min(p[1] for p in ring), max(p[0] for p in ring), max(p[1] for p in ring)
    flood = json.loads(fixture_path(variant, 'flood_zones').read_text())['features'][0]
    a, b, c, d = bounds(flood)
    assert a < left < right < c and b < bottom < top < d
    parcels = json.loads(fixture_path(variant, 'parcels').read_text())['features']
    assert all(right <= a or left >= c or top <= b or bottom >= d for a, b, c, d in map(bounds, parcels))
    walked = _walk_extent(variant)
    style = P09().expected(variant)['map.style']
    body = map_body(variant, walked, True)
    assert style['styleDocumentSha256'] == canonical_digest(body['mapSpec'])[7:]
    assert style['floodColor'] == '#0088ff'
    assert style['floodOpacity'] == 0.35
    assert style['labelField'] == 'name'
    assert P09().expected(variant)['state.saved']['documentSha256'] == {'$role': 'recorded:p09-expected'}
    assert P24().expected(variant)['map.style']['styleDocumentSha256'] == canonical_digest(map_spec(True, filtered=True))[7:]
    assert 'tolerance' in P24().expected(variant)['features.exact']
    with pytest.raises(ValueError):
        P08().expected('C')
    with pytest.raises(ValueError):
        select_case(PACK, 'P08', 'C')


def test_style_hash_is_shared_and_saved_document_uses_bound_seed():
    assert P09().expected('A')['map.style']['styleDocumentSha256'] == P09().expected('B')['map.style']['styleDocumentSha256']
    assert P09().expected('A')['state.saved']['documentSha256'] == {'$role': 'recorded:p09-expected'}
    assert P09().expected('B')['state.saved']['documentSha256'] == {'$role': 'recorded:p09-expected'}


def test_scenario_modules_do_not_carry_pack_prompts():
    root = Path(__file__).parent / 'agent_e2e_local_scenarios'
    for name in ('P08.py', 'P09.py', 'P24.py'):
        text = (root / name).read_text(encoding='utf-8')
        assert 'Kestrel' not in text and 'Willow' not in text


def test_seed_is_idempotent(monkeypatch, tmp_path):
    calls = []

    class Town:
        def __init__(self, http, manifest):
            pass

        def prepare(self, **kwargs):
            calls.append(kwargs['variant'])
            return {'layers': {name: {'service': 'svc', 'layerId': 1} for name in LAYER_NAMES}}

    import agent_e2e_scenario_backend
    monkeypatch.setattr(agent_e2e_scenario_backend, 'TownSetup', Town)

    def factory(recorder, principal, method, path, body, headers):
        if path == LIST_PATH:
            return 200, _dumps({'data': {'items': []}})
        return 200, _dumps({'type': 'FeatureCollection', 'crs': {'properties': {'name': 'EPSG:3857'}}, 'features': []})

    recorder = ControllerRecorder(
        base_url='http://127.0.0.1:8137', workdir=str(tmp_path), candidate_digest=DIGEST,
        attempt_id='attempt-1', principals={'controller': (None, 'controller')}, http_factory=factory)
    stack = Stack('http://127.0.0.1:8137', 'k', None, 'g41studio', 'f', str(tmp_path), DIGEST, {
        'recorder': recorder, 'ensureConnection': lambda: 'conn-1', 'agentPrincipal': AGENT,
        'validatedConsoleCustomer': AGENT, 'consoleUserId': AGENT,
        'consoleUrl': 'http://127.0.0.1:25174',
    })
    first = P08().seed(stack, 'A')
    assert P08().seed(stack, 'A') == first
    assert calls == ['A']
    assert stack.extra['needsStudioBrowser'] is True
    with pytest.raises(ValueError):
        P08().seed(stack, 'B')


@pytest.mark.parametrize('scenario_id', ['P08', 'P09'])
def test_fixture_style_and_source_content_pass_without_inventing_render(monkeypatch, tmp_path, scenario_id):
    outcome = _run(tmp_path, monkeypatch, scenario_id, 'A', png=BLUE_PNG)
    failed = {name: row for name, row in outcome.results.items() if row['status'] != 'pass'}
    assert set(failed) == {'render.actual'}
    assert 'unrecorded step' in failed['render.actual']['reason']
    assert all(row.observed is None for row in outcome.observations)
    blob = json.dumps([row.evidence for row in outcome.observations])
    assert 'Kestrel' not in blob and 'Willow' not in blob


def test_wrong_style_fails_the_oracle(monkeypatch, tmp_path):
    red = map_spec(True)
    for layer in red['layers']:
        if layer['id'] == 'flood_zones':
            layer['paint'] = {'fill-color': '#ff0000', 'fill-opacity': 1}
    measured = project(red, STYLE_CONFIG)
    assert measured['floodColor'] == '#ff0000'
    assert measured['floodOpacity'] == 1
    assert measured['labelField'] == 'name'
    outcome = _run(tmp_path, monkeypatch, 'P09', 'A', png=BLUE_PNG, flood=('#ff0000', 1))
    assert outcome.recorder.records['p09-style'].json['floodColor'] == '#ff0000'
    assert _status(outcome, 'map.style') == 'fail'
    assert 'value differs' in outcome.results['map.style']['reason']
    assert _status(outcome, 'source-content.unchanged') == 'pass'


def test_red_canvas_decoder_and_missing_render_fail_closed(monkeypatch, tmp_path):
    direct = measure_png(RED_PNG, render_contract(), http_status=200, loaded_layer_ids=['flood_zones', 'parcels', 'shelters'],
                         visible_label_source_ids=['s1'], browser_errors=[])
    assert direct['regions']['flood']['matchingPixels'] == 0
    assert direct['nonBackgroundPixels'] == 4800
    blue = measure_png(BLUE_PNG, render_contract(), http_status=200, loaded_layer_ids=[],
                       visible_label_source_ids=[], browser_errors=[])
    assert blue['regions']['flood']['matchingPixels'] == 4800
    outcome = _run(tmp_path, monkeypatch, 'P09', 'A', png=RED_PNG)
    assert _status(outcome, 'map.style') == 'pass'
    assert _status(outcome, 'render.actual') == 'fail'
    assert 'unrecorded step' in outcome.results['render.actual']['reason']


def test_source_read_of_another_service_fails_the_frozen_address(monkeypatch, tmp_path):
    outcome = _run(tmp_path, monkeypatch, 'P09', 'A', png=BLUE_PNG)
    records = dict(outcome.recorder.records)
    step = 'p09-src-shelters-after-raw'
    records[step] = dataclasses.replace(records[step], path=records[step].path.replace('svc-shelters', 'svc-parcels'))
    results = score_assertions(
        ['source-content.unchanged'],
        [row for row in outcome.observations if row.assertion_id == 'source-content.unchanged'],
        {'source-content.unchanged': outcome.scenario.expected('A')['source-content.unchanged']},
        records=records, roles=outcome.roles, scenario_id='P09', frozen=frozen_projections(outcome.scenario, 'A'))
    assert results[0]['status'] == 'fail'
    assert 'does not address seeded:service:shelters' in results[0]['reason']


def test_p24_scores_style_and_source_and_fails_the_rest_closed(monkeypatch, tmp_path):
    outcome = _run(tmp_path, monkeypatch, 'P24', 'A', png=BLUE_PNG, published=True)
    assert P24().needs_approver() is True
    assert P08().needs_approver() is False
    assert P09().needs_approver() is False
    assertions = list(select_case(PACK, 'P24', 'A')['assertions'])
    assert [row.assertion_id for row in outcome.observations] == assertions
    assert all(row.observed is None for row in outcome.observations)
    blob = json.dumps([row.evidence for row in outcome.observations])
    assert 'Kestrel' not in blob and 'Willow' not in blob
    for name in ('features.exact', 'map.style', 'source-content.unchanged'):
        assert _status(outcome, name) == 'pass', outcome.results[name]
    assert _status(outcome, 'render.actual') == 'fail'
    assert _status(outcome, 'custody.worker-replaced') == 'excluded'
    assert _status(outcome, 'candidate.identity') == 'fail'
    assert 'unrecorded step' in outcome.results['candidate.identity']['reason']


def test_p24_agent_claim_cannot_create_an_owned_layer(tmp_path):
    from agent_e2e_local_api import RunRecord
    recorder = _recorder(tmp_path, {'scenario': 'P24', 'variant': 'A', 'flood': None})
    stack = Stack('http://127.0.0.1:8137', 'controller-key', None, 'g41studio', 'compose.yml',
                  str(tmp_path), DIGEST, {'recorder': recorder, 'agentPrincipal': AGENT,
                  'validatedConsoleCustomer': AGENT, 'consoleUserId': AGENT,
                  'studioUrl': 'http://127.0.0.1:8137'})
    scenario = P24()
    seeded = scenario.seed(stack, 'A')
    recorder.set_phase('observe')
    run = RunRecord(AGENT, 1, (), (), ('I created svc-parcels and its layer.',))
    scenario.observe(stack, seeded, 'A', run)
    with pytest.raises(ProjectionError, match='unrecorded step'):
        resolve_roles(frozen_roles(scenario, stack, seeded, 'A', run), recorder.records,
                      agent_principal=AGENT, custody={'agentUserId': AGENT, 'agentKeyId': AGENT,
                                                      'agentKeyOwnerUserId': AGENT})
    assert 'p24-render' not in recorder.records


def test_headless_chromium_blocks_other_origins_and_reads_the_canvas(tmp_path):
    pytest.importorskip('playwright')
    browsers = ROOT / '.playwright-browsers'
    if not browsers.is_dir() or not any(browsers.iterdir()):
        pytest.skip('worktree chromium is not installed')
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(browsers)
    canvas = """<!doctype html><meta charset="utf-8"><style>
        html,body{margin:0;width:512px;height:512px;background:#fff;overflow:hidden}
        #flood{position:absolute;left:40px;top:40px;width:80px;height:60px;background:rgb(166,213,255)}
        img{position:absolute;left:0;top:0;width:1px;height:1px}
        </style><div id="flood"></div>
        <img src="https://example.com/pixel.png" alt=""><img src="http://127.0.0.1:9/pixel.png" alt="">
        <a id="escape" href="about:blank">Leave Studio</a>"""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            raw = canvas.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    origin = f'http://127.0.0.1:{httpd.server_address[1]}'
    thread = __import__('threading').Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    page = None
    server = None
    try:
        try:
            page = PlaywrightPage(OriginLock(origin))
        except Exception as exc:
            if 'playwright' in getattr(type(exc), '__module__', ''):
                pytest.skip('chromium did not launch: ' + str(exc)[:180])
            raise
        page.goto(origin + '/canvas')
        assert page.current_url().startswith(origin)
        measured = measure_png(page.screenshot(), render_contract(), http_status=200,
                               loaded_layer_ids=['flood_zones', 'parcels', 'shelters'],
                               visible_label_source_ids=[], browser_errors=[])
        assert measured['decodedWidth'] == 512 and measured['decodedHeight'] == 512
        assert measured['regions']['flood']['matchingPixels'] >= 4000
        assert any(urlsplit(url).hostname == 'example.com' for url in page.aborted)
        assert any(urlsplit(url).hostname == '127.0.0.1' and urlsplit(url).port == 9 for url in page.aborted)
        log = ActionLog(tmp_path / 'log', 1, set(), 5)
        # The agent browser must be constructed on its owning worker, independently of this
        # controller page. Exercise actual HTTP MCP requests, not a same-thread perform call.
        server = StudioBrowserServer(origin, log, str(tmp_path))
        server.start()
        raw = _rpc_call(server, 'studio_navigate', {'url': '/canvas'})
        result = json.loads(raw)['result']
        assert result['isError'] is False
        digest = result['structuredContent']['screenshotSha256']
        assert digest
        assert Path(log.rows[0]['resultRawPath']).read_bytes() == raw
        refused = json.loads(_rpc_call(server, 'studio_navigate', {'url': 'https://example.com/'}))['result']
        assert refused['isError'] is True
        assert refused['structuredContent']['screenshotSha256'] == digest
        vault = EvidenceVault('attempt-1')
        vault.append_actions(1, log.rows)
        assert vault.action_results()[0]['json']['result']['structuredContent']['screenshotSha256'] == digest
        server.decoy[:] = ['corrupt']
        assert vault.action_results()[0]['json']['result']['structuredContent']['screenshotSha256'] == digest
        escaped = json.loads(_rpc_call(server, 'studio_click', {'selector': '#escape'}))['result']
        assert escaped['isError'] is True
        assert escaped['structuredContent']['screenshotSha256'] is None
        assert log.rows[-1]['status'] == 'fail'
        # The independent controller browser still holds the original page.
        assert page.current_url() == origin + '/canvas'
    finally:
        if server is not None:
            server.stop()
        if page is not None:
            page.close()
        httpd.shutdown()
        httpd.server_close()


def test_saved_default_fill_export_binds_real_source_and_rejects_unknown_style():
    from agent_browser_studio import saved_default_fill_export
    body = {'layers': [{'id': 'explore-layer', 'sourceId': 'svc/4', 'type': 'fill', 'visible': True}],
            'sourceBindings': [{'sourceId': 'svc/4', 'protocol': 'geoservices_feature_service',
                                'locator': {'url': '/rest/services/g41proof-flat/FeatureServer/4', 'layerId': 4}}],
            'view': {'bbox': [-0.18, -0.05, -0.16, -0.04]}}
    path, rgba = saved_default_fill_export(body, service='g41proof-flat', layer_id=4)
    assert path.startswith('/rest/services/g41proof-flat/MapServer/export?')
    assert rgba == (31, 122, 140, 115)
    bad = dict(body, mapSpec={'version': 8, 'layers': []})
    with pytest.raises(ProjectionError):
        saved_default_fill_export(bad, service='g41proof-flat', layer_id=4)
    with pytest.raises(ProjectionError):
        saved_default_fill_export(body, service='other', layer_id=4)


def test_saved_default_fill_render_rejects_wrong_frozen_color():
    import base64
    from types import SimpleNamespace
    from agent_browser_studio import verify_saved_default_fill
    body = {'layers': [{'id': 'explore-layer', 'sourceId': 'svc/4', 'type': 'fill', 'visible': True}],
            'sourceBindings': [{'sourceId': 'svc/4', 'protocol': 'geoservices_feature_service',
                                'locator': {'url': '/rest/services/g41proof-flat/FeatureServer/4', 'layerId': 4}}],
            'view': {'bbox': [-0.18, -0.05, -0.16, -0.04]}}
    rgba = (31, 122, 140, 115)
    class Recorder:
        records = {'version': SimpleNamespace(json={'data': {'envelope': {'body': body}}})}
        def json(self, step, method, path):
            assert step == 'render' and method == 'GET' and '/MapServer/export?' in path
            return {'observation': {'httpStatus': 200, 'rawBodyBase64': base64.b64encode(_png(_paint(rgba))).decode()}}
    result = verify_saved_default_fill(Recorder(), 'version', 'render', service='g41proof-flat',
                                       layer_id=4, expected_rgba=rgba, minimum_pixels=4000)
    assert result['matchedPixels'] == 4800
    with pytest.raises(ProjectionError, match='frozen fill expectation'):
        verify_saved_default_fill(Recorder(), 'version', 'render', service='g41proof-flat',
                                  layer_id=4, expected_rgba=(255, 0, 0, 115), minimum_pixels=4000)


def test_fixture_source_read_rejects_embedded_server_error():
    from agent_browser_studio import snapshot_sources
    class Recorder:
        def json(self, *_args):
            return {'observation': {'httpStatus': 200, 'body': {'error': {'code': 400}}}}
    with pytest.raises(ProjectionError, match='required fields'):
        snapshot_sources(Recorder(), 'p08', LAYERS, 'before')


def test_fixture_source_queries_only_customer_fields_and_object_id():
    from urllib.parse import parse_qs, urlsplit
    from agent_browser_studio import snapshot_sources
    requests = []
    class Recorder:
        def json(self, step, method, path):
            if step.endswith('-raw'):
                requests.append((step, parse_qs(urlsplit(path).query)['outFields'][0].split(',')))
            return {'observation': {'httpStatus': 200, 'body': {'type': 'FeatureCollection', 'features': []}}}
    snapshot_sources(Recorder(), 'p08', LAYERS, 'before', variant='A')
    assert dict(requests) == {
        'p08-src-parcels-before-raw': ['id', 'name', 'owner', 'source_id', 'value'],
        'p08-src-flood-zones-before-raw': ['id', 'name', 'source_id'],
        'p08-src-shelters-before-raw': ['id', 'capacity', 'is_open', 'name', 'source_id'],
    }


def test_unstyled_map_still_records_composition_and_save():
    from agent_browser_studio import record_map_measurements
    body = {'layers': [{'id': 'parcels', 'visible': True}], 'view': {'bbox': [-1, -1, 1, 1]}}
    class Recorder:
        records = {'version': SimpleNamespace(json={'data': {'envelope': {'body': body}}})}
        def __init__(self): self.derived = {}
        def store_derived(self, name, value): self.derived[name] = value
    recorder = Recorder()
    record_map_measurements(recorder, 'version', 'p08')
    assert set(recorder.derived) == {'p08-composition', 'p08-saved'}
    assert recorder.derived['p08-composition']['orderedLayerIds'] == ['parcels']


def test_authored_exports_bind_each_style_to_its_published_layer():
    from urllib.parse import parse_qs, urlsplit
    from agent_browser_studio import saved_authored_exports
    body = map_body('A', [-20010, -5010, -18800, -4700], True)
    body['view']['bbox'] = [-0.18, -0.05, -0.16, -0.04]
    body['sourceBindings'] = []
    for row in body['layers']:
        name = row['id']
        row.update(type='circle' if name == 'shelters' else 'fill', sourceId='source-' + name)
        binding = LAYERS[name]
        body['sourceBindings'].append({'sourceId': row['sourceId'], 'protocol': 'geoservices_feature_service',
            'locator': {'url': f"/rest/services/{binding['service']}/FeatureServer/{binding['layerId']}",
                        'layerId': binding['layerId']}})
    exports = saved_authored_exports(body, LAYERS)
    assert [row['name'] for row in exports] == ['parcels', 'flood_zones', 'shelters']
    flood = parse_qs(urlsplit(exports[1]['path']).query)
    renderer = json.loads(flood['dynamicLayers'][0])[0]['drawingInfo']['renderer']
    assert renderer['symbol']['color'] == [0, 136, 255, 89]
    point = parse_qs(urlsplit(exports[2]['path']).query)
    assert json.loads(point['dynamicLayers'][0])[0]['drawingInfo']['renderer']['symbol']['type'] == 'esriSMS'
    shelter = next(row for row in body['mapSpec']['layers'] if row['id'] == 'shelters')
    shelter['filter'] = ['all', ['==', ['get', 'is_open'], True], ['>', ['get', 'capacity'], 50]]
    point = parse_qs(urlsplit(saved_authored_exports(body, LAYERS)[2]['path']).query)
    expression = json.loads(point['dynamicLayers'][0])[0]['definitionExpression']
    assert expression == '(is_open = TRUE AND capacity > 50)'
    shelter['filter'] = ['==', ['get', 'unknown_field'], True]
    with pytest.raises(ProjectionError, match='filter'):
        saved_authored_exports(body, LAYERS)
    del shelter['filter']
    body['sourceBindings'][1]['locator']['url'] = '/rest/services/other/FeatureServer/1'
    with pytest.raises(ProjectionError, match='fixture'):
        saved_authored_exports(body, LAYERS)


def test_authored_exports_require_each_frozen_color_and_nonempty_pixels():
    from agent_browser_studio import verify_saved_authored_layers
    body = map_body('A', [-20010, -5010, -18800, -4700], True)
    body['view']['bbox'] = [-0.18, -0.05, -0.16, -0.04]
    body['sourceBindings'] = []
    for row in body['layers']:
        name = row['id']
        row.update(type='circle' if name == 'shelters' else 'fill', sourceId='source-' + name)
        fixture = LAYERS[name]
        body['sourceBindings'].append({'sourceId': row['sourceId'], 'protocol': 'geoservices_feature_service',
            'locator': {'url': f"/rest/services/{fixture['service']}/FeatureServer/{fixture['layerId']}",
                        'layerId': fixture['layerId']}})
    class Recorder:
        records = {'version': SimpleNamespace(json={'data': {'envelope': {'body': body}}})}
        def json(self, step, method, path):
            from urllib.parse import parse_qs, urlsplit
            rule = json.loads(parse_qs(urlsplit(path).query)['dynamicLayers'][0])[0]
            color = tuple(rule['drawingInfo']['renderer']['symbol']['color'])
            return {'observation': {'httpStatus': 200, 'rawBodyBase64': base64.b64encode(_png(_paint(color))).decode()}}
    expected = {'parcels': [217, 211, 197, 255], 'flood_zones': [0, 136, 255, 89],
                'shelters': [34, 34, 34, 255]}
    regions = {name: [40, 40, 80, 60] for name in expected}
    result = verify_saved_authored_layers(Recorder(), 'version', 'proof', fixtures=LAYERS,
                                          expected_rgba=expected, regions=regions, minimum_pixels=4000)
    assert set(result['matchedPixels']) == set(expected)
    expected['flood_zones'] = [255, 0, 0, 89]
    with pytest.raises(ProjectionError, match='frozen'):
        verify_saved_authored_layers(Recorder(), 'version', 'negative', fixtures=LAYERS,
                                     expected_rgba=expected, regions=regions, minimum_pixels=4000)
    body['layers'].pop()
    with pytest.raises(ProjectionError, match='fixture'):
        verify_saved_authored_layers(Recorder(), 'version', 'missing', fixtures=LAYERS,
                                     expected_rgba=expected, regions=regions, minimum_pixels=4000)


def test_missing_saved_label_rule_is_not_style_evidence():
    from agent_browser_studio import style_from_body
    from agent_e2e_pack import PackError
    body = map_body('A', EXTENTS['A'], True)
    body['mapSpec']['layers'] = [row for row in body['mapSpec']['layers'] if row['type'] != 'symbol']
    with pytest.raises(PackError, match='label'):
        style_from_body(body)
