"""Shared fail-closed parsers for publication layer ids and feature-query responses."""
from __future__ import annotations

import math
import re

from agent_e2e_local_recorder import ProjectionError

HTTP_OK = 200


def _require(condition, reason):
    if not condition:
        raise ProjectionError(reason)


def publication_resource_layer(ids):
    """Published layer id in service.publish resourceIds (storageLayerId or legacy layerId); fail on conflict."""
    _require(isinstance(ids, dict), 'publication handle lacks its resource ids')
    has_storage, has_layer = 'storageLayerId' in ids, 'layerId' in ids
    if has_storage and has_layer:
        _require(str(ids['storageLayerId']) == str(ids['layerId']), 'conflicting publication layer ids')
    raw = ids['storageLayerId'] if has_storage else ids.get('layerId')
    _require(isinstance(raw, (str, int)) and not isinstance(raw, bool)
             and re.fullmatch(r'0|[1-9][0-9]*', str(raw)) is not None, 'publication handle lacks its resource ids')
    return raw


def published_layer_value(value):
    """Read storageLayerId or legacy layerId from a publication structuredContent; None on conflict."""
    if not isinstance(value, dict):
        return None
    has_storage, has_layer = 'storageLayerId' in value, 'layerId' in value
    if has_storage and has_layer and str(value['storageLayerId']) != str(value['layerId']):
        return None
    return value['storageLayerId'] if has_storage else value.get('layerId')


def query_layer_id(mapping):
    """Layer selector from honua_query_features arguments or structuredContent (serviceLayerIndex or layerId)."""
    if not isinstance(mapping, dict):
        return None
    has_index, has_layer = 'serviceLayerIndex' in mapping, 'layerId' in mapping
    if not (has_index or has_layer):
        return None
    index_val = mapping.get('serviceLayerIndex') if has_index else None
    layer_val = mapping.get('layerId') if has_layer else None
    if not (_valid_layer_scalar(has_index, index_val) and _valid_layer_scalar(has_layer, layer_val)):
        return None
    if has_index and has_layer and str(index_val) != str(layer_val):
        return None
    return index_val if has_index else layer_val


def _valid_layer_scalar(present, candidate):
    if not present:
        return True
    return isinstance(candidate, (int, str)) and not isinstance(candidate, bool) and str(candidate) != ''


def query_features(value):
    """Extract (features, attr_key) from honua_query_features structuredContent (flat features or legacy geojson)."""
    _require(isinstance(value, dict), 'feature query lacks features')
    has_geo, has_flat = 'geojson' in value, 'features' in value
    _require(has_geo != has_flat, 'feature query lacks features')
    if has_geo:
        collection = value.get('geojson')
        _require(isinstance(collection, dict) and collection.get('type') == 'FeatureCollection'
                 and isinstance(collection.get('features'), list), 'feature query lacks features')
        return collection['features'], 'properties'
    features = value.get('features')
    _require(isinstance(features, list), 'feature query lacks features')
    return features, 'attributes'


def _normalize_coord_node(node):
    if isinstance(node, list):
        return [_normalize_coord_node(item) for item in node]
    if type(node) is float and math.isfinite(node) and node.is_integer():
        return int(node)
    return node


def _normalize_geometry(geometry):
    if 'coordinates' not in geometry:
        return geometry
    coords = _normalize_coord_node(geometry['coordinates'])
    if (geometry.get('type') == 'MultiPolygon' and isinstance(coords, list)
            and len(coords) == 1 and isinstance(coords[0], list)):
        return {**geometry, 'type': 'Polygon', 'coordinates': coords[0]}
    return {**geometry, 'coordinates': coords}


def service_list_items(body):
    """Extract the complete paged items list from GET /api/v1/admin/services/."""
    data = body.get('data') if isinstance(body, dict) else None
    items = data.get('items') if isinstance(data, dict) else None
    total = data.get('total') if isinstance(data, dict) else None
    if not isinstance(items, list) or type(total) is not int or total != len(items):
        raise ValueError('service inventory has an invalid or incomplete paged shape')
    return items


