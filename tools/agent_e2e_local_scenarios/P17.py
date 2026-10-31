"""Blocked by the immutable core role resolver: the native proposal list returns only proposals, with no total or cursor. A model-created pending proposal cannot satisfy the required complete seed/observe enumeration. The separate approver hook exists, but this case must not approve its own pending proposal.
"""
from __future__ import annotations

REASON = 'Blocked by the immutable core role resolver: the native proposal list returns only proposals, with no total or cursor. A model-created pending proposal cannot satisfy the required complete seed/observe enumeration. The separate approver hook exists, but this case must not approve its own pending proposal.'

from agent_e2e_local_api import Scenario as BaseScenario
from agent_e2e_local_projections_w2g4 import observe_partial, role_bindings


class LocalScenario(BaseScenario):
    id = "P17"
    supported_variants = ()

    def seed(self, stack, variant):
        self.expected(variant)  # validate the development variant before touching the stack
        raise NotImplementedError("P17: " + REASON)

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
