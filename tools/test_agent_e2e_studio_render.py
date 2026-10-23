"""Pixel/native-original replay controls; these are never live campaign receipts."""
import base64
import copy
import json
import struct
import zlib

import pytest

from agent_e2e_pack import PackError, canonical_digest, digest_bytes
from agent_e2e_studio_render import project
from agent_e2e_gate import oracle_predicates


def png():
    def chunk(kind, raw):
        return struct.pack('>I', len(raw))+kind+raw+struct.pack('>I', zlib.crc32(kind+raw))
    rows = []
    for y in range(256):
        rows.append(b'\0'+b''.join(bytes((239, 32, 32, 255) if 120 <= x < 136 and 120 <= y < 136 else (255, 255, 255, 255)) for x in range(256)))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('>IIBBBBB', 256, 256, 8, 6, 0, 0, 0))+chunk(b'IDAT', zlib.compress(b''.join(rows)))+chunk(b'IEND', b'')


def observed(path, body):
    raw = json.dumps(body, separators=(',', ':')).encode()
    return {'path': path, 'status': 200, 'bytes': len(raw), 'sha256': digest_bytes(raw)[7:], 'bodyBase64': base64.b64encode(raw).decode()}


@pytest.fixture
def rendering():
    item, draft, version = ('11111111-1111-1111-1111-111111111111',
        '22222222-2222-2222-2222-222222222222', '33333333-3333-3333-3333-333333333333')
    body = {'layers': [{'id': 'shelters', 'sourceId': 'service/1', 'visible': True}], 'view': {'center': [0, 0], 'zoom': 2}}
    scope = {'itemId': item, 'draftId': draft, 'versionId': version, 'namespace': 'ae_render_control',
        'ownerPrincipalId': 'real-scoped-owner', 'documentSha256': canonical_digest(body)[7:]}
    contract = {'schemaVersion': 1, 'candidateKey': 'sha256:'+'a'*64, 'consoleCommit': 'b'*40,
        'endpoint': 'http://127.0.0.1:3030', 'session': {'principalId': 'signed-in-human', 'environmentId': 'actual-target'},
        'scope': scope, 'runtimeAssets': [{'path': '/_studio/console.js', 'sha256': 'c'*64}],
        'width': 256, 'height': 256, 'background': [255, 255, 255, 255], 'backgroundTolerance': 0,
        'layers': [{'id': 'shelters', 'sourceIdProperty': 'source_id', 'featureIds': ['s1'], 'geometryType': 'Point',
            'properties': {'s1': {'name': 'First shelter'}}, 'paint': {'circle-color': '#ef2020'}, 'layout': {}}],
        'regions': [{'id': 'shelter', 'rect': [120, 120, 16, 16], 'rgba': [239, 32, 32, 255], 'tolerance': 0}]}
    base = {'itemId': item, 'ownerId': scope['ownerPrincipalId'], 'workspaceId': scope['namespace'],
        'packageKey': scope['namespace'], 'envelope': {'family': 'map', 'schemaVersion': '1.0', 'body': body}}
    draft_value = {**base, 'draftId': draft, 'generation': 3}
    version_value = {**base, 'sourceDraftId': draft, 'versionId': version, 'versionNumber': 1}
    current = {'total': 1, 'nextCursor': None, 'items': [{'itemId': item, 'currentVersionId': version}]}
    from urllib.parse import urlencode
    originals = {'draft': observed('/api/studio/v1/studio/package-drafts/'+draft, draft_value),
        'version': observed('/api/studio/v1/studio/content-items/'+item+'/versions/'+version, version_value),
        'current': observed('/api/studio/v1/studio/content-items?'+urlencode({'family': 'map', 'workspaceId': scope['namespace'],
            'owner': scope['ownerPrincipalId'], 'limit': '100'}), current)}
    image = png()
    report = {'schema': 'honua.studio-browser-render/v1', 'candidateKey': contract['candidateKey'], 'contractDigest': canonical_digest(contract),
        'startedAt': '2026-10-22T00:00:00Z', 'completedAt': '2026-10-22T00:01:00Z', 'errors': [],
        'session': {**contract['session'], 'authenticated': True}, 'consoleVersion': observed('/version.json', {'commit': contract['consoleCommit']}),
        'runtimeAssets': [{**row, 'status': 200, 'bytes': 100} for row in contract['runtimeAssets']],
        'native': {'before': originals, 'after': copy.deepcopy(originals)},
        'network': [{'method': 'GET', 'path': '/api/studio/data/rest/services/shelters/FeatureServer/1/query?f=geojson',
            'status': 200, 'bytes': 200, 'sha256': 'e'*64}],
        'render': {'status': 'ready', 'projection': {'unresolved': [], 'renderedLayerIds': ['shelters']}, 'renderedFeatureCount': 1,
            'layers': [{'id': 'shelters', 'styles': [{'id': 'shelters', 'type': 'circle', 'paint': {'circle-color': '#ef2020'}}],
                'features': [{'layerId': 'shelters', 'type': 'circle', 'properties': {'source_id': 's1', 'name': 'First shelter'},
                    'geometry': {'type': 'Point', 'coordinates': [0, 0]}}]}]},
        'png': {'base64': base64.b64encode(image).decode(), 'sha256': digest_bytes(image)[7:], 'bytes': len(image)}}
    expected = {'layerIds': ['shelters'], 'minimumNonBackgroundPixels': 200,
        'regions': [{'id': 'shelter', 'minimumMatchingPixels': 200}], 'labelSourceIds': []}
    return report, contract, expected


