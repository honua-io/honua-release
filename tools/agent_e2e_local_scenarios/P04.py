"""P04 remains unsupported.

The runner needs controller reads of un-published imported table contents and before/after attachment bytes; featureServerFeatures requires a published layer.
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
