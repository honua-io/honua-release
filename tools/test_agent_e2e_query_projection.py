"""Protocol-shaped fixtures test replay; they are not live qualification."""
import copy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_e2e_pack import PackError
from agent_e2e_query_projection import normalize
from agent_e2e_query_recipes import point_selection_recipe
from agent_e2e_semantics import fixture, project_observation, response_evidence, verify


def query(features=None):
    rows = fixture()['features'] if features is None else features
    return {'geometryType': 'esriGeometryPoint', 'objectIdFieldName': 'objectid',
            'spatialReference': {'wkid': 3857, 'latestWkid': 3857}, 'exceededTransferLimit': False,
            'fields': [{'name': 'objectid', 'type': 'esriFieldTypeOID'},
                       {'name': 'source_id', 'type': 'esriFieldTypeString'},
                       {'name': 'capacity', 'type': 'esriFieldTypeInteger'}],
            'features': [{'attributes': {'objectid': index+700, **row['properties']},
                          'geometry': dict(zip(('x', 'y'), row['geometry']['coordinates']))}
                         for index, row in enumerate(rows)]}


def raw(value, status=200):
    return response_evidence(status, json.dumps(value).encode())


def project(case, before, selected, after, status=200):
    recipe = point_selection_recipe('observed service', 37, case)
    projection = {'sourceCount': 1, 'sourceFields': recipe['source']['result'], 'result': recipe['result']}
    return project_observation([raw(before), raw(selected, status), raw(after)], projection)


@pytest.mark.parametrize('case,indexes', [('select-strict', [2]), ('select-inclusive', [1, 2]),
                                        ('select-inclusive-null', [1, 2, 3])])
def test_protocol_response_replays_into_independent_selection_predicate(case, indexes):
    source = query()
    selected = query([fixture()['features'][index] for index in indexes])
    before = copy.deepcopy(source)
    observed = project(case, source, selected, source)
    verify(case, observed)
    assert observed['sourceBefore'] == fixture()
    assert source == before  # No destructive mutation of original evidence.
    assert observed['result']['srid'] == 3857
    recipe = point_selection_recipe('observed service', 37, case)
    url = urlsplit(recipe['requests'][0]['path'])
    assert url.path == '/rest/services/observed%20service/FeatureServer/37/query'
    params = parse_qs(url.query)
    assert params['outFields'] == ['*'] and params['outSR'] == ['3857']
    assert params['where'] == [{'select-strict': 'capacity > 50', 'select-inclusive': 'capacity >= 50',
                               'select-inclusive-null': 'capacity >= 50 OR capacity IS NULL'}[case]]


@pytest.mark.parametrize('change', [
    lambda value: value.update(error={'code': 500}),
    lambda value: value.update(exceededTransferLimit=True),
    lambda value: value.pop('exceededTransferLimit'),
    lambda value: value.update(transform={'scale': [1, 1]}),
    lambda value: value.update(hasZ=True),
    lambda value: value.update(hasM=True),
    lambda value: value.update(geometryType='esriGeometryPolygon'),
    lambda value: value.update(spatialReference={'wkid': 3857, 'latestWkid': 4326}),
    lambda value: value.update(spatialReference={'wkid': True}),
    lambda value: value.update(objectIdFieldName='source_id'),
    lambda value: value['fields'][0].update(type='esriFieldTypeInteger'),
    lambda value: value['fields'].append(value['fields'][1]),
    lambda value: value['features'][0]['attributes'].pop('capacity'),
    lambda value: value['features'][0]['attributes'].update(objectid=True),
    lambda value: value['features'][1]['attributes'].update(objectid=700),
    lambda value: value['features'][1]['attributes'].update(source_id='s1'),
    lambda value: value['features'][0]['geometry'].update(x=True),
    lambda value: value['features'][0]['geometry'].update(y=float('nan')),
    lambda value: value['features'][0]['geometry'].update(z=10),
    lambda value: value['features'][0]['geometry'].update(spatialReference={'wkid': 4326}),
])
def test_ambiguous_partial_or_unsupported_query_cannot_be_normalized(change):
    value = query()
    change(value)
    with pytest.raises(PackError):
        normalize('feature-server-points/v1', value)


@pytest.mark.parametrize('change', [
    lambda value: value['features'][0]['attributes'].update(capacity=900),
    lambda value: value['features'][0]['geometry'].update(x=900),
    lambda value: value.update(spatialReference={'wkid': 4326}),
    lambda value: value['features'].reverse(),
    lambda value: value['features'].pop(),
])
def test_normalization_does_not_hide_source_changes(change):
    source, after = query(), query()
    change(after)
    selected = query([fixture()['features'][2]])
    with pytest.raises(PackError, match='source differs or changed'):
        verify('select-strict', project('select-strict', source, selected, after))


def test_unknown_attribute_is_preserved_and_rejected_by_source_oracle():
    source = query()
    source['fields'].append({'name': 'unexpected', 'type': 'esriFieldTypeString'})
    for row in source['features']:
        row['attributes']['unexpected'] = 'observed'
    assert normalize('feature-server-points/v1', source)['features'][0]['properties']['unexpected'] == 'observed'
    with pytest.raises(PackError, match='source differs or changed'):
        verify('select-strict', project('select-strict', source, query([fixture()['features'][2]]), source))


def test_error_http_status_cannot_be_projected_as_success():
    with pytest.raises(PackError, match='unsuccessful query'):
        project('select-strict', query(), query([fixture()['features'][2]]), query(), status=403)


@pytest.mark.parametrize('srid', [4326, None, True])
def test_correct_membership_and_coordinates_cannot_hide_wrong_or_missing_result_crs(srid):
    source, selected = query(), query([fixture()['features'][2]])
    observed = project('select-strict', source, selected, source)
    if srid is None:
        observed['result'].pop('srid')
    else:
        observed['result']['srid'] = srid
    with pytest.raises(PackError, match='coordinate reference system'):
        verify('select-strict', observed)


def test_unsupported_normalizer_and_unbound_recipe_fail():
    with pytest.raises(PackError, match='unreviewed'):
        normalize('python-expression', query())
    for service, layer, case in [('foreign/service', 3, 'select-strict'), ('ok', True, 'select-strict'),
                                  ('ok', 3, 'invented-case'), ('..', 3, 'select-strict'),
                                  ('ok', 2147483648, 'select-strict')]:
        with pytest.raises(PackError):
            point_selection_recipe(service, layer, case)


@pytest.mark.parametrize('case', ['select-strict', 'select-inclusive', 'select-inclusive-null'])
def test_captured_candidate_api_bytes_replay_through_production_recipe(case):
    path = Path(__file__).resolve().parents[1]/'certification/agent-e2e/fixtures/feature-server-points-0e03bdd.json'
    capture = json.loads(path.read_text(encoding='utf-8'))
    assert capture['qualification'] is False
    assert capture['setup']['publication']['httpStatus'] == 201
    assert capture['setup']['scopedEnable']['body']['data']['enabled'] is True
    assert capture['setup']['initialQuery']['body']['error']['code'] == 404
    rows = capture['queries'][case]
    recipe = point_selection_recipe('sem-probe-624', capture['layerId'], case)
    projection = {'sourceCount': 1, 'sourceFields': recipe['source']['result'], 'result': recipe['result']}
    observed = project_observation([rows[phase] for phase in ('before', 'operation', 'after')], projection)
    verify(case, observed)
    # HTTP 200 carrying a protocol error cannot replace a successful query.
    with pytest.raises(PackError, match='successful FeatureServer query'):
        project_observation([rows['before'], capture['setup']['initialQuery'], rows['after']], projection)
