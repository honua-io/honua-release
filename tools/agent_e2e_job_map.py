"""Join saved inline map coordinates to the actual metric job output.

This checks a relationship between two independent native originals. It never
changes the artifact or derives the frozen radius/area/source expectations.
The production Studio SDK passes inline GeoJSON directly to MapLibre.
"""
from __future__ import annotations

import math

from agent_e2e_job_observer import metric_buffer
from agent_e2e_pack import PackError, canonical_digest

EARTH_RADIUS = 6378137.0
MAX_LATITUDE = 85.0511287798066
COORDINATE_TOLERANCE_METRES = 0.001


def require(condition, reason):
    if not condition:
        raise PackError(reason)


def join(saved, artifact, *, layer_id):
    metric_buffer(artifact)  # Requires the producer's actual, known metric frame.
    body = saved['envelope']['body']
    layers = body.get('layers')
    require(isinstance(layers, list), 'saved map has no actual composition')
    selected = [row for row in layers if isinstance(row, dict) and row.get('id') == layer_id]
    require(len(selected) == 1 and selected[0].get('visible') is True and selected[0].get('type') == 'fill',
            'actual buffer must be one visible saved polygon layer')
    layer = selected[0]
    metadata = layer.get('metadata')
    require(isinstance(metadata, dict) and isinstance(metadata.get('honua'), dict),
            'buffer layer lacks its actual inline source declaration')
    source = metadata['honua'].get('source')
    require(isinstance(source, dict) and source.get('type') == 'geojson' and
            not any(key in source for key in ('url', 'urls', 'tiles')) and isinstance(source.get('data'), dict),
            'buffer map requires its actual self-contained inline source')
    document = source['data']
    require(document.get('type') == 'FeatureCollection' and isinstance(document.get('features'), list) and
            len(document['features']) == 1 and 'crs' not in document, 'mapped buffer is not one RFC7946 inline feature')
    declared = document.get('honua:spatialReference')
    require(declared is None or isinstance(declared, dict) and declared.get('schemaVersion') == 'honua.feature-crs/v1' and
            declared.get('state') == 'known' and type(declared.get('srid')) is int and declared['srid'] == 4326,
            'saved inline coordinates declare an incompatible frame')
    feature, original = document['features'][0], artifact['features'][0]
    require(isinstance(feature, dict) and feature.get('type') == 'Feature' and
            canonical_digest(feature.get('properties')) == canonical_digest(original['properties']) and
            ('id' not in original or feature.get('id') == original['id']) and isinstance(feature.get('geometry'), dict) and
            feature['geometry'].get('type') == 'Polygon', 'saved map does not preserve original output feature identity/properties')
    actual, metric = feature['geometry'].get('coordinates'), original['geometry']['coordinates']
    require(isinstance(actual, list) and len(actual) == len(metric) == 1 and
            isinstance(actual[0], list) and len(actual[0]) == len(metric[0]) >= 33 and actual[0][0] == actual[0][-1],
            'mapped buffer ring differs from actual native output')
    for geographic, projected in zip(actual[0], metric[0]):
        require(isinstance(geographic, list) and isinstance(projected, list) and len(geographic) == len(projected) == 2 and
                all(type(value) in (int, float) and math.isfinite(value) for value in geographic+projected),
                'mapped/native buffer contains invalid coordinates')
        lon, lat = geographic
        require(-180 <= lon <= 180 and -MAX_LATITUDE <= lat <= MAX_LATITUDE,
                'inline buffer coordinates are outside the supported geographic frame')
        x = EARTH_RADIUS*math.radians(lon)
        y = EARTH_RADIUS*math.log(math.tan(math.pi/4+math.radians(lat)/2))
        require(math.hypot(x-projected[0], y-projected[1]) <= COORDINATE_TOLERANCE_METRES,
                'saved inline buffer does not map the actual native job geometry')
    return {'layerId': layer_id, 'sourceId': original['properties']['source_id'],
            'nativeArtifactDigest': canonical_digest(artifact), 'savedInlineDigest': canonical_digest(document),
            'nativeSrid': 3857, 'savedSrid': 4326, 'coordinateToleranceMetres': COORDINATE_TOLERANCE_METRES}
