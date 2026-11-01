"""Observe the agent's metric buffer, published result and saved map.

Viewport containment uses the saved view's declared CRS and fixture buffer.
Typed attribute reads require honua-server #6815.
"""
from pathlib import Path
import re
from urllib.parse import quote, urlencode

from agent_e2e_local_api import Observation, RoleBinding, Scenario, Seeded

ROOT = Path(__file__).resolve().parents[2]


class P11(Scenario):
    id = 'P11'
    supported_variants = ('A', 'B')

    def seed(self, stack, variant):
        from agent_e2e_local_scenarios.w2g3_common import read_town, town_setup
        if variant not in ('A', 'B'):
            raise ValueError('unsupported variant')
        if 'p11Seed' in stack.extra:
            if stack.extra['p11Seed']['variant'] != variant:
                raise ValueError('case already seeded for a different variant')
            return Seeded(stack.extra['p11Seed'])
        recorder = stack.extra['recorder']
        namespace = 'p11_' + stack.compose_project.replace('-', '')[-16:]
        if 'connectionId' not in stack.extra:
            stack.extra['connectionId'] = stack.extra['ensureConnection']()
        town = town_setup(recorder, variant).prepare(
            variant=variant, namespace=namespace, starting_state='published-town',
            connection_id=stack.extra['connectionId'])
        binding = town['layers']['shelters']
        read_town(recorder, 'p11', town['layers'], 'before')
        recorder.json('p11-jobs-before', 'GET', '/api/v1/admin/jobs?limit=100')
        recorder.json('p11-maps-before', 'GET', '/api/v1/studio/content-items?family=map&limit=200')
        recorder.json('p11-services-before', 'GET', '/api/v1/admin/services/')
        seed = {'variant': variant, 'service': binding['service'], 'layerId': binding['layerId'], 'layers': town['layers']}
        stack.extra['p11Seed'] = seed
        return Seeded(seed)

    def roles(self, stack, seeded, variant, run=None):
        return [RoleBinding('created:job:buffer', 'p11-jobs-after', items='/items', id='/jobId',
                            before='p11-jobs-before',
                            created_by={'tool':'honua_execute_plan', 'pointer':'/result/structuredContent/jobId'}),
                RoleBinding('created:service:buffer', 'p11-services-after', items='/data', id='/serviceName',
                            before='p11-services-before',
                            created_by={'tool':'honua_publish_result', 'pointer':'/result/structuredContent/serviceId'}),
                RoleBinding('child:layer:published', 'p11-service-meta', pointer='/layers/0/id',
                            parent='created:service:buffer', parent_link={'at': {'pathSegment': 2}, 'pointer': '/serviceName'}),
                RoleBinding('created:map:result', 'p11-maps-after', items='/data/items', id='/itemId',
                            before='p11-maps-before',
                            created_by={'tool':'honua_studio_create_draft', 'pointer':'/result/structuredContent/itemId'}),
                RoleBinding('child:layer:result', 'p11-map-version', pointer='/data/envelope/body/layers/0/id',
                            parent='created:map:result', parent_link={'at': {'pathSegment': 4}, 'pointer': '/data/itemId'}),
                *[RoleBinding(f'seeded:{kind}:{layer}', f'publish-{layer.replace("_", "-")}-submit',
                            pointer=f'/data/resourceIds/{field}')
                for layer in ('parcels', 'flood_zones', 'shelters')
                for kind, field in (('service', 'serviceName'), ('layer', 'layerId'))]]

    def observe(self, stack, seeded, variant, run=None):
        from agent_e2e_local_scenarios.w2g3_common import read_town
        recorder = stack.extra['recorder']
        read_town(recorder, 'p11', seeded.state['layers'], 'after')
        listing = recorder.json('p11-jobs-after', 'GET', '/api/v1/admin/jobs?limit=100')
        live = listing['observation']['body']
        maps = recorder.json('p11-maps-after', 'GET', '/api/v1/studio/content-items?family=map&limit=200')
        map_rows = maps['observation']['body'].get('data', {}).get('items', [])
        services = recorder.json('p11-services-after', 'GET', '/api/v1/admin/services/')
        service_rows = services['observation']['body'].get('data', [])
        publish_candidates = []
        map_candidates = []
        candidates = []
        for index, action in enumerate(recorder.actions()):
            if action.kind != 'tool_call' or action.status != 'pass' or action.tool not in ('honua_execute_plan', 'honua_studio_create_draft', 'honua_publish_result'):
                continue
            response = recorder.action_result(f'p11-action-{index}', action)
            value = response.json.get('result', {}).get('structuredContent', {})
            if action.tool == 'honua_publish_result':
                service = value.get('serviceId') if isinstance(value, dict) else None
                if isinstance(service, str) and value.get('status') == 'Completed':
                    if any(row.get('serviceName') == service for row in service_rows):
                        publish_candidates.append((service, action))
                continue
            if action.tool == 'honua_studio_create_draft':
                item = value.get('itemId') if isinstance(value, dict) else None
                for row in map_rows:
                    if row.get('itemId') == item and row.get('currentVersionId'):
                        map_candidates.append((item, row))
                continue
            job = value.get('jobId') if isinstance(value, dict) else None
            if isinstance(job, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', job):
                if any(row.get('jobId') == job for row in live.get('items', [])):
                    candidates.append((job, action))
        if len(candidates) == 1:
            job, action = candidates[0]
            recorder.action_result('p11-job-action', action)
            recorder.json('p11-job-detail', 'GET', '/api/v1/admin/jobs/' + quote(job, safe=''))
        if len(map_candidates) == 1:
            item, row = map_candidates[0]
            version = row['currentVersionId']
            if all(isinstance(x, str) and re.fullmatch(r'[0-9a-fA-F-]{36}', x) for x in (item, version)):
                recorder.json('p11-map-version', 'GET', '/api/v1/studio/content-items/' + item + '/versions/' + version)
                if row.get('workspaceId') and row.get('ownerId'):
                    scope = urlencode({'family':'map','workspaceId':row['workspaceId'],'owner':row['ownerId'],'limit':200})
                    recorder.json('p11-map-current', 'GET', '/api/v1/studio/content-items?' + scope)
        if len(publish_candidates) == 1:
            service, action = publish_candidates[0]
            recorder.action_result('p11-result-publish', action)
            meta = recorder.json('p11-service-meta', 'GET', '/rest/services/' + quote(service, safe='') + '/FeatureServer?f=json')
            layers = meta['observation']['body'].get('layers', [])
            if len(layers) == 1 and type(layers[0].get('id')) is int:
                layer = layers[0]['id']
                from agent_e2e_p10_observer import source_path
                recorder.json('p11-result-feature', 'GET', source_path({'service':service,'layerId':layer}))
                recorder.mcp_call('p11-result-geojson', 'honua_query_features', {
                    'serviceId':service,'layerId':layer,'where':'1=1','outFields':['*'],
                    'limit':1000,'resultOffset':0,'returnGeometry':True,'outSrid':3857,'geometryPrecision':-1})
        return [Observation('job.completed', None, {'projection': 'extract/v1'}),
                Observation('buffer.metric', None, {'projection': 'extract/v1'}),
                Observation('map.composition', None, {'projection': 'extract/v1'}),
                Observation('source-content.unchanged', None, {'projection': 'extract/v1'})]


    def handoff(self, stack, seeded, variant, run=None):
        return {'object': {'resources': {'list': []}, 'facts': {'object': {
            'counts': {'object': {'features': {'body': 'p11-result-geojson',
                                               'pointer': '/result/structuredContent/returnedCount'}}}}}}}


scenario = P11()
