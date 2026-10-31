"""P06 read-only service: unsupported until model-created services can be located.

The runner needs a controller-readable service inventory with owner or creation
baseline and a projection for polygon/multipoint features and permission probes.
The survey fixture is seeded alongside the imported town; no publication is
created for the agent. No model-authored identity is treated as evidence.
"""
from pathlib import Path

from agent_e2e_local_api import Scenario, Seeded

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / 'certification/agent-e2e/corpus'


class P06(Scenario):
    id = 'P06'
    supported_variants = ()

    def seed(self, stack, variant):
        from agent_e2e_scenario_backend import TownSetup
        from agent_e2e_scenario_operations import completed_import, discover_tables
        import json

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
        town = TownSetup(recorder, CORPUS / 'manifest.v1.json').prepare(
            variant=variant, namespace=namespace, starting_state='imported-town', connection_id=connection)
        source = CORPUS / f'town-{variant}' / 'survey_points.3857.geojson'
        raw = source.read_bytes()
        table = namespace + '_survey_points'
        before = discover_tables(recorder, connection, 'p06-survey-before-tables')
        accepted = recorder.upload('p06-survey-import', raw, table=table, schema='honua_data', srid=3857)
        imported = completed_import(recorder, accepted, table=table,
            feature_count=len(json.loads(raw)['features']), step='p06-survey-complete',
            before_tables=before, connection=connection)
        state = {'variant': variant, 'town': town, 'survey': imported}
        stack.extra['p06Seed'] = state
        return Seeded(state)

    def observe(self, stack, seeded, variant, run=None):
        raise NotImplementedError('P06: service ownership inventory and assertion projections are unavailable')


scenario = P06()
