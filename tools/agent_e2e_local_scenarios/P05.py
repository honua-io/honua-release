"""P05 remains unsupported.

Customer files are declared and action_result can retain an import report. The runner still needs an owner-bound imported-resource inventory and a projection binding row-number errors to fixture source IDs and live imported features.
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
