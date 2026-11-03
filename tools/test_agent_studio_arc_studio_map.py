"""PREREG A28 and A30: stage four scores a map styled and saved in Studio in Console.

The body is the real Studio-saved map from the no-model stage-4 check (run8, honua-eval-4, 2026-11-03):
test_ai_arc_fixtures/studio-saved-map/run8-body.json (only the vendored basemap's GeoJSON features are emptied)."""
import copy
import json
from pathlib import Path

import pytest

from agent_browser_studio import (authored_paint_colors, blended_over, has_saved_view, is_studio_authored_map,
                                  normalize_studio_authored_map, studio_composition_without_view, style_from_body)
from agent_e2e_errors import PackError
from agent_e2e_local_recorder import ProjectionError
from agent_e2e_studio_arc import blue_see_through

BODY = json.loads((Path(__file__).parent / 'test_ai_arc_fixtures/studio-saved-map/run8-body.json').read_text())['body']
# run8's controller catalog: Studio's source id is the catalog serviceId plus the layer id.
CATALOG = {'parcels': 'svc-publish-kestrel/2', 'flood_zones': 'svc-publish-kestrel/3', 'shelters': 'svc-publish-kestrel/1'}
FIXTURES = {'parcels': {'service': 'kestrel', 'layerId': 2}, 'flood_zones': {'service': 'kestrel', 'layerId': 3},
            'shelters': {'service': 'kestrel', 'layerId': 1}}


def body():
    return copy.deepcopy(BODY)


def rows(value, ident):
    return [row for row in value['mapSpec']['layers'] if row['id'] == ident]


def test_the_real_studio_body_is_the_authored_shape_and_literal_ids_cannot_score_it():
    assert is_studio_authored_map(BODY) and 'sourceBindings' not in BODY and BODY['view'] == {}
    with pytest.raises(PackError, match='ambiguous or missing actual flood/shelter'):
        style_from_body(BODY)          # D4: the literal flood_zones / shelters ids are absent from a Studio map


def test_studio_map_binds_to_fixture_names_through_the_catalog():
    normalized = normalize_studio_authored_map(body(), CATALOG, FIXTURES)
    assert [(r['id'], r['type'], r['sourceId']) for r in normalized['layers']] == [
        ('parcels', 'fill', 'svc-publish-kestrel/2'), ('flood_zones', 'fill', 'svc-publish-kestrel/3'),
        ('shelters', 'circle', 'svc-publish-kestrel/1')]
    assert normalized['sourceBindings'] == [
        {'sourceId': CATALOG[n], 'protocol': 'geoservices_feature_service',
         'locator': {'url': f"/rest/services/kestrel/FeatureServer/{FIXTURES[n]['layerId']}",
                     'layerId': FIXTURES[n]['layerId']}} for n in ('parcels', 'flood_zones', 'shelters')]
    owners = {r['id']: r.get('metadata', {}).get('honua:compositionLayerId') for r in normalized['mapSpec']['layers']}
    assert owners['svc-publish-kestrel/3__outline'] == 'flood_zones' and owners['shelter-labels'] == 'shelters'
    assert owners['honua-basemap-land'] is None
    assert normalized['mapSpec']['metadata']['honua:compositionLayerIds'] == ['parcels', 'flood_zones', 'shelters']
    # Authored paint and layout are untouched; the saved document itself is not modified.
    assert rows(normalized, 'flood_zones')[0]['paint'] == {'fill-color': '#0088ff', 'fill-opacity': 0.35}
    assert BODY['layers'][0]['id'] == 'svc-publish-kestrel/2'
    style = style_from_body(normalized)      # D4: owners resolved through the mapping
    assert (style['floodColor'], style['floodOpacity'], style['labelField']) == ('#0088ff', 0.35, 'name')
    assert blue_see_through(style['floodColor'], style['floodOpacity'])


def test_a_view_less_studio_map_keeps_order_and_visibility_but_no_extent():
    normalized = normalize_studio_authored_map(body(), CATALOG, FIXTURES)
    assert not has_saved_view(normalized)
    assert studio_composition_without_view(normalized) == {
        'orderedLayerIds': ['parcels', 'flood_zones', 'shelters'],
        'visibility': {'parcels': True, 'flood_zones': True, 'shelters': True}, 'view': None}
    normalized['view'] = {'bbox': [-0.2, -0.05, -0.1, 0.0]}
    assert has_saved_view(normalized)