def _normalize_query_feature(feature, attr_key):
    _require(isinstance(feature, dict) and isinstance(feature.get(attr_key), dict)
             and isinstance(feature.get('geometry'), dict), 'feature query item is malformed')
    if attr_key == 'properties' and 'type' in feature:
        _require(feature['type'] == 'Feature', 'feature query item has unexpected type')
    return {'type': 'Feature', 'geometry': _normalize_geometry(feature['geometry']),
            'properties': dict(feature[attr_key])}


def _query_structured_content(record):
    _require(record.kind == 'mcp' and record.status == HTTP_OK and isinstance(record.json, dict),
             'feature query response missing or failed')
    result = record.json.get('result')
    _require(isinstance(result, dict) and result.get('isError', False) is False, 'feature query failed')
    value = result.get('structuredContent')
    _require(isinstance(value, dict), 'feature query lacks structuredContent')
    return value


def normalize_query_features(record):
    """Extract normalized GeoJSON-style features from flat or legacy honua_query_features output."""
    features, attr_key = query_features(_query_structured_content(record))
    return [_normalize_query_feature(feature, attr_key) for feature in features]


def query_service_id(record, service):
    """Verify honua_query_features echoed the queried service (or svc-publish-<service>) and return service."""
    from agent_e2e_local_projections import echoes_service
    value = _query_structured_content(record)
    _require(echoes_service(value.get('serviceId'), service), 'feature query echoed a foreign service')
    return str(service)


def _fs_shape(kind, geometry, reference):
    if kind == 'esriGeometryPoint':
        _require(set(geometry) in ({'x', 'y'}, {'x', 'y', 'spatialReference'})
                 and ('spatialReference' not in geometry or geometry['spatialReference'] == reference),
                 'query point has missing or unsupported ordinates')
        coords = [geometry['x'], geometry['y']]
        _require(all(type(n) in (int, float) and math.isfinite(n) for n in coords), 'query point has invalid coordinates')
        return {'type': 'Point', 'coordinates': coords}
    if kind == 'esriGeometryPolyline':
        paths = geometry['paths']
        return {'type': 'LineString', 'coordinates': paths[0]} if len(paths) == 1 else {
            'type': 'MultiLineString', 'coordinates': paths}
    return {'type': 'Polygon', 'coordinates': geometry['rings']}


def _validate_fs_envelope(value):
    kind = value.get('geometryType')
    _require(kind in ('esriGeometryPoint', 'esriGeometryPolyline', 'esriGeometryPolygon'), 'unsupported geometry type')
    _require(value.get('exceededTransferLimit') is False and value.get('transform') is None
             and value.get('hasZ', False) is False and value.get('hasM', False) is False,
             'query is incomplete, quantized or not 2D')
    reference = value.get('spatialReference')
    _require(isinstance(reference, dict) and type(reference.get('wkid')) is int and reference['wkid'] > 0
             and set(reference) <= {'wkid', 'latestWkid'}
             and reference.get('latestWkid', reference['wkid']) == reference['wkid'],
             'query lacks its coordinate system')
    oid, fields = value.get('objectIdFieldName'), value.get('fields')
    _require(isinstance(oid, str) and bool(oid.strip()) and oid != 'source_id' and isinstance(fields, list),
             'invalid query fields')
    oid_fields = [f for f in fields if isinstance(f, dict) and f.get('type') == 'esriFieldTypeOID']
    if oid_fields:
        _require(len(oid_fields) == 1 and oid_fields[0].get('name') == oid, 'query object-ID metadata differs')
    names = [f.get('name') for f in fields if isinstance(f, dict)]
    _require(all(isinstance(name, str) and name for name in names) and oid in names and 'source_id' in names
             and len(set(names)) == len(names), 'invalid query field inventory')
    return reference, oid, names


