"""Owner sets bind only to runner-attested user/key custody."""
import hashlib
import json

import pytest

from agent_e2e_local_api import RoleBinding
from agent_e2e_local_projections import resolve_roles
from agent_e2e_local_recorder import ProjectionError, Record

PATH = '/api/v1/studio/content-items?family=map&limit=200'
USER = 'subject:https%3A%2F%2Fissuer:customer@tenant:public'
KEY = 'runner-key-id'
FORMATS = ['{agentUser}', '{agentKey}', 'api-key:{agentKey}']
CUSTODY = {'agentUserId': USER, 'agentKeyId': KEY, 'agentKeyOwnerUserId': USER}


def record(step, phase, items):
    raw = json.dumps({'data': {'items': items, 'total': len(items)}}).encode()
    return Record(step, phase, 'http', 'controller', None, 'GET', PATH, 200, raw,
                  'sha256:' + hashlib.sha256(raw).hexdigest())


def binding(formats=FORMATS):
    return RoleBinding('created:map', 'after', items='/data/items', id='/id', owner='/owner',
                       before='before', owner_any_of=formats)


def records(owner):
    return {'before': record('before', 'seed', []),
            'after': record('after', 'observe', [{'id': 'new-map', 'owner': owner}])}


@pytest.mark.parametrize('owner', [USER, KEY, 'api-key:' + KEY])
def test_created_resource_accepts_only_attested_user_or_linked_key(owner):
    assert resolve_roles([binding()], records(owner), agent_principal=KEY, custody=CUSTODY) == {'created:map': 'new-map'}


@pytest.mark.parametrize('custody', [{}, {**CUSTODY, 'agentKeyOwnerUserId': 'other'},
                                    {**CUSTODY, 'agentKeyId': 'other'}])
def test_owner_set_requires_runner_attested_key_user_link(custody):
    with pytest.raises(ProjectionError, match='does not link'):
        resolve_roles([binding()], records(USER), agent_principal=KEY, custody=custody)


def test_owner_set_rejects_other_owner_and_unreviewed_formats():
    with pytest.raises(ProjectionError, match='matched 0'):
        resolve_roles([binding()], records('other'), agent_principal=KEY, custody=CUSTODY)
    with pytest.raises(ProjectionError, match='unreviewed owner set'):
        resolve_roles([binding(['{agentUser}', '{modelClaim}'])], records(USER), agent_principal=KEY, custody=CUSTODY)


def test_geoservices_boolean_cross_check_preserves_type_and_rejects_disagreement():
    from agent_e2e_local_projections import restore_feature_booleans
    from agent_e2e_local_recorder import ProjectionError
    normalized = {'srid': 3857, 'features': [{'properties': {'source_id': 's1', 'is_open': 1}}]}
    geo = {'type': 'FeatureCollection', 'features': [{'properties': {'source_id': 's1', 'is_open': True}}]}
    assert restore_feature_booleans(normalized, geo, ['is_open'])['features'][0]['properties']['is_open'] is True
    normalized['features'][0]['properties']['is_open'] = 0
    with pytest.raises(ProjectionError, match='value differs'):
        restore_feature_booleans(normalized, geo, ['is_open'])


@pytest.mark.parametrize('bounds', [[-20010, -5010, -18800, -4700], [12990, -8010, 14200, -7700]])
def test_studio_view_reprojects_fixture_and_rejects_wrong_crs(bounds):
    import math
    from agent_e2e_local_projections import studio_view_contains_fixture
    def lon(x): return math.degrees(x / 6378137)
    def lat(y): return math.degrees(2 * math.atan(math.exp(y / 6378137)) - math.pi / 2)
    view = {'bbox': [lon(bounds[0]), lat(bounds[1]), lon(bounds[2]), lat(bounds[3])], 'crs': 'EPSG:4326'}
    assert studio_view_contains_fixture(view, bounds)
    view['crs'] = 'EPSG:3857'
    with pytest.raises(ProjectionError, match='CRS'):
        studio_view_contains_fixture(view, bounds)
    view['crs'] = 'EPSG:4326'
    view['bbox'][0] = view['bbox'][2] - (view['bbox'][2] - view['bbox'][0]) / 2
    assert not studio_view_contains_fixture(view, bounds)


