"""P05 remains unsupported.

The runner exposes no model-visible fixture-file channel or controller-verifiable invalid-row reporting artifact. The runner needs an input handoff and independently recorded report evidence.
"""
from agent_e2e_local_api import Scenario


class P05(Scenario):
    id = 'P05'
    supported_variants = ()

    def seed(self, stack, variant):
        raise NotImplementedError('P05: see module docstring')

    def observe(self, stack, seeded, variant, run=None):
        raise NotImplementedError('P05: see module docstring')


scenario = P05()
