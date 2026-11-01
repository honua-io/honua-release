"""ARC P10 raw-input profile: the customer agent publishes the imported table."""
from __future__ import annotations

import re
from urllib.parse import quote

from agent_e2e_local_api import Observation, Seeded
from agent_e2e_local_expectations import fixture_catalog, load_roles
from agent_e2e_local_scenarios.P10 import P10


class ArcP10(P10):
    expectation_profile = 'arc'

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
        if self.fault == 'response_lost_after_commit':
            return {'kind': self.fault, 'id': 'arc-p10-publish-lost', 'after': None,
                    'match': {'tool': 'honua_publish_service'}, 'commit': {
                        'pointer': '/result/structuredContent/serviceName',
                        'confirm': {'path': '/api/v1/admin/services/{id}/settings'}}}
        return {'kind': 'http_503_once', 'id': 'arc-p10-query-503',
                'after': {'tool': 'honua_publish_service'}, 'match': {'tool': 'honua_query_features'}}

    def observe(self, stack, seeded, variant, run=None):
        rec, state = stack.extra['recorder'], seeded.state
        if state['variant'] != variant:
            raise ValueError('raw fixture variant differs')
        rec.table_rows('arc-p10-source-after', state['schema'], state['table'], ['geometry', 'properties'])
        after = rec.json('arc-p10-services-after', 'GET', '/api/v1/admin/services/')
        rows = after['observation']['body'].get('data')
        if not isinstance(rows, list):
            raise ValueError('service inventory has an invalid shape')
        publications = []
        for index, action in enumerate(rec.actions()):
            args = action.arguments if isinstance(action.arguments, dict) else {}
            lost = [e for e in (run.fault_events if run is not None else ())
                    if e.get('kind') == 'response_lost_after_commit' and e.get('forwarded') is True
                    and e.get('actionDigest') == action.digest and e.get('tool') == 'honua_publish_service']
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
            layers = body.get('layers')
            if (body.get('serviceName') == service and isinstance(layers, list) and len(layers) == 1
                    and isinstance(layers[0], dict) and layers[0].get('id') == layer):
                state.update(service=service, layerId=layer)
                rec.mcp_call('arc-p10-published-full', 'honua_query_features', {
                    'serviceId': service, 'layerId': layer, 'where': '1=1',
                    'outFields': ['source_id', 'name', 'capacity', 'is_open'],
                    'limit': 1000, 'resultOffset': 0, 'returnGeometry': True, 'outSrid': 3857,
                    'geometryPrecision': -1})
                actions = rec.actions()
                count = sum(a.kind == 'tool_call' and a.tool == 'honua_query_features' for a in actions)
                query = self.selected_query(actions, seeded) if count <= fixture_catalog()['maxP10Queries'] else None
                if query is not None:
                    rec.replay('p10-replay-query', query)
        return [Observation('filter.exact', None, {'projection': 'extract/v1'}),
                Observation('source-content.unchanged', None, {'projection': 'extract/v1'})]


scenario = ArcP10()
