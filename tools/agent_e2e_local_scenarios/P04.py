"""P04 remains unsupported.

The runner exposes neither a model-visible fixture-file channel nor a polygon feature projection. The runner needs a bound input handoff and a registered polygon projection over controller reads.
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
