"""P03 remains unsupported.

The tenant assertion is preregistered as excluded. The connection list response has no total or cursor, so the runner cannot prove the complete baseline required to bind a model-created connection. A complete owner-bearing enumeration is needed.
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