def test_audited_map_sources_require_customer_publish_and_reject_claims():
    from types import SimpleNamespace
    from agent_e2e_local_projections import audited_map_sources
    now = '2026-10-31T22:00:00+00:00'
    later = '2026-10-31T22:10:00+00:00'
    names = ('parcels', 'flood_zones', 'shelters')
    rows = [{'serviceId': 'svc-' + n, 'serviceName': n, 'layerId': i,
             'studioSourceId': f'svc-{n}/{i}'} for i, n in enumerate(names, 1)]
    body = {'layers': [{'id': n, 'sourceId': row['studioSourceId']} for n, row in zip(names, rows)],
            'sourceBindings': [{'sourceId': row['studioSourceId'], 'protocol': 'geoservices_feature_service',
                'locator': {'url': f"/rest/services/{row['serviceName']}/FeatureServer/{row['layerId']}",
                            'layerId': row['layerId']}} for row in rows]}
    record = lambda value, **kw: SimpleNamespace(step='test', json=value, status=200, phase=kw.get('phase', 'observe'),
        method='POST' if kw.get('kind') == 'mcp' else 'GET',
        principal='controller', kind=kw.get('kind', 'http'), path=kw.get('path', '/x'),
        recorded_at=kw.get('at', later), request_arguments=kw.get('arguments'))
    records = {'p24-maps-before': record({}, phase='seed', at=now, path='/api/v1/studio/content-items?family=map&limit=200'),
      'p24-version': record({'data': {'envelope': {'body': body}}}, path='/api/v1/studio/content-items/map/versions/ver'),
      'p24-catalog': record({'result': {'structuredContent': {'layers': rows, 'totalCount': 3, 'offset': 0,
          'layerCount': 3, 'hasMore': False}}}, kind='mcp', path='/mcp#tools/call',
          arguments={'tool': 'honua_list_layers', 'arguments': {'offset': 0, 'limit': 500}}),
      'p24-audit': record({'items': [{'actor': 'apikey:api-key:' + KEY, 'resourceType': 'operation_instance',
          'resourceId': f'op-{i}', 'action': 'operation.completed', 'outcome': 'Success',
          'details': 'operationId=service.publish;status=Completed', 'timestamp': '2026-10-31T22:05:00+00:00'}
          for i in range(1, 4)], 'nextCursor': None}, path='/api/v1/admin/observability/audit?action=operation.completed&resourceType=operation_instance&pageSize=200')}
    receipts = [{'tool': 'honua_publish_service', 'status': 'pass', 'json': {'result': {'structuredContent': {
        'status': 'Completed', 'operationId': 'service.publish', 'operationInstanceId': f'op-{i}',
        'serviceName': n, 'layerId': str(i)}}}} for i, n in enumerate(names, 1)]
    result = audited_map_sources(records, receipts, CUSTODY, KEY, 'map')
    assert result['shelters'] == {'service': 'shelters', 'layerId': 3}
    receipts[-1]['json']['result']['structuredContent']['serviceName'] = 'uncreated'
    with pytest.raises(ProjectionError):
        audited_map_sources(records, receipts, CUSTODY, KEY, 'map')
    receipts[-1]['json']['result']['structuredContent']['serviceName'] = 'shelters'
    records['p24-audit'].json['items'][-1]['actor'] = 'other-customer'
    with pytest.raises(ProjectionError):
        audited_map_sources(records, receipts, CUSTODY, KEY, 'map')
    records['p24-audit'].json['items'][-1]['actor'] = 'apikey:api-key:' + KEY
    records['p24-audit'].json['items'][-1]['timestamp'] = '2026-10-31T21:59:59+00:00'
    with pytest.raises(ProjectionError):
        audited_map_sources(records, receipts, CUSTODY, KEY, 'map')