def _normalize_fs_rows(value, oid, names):
    kind, reference, out, seen, object_ids = value['geometryType'], value['spatialReference'], [], set(), set()
    for row in value.get('features') or []:
        _require(isinstance(row, dict) and set(row) == {'attributes', 'geometry'}, 'invalid query feature')
        attributes, geometry = dict(row['attributes']), row['geometry']
        _require(set(attributes) == set(names) and isinstance(geometry, dict), 'query attribute inventory differs')
        source_id = attributes['source_id']
        _require(isinstance(source_id, str) and source_id and source_id not in seen, 'missing or duplicate source id')
        _require(type(attributes[oid]) is int and attributes[oid] not in object_ids,
                 'missing or duplicate query object identity')
        seen.add(source_id)
        object_ids.add(attributes[oid])
        del attributes[oid]
        out.append({'type': 'Feature', 'id': source_id, 'properties': attributes,
                    'geometry': _fs_shape(kind, geometry, reference)})
    out.sort(key=lambda feature: feature['properties']['source_id'])
    return out


def normalize_feature_server_features(value):
    """Normalize a complete 2D FeatureServer query, enforcing unique integer OIDs and valid CRS/coords."""
    _require(isinstance(value, dict) and 'error' not in value, 'missing successful FeatureServer query')
    reference, oid, names = _validate_fs_envelope(value)
    return {'srid': reference['wkid'], 'features': _normalize_fs_rows(value, oid, names)}


def restore_boolean_source(spec, ctx, named):
    """Cross-check FeatureServer integer booleans against a same-resource controller GeoJSON read."""
    from urllib.parse import urlsplit
    from agent_e2e_local_projections import restore_feature_booleans
    is_points = 'featureServerPoints' in spec
    name = named(spec['featureServerPoints' if is_points else 'featureServerFeatures'])
    record = ctx.record(name)
    mapping = spec['booleanGeo']
    _require(isinstance(mapping, dict) and name in mapping and isinstance(mapping[name], str),
             'boolean GeoJSON read is missing')
    geo = ctx.record(mapping[name])
    _require(record.status == HTTP_OK and geo.status == HTTP_OK and record.phase == geo.phase
             and record.method == geo.method == 'GET' and record.principal == geo.principal == 'controller'
             and urlsplit(record.path).path == urlsplit(geo.path).path,
             'boolean GeoJSON does not address the same source')
    if is_points:
        from agent_e2e_query_projection import feature_server_points
        normalized = feature_server_points(record.json)
        normalized['features'].sort(key=lambda feature: feature['properties']['source_id'])
    else:
        normalized = normalize_feature_server_features(record.json)
    return restore_feature_booleans(normalized, geo.json, spec['booleanFields'])


def source_oid_map(record, service, layer):
    """1-to-1 objectId -> source_id map from an observe-phase controller FeatureServer read on the same layer."""
    from agent_e2e_local_projections import address_value
    _require(record.kind == 'http' and record.method == 'GET' and record.principal == 'controller'
             and record.phase == 'observe' and record.status == HTTP_OK and isinstance(record.json, dict),
             'oid source read failed')
    _require(address_value(record, {'pathSegment': 2}) == str(service)
             and address_value(record, {'pathSegment': 4}) == str(layer),
             'oid source addresses another service or layer')
    body = record.json
    oid_field, features = body.get('objectIdFieldName'), body.get('features')
    _require(body.get('exceededTransferLimit') is False and isinstance(oid_field, str) and oid_field
             and isinstance(features, list), 'oid source is incomplete')
    return oid_field, _build_oid_map(features, oid_field)


def _build_oid_map(features, oid_field):
    mapping, seen_ids = {}, set()
    for feature in features:
        attrs = feature.get('attributes') if isinstance(feature, dict) else None
        oid = attrs.get(oid_field) if isinstance(attrs, dict) else None
        source_id = attrs.get('source_id') if isinstance(attrs, dict) else None
        _require(type(oid) is int and oid not in mapping
                 and isinstance(source_id, str) and source_id and source_id not in seen_ids,
                 'oid source lacks unique objectId to source_id mapping')
        mapping[oid] = source_id
        seen_ids.add(source_id)
    return mapping