def test_original_canvas_pixels_and_native_scope_drive_unchanged_corpus_predicate(rendering):
    report, contract, expected = rendering
    actual = project(report, contract)
    assert actual['nonBackgroundPixels'] == 256 and actual['regions']['shelter']['matchingPixels'] == 256
    oracle_predicates.verify('render.actual', actual, expected)


@pytest.mark.parametrize('change', ['different-contract', 'different-candidate', 'wrong-human', 'runtime-error', 'network-failed',
    'writer-request', 'text-summary', 'wrong-native-owner', 'wrong-body', 'later-save', 'forged-bytes', 'style', 'feature',
    'geometry', 'property', 'unresolved', 'no-features', 'wrong-console', 'asset'])
def test_browser_or_rehashed_native_claims_cannot_replace_frozen_render_expectations(rendering, change):
    report, contract, _ = rendering
    if change == 'different-contract': contract['scope']['documentSha256'] = 'f'*64
    elif change == 'different-candidate': report['candidateKey'] = 'sha256:'+'f'*64
    elif change == 'wrong-human': report['session']['principalId'] = 'machine'
    elif change == 'runtime-error': report['errors'].append({'kind': 'pageerror', 'sha256': 'e'*64})
    elif change == 'network-failed': report['network'][0]['status'] = 403
    elif change == 'writer-request': report['network'][0]['method'] = 'POST'
    elif change == 'text-summary': report['png'] = {'bytes': 7, 'base64': base64.b64encode(b'summary').decode(), 'sha256': digest_bytes(b'summary')[7:]}
    elif change in ('wrong-native-owner', 'wrong-body', 'later-save'):
        name = 'current' if change == 'later-save' else 'version'
        original = report['native']['before'][name]
        body = json.loads(base64.b64decode(original['bodyBase64']))
        if change == 'wrong-native-owner': body['ownerId'] = 'other-owner'
        elif change == 'wrong-body': body['envelope']['body']['view']['zoom'] = 3
        else: body['items'][0]['currentVersionId'] = '44444444-4444-4444-4444-444444444444'
        changed = observed(original['path'], body)
        report['native']['before'][name] = changed
        report['native']['after'][name] = copy.deepcopy(changed)
    elif change == 'forged-bytes': report['png']['sha256'] = 'e'*64
    elif change == 'style': report['render']['layers'][0]['styles'][0]['paint']['circle-color'] = '#ffffff'
    elif change == 'feature': report['render']['layers'][0]['features'][0]['properties']['source_id'] = 's2'
    elif change == 'geometry': report['render']['layers'][0]['features'][0]['geometry']['type'] = 'Polygon'
    elif change == 'property': report['render']['layers'][0]['features'][0]['properties']['name'] = 'Other shelter'
    elif change == 'unresolved': report['render']['projection']['unresolved'] = [{'layerId': 'shelters'}]
    elif change == 'no-features': report['render']['renderedFeatureCount'] = 0
    elif change == 'wrong-console': report['consoleVersion'] = observed('/version.json', {'commit': 'f'*40})
    else: report['runtimeAssets'][0]['sha256'] = 'f'*64
    with pytest.raises((PackError, ValueError)): project(report, contract)


def test_observed_pixels_cannot_redefine_expected_geometry_region(rendering):
    report, contract, expected = rendering
    contract['regions'][0]['rect'] = [0, 0, 16, 16]
    report['contractDigest'] = canonical_digest(contract)
    with pytest.raises(ValueError, match='region absent'):
        oracle_predicates.verify('render.actual', project(report, contract), expected)


@pytest.mark.parametrize('change', [None, 'field', 'color', 'hidden', 'missing', 'duplicate', 'type'])
def test_auxiliary_symbol_style_is_independently_frozen_with_its_composition_owner(rendering, change):
    report, contract, _ = rendering
    wanted = {'id': 'shelter-names', 'type': 'symbol', 'paint': {'text-color': '#1b1b1b'},
        'layout': {'text-field': ['get', 'name'], 'text-size': 14, 'visibility': 'visible'}}
    contract['layers'][0]['styles'] = [wanted]
    report['contractDigest'] = canonical_digest(contract)
    styles = report['render']['layers'][0]['styles']
    styles.append(copy.deepcopy(wanted))
    if change == 'field': styles[-1]['layout']['text-field'] = ['get', 'other']
    elif change == 'color': styles[-1]['paint']['text-color'] = '#ffffff'
    elif change == 'hidden': styles[-1]['layout']['visibility'] = 'none'
    elif change == 'missing': styles.pop()
    elif change == 'duplicate': styles.append(copy.deepcopy(wanted))
    elif change == 'type': styles[-1]['type'] = 'circle'
    if change is None:
        assert project(report, contract)['loadedLayerIds'] == ['shelters']
    else:
        with pytest.raises(PackError, match='auxiliary MapLibre style differs'):
            project(report, contract)
