"""P03 database discovery remains unsupported.

The local customer surface needs a connection-creation tool and a seeded database
handoff; the controller records the reviewed unpaged connection inventory before
and after the session, but cannot substitute its own connection for the agent's.
"""
from agent_e2e_local_api import Scenario, Seeded


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
        stack.extra['recorder'].json('p03-connections-before', 'GET', '/api/v1/admin/connections')
        state = {'variant': variant}
        stack.extra['p03Seed'] = state
        return Seeded(state)

    def observe(self, stack, seeded, variant, run=None):
        stack.extra['recorder'].json('p03-connections-after', 'GET', '/api/v1/admin/connections')
        return []


scenario = P03()
