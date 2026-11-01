"""Controller-only source custody for the Studio arc; no publication calls."""
from __future__ import annotations

import copy
import re
from datetime import timedelta
from uuid import UUID

from agent_e2e_local_recorder import ProjectionError
from agent_e2e_local_projections import Context, evaluate, require, require_address

NAMES = ('parcels', 'flood_zones', 'shelters')


def same_connection_guid(recorded, handoff):
    if not isinstance(recorded, str) or not isinstance(handoff, str):
        return False
    try:
        return UUID(recorded) == UUID(handoff)
    except ValueError:
        return False


def validate_sources(handoff):
    connection = handoff.get('connectionId')
    try:
        require(isinstance(connection, str) and str(UUID(connection)) == connection.lower(),
                'invalid handoff connectionId')
    except (ValueError, AttributeError) as exc:
        raise ProjectionError('invalid handoff connectionId') from exc
    tables = handoff.get('tables')
    require(isinstance(tables, dict) and set(tables) == set(NAMES), 'invalid handoff tables')
    for row in tables.values():
        require(isinstance(row, dict) and row.get('schema') == 'honua_data'
                and isinstance(row.get('table'), str) and re.fullmatch(r'[a-z][a-z0-9_]{0,62}', row['table']),
                'invalid handoff table identity')
    require(len({v['table'] for v in tables.values()}) == 3, 'handoff tables collide')


def seed_sources(stack, handoff):
    from agent_e2e_scenario_operations import discover_tables, tables
    rec = stack.extra['recorder']
    inventory = tables(discover_tables(rec, handoff['connectionId'], 'arc-source-tables'))
    for name in NAMES:
        source = handoff['tables'][name]
        require((source['schema'], source['table']) in inventory, 'handoff table is absent from connection')
        rec.table_rows('arc-source-' + name.replace('_', '-') + '-before',
                       source['schema'], source['table'], ['geometry', 'properties'])


def table_content(record):
    require(record.kind == 'sql' and record.principal == 'controller' and record.phase == 'seed'
            and record.status == 200 and isinstance(record.json, list), 'raw table baseline is unavailable')
    features = []
    for row in record.json:
        require(isinstance(row, dict) and isinstance(row.get('properties'), dict)
                and isinstance(row.get('geometry'), dict), 'raw table row is malformed')
        geometry = copy.deepcopy(row['geometry'])
        if geometry.get('type') == 'Polygon':
            rings = []
            for index, ring in enumerate(geometry['coordinates']):
                area = sum(a[0]*b[1]-b[0]*a[1] for a, b in zip(ring, ring[1:]))
                rings.append(list(reversed(ring)) if (index == 0 and area > 0) or (index > 0 and area < 0) else ring)
            geometry['coordinates'] = rings
        identity = row['properties'].get('source_id')
        require(isinstance(identity, str) and identity, 'raw table lacks source identity')
        features.append({'type': 'Feature', 'id': identity, 'properties': row['properties'], 'geometry': geometry})
    require(len({f['id'] for f in features}) == len(features), 'duplicate raw source identity')
    return {'srid': 3857, 'features': sorted(features, key=lambda f: f['id'])}


def published_content(records, prefix, name, when='after'):
    slug = name.replace('_', '-')
    raw, geo = f'{prefix}-src-{slug}-{when}-raw', f'{prefix}-src-{slug}-{when}-geo'
    spec = {'featureServerFeatures': raw}
    if name == 'shelters':
        spec.update(booleanFields=['is_open'], booleanGeo={raw: geo})
    return evaluate(spec, Context(records))


