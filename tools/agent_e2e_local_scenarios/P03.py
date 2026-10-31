"""P03 remains unsupported pending connection owner normalization.

The server records the owner as api-key:<id>, but the runner compares the bare key ID; it needs a verified owner normalization before enabling this case.
"""
import re
from urllib.parse import quote

from agent_e2e_local_api import Observation, Scenario, Seeded
from agent_e2e_local_expectations import load_roles

LAYERS = ('flood_zones', 'parcels', 'shelters', 'survey_points')


class P03(Scenario):
    id = 'P03'
    supported_variants = ()

    def seed(self, stack, variant):
        if variant not in ('A', 'B'):
            raise ValueError('unsupported variant')
        previous = stack.extra.get('p03Seed')
        if previous is not None:
            if previous['variant'] != variant:
                raise ValueError('case already seeded for another variant')
            return Seeded(previous)
        from agent_e2e_local_scenarios.P06 import P06
        town = P06().seed(stack, variant)
        recorder = stack.extra['recorder']
        for name in LAYERS:
            table = town.state['layers'][name]['table']
            schema = town.state['layers'][name]['schema']
            if not all(isinstance(v, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,62}', v) for v in (schema, table)):
                raise ValueError('fixture table identity is invalid')
            rows = recorder.sql('p03-' + name.replace('_', '-') + '-table',
                "SELECT table_name FROM information_schema.tables WHERE table_schema = '" + schema +
                "' AND table_name = '" + table + "'")
            if not isinstance(rows, list) or len(rows) != 1 or rows[0].get('table_name') != table:
                raise ValueError('fixture table was not independently observed')
        stack.extra['databaseHandoff']('town', 'honua_data')
        before = recorder.json('p03-connections-before', 'GET', '/api/v1/admin/connections')
        body = before['observation']['body']
        if before['observation']['httpStatus'] != 200 or not isinstance(body, dict) or not isinstance(body.get('data'), list):
            raise ValueError('connection baseline unavailable')
        state = {'variant': variant, 'baseline': [row['connectionId'] for row in body['data']]}
        stack.extra['p03Seed'] = state
        return Seeded(state)

    def roles(self, stack, seeded, variant, run=None):
        return load_roles(self.id)

    def observe(self, stack, seeded, variant, run=None):
        recorder = stack.extra['recorder']
        after = recorder.json('p03-connections-after', 'GET', '/api/v1/admin/connections')
        body = after['observation']['body']
        rows = body.get('data', []) if isinstance(body, dict) else []
        matches = [row for row in rows if isinstance(row, dict) and row.get('connectionId') not in seeded.state['baseline']
                   and run is not None and row.get('createdBy') == 'api-key:' + run.agent_principal]
        observations = [Observation('tenant.denied', None, {'projection': 'extract/v1'})]
        if len(matches) != 1:
            return observations
        connection = matches[0]['connectionId']
        if not isinstance(connection, str) or not re.fullmatch(r'[0-9a-fA-F-]{36}', connection):
            return observations
        path = '/api/v1/admin/connections/' + quote(connection, safe='')
        probe = recorder.json('p03-connection-probe', 'POST', path + '/test')
        catalog = recorder.json('p03-catalog', 'GET', path + '/tables')
        health = probe['observation']['body']
        if (probe['observation']['httpStatus'] == 200 and isinstance(health, dict)
                and isinstance(health.get('data'), dict) and health['data'].get('isHealthy') is True
                and catalog['observation']['httpStatus'] == 200):
            observations.extend(Observation(name, None, {'projection': 'extract/v1'})
                                for name in ('connection.verified', 'catalog.exact'))
        return observations

    def handoff(self, stack, seeded, variant, run=None):
        tables = {'body': 'p03-catalog', 'pointer': '/tables/*/table'}
        return {'object': {'resources': {'list': []}, 'facts': {'object': {'names': tables}}}}


scenario = P03()
