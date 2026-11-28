"""Shared fail-closed parsers for publication layer ids and feature-query responses."""
from __future__ import annotations

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
