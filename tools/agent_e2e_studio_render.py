"""Replay actual deployed Studio browser render originals and decode its PNG.

The contract's document, feature, style and pixel predicates are source-frozen.
Only the explicitly bound native saved version can select a document; candidate
pixels/content never supply expected values. The unchanged render.actual oracle
still checks minimum visible geometry, region pixels, layers, labels and errors.
"""
from __future__ import annotations

import base64
import importlib.util
import re
from pathlib import Path
from urllib.parse import urlencode

from agent_e2e_pack import PackError, canonical_digest, digest_bytes
from agent_e2e_studio_observer import instant

_path = Path(__file__).resolve().parents[1]/'certification/installed-clients/render_png.py'
_spec = importlib.util.spec_from_file_location('honua_agent_render_png', _path)
_png = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_png)


def require(condition, reason):
    if not condition:
        raise PackError(reason)


def binary(value, *, encoded='bodyBase64', maximum=2*1024*1024):
    require(isinstance(value, dict) and type(value.get('bytes')) is int and 0 <= value['bytes'] <= maximum and
            isinstance(value.get(encoded), str) and len(value[encoded]) <= 4*((maximum+2)//3),
            'browser original exceeds its retained byte bound')
    try:
        raw = base64.b64decode(value[encoded], validate=True)
    except ValueError:
        raise PackError('browser original has invalid base64') from None
    require(len(raw) == value['bytes'] and digest_bytes(raw) == 'sha256:'+value.get('sha256', ''),
            'browser original byte count or digest changed')
    return raw


def original(value, path):
    require(value.get('path') == path and value.get('status') == 200,
            'browser original read differs from the exact native path/status')
    raw = binary(value)
    # Reuse the strict duplicate-key/nonfinite parser used by the campaign.
    from agent_e2e_mcp import parse
    parsed = parse(raw)
    require(isinstance(parsed, dict) and parsed.get('success', True) is not False and 'error' not in parsed,
            'browser native original reports a product failure')
    return parsed.get('data', parsed)


def channels(value):
    return isinstance(value, list) and len(value) == 4 and all(type(channel) is int and 0 <= channel <= 255 for channel in value)


def project(report, contract):
    require(isinstance(contract, dict) and contract.get('schemaVersion') == 1 and
            isinstance(report, dict) and report.get('schema') == 'honua.studio-browser-render/v1' and
            report.get('candidateKey') == contract.get('candidateKey') and
            report.get('contractDigest') == canonical_digest(contract),
            'browser render differs from source-frozen candidate/contract')
    require(instant(report['startedAt']) <= instant(report['completedAt']) and report.get('errors') == [] and
            report.get('session') == {**contract['session'], 'authenticated': True},
            'browser render lacks original signed-in context or contains runtime/network errors')
    require(original(report['consoleVersion'], '/version.json').get('commit') == contract['consoleCommit'],
            'browser Console differs from the installed candidate source')
    assets = report.get('runtimeAssets')
    require(isinstance(assets, list) and len(assets) == len(contract['runtimeAssets']) and
            [{key: row[key] for key in ('path', 'sha256')} for row in assets] == contract['runtimeAssets'] and
            all(row.get('status') == 200 and type(row.get('bytes')) is int and 0 < row['bytes'] <= 16*1024*1024 for row in assets),
            'browser deployed renderer assets differ from frozen source')
    scope = contract['scope']
    paths = {'draft': '/api/studio/v1/studio/package-drafts/'+scope['draftId'],
        'version': '/api/studio/v1/studio/content-items/'+scope['itemId']+'/versions/'+scope['versionId'],
        'current': '/api/studio/v1/studio/content-items?'+urlencode({'family': 'map', 'workspaceId': scope['namespace'],
            'owner': scope['ownerPrincipalId'], 'limit': '100'})}
    require(isinstance(report.get('native'), dict) and set(report['native']) == {'before', 'after'} and
            report['native']['before'] == report['native']['after'] and set(report['native']['before']) == set(paths),
            'native draft/version/current pointer changed during browser rendering')
    resources = {name: original(report['native']['before'][name], path) for name, path in paths.items()}
    draft, version, current = resources['draft'], resources['version'], resources['current']
    for value in (draft, version):
        require(value.get('itemId') == scope['itemId'] and value.get('ownerId') == scope['ownerPrincipalId'] and
                value.get('workspaceId') == value.get('packageKey') == scope['namespace'] and
                value.get('envelope', {}).get('schemaVersion') == '1.0' and value['envelope'].get('family') == 'map' and
                canonical_digest(value['envelope'].get('body')) == 'sha256:'+scope['documentSha256'],
                'browser rendered body differs from independently frozen document/scope')
    require(draft.get('draftId') == scope['draftId'] and version.get('versionId') == scope['versionId'] and
            version.get('sourceDraftId') == scope['draftId'] and draft['envelope'] == version['envelope'],
            'browser rendered a different draft or immutable saved version')
    require(current.get('total') == 1 and current.get('nextCursor') is None and len(current.get('items', [])) == 1 and
            current['items'][0].get('itemId') == scope['itemId'] and current['items'][0].get('currentVersionId') == scope['versionId'],
            'browser rendered an unrelated or superseded saved version')
    network = report.get('network')
    require(isinstance(network, list) and network and all(row.get('method') in ('GET', 'HEAD') and
            type(row.get('status')) is int and 200 <= row['status'] < 400 for row in network) and
            any(row.get('path', '').startswith('/api/studio/data/') and row.get('status') == 200 and
                type(row.get('bytes')) is int and row['bytes'] > 0 and re.fullmatch(r'[0-9a-f]{64}', row.get('sha256', ''))
                for row in network), 'render lacks successful authorized native feature network observations')
    rendered = report.get('render', {})
    projection = rendered.get('projection', {})
    require(rendered.get('status') == 'ready' and projection.get('unresolved') == [] and
            type(rendered.get('renderedFeatureCount')) is int and rendered['renderedFeatureCount'] > 0,
            'deployed native map is absent, unresolved or featureless')
    require(isinstance(rendered.get('layers'), list) and len(rendered['layers']) == len(contract['layers']) and
            [row.get('id') for row in rendered['layers']] == [row['id'] for row in contract['layers']],
            'rendered native layer inventory differs from fixed predicates')
    label_ids = set()
    for observed, wanted in zip(rendered['layers'], contract['layers']):
        styles, features = observed.get('styles', []), observed.get('features', [])
        primary = [row for row in styles if row.get('id') == wanted['id']]
        require(len(primary) == 1 and all(primary[0].get('paint', {}).get(key) == value for key, value in wanted['paint'].items()) and
                all(primary[0].get('layout', {}).get(key) == value for key, value in wanted['layout'].items()),
                'actual MapLibre layer paint/layout differs from frozen style predicates')
        source_ids = set()
        for feature in features:
            require(isinstance(feature.get('properties'), dict) and isinstance(feature['properties'].get(wanted['sourceIdProperty']), str),
                    'rendered native feature lost its original source identity')
            source_id = feature['properties'][wanted['sourceIdProperty']]
            source_ids.add(source_id)
            if 'geometryType' in wanted:
                require(feature.get('geometry', {}).get('type') == wanted['geometryType'], 'rendered geometry type differs from frozen predicate')
            for key, value in wanted.get('properties', {}).get(source_id, {}).items():
                require(feature['properties'].get(key) == value, 'rendered feature property differs from frozen predicate')
            if feature.get('type') == 'symbol':
                style = next((row for row in styles if row.get('id') == feature.get('layerId')), {})
                require(style.get('layout', {}).get('visibility', 'visible') != 'none' and
                        style.get('paint', {}).get('text-opacity', 1) != 0 and style.get('layout', {}).get('text-field'),
                        'rendered label is hidden, transparent or has no text field')
                label_ids.add(source_id)
        require(source_ids == set(wanted['featureIds']), 'actual rendered source features differ from frozen viewport predicates')
    raw = binary(report['png'], encoded='base64', maximum=16*1024*1024)
    try:
        width, height, pixel = _png.decode_png(raw)
    except ValueError as error:
        raise PackError('render PNG decoder refused '+str(error)) from None
    require((width, height) == (contract['width'], contract['height']), 'decoded canvas size differs from fixed viewport')
    background, tolerance = contract['background'], contract['backgroundTolerance']
    require(channels(background) and type(tolerance) is int and 0 <= tolerance <= 255, 'invalid frozen background pixel predicate')
    def matches(actual, expected, slack):
        return all(abs(left-right) <= slack for left, right in zip(actual, expected))
    non_background = sum(not matches(pixel(x, y), background, tolerance) for y in range(height) for x in range(width))
    regions = {}
    for region in contract['regions']:
        x, y, across, down = region['rect']
        require(all(type(value) is int for value in (x, y, across, down)) and 0 <= x < width and 0 <= y < height and
                0 < across <= width-x and 0 < down <= height-y and channels(region['rgba']) and
                type(region['tolerance']) is int and 0 <= region['tolerance'] <= 255 and region['id'] not in regions,
                'invalid or duplicate frozen pixel region')
        regions[region['id']] = {'matchingPixels': sum(matches(pixel(column, row), region['rgba'], region['tolerance'])
            for row in range(y, y+down) for column in range(x, x+across))}
    return {'httpStatus': 200, 'decodedWidth': width, 'decodedHeight': height,
        'loadedLayerIds': projection.get('renderedLayerIds'), 'nonBackgroundPixels': non_background,
        'regions': regions, 'visibleLabelSourceIds': sorted(label_ids), 'browserErrors': report['errors']}
