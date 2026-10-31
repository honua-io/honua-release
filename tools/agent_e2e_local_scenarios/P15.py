"""The runner needs a verified Studio saved-map fixture, operator membership and tenant-bound outsider identity. Its current addPrincipal mints tenant-independent API keys and custody labels key IDs as worker IDs. It must record real isolated bootstrap and task worker lifecycles before enabling the complete case.

The case stays unsupported until every pack assertion can be independently scored.
"""
from __future__ import annotations

REASON = 'The runner needs a verified Studio saved-map fixture, operator membership and tenant-bound outsider identity. Its current addPrincipal mints tenant-independent API keys and custody labels key IDs as worker IDs. It must record real isolated bootstrap and task worker lifecycles before enabling the complete case.'

from agent_e2e_local_api import Scenario as BaseScenario
from agent_e2e_local_projections_w2g4 import observe_partial, role_bindings


class LocalScenario(BaseScenario):
    id = "P15"
    supported_variants = ()

    def seed(self, stack, variant):
        self.expected(variant)  # validate the development variant before touching the stack
        raise NotImplementedError("P15: " + REASON)

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
