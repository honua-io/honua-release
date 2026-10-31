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