def test_render_colours_derive_from_the_saved_paint():
    colors = authored_paint_colors(normalize_studio_authored_map(body(), CATALOG, FIXTURES))
    assert colors == {'parcels': [180, 83, 27, 115], 'flood_zones': [0, 136, 255, 89], 'shelters': [11, 107, 77, 255]}
    flood = blended_over(colors['flood_zones'], [255, 255, 255, 255])
    assert all(abs(a - b) <= 1 for a, b in zip(flood, [166, 213, 255, 255]))   # the frozen #0088ff/0.35 composite


def _unmapped(value):
    value['layers'][0] = {**value['layers'][0], 'id': 'svc-publish-kestrel/9', 'sourceId': 'svc-publish-kestrel/9'}


def _duplicate(value):
    value['layers'][2] = copy.deepcopy(value['layers'][1])


def _fourth(value):
    value['layers'].append({**value['layers'][0], 'id': 'svc-publish-kestrel/4', 'sourceId': 'svc-publish-kestrel/4'})


def _unknown_owner(value):
    rows(value, 'shelter-labels')[0]['metadata']['honua:compositionLayerId'] = 'svc-publish-kestrel/9'


def _metadata_contradicts_id(value):
    rows(value, 'svc-publish-kestrel/3')[0]['metadata']['honua:compositionLayerId'] = 'svc-publish-kestrel/1'


def _metadata_contradicts_source(value):
    rows(value, 'shelter-labels')[0]['source'] = 'svc-publish-kestrel/3'


def _claims_fixture_name(value):
    rows(value, 'shelter-labels')[0]['id'] = 'shelters'


def _unknown_source(value):
    rows(value, 'shelter-labels')[0].update(source='svc-publish-other/7', metadata={})


def _no_primary_row(value):
    value['mapSpec']['layers'] = [r for r in value['mapSpec']['layers'] if r['id'] != 'svc-publish-kestrel/3']


def _id_is_not_its_source(value):
    value['layers'][1]['id'] = 'flood'


def _unknown_layer_property(value):
    value['layers'][1]['styleRef'] = 'x'


def _listed_unknown(value):
    value['mapSpec']['metadata']['honua:compositionLayerIds'].append('svc-publish-kestrel/9')


@pytest.mark.parametrize('fault, cause', [
    (_unmapped, 'absent from the fixture catalog'), (_duplicate, 'duplicated'), (_fourth, 'missing or additional layer'),
    (_unknown_owner, 'owned by an unknown layer'), (_metadata_contradicts_id, 'ownership contradicts'),
    (_metadata_contradicts_source, 'ownership contradicts'), (_claims_fixture_name, 'claims a fixture name'),
    (_unknown_source, 'unknown source'), (_no_primary_row, 'has no style row'),
    (_id_is_not_its_source, 'not a catalog source'), (_unknown_layer_property, 'unsupported Studio composition layer'),
    (_listed_unknown, 'lists an unknown layer')])
def test_studio_map_binding_fails_closed(fault, cause):
    value = body()
    fault(value)
    with pytest.raises(ProjectionError, match=cause):
        normalize_studio_authored_map(value, CATALOG, FIXTURES)


def test_a_bound_or_unstyled_body_is_not_the_studio_shape():
    assert not is_studio_authored_map({**body(), 'sourceBindings': []})
    assert not is_studio_authored_map({k: v for k, v in body().items() if k != 'mapSpec'})
    with pytest.raises(ProjectionError, match='not a Studio-authored map'):
        normalize_studio_authored_map({**body(), 'sourceBindings': []}, CATALOG, FIXTURES)


@pytest.mark.parametrize('color, opacity', [('#0088ff', 0.35), ('#1f6fd1', 0.5), ('#0000FF', 0.01), ('#4080ff', 0.99)])
def test_blue_and_see_through(color, opacity):
    assert blue_see_through(color, opacity)


@pytest.mark.parametrize('color, opacity', [
    ('#008080', 0.35),           # teal: blue equals green
    ('#00aaa0', 0.35),           # teal-green: green above blue
    ('#808080', 0.35),           # grey
    ('#0088ff', 1), ('#0088ff', 1.0),   # opaque
    ('#0088ff', 0), ('#0088ff', 0.0),   # invisible
    ('#0088ff', True), ('#0088ff', None), ('#0088ff', float('nan')), ('#0088ff', '0.35'),
    ('blue', 0.35), ('#08f', 0.35), (['rgb', 0, 136, 255], 0.35), ({'unsupportedIntermediateColor': 'x'}, 0.35)])
def test_not_blue_or_not_see_through(color, opacity):
    assert not blue_see_through(color, opacity)
