"""P03 remains unsupported.

The frozen release rules prohibit multi-tenant work; the tenant.denied assertion requires a second tenant and an authorized positive control. The runner needs an approved isolated tenant fixture and probes.
"""
from agent_e2e_local_api import Scenario


class P03(Scenario):
    id = 'P03'
    supported_variants = ()

    def seed(self, stack, variant):
        raise NotImplementedError('P03: see module docstring')

    def observe(self, stack, seeded, variant, run=None):
        raise NotImplementedError('P03: see module docstring')


scenario = P03()
