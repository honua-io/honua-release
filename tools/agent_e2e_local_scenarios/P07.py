"""P07: seed the town and countries catalog and replay the agent's catalog request.

The local tenant assertion remains preregistered as excluded by the runner.
"""
from pathlib import Path
import hashlib
import json

from agent_e2e_local_api import Observation, Scenario, Seeded
from agent_e2e_local_expectations import load_roles

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / 'certification/agent-e2e/corpus'
LAYERS = ('parcels', 'flood_zones', 'shelters', 'countries')


class P07(Scenario):
    id = 'P07'
    supported_variants = ('A', 'B')

    def seed(self, stack, variant):
        from agent_e2e_scenario_operations import (completed_import, discover_tables,
                                                   publication_request, submit_publication)
        if variant not in self.supported_variants:
            raise ValueError('unsupported variant')
        previous = stack.extra.get('p07Seed')
        if previous is not None:
            if previous['variant'] != variant:
                raise ValueError('case already seeded for another variant')
            return Seeded(previous)
        recorder = stack.extra['recorder']
        connection = stack.extra.get('connectionId') or stack.extra['ensureConnection']()
        stack.extra['connectionId'] = connection
        namespace = 'p07_' + stack.compose_project.replace('-', '')[-16:]
        manifest = json.loads((CORPUS / 'manifest.v1.json').read_text())
        pins = {row['path']: row['sha256'] for row in manifest['files']}
        bindings = {}
        for name in LAYERS:
            relative = ('natural-earth/countries.geojson' if name == 'countries'
                        else f'town-{variant}/{name}.3857.geojson')
            raw = (CORPUS / relative).read_bytes()
            if hashlib.sha256(raw).hexdigest() != pins.get(relative):
                raise ValueError('P07 fixture digest mismatch')
            table = namespace + '_' + name
            srid = 4326 if name == 'countries' else 3857
            step = name.replace('_', '-')
            before = discover_tables(recorder, connection, 'p07-' + step + '-before')
            accepted = recorder.upload('p07-' + step + '-import', raw, table=table, schema='honua_data', srid=srid)
            imported = completed_import(recorder, accepted, table=table,
                feature_count=len(json.loads(raw)['features']), step='p07-' + step + '-complete',
                before_tables=before, connection=connection)
            service = namespace.replace('_', '-') + '-' + step
            geometry = 'Point' if name == 'shelters' else ('MultiPolygon' if name == 'countries' else 'Polygon')
            request = publication_request(connection=connection, schema=imported['schema'], table=imported['table'],
                                          service=service, geometry_type=geometry, srid=srid)
            published = submit_publication(recorder, request, step='p07-' + step + '-publish')
            if published['status'] != 'completed':
                raise ValueError('P07 seed publication requires approval')
            bindings[name] = published['service']
        seed = {'variant': variant, 'services': bindings}
        stack.extra['p07Seed'] = seed
        return Seeded(seed)

    def roles(self, stack, seeded, variant, run=None):
        return load_roles(self.id)

    def observe(self, stack, seeded, variant, run=None):
        recorder = stack.extra['recorder']
        actions = [a for a in recorder.actions() if a.kind == 'tool_call' and a.tool == 'honua_list_layers']
        observations = [Observation('tenant.denied', None, {'projection': 'extract/v1'})]
        if actions:
            original = recorder.action_result('p07-agent-catalog', actions[-1])
            result = recorder.replay('p07-catalog', actions[-1])

            def catalog(value):
                content = value.get('structuredContent') if isinstance(value, dict) else None
                if not (isinstance(content, dict) and value.get('isError', False) is False
                        and isinstance(content.get('layers'), list) and content.get('offset') == 0
                        and content.get('hasMore') is False
                        and content.get('layerCount') == content.get('totalCount') == len(content['layers'])):
                    return None
                names = [row.get('serviceName') for row in content['layers'] if isinstance(row, dict)]
                return sorted(names) if len(names) == len(content['layers']) and all(isinstance(n, str) for n in names) else None

            original_message = original.json if original.status == 200 else None
            seen = catalog(original_message.get('result')) if isinstance(original_message, dict) else None
            live = catalog(result)
            if seen is not None and seen == live:
                observations.insert(0, Observation('catalog.exact', None, {'projection': 'extract/v1'}))
        return observations

    def handoff(self, stack, seeded, variant, run=None):
        features = {'body': 'p07-catalog', 'pointer': '/result/structuredContent/layers'}
        count = {'count': features}
        return {'object': {'resources': {'list': []}, 'facts': {'object': {
            'counts': {'object': {'layers': count, 'services': count}},
            'names': {'map': features, 'to': {'item': '/name'}}}}}}


scenario = P07()
