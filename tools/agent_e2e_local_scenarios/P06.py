"""P06 read-only service: unsupported until model-created services can be located.

The runner needs a per-role source selector for multiple services authored by the same tool.
The survey fixture is seeded alongside the imported town; no publication is
created for the agent. No model-authored identity is treated as evidence.
"""
from pathlib import Path
import hashlib
import json

from agent_e2e_local_api import Scenario, Seeded

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / 'certification/agent-e2e/corpus'


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
        before = recorder.json('p06-services-before', 'GET', '/api/v1/admin/services')
        body = before['observation']['body']
        if before['observation']['httpStatus'] != 200 or not isinstance(body, dict) or not isinstance(body.get('data'), list):
            raise ValueError('service baseline unavailable')
        state = {'variant': variant, 'layers': layers}
        stack.extra['p06Seed'] = state
        return Seeded(state)

    def observe(self, stack, seeded, variant, run=None):
        recorder = stack.extra['recorder']
        recorder.json('p06-services-after', 'GET', '/api/v1/admin/services')
        actions = [action for action in recorder.actions()
                   if action.kind == 'tool_call' and action.tool == 'honua_publish_service']
        for ordinal, action in enumerate(actions, 1):
            recorder.action_result(f'p06-publish-{ordinal:03}', action)
        return []


scenario = P06()
