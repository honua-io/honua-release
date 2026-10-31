"""P06 unsupported: core needs seeded inventory-member roles and structured geometry SRID evidence.

The runner needs a projection that aggregates all sealed model query replays for
the same service and layer. Imported typed GeoJSON fields also depend on
honua-server #6815. The controller never publishes the agent's services.
"""
from pathlib import Path
import hashlib
import json
import re
from urllib.parse import quote

from agent_e2e_local_api import Observation, Scenario, Seeded
from agent_e2e_local_expectations import load_roles

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / 'certification/agent-e2e/corpus'
TARGETS = ('parcels', 'shelters', 'survey_points')
FIELDS = {'parcels': ('source_id', 'name', 'owner', 'value'),
          'shelters': ('source_id', 'name', 'capacity', 'is_open'),
          'survey_points': ('source_id', 'name', 'reading')}




class P06(Scenario):
    id = 'P06'
    supported_variants = ()

    def seed(self, stack, variant):
        from agent_e2e_scenario_operations import completed_import, discover_tables

        if variant not in ('A', 'B'):
            raise ValueError('unsupported variant')
        previous = stack.extra.get('p06Seed')
        if previous is not None:
            if previous['variant'] != variant:
                raise ValueError('case already seeded for another variant')
            return Seeded(previous)
        recorder = stack.extra['recorder']
        connection = stack.extra.get('connectionId')
        if connection is None:
            connection = stack.extra['ensureConnection']()
            stack.extra['connectionId'] = connection
        namespace = 'p06_' + stack.compose_project.replace('-', '')[-16:]
        manifest = json.loads((CORPUS / 'manifest.v1.json').read_text())
        pins = {row['path']: row['sha256'] for row in manifest['files']}
        layers = {}
        for name in ('parcels', 'flood_zones', 'shelters', 'survey_points'):
            relative = f'town-{variant}/{name}.3857.geojson'
            raw = (CORPUS / relative).read_bytes()
            if hashlib.sha256(raw).hexdigest() != pins.get(relative):
                raise ValueError('survey setup fixture digest mismatch')
            table = namespace + '_' + name
            step = name.replace('_', '-')
            before = discover_tables(recorder, connection, 'p06-' + step + '-before')
            accepted = recorder.upload('p06-' + step + '-import', raw, table=table,
                                       schema='honua_data', srid=3857)
            layers[name] = completed_import(recorder, accepted, table=table,
                feature_count=len(json.loads(raw)['features']), step='p06-' + step + '-complete',
                before_tables=before, connection=connection)
        if not stack.extra.get('p06DiscoveryOnly'):
            stack.extra['addPrincipal']('p06-viewer', ['read:*'])
            for name in TARGETS:
                binding = layers[name]
                schema, table = binding['schema'], binding['table']
                if not all(isinstance(v, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,62}', v) for v in (schema, table)):
                    raise ValueError('invalid fixture table identity')
                step = name.replace('_', '-')
                identity = recorder.table_inventory('p06-' + step + '-table', schema)
                if not any(row.get('table_name') == table and row.get('table_schema') == schema for row in identity):
                    raise ValueError('fixture table was not independently observed')
                rows = recorder.table_rows('p06-' + step + '-source-before', schema, table, (*FIELDS[name], 'geometry'))
                if not isinstance(rows, list) or not rows:
                    raise ValueError('fixture source snapshot unavailable')
        before = recorder.json('p06-services-before', 'GET', '/api/v1/admin/services')
        body = before['observation']['body']
        if before['observation']['httpStatus'] != 200 or not isinstance(body, dict) or not isinstance(body.get('data'), list):
            raise ValueError('service baseline unavailable')
        state = {'variant': variant, 'layers': layers}
        stack.extra['p06Seed'] = state
        return Seeded(state)

    def roles(self, stack, seeded, variant, run=None):
        return load_roles(self.id)

    def observe(self, stack, seeded, variant, run=None):
        recorder = stack.extra['recorder']
        for name in TARGETS:
            binding = seeded.state['layers'][name]
            recorder.table_rows('p06-' + name.replace('_', '-') + '-source-after', binding['schema'], binding['table'], (*FIELDS[name], 'geometry'))
        recorder.json('p06-services-after', 'GET', '/api/v1/admin/services')
        actions = [action for action in recorder.actions()
                   if action.kind == 'tool_call' and action.tool == 'honua_publish_service']
        for name in TARGETS:
            table = seeded.state['layers'][name]['table']
            matching = [action for action in actions if isinstance(action.arguments, dict)
                        and action.arguments.get('table') == table]
            if not matching:
                continue
            label = name.replace('_', '-')
            report = recorder.action_result('p06-' + label + '-publish', matching[-1])
            message = report.json if report.status == 200 else None
            result = message.get('result', {}) if isinstance(message, dict) else {}
            content = result.get('structuredContent') if isinstance(result, dict) else None
            if not isinstance(content, dict) or result.get('isError', False) is not False or content.get('status') != 'Completed':
                continue
            service, layer = content.get('serviceName'), content.get('layerId')
            if not isinstance(service, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', service):
                continue
            if not isinstance(layer, (int, str)) or not str(layer).isdigit():
                continue
            description = recorder.mcp_call('p06-' + label + '-describe', 'honua_describe_layer',
                              {'serviceId': service, 'layerId': int(layer)})
            described = description.get('structuredContent') if isinstance(description, dict) else None
            if not isinstance(described, dict) or description.get('isError', False) is not False:
                continue
            service_id = described.get('serviceId')
            if not isinstance(service_id, str) or not service_id:
                continue
            if name == 'survey_points':
                survey_actions = [action for action in recorder.actions()
                    if action.kind == 'tool_call' and action.tool == 'honua_query_features'
                    and isinstance(action.arguments, dict)
                    and str(action.arguments.get('serviceId')) == service_id
                    and str(action.arguments.get('layerId')) == str(layer)]
                for index, action in enumerate(survey_actions):
                    recorder.replay('p06-survey-replay-' + str(index), action)
            offsets = (0, 1000, 2000) if name == 'survey_points' else (0,)
            for offset in offsets:
                recorder.mcp_call('p06-' + label + '-query-' + str(offset), 'honua_query_features',
                    {'serviceId': service_id, 'layerId': int(layer), 'where': '1=1',
                     'limit': 1000, 'resultOffset': offset, 'returnGeometry': False})
            path = '/rest/services/' + quote(service, safe='') + '/FeatureServer/' + str(int(layer))
            recorder.json('p06-' + label + '-read', 'GET', path + '?f=json')
            recorder.json('p06-' + label + '-write', 'POST', path + '/applyEdits?f=json',
                          {'adds': [], 'updates': [], 'deletes': []}, principal='p06-viewer')
        return [Observation(name, None, {'projection': 'extract/v1'})
                for name in ('service.query', 'pagination.complete', 'service.write-denied', 'source-content.unchanged')]


scenario = P06()
