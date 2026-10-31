"""P02 remains unsupported pending independent evidence for all three assertions.

The recorded role hashes the pre-restart inventory with a sha256: prefix,
while canonicalSha256 projects the post-restart inventory without it, so the
exact configuration predicate fails on identical data. Secret scans require
a canary planted in a real secret field without seeding the requested setup.
The runner-owned lifecycle fact independently proves the restart.
"""
from agent_e2e_local_api import Observation, Scenario, Seeded
from agent_e2e_scenario_operations import body


class P02(Scenario):
    id = 'P02'
    supported_variants = ()

    def sessions_required(self):
        return 2

    def between_sessions(self, stack, seeded, variant, after_session):
        if after_session != 1 or seeded.state.get('variant') != variant:
            raise ValueError('invalid P02 between-session state')
        body(stack.extra['recorder'].json('p02-connections-before-restart', 'GET', '/api/v1/admin/connections/'))
        return {'restartServer': True}

    def seed(self, stack, variant):
        if variant not in ('A', 'B'):
            raise ValueError('unsupported development variant')
        if 'p02Seed' not in stack.extra:
            response = stack.extra['recorder'].json('p02-connections-seed', 'GET', '/api/v1/admin/connections/')
            body(response)
            stack.extra['p02Seed'] = {'variant': variant}
        if stack.extra['p02Seed']['variant'] != variant:
            raise ValueError('case already seeded for a different variant')
        return Seeded(stack.extra['p02Seed'].copy())

    def observe(self, stack, seeded, variant, run=None):
        if seeded.state.get('variant') != variant:
            raise ValueError('seed variant differs')
        recorder = stack.extra['recorder']
        body(recorder.json('p02-connections-after-restart', 'GET', '/api/v1/admin/connections/'))
        return [Observation('configuration.persisted', None, {'projection': 'extract/v1'}),
                Observation('restart.observed', None, {'projection': 'extract/v1'})]


scenario = P02()
