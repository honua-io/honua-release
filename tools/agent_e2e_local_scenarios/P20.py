"""Blocked by the frozen expectation interface: clarification reply IDs vary with preceding model actions, and no role can bind run.clarifications. Core assigns a topic to any question rather than observing the topic asked, and exposes no publication-after-answers fact. Freezing one transcript position or an empty answer ID list would produce invalid evidence.
"""
from __future__ import annotations

REASON = 'Blocked by the frozen expectation interface: clarification reply IDs vary with preceding model actions, and no role can bind run.clarifications. Core assigns a topic to any question rather than observing the topic asked, and exposes no publication-after-answers fact. Freezing one transcript position or an empty answer ID list would produce invalid evidence.'

from agent_e2e_local_api import Scenario as BaseScenario
from agent_e2e_local_projections_w2g4 import observe_partial, role_bindings


class LocalScenario(BaseScenario):
    id = "P20"
    supported_variants = ()

    def seed(self, stack, variant):
        self.expected(variant)  # validate the development variant before touching the stack
        raise NotImplementedError("P20: " + REASON)

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
