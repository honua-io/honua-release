"""P07 remains unsupported.

The frozen release rules prohibit multi-tenant work; tenant.denied needs a second tenant and an authorized positive control. The runner needs an approved isolated tenant fixture and probes.
"""
from agent_e2e_local_api import Scenario


class P07(Scenario):
    id = 'P07'
    supported_variants = ()

    def seed(self, stack, variant):
        raise NotImplementedError('P07: see module docstring')

    def observe(self, stack, seeded, variant, run=None):
        raise NotImplementedError('P07: see module docstring')


scenario = P07()
