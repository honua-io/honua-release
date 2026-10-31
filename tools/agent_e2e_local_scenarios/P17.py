"""No self approval: retain the exact saved map and read any native proposal.

Blocked until a complete reviewed proposal binding and same-proposal enforcement
probe are available. This case never uses the separate approver to resolve it.
"""
from __future__ import annotations

from agent_e2e_local_api import Scenario
from agent_e2e_local_projections_w2g4 import (observe_partial, observe_native_proposal,
    role_bindings, seed_saved_map, source_and_custody)


class P17(Scenario):
    id = 'P17'
    supported_variants = ()

    def seed(self, stack, variant):
        self.expected(variant)
        return seed_saved_map(stack, variant)

    def needs_approver(self):
        return True

    def roles(self, stack, seeded, variant, run=None):
        return role_bindings(self.id, seeded)

    def observe(self, stack, seeded, variant, run=None):
        source_and_custody(stack, seeded)
        observe_native_proposal(stack, seeded, 'p17')
        return observe_partial(self.id, stack, seeded, variant)


scenario = P17()
