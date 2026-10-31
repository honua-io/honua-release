"""The entire case is excluded on local Docker by the frozen preregistration because multi-tenant evaluation is out of scope. The runner must retain every assertion as excluded, never as passed. No tenant fixtures are provisioned.
"""
from __future__ import annotations

REASON = 'The entire case is excluded on local Docker by the frozen preregistration because multi-tenant evaluation is out of scope. The runner must retain every assertion as excluded, never as passed. No tenant fixtures are provisioned.'

from agent_e2e_local_api import Scenario as BaseScenario
from agent_e2e_local_projections_w2g4 import observe_partial, role_bindings


class LocalScenario(BaseScenario):
    id = "P18"
    supported_variants = ('A', 'B')

    def seed(self, stack, variant):
        self.expected(variant)  # validate the development variant before touching the stack
        raise NotImplementedError("P18: " + REASON)

    def needs_approver(self):
        return False

    def roles(self, stack, seeded, variant, run=None):
        return role_bindings(self.id, seeded)

    def observe(self, stack, seeded, variant, run=None):
        return observe_partial(self.id, stack, seeded, variant)

    def cleanup(self, stack, seeded):
        if seeded.state:
            raise ValueError("Unsupported scenario cannot claim cleanup ownership")


scenario = LocalScenario()
