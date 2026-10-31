"""Origin lock, recorder action log, and studio oracle checks for P08, P09 and P24.

The headless browser test starts Chromium only when this worktree has it. Every other
test uses a fake page or recorded bytes, so the manifest gate can collect this file
without that browser.
"""
from __future__ import annotations

import hashlib
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
    map_body, map_spec, measure_png, open_studio_browser, render_contract, resolve_fixture,
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

    def attribute(self, name):
        return ''

    def evaluate(self, *args, **kwargs):
        self.calls.append('evaluate')
        raise AssertionError('evaluate is not a studio tool')

    def close(self):
        self.closed = True


class RenderPage:
    def __init__(self, png, attributes):
        self.png = png
        self.attributes = attributes
        self.url = None

    def goto(self, url):
        self.url = url
        return 200

    def screenshot(self):
        return self.png

    def attribute(self, name):
        return self.attributes.get(name, '')

    def close(self):
        pass


def _dumps(value):
    return json.dumps(value, ensure_ascii=False).encode('utf-8')


def _version_document(variant, flood):
    body = map_body(variant, EXTENTS[variant], True)
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
            return 200, _dumps(_version_document(state['variant'], state['flood']))
        return 404, b'{}'
    return factory


def _query_document(variant, name):
    document = json.loads(fixture_path(variant, name).read_bytes())
    kind = document['features'][0]['geometry']['type']
    features = []
    for index, feature in enumerate(document['features']):
        coords = feature['geometry']['coordinates']
        geometry = {'x': coords[0], 'y': coords[1]} if kind == 'Point' else {'rings': coords}
        features.append({'attributes': {'OBJECTID': index, **feature['properties']}, 'geometry': geometry})
    return {'geometryType': 'esriGeometryPoint' if kind == 'Point' else 'esriGeometryPolygon',
            'spatialReference': {'wkid': 3857}, 'exceededTransferLimit': False,
            'objectIdFieldName': 'OBJECTID', 'fields': [{'name': key} for key in features[0]['attributes']],
            'features': features}


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
    state = {'scenario': scenario_id, 'variant': variant, 'flood': flood}
    recorder = _recorder(tmp_path, state)
    labels = ','.join(label_ids(scenario_id, variant))
    page = RenderPage(png, {
        'data-loaded-layers': 'parcels,flood_zones,shelters',
        'data-label-sources': labels,
        'data-browser-errors': '',
    })
    extra = {
        'recorder': recorder, 'ensureConnection': lambda: 'conn-1', 'agentPrincipal': AGENT,
        'renderPage': page, 'studioUrl': 'http://127.0.0.1:8137',
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
    roles = resolve_roles(frozen_roles(scenario, stack, seeded, variant, None), recorder.records, agent_principal=AGENT)
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
        assert render['regions'][0]['minimumMatchingPixels'] == 4000
        assert render['labelSourceIds'] == label_ids(scenario_id, variant)
        for row in expected['source-content.unchanged']['resources']:
            fixture = oracle.fixture_expectation(variant, 'features.exact', layer=row['resourceId'])
            assert 'tolerance' not in row['content']
            assert row['content']['srid'] == 3857
            assert row['content']['features'] == sorted(fixture['features'], key=lambda feature: feature['properties']['source_id'])
    walked = _walk_extent(variant)
    assert walked == EXTENTS[variant] == fixture_extent(variant)
    composition = P08().expected(variant)['map.composition']
    assert composition['extent'] == walked
    assert composition['orderedLayerIds'] == ['parcels', 'flood_zones', 'shelters']
    style = P09().expected(variant)['map.style']
    body = map_body(variant, walked, True)
    assert style['styleDocumentSha256'] == canonical_digest(body['mapSpec'])[7:]
    assert style['floodColor'] == '#0088ff'
    assert style['floodOpacity'] == 0.35
    assert style['labelField'] == 'name'
    assert P09().expected(variant)['state.saved']['documentSha256'] == canonical_digest(body)[7:]
    assert 'tolerance' in P24().expected(variant)['features.exact']
    with pytest.raises(ValueError):
        P08().expected('C')
    with pytest.raises(ValueError):
        select_case(PACK, 'P08', 'C')


def test_style_hash_is_shared_and_the_saved_document_hash_is_not():
    assert P09().expected('A')['map.style']['styleDocumentSha256'] == P09().expected('B')['map.style']['styleDocumentSha256']
    assert P09().expected('A')['state.saved']['documentSha256'] != P09().expected('B')['state.saved']['documentSha256']


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
    })
    first = P08().seed(stack, 'A')
    assert P08().seed(stack, 'A') == first
    assert calls == ['A']
    assert stack.extra['needsStudioBrowser'] is True
    with pytest.raises(ValueError):
        P08().seed(stack, 'B')


@pytest.mark.parametrize('scenario_id', ['P08', 'P09'])
def test_fixture_style_render_and_source_content_pass(monkeypatch, tmp_path, scenario_id):
    outcome = _run(tmp_path, monkeypatch, scenario_id, 'A', png=BLUE_PNG)
    failed = {name: row for name, row in outcome.results.items() if row['status'] != 'pass'}
    assert failed == {}
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


def test_red_canvas_fails_the_render_while_the_style_passes(monkeypatch, tmp_path):
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
    assert 'region absent' in outcome.results['render.actual']['reason']


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


def test_p24_scores_style_render_and_source_and_fails_the_rest_closed(monkeypatch, tmp_path):
    outcome = _run(tmp_path, monkeypatch, 'P24', 'A', png=BLUE_PNG, published=True)
    assert P24().needs_approver() is True
    assert P08().needs_approver() is False
    assert P09().needs_approver() is False
    assertions = list(select_case(PACK, 'P24', 'A')['assertions'])
    assert [row.assertion_id for row in outcome.observations] == assertions
    assert all(row.observed is None for row in outcome.observations)
    blob = json.dumps([row.evidence for row in outcome.observations])
    assert 'Kestrel' not in blob and 'Willow' not in blob
    for name in ('features.exact', 'map.style', 'render.actual', 'source-content.unchanged'):
        assert _status(outcome, name) == 'pass', outcome.results[name]
    assert _status(outcome, 'custody.worker-replaced') == 'excluded'
    assert _status(outcome, 'candidate.identity') == 'fail'
    assert 'unrecorded step' in outcome.results['candidate.identity']['reason']


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
        <div id="honua-render" data-loaded-layers="parcels,flood_zones,shelters" data-label-sources="" data-browser-errors=""></div>
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