def _feature_oid(feature, props, oid_field):
    top = feature.get('id') if isinstance(feature, dict) else None
    attr = props.get(oid_field) if isinstance(props, dict) else None
    if top is not None and attr is not None:
        _require(type(top) is int and type(attr) is int and top == attr, 'feature oid mismatch')
        return top
    oid = top if top is not None else attr
    _require(type(oid) is int, 'feature lacks integer oid')
    return oid


def _cross_check_oids(feature_ids, field, oid_source):
    oid_field, oid_map = oid_source
    for feature, source_id in feature_ids:
        props = feature.get(field) if isinstance(feature, dict) else None
        top = feature.get('id') if isinstance(feature, dict) else None
        if field == 'properties' and isinstance(top, str):
            _require(top == source_id, 'feature id disagrees with source_id')
            top = None
        attr = props.get(oid_field) if isinstance(props, dict) else None
        if top is not None or attr is not None:
            oid = _feature_oid({'id': top}, props, oid_field)
            _require(oid_map.get(oid) == source_id, 'feature query oid disagrees with source_id')


def extract_query_source_ids(features, field, oid_source=None):
    """Return distinct source_id values directly from feature attributes/properties, or via controller oid_source."""
    direct = [(f.get(field) or {}).get('source_id') if isinstance(f, dict) and isinstance(f.get(field), dict) else None
              for f in features]
    if any(i is not None for i in direct):
        _require(all(isinstance(i, str) and i for i in direct) and len(set(direct)) == len(direct),
                 'feature query lacks distinct source ids')
        if oid_source is not None:
            _cross_check_oids(zip(features, direct), field, oid_source)
        return direct
    if not features:
        return []
    _require(oid_source is not None, 'feature query lacks distinct source ids')
    return _map_oids_to_source_ids(features, field, oid_source)


def _map_oids_to_source_ids(features, field, oid_source):
    oid_field, oid_map = oid_source
    ids = []
    for feature in features:
        props = feature.get(field) if isinstance(feature, dict) else None
        oid = _feature_oid(feature, props, oid_field)
        _require(oid in oid_map, 'feature query oid absent from source layer')
        ids.append(oid_map[oid])
    _require(len(set(ids)) == len(ids), 'feature query lacks distinct source ids')
    return ids


def resolve_arc_publication_role(name, binding, record):
    """Resolve an arc:{service,layer}:<layer> role from a controller publication handle."""
    from agent_e2e_local_projections import publication_handle_identity
    parts = name.split(':')
    _require(len(parts) == 3 and parts[1] in ('service', 'layer')
             and parts[2] in ('parcels', 'flood_zones', 'shelters')
             and binding.step == 'arc-publish-' + parts[2].replace('_', '-')
             and record.phase == 'observe' and record.principal == 'controller'
             and record.kind == 'http' and record.method == 'GET' and record.intact(),
             'invalid arc publication role')
    handle = record.json.get('data', {})
    ids = handle.get('resourceIds', {}) if isinstance(handle, dict) else {}
    identity = _safe_publication_identity(handle)
    _require(identity is not None
             and record.path == '/api/v1/operations/handles/' + str(handle.get('operationInstanceId')),
             'arc role lacks a completed controller publication handle')
    return _select_arc_role_value(parts[1], binding.pointer, (identity, ids))


def _safe_publication_identity(handle):
    from agent_e2e_local_projections import publication_handle_identity
    try:
        return publication_handle_identity(handle)
    except ProjectionError:
        return None


def _select_arc_role_value(kind, pointer, identity_and_ids):
    identity, ids = identity_and_ids
    if kind == 'service':
        _require(pointer == '/data/resourceIds/serviceName', 'invalid arc role pointer')
        if identity['operationId'] == 'service.publish':
            _require(ids.get('serviceName') is not None, 'invalid arc role pointer')
            return ids['serviceName']
        return identity['service']
    _require(pointer in ('/data/resourceIds/layerId', '/data/resourceIds/storageLayerId'),
             'invalid arc role pointer')
    if identity['operationId'] == 'service.publish':
        return str(publication_resource_layer(ids))
    return str(identity['layerId'])
