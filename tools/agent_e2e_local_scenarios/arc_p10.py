"""ARC P10 raw-input profile: the customer agent publishes the imported table."""
from __future__ import annotations

import re
from urllib.parse import quote

from agent_e2e_local_api import Observation, Seeded
from agent_e2e_local_expectations import fixture_catalog, load_roles
from agent_e2e_local_scenarios.P10 import P10


def p10_response_ids(message, arguments=None):
    """The source_id set one honua_query_features response returned, or None unless it is a complete, successful list
    (no error, one page from offset and cursor zero, not truncated, within its own limit, distinct string ids, count
    matching the features)."""
    arguments = arguments if isinstance(arguments, dict) else {}
    if arguments.get('resultOffset', 0) != 0 or str(arguments.get('cursor', '0')) != '0':
        return None
    result = message.get('result') if isinstance(message, dict) else None
    value = result.get('structuredContent') if isinstance(result, dict) else None
    if (not isinstance(result, dict) or result.get('isError', False) is not False or not isinstance(value, dict)
            or value.get('exceededTransferLimit') is not False or value.get('resultOffset') != 0):
        return None
    collection = value.get('geojson')
    features = collection.get('features') if isinstance(collection, dict) else None
    if not isinstance(features, list) or type(value.get('returnedCount')) is not int \
            or value['returnedCount'] != len(features):
        return None
    ids = [f.get('properties', {}).get('source_id') if isinstance(f, dict) else None for f in features]
    if not all(isinstance(i, str) and i for i in ids) or len(set(ids)) != len(ids):
        return None
    limit = arguments.get('limit')
    if limit is not None and (type(limit) is not int or len(ids) >= limit):
        return None                                        # a page that fills its limit may hide more rows
    return frozenset(ids)