def complete_sources(stack, seeded, body):
    """Join actual publishes by either stage's actor to the exact imported source contents."""
    from agent_e2e_studio_arc import _events, _instant
    from agent_browser_studio import snapshot_sources, observed_fixture_source_ids, normalize_catalog_map
    rec, handoff = stack.extra['recorder'], stack.extra['arcServicesHandoff']
    before, after = _events(rec.records, 'before'), _events(rec.records, 'after')
    require(before.keys() <= after.keys() and all(after[k] == v for k, v in before.items()),
            'Studio audit baseline changed')
    key, user = stack.extra.get('agentPrincipal'), stack.extra.get('consoleUserId')
    require(isinstance(key, str) and key and isinstance(user, str) and user, 'stage-four actor is unavailable')
    subject = user.split('@tenant:', 1)[0]
    actors = {key, 'api-key:' + key, 'apikey:api-key:' + key, user, subject, 'bearer:' + subject}
    start = _instant(rec.records['p08-maps-before'].recorded_at) - timedelta(seconds=1)
    end = max(_instant(rec.records[k].recorded_at) for k in rec.records if k.startswith('studio-audit-after-')) + timedelta(seconds=1)
    catalog_result = rec.mcp_call('arc-map-catalog', 'honua_list_layers', {'offset': 0, 'limit': 500})
    catalog = catalog_result.get('structuredContent', {})
    rows = catalog.get('layers')
    require(isinstance(rows, list) and catalog.get('offset') == 0 and catalog.get('hasMore') is False
            and catalog.get('layerCount') == len(rows), 'arc map source catalog is incomplete')
    map_layers = body.get('layers')
    require(isinstance(map_layers, list) and len(map_layers) == 3
            and all(isinstance(r, dict) for r in map_layers), 'arc map layer inventory differs')
    source_ids = {r.get('sourceId') for r in map_layers}
    mapped = {(r.get('serviceName'), r.get('layerId')) for r in rows if isinstance(r, dict)
              and isinstance(r.get('serviceId'), str)
              and r['serviceId'] + '/' + str(r.get('layerId')) in source_ids}
    if 'sourceBindings' in body:
        bindings = body['sourceBindings']
        require(isinstance(bindings, list) and len(bindings) == 3, 'arc map source bindings differ')
        mapped = set()
        for binding in bindings:
            require(isinstance(binding, dict) and binding.get('sourceId') in source_ids
                    and binding.get('protocol') == 'geoservices_feature_service', 'unbound arc map source')
            locator = binding.get('locator', {})
            match = re.fullmatch(r'/rest/services/([A-Za-z0-9_-]{1,80})/FeatureServer/([0-9]+)', str(locator.get('url')))
            require(match is not None and locator.get('layerId') == int(match[2]), 'invalid arc source locator')
            identity = (match[1], int(match[2]))
            require(sum((r.get('serviceName'), r.get('layerId')) == identity for r in rows) == 1,
                    'arc map source is absent or ambiguous in catalog')
            mapped.add(identity)
    require(len(mapped) == 3, 'arc map source is absent or ambiguous in catalog')
    publications = {}
    for index, action in enumerate(rec.actions()):
        if action.kind != 'tool_call' or action.tool != 'honua_publish_service' or action.status != 'pass':
            continue
        try:
            response = rec.action_result('arc-publish-request-' + str(index), action)
        except ProjectionError:
            continue
        if response.status != 200:
            continue
        try:
            response_body = response.json
        except ProjectionError as exc:
            raise ProjectionError('malformed stage-four publish response: ' + str(exc)) from exc
        require(isinstance(response_body, dict), 'malformed stage-four publish response: body is not an object')
        result = response_body.get('result')
        require(isinstance(result, dict), 'malformed stage-four publish response: result is not an object')
        if result.get('isError') is True:
            continue
        value = result.get('structuredContent')
        require(isinstance(value, dict),
                'malformed stage-four publish response: structuredContent is not an object')
        operation = value.get('operationInstanceId')
        if (result.get('isError', False) is False
                and value.get('status') == 'Completed' and value.get('operationId') == 'service.publish'
                and isinstance(operation, str)):
            publications.setdefault(operation, []).append((action, value))
    candidates, examined = [], 0
    for audit_id, event in after.items():
        if audit_id in before or event.get('actor') not in actors:
            continue
        if (event.get('resourceType') != 'operation_instance' or event.get('action') != 'operation.completed'
                or event.get('outcome') != 'Success' or event.get('details', '').split(';')[:2] !=
                ['operationId=service.publish', 'status=Completed']):
            continue
        require(start <= _instant(event.get('timestamp')) <= end, 'stage-four publish outside customer session')
        examined += 1
        require(examined <= 30, 'too many source publications')
        operation = event.get('resourceId')
        require(isinstance(operation, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', operation), 'invalid publish handle')
        answer = rec.json('arc-publish-candidate-' + str(examined), 'GET', '/api/v1/operations/handles/' + operation)['observation']
        handle = answer.get('body', {}).get('data', {})
        ids = handle.get('resourceIds', {})
        require(answer.get('httpStatus') == 200 and handle.get('operationInstanceId') == operation
                and handle.get('operationId') == 'service.publish' and handle.get('status') == 'Completed'
                and isinstance(ids.get('serviceName'), str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', ids['serviceName'])
                and re.fullmatch(r'0|[1-9][0-9]*', str(ids.get('layerId'))), 'stage-four publish handle differs')
        if (ids['serviceName'], int(ids['layerId'])) in mapped:
            candidates.append(({'service': ids['serviceName'], 'layerId': int(ids['layerId'])}, event))
    require(len(candidates) <= 30, 'too many source publications')
    layers = dict(seeded.state['layers'])
    lineage = {name: {**handoff['layers'][name], 'stage': 3, 'actor': handoff['principal'],
                       'connectionId': handoff['connectionId'], **handoff['tables'][name]} for name in layers}
    for name in NAMES:
        slug = name.replace('_', '-')
        if name not in layers:
            baseline = table_content(rec.records['arc-source-' + slug + '-before'])
            matches = []
            for index, (binding, event) in enumerate(candidates):
                requests = [(action, result) for action, result in publications.get(event['resourceId'], [])
                    if isinstance(action.arguments, dict) and action.arguments.get('dryRun', False) is False
                    and all(action.arguments.get(k) == handoff['tables'][name][k] for k in ('schema', 'table'))
                    and same_connection_guid(action.arguments.get('connectionId'), handoff['connectionId'])
                    and result.get('serviceName') == binding['service']
                    and str(result.get('layerId')) == str(binding['layerId'])]
                if len(requests) != 1:
                    continue
                prefix = 'arc-candidate-' + str(index)
                try:
                    snapshot_sources(rec, prefix, {name: binding}, 'after', variant=seeded.state['variant'])
                    if published_content(rec.records, prefix, name) == baseline:
                        matches.append((binding, event, requests[0][0]))
                except ProjectionError:
                    continue
            require(len(matches) == 1, 'map layer lacks one stage-four publication from the handoff table: ' + name)
            binding, event, publication = matches[0]
            layers[name] = binding
            lineage[name] = {**binding, 'stage': 4, 'actor': event['actor'], 'auditId': event['auditId'],
                             'operationInstanceId': event['resourceId'], 'connectionId': handoff['connectionId'],
                             'actionDigest': publication.digest,
                             **handoff['tables'][name]}
        # Each role binds a real observe-phase GET, including stage-three services.
        rec.json('arc-publish-' + slug, 'GET', '/api/v1/operations/handles/' + lineage[name]['operationInstanceId'])
    require(len({(r['service'], r['layerId']) for r in layers.values()}) == 3, 'arc source identities collide')
    catalog = observed_fixture_source_ids(rec, layers, step='arc-source-catalog')
    if 'mapSpec' not in body and 'sourceBindings' not in body:
        normalize_catalog_map(body, catalog, layers)
    else:
        bindings = body.get('sourceBindings')
        require(isinstance(bindings, list) and len(bindings) == 3, 'map has unbound sources')
        for name, binding in layers.items():
            selected = [r for r in body.get('layers', []) if r.get('id') == name]
            require(len(selected) == 1, 'map layer identity differs')
            matches = [r for r in bindings if r.get('sourceId') == selected[0].get('sourceId')]
            require(len(matches) == 1 and matches[0].get('protocol') == 'geoservices_feature_service'
                    and matches[0].get('locator') == {'url': f"/rest/services/{binding['service']}/FeatureServer/{binding['layerId']}",
                                                    'layerId': binding['layerId']},
                    'map layer was not published by either stage')
    stack.extra['arcServicesLineage'] = lineage
    stack.extra['arcLineageVerified'] = True
    return layers


def source_content(ctx):
    before, after = [], []
    for name in NAMES:
        slug = name.replace('_', '-')
        baseline = ctx.record('arc-source-' + slug + '-before')
        before.append({'resourceId': name, 'content': table_content(baseline)})
        raw = ctx.record('p08-src-' + slug + '-after-raw')
        require(raw.intact() and baseline.intact(), 'source evidence changed')
        request = baseline.request_arguments or {}
        require(request.get('op') == 'table_rows' and request.get('schema') == 'honua_data'
                and request.get('columns') == ['geometry', 'properties'], 'raw source request differs')
        for kind, segment in (('service', 2), ('layer', 4)):
            require_address(raw, {'role': f'arc:{kind}:{name}', 'at': {'pathSegment': segment}}, ctx.roles)
        content = published_content(ctx.records, 'p08', name)
        # Retain the original source baseline for stage-three publications as well.
        old = 'p08-src-' + slug + '-before-raw'
        if old in ctx.records:
            old_record = ctx.record(old)
            require(old_record.phase == 'seed' and old_record.principal == 'controller' and old_record.intact()
                    and old_record.path == raw.path, 'stage-three source baseline differs')
            require(published_content(ctx.records, 'p08', name, 'before') == content,
                    'stage-three source content changed')
        after.append({'resourceId': name, 'content': content})
    return {'profile': 'honua.protected-content/v1', 'before': before, 'after': after}
