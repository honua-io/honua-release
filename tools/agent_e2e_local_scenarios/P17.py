"""The runner rejects needs_approver before setup. It must expose the actual requester credential, a distinct approver, an owned pending proposal and a specific same-proposal self-approval denial, with independent worker lifecycle evidence.

The case stays unsupported until every pack assertion can be independently scored.
"""
from __future__ import annotations

REASON = 'The runner rejects needs_approver before setup. It must expose the actual requester credential, a distinct approver, an owned pending proposal and a specific same-proposal self-approval denial, with independent worker lifecycle evidence.'

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