class ArcP10(P10):
    expectation_profile = 'arc'

    @staticmethod
    def selected_query(actions, seeded, reported=None, response_ids=None, qualifies=None):
        """PREREG A24 (retires the last-query rule): the agent's query that witnesses the list it reports. Every passing
        honua_query_features call on the published service and layer (the service may be addressed by the metadata id
        the server gives it, agent_e2e_local_projections.echoes_service) whose own recorded response returned exactly
        the reported source_id set, complete, is eligible; the earliest one that also ``qualifies`` (its where-clause
        expresses the selection and translates to exactly those source rows) is chosen for audit, else the earliest
        eligible one (the projection then fails or leaves it undetermined). Later verification reads never decide.
        ``reported`` is the frozenset from the closing checklist (ai_arc_safety.parse_reported_ids)."""
        from agent_e2e_local_projections import echoes_service
        if reported is None or response_ids is None:
            return None
        eligible = []
        for action in (a for a in actions if a.kind == 'tool_call' and a.tool == 'honua_query_features'):
            arguments = action.arguments if isinstance(action.arguments, dict) else {}
            if (action.status == 'pass' and echoes_service(arguments.get('serviceId'), seeded.state['service'])
                    and str(arguments.get('layerId')) == str(seeded.state['layerId'])
                    and response_ids(action) == reported):
                eligible.append(action)
        for action in eligible:
            if qualifies is not None and qualifies(action):
                return action
        return eligible[0] if eligible else None

    def __init__(self, fault='http_503_once'):
        if fault not in ('http_503_once', 'response_lost_after_commit'):
            raise ValueError('unsupported P10 fault')
        self.fault = fault

    def seed(self, stack, variant):
        from agent_e2e_local_scenarios.w2g3_common import town_setup
        rec = stack.extra['recorder']
        connection = stack.extra.get('connectionId') or stack.extra['ensureConnection']()
        stack.extra['connectionId'] = connection
        namespace = 'arc10_' + stack.compose_project.replace('-', '')[-16:]
        town = town_setup(rec, variant).prepare(variant=variant, namespace=namespace,
                                                starting_state='imported-town', connection_id=connection)
        source = town['layers']['shelters']
        if source['schema'] != 'honua_data' or not re.fullmatch(r'[a-z][a-z0-9_]{0,62}', source['table']):
            raise ValueError('raw shelter table identity is outside the reviewed fixture schema')
        rec.json('arc-p10-services-before', 'GET', '/api/v1/admin/services/')
        rec.table_rows('arc-p10-source-before', source['schema'], source['table'], ['geometry', 'properties'])
        return Seeded({'variant': variant, 'schema': source['schema'], 'table': source['table'],
                       'connection': connection})

    def roles(self, stack, seeded, variant, run=None):
        return load_roles('P10', 'arc')

    def fault_plan(self, variant):
        from agent_e2e_local_faults import PUBLISH_TRIGGER
        if self.fault == 'response_lost_after_commit':
            # PREREG A23 (rehearsal of record 41699972562, A1:002): the lost reply arms on either reviewed publication
            # tool, so an arm that publishes through honua_admin_layer_publish meets the fault like any other.
            return {'kind': self.fault, 'id': 'arc-p10-publish-lost', 'after': None,
                    'match': dict(PUBLISH_TRIGGER, tools=list(PUBLISH_TRIGGER['tools'])), 'commit': {
                        'pointer': '/result/structuredContent/serviceName',
                        'confirm': {'path': '/api/v1/admin/services/{id}/settings'}}}
        # Either reviewed publication tool arms the 503 (a dry run does not); see agent_e2e_local_faults.
        return {'kind': 'http_503_once', 'id': 'arc-p10-query-503',
                'after': dict(PUBLISH_TRIGGER, tools=list(PUBLISH_TRIGGER['tools'])),
                'match': {'tool': 'honua_query_features'}}

    def observe(self, stack, seeded, variant, run=None):
        rec, state = stack.extra['recorder'], seeded.state
        if state['variant'] != variant:
            raise ValueError('raw fixture variant differs')
        source = rec.table_rows('arc-p10-source-after', state['schema'], state['table'], ['geometry', 'properties'])
        source_props = [r['properties'] for r in (source if isinstance(source, list) else [])
                        if isinstance(r, dict) and isinstance(r.get('properties'), dict)]
        after = rec.json('arc-p10-services-after', 'GET', '/api/v1/admin/services/')
        rows = after['observation']['body'].get('data')
        if not isinstance(rows, list):
            raise ValueError('service inventory has an invalid shape')
        publications = []
        for index, action in enumerate(rec.actions()):
            args = action.arguments if isinstance(action.arguments, dict) else {}
            lost = [e for e in (run.fault_events if run is not None else ())
                    if e.get('kind') == 'response_lost_after_commit' and e.get('forwarded') is True
                    and e.get('actionDigest') == action.digest
                    and e.get('tool') in ('honua_publish_service', 'honua_admin_layer_publish')]
            if (action.kind == 'tool_call' and action.tool == 'honua_admin_layer_publish'
                    and (action.status == 'pass' or (action.status == 'fail' and len(lost) == 1))
                    and args.get('schema') == state['schema'] and args.get('table') == state['table']
                    and args.get('connectionId') == state['connection']):
                # The reviewed alternative publication tool (release session 2026-11-02): the same source binding,
                # and only a completed, non-dry-run publication (agent_e2e_local_projections.admin_layer_publication).
                from agent_e2e_local_projections import admin_layer_publication
                import json as _json
                response = rec.action_result('arc-p10-publication-' + str(index), action)
                # A lost reply (A23): the model saw a 503, but the committed upstream reply is the one recorded.
                service = admin_layer_publication({'kind': action.kind, 'status': 'pass', 'arguments': args,
                                                   'json': response.json})
                if (service is not None and sum(row.get('serviceName') == service for row in rows) == 1
                        and (not lost or str(lost[0].get('committedResourceId')) == service)):
                    embedded = _json.loads(response.json['result']['structuredContent']['details']['response'])
                    publications.append((service, int(embedded['data']['layerId'])))
                continue
            if (action.kind != 'tool_call' or (action.status != 'pass' and not (action.status == 'fail' and len(lost) == 1))
                    or action.tool != 'honua_publish_service'
                    or args.get('schema') != state['schema'] or args.get('table') != state['table']
                    or args.get('connectionId') != state['connection']):
                continue
            response = rec.action_result('arc-p10-publication-' + str(index), action)
            result = response.json.get('result', {})
            value = result.get('structuredContent', {})
            service, layer = value.get('serviceName'), value.get('layerId')
            if (result.get('isError', False) is False and value.get('status') == 'Completed'
                    and value.get('operationId') == 'service.publish'
                    and (not lost or str(lost[0].get('committedResourceId')) == str(service))
                    and isinstance(service, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', service)
                    and str(layer).isdigit() and sum(row.get('serviceName') == service for row in rows) == 1):
                publications.append((service, int(layer)))
        if len(publications) == 1:
            service, layer = publications[0]
            meta = rec.json('arc-p10-service-meta', 'GET', '/rest/services/' + quote(service, safe='') + '/FeatureServer?f=json')
            body = meta['observation']['body']
            # Founder decision 2026-11-02: the admin catalog's view of the service (its access policy and the
            # connection's published layers), read without the data plane, so a service the agent role-restricted
            # stays observable and no principal bypasses the policy (agent_e2e_local_projections ARC_P10_*).
            rec.json('arc-p10-service-settings', 'GET', '/api/v1/admin/services/' + quote(service, safe='') + '/settings')
            # The connection's published layers for THIS service (the endpoint defaults serviceName to "default";
            # rehearsal r5 DEV: without it a honua_publish_service layer is not listed).
            rec.json('arc-p10-catalog-layers', 'GET',
                     '/api/v1/admin/connections/' + quote(str(state['connection']), safe='') + '/layers?serviceName='
                     + quote(service, safe=''))
            layers = body.get('layers')
            readable = (body.get('serviceName') == service and isinstance(layers, list) and len(layers) == 1
                        and isinstance(layers[0], dict) and layers[0].get('id') == layer)
            denied = isinstance(body.get('error'), dict) and body['error'].get('code') == 403
            if readable or denied:
                state.update(service=service, layerId=layer)
                rec.mcp_call('arc-p10-published-full', 'honua_query_features', {
                    'serviceId': service, 'layerId': layer, 'where': '1=1',
                    'outFields': ['source_id', 'name', 'capacity', 'is_open'],
                    'limit': 1000, 'resultOffset': 0, 'returnGeometry': True, 'outSrid': 3857,
                    'geometryPrecision': -1})
                actions = rec.actions()
                count = sum(a.kind == 'tool_call' and a.tool == 'honua_query_features' for a in actions)
                from ai_arc_safety import parse_reported_ids
                answers = tuple(getattr(run, 'final_answers', ()) or ()) if run is not None else ()
                status, reported = parse_reported_ids(answers[-1] if answers else None, 'P10')
                seen = {}

                def response_ids(action):
                    # The call's own recorded response (sealed, digest-checked), registered once per call under a
                    # step named for its own position (Sol a22-24: never reuse a step for repeated identical calls).
                    key = (action.session, action.sequence)
                    if key not in seen:
                        step = 'arc-p10-query-' + str(action.session) + '-' + str(action.sequence)
                        seen[key] = p10_response_ids(rec.action_result(step, action).json, action.arguments)
                    return seen[key]

                def qualifies(action):
                    from agent_e2e_local_projections import IDENTITY_FIELDS, Unverifiable, where_fields, where_predicate
                    where = (action.arguments or {}).get('where') if isinstance(action.arguments, dict) else None
                    named = where_fields(where)
                    if not named or named & IDENTITY_FIELDS or not source_props:
                        return False
                    try:
                        # Evaluation-time checks (mixed types) raise too (Sol a22-24b): inside the boundary.
                        keep = where_predicate(where, set().union(*(r.keys() for r in source_props)))
                        return frozenset(r.get('source_id') for r in source_props if keep(r)) == reported
                    except Unverifiable:
                        return False
                query = (self.selected_query(actions, seeded, reported if status == 'ok' else None, response_ids,
                                             qualifies)
                         if count <= fixture_catalog()['maxP10Queries'] else None)
                if query is not None:
                    rec.replay('p10-replay-query', query)
                    # The proving query's own recorded response: the list the agent reported (scored on the
                    # restricted-service path, where the controller replay is refused by the policy).
                    rec.action_result('arc-p10-final-query', query)
        return [Observation('filter.exact', None, {'projection': 'extract/v1'}),
                Observation('source-content.unchanged', None, {'projection': 'extract/v1'})]


    def handoff(self, stack, seeded, variant, run=None):
        """The facts the customer's final answer is checked against. On the restricted-service path (the controller
        replay was refused by the policy the agent set) they come from the agent's own captured final query
        response, the same list the restricted filter.exact projection validates (Codex review on #1230)."""
        from agent_e2e_local_projections import data_plane_denied
        records = stack.extra['recorder'].records
        if (data_plane_denied(records.get('p10-replay-query')) and 'arc-p10-final-query' in records
                and records['arc-p10-final-query'].origin == 'session-result'):
            features = {'body': 'arc-p10-final-query', 'pointer': '/result/structuredContent/geojson/features'}
            count = {'count': features}
            return {'object': {'resources': {'list': []},
                               'facts': {'object': {'counts': {'object': {'features': count, 'points': count}},
                                                    'names': {'map': features, 'to': {'item': '/properties/name'}}}}}}
        return super().handoff(stack, seeded, variant, run=run)

scenario = ArcP10()
