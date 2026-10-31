"""The runner rejects needs_approver before setup; it must provision a distinct approver principal and bind a real proposal, audit record, approved version and anonymous route. Worker lifecycle custody must also identify actual separate workers.

The case stays unsupported until every pack assertion can be independently scored.
"""
from __future__ import annotations

REASON = 'The runner rejects needs_approver before setup; it must provision a distinct approver principal and bind a real proposal, audit record, approved version and anonymous route. Worker lifecycle custody must also identify actual separate workers.'

from agent_e2e_local_api import Scenario as BaseScenario
from agent_e2e_local_projections_w2g4 import observe_partial, role_bindings


class LocalScenario(BaseScenario):
    id = "P14"
    supported_variants = ()

    def seed(self, stack, variant):
        self.expected(variant)  # validate the development variant before touching the stack
        raise NotImplementedError("P14: " + REASON)

    def needs_approver(self):
        return True

    def roles(self, stack, seeded, variant, run=None):
        return role_bindings(self.id, seeded)

    def observe(self, stack, seeded, variant, run=None):
        return observe_partial(self.id, stack, seeded, variant)

    def cleanup(self, stack, seeded):
        if seeded.state:
            raise ValueError("Unsupported scenario cannot claim cleanup ownership")


scenario = LocalScenario()
