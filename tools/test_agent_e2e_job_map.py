"""Saved inline coordinates join real native artifacts without replacing oracles."""
import copy
import math

import pytest

from agent_e2e_job_map import EARTH_RADIUS, join
from agent_e2e_job_observer import metric_buffer
from agent_e2e_gate import oracle_predicates
from agent_e2e_pack import PackError
from test_agent_e2e_job_observer import native_job
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server


def saved_map(artifact):
    document = copy.deepcopy(artifact)
    document['honua:spatialReference']['srid'] = 4326
    document['features'][0]['geometry']['coordinates'] = [[[
        math.degrees(x/EARTH_RADIUS), math.degrees(math.atan(math.sinh(y/EARTH_RADIUS)))
        ] for x, y in ring] for ring in artifact['features'][0]['geometry']['coordinates']]
    return {'envelope': {'body': {'layers': [{'id': 'buffer', 'sourceId': 'actual-buffer', 'type': 'fill', 'visible': True,
        'metadata': {'honua': {'source': {'type': 'geojson', 'data': document}}}}]}}}


def test_saved_geographic_inline_source_matches_actual_metric_output(native_job):
    artifact = native_job.state.document
    saved = saved_map(artifact)
    before = copy.deepcopy((artifact, saved))
    result = join(saved, artifact, layer_id='buffer')
    assert result['nativeSrid'] == 3857 and result['savedSrid'] == 4326
    assert result['sourceId'] == 'shelter-first'
    assert (artifact, saved) == before


@pytest.mark.parametrize('mutation', ['unprojected', 'wrong-geometry', 'wrong-source', 'hidden', 'duplicate',
    'url', 'extra-remote-selector', 'wrong-crs', 'non-finite', 'boolean-coordinate', 'missing-feature', 'legacy-crs'])
def test_layer_name_alone_cannot_prove_saved_mapped_output(native_job, mutation):
    artifact = native_job.state.document
    saved = saved_map(artifact)
    layer = saved['envelope']['body']['layers'][0]
    source = layer['metadata']['honua']['source']
    feature = source['data']['features'][0]
    if mutation == 'unprojected': feature['geometry'] = copy.deepcopy(artifact['features'][0]['geometry'])
    elif mutation == 'wrong-geometry': feature['geometry']['coordinates'][0][1][0] += 0.0001
    elif mutation == 'wrong-source': feature['properties']['source_id'] = 'second-shelter'
    elif mutation == 'hidden': layer['visible'] = False
    elif mutation == 'duplicate': saved['envelope']['body']['layers'].append(copy.deepcopy(layer))
    elif mutation == 'url': source['data'] = 'https://example.invalid/claimed-buffer.json'
    elif mutation == 'extra-remote-selector': source['url'] = 'https://example.invalid/other'
    elif mutation == 'wrong-crs': source['data']['honua:spatialReference']['srid'] = 3857
    elif mutation == 'non-finite': feature['geometry']['coordinates'][0][1][0] = float('inf')
    elif mutation == 'boolean-coordinate': feature['geometry']['coordinates'][0][1][0] = True
    elif mutation == 'missing-feature': source['data']['features'] = []
    else: source['data']['crs'] = {'type': 'name', 'properties': {'name': 'EPSG:3857'}}
    with pytest.raises(PackError):
        join(saved, artifact, layer_id='buffer')


def test_consistent_copy_of_wrong_native_geometry_still_fails_frozen_metric_oracle(native_job):
    artifact = native_job.state.document
    artifact['features'][0]['geometry']['coordinates'][0][1][0] += 50
    join(saved_map(artifact), artifact, layer_id='buffer')
    with pytest.raises(ValueError):
        oracle_predicates.verify('buffer.metric', metric_buffer(artifact), {'sourceId': 'shelter-first',
            'center3857': [0, 0], 'radiusMetres': 500, 'radialToleranceMetres': 0.01,
            'expectedArea': math.pi*500*500, 'areaRelativeTolerance': 0.01})
