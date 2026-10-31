"""P04 remains unsupported.

Customer files are declared. Independent scoring still needs an owner-bound imported-resource inventory and a frozen projection for polygon features and protected attachment snapshots.
"""
from agent_e2e_local_api import Scenario


class P04(Scenario):
    id = 'P04'
    supported_variants = ()

    def seed(self, stack, variant):
        raise NotImplementedError('P04: see module docstring')

    def observe(self, stack, seeded, variant, run=None):
        raise NotImplementedError('P04: see module docstring')


scenario = P04()
