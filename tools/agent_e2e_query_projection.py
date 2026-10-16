"""Fixed conversions of retained protocol responses, never oracle substitutions.

Recipes select a named converter, not Python expressions or literal results.
Keep this module controller-only. It neither queries nor mutates a deployment.
"""
from __future__ import annotations

import copy
import math

from agent_e2e_pack import PackError


def require(condition, message):
    if not condition:
        raise PackError(message)


def feature_server_points(value):
    """Normalize a complete, unquantized 2D FeatureServer point query.

    source_id is the corpus identity. Only the advertised numeric object-ID
    attribute is removed; every other attribute and coordinate remains observed.
    No ordering, filtering, rounding, null repair or expected-value insertion.
    """
    require(isinstance(value, dict) and 'error' not in value, 'missing successful FeatureServer query')
    require(value.get('geometryType') == 'esriGeometryPoint', 'query is not a point feature set')
    require(value.get('exceededTransferLimit') is False, 'query is incomplete or lacks transfer-limit observation')
    require(value.get('transform') is None and value.get('hasZ', False) is False
            and value.get('hasM', False) is False, 'quantized or higher-dimensional query is unsupported')
    reference = value.get('spatialReference')
    require(isinstance(reference, dict) and type(reference.get('wkid')) is int and reference['wkid'] > 0,
            'query lacks observed coordinate reference system')
    require(set(reference) <= {'wkid', 'latestWkid'} and reference.get('latestWkid', reference['wkid']) == reference['wkid'],
            'ambiguous query coordinate reference system')
    oid = value.get('objectIdFieldName')
    require(isinstance(oid, str) and bool(oid.strip()) and oid != 'source_id', 'invalid query object-ID field')
    fields = value.get('fields')
    require(isinstance(fields, list) and all(isinstance(field, dict) for field in fields), 'missing query field metadata')
    oid_fields = [field for field in fields if field.get('type') == 'esriFieldTypeOID']
    require(len(oid_fields) == 1 and oid_fields[0].get('name') == oid, 'query object-ID metadata differs')
    names = [field.get('name') for field in fields]
    require(all(isinstance(name, str) and name for name in names) and len(set(names)) == len(names)
            and 'source_id' in names, 'invalid query field inventory')
    rows = value.get('features')
    require(isinstance(rows, list), 'missing query features')
    features, source_ids, object_ids = [], set(), set()
    for row in rows:
        require(isinstance(row, dict) and set(row) == {'attributes', 'geometry'}, 'invalid query feature')
        attributes, geometry = row['attributes'], row['geometry']
        require(isinstance(attributes, dict) and set(attributes) == set(names), 'query attribute inventory differs')
        source_id = attributes['source_id']
        require(isinstance(source_id, str) and bool(source_id) and source_id not in source_ids,
                'missing or duplicate corpus source identity')
        require(type(attributes[oid]) is int and attributes[oid] not in object_ids, 'missing or duplicate query object identity')
        require(isinstance(geometry, dict) and set(geometry) in ({'x', 'y'}, {'x', 'y', 'spatialReference'}),
                'query point has missing or unsupported ordinates')
        require('spatialReference' not in geometry or geometry['spatialReference'] == reference,
                'feature coordinate reference system differs')
        coordinates = [geometry['x'], geometry['y']]
        require(all(type(number) in (int, float) and math.isfinite(number) for number in coordinates),
                'query point has invalid coordinates')
        properties = copy.deepcopy(attributes)
        del properties[oid]
        features.append({'type': 'Feature', 'id': source_id, 'properties': properties,
                         'geometry': {'type': 'Point', 'coordinates': coordinates}})
        source_ids.add(source_id)
        object_ids.add(attributes[oid])
    return {'srid': reference['wkid'], 'features': features}


def normalize(name, value):
    require(name == 'feature-server-points/v1', 'unreviewed query normalizer')
    return feature_server_points(value)
